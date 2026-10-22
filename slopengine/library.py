"""Discover existing outputs without modifying user files."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


@dataclass
class OutputEntry:
    path: Path
    title: str
    topic: str
    created: datetime
    size: int
    metadata: dict
    youtube: str = "Not uploaded"
    metadata_error: str = ""


def discover_outputs(directory: Path | str) -> list[OutputEntry]:
    entries = []
    try:
        paths = list(Path(directory).glob("*.mp4"))
    except OSError:
        return []
    for path in paths:
        if ".partial" in path.name:
            continue
        try:
            stat = path.stat()
            if not path.is_file() or not stat.st_size:
                continue
            metadata = {}
            error = ""
            try:
                metadata = json.loads(path.with_suffix(".metadata.json").read_text(encoding="utf-8"))
                if not isinstance(metadata, dict):
                    metadata = {}
                    error = "Metadata must be a JSON object."
            except FileNotFoundError:
                error = "No metadata available."
            except (OSError, ValueError):
                error = "Metadata could not be read."
            payload = metadata.get("script_payload", {})
            youtube = metadata.get("youtube", {})
            entries.append(OutputEntry(path.resolve(), str(payload.get("title") or path.stem) if isinstance(payload, dict) else path.stem,
                                       str(metadata.get("topic") or ""), datetime.fromtimestamp(stat.st_mtime, timezone.utc), stat.st_size,
                                       metadata, str(youtube.get("url") or youtube.get("video_id") or "Uploaded") if isinstance(youtube, dict) and youtube else "Not uploaded", error))
        except OSError:
            continue
    return sorted(entries, key=lambda entry: entry.created, reverse=True)


def open_output(path: Path | str) -> None:
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("The selected output no longer exists.")
    if sys.platform == "win32":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
