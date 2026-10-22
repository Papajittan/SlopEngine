"""Process-backed generation with event streaming, logs and group cancellation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

from .settings import redact, state_dir


class WorkerProcess:
    def __init__(self, log_dir: Path | None = None):
        self.log_dir = Path(log_dir) if log_dir is not None else state_dir() / "logs"
        self.log_path: Path | None = None
        self.process: subprocess.Popen | None = None
        self._events = queue.Queue()
        self._readers: list[threading.Thread] = []
        self._terminal = False
        self._cancel_requested = False
        self._cancel_thread: threading.Thread | None = None
        self._exited_at: float | None = None
        self._exit_handled = False

    @property
    def running(self) -> bool:
        return self.process is not None and (self.process.poll() is None or not self._exit_handled)

    def start(self, topic: str, output: Path | str | None = None, overrides: dict | None = None, *, payload: dict | None = None) -> None:
        if self.running:
            raise RuntimeError("A generation job is already running.")
        self.poll()
        self._terminal = False
        self._cancel_requested = False
        self._exited_at = None
        self._exit_handled = False
        # A reader from a crashed job must never publish into a subsequent job.
        self._events = queue.Queue()
        self.job_id = (payload or {}).get("job_id")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.jsonl"
        self.log_path.touch(mode=0o600)
        self.process = subprocess.Popen([sys.executable, "-u", "-m", "slopengine.worker"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace", start_new_session=os.name == "posix",
                                        env={**os.environ, "SLOPENGINE_JOB_ID": (payload or {}).get("job_id", "")})
        process = self.process
        self._readers = []
        for stream, protocol in ((process.stdout, True), (process.stderr, False)):
            thread = threading.Thread(target=self._read_stream, args=(stream, protocol, self._events), daemon=True)
            thread.start()
            self._readers.append(thread)
        try:
            process.stdin.write(json.dumps(payload or {"topic": topic, "output": str(output) if output else None,
                                           "overrides": overrides or {}}, default=str) + "\n")
            process.stdin.flush()
        except (OSError, BrokenPipeError):
            # The exit watcher reports early worker termination with context.
            pass

    def _read_stream(self, stream, protocol: bool, event_queue) -> None:
        try:
            for line in stream:
                try:
                    event = json.loads(line) if protocol else None
                    if not isinstance(event, dict) or "type" not in event:
                        raise ValueError()
                except (ValueError, TypeError):
                    event = {"type": "log_message", "level": "INFO" if protocol else "ERROR", "message": line.strip()}
                event_queue.put(event)
        finally:
            stream.close()

    def _record(self, event: dict) -> dict:
        # Redact individual values so redaction never changes JSON quoting.
        def safe(value):
            if isinstance(value, str):
                return redact(value)
            if isinstance(value, dict):
                return {key: safe(item) for key, item in value.items()}
            if isinstance(value, list):
                return [safe(item) for item in value]
            return value
        event = safe({"time": datetime.now(timezone.utc).isoformat(), **event,
                      **({"job_id": self.job_id} if getattr(self, "job_id", None) else {})})
        if self.log_path:
            try:
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event) + "\n")
            except OSError:
                pass
        return event

    def poll(self) -> list[dict]:
        events = []
        def drain_events():
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                if event.get("type") in {"job_completed", "job_failed", "job_cancelled"}:
                    self._terminal = True
                events.append(self._record(event))
        drain_events()
        if self.process is not None and self.process.poll() is not None and not self._exit_handled:
            if self._exited_at is None:
                self._exited_at = time.monotonic()
            # Give final protocol events time to drain. Descendants may inherit
            # pipes, so EOF must never be a prerequisite for detecting a crash.
            drained = not any(thread.is_alive() for thread in self._readers)
            if drained or time.monotonic() - self._exited_at >= 0.2:
                # A reader may have queued the final event immediately after
                # the first drain and then exited. Read it before crash inference.
                drain_events()
                self._exit_handled = True
                if os.name == "posix":
                    self._signal_process(self.process, signal.SIGKILL)
                if not self._terminal:
                    self._terminal = True
                    events.append(self._record({"type": "job_cancelled" if self._cancel_requested else "job_failed",
                                               "level": "ERROR", "message": "Generation cancelled." if self._cancel_requested else
                                               f"Worker exited unexpectedly (status {self.process.returncode}). Check dependencies and job logs."}))
        return events

    @staticmethod
    def _signal_process(process, sig) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass

    def cancel(self) -> None:
        if not self.running or self._cancel_requested:
            return
        self._cancel_requested = True
        process = self.process
        self._signal_process(process, signal.SIGTERM)

        def reap():
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self._signal_process(process, getattr(signal, "SIGKILL", signal.SIGTERM))
                process.wait()
            finally:
                # Also reap subprocess descendants if the worker exited first.
                if os.name == "posix":
                    self._signal_process(process, signal.SIGKILL)

        self._cancel_thread = threading.Thread(target=reap, daemon=True)
        self._cancel_thread.start()

    def close(self) -> None:
        self.cancel()
        if self._cancel_thread:
            self._cancel_thread.join(timeout=6)
        for thread in self._readers:
            thread.join(timeout=1)
        self.poll()
        if self.process and self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()


def read_logs(path: Path) -> list[dict]:
    """Malformed or interrupted JSONL records do not prevent inspection."""
    events = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except ValueError:
                continue
    except OSError:
        pass
    return events
