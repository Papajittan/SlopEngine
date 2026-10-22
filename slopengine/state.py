"""UI state updated exclusively from structured worker events."""

from dataclasses import dataclass, field
import time

STAGES = {
    "script": "Gemini · title and narration",
    "narration": "Edge TTS · synthesize speech",
    "timestamps": "Whisper · word timestamps",
    "footage": "Pexels · download footage",
    "render": "MoviePy · render video",
    "save": "Save video and metadata",
    "upload": "YouTube · optional upload",
}


@dataclass
class JobState:
    status: str = "Idle"
    stages: dict[str, str] = field(default_factory=lambda: dict.fromkeys(STAGES, "Pending"))
    started: float | None = None
    finished: float | None = None
    output: str = ""
    detail: str = ""

    def begin(self, upload: bool = False) -> None:
        self.status = "Starting"
        self.stages = dict.fromkeys(STAGES, "Pending")
        if not upload:
            self.stages["upload"] = "Skipped (not requested)"
        self.started = time.monotonic()
        self.finished = None
        self.output = self.detail = ""

    def apply(self, event: dict) -> None:
        kind, stage = event.get("type"), event.get("stage")
        if kind == "job_started":
            self.status = "Running"
        if stage in self.stages:
            status = {"stage_started": "Running", "stage_completed": "Completed", "stage_failed": "Failed"}.get(kind)
            if status:
                self.stages[stage] = status
        terminal = {"job_completed": "Completed", "job_failed": "Failed", "job_cancelled": "Cancelled"}.get(kind)
        if terminal:
            self.status = terminal
            self.finished = time.monotonic()
            for key, value in self.stages.items():
                if value == "Running":
                    self.stages[key] = "Failed" if terminal == "Failed" else terminal
                elif value == "Pending":
                    self.stages[key] = "Cancelled" if terminal == "Cancelled" else "Not run"
        self.output = str(event.get("output") or self.output)
        self.detail = str(event.get("message") or self.detail)

    @property
    def elapsed(self) -> int:
        return int((self.finished or time.monotonic()) - self.started) if self.started else 0
