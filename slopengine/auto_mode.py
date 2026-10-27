"""Persistent, bounded producer-consumer controller for Auto Mode."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
import json
import math
import os
from pathlib import Path
import random
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone

from .config_core import PipelineConfig
from .settings import PATH_FIELDS, build_config, redact, validate_config


STATES = {"IDLE", "STARTING", "RUNNING", "PAUSING", "PAUSED", "STOPPING", "STOPPED", "COMPLETED", "ERROR"}
BATCH_SIZE = 5
QUEUE_TARGET = 3


class AutoModeError(RuntimeError):
    pass


class AutoBudgetError(AutoModeError):
    pass


class AutoRequestPaused(AutoModeError):
    pass


@dataclass(frozen=True)
class AutoSettings:
    requests_per_minute: int = 4
    request_budget: int = 0
    cooldown_seconds: float = 5.0
    min_free_disk_gib: float = 1.0
    max_consecutive_failures: int = 5
    duplicate_batches_before_error: int = 3
    similarity_threshold: float = 0.9

    @classmethod
    def from_env(cls):
        def integer(name, default, minimum=0):
            try:
                value = int(os.environ.get(name, str(default)))
            except ValueError:
                raise AutoModeError(f"{name} must be an integer.") from None
            if not math.isfinite(value) or value < minimum:
                raise AutoModeError(f"{name} must be at least {minimum}.")
            return value

        def number(name, default, minimum=0.0):
            try:
                value = float(os.environ.get(name, str(default)))
            except ValueError:
                raise AutoModeError(f"{name} must be a number.") from None
            if value < minimum:
                raise AutoModeError(f"{name} must be at least {minimum}.")
            return value

        similarity = number("AUTO_DUPLICATE_SIMILARITY", 0.9)
        if similarity > 1:
            raise AutoModeError("AUTO_DUPLICATE_SIMILARITY must be between 0 and 1.")
        return cls(
            requests_per_minute=integer("AUTO_MAX_TOPIC_REQUESTS_PER_MINUTE", 4, 1),
            request_budget=integer("AUTO_MAX_TOPIC_REQUESTS_SESSION", 0),
            cooldown_seconds=number("AUTO_TOPIC_REQUEST_COOLDOWN_SECONDS", 5.0),
            min_free_disk_gib=number("AUTO_MIN_FREE_DISK_GIB", 1.0),
            max_consecutive_failures=integer("AUTO_MAX_CONSECUTIVE_FAILURES", 5, 1),
            duplicate_batches_before_error=integer("AUTO_DUPLICATE_BATCH_LIMIT", 3, 1),
            similarity_threshold=similarity,
        )


class TopicGenerator:
    """Lazy Gemini transport with bounded retries and strict structured output."""

    RESPONSE_SCHEMA = {
        "type": "object",
        "properties": {"topics": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string", "maxLength": 90}, "angle": {"type": "string", "maxLength": 500}},
            "required": ["title", "angle"],
        }}},
        "required": ["topics"],
    }

    def __init__(self, config):
        self.config = config
        self._client = None
        self._types = None
        self._request_attempts = 0

    def _client_for_config(self):
        if self._client is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:
                raise AutoModeError("Install google-genai to use Auto topic generation.") from exc
            if not self.config.gemini_api_key:
                raise AutoModeError("Configure GEMINI_API_KEY before starting Auto Mode.")
            http_options = types.HttpOptions(
                timeout=int(self.config.gemini_timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            )
            self._client = genai.Client(api_key=self.config.gemini_api_key, http_options=http_options)
            self._types = types
        return self._client

    @staticmethod
    def _retryable(exc):
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        message = str(exc).casefold()
        return status in {429, 500, 502, 503, 504} or any(word in message for word in (
            "temporarily unavailable", "high demand", "timed out", "timeout", "connection reset",
        ))

    @staticmethod
    def _missing_model(exc):
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        message = str(exc).casefold()
        return status == 404 or "not found" in message or "not supported for generatecontent" in message

    @staticmethod
    def _fallback_worthy(exc):
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        message = str(exc).casefold()
        return TopicGenerator._missing_model(exc) or status in {500, 502, 503, 504} or "high demand" in message or "temporarily unavailable" in message

    def _generate_once(self, model, prompt, permit_request, delay=0.0):
        if not permit_request(delay):
            raise AutoRequestPaused("Topic request stopped before dispatch.")
        self._request_attempts += 1
        client = self._client_for_config()
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=self._types.GenerateContentConfig(
                response_mime_type="application/json", response_json_schema=self.RESPONSE_SCHEMA,
            ),
        )
        return getattr(response, "text", "") or ""

    def _fallback_model(self, client, excluded, permit_request):
        if not permit_request(0.0):
            raise AutoRequestPaused("Topic request stopped before model fallback.")
        self._request_attempts += 1
        models = []
        for model in client.models.list():
            actions = getattr(model, "supported_actions", None) or []
            if "generateContent" not in actions:
                continue
            name = str(getattr(model, "name", "")).removeprefix("models/")
            if name and name not in excluded:
                models.append(name)
        preferred = (self.config.gemini_model, "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash")
        return next((name for name in preferred if name in models), next(iter(models), None))

    def generate(self, niche, count, *, accepted, rejected, completed, permit_request, diversity_guidance=""):
        self._request_attempts = 0
        client = self._client_for_config()
        rows = lambda items: "\n".join(f"- {item}" for item in items) or "- (none yet)"
        prompt = f"""Create {count} distinct, factual short-video topic ideas for this niche: {niche}

Choose specific and surprising angles that can support a clear explanation. Avoid repetitive Top 5, shocking, and generic list templates. Avoid cosmetic rewrites of the same concept. Vary the underlying question, mechanism, era, person, consequence, and misconception. Keep each title concise and each angle concrete enough to guide narration.

Recently accepted topics (avoid repeating these):
{rows(accepted)}

Previously rejected duplicates (avoid these concepts too):
{rows(rejected)}

Completed topics in the session:
{rows(completed)}

Additional diversity guidance for this batch:
{diversity_guidance or 'Use the broad niche guidance above.'}

Return exactly this JSON shape, with {count} objects when possible:
{{"topics":[{{"title":"short title","angle":"specific factual angle"}}]}}"""
        model = self.config.gemini_model
        last_error = None
        attempts = 0
        retry_delay = 0.0
        while attempts < 3:
            try:
                raw = self._generate_once(model, prompt, permit_request, retry_delay)
                break
            except AutoRequestPaused:
                raise
            except Exception as exc:
                if isinstance(exc, AutoBudgetError):
                    raise
                last_error = exc
                if self._fallback_worthy(exc):
                    try:
                        alternate = self._fallback_model(client, {model}, permit_request)
                    except AutoRequestPaused:
                        raise
                    except Exception as fallback_exc:
                        if isinstance(fallback_exc, AutoBudgetError):
                            raise
                        if not self._retryable(fallback_exc):
                            raise AutoModeError(f"Gemini model lookup failed: {type(fallback_exc).__name__}: {fallback_exc}") from fallback_exc
                        alternate = None
                    if alternate:
                        try:
                            raw = self._generate_once(alternate, prompt, permit_request)
                            break
                        except AutoRequestPaused:
                            raise
                        except Exception as fallback_exc:
                            if isinstance(fallback_exc, AutoBudgetError):
                                raise
                            last_error = fallback_exc
                            if not self._retryable(fallback_exc):
                                raise AutoModeError(f"Gemini topic request failed: {type(fallback_exc).__name__}: {fallback_exc}") from fallback_exc
                if not self._retryable(exc):
                    raise AutoModeError(f"Gemini topic request failed: {type(exc).__name__}: {exc}") from exc
                attempts += 1
                if attempts >= 3:
                    break
                retry_delay = min(30.0, 2 ** (attempts - 1)) * random.uniform(0.75, 1.25)
        else:
            raw = ""
        if "raw" not in locals():
            raise AutoModeError(f"Gemini topic request failed after bounded retries: {type(last_error).__name__}: {last_error}")
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            raise AutoModeError("Gemini returned invalid JSON for topics; check the job log and retry.") from None
        if not isinstance(data, dict) or not isinstance(data.get("topics"), list) or not data["topics"]:
            raise AutoModeError("Gemini returned an empty or malformed topics list; retry the session.")
        if len(data["topics"]) > count:
            raise AutoModeError("Gemini returned more topics than requested; response rejected.")
        result = []
        for item in data["topics"]:
            if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not isinstance(item.get("angle"), str):
                raise AutoModeError("Gemini topic items must contain string title and angle fields.")
            title, angle = item["title"].strip(), item["angle"].strip()
            if (not title or not angle or len(title) > 90 or len(angle) > 500 or
                    "\n" in title or "\r" in title or "\n" in angle or "\r" in angle or
                    any(ord(char) < 32 and char not in "\t" for char in title + angle)):
                raise AutoModeError("Gemini returned an empty topic title or angle; response rejected.")
            result.append({"title": title, "angle": angle})
        return result


class AutoMode:
    def __init__(self, jobs, *, preferences=None, generator_factory=TopicGenerator, settings=None):
        self.jobs = jobs
        self.preferences = dict(preferences or {})
        self._configured_settings = settings or AutoSettings.from_env()
        self.settings = self._configured_settings
        self.generator_factory = generator_factory
        self._condition = threading.Condition(threading.RLock())
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slopengine-auto-topics")
        self._future: Future | None = None
        self._future_epoch: int | None = None
        self._epoch = 0
        self._next_request_at = 0.0
        self._request_times: list[float] = []
        self._zero_unique_batches = 0
        self._generation_failures = 0
        self.session_id = None
        self.state = "IDLE"
        self.message = "Enter a niche and start Auto Mode."
        self.target = None
        self.niche = ""
        self.config = None
        self.generator = None
        self._closed = False
        self._restore_latest()

    def _restore_latest(self):
        sessions = self.jobs.auto_sessions()
        if not sessions:
            return
        row = sessions[0]
        self.session_id = row["id"]
        self.niche = row["niche"]
        self.target = row["target"]
        self.message = row["message"] or "Previous Auto session needs explicit resume."
        self.state = row["status"]
        try:
            saved_config = json.loads(row["config"])
            saved_settings = saved_config.get("auto_settings")
            if saved_settings:
                self.settings = AutoSettings(**saved_settings)
        except (ValueError, TypeError):
            saved_config = {}
        self._restore_request_window()
        if self.state in {"STARTING", "RUNNING", "PAUSING", "STOPPING"}:
            self.state = "PAUSED"
            self.message = "Session interrupted by application shutdown. Press R to resume explicitly."
            self.jobs.set_auto_state(self.session_id, "PAUSED", message=self.message)
            self.jobs.auto_event(self.session_id, "restart", self.message, "WARN")

    def _restore_request_window(self):
        self._request_times = []
        self._next_request_at = 0.0
        if not self.session_id:
            return
        now_utc = datetime.now(timezone.utc).timestamp()
        now_mono = time.monotonic()
        for event in self.jobs.auto_request_events(self.session_id, limit=1000):
            try:
                stamp = datetime.fromisoformat(event["time"]).timestamp()
            except (ValueError, TypeError):
                continue
            elapsed = max(0.0, now_utc - stamp)
            if elapsed < 60:
                self._request_times.append(now_mono - elapsed)
            remaining = self.settings.cooldown_seconds - elapsed
            if remaining > 0:
                self._next_request_at = max(self._next_request_at, now_mono + remaining)

    def start(self, niche, target, *, config=None):
        niche = str(niche).strip()
        if not niche:
            raise ValueError("Enter a niche before starting Auto Mode.")
        if target is not None and (type(target) is not int or target <= 0):
            raise ValueError("Number of videos must be a positive integer, or blank for unlimited.")
        with self._condition:
            if self.state in {"STARTING", "RUNNING", "PAUSING", "PAUSED", "STOPPING"}:
                raise ValueError("Resume or stop the current Auto session before starting another.")
            if self.state == "ERROR" and self.session_id:
                previous = self.snapshot()
                if previous["active"] or previous["queued"]:
                    raise ValueError("This errored session still has queued or active jobs. Resume it, or stop/cancel it before starting a new session.")
            self.state = "STARTING"
            self.session_id = None
            self.message = "Checking credentials, FFmpeg, output directory, and available disk space…"
            self.niche, self.target = niche, target
            self.config = config or build_config(self.preferences)
            issues = validate_config(self.config, topic=niche, require_keys=True)
            if issues:
                self.state = "ERROR"
                self.message = " ".join(issues)
                raise ValueError(self.message)
            if shutil.which("ffmpeg") is None:
                self.state = "ERROR"
                self.message = "FFmpeg is missing. Install it and make it available on PATH."
                raise ValueError(self.message)
            output_dir = Path(self.config.output_dir)
            temp_root = Path(self.config.temp_root)
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                temp_root.mkdir(parents=True, exist_ok=True)
                free = min(shutil.disk_usage(output_dir).free,
                           shutil.disk_usage(temp_root).free) / (1024 ** 3)
            except OSError as exc:
                self.state = "ERROR"
                self.message = f"Cannot check the output disk: {exc}"
                raise ValueError(self.message) from exc
            if free < self.settings.min_free_disk_gib:
                self.state = "ERROR"
                self.message = f"Only {free:.1f} GiB is free; Auto Mode requires {self.settings.min_free_disk_gib:.1f} GiB."
                raise ValueError(self.message)
            self.session_id = uuid.uuid4().hex
            safe_config = asdict(self.config)
            safe_config.pop("gemini_api_key", None)
            safe_config.pop("pexels_api_key", None)
            for name in PATH_FIELDS:
                if safe_config.get(name) is not None:
                    safe_config[name] = str(Path(safe_config[name]).expanduser().resolve())
            safe_config["auto_settings"] = asdict(self.settings)
            self.jobs.create_auto_session(session_id=self.session_id, niche=niche, target=target, config=safe_config)
            self.jobs.import_auto_outputs(self.config.output_dir)
            self.generator = self.generator_factory(self.config)
            self._epoch += 1
            self._request_times = []
            self._zero_unique_batches = 0
            self._generation_failures = 0
            self._future = None
            self._future_epoch = None
            self._next_request_at = 0.0
            self.state = "RUNNING"
            self.message = "Auto Mode is running."
            self.jobs.set_auto_state(self.session_id, "RUNNING", message=self.message)
            self.jobs.auto_event(self.session_id, "start", f"Started for niche: {niche}")
            self._condition.notify_all()
        return self.session_id

    def _request_permit(self, epoch, delay=0.0):
        with self._condition:
            if epoch != self._epoch or self.state != "RUNNING":
                return False
            if delay:
                self._condition.wait(timeout=delay)
                if epoch != self._epoch or self.state != "RUNNING":
                    return False
            while True:
                now = time.monotonic()
                self._request_times = [stamp for stamp in self._request_times if now - stamp < 60]
                if self.settings.request_budget and self.jobs.auto_session(self.session_id)["request_count"] >= self.settings.request_budget:
                    raise AutoBudgetError(
                        f"Topic discovery budget of {self.settings.request_budget} Gemini requests is exhausted; increase AUTO_MAX_TOPIC_REQUESTS_SESSION or start a new session."
                    )
                cooldown = self._next_request_at - now
                if len(self._request_times) < self.settings.requests_per_minute and cooldown <= 0:
                    break
                rpm_wait = 60 - (now - self._request_times[0]) if len(self._request_times) >= self.settings.requests_per_minute else 0
                wait_for = max(0.05, cooldown, rpm_wait)
                self._condition.wait(timeout=min(wait_for, 1.0))
                if epoch != self._epoch or self.state != "RUNNING":
                    return False
            self._request_times.append(time.monotonic())
            self._next_request_at = time.monotonic() + self.settings.cooldown_seconds
            session = self.jobs.auto_session(self.session_id)
            count = session["request_count"] + 1
            self.jobs.set_auto_state(self.session_id, "RUNNING", request_count=count)
            self.jobs.auto_event(self.session_id, "request", f"Gemini topic discovery request {count}")
            return True

    def pause(self):
        with self._condition:
            if self.state not in {"RUNNING", "STARTING"}:
                return
            self._epoch += 1
            self.state = "PAUSING"
            self.message = "Pausing at the next safe pipeline boundary."
            session_id = self.session_id
            self.jobs.set_auto_session_paused(session_id, True)
            self.jobs.set_auto_state(session_id, "PAUSING", message=self.message)
            self.jobs.auto_event(session_id, "pause", self.message)
            self._condition.notify_all()

    def resume(self, *, config=None):
        with self._condition:
            if self.state not in {"PAUSED", "ERROR"}:
                raise ValueError("Only a paused or errored Auto session can resume.")
            if not self.session_id:
                raise ValueError("There is no persisted Auto session to resume. Fix the setup and start a new session.")
            session = self.jobs.auto_session(self.session_id)
            if session is None:
                raise ValueError("The persisted Auto session is missing from the job database.")
            # Let current environment settings raise a stored request budget or
            # adjust limits before explicit resume; request_count stays durable.
            self.settings = self._configured_settings
            self._restore_request_window()
            try:
                saved = json.loads(session["config"])
            except (ValueError, TypeError):
                saved = {}
            current_config = config or build_config(self.preferences)
            pipeline_values = asdict(current_config)
            for field in fields(PipelineConfig):
                if field.name in saved:
                    pipeline_values[field.name] = saved[field.name]
            pipeline_values["youtube_upload_enabled"] = bool(saved.get("youtube_upload_enabled", False))
            for name in PATH_FIELDS:
                if pipeline_values.get(name) is not None:
                    pipeline_values[name] = Path(pipeline_values[name]).expanduser()
            pipeline_values["gemini_api_key"] = current_config.gemini_api_key
            pipeline_values["pexels_api_key"] = current_config.pexels_api_key
            self.config = PipelineConfig(**pipeline_values)
            if not self.config.gemini_api_key:
                raise ValueError("Configure GEMINI_API_KEY before resuming Auto Mode.")
            issues = validate_config(self.config, topic=self.niche, require_keys=True)
            if issues:
                raise ValueError(" ".join(issues))
            if shutil.which("ffmpeg") is None:
                raise ValueError("FFmpeg is missing. Install it and make it available on PATH.")
            try:
                Path(self.config.output_dir).mkdir(parents=True, exist_ok=True)
                Path(self.config.temp_root).mkdir(parents=True, exist_ok=True)
                free = min(shutil.disk_usage(Path(self.config.output_dir)).free,
                           shutil.disk_usage(Path(self.config.temp_root)).free) / (1024 ** 3)
            except OSError as exc:
                raise ValueError(f"Cannot check the output disk: {exc}") from exc
            if free < self.settings.min_free_disk_gib:
                self._fail(f"Only {free:.1f} GiB is free; Auto Mode requires {self.settings.min_free_disk_gib:.1f} GiB.")
                raise ValueError(self.message)
            self.jobs.resume_auto_session(self.session_id)
            self.jobs.import_auto_outputs(self.config.output_dir)
            self.jobs.set_auto_session_paused(self.session_id, False)
            self.jobs.set_auto_state(self.session_id, "RUNNING", consecutive_failures=0,
                                     consecutive_rejections=0)
            self.generator = self.generator_factory(self.config)
            self._epoch += 1
            self._generation_failures = 0
            self._zero_unique_batches = 0
            self.state = "RUNNING"
            self.message = "Auto Mode resumed."
            self.jobs.set_auto_state(self.session_id, "RUNNING", message=self.message)
            self.jobs.auto_event(self.session_id, "resume", self.message)
            self._condition.notify_all()

    def stop(self, *, cancel=False):
        with self._condition:
            if self.state not in {"RUNNING", "PAUSING", "PAUSED", "STARTING", "ERROR"}:
                return
            if not self.session_id:
                self.state = "STOPPED"
                self.message = "Auto session stopped."
                return
            self._epoch += 1
            self.state = "STOPPING"
            self.message = "Cancelling this Auto session…" if cancel else "Stopping after active topics finish…"
            self.jobs.set_auto_state(self.session_id, "STOPPING", message=self.message)
            self.jobs.auto_event(self.session_id, "cancel" if cancel else "stop", self.message,
                                 "WARN" if cancel else "INFO")
            self.jobs.stop_auto_session(self.session_id, cancel=cancel)
            self._condition.notify_all()

    def _topic_rows(self):
        return self.jobs.auto_topic_rows(self.session_id) if self.session_id else []

    def _summary(self):
        rows = self._topic_rows()
        accepted = [row for row in rows if row["source"] == "auto" and row["status"] != "REJECTED"]
        rejected = [row for row in rows if row["status"] == "REJECTED"]
        completed = [row for row in accepted if row.get("video_finalized")]
        return rows, accepted, rejected, completed

    def tick(self):
        """Advance state without waiting for provider I/O."""
        with self._condition:
            if not self.session_id:
                return
            rows, accepted, rejected, completed = self._summary()
            jobs = [row for row in accepted if row.get("job_id")]
            latest = self.jobs.auto_session(self.session_id)
            if latest is None:
                self._fail("Auto session record is missing from the job database.")
                return
            owned_jobs = [row for row in rows if row["source"] == "auto" and row.get("job_id")]
            if self.state == "STOPPING":
                if not any(row.get("job_status") in {"QUEUED", "RUNNING", "WAITING", "CANCELLING", "HELD"} for row in owned_jobs):
                    self.state = "STOPPED"
                    self.message = "Auto session stopped."
                    self.jobs.set_auto_state(self.session_id, "STOPPED", message=self.message)
                    self.jobs.auto_event(self.session_id, "stopped", self.message)
                return
            if self.state == "RUNNING":
                try:
                    free = min(shutil.disk_usage(Path(self.config.output_dir)).free,
                               shutil.disk_usage(Path(self.config.temp_root)).free) / (1024 ** 3)
                except OSError as exc:
                    self._fail(f"Cannot check the output disk: {exc}")
                    return
                if free < self.settings.min_free_disk_gib:
                    self._fail(f"Only {free:.1f} GiB is free; Auto Mode requires {self.settings.min_free_disk_gib:.1f} GiB.")
                    return
            unseen = [row for row in owned_jobs
                      if row.get("job_status") in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}
                      and not row.get("accounted")]
            for row in sorted(unseen, key=lambda item: item.get("updated") or ""):
                if row.get("video_finalized"):
                    outcome_class = "success"
                    kind, level = "video_completed", "INFO"
                    message = f"Finalized video: {row['title']} · {row.get('job_output') or ''}"
                elif row["status"] == "REJECTED" and "script title duplicate" in str(row.get("outcome") or ""):
                    outcome_class = "rejection"
                    kind, level = "final_title_rejection_accounted", "WARN"
                    message = f"Final script title rejected: {row['title']} · {row.get('outcome') or ''}"
                else:
                    outcome_class = "failure"
                    kind, level = "job_failure", "WARN"
                    message = f"{row['title']}: {row.get('job_message') or row.get('job_status')}"
                self.jobs.account_auto_terminal(self.session_id, row["job_id"], outcome_class,
                                                kind=kind, message=message, level=level)
            latest = self.jobs.auto_session(self.session_id)
            failed_upload = next((row for row in owned_jobs
                                  if row.get("video_finalized") and row.get("job_status") == "FAILED"
                                  and not row.get("accounted")
                                  and self.config.youtube_upload_enabled
                                  and (row.get("stage") == "upload" or "upload" in str(row.get("job_message") or "").lower())), None)
            if failed_upload:
                self._fail(
                    f"Video saved at {failed_upload.get('job_output')} but YouTube upload failed: "
                    f"{failed_upload.get('job_message') or 'check OAuth and upload settings'}. "
                    "The MP4 is preserved and counts toward the target; resume continues with new videos without rerendering it."
                )
                return
            if latest["consecutive_failures"] >= self.settings.max_consecutive_failures:
                self._fail(f"{latest['consecutive_failures']} consecutive video jobs failed. Work is preserved; inspect Queue and resume after fixing the cause.")
                return
            if latest["consecutive_rejections"] >= self.settings.duplicate_batches_before_error:
                self._fail(f"{latest['consecutive_rejections']} consecutive script titles overlapped duplicate history. Work is preserved; review history or adjust the niche before resuming.")
                return
            if self.state == "PAUSING":
                if self.jobs.auto_checkpoint(self.session_id):
                    self.state = "PAUSED"
                    self.message = "Paused at a safe stage boundary. Queue is preserved."
                    self.jobs.set_auto_state(self.session_id, "PAUSED", message=self.message)
                    self.jobs.auto_event(self.session_id, "paused", self.message)
                return
            if self.state != "RUNNING":
                return
            if self.target is not None and len(completed) >= self.target:
                self.state = "COMPLETED"
                self.message = f"Completed exactly {self.target} finalized videos."
                self.jobs.set_auto_state(self.session_id, "COMPLETED", message=self.message)
                self.jobs.auto_event(self.session_id, "completed", self.message)
                return
            if self._future is not None and self._future.done():
                future, epoch = self._future, self._future_epoch
                self._future = None
                self._future_epoch = None
                if epoch != self._epoch or self.state != "RUNNING":
                    return
                try:
                    ideas = future.result()
                except AutoRequestPaused:
                    return
                except Exception as exc:
                    self._generation_failures += 1
                    message = redact(f"Topic generation failed: {type(exc).__name__}: {exc}")
                    self.jobs.auto_event(self.session_id, "topic_error", message, "ERROR")
                    if isinstance(exc, AutoBudgetError) or self._generation_failures >= 3:
                        self._fail(message)
                    return
                self._generation_failures = 0
                before = sum(row["status"] != "REJECTED" for row in accepted)
                capacity_hit = False
                for idea in ideas:
                    current_completed = sum(bool(row.get("video_finalized")) for row in self.jobs.auto_topic_rows(self.session_id))
                    current_open = sum(row.get("job_status") in {"QUEUED", "RUNNING", "WAITING", "CANCELLING"}
                                       for row in self.jobs.auto_topic_rows(self.session_id))
                    if self.target is not None and current_completed + current_open >= self.target:
                        break
                    try:
                        free = min(shutil.disk_usage(Path(self.config.output_dir)).free,
                                   shutil.disk_usage(Path(self.config.temp_root)).free) / (1024 ** 3)
                    except OSError as exc:
                        self._fail(f"Cannot check the output disk: {exc}")
                        return
                    if free < self.settings.min_free_disk_gib:
                        self._fail(f"Only {free:.1f} GiB is free; Auto Mode requires {self.settings.min_free_disk_gib:.1f} GiB.")
                        return
                    title, angle = idea["title"], idea["angle"]
                    topic = f"{title}: {angle}"
                    try:
                        result = self.jobs.start_auto(topic, self.session_id, title, angle,
                                                     config=self.config, niche=self.niche,
                                                     similarity_threshold=self.settings.similarity_threshold)
                    except Exception as exc:
                        self._fail(redact(f"Could not reserve an Auto topic/job: {type(exc).__name__}: {exc}"))
                        return
                    if result["accepted"]:
                        self.jobs.auto_event(self.session_id, "topic_accepted", title)
                    elif result["reason"] == "duplicate":
                        matched = result["matched"]
                        self.jobs.auto_event(self.session_id, "duplicate", f"Rejected duplicate: {title} → {matched['title']}", "WARN")
                    elif result["reason"] == "capacity":
                        capacity_hit = True
                        break
                after_rows = self.jobs.auto_topic_rows(self.session_id)
                after = sum(row["status"] != "REJECTED" for row in after_rows)
                if after == before and ideas and not capacity_hit:
                    self._zero_unique_batches += 1
                else:
                    self._zero_unique_batches = 0
                if self._zero_unique_batches >= self.settings.duplicate_batches_before_error:
                    self._fail("Gemini produced no unique topics in several batches. Change the niche or clear duplicate history only after reviewing it.")
                    return
                rows, accepted, rejected, completed = self._summary()
            active_limit = getattr(getattr(self.jobs, "limits", None), "max_active_jobs", 2)
            active_count = sum(row.get("job_status") in {"RUNNING", "WAITING", "CANCELLING"}
                               for row in owned_jobs)
            queued_count = sum(row.get("job_status") in {"QUEUED", "HELD"} for row in owned_jobs)
            capacity = (0 if queued_count >= QUEUE_TARGET else
                        max(0, active_limit - active_count) + max(0, QUEUE_TARGET - queued_count))
            if self.target is not None:
                outstanding = active_count + queued_count
                capacity = min(capacity, self.target - len(completed) - outstanding)
            free_slots = max(0, capacity)
            if free_slots and self._future is None:
                count = min(BATCH_SIZE, free_slots)
                context = self.jobs.auto_prompt_context(40)
                accepted_context = context["accepted"]
                rejected_context = context["rejected"]
                completed_context = context["completed"]
                diversity = ""
                if self._zero_unique_batches:
                    diversity = (f"The last {self._zero_unique_batches} batch(es) were all duplicate. Pick genuinely different underlying subjects, "
                                 "mechanisms, eras, people, or consequences. Do not paraphrase or reorder an earlier idea. Avoid every listed title and angle.")
                    if self._zero_unique_batches > 1:
                        diversity += " Choose a different subfield within the niche and avoid the same entities or causal process."
                epoch = self._epoch
                self._future = self._executor.submit(
                    self.generator.generate, self.niche, count, accepted=accepted_context,
                    rejected=rejected_context, completed=completed_context,
                    permit_request=lambda delay=0.0: self._request_permit(epoch, delay),
                    diversity_guidance=diversity,
                )
                self._future_epoch = epoch

    def _fail(self, message):
        with self._condition:
            self._epoch += 1
            self.state = "ERROR"
            self.message = str(message)
            self.jobs.set_auto_session_paused(self.session_id, True)
            self.jobs.set_auto_state(self.session_id, "ERROR", message=self.message)
            self.jobs.auto_event(self.session_id, "error", self.message, "ERROR")
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            rows, accepted, rejected, completed = self._summary() if self.session_id else ([], [], [], [])
            nonterminal = [row for row in accepted if row.get("job_status") in {"QUEUED", "HELD", "RUNNING", "WAITING", "CANCELLING"}]
            active = [row for row in nonterminal if row.get("job_status") in {"RUNNING", "WAITING", "CANCELLING"}]
            queued = [row for row in nonterminal if row.get("job_status") in {"QUEUED", "HELD"}]
            session = self.jobs.auto_session(self.session_id) if self.session_id else None
            try:
                saved_config = json.loads(session["config"]) if session else {}
            except (ValueError, TypeError):
                saved_config = {}
            return {
                "id": self.session_id, "state": self.state, "message": self.message,
                "niche": self.niche, "target": self.target,
                "generated": len(completed), "active": len(active), "queued": len(queued),
                "duplicates": sum(row["status"] == "REJECTED" for row in rows),
                "request_count": session["request_count"] if session else 0,
                "request_budget": self.settings.request_budget,
                "requests_per_minute": self.settings.requests_per_minute,
                "upload_enabled": bool(saved_config.get("youtube_upload_enabled", False)),
                "upload_privacy": saved_config.get("youtube_privacy_status", "private"),
                "topics": [row for row in accepted if row.get("job_id")],
                "events": self.jobs.auto_events(self.session_id) if self.session_id else [],
            }

    def close(self):
        with self._condition:
            self._closed = True
            self._epoch += 1
            self._condition.notify_all()
        self._executor.shutdown(wait=False, cancel_futures=True)


AutoModeController = AutoMode
