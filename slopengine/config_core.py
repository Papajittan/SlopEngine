"""Lightweight shared pipeline configuration; no media dependencies."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

ENV_CONFIG = {
    "GEMINI_API_KEY": "replace-with-google-ai-studio-key-or-export-env",
    "PEXELS_API_KEY": "replace-with-pexels-api-key-or-export-env",
}

DEFAULT_CONFIG = {
    "gemini_model": "gemini-2.5-flash",
    "gemini_timeout_seconds": 60,
    "edge_voice": "en-US-AriaNeural",
    "whisper_model": "base",
    "width": 1080,
    "height": 1920,
    "fps": 30,
}


class PipelineError(RuntimeError):
    """Raised when a required media generation step cannot proceed."""


class TimeoutError(PipelineError):
    """Raised when an external API call exceeds the configured timeout."""


@dataclass(frozen=True)
class PipelineConfig:
    gemini_api_key: str
    pexels_api_key: str
    gemini_model: str = DEFAULT_CONFIG["gemini_model"]
    gemini_timeout_seconds: int = DEFAULT_CONFIG["gemini_timeout_seconds"]
    edge_voice: str = DEFAULT_CONFIG["edge_voice"]
    tts_voice_overridden: bool = False
    edge_rate: str = "+7%"
    edge_volume: str = "+0%"
    edge_pitch: str = "-3Hz"
    whisper_model: str = DEFAULT_CONFIG["whisper_model"]
    whisper_device: str = "cpu"
    output_dir: Path = Path("outputs")
    temp_root: Path = Path(tempfile.gettempdir()) / "shorts_core"
    width: int = DEFAULT_CONFIG["width"]
    height: int = DEFAULT_CONFIG["height"]
    fps: int = DEFAULT_CONFIG["fps"]
    caption_font_path: str | None = None
    caption_font_size: int = 220
    caption_color: str = "#FFFFFF"
    caption_stroke_color: str = "#101014"
    caption_stroke_width: int = 10
    caption_y_ratio: float = 0.5
    background_music_path: Path | None = None
    background_music_volume: float = 0.12
    pexels_per_page: int = 12
    request_timeout_seconds: int = 20
    download_timeout_seconds: int = 90
    max_download_bytes: int = 240 * 1024 * 1024
    cleanup_max_age_hours: int = 6
    threads: int = max(1, (os.cpu_count() or 2) - 1)
    keep_temp: bool = False
    youtube_upload_enabled: bool = False
    youtube_client_secrets_file: Path = Path("client_secrets.json")
    youtube_token_file: Path = Path(".youtube_token.json")
    youtube_privacy_status: str = "private"
    youtube_category_id: str = "22"


def _candidate_env_paths(env_path: Path | None = None) -> list[Path]:
    if env_path is not None:
        return [env_path]

    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    if key.startswith("export "):
        key = key.removeprefix("export ").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return None

    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def load_environment(env_path: Path | None = None) -> None:
    paths = _candidate_env_paths(env_path)

    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None

    if load_dotenv is not None:
        for path in paths:
            if path.exists():
                load_dotenv(dotenv_path=path, override=False)
        return

    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                parsed = _parse_env_line(line)
                if parsed is None:
                    continue
                key, value = parsed
                os.environ.setdefault(key, value)


def bool_from_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise PipelineError(f"{name} must be true or false.")


def youtube_privacy_from_env() -> str:
    privacy_status = os.environ.get("YOUTUBE_PRIVACY_STATUS", "private").strip().lower() or "private"
    if privacy_status not in {"private", "unlisted", "public"}:
        raise PipelineError("YOUTUBE_PRIVACY_STATUS must be private, unlisted, or public.")
    return privacy_status


def gemini_key_is_configured(value: str) -> bool:
    """Check presence without treating template values as credentials."""
    return value.strip().lower() not in {
        "", "your_gemini_key_here", ENV_CONFIG["GEMINI_API_KEY"].lower(),
    }


def config_from_env(require_keys: bool = True) -> PipelineConfig:
    load_environment()

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key_is_configured(gemini_key):
        gemini_key = ""
    pexels_key = os.environ.get("PEXELS_API_KEY", "").strip()
    if require_keys and not gemini_key:
        raise PipelineError("GEMINI_API_KEY is required.")
    if require_keys and not pexels_key:
        raise PipelineError("PEXELS_API_KEY is required.")

    music_path = os.environ.get("BACKGROUND_MUSIC_PATH", "").strip()

    return PipelineConfig(
        gemini_api_key=gemini_key,
        pexels_api_key=pexels_key,
        gemini_model=os.environ.get("GEMINI_MODEL", DEFAULT_CONFIG["gemini_model"]),
        gemini_timeout_seconds=int(
            os.environ.get("GEMINI_TIMEOUT_SECONDS", str(DEFAULT_CONFIG["gemini_timeout_seconds"]))
        ),
        edge_voice=os.environ.get("EDGE_TTS_VOICE", DEFAULT_CONFIG["edge_voice"]),
        edge_rate=os.environ.get("EDGE_TTS_RATE", "+7%"),
        edge_volume=os.environ.get("EDGE_TTS_VOLUME", "+0%"),
        edge_pitch=os.environ.get("EDGE_TTS_PITCH", "-3Hz"),
        whisper_model=os.environ.get("WHISPER_MODEL", DEFAULT_CONFIG["whisper_model"]),
        whisper_device=os.environ.get("WHISPER_DEVICE", "cpu"),
        output_dir=Path(os.environ.get("SHORTS_OUTPUT_DIR", "outputs")),
        temp_root=Path(os.environ.get("SHORTS_TEMP_ROOT", str(Path(tempfile.gettempdir()) / "shorts_core"))),
        caption_font_path=os.environ.get("CAPTION_FONT_PATH") or None,
        caption_font_size=int(os.environ.get("CAPTION_FONT_SIZE", "220")),
        background_music_path=Path(music_path) if music_path else None,
        background_music_volume=float(os.environ.get("BACKGROUND_MUSIC_VOLUME", "0.12")),
        youtube_upload_enabled=bool_from_env("YOUTUBE_UPLOAD_ENABLED", False),
        youtube_client_secrets_file=Path(os.environ.get("YOUTUBE_CLIENT_SECRETS_FILE", "client_secrets.json")),
        youtube_token_file=Path(os.environ.get("YOUTUBE_TOKEN_FILE", ".youtube_token.json")),
        youtube_privacy_status=youtube_privacy_from_env(),
        youtube_category_id=os.environ.get("YOUTUBE_CATEGORY_ID", "22").strip() or "22",
    )


