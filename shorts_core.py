#!/usr/bin/env python3
"""
Autonomous vertical short-form video core pipeline.

Engines:
- google-genai for narration and Pexels keyword synthesis.
- edge-tts for cloud neural TTS.
- openai-whisper for word-level timestamps.
- Pexels Video API for dynamic background video retrieval.
- MoviePy for 9:16 MP4 compositing.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

import edge_tts
import numpy as np
import requests
import whisper
from PIL import Image, ImageDraw, ImageFont

try:
    from moviepy import (
        AudioFileClip,
        ColorClip,
        CompositeAudioClip,
        CompositeVideoClip,
        ImageClip,
        VideoFileClip,
        concatenate_videoclips,
    )
except ImportError:  # MoviePy 1.x fallback.
    from moviepy.editor import (  # type: ignore
        AudioFileClip,
        ColorClip,
        CompositeAudioClip,
        CompositeVideoClip,
        ImageClip,
        VideoFileClip,
        concatenate_videoclips,
    )

try:
    from google import genai as google_genai
except ImportError:
    google_genai = None

try:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2.credentials import Credentials as GoogleCredentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build as google_api_build
    from googleapiclient.http import MediaFileUpload
except ImportError:
    GoogleAuthRequest = None
    GoogleCredentials = None
    InstalledAppFlow = None
    google_api_build = None
    MediaFileUpload = None


ENV_CONFIG = {
    "GEMINI_API_KEY": "replace-with-google-ai-studio-key-or-export-env",
    "PEXELS_API_KEY": "replace-with-pexels-api-key-or-export-env",
}

DEFAULT_CONFIG = {
    "gemini_model": "gemini-2.5-flash",
    "edge_voice": "en-US-AriaNeural",
    "whisper_model": "base",
    "width": 1080,
    "height": 1920,
    "fps": 30,
}


EDGE_TTS_DEFAULT_VOICES_BY_LOCALE = {
    "af-za": "af-ZA-AdriNeural",
    "am-et": "am-ET-MekdesNeural",
    "ar-ae": "ar-AE-FatimaNeural",
    "ar-bh": "ar-BH-LailaNeural",
    "ar-dz": "ar-DZ-AminaNeural",
    "ar-eg": "ar-EG-SalmaNeural",
    "ar-iq": "ar-IQ-RanaNeural",
    "ar-jo": "ar-JO-SanaNeural",
    "ar-kw": "ar-KW-NouraNeural",
    "ar-lb": "ar-LB-LaylaNeural",
    "ar-ly": "ar-LY-ImanNeural",
    "ar-ma": "ar-MA-MounaNeural",
    "ar-om": "ar-OM-AyshaNeural",
    "ar-qa": "ar-QA-AmalNeural",
    "ar-sa": "ar-SA-ZariyahNeural",
    "ar-sy": "ar-SY-AmanyNeural",
    "ar-tn": "ar-TN-ReemNeural",
    "ar-ye": "ar-YE-MaryamNeural",
    "as-in": "as-IN-YashicaNeural",
    "az-az": "az-AZ-BanuNeural",
    "bg-bg": "bg-BG-KalinaNeural",
    "bn-bd": "bn-BD-NabanitaNeural",
    "bn-in": "bn-IN-TanishaaNeural",
    "bs-ba": "bs-BA-VesnaNeural",
    "ca-es": "ca-ES-JoanaNeural",
    "cs-cz": "cs-CZ-VlastaNeural",
    "cy-gb": "cy-GB-NiaNeural",
    "da-dk": "da-DK-ChristelNeural",
    "de-at": "de-AT-IngridNeural",
    "de-ch": "de-CH-LeniNeural",
    "de-de": "de-DE-KatjaNeural",
    "el-gr": "el-GR-AthinaNeural",
    "en-au": "en-AU-NatashaNeural",
    "en-ca": "en-CA-ClaraNeural",
    "en-gb": "en-GB-SoniaNeural",
    "en-hk": "en-HK-YanNeural",
    "en-ie": "en-IE-EmilyNeural",
    "en-in": "en-IN-NeerjaNeural",
    "en-ke": "en-KE-AsiliaNeural",
    "en-ng": "en-NG-EzinneNeural",
    "en-nz": "en-NZ-MollyNeural",
    "en-ph": "en-PH-RosaNeural",
    "en-sg": "en-SG-LunaNeural",
    "en-tz": "en-TZ-ImaniNeural",
    "en-us": "en-US-AriaNeural",
    "en-za": "en-ZA-LeahNeural",
    "es-ar": "es-AR-ElenaNeural",
    "es-bo": "es-BO-SofiaNeural",
    "es-cl": "es-CL-CatalinaNeural",
    "es-co": "es-CO-SalomeNeural",
    "es-cr": "es-CR-MariaNeural",
    "es-cu": "es-CU-BelkysNeural",
    "es-do": "es-DO-RamonaNeural",
    "es-ec": "es-EC-AndreaNeural",
    "es-es": "es-ES-ElviraNeural",
    "es-gq": "es-GQ-TeresaNeural",
    "es-gt": "es-GT-MartaNeural",
    "es-hn": "es-HN-KarlaNeural",
    "es-mx": "es-MX-DaliaNeural",
    "es-ni": "es-NI-YolandaNeural",
    "es-pa": "es-PA-MargaritaNeural",
    "es-pe": "es-PE-CamilaNeural",
    "es-pr": "es-PR-KarinaNeural",
    "es-py": "es-PY-TaniaNeural",
    "es-sv": "es-SV-LorenaNeural",
    "es-us": "es-US-PalomaNeural",
    "es-uy": "es-UY-ValentinaNeural",
    "es-ve": "es-VE-PaolaNeural",
    "et-ee": "et-EE-AnuNeural",
    "eu-es": "eu-ES-AinhoaNeural",
    "fa-ir": "fa-IR-DilaraNeural",
    "fi-fi": "fi-FI-SelmaNeural",
    "fil-ph": "fil-PH-BlessicaNeural",
    "fr-be": "fr-BE-CharlineNeural",
    "fr-ca": "fr-CA-SylvieNeural",
    "fr-ch": "fr-CH-ArianeNeural",
    "fr-fr": "fr-FR-DeniseNeural",
    "ga-ie": "ga-IE-OrlaNeural",
    "gl-es": "gl-ES-SabelaNeural",
    "gu-in": "gu-IN-DhwaniNeural",
    "he-il": "he-IL-HilaNeural",
    "hi-in": "hi-IN-SwaraNeural",
    "hr-hr": "hr-HR-GabrijelaNeural",
    "hu-hu": "hu-HU-NoemiNeural",
    "hy-am": "hy-AM-AnahitNeural",
    "id-id": "id-ID-GadisNeural",
    "is-is": "is-IS-GudrunNeural",
    "it-it": "it-IT-IsabellaNeural",
    "iu-cans-ca": "iu-Cans-CA-SiqiniqNeural",
    "iu-latn-ca": "iu-Latn-CA-SiqiniqNeural",
    "ja-jp": "ja-JP-NanamiNeural",
    "jv-id": "jv-ID-SitiNeural",
    "ka-ge": "ka-GE-EkaNeural",
    "kk-kz": "kk-KZ-AigulNeural",
    "km-kh": "km-KH-SreymomNeural",
    "kn-in": "kn-IN-SapnaNeural",
    "ko-kr": "ko-KR-SunHiNeural",
    "lo-la": "lo-LA-KeomanyNeural",
    "lt-lt": "lt-LT-OnaNeural",
    "lv-lv": "lv-LV-EveritaNeural",
    "mk-mk": "mk-MK-MarijaNeural",
    "ml-in": "ml-IN-SobhanaNeural",
    "mn-mn": "mn-MN-YesuiNeural",
    "mr-in": "mr-IN-AarohiNeural",
    "ms-my": "ms-MY-YasminNeural",
    "mt-mt": "mt-MT-GraceNeural",
    "my-mm": "my-MM-NilarNeural",
    "nb-no": "nb-NO-PernilleNeural",
    "ne-np": "ne-NP-HemkalaNeural",
    "nl-be": "nl-BE-DenaNeural",
    "nl-nl": "nl-NL-FennaNeural",
    "or-in": "or-IN-SubhasiniNeural",
    "pa-in": "pa-IN-VaaniNeural",
    "pl-pl": "pl-PL-ZofiaNeural",
    "ps-af": "ps-AF-LatifaNeural",
    "pt-br": "pt-BR-FranciscaNeural",
    "pt-pt": "pt-PT-RaquelNeural",
    "ro-ro": "ro-RO-AlinaNeural",
    "ru-ru": "ru-RU-SvetlanaNeural",
    "si-lk": "si-LK-ThiliniNeural",
    "sk-sk": "sk-SK-ViktoriaNeural",
    "sl-si": "sl-SI-PetraNeural",
    "so-so": "so-SO-UbaxNeural",
    "sq-al": "sq-AL-AnilaNeural",
    "sr-latn-rs": "sr-Latn-RS-SophieNeural",
    "sr-rs": "sr-RS-SophieNeural",
    "su-id": "su-ID-TutiNeural",
    "sv-se": "sv-SE-SofieNeural",
    "sw-ke": "sw-KE-ZuriNeural",
    "sw-tz": "sw-TZ-RehemaNeural",
    "ta-in": "ta-IN-PallaviNeural",
    "ta-lk": "ta-LK-SaranyaNeural",
    "ta-my": "ta-MY-KaniNeural",
    "ta-sg": "ta-SG-VenbaNeural",
    "te-in": "te-IN-ShrutiNeural",
    "th-th": "th-TH-PremwadeeNeural",
    "tr-tr": "tr-TR-EmelNeural",
    "uk-ua": "uk-UA-PolinaNeural",
    "ur-in": "ur-IN-GulNeural",
    "ur-pk": "ur-PK-UzmaNeural",
    "uz-uz": "uz-UZ-MadinaNeural",
    "vi-vn": "vi-VN-HoaiMyNeural",
    "wuu-cn": "wuu-CN-XiaotongNeural",
    "yue-cn": "yue-CN-XiaoMinNeural",
    "zh-cn": "zh-CN-XiaoxiaoNeural",
    "zh-cn-guangxi": "zh-CN-guangxi-YunqiNeural",
    "zh-cn-henan": "zh-CN-henan-YundengNeural",
    "zh-cn-liaoning": "zh-CN-liaoning-XiaobeiNeural",
    "zh-cn-shaanxi": "zh-CN-shaanxi-XiaoniNeural",
    "zh-cn-shandong": "zh-CN-shandong-YunxiangNeural",
    "zh-cn-sichuan": "zh-CN-sichuan-YunxiNeural",
    "zh-hk": "zh-HK-HiuMaanNeural",
    "zh-tw": "zh-TW-HsiaoChenNeural",
    "zu-za": "zu-ZA-ThandoNeural",
}

EDGE_TTS_DEFAULT_LOCALE_BY_LANGUAGE = {
    "af": "af-za",
    "am": "am-et",
    "ar": "ar-sa",
    "as": "as-in",
    "az": "az-az",
    "bg": "bg-bg",
    "bn": "bn-in",
    "bs": "bs-ba",
    "ca": "ca-es",
    "cs": "cs-cz",
    "cy": "cy-gb",
    "da": "da-dk",
    "de": "de-de",
    "el": "el-gr",
    "en": "en-us",
    "es": "es-es",
    "et": "et-ee",
    "eu": "eu-es",
    "fa": "fa-ir",
    "fi": "fi-fi",
    "fil": "fil-ph",
    "fr": "fr-fr",
    "ga": "ga-ie",
    "gl": "gl-es",
    "gu": "gu-in",
    "he": "he-il",
    "hi": "hi-in",
    "hr": "hr-hr",
    "hu": "hu-hu",
    "hy": "hy-am",
    "id": "id-id",
    "is": "is-is",
    "it": "it-it",
    "iu": "iu-cans-ca",
    "iu-cans": "iu-cans-ca",
    "iu-latn": "iu-latn-ca",
    "ja": "ja-jp",
    "jv": "jv-id",
    "ka": "ka-ge",
    "kk": "kk-kz",
    "km": "km-kh",
    "kn": "kn-in",
    "ko": "ko-kr",
    "lo": "lo-la",
    "lt": "lt-lt",
    "lv": "lv-lv",
    "mk": "mk-mk",
    "ml": "ml-in",
    "mn": "mn-mn",
    "mr": "mr-in",
    "ms": "ms-my",
    "mt": "mt-mt",
    "my": "my-mm",
    "nb": "nb-no",
    "ne": "ne-np",
    "nl": "nl-nl",
    "or": "or-in",
    "pa": "pa-in",
    "pl": "pl-pl",
    "ps": "ps-af",
    "pt": "pt-br",
    "ro": "ro-ro",
    "ru": "ru-ru",
    "si": "si-lk",
    "sk": "sk-sk",
    "sl": "sl-si",
    "so": "so-so",
    "sq": "sq-al",
    "sr": "sr-rs",
    "sr-latn": "sr-latn-rs",
    "su": "su-id",
    "sv": "sv-se",
    "sw": "sw-ke",
    "ta": "ta-in",
    "te": "te-in",
    "th": "th-th",
    "tr": "tr-tr",
    "uk": "uk-ua",
    "ur": "ur-pk",
    "uz": "uz-uz",
    "vi": "vi-vn",
    "wuu": "wuu-cn",
    "yue": "yue-cn",
    "zh": "zh-cn",
    "zh-hans": "zh-cn",
    "zh-hant": "zh-tw",
    "zu": "zu-za",
}


STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "because",
    "before",
    "being",
    "between",
    "could",
    "every",
    "from",
    "have",
    "into",
    "just",
    "like",
    "more",
    "most",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "through",
    "when",
    "where",
    "with",
    "without",
    "your",
}


class PipelineError(RuntimeError):
    """Raised when a required media generation step cannot proceed."""


class TimeoutError(PipelineError):
    """Raised when an external API call exceeds the configured timeout."""


class AlarmTimeout:
    def __init__(self, seconds: int, label: str) -> None:
        self.seconds = max(1, int(seconds))
        self.label = label
        self.previous_handler: Any | None = None

    def __enter__(self) -> None:
        if not hasattr(signal, "SIGALRM"):
            return
        self.previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self._handle_timeout)
        signal.alarm(self.seconds)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if not hasattr(signal, "SIGALRM"):
            return
        signal.alarm(0)
        if self.previous_handler is not None:
            signal.signal(signal.SIGALRM, self.previous_handler)

    def _handle_timeout(self, signum: int, frame: Any) -> None:
        raise TimeoutError(f"{self.label} exceeded {self.seconds} seconds.")


@dataclass(frozen=True)
class PipelineConfig:
    gemini_api_key: str
    pexels_api_key: str
    gemini_model: str = DEFAULT_CONFIG["gemini_model"]
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


@dataclass(frozen=True)
class ScriptPayload:
    title: str
    script: str
    keywords: list[str]
    pexels_query: str
    language: str = ""


@dataclass(frozen=True)
class WordEvent:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class VideoCandidate:
    url: str
    video_id: int | str
    duration: float
    width: int
    height: int
    quality: str


def _candidate_env_paths(env_path: Path | None = None) -> list[Path]:
    if env_path is not None:
        return [env_path]

    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
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


def config_from_env() -> PipelineConfig:
    load_environment()

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    pexels_key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not gemini_key:
        raise PipelineError("GEMINI_API_KEY is required.")
    if not pexels_key:
        raise PipelineError("PEXELS_API_KEY is required.")

    music_path = os.environ.get("BACKGROUND_MUSIC_PATH", "").strip()

    return PipelineConfig(
        gemini_api_key=gemini_key,
        pexels_api_key=pexels_key,
        gemini_model=os.environ.get("GEMINI_MODEL", DEFAULT_CONFIG["gemini_model"]),
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


def slugify(value: str, max_len: int = 72) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return (value or "short")[:max_len].strip("-")


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_language_tag(value: str) -> str:
    raw = normalize_spaces(str(value or "")).replace("_", "-").strip(" .")
    if not raw or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}", raw):
        return ""

    parts = raw.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def edge_tts_voice_for_language(language: str, fallback_voice: str) -> str:
    tag = normalize_language_tag(language)
    if not tag:
        return fallback_voice

    key = tag.lower()
    voice = EDGE_TTS_DEFAULT_VOICES_BY_LOCALE.get(key)
    if voice:
        return voice

    parts = key.split("-")
    language_keys = [key]
    if len(parts) >= 2:
        language_keys.append("-".join(parts[:2]))
    language_keys.append(parts[0])

    for language_key in language_keys:
        default_locale = EDGE_TTS_DEFAULT_LOCALE_BY_LANGUAGE.get(language_key)
        if default_locale:
            return EDGE_TTS_DEFAULT_VOICES_BY_LOCALE[default_locale]

    return fallback_voice


def extract_keywords(text: str, limit: int = 6) -> list[str]:
    tokens = re.findall(r"[a-zA-Z][a-zA-Z'-]{3,}", text.lower())
    counts = Counter(token.strip("'") for token in tokens if token not in STOPWORDS)
    return [word for word, _ in counts.most_common(limit)]


def extract_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def close_all(clips: Iterable[Any]) -> None:
    for clip in clips:
        if clip is None:
            continue
        close = getattr(clip, "close", None)
        if callable(close):
            close()


def clip_with_duration(clip: Any, duration: float) -> Any:
    if hasattr(clip, "with_duration"):
        return clip.with_duration(duration)
    return clip.set_duration(duration)


def clip_with_start(clip: Any, start: float) -> Any:
    if hasattr(clip, "with_start"):
        return clip.with_start(start)
    return clip.set_start(start)


def clip_with_position(clip: Any, position: Any) -> Any:
    if hasattr(clip, "with_position"):
        return clip.with_position(position)
    return clip.set_position(position)


def clip_with_audio(clip: Any, audio: Any) -> Any:
    if hasattr(clip, "with_audio"):
        return clip.with_audio(audio)
    return clip.set_audio(audio)


def clip_with_volume(clip: Any, volume: float) -> Any:
    if hasattr(clip, "with_volume_scaled"):
        return clip.with_volume_scaled(volume)
    if hasattr(clip, "volumex"):
        return clip.volumex(volume)

    try:
        from moviepy.audio.fx.MultiplyVolume import MultiplyVolume

        return clip.with_effects([MultiplyVolume(volume)])
    except Exception:
        pass

    try:
        from moviepy.audio.fx.all import volumex

        return clip.fx(volumex, volume)
    except Exception:
        return clip


def clip_with_fps(clip: Any, fps: int) -> Any:
    if hasattr(clip, "with_fps"):
        return clip.with_fps(fps)
    return clip.set_fps(fps)


def clip_without_audio(clip: Any) -> Any:
    without_audio = getattr(clip, "without_audio", None)
    if callable(without_audio):
        return without_audio()
    if hasattr(clip, "set_audio"):
        return clip.set_audio(None)
    return clip


def clip_subclipped(clip: Any, start: float, end: float) -> Any:
    if hasattr(clip, "subclipped"):
        return clip.subclipped(start, end)
    return clip.subclip(start, end)


def clip_resized(clip: Any, *, width: int | None = None, height: int | None = None) -> Any:
    if hasattr(clip, "resized"):
        return clip.resized(width=width, height=height)
    return clip.resize(width=width, height=height)


def clip_cropped(clip: Any, *, x1: float, y1: float, x2: float, y2: float) -> Any:
    if hasattr(clip, "cropped"):
        return clip.cropped(x1=x1, y1=y1, x2=x2, y2=y2)
    return clip.crop(x1=x1, y1=y1, x2=x2, y2=y2)


def loop_clip_to_duration(clip: Any, duration: float) -> Any:
    if clip.duration and clip.duration >= duration:
        return clip_subclipped(clip, 0, duration)

    try:
        from moviepy.video.fx.Loop import Loop

        return clip.with_effects([Loop(duration=duration)])
    except Exception:
        pass

    try:
        from moviepy.video.fx.all import loop

        return clip.fx(loop, duration=duration)
    except Exception:
        pass

    source_duration = float(clip.duration or 1.0)
    if hasattr(clip, "time_transform"):
        return clip_with_duration(clip.time_transform(lambda t: t % source_duration, apply_to=["mask"]), duration)
    if hasattr(clip, "fl_time"):
        return clip_with_duration(clip.fl_time(lambda t: t % source_duration, apply_to=["mask"]), duration)

    repeats = max(1, math.ceil(duration / source_duration))
    joined = concatenate_videoclips([clip.copy() for _ in range(repeats)], method="compose")
    return clip_subclipped(joined, 0, duration)


def get_audio_duration(audio_path: Path) -> float:
    audio = AudioFileClip(str(audio_path))
    try:
        return float(audio.duration)
    finally:
        audio.close()


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def purge_old_temp_runs(temp_root: Path, max_age_hours: int) -> None:
    if not temp_root.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    for child in temp_root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def system_font_candidates() -> list[str]:
    candidates = [
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/noto/NotoSans-Black.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]

    try:
        matched = subprocess.run(
            ["fc-match", "-f", "%{file}\\n", "DejaVu Sans:style=Bold"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except Exception:
        matched = ""

    if matched:
        candidates.insert(0, matched)

    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


class GeminiScriptEngine:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.client: Any | None = None
        self.legacy_genai: Any | None = None
        self.legacy_model: Any | None = None
        self.model_name = config.gemini_model
        if google_genai is not None:
            self.client = google_genai.Client(api_key=config.gemini_api_key)
        else:
            import google.generativeai as legacy_genai

            self.legacy_genai = legacy_genai
            self.legacy_genai.configure(api_key=config.gemini_api_key)
            self.legacy_model = self.legacy_genai.GenerativeModel(self.model_name)

    def synthesize(self, topic: str) -> ScriptPayload:
        prompt = f"""
Return only valid JSON for a vertical short-form video narration.
Topic: {topic}
Rules:
- Rewrite the topic into a curiosity-driven, high-click YouTube title, 35 to 60 characters.
- Prefer concrete stakes, mystery, value, or surprise over summary-style wording.
- Include one relevant emoji at the end when it naturally fits the topic.
- Keep the title factual; do not add claims beyond the topic.
- Do not use hashtags, markdown, or trailing punctuation in the title.
- Keep the title and spoken narration in the same language as the topic unless the topic explicitly asks for another output language.
- Write punchy spoken narration that takes about 60 to 180 seconds to read aloud.
- Use plain narration only; no markdown, title, scene labels, hashtags, or stage directions.
- Make the first sentence high-retention but factual.
- Include 4 to 7 English visual keywords for Pexels video search.
- Include one concise English Pexels search query, 2 to 5 words, concrete and visual.
- Set language to the BCP-47 language or locale code for the title and script, for example en, es, es-MX, fr-FR.
JSON schema:
{{
  "language": "BCP-47 language or locale code",
  "title": "engaging rewritten YouTube title",
  "script": "spoken narration",
  "keywords": ["keyword one", "keyword two"],
  "pexels_query": "visual search phrase"
}}
""".strip()

        raw_text = self._generate_text(prompt)

        try:
            data = extract_json_object(raw_text)
        except Exception:
            data = self._fallback_payload(topic, raw_text)

        script = normalize_spaces(str(data.get("script", "")).strip())
        if not script:
            data = self._fallback_payload(topic, "")
            script = data["script"]

        language = normalize_language_tag(str(data.get("language", "")))

        title = normalize_spaces(str(data.get("title", "")).strip())
        if not title:
            title = normalize_spaces(topic)
        title = youtube_title(title)

        keywords = [normalize_spaces(str(item)) for item in data.get("keywords", []) if normalize_spaces(str(item))]
        if not keywords:
            keywords = extract_keywords(script)

        pexels_query = normalize_spaces(str(data.get("pexels_query", "")))
        if not pexels_query:
            pexels_query = " ".join(keywords[:4]) or normalize_spaces(topic)

        return ScriptPayload(title=title, script=script, keywords=keywords[:7], pexels_query=pexels_query, language=language)

    def _generate_text(self, prompt: str) -> str:
        try:
            response = self._generate_text_once(prompt)
        except Exception as exc:
            if self._is_missing_model_error(exc):
                response = self._retry_with_available_model(
                    prompt,
                    exc,
                    reason="is unavailable for this API key",
                    no_fallback_message=(
                        f"Gemini model {self.model_name!r} is unavailable for this API key. "
                        "Set GEMINI_MODEL to a model returned by the Gemini models.list endpoint."
                    ),
                )
            elif self._is_transient_gemini_error(exc):
                response = self._retry_with_available_model(
                    prompt,
                    exc,
                    excluded={self.model_name},
                    reason="is temporarily unavailable",
                    no_fallback_message=(
                        f"Gemini model {self.model_name!r} is temporarily unavailable. "
                        "Retry later or set GEMINI_MODEL to another available generateContent model."
                    ),
                )
            else:
                raise

        return getattr(response, "text", "") or str(response)

    def _retry_with_available_model(
        self,
        prompt: str,
        exc: Exception,
        *,
        reason: str,
        no_fallback_message: str,
        excluded: set[str] | None = None,
    ) -> Any:
        original_model = self.model_name
        fallback_model = self._find_available_generate_model(excluded=excluded)
        if not fallback_model or fallback_model == original_model:
            raise PipelineError(no_fallback_message) from exc

        print(f"[warn] Gemini model {original_model!r} {reason}; retrying with {fallback_model!r}.")
        self.model_name = fallback_model
        if self.legacy_genai is not None:
            self.legacy_model = self.legacy_genai.GenerativeModel(self.model_name)

        try:
            return self._generate_text_once(prompt)
        except Exception as retry_exc:
            if self._is_transient_gemini_error(retry_exc):
                raise PipelineError(
                    f"Gemini model {fallback_model!r} is temporarily unavailable. "
                    "Retry later or set GEMINI_MODEL to another available generateContent model."
                ) from retry_exc
            raise

    def _generate_text_once(self, prompt: str) -> Any:
        with AlarmTimeout(self.config.request_timeout_seconds, "Gemini generate_content"):
            if self.client is not None:
                return self.client.models.generate_content(model=self.model_name, contents=prompt)
            if self.legacy_model is not None:
                return self.legacy_model.generate_content(
                    prompt,
                    request_options={"timeout": self.config.request_timeout_seconds},
                )
        raise PipelineError("Install google-genai or google-generativeai to use Gemini script generation.")

    def _find_available_generate_model(self, excluded: set[str] | None = None) -> str | None:
        excluded = excluded or set()
        available = self._list_generate_models()
        preferred = [
            self.config.gemini_model,
            DEFAULT_CONFIG["gemini_model"],
            "gemini-3-flash",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
        ]
        for candidate in preferred:
            if candidate not in excluded and candidate in available:
                return candidate
        return next((model for model in available if model not in excluded), None)

    def _list_generate_models(self) -> list[str]:
        models: list[str] = []
        if self.client is not None:
            model_iterable = self.client.models.list()
        elif self.legacy_genai is not None:
            model_iterable = self.legacy_genai.list_models()
        else:
            return models

        for model in model_iterable:
            actions = getattr(model, "supported_actions", None) or getattr(model, "supported_generation_methods", None) or []
            if "generateContent" not in actions:
                continue
            raw_name = str(getattr(model, "name", ""))
            name = raw_name.removeprefix("models/")
            if name:
                models.append(name)
        return models

    def _is_missing_model_error(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        message = str(exc).lower()
        return status_code == 404 or "not found" in message or "is not supported for generatecontent" in message

    def _is_transient_gemini_error(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        message = str(exc).lower()
        return status_code in {500, 502, 503, 504} or "high demand" in message or "temporarily unavailable" in message

    def _fallback_payload(self, topic: str, raw_text: str) -> dict[str, Any]:
        candidate = normalize_spaces(raw_text)
        if not candidate or len(candidate.split()) < 16:
            candidate = (
                f"Here is the part of {topic} most people miss. "
                f"The real story is not the obvious detail, but the pattern behind it. "
                f"Once you notice that pattern, the whole topic becomes easier to understand, "
                f"faster to explain, and much harder to ignore."
            )
        script = normalize_spaces(candidate)
        keywords = extract_keywords(f"{topic} {script}")
        return {
            "title": youtube_title(topic),
            "script": script,
            "language": "",
            "keywords": keywords,
            "pexels_query": " ".join(keywords[:4]) or "cinematic motion",
        }


class EdgeTTSEngine:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    async def _save_async(self, text: str, audio_path: Path, metadata_path: Path) -> None:
        communicate = edge_tts.Communicate(
            text=text,
            voice=self.config.edge_voice,
            rate=self.config.edge_rate,
            volume=self.config.edge_volume,
            pitch=self.config.edge_pitch,
            boundary="WordBoundary",
            connect_timeout=self.config.request_timeout_seconds,
            receive_timeout=self.config.download_timeout_seconds,
        )
        await communicate.save(str(audio_path), str(metadata_path))

    def synthesize(self, text: str, work_dir: Path) -> Path:
        audio_path = work_dir / "voice.mp3"
        metadata_path = work_dir / "edge_word_boundaries.jsonl"
        asyncio.run(self._save_async(text, audio_path, metadata_path))
        if not audio_path.exists() or audio_path.stat().st_size == 0:
            raise PipelineError("Edge TTS returned an empty audio file.")
        return audio_path


class WhisperWordTracker:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self._model: Any | None = None
        self._cpu_fallback_model: Any | None = None

    @property
    def model(self) -> Any:
        if self._model is None:
            self._model = whisper.load_model(self.config.whisper_model, device=self.config.whisper_device)
        return self._model

    def transcribe_words(self, audio_path: Path, script_text: str) -> list[WordEvent]:
        try:
            result = self._transcribe_with_word_timestamps(self.model, audio_path)
        except Exception as exc:
            if not self._is_cuda_word_timestamp_error(exc):
                raise
            print("[warn] Whisper CUDA word timestamps failed; retrying on CPU.")
            result = self._transcribe_with_word_timestamps(self._cpu_fallback(), audio_path)

        events = self._word_events_from_transcript(result)

        if events:
            return events

        duration = get_audio_duration(audio_path)
        return self._estimate_word_events(script_text, duration)

    def _transcribe_with_word_timestamps(self, model: Any, audio_path: Path) -> dict[str, Any]:
        return model.transcribe(
            str(audio_path),
            word_timestamps=True,
            fp16=False,
            temperature=0,
            verbose=False,
        )

    def _cpu_fallback(self) -> Any:
        if self._cpu_fallback_model is None:
            self._cpu_fallback_model = whisper.load_model(self.config.whisper_model, device="cpu")
        return self._cpu_fallback_model

    def release(self) -> None:
        self._model = None
        self._cpu_fallback_model = None
        gc.collect()

        try:
            import torch
        except Exception:
            return

        cuda = getattr(torch, "cuda", None)
        is_available = getattr(cuda, "is_available", None)
        if cuda is None or not callable(is_available) or not is_available():
            return

        empty_cache = getattr(cuda, "empty_cache", None)
        if callable(empty_cache):
            empty_cache()

    def _is_cuda_word_timestamp_error(self, exc: Exception) -> bool:
        device = self.config.whisper_device.strip().lower()
        if device != "cuda" and not device.startswith("cuda:"):
            return False

        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            module = type(current).__module__.lower()
            name = type(current).__name__.lower()
            message = str(current).lower()
            if "triton" in module or "triton" in name or "dtw_cuda" in message:
                return True
            if "ast" in message and "has no attribute 'num'" in message:
                return True
            current = current.__cause__ or current.__context__

        return False

    def _word_events_from_transcript(self, result: dict[str, Any]) -> list[WordEvent]:
        events: list[WordEvent] = []
        for segment in result.get("segments", []):
            for word in segment.get("words", []) or []:
                text = normalize_spaces(str(word.get("word", "")))
                if not text:
                    continue
                start = max(0.0, float(word.get("start", 0.0)))
                end = max(start + 0.05, float(word.get("end", start + 0.12)))
                events.append(
                    WordEvent(
                        text=text,
                        start_ms=int(round(start * 1000)),
                        end_ms=int(round(end * 1000)),
                    )
                )
        return events

    def _estimate_word_events(self, script_text: str, duration: float) -> list[WordEvent]:
        words = [word for word in normalize_spaces(script_text).split() if word]
        if not words:
            return []

        usable_duration = max(0.5, duration - 0.2)
        total_weight = sum(max(2, len(word)) for word in words)
        cursor = 0.1
        events: list[WordEvent] = []

        for index, word in enumerate(words):
            weight = max(2, len(word))
            raw_duration = usable_duration * weight / total_weight
            word_duration = max(0.08, raw_duration)
            start = cursor
            end = min(duration, start + word_duration)
            if index == len(words) - 1:
                end = min(duration, max(end, duration - 0.05))
            events.append(WordEvent(text=word, start_ms=int(start * 1000), end_ms=int(end * 1000)))
            cursor = end + 0.025
            if cursor >= duration:
                break

        return events


class PexelsVisualClient:
    SEARCH_URL = "https://api.pexels.com/v1/videos/search"
    POPULAR_URL = "https://api.pexels.com/v1/videos/popular"

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"Authorization": config.pexels_api_key})

    def fetch_backgrounds(self, query: str, target_duration: float, work_dir: Path) -> list[Path]:
        try:
            candidates = self._search_candidates(query)
        except Exception as exc:
            print(f"[warn] Pexels search failed for query {query!r}: {exc}")
            candidates = []

        if not candidates:
            try:
                candidates = self._popular_candidates()
            except Exception as exc:
                print(f"[warn] Pexels popular fallback failed: {exc}")
                candidates = []

        downloaded: list[Path] = []
        accumulated = 0.0
        seen_urls: set[str] = set()

        for candidate in candidates:
            if candidate.url in seen_urls:
                continue
            seen_urls.add(candidate.url)

            target_path = work_dir / f"pexels_{candidate.video_id}_{len(downloaded)}.mp4"
            try:
                self._download(candidate.url, target_path)
            except Exception as exc:
                print(f"[warn] skipping failed Pexels download: {exc}")
                continue

            downloaded.append(target_path)
            accumulated += max(1.0, candidate.duration)
            if accumulated >= target_duration + 1.0:
                break

        return downloaded

    def _search_candidates(self, query: str) -> list[VideoCandidate]:
        params = {
            "query": query[:80],
            "orientation": "portrait",
            "size": "medium",
            "per_page": self.config.pexels_per_page,
        }
        payload = self._get_json(self.SEARCH_URL, params)
        return self._candidates_from_payload(payload)

    def _popular_candidates(self) -> list[VideoCandidate]:
        params = {
            "min_width": self.config.width,
            "min_height": self.config.height,
            "per_page": self.config.pexels_per_page,
        }
        payload = self._get_json(self.POPULAR_URL, params)
        return self._candidates_from_payload(payload)

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(url, params=params, timeout=self.config.request_timeout_seconds)
        response.raise_for_status()
        return response.json()

    def _candidates_from_payload(self, payload: dict[str, Any]) -> list[VideoCandidate]:
        videos = payload.get("videos") or []
        candidates: list[VideoCandidate] = []
        for video in videos:
            selected = self._select_best_file(video)
            if not selected:
                continue
            candidates.append(
                VideoCandidate(
                    url=selected["link"],
                    video_id=video.get("id", selected.get("id", len(candidates))),
                    duration=float(video.get("duration") or 1.0),
                    width=int(selected.get("width") or 0),
                    height=int(selected.get("height") or 0),
                    quality=str(selected.get("quality") or ""),
                )
            )
        return candidates

    def _select_best_file(self, video: dict[str, Any]) -> dict[str, Any] | None:
        files = [
            item
            for item in video.get("video_files", [])
            if item.get("link")
            and item.get("file_type") == "video/mp4"
            and item.get("width")
            and item.get("height")
            and item.get("quality") != "hls"
        ]
        if not files:
            return None

        target_aspect = self.config.width / self.config.height

        def score(item: dict[str, Any]) -> tuple[float, float, float, float]:
            width = float(item.get("width") or 1)
            height = float(item.get("height") or 1)
            aspect = width / height
            portrait = 1.0 if height >= width else 0.0
            quality = 1.0 if item.get("quality") == "hd" else 0.0
            aspect_score = -abs(aspect - target_aspect)
            size_score = min(width * height, self.config.width * self.config.height * 2)
            return portrait, quality, aspect_score, size_score

        return max(files, key=score)

    def _download(self, url: str, target_path: Path) -> None:
        with self.session.get(
            url,
            stream=True,
            timeout=(self.config.request_timeout_seconds, self.config.download_timeout_seconds),
        ) as response:
            response.raise_for_status()
            content_length = int(response.headers.get("content-length") or 0)
            if content_length > self.config.max_download_bytes:
                raise PipelineError(f"video exceeds byte cap: {content_length}")

            bytes_seen = 0
            with target_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    bytes_seen += len(chunk)
                    if bytes_seen > self.config.max_download_bytes:
                        raise PipelineError("video exceeded byte cap while streaming")
                    handle.write(chunk)

        if target_path.stat().st_size == 0:
            raise PipelineError("downloaded empty Pexels video")


class MoviePyShortCompiler:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def compile(
        self,
        audio_path: Path,
        word_events: list[WordEvent],
        background_paths: list[Path],
        output_path: Path,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        audio_duration = get_audio_duration(audio_path)
        caption_duration = max((event.end_ms for event in word_events), default=0) / 1000.0
        final_duration = max(audio_duration, caption_duration, 1.0)

        opened_clips: list[Any] = []
        audio_clips: list[Any] = []
        caption_clips: list[Any] = []
        final_clip: Any | None = None

        try:
            background = self._build_background(background_paths, final_duration, opened_clips)
            layers: list[Any] = [background]
            caption_clips = [self._word_layer(event) for event in word_events if event.end_ms > event.start_ms]
            layers.extend(caption_clips)

            audio_clip = self._build_audio(audio_path, final_duration, audio_clips)
            final_clip = CompositeVideoClip(layers, size=(self.config.width, self.config.height))
            final_clip = clip_with_duration(final_clip, final_duration)
            final_clip = clip_with_audio(final_clip, audio_clip)
            final_clip = clip_with_fps(final_clip, self.config.fps)

            final_clip.write_videofile(
                str(output_path),
                fps=self.config.fps,
                codec="libx264",
                audio_codec="aac",
                preset="medium",
                threads=self.config.threads,
                bitrate="6500k",
                temp_audiofile=str(output_path.with_suffix(".temp-audio.m4a")),
                remove_temp=True,
                logger="bar",
            )
            return output_path
        finally:
            close_all([final_clip])
            close_all(audio_clips)
            close_all(caption_clips)
            close_all(opened_clips)

    def _build_audio(self, narration_path: Path, duration: float, opened_clips: list[Any]) -> Any:
        narration_clip = AudioFileClip(str(narration_path))
        opened_clips.append(narration_clip)

        if self.config.background_music_path is None:
            return narration_clip

        if not self.config.background_music_path.exists():
            raise PipelineError(f"Background music file not found: {self.config.background_music_path}")

        music_clip = AudioFileClip(str(self.config.background_music_path))
        opened_clips.append(music_clip)
        music_clip = loop_clip_to_duration(music_clip, duration)
        music_clip = clip_with_volume(music_clip, self.config.background_music_volume)

        mixed_audio = CompositeAudioClip([narration_clip, music_clip])
        mixed_audio = clip_with_duration(mixed_audio, duration)
        opened_clips.append(mixed_audio)
        return mixed_audio

    def _build_background(self, paths: list[Path], duration: float, opened_clips: list[Any]) -> Any:
        prepared: list[Any] = []
        for path in paths:
            try:
                clip = VideoFileClip(str(path), audio=False)
            except TypeError:
                clip = clip_without_audio(VideoFileClip(str(path)))
            except Exception as exc:
                print(f"[warn] skipping unreadable video {path}: {exc}")
                continue

            opened_clips.append(clip)
            fitted = self._fit_vertical(clip_without_audio(clip))
            prepared.append(fitted)

        if not prepared:
            fallback = ColorClip(size=(self.config.width, self.config.height), color=(18, 20, 24), duration=duration)
            return clip_with_duration(fallback, duration)

        if len(prepared) == 1:
            return loop_clip_to_duration(prepared[0], duration)

        joined = concatenate_videoclips(prepared, method="compose")
        opened_clips.append(joined)
        return loop_clip_to_duration(joined, duration)

    def _fit_vertical(self, clip: Any) -> Any:
        target_w = self.config.width
        target_h = self.config.height
        clip_w = float(getattr(clip, "w", target_w) or target_w)
        clip_h = float(getattr(clip, "h", target_h) or target_h)
        target_aspect = target_w / target_h
        source_aspect = clip_w / clip_h

        if source_aspect > target_aspect:
            resized = clip_resized(clip, height=target_h)
            new_w = float(getattr(resized, "w", target_w))
            x1 = max(0.0, (new_w - target_w) / 2.0)
            return clip_cropped(resized, x1=x1, y1=0, x2=x1 + target_w, y2=target_h)

        resized = clip_resized(clip, width=target_w)
        new_h = float(getattr(resized, "h", target_h))
        y1 = max(0.0, (new_h - target_h) / 2.0)
        return clip_cropped(resized, x1=0, y1=y1, x2=target_w, y2=y1 + target_h)

    def _word_layer(self, event: WordEvent) -> Any:
        image = self._caption_image(event.text)
        try:
            layer = ImageClip(np.array(image), transparent=True)
        except TypeError:
            layer = ImageClip(np.array(image))

        start = event.start_ms / 1000.0
        duration = max(0.05, (event.end_ms - event.start_ms) / 1000.0)
        caption_center_y = int(self.config.height * self.config.caption_y_ratio)
        y = max(0, caption_center_y - image.height // 2)
        layer = clip_with_duration(layer, duration)
        layer = clip_with_start(layer, start)
        return clip_with_position(layer, ("center", y))

    def _caption_image(self, raw_word: str) -> Image.Image:
        word = normalize_spaces(raw_word).upper()
        if len(word) > 28:
            word = word[:25] + "..."

        canvas_w = int(self.config.width * 0.92)
        canvas_h = max(300, int(self.config.caption_font_size * 2.25))
        max_w = int(canvas_w * 0.94)
        max_h = int(canvas_h * 0.72)
        image = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        font_size = self.config.caption_font_size
        font = self._load_font(font_size)
        while font_size > 34:
            bbox = draw.textbbox((0, 0), word, font=font, stroke_width=self.config.caption_stroke_width)
            if bbox[2] - bbox[0] <= max_w and bbox[3] - bbox[1] <= max_h:
                break
            font_size -= 4
            font = self._load_font(font_size)

        bbox = draw.textbbox((0, 0), word, font=font, stroke_width=self.config.caption_stroke_width)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = (canvas_w - text_w) / 2 - bbox[0]
        y = (canvas_h - text_h) / 2 - bbox[1]
        draw.text(
            (x, y),
            word,
            font=font,
            fill=self.config.caption_color,
            stroke_width=self.config.caption_stroke_width,
            stroke_fill=self.config.caption_stroke_color,
        )
        return image

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [self.config.caption_font_path, *system_font_candidates()]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return ImageFont.truetype(candidate, size=size)
        return ImageFont.load_default()


class ShortsPipeline:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.script_engine = GeminiScriptEngine(config)
        self.tts_engine = EdgeTTSEngine(config)
        self.word_tracker = WhisperWordTracker(config)
        self.visual_client = PexelsVisualClient(config)
        self.compiler = MoviePyShortCompiler(config)

    def _tts_engine_for_payload(self, payload: ScriptPayload) -> EdgeTTSEngine:
        if self.config.tts_voice_overridden:
            if hasattr(self, "tts_engine"):
                return self.tts_engine
            return EdgeTTSEngine(self.config)

        selected_voice = edge_tts_voice_for_language(payload.language, self.config.edge_voice)
        if selected_voice == self.config.edge_voice:
            if hasattr(self, "tts_engine"):
                return self.tts_engine
            return EdgeTTSEngine(self.config)

        return EdgeTTSEngine(replace(self.config, edge_voice=selected_voice))

    def run(self, topic: str, output_path: Path | None = None) -> Path:
        purge_old_temp_runs(self.config.temp_root, self.config.cleanup_max_age_hours)
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.config.temp_root.mkdir(parents=True, exist_ok=True)

        run_slug = f"{int(time.time())}-{slugify(topic)}"
        run_dir = self.config.temp_root / run_slug
        run_dir.mkdir(parents=True, exist_ok=True)

        try:
            payload = self.script_engine.synthesize(topic)
            save_json(run_dir / "script_payload.json", asdict(payload))

            audio_path = self._tts_engine_for_payload(payload).synthesize(payload.script, run_dir)
            word_events = self.word_tracker.transcribe_words(audio_path, payload.script)
            save_json(run_dir / "whisper_word_events.json", [asdict(event) for event in word_events])
            self.word_tracker.release()

            duration = max(get_audio_duration(audio_path), max((event.end_ms for event in word_events), default=0) / 1000.0)
            backgrounds = self.visual_client.fetch_backgrounds(payload.pexels_query, duration, run_dir)

            if output_path is None:
                output_path = self.config.output_dir / f"{run_slug}.mp4"

            rendered = self.compiler.compile(audio_path, word_events, backgrounds, output_path)
            save_json(
                rendered.with_suffix(".metadata.json"),
                {
                    "topic": topic,
                    "script_payload": asdict(payload),
                    "word_events": [asdict(event) for event in word_events],
                    "background_files": [str(path) for path in backgrounds],
                    "output": str(rendered),
                },
            )
            return rendered
        finally:
            if not self.config.keep_temp:
                shutil.rmtree(run_dir, ignore_errors=True)


class YouTubeShortsUploader:
    SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def upload(self, video_path: Path, *, title: str, description: str) -> str:
        client = self._youtube_client()
        media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
        request = client.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": youtube_title(title),
                    "description": description,
                    "tags": ["Shorts"],
                    "categoryId": self.config.youtube_category_id,
                },
                "status": {
                    "privacyStatus": self.config.youtube_privacy_status,
                    "selfDeclaredMadeForKids": False,
                },
            },
            media_body=media,
        )
        response = request.execute()
        video_id = normalize_spaces(str(response.get("id", "")))
        if not video_id:
            raise PipelineError("YouTube upload completed without returning a video id.")
        return video_id

    def _youtube_client(self) -> Any:
        self._ensure_youtube_dependencies()
        if not self.config.youtube_client_secrets_file.exists():
            raise PipelineError(
                f"YouTube client secrets file not found: {self.config.youtube_client_secrets_file}"
            )

        credentials = None
        if self.config.youtube_token_file.exists():
            if GoogleCredentials is None:
                raise PipelineError(
                    "Install YouTube upload dependencies first: "
                    "python3 -m pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
                )
            credentials = GoogleCredentials.from_authorized_user_file(
                str(self.config.youtube_token_file),
                self.SCOPES,
            )

        if credentials and credentials.expired and credentials.refresh_token:
            if GoogleAuthRequest is None:
                raise PipelineError(
                    "Install YouTube upload dependencies first: "
                    "python3 -m pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
                )
            credentials.refresh(GoogleAuthRequest())

        if not credentials or not credentials.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.config.youtube_client_secrets_file),
                self.SCOPES,
            )
            credentials = flow.run_local_server(port=0)

        self.config.youtube_token_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.youtube_token_file.write_text(credentials.to_json(), encoding="utf-8")
        return google_api_build("youtube", "v3", credentials=credentials)

    def _ensure_youtube_dependencies(self) -> None:
        dependencies = {
            "google-auth-oauthlib": InstalledAppFlow,
            "google-api-python-client": google_api_build,
            "googleapiclient.http.MediaFileUpload": MediaFileUpload,
        }
        missing = [name for name, dependency in dependencies.items() if dependency is None]
        if missing:
            raise PipelineError(
                "Install YouTube upload dependencies first: "
                "python3 -m pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
            )


def youtube_title(value: str) -> str:
    title = normalize_spaces(value) or "Generated Short"
    max_length = 100
    if len(title) <= max_length:
        return title
    return title[:max_length].rstrip()


def youtube_description(topic: str) -> str:
    topic = normalize_spaces(topic)
    if not topic:
        return "#Shorts"
    return f"{topic}\n\n#Shorts"


def youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def save_youtube_upload_metadata(video_path: Path, video_id: str) -> None:
    metadata_path = video_path.with_suffix(".metadata.json")
    metadata: dict[str, Any] = {}
    if metadata_path.exists():
        try:
            raw_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(raw_metadata, dict):
                metadata = raw_metadata
        except json.JSONDecodeError:
            metadata = {}

    metadata["youtube"] = {
        "video_id": video_id,
        "url": youtube_url(video_id),
    }
    save_json(metadata_path, metadata)


def rendered_youtube_title(video_path: Path, fallback_topic: str) -> str:
    metadata_path = video_path.with_suffix(".metadata.json")
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = {}
        if isinstance(metadata, dict):
            script_payload = metadata.get("script_payload")
            if isinstance(script_payload, dict):
                title = normalize_spaces(str(script_payload.get("title", "")))
                if title:
                    return youtube_title(title)
    return youtube_title(fallback_topic)


def upload_rendered_short(video_path: Path, topic: str, config: PipelineConfig) -> str:
    video_id = YouTubeShortsUploader(config).upload(
        video_path,
        title=rendered_youtube_title(video_path, topic),
        description=youtube_description(topic),
    )
    save_youtube_upload_metadata(video_path, video_id)
    return video_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a vertical short-form MP4 with AI script, TTS, captions, and Pexels backgrounds.")
    parser.add_argument("--topic", default=None, help="Topic or prompt for the short. Prompts when omitted.")
    parser.add_argument("--output", default=None, help="Optional output MP4 path.")
    parser.add_argument("--upload", action="store_true", help="Upload the rendered MP4 to YouTube after generation.")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary media assets for debugging.")
    parser.add_argument("--voice", default=None, help="Override Edge TTS voice, for example en-US-GuyNeural.")
    parser.add_argument("--tts-rate", default=None, help="Override Edge TTS speaking rate, for example +7%% or -4%%.")
    parser.add_argument("--tts-pitch", default=None, help="Override Edge TTS pitch, for example -3Hz or +2Hz.")
    parser.add_argument("--tts-volume", default=None, help="Override Edge TTS volume, for example +0%% or -5%%.")
    parser.add_argument("--whisper-model", default=None, help="Override Whisper model, for example tiny, base, small.")
    parser.add_argument("--whisper-device", default=None, help="Override Whisper device. Defaults to cpu to avoid CUDA/Triton timestamp crashes on Python 3.14.")
    parser.add_argument("--music", default=None, help="Optional local background music file to mix under narration.")
    parser.add_argument("--music-volume", type=float, default=None, help="Background music volume multiplier, for example 0.12.")
    return parser.parse_args()


def resolve_run_inputs(args: argparse.Namespace, config: PipelineConfig) -> tuple[str, Path | None]:
    topic = normalize_spaces(args.topic or "")
    output_value = args.output
    output_from_prompt = False

    if not topic:
        while not topic:
            topic = normalize_spaces(input("Topic: "))
            if not topic:
                print("Topic is required.")
        if output_value is None:
            output_value = input("Output file name (blank for auto): ")
            output_from_prompt = True

    output_value = normalize_spaces(output_value or "")
    if not output_value:
        return topic, None

    output_path = Path(output_value)
    if not output_from_prompt:
        return topic, output_path

    if output_path.suffix.lower() != ".mp4":
        output_path = output_path.with_name(f"{output_path.name}.mp4")
    if not output_path.is_absolute() and output_path.parent == Path("."):
        output_path = config.output_dir / output_path
    return topic, output_path


def main() -> None:
    args = parse_args()
    config = config_from_env()
    if (
        args.keep_temp
        or args.voice
        or args.tts_rate
        or args.tts_pitch
        or args.tts_volume
        or args.whisper_model
        or args.whisper_device
        or args.music
        or args.music_volume is not None
        or args.upload
    ):
        config = PipelineConfig(
            **{
                **asdict(config),
                "keep_temp": bool(args.keep_temp),
                "edge_voice": args.voice or config.edge_voice,
                "tts_voice_overridden": bool(args.voice),
                "edge_rate": args.tts_rate or config.edge_rate,
                "edge_pitch": args.tts_pitch or config.edge_pitch,
                "edge_volume": args.tts_volume or config.edge_volume,
                "whisper_model": args.whisper_model or config.whisper_model,
                "whisper_device": args.whisper_device or config.whisper_device,
                "background_music_path": Path(args.music) if args.music else config.background_music_path,
                "background_music_volume": args.music_volume
                if args.music_volume is not None
                else config.background_music_volume,
                "youtube_upload_enabled": bool(args.upload) or config.youtube_upload_enabled,
            }
        )

    topic, output_path = resolve_run_inputs(args, config)
    rendered = ShortsPipeline(config).run(topic, output_path)
    print(str(rendered))
    if config.youtube_upload_enabled:
        video_id = upload_rendered_short(rendered, topic, config)
        print(f"YouTube: {youtube_url(video_id)}")


def cli() -> None:
    try:
        main()
    except PipelineError as exc:
        raise SystemExit(f"error: {exc}") from None
    except KeyboardInterrupt:
        raise SystemExit("interrupted") from None


if __name__ == "__main__":
    cli()
