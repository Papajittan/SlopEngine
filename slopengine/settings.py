"""Lightweight configuration, preferences and safe diagnostics for the terminal app."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil

from .config_core import PipelineConfig, config_from_env

PREFERENCE_FIELDS = {
    "gemini_model", "edge_voice", "edge_rate", "edge_pitch", "edge_volume",
    "whisper_model", "whisper_device", "output_dir", "background_music_path",
    "background_music_volume", "caption_font_path", "caption_font_size",
    "caption_color", "caption_stroke_color", "caption_stroke_width", "caption_y_ratio",
    "youtube_privacy_status", "keep_temp", "tts_voice_overridden",
}
PATH_FIELDS = {
    "output_dir", "temp_root", "background_music_path",
    "youtube_client_secrets_file", "youtube_token_file",
}


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "slopengine"


def redact(value: object) -> str:
    text = str(value)
    for name, secret in os.environ.items():
        if any(marker in name.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")) and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,}]+", r"\1[REDACTED]", text)
    sensitive_key = r'''(?i)(["'](?:[a-z_]*api[_-]?key|access_token|refresh_token|client_secret|id_token|token|password|authorization)["']\s*:\s*)'''
    text = re.sub(sensitive_key + r'''"(?:[^"\\]|\\.)*"''', r'\1"[REDACTED]"', text)
    text = re.sub(sensitive_key + r"'(?:[^'\\]|\\.)*'", r"\1'[REDACTED]'", text)
    text = re.sub(r"(?i)((?:api[_-]?key|access_token|refresh_token|client_secret|key)\s*[=:]\s*)[^\s&,'\"}]+", r"\1[REDACTED]", text)
    return text


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else state_dir() / "preferences.json"

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {key: value for key, value in data.items() if key in PREFERENCE_FIELDS} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, preferences: dict) -> None:
        unknown = set(preferences) - PREFERENCE_FIELDS
        if unknown:
            raise ValueError("Only supported non-secret preferences may be saved.")
        errors = validate_config(build_config(preferences))
        if errors:
            raise ValueError(" ".join(errors))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(preferences, indent=2, default=str) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)


def build_config(preferences: dict | None = None, overrides: dict | None = None) -> PipelineConfig:
    """Environment defaults < persisted preferences < temporary job overrides."""
    config = config_from_env(require_keys=False)
    values = {key: value for key, value in (preferences or {}).items() if key in PREFERENCE_FIELDS}
    allowed = PREFERENCE_FIELDS | {"tts_voice_overridden", "youtube_upload_enabled"}
    values.update({key: value for key, value in (overrides or {}).items() if key in allowed})
    for key, value in values.items():
        expected = type(getattr(config, key))
        if value is not None and expected in (str, bool, int, float):
            if expected is float and isinstance(value, (int, float)) and not isinstance(value, bool):
                continue
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise ValueError(f"{key} has an invalid value type; reset this preference.")
    for key in PATH_FIELDS:
        if key in values:
            if values[key] is not None and not isinstance(values[key], (str, Path)):
                raise ValueError(f"{key} must be a filesystem path.")
            if key == "output_dir" and not values[key]:
                raise ValueError("Output directory is required.")
            try:
                values[key] = Path(values[key]).expanduser() if values[key] else None
            except TypeError:
                raise ValueError(f"{key} must be a filesystem path.") from None
    if "caption_font_path" in values:
        if values["caption_font_path"] is not None and not isinstance(values["caption_font_path"], str):
            raise ValueError("caption_font_path must be a filesystem path.")
        values["caption_font_path"] = values["caption_font_path"] or None
    # Environment upload defaults never count as consent in the interactive app.
    values["youtube_upload_enabled"] = bool((overrides or {}).get("youtube_upload_enabled", False))
    return replace(config, **values)


def validate_config(config: PipelineConfig, topic: str | None = None, output: Path | str | None = None,
                    require_keys: bool = False) -> list[str]:
    errors = []
    for name in PREFERENCE_FIELDS:
        value = getattr(config, name)
        if name in {"background_music_path", "caption_font_path"} and value is None:
            continue
        if name in {"output_dir", "background_music_path", "caption_font_path"}:
            if not isinstance(value, (str, Path)):
                errors.append(f"{name} must be a filesystem path.")
        elif name in {"keep_temp", "tts_voice_overridden"}:
            if not isinstance(value, bool):
                errors.append(f"{name} must be true or false.")
        elif name in {"background_music_volume", "caption_font_size", "caption_stroke_width", "caption_y_ratio"}:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"{name} must be numeric.")
        elif not isinstance(value, str):
            errors.append(f"{name} must be text.")
    if errors:
        return errors
    if topic is not None and not topic.strip():
        errors.append("Enter a topic or generation prompt.")
    if require_keys:
        for key in ("gemini_api_key", "pexels_api_key"):
            if not getattr(config, key):
                errors.append(f"Configure {key.upper()} in .env; see .env.example.")
        if not shutil.which("ffmpeg"):
            errors.append("FFmpeg is missing. Install ffmpeg and add it to PATH.")
    for field, pattern, example in (("edge_rate", r"[+-]\d+%", "+7%"), ("edge_volume", r"[+-]\d+%", "+0%"), ("edge_pitch", r"[+-]\d+Hz", "-3Hz")):
        if not re.fullmatch(pattern, str(getattr(config, field))):
            errors.append(f"{field} must use a signed value such as {example}.")
    if config.whisper_device not in {"cpu", "cuda"} and not re.fullmatch(r"cuda:\d+", config.whisper_device):
        errors.append("Whisper device must be cpu, cuda, or cuda:N; cpu is recommended.")
    if not config.whisper_model.strip() or not config.gemini_model.strip():
        errors.append("Gemini and Whisper model names are required.")
    try:
        if not 0 <= float(config.background_music_volume) <= 1:
            errors.append("Music volume must be between 0 and 1.")
        if not 8 <= int(config.caption_font_size) <= 500:
            errors.append("Caption font size must be between 8 and 500.")
        if not 0 <= float(config.caption_y_ratio) <= 1 or int(config.caption_stroke_width) < 0:
            errors.append("Caption position must be between 0 and 1 and stroke width non-negative.")
    except (ValueError, TypeError):
        errors.append("Caption and music settings must be numeric.")
    if config.youtube_privacy_status not in {"private", "unlisted", "public"}:
        errors.append("YouTube privacy must be private, unlisted, or public.")
    for label, path in (("Background music", config.background_music_path), ("Caption font", config.caption_font_path)):
        if path and not Path(path).is_file():
            errors.append(f"{label} file does not exist.")
    if config.output_dir is None:
        errors.append("Output directory is required.")
    elif Path(config.output_dir).exists() and not Path(config.output_dir).is_dir():
        errors.append("Output directory points to a file.")
    if output:
        target = Path(output).expanduser()
        if target.suffix.lower() != ".mp4":
            errors.append("Output filename must end with .mp4.")
        if target.exists() or target.with_suffix(".metadata.json").exists():
            errors.append("Output already exists. Choose a different filename.")
        if target.parent.exists() and not target.parent.is_dir():
            errors.append("Output parent is not a directory.")
    return errors
