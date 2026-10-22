"""One isolated generation job. Standard output is a JSON-lines event protocol."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import queue
import signal
import sys
import threading

import psutil

from .config_core import PipelineConfig
from .settings import build_config, redact, validate_config

class JobCancelled(KeyboardInterrupt):
    pass


def main() -> int:
    protocol = sys.stdout
    stage = ""
    rendered = None

    def emit(event: dict) -> None:
        nonlocal stage
        if event.get("stage"):
            stage = event["stage"]
        event = {"time": datetime.now(timezone.utc).isoformat(), **event}
        def safe(value):
            if isinstance(value, str):
                return redact(value)
            if isinstance(value, dict):
                return {key: safe(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [safe(item) for item in value]
            return value
        protocol.write(json.dumps(safe(event), default=str) + "\n")
        protocol.flush()

    class LogStream(io.TextIOBase):
        def write(self, value: str) -> int:
            for line in value.splitlines():
                if line.strip():
                    emit({"type": "log_message", "stage": stage, "level": "INFO", "message": line})
            return len(value)

        def flush(self) -> None:
            pass

    def cancel(signum, frame):
        raise JobCancelled()

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        payload = json.loads(sys.stdin.readline())
        topic = payload.get("topic", "")
        managed = payload.get("managed", False)
        if managed:
            snapshot = dict(payload["config"])
            for key in ("output_dir", "temp_root", "background_music_path", "youtube_client_secrets_file", "youtube_token_file"):
                if snapshot.get(key) is not None:
                    snapshot[key] = Path(snapshot[key])
            config = PipelineConfig(**snapshot)
        else:
            config = build_config(overrides=payload.get("overrides", {}))
        permits = queue.Queue()
        if managed:
            control = sys.stdin
            def controls():
                try:
                    for line in control:
                        permits.put(json.loads(line))
                except (ValueError, OSError):
                    pass
                # Manager death closes the control pipe even during a render.
                os.kill(os.getpid(), signal.SIGTERM)
            threading.Thread(target=controls, daemon=True).start()

        def stage_gate(stage):
            emit({"type": "stage_requested", "stage": stage})
            permit = permits.get()
            if permit != {"permit": stage}:
                raise JobCancelled()

        output = Path(payload["output"]).expanduser() if payload.get("output") else None
        errors = validate_config(config, topic=topic, output=output, require_keys=True)
        if errors:
            raise ValueError(" ".join(errors))
        with redirect_stdout(LogStream()), redirect_stderr(LogStream()):
            from shorts_core import ShortsPipeline, upload_rendered_short

            def progress(event: dict) -> None:
                # Completion belongs to this worker, including any requested upload.
                if event.get("type") not in {"job_completed", "job_failed", "job_cancelled"}:
                    emit(event)

            if managed:
                pipeline = ShortsPipeline(config, event_handler=progress, stage_gate=stage_gate,
                                          staging_path=output.parent / (".slopengine-" + payload["job_id"]))
            else:
                pipeline = ShortsPipeline(config, event_handler=progress)
            rendered = pipeline.run(topic, output)
            if config.youtube_upload_enabled:
                if managed:
                    stage_gate("upload")
                emit({"type": "stage_started", "stage": "upload", "message": "Uploading to YouTube"})
                upload_rendered_short(rendered, topic, config)
                emit({"type": "stage_completed", "stage": "upload"})
            emit({"type": "job_completed", "output": str(rendered), "message": "Video saved successfully."})
        return 0
    except JobCancelled:
        emit({"type": "job_cancelled", "stage": stage, "message": "Generation cancelled."})
        return 130
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        if rendered is not None:
            message += f" Video was saved at {rendered}; upload failed."
        emit({"type": "job_failed", "stage": stage, "level": "ERROR", "message": message,
              **({"output": str(rendered)} if rendered is not None else {})})
        return 1
    finally:
        # Do not leave ffmpeg/ML descendants after cancellation or a worker error.
        try:
            children = psutil.Process().children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(children, timeout=1)
            for child in alive:
                try:
                    child.kill()
                except psutil.Error:
                    pass
        except psutil.Error:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
