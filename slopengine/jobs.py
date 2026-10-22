"""Persistent, single-owner queue and serialized stage resource admission.

Workers never share pipeline objects. A permit is reserved before it is sent;
only a stage completion or a reaped process releases it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import threading
import uuid

import psutil

from .config_core import PipelineConfig
from .processes import WorkerProcess, read_logs
from .settings import build_config, state_dir

GiB = 1024 ** 3


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    HELD = "HELD"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    CANCELLING = "CANCELLING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class StageStatus(StrEnum):
    PENDING = "PENDING"
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}
STAGES = ("script", "narration", "timestamps", "footage", "render", "save", "upload")
PATH_FIELDS = {"output_dir", "temp_root", "background_music_path", "youtube_client_secrets_file", "youtube_token_file"}
SECRET_FIELDS = {"gemini_api_key", "pexels_api_key"}


@dataclass(frozen=True)
class ResourceLimits:
    max_active_jobs: int = 2
    max_concurrent_renders: int = 1
    max_concurrent_gpu_whisper: int = 1
    system_headroom_gib: float = 3.0
    # Rendering measured 5.99 GiB; reserve 7 GiB including margin.
    stage_memory_gib: dict = field(default_factory=lambda: {
        "startup": 1.5, "script": 1.5, "narration": 1.5,
        "timestamps": 3.0, "footage": 1.5, "render": 7.0,
        "save": 1.5, "upload": 1.5,
    })

    def __post_init__(self):
        import math
        if any(type(v) is not int or v < 1 for v in (
            self.max_active_jobs, self.max_concurrent_renders, self.max_concurrent_gpu_whisper)):
            raise ValueError("Concurrency limits must be positive integers.")
        if not math.isfinite(self.system_headroom_gib) or self.system_headroom_gib < 0:
            raise ValueError("System memory headroom must be finite and nonnegative.")
        if set(self.stage_memory_gib) != set(STAGES) | {"startup"} or any(
            not math.isfinite(v) or v <= 0 for v in self.stage_memory_gib.values()):
            raise ValueError("Supply a positive memory estimate for every stage and startup.")


class ResourceManager:
    def __init__(self, limits: ResourceLimits, memory_available=None):
        self.limits = limits
        self.memory_available = memory_available or (lambda: psutil.virtual_memory().available)
        self.leases: dict[str, tuple[str, int, bool]] = {}

    def requirement(self, stage, config):
        estimate = self.limits.stage_memory_gib[stage]
        # Large Whisper models need more host RAM even when GPU is selected.
        if stage == "timestamps":
            model = config.get("whisper_model", "base").removesuffix(".en")
            estimate = max(estimate, {"tiny": 3, "base": 3, "small": 4, "medium": 7,
                                      "large": 12, "large-v1": 12, "large-v2": 12,
                                      "large-v3": 12, "large-v3-turbo": 7, "turbo": 7}.get(model, 12))
        if stage == "render":
            estimate *= max(1, config.get("width", 1080) * config.get("height", 1920) / (1080 * 1920))
        return int(estimate * GiB)

    def waiting_reason(self, job_id, stage, config, usage):
        others = [lease for key, lease in self.leases.items() if key != job_id]
        gpu = stage == "timestamps" and config.get("whisper_device", "cpu").startswith("cuda")
        if stage == "render" and sum(s == "render" for s, _, _ in others) >= self.limits.max_concurrent_renders:
            return "Waiting for render slot"
        if gpu and sum(g for _, _, g in others) >= self.limits.max_concurrent_gpu_whisper:
            return "Waiting for GPU resource"
        if stage == "upload" and any(s == "upload" for s, _, _ in others):
            return "Waiting for upload slot"
        # Available RAM already excludes resident usage. Charge only unconsumed
        # reservations, and the requesting process's incremental stage demand.
        outstanding = sum(max(0, size - usage.get(key, 0))
                          for key, (_, size, _) in self.leases.items() if key != job_id)
        required = max(0, self.requirement(stage, config) - usage.get(job_id, 0))
        if self.memory_available() - outstanding - required < self.limits.system_headroom_gib * GiB:
            return "Waiting for available memory"
        return ""

    def can_drain(self, job_id, stage, config):
        lease = self.leases.get(job_id)
        return (stage not in {"render", "timestamps", "startup"} and lease is not None
                and self.requirement(stage, config) <= lease[1])

    def reserve(self, job_id, stage, config, usage, *, allow_drain=False):
        reason = self.waiting_reason(job_id, stage, config, usage)
        # An already reserved light stage may finish under pressure, freeing its
        # worker. Heavy stages always require a fresh headroom check.
        if reason == "Waiting for available memory" and allow_drain and self.can_drain(job_id, stage, config):
            reason = ""
        if not reason:
            self.leases[job_id] = (stage, self.requirement(stage, config),
                                  stage == "timestamps" and config.get("whisper_device", "cpu").startswith("cuda"))
        return reason


class JobManager:
    def __init__(self, log_dir: Path | None = None, *, db_path: Path | None = None,
                 limits: ResourceLimits | None = None, memory_available=None, worker_factory=None):
        self.log_dir = Path(log_dir) if log_dir is not None else state_dir() / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path else (self.log_dir.parent if log_dir is None else self.log_dir) / "jobs.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._workers = {}
        self._credentials = {}
        self._last_id = None
        self._factory = worker_factory or WorkerProcess
        # An OS lock prevents two TUI instances admitting against the same DB.
        self._owner = self.db_path.with_suffix(".lock").open("a+")
        if os.name != "posix":
            self._owner.close()
            raise RuntimeError("Concurrent job management currently requires POSIX process groups.")
        import fcntl
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._owner.close()
            raise RuntimeError("This job queue is already managed by another application.") from None
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db_path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, topic TEXT NOT NULL, config TEXT NOT NULL,
                output TEXT NOT NULL UNIQUE, temp_dir TEXT NOT NULL, log_path TEXT,
                status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
                stage_states TEXT NOT NULL, waiting_reason TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL, created TEXT NOT NULL, pid INTEGER, process_created REAL,
                message TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        saved = self.db.execute("SELECT value FROM settings WHERE key='limits'").fetchone()
        self.limits = limits or (ResourceLimits(**json.loads(saved[0])) if saved else ResourceLimits())
        self.resources = ResourceManager(self.limits, memory_available)
        self._recover()

    def _recover(self):
        for row in self.db.execute("SELECT * FROM jobs WHERE status IN ('RUNNING','WAITING','CANCELLING')").fetchall():
            self._reap_orphan(dict(row))
            self._cleanup(dict(row))
            states = json.loads(row["stage_states"])
            for stage in states:
                if states[stage] in {"RUNNING", "WAITING"}:
                    states[stage] = "INTERRUPTED"
            self.db.execute("UPDATE jobs SET status='INTERRUPTED', waiting_reason='', pid=NULL, stage_states=?, message=? WHERE id=?",
                            (json.dumps(states), "Interrupted by application shutdown; inspect output before explicitly retrying.", row["id"]))
        self.db.execute("UPDATE jobs SET status='HELD', waiting_reason='Restored queue; explicit retry required', message='Pending job restored; retry explicitly to generate. Upload will be off.' WHERE status='QUEUED'")
        self.db.commit()

    @staticmethod
    def _reap_orphan(row):
        pid, created = row.get("pid"), row.get("process_created")
        if not pid or not created:
            return
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - created) < .01 and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
                return
        except (psutil.Error, OSError):
            pass
        # A dead leader can leave descendants. Identify these by the inherited
        # private job token AND original process group, never by PID alone.
        for process in psutil.process_iter():
            try:
                if (os.getpgid(process.pid) == pid and process.create_time() >= created and
                    process.environ().get("SLOPENGINE_JOB_ID") == row["id"]):
                    os.killpg(pid, signal.SIGKILL)
                    return
            except (psutil.Error, OSError):
                continue

    def _cleanup(self, row, *, keep_temp=False):
        if not keep_temp:
            shutil.rmtree(row["temp_dir"], ignore_errors=True)
        # Staging is named by job, so SIGKILL cleanup cannot touch a peer.
        staging = Path(row["output"]).parent / (".slopengine-" + row["id"])
        shutil.rmtree(staging, ignore_errors=True)

    @property
    def running(self):
        with self._lock:
            return not self._closed and self.db.execute("SELECT 1 FROM jobs WHERE status IN ('QUEUED','RUNNING','WAITING','CANCELLING') LIMIT 1").fetchone() is not None

    @property
    def process(self):
        worker = self._workers.get(self._last_id)
        return worker.process if worker else getattr(self, "_last_process", None)

    @property
    def log_path(self):
        if not self._last_id:
            return None
        row = self.db.execute("SELECT log_path FROM jobs WHERE id=?", (self._last_id,)).fetchone()
        return Path(row[0]) if row and row[0] else None

    @staticmethod
    def _signal_process(process, sig):
        WorkerProcess._signal_process(process, sig)

    def list_jobs(self):
        with self._lock:
            rows = []
            for row in self.db.execute("SELECT * FROM jobs ORDER BY priority, created, id"):
                item = dict(row)
                item["config"] = json.loads(item["config"])
                item["stage_states"] = json.loads(item["stage_states"])
                rows.append(item)
            return rows

    def configure(self, **changes):
        with self._lock:
            values = {**asdict(self.limits), **changes}
            limits = ResourceLimits(**values)
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('limits', ?)", (json.dumps(asdict(limits)),))
            self.db.commit()
            self.limits = self.resources.limits = limits

    def start(self, topic, output=None, overrides=None, *, config=None):
        with self._lock:
            if self._closed:
                raise RuntimeError("Job manager is closed.")
            if not topic.strip():
                raise ValueError("Enter a topic.")
            config = config or build_config(overrides=overrides)
            snapshot = asdict(config)
            credentials = {key: snapshot.pop(key) for key in SECRET_FIELDS}
            job_id = uuid.uuid4().hex
            target = (Path(output).expanduser() if output else config.output_dir / (job_id + ".mp4")).resolve()
            if target.suffix.lower() != ".mp4" or target.exists() or target.with_suffix(".metadata.json").exists():
                raise ValueError("Output must be a new .mp4 destination.")
            snapshot["temp_root"] = str(Path(config.temp_root).resolve() / job_id)
            # Paths are resolved at enqueue, independent of future working dirs.
            for name in PATH_FIELDS:
                if snapshot.get(name) is not None:
                    snapshot[name] = str(Path(snapshot[name]).expanduser().resolve())
            priority = self.db.execute("SELECT COALESCE(MAX(priority),0)+1 FROM jobs").fetchone()[0]
            try:
                self.db.execute("INSERT INTO jobs (id,topic,config,output,temp_dir,status,stage_states,priority,created) VALUES (?,?,?,?,?,'QUEUED',?,?,?)",
                                (job_id, topic.strip(), json.dumps(snapshot), str(target), snapshot["temp_root"],
                                 json.dumps(dict.fromkeys(STAGES, "PENDING")), priority, datetime.now(timezone.utc).isoformat()))
                self.db.commit()
            except sqlite3.IntegrityError:
                raise ValueError("Output destination is already reserved by another job.") from None
            self._credentials[job_id] = credentials
            self._last_id = job_id
            self._schedule()
            return job_id

    def _usage(self):
        usage = {}
        for job_id, worker in self._workers.items():
            try:
                parent = psutil.Process(worker.process.pid)
                processes = [parent, *parent.children(recursive=True)]
                usage[job_id] = sum(p.memory_info().rss for p in processes if p.is_running())
            except (psutil.Error, TypeError):
                usage[job_id] = 0
        return usage

    def _update(self, job_id, **values):
        self.db.execute("UPDATE jobs SET " + ", ".join(key + "=?" for key in values) + " WHERE id=?", (*values.values(), job_id))
        self.db.commit()

    def _schedule(self):
        usage = self._usage()
        rows = self.list_jobs()
        memory_blocked = False
        # Existing stage requests precede new starts. FIFO priority is stable;
        # newer requests cannot jump an older request for the same resource.
        blocked_resources = set()
        for row in rows:
            if row["status"] != "WAITING" or row["id"] not in self._workers:
                continue
            stage, job_id = row["stage"], row["id"]
            draining = self.resources.can_drain(job_id, stage, row["config"])
            if memory_blocked and not draining:
                self._update(job_id, waiting_reason="Waiting for available memory")
                continue
            resource = stage if stage in {"render", "upload"} else ("gpu" if stage == "timestamps" and row["config"].get("whisper_device", "cpu").startswith("cuda") else "memory")
            reason = self.resources.reserve(job_id, stage, row["config"], usage, allow_drain=True) if resource not in blocked_resources or (resource == "memory" and draining) else row["waiting_reason"]
            if reason:
                blocked_resources.add(resource)
                if reason == "Waiting for available memory":
                    memory_blocked = True
                    self._update(job_id, waiting_reason=reason)
                    continue
                if row["waiting_reason"] != reason:
                    self._update(job_id, waiting_reason=reason)
                continue
            try:
                worker = self._workers[job_id]
                worker.process.stdin.write(json.dumps({"permit": stage}) + "\n")
                worker.process.stdin.flush()
                states = row["stage_states"]
                states[stage] = "RUNNING"
                self._update(job_id, status="RUNNING", waiting_reason="", stage_states=json.dumps(states))
            except (OSError, ValueError):
                # Lease survives until process reaping, even on broken pipes.
                worker.cancel()
        if memory_blocked:
            return
        for row in rows:
            if row["status"] != "QUEUED":
                continue
            job_id = row["id"]
            if len(self._workers) >= self.limits.max_active_jobs:
                self._update(job_id, waiting_reason="Waiting for active job slot")
                continue
            reason = self.resources.reserve(job_id, "startup", row["config"], usage)
            if reason:
                self._update(job_id, waiting_reason=reason)
                # Prevent a stream of new workers from starving an older request.
                break
            worker = self._factory(self.log_dir)
            self._workers[job_id] = worker
            worker._signal_process = self._signal_process
            self._update(job_id, status="RUNNING", waiting_reason="", message="Starting worker")
            try:
                credentials = self._credentials.get(job_id)
                if credentials is None:
                    current = build_config()
                    credentials = {key: getattr(current, key) for key in SECRET_FIELDS}
                payload = {"job_id": job_id, "topic": row["topic"], "output": row["output"],
                           "config": {**row["config"], **credentials}, "managed": True}
                worker.start(row["topic"], row["output"], payload=payload)
                created = None
                try:
                    created = psutil.Process(worker.process.pid).create_time()
                except (psutil.Error, TypeError):
                    pass
                self._update(job_id, pid=worker.process.pid if isinstance(worker.process.pid, int) else None,
                             process_created=created, log_path=str(worker.log_path))
            except Exception as exc:
                worker.close()
                self.resources.leases.pop(job_id, None)
                self._workers.pop(job_id, None)
                self._update(job_id, status="FAILED", message=f"Worker startup failed: {type(exc).__name__}")

    def poll(self):
        with self._lock:
            if self._closed:
                return []
            events = []
            for job_id, worker in list(self._workers.items()):
                for event in worker.poll():
                    event = {**event, "job_id": job_id}
                    row = next(row for row in self.list_jobs() if row["id"] == job_id)
                    kind, stage = event.get("type"), event.get("stage")
                    values = {}
                    states = row["stage_states"]
                    if kind == "stage_requested" and row["status"] != "CANCELLING":
                        if stage not in STAGES:
                            worker.cancel()
                            continue
                        # Keep the old reservation until replacement admission to
                        # account for resident working sets between stages.
                        states[stage] = "WAITING"
                        values.update(stage=stage, status="WAITING", waiting_reason="Waiting for resource admission")
                    elif kind in {"stage_started", "stage_completed", "stage_failed"} and stage in STAGES:
                        states[stage] = {"stage_started": "RUNNING", "stage_completed": "COMPLETED", "stage_failed": "FAILED"}[kind]
                        values["stage"] = stage
                        if kind == "stage_completed":
                            # The stage has released its own resources before
                            # reporting completion. Retain resident memory plus a
                            # baseline, not its obsolete peak or exclusive slot.
                            resident = self._usage().get(job_id, 0)
                            baseline = self.resources.requirement("startup", row["config"])
                            self.resources.leases[job_id] = ("idle", max(resident, baseline), False)
                    terminal = {"job_completed": "COMPLETED", "job_failed": "FAILED", "job_cancelled": "CANCELLED"}.get(kind)
                    if terminal:
                        if row["status"] == "CANCELLING":
                            terminal = "CANCELLED"
                            event["type"] = "job_cancelled"
                        # Terminal state is not trusted until the process exits.
                        values["message"] = event.get("message", terminal)
                        worker._queue_terminal = terminal
                        worker._terminal_event = event
                    if kind in {"stage_requested", "stage_started", "stage_completed", "stage_failed"}:
                        values["stage_states"] = json.dumps(states)
                    if event.get("message"):
                        values["message"] = event["message"]
                    if values:
                        self._update(job_id, **values)
                    if not terminal:
                        events.append(event)
                if not worker.running:
                    row = next(row for row in self.list_jobs() if row["id"] == job_id)
                    terminal = getattr(worker, "_queue_terminal", "FAILED")
                    event = getattr(worker, "_terminal_event", {"type": "job_failed", "job_id": job_id,
                                                                "message": "Worker exited without completion"})
                    if terminal == "COMPLETED":
                        target = Path(row["output"])
                        if (worker.process.returncode != 0 or not target.is_file() or target.stat().st_size == 0
                            or not target.with_suffix(".metadata.json").is_file()):
                            terminal = "FAILED"
                            event = {"type": "job_failed", "job_id": job_id,
                                     "message": "Worker completion could not be verified; inspect job logs and output."}
                            worker._record(event)
                    events.append(event)
                    states = row["stage_states"]
                    for stage in states:
                        if states[stage] in {"WAITING", "RUNNING"}:
                            states[stage] = terminal if terminal != "COMPLETED" else "COMPLETED"
                    self._update(job_id, status=terminal, waiting_reason="", pid=None, message=event.get("message", terminal), stage_states=json.dumps(states))
                    self._last_process = worker.process
                    worker.close()
                    self._cleanup(row, keep_temp=terminal == "COMPLETED" and row["config"].get("keep_temp", False))
                    self.resources.leases.pop(job_id, None)
                    self._credentials.pop(job_id, None)
                    self._workers.pop(job_id, None)
            self._schedule()
            return events

    def cancel(self, job_id=None):
        with self._lock:
            job_id = job_id or self._last_id
            row = next((row for row in self.list_jobs() if row["id"] == job_id), None)
            if row is None or row["status"] in TERMINAL:
                return
            if job_id in self._workers:
                self._update(job_id, status="CANCELLING", waiting_reason="", message="Cancellation requested")
                self._workers[job_id].cancel()
            else:
                self._update(job_id, status="CANCELLED", waiting_reason="", message="Cancelled before startup")
                self._credentials.pop(job_id, None)

    def retry(self, job_id):
        with self._lock:
            row = next(row for row in self.list_jobs() if row["id"] == job_id)
            if row["status"] not in {"FAILED", "CANCELLED", "INTERRUPTED", "HELD"}:
                raise ValueError("Only failed, cancelled, interrupted or held jobs can be retried.")
            config = dict(row["config"])
            for key in PATH_FIELDS:
                if config.get(key) is not None:
                    config[key] = Path(config[key])
            config["temp_root"] = Path(row["temp_dir"]).parent
            current = build_config()
            config.update({key: getattr(current, key) for key in SECRET_FIELDS})
            # Retry is a new job and output; uploads always need new consent.
            config["youtube_upload_enabled"] = False
            return self.start(row["topic"], config=PipelineConfig(**config))

    def reorder(self, job_id, direction):
        with self._lock:
            rows = [row for row in self.list_jobs() if row["status"] == "QUEUED"]
            index = next((i for i, row in enumerate(rows) if row["id"] == job_id), -1)
            target = index + (-1 if direction < 0 else 1)
            if index < 0 or not 0 <= target < len(rows):
                return
            other = rows[target]
            self.db.execute("UPDATE jobs SET priority=? WHERE id=?", (other["priority"], job_id))
            self.db.execute("UPDATE jobs SET priority=? WHERE id=?", (rows[index]["priority"], other["id"]))
            self.db.commit()

    def close(self):
        with self._lock:
            if self._closed:
                return
            for worker in self._workers.values():
                worker.close()
            # Graceful application exit preserves pending jobs and records active
            # jobs as interrupted, just like a crash. It never silently retries.
            self._recover()
            self._workers.clear()
            self.resources.leases.clear()
            self._credentials.clear()
            self._closed = True
            self.db.close()
            self._owner.close()
