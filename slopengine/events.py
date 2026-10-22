"""UI-independent pipeline events and secret-safe message formatting."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, TypedDict

STAGES = ("script", "narration", "timestamps", "footage", "render", "save", "upload")


class ProgressEvent(TypedDict, total=False):
    type: str
    time: str
    stage: str
    message: str
    output: str
    level: str


EventHandler = Callable[[ProgressEvent], None]


def make_event(event_type: str, stage: str | None = None, **details: Any) -> ProgressEvent:
    event: ProgressEvent = {"type": event_type, "time": datetime.now(timezone.utc).isoformat()}
    if stage is not None:
        event["stage"] = stage
    event.update(details)
    return event


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|access_token|refresh_token|client_secret|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,}]+", r"\1[REDACTED]", text)
    return text
