import importlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def import_shorts_core_with_fakes(gemini_text: str | None = None):
    fake_edge_tts = types.ModuleType("edge_tts")
    fake_edge_tts.Communicate = object

    class FakeModels:
        def __init__(self):
            self.calls = []

        def generate_content(self, *, model, contents):
            self.calls.append({"model": model, "contents": contents})
            return types.SimpleNamespace(
                text=gemini_text
                or '{"script":"Test script for a short.","keywords":["test"],"pexels_query":"test video"}'
            )

    class FakeClient:
        instances = []

        def __init__(self, *, api_key=None):
            self.api_key = api_key
            self.models = FakeModels()
            self.__class__.instances.append(self)

    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = FakeClient
    fake_google.genai = fake_genai

    fake_requests = types.ModuleType("requests")
    fake_requests.Session = object

    fake_whisper = types.ModuleType("whisper")
    fake_whisper.load_model_calls = []

    def fake_load_model(*args, **kwargs):
        fake_whisper.load_model_calls.append({"args": args, "kwargs": kwargs})
        return types.SimpleNamespace(transcribe=lambda *_, **__: {"segments": []})

    fake_whisper.load_model = fake_load_model

    fake_numpy = types.ModuleType("numpy")
    fake_numpy.array = lambda value: value

    fake_pil = types.ModuleType("PIL")
    fake_pil_image = types.ModuleType("PIL.Image")
    fake_pil_draw = types.ModuleType("PIL.ImageDraw")
    fake_pil_font = types.ModuleType("PIL.ImageFont")
    fake_pil.Image = fake_pil_image
    fake_pil.ImageDraw = fake_pil_draw
    fake_pil.ImageFont = fake_pil_font

    fake_moviepy = types.ModuleType("moviepy")
    for name in (
        "AudioFileClip",
        "ColorClip",
        "CompositeAudioClip",
        "CompositeVideoClip",
        "ImageClip",
        "VideoFileClip",
        "concatenate_videoclips",
    ):
        setattr(fake_moviepy, name, object)

    modules = {
        "edge_tts": fake_edge_tts,
        "google": fake_google,
        "google.genai": fake_genai,
        "requests": fake_requests,
        "whisper": fake_whisper,
        "numpy": fake_numpy,
        "PIL": fake_pil,
        "PIL.Image": fake_pil_image,
        "PIL.ImageDraw": fake_pil_draw,
        "PIL.ImageFont": fake_pil_font,
        "moviepy": fake_moviepy,
    }

    with patch.dict(sys.modules, modules):
        sys.modules.pop("shorts_core", None)
        module = importlib.import_module("shorts_core")
        module._fake_genai_client = FakeClient
        module._fake_whisper = fake_whisper
        return module


class EnvLoadingTests(unittest.TestCase):
    def test_config_from_env_loads_dotenv_in_current_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text(
                "\n".join(
                    [
                        "GEMINI_API_KEY=gemini-from-dotenv",
                        "PEXELS_API_KEY=pexels-from-dotenv",
                        "EDGE_TTS_VOICE=en-US-AriaNeural",
                        "EDGE_TTS_RATE=+7%",
                        "EDGE_TTS_PITCH=-3Hz",
                        "EDGE_TTS_VOLUME=+0%",
                        "GEMINI_TIMEOUT_SECONDS=75",
                        "WHISPER_MODEL=tiny",
                        "WHISPER_DEVICE=cpu",
                        "CAPTION_FONT_SIZE=220",
                        "BACKGROUND_MUSIC_PATH=/tmp/music.mp3",
                        "BACKGROUND_MUSIC_VOLUME=0.18",
                        "YOUTUBE_UPLOAD_ENABLED=true",
                        "YOUTUBE_CLIENT_SECRETS_FILE=/tmp/client_secrets.json",
                        "YOUTUBE_TOKEN_FILE=/tmp/youtube_token.json",
                        "YOUTUBE_PRIVACY_STATUS=unlisted",
                        "YOUTUBE_CATEGORY_ID=27",
                    ]
                ),
                encoding="utf-8",
            )
            old_cwd = os.getcwd()

            with patch.dict(os.environ, {}, clear=True):
                os.chdir(tmp)
                try:
                    shorts_core = import_shorts_core_with_fakes()
                    config = shorts_core.config_from_env()
                finally:
                    os.chdir(old_cwd)

            self.assertEqual(config.gemini_api_key, "gemini-from-dotenv")
            self.assertEqual(config.pexels_api_key, "pexels-from-dotenv")
            self.assertEqual(config.edge_voice, "en-US-AriaNeural")
            self.assertEqual(config.edge_rate, "+7%")
            self.assertEqual(config.edge_pitch, "-3Hz")
            self.assertEqual(config.edge_volume, "+0%")
            self.assertEqual(config.gemini_timeout_seconds, 75)
            self.assertEqual(config.whisper_model, "tiny")
            self.assertEqual(config.whisper_device, "cpu")
            self.assertEqual(config.caption_font_size, 220)
            self.assertEqual(config.background_music_path, Path("/tmp/music.mp3"))
            self.assertEqual(config.background_music_volume, 0.18)
            self.assertTrue(config.youtube_upload_enabled)
            self.assertEqual(config.youtube_client_secrets_file, Path("/tmp/client_secrets.json"))
            self.assertEqual(config.youtube_token_file, Path("/tmp/youtube_token.json"))
            self.assertEqual(config.youtube_privacy_status, "unlisted")
            self.assertEqual(config.youtube_category_id, "27")

    def test_gemini_engine_uses_current_google_genai_client(self):
        shorts_core = import_shorts_core_with_fakes()
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
        )

        payload = shorts_core.GeminiScriptEngine(config).synthesize("test topic")

        client = shorts_core._fake_genai_client.instances[-1]
        self.assertEqual(client.api_key, "gemini-key")
        self.assertEqual(client.models.calls[-1]["model"], shorts_core.DEFAULT_CONFIG["gemini_model"])
        self.assertNotEqual(shorts_core.DEFAULT_CONFIG["gemini_model"], "gemini-1.5-flash")
        self.assertEqual(payload.pexels_query, "test video")

    def test_gemini_engine_targets_60_to_180_seconds_without_trimming_script(self):
        long_script = " ".join(f"word{index}" for index in range(150))
        shorts_core = import_shorts_core_with_fakes(
            json.dumps(
                {
                    "script": long_script,
                    "keywords": ["test"],
                    "pexels_query": "test video",
                }
            )
        )
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
        )

        payload = shorts_core.GeminiScriptEngine(config).synthesize("test topic")

        client = shorts_core._fake_genai_client.instances[-1]
        self.assertIn("60 to 180 seconds", client.models.calls[-1]["contents"])
        self.assertEqual(payload.script, long_script)

    def test_gemini_engine_preserves_topic_language_for_narration(self):
        shorts_core = import_shorts_core_with_fakes()
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
        )

        shorts_core.GeminiScriptEngine(config).synthesize(
            "En 1930, un pueblo entero desaparecio de la noche a la manana."
        )

        prompt = shorts_core._fake_genai_client.instances[-1].models.calls[-1]["contents"]
        self.assertIn("same language as the topic", prompt)
        self.assertIn('"language"', prompt)
        self.assertIn("BCP-47", prompt)
        self.assertIn("Pexels", prompt)
        self.assertIn("English", prompt)

    def test_gemini_engine_returns_normalized_language_code(self):
        shorts_core = import_shorts_core_with_fakes(
            json.dumps(
                {
                    "language": "es_mx",
                    "title": "Titulo",
                    "script": "Narracion en espanol.",
                    "keywords": ["test"],
                    "pexels_query": "test video",
                }
            )
        )
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
        )

        payload = shorts_core.GeminiScriptEngine(config).synthesize("tema")

        self.assertEqual(payload.language, "es-MX")

    def test_gemini_engine_uses_longer_generation_timeout_by_default(self):
        shorts_core = import_shorts_core_with_fakes()

        class FakeAlarmTimeout:
            calls = []

            def __init__(self, seconds, label):
                self.__class__.calls.append({"seconds": seconds, "label": label})

            def __enter__(self):
                return None

            def __exit__(self, exc_type, exc, traceback):
                return None

        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
        )

        with patch.object(shorts_core, "AlarmTimeout", FakeAlarmTimeout):
            shorts_core.GeminiScriptEngine(config).synthesize("test topic")

        self.assertEqual(FakeAlarmTimeout.calls[-1], {"seconds": 60, "label": "Gemini generate_content"})

    def test_gemini_engine_retries_high_demand_errors_with_alternate_model(self):
        shorts_core = import_shorts_core_with_fakes()

        class HighDemandError(Exception):
            status_code = 503

        class BusyThenAvailableModels:
            def __init__(self):
                self.calls = []

            def generate_content(self, *, model, contents):
                self.calls.append({"model": model, "contents": contents})
                if model == "gemini-2.5-flash":
                    raise HighDemandError(
                        "503 UNAVAILABLE. This model is currently experiencing high demand."
                    )
                return types.SimpleNamespace(
                    text=json.dumps(
                        {
                            "script": "Fallback model script for a short.",
                            "keywords": ["fallback"],
                            "pexels_query": "fallback video",
                        }
                    )
                )

            def list(self):
                return [
                    types.SimpleNamespace(
                        name="models/gemini-2.5-flash",
                        supported_actions=["generateContent"],
                    ),
                    types.SimpleNamespace(
                        name="models/gemini-2.5-flash-lite",
                        supported_actions=["generateContent"],
                    ),
                ]

        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
        )
        engine = shorts_core.GeminiScriptEngine(config)
        engine.client.models = BusyThenAvailableModels()

        payload = engine.synthesize("test topic")

        self.assertEqual(
            [call["model"] for call in engine.client.models.calls],
            ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )
        self.assertEqual(payload.pexels_query, "fallback video")

    def test_gemini_engine_returns_engaging_title_from_payload(self):
        title = "Why Your 8-Hour Workday Is Failing"
        shorts_core = import_shorts_core_with_fakes(
            json.dumps(
                {
                    "title": title,
                    "script": "Test script for a short.",
                    "keywords": ["workday", "productivity"],
                    "pexels_query": "office workday",
                }
            )
        )
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
        )

        payload = shorts_core.GeminiScriptEngine(config).synthesize(
            "The '8-hour workday' is actually a scam designed for the 1800s, "
            "and it is destroying your productivity."
        )

        client = shorts_core._fake_genai_client.instances[-1]
        prompt = client.models.calls[-1]["contents"]
        self.assertIn('"title"', prompt)
        self.assertIn("curiosity-driven", prompt)
        self.assertIn("one relevant emoji", prompt)
        self.assertEqual(payload.title, title)

    def test_youtube_title_hard_caps_without_ellipsis(self):
        shorts_core = import_shorts_core_with_fakes()
        topic = "A" * 120

        title = shorts_core.youtube_title(topic)

        self.assertEqual(len(title), 100)
        self.assertEqual(title, topic[:100])
        self.assertFalse(title.endswith("..."))

    def test_whisper_tracker_loads_model_on_cpu_by_default(self):
        shorts_core = import_shorts_core_with_fakes()
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
            whisper_model="tiny",
        )

        _ = shorts_core.WhisperWordTracker(config).model

        self.assertEqual(shorts_core._fake_whisper.load_model_calls[-1]["args"], ("tiny",))
        self.assertEqual(shorts_core._fake_whisper.load_model_calls[-1]["kwargs"], {"device": "cpu"})

    def test_whisper_tracker_retries_cuda_timestamp_error_on_cpu(self):
        shorts_core = import_shorts_core_with_fakes()
        calls = []

        class FakeCudaModel:
            def transcribe(self, *args, **kwargs):
                raise RuntimeError(
                    "triton.compiler.errors.CompilationError: "
                    "AttributeError(\"module 'ast' has no attribute 'Num'\")"
                )

        class FakeCpuModel:
            def transcribe(self, *args, **kwargs):
                return {
                    "segments": [
                        {
                            "words": [
                                {"word": " retry ", "start": 0.1, "end": 0.35},
                                {"word": " worked ", "start": 0.36, "end": 0.6},
                            ]
                        }
                    ]
                }

        def fake_load_model(model, *, device):
            calls.append((model, device))
            return FakeCudaModel() if device == "cuda" else FakeCpuModel()

        shorts_core.whisper.load_model = fake_load_model
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
            whisper_model="tiny",
            whisper_device="cuda",
        )

        events = shorts_core.WhisperWordTracker(config).transcribe_words(Path("voice.mp3"), "retry worked")

        self.assertEqual(calls, [("tiny", "cuda"), ("tiny", "cpu")])
        self.assertEqual([event.text for event in events], ["retry", "worked"])

    def test_whisper_tracker_does_not_retry_unrelated_cuda_errors(self):
        shorts_core = import_shorts_core_with_fakes()
        calls = []

        class FakeCudaModel:
            def transcribe(self, *args, **kwargs):
                raise RuntimeError("unexpected decoder failure")

        def fake_load_model(model, *, device):
            calls.append((model, device))
            return FakeCudaModel()

        shorts_core.whisper.load_model = fake_load_model
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
            whisper_model="tiny",
            whisper_device="cuda",
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected decoder failure"):
            shorts_core.WhisperWordTracker(config).transcribe_words(Path("voice.mp3"), "retry worked")

        self.assertEqual(calls, [("tiny", "cuda")])

    def test_pipeline_releases_word_tracker_before_rendering(self):
        shorts_core = import_shorts_core_with_fakes()
        events = []

        class FakeScriptEngine:
            def synthesize(self, topic):
                events.append("script")
                return shorts_core.ScriptPayload(
                    title="Test title",
                    script="test narration",
                    language="en",
                    keywords=["test"],
                    pexels_query="test video",
                )

        class FakeTTSEngine:
            def synthesize(self, text, work_dir):
                events.append("tts")
                audio_path = work_dir / "voice.mp3"
                audio_path.write_bytes(b"fake audio")
                return audio_path

        class FakeWordTracker:
            def transcribe_words(self, audio_path, script_text):
                events.append("transcribe")
                return [shorts_core.WordEvent(text="test", start_ms=0, end_ms=500)]

            def release(self):
                events.append("release")

        class FakeVisualClient:
            def fetch_backgrounds(self, query, duration, work_dir):
                events.append("backgrounds")
                return []

        class FakeCompiler:
            def compile(self, audio_path, word_events, backgrounds, output_path):
                events.append("compile")
                output_path.write_bytes(b"fake mp4")
                return output_path

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = shorts_core.PipelineConfig(
                gemini_api_key="gemini-key",
                pexels_api_key="pexels-key",
                output_dir=tmp_path / "outputs",
                temp_root=tmp_path / "temp",
            )
            pipeline = shorts_core.ShortsPipeline.__new__(shorts_core.ShortsPipeline)
            pipeline.config = config
            pipeline.script_engine = FakeScriptEngine()
            pipeline.tts_engine = FakeTTSEngine()
            pipeline.word_tracker = FakeWordTracker()
            pipeline.visual_client = FakeVisualClient()
            pipeline.compiler = FakeCompiler()

            with patch.object(shorts_core, "get_audio_duration", return_value=1.0):
                pipeline.run("memory test")

        self.assertEqual(events, ["script", "tts", "transcribe", "release", "backgrounds", "compile"])

    def test_pipeline_auto_selects_tts_voice_from_generated_language(self):
        shorts_core = import_shorts_core_with_fakes()
        voices = []

        class FakeScriptEngine:
            def synthesize(self, topic):
                return shorts_core.ScriptPayload(
                    title="Titulo",
                    script="Narracion en espanol.",
                    language="es",
                    keywords=["test"],
                    pexels_query="test video",
                )

        class FakeTTSEngine:
            def __init__(self, config):
                voices.append(config.edge_voice)

            def synthesize(self, text, work_dir):
                audio_path = work_dir / "voice.mp3"
                audio_path.write_bytes(b"fake audio")
                return audio_path

        class FakeWordTracker:
            def transcribe_words(self, audio_path, script_text):
                return [shorts_core.WordEvent(text="test", start_ms=0, end_ms=500)]

            def release(self):
                pass

        class FakeVisualClient:
            def fetch_backgrounds(self, query, duration, work_dir):
                return []

        class FakeCompiler:
            def compile(self, audio_path, word_events, backgrounds, output_path):
                output_path.write_bytes(b"fake mp4")
                return output_path

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = shorts_core.PipelineConfig(
                gemini_api_key="gemini-key",
                pexels_api_key="pexels-key",
                output_dir=tmp_path / "outputs",
                temp_root=tmp_path / "temp",
            )
            pipeline = shorts_core.ShortsPipeline.__new__(shorts_core.ShortsPipeline)
            pipeline.config = config
            pipeline.script_engine = FakeScriptEngine()
            pipeline.word_tracker = FakeWordTracker()
            pipeline.visual_client = FakeVisualClient()
            pipeline.compiler = FakeCompiler()

            with patch.object(shorts_core, "EdgeTTSEngine", FakeTTSEngine):
                with patch.object(shorts_core, "get_audio_duration", return_value=1.0):
                    pipeline.run("tema")

        self.assertEqual(voices, ["es-ES-ElviraNeural"])

    def test_pipeline_keeps_explicit_tts_voice_override(self):
        shorts_core = import_shorts_core_with_fakes()
        voices = []

        class FakeScriptEngine:
            def synthesize(self, topic):
                return shorts_core.ScriptPayload(
                    title="Titulo",
                    script="Narracion en espanol.",
                    language="es",
                    keywords=["test"],
                    pexels_query="test video",
                )

        class FakeTTSEngine:
            def __init__(self, config):
                voices.append(config.edge_voice)

            def synthesize(self, text, work_dir):
                audio_path = work_dir / "voice.mp3"
                audio_path.write_bytes(b"fake audio")
                return audio_path

        class FakeWordTracker:
            def transcribe_words(self, audio_path, script_text):
                return [shorts_core.WordEvent(text="test", start_ms=0, end_ms=500)]

            def release(self):
                pass

        class FakeVisualClient:
            def fetch_backgrounds(self, query, duration, work_dir):
                return []

        class FakeCompiler:
            def compile(self, audio_path, word_events, backgrounds, output_path):
                output_path.write_bytes(b"fake mp4")
                return output_path

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = shorts_core.PipelineConfig(
                gemini_api_key="gemini-key",
                pexels_api_key="pexels-key",
                edge_voice="es-MX-DaliaNeural",
                output_dir=tmp_path / "outputs",
                temp_root=tmp_path / "temp",
                tts_voice_overridden=True,
            )
            pipeline = shorts_core.ShortsPipeline.__new__(shorts_core.ShortsPipeline)
            pipeline.config = config
            pipeline.script_engine = FakeScriptEngine()
            pipeline.word_tracker = FakeWordTracker()
            pipeline.visual_client = FakeVisualClient()
            pipeline.compiler = FakeCompiler()

            with patch.object(shorts_core, "EdgeTTSEngine", FakeTTSEngine):
                with patch.object(shorts_core, "get_audio_duration", return_value=1.0):
                    pipeline.run("tema")

        self.assertEqual(voices, ["es-MX-DaliaNeural"])

    def test_default_caption_size_is_large_for_vertical_shorts(self):
        shorts_core = import_shorts_core_with_fakes()
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
        )

        self.assertEqual(config.caption_font_size, 220)

    def test_word_caption_layer_defaults_to_vertical_center(self):
        shorts_core = import_shorts_core_with_fakes()
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
        )

        class FakeLayer:
            def with_duration(self, duration):
                self.duration = duration
                return self

            def with_start(self, start):
                self.start = start
                return self

            def with_position(self, position):
                self.position = position
                return self

        shorts_core.ImageClip = lambda *_, **__: FakeLayer()
        compiler = shorts_core.MoviePyShortCompiler(config)
        compiler._caption_image = lambda _: types.SimpleNamespace(height=400)

        layer = compiler._word_layer(shorts_core.WordEvent(text="center", start_ms=1000, end_ms=1500))

        self.assertEqual(layer.position, ("center", (config.height - 400) // 2))

    def test_default_tts_settings_avoid_overused_flat_voice(self):
        shorts_core = import_shorts_core_with_fakes()
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
        )

        self.assertEqual(config.edge_voice, "en-US-AriaNeural")
        self.assertEqual(config.edge_rate, "+7%")
        self.assertEqual(config.edge_pitch, "-3Hz")
        self.assertEqual(config.edge_volume, "+0%")

    def test_short_compiler_mixes_background_music_under_narration(self):
        shorts_core = import_shorts_core_with_fakes()

        class FakeAudioClip:
            def __init__(self, path):
                self.path = path
                self.duration = 3.0
                self.volume = 1.0

            def subclipped(self, start, end):
                self.start = start
                self.end = end
                self.duration = end - start
                return self

            def with_duration(self, duration):
                self.duration = duration
                return self

            def with_volume_scaled(self, volume):
                self.volume *= volume
                return self

            def close(self):
                self.closed = True

        class FakeVideoClip:
            def __init__(self, duration=None):
                self.duration = duration

            def with_duration(self, duration):
                self.duration = duration
                return self

            def close(self):
                self.closed = True

        class FakeCompositeAudioClip:
            instances = []

            def __init__(self, clips):
                self.clips = clips
                self.duration = None
                self.__class__.instances.append(self)

            def with_duration(self, duration):
                self.duration = duration
                return self

            def close(self):
                self.closed = True

        class FakeCompositeVideoClip(FakeVideoClip):
            instances = []

            def __init__(self, layers, size):
                super().__init__()
                self.layers = layers
                self.size = size
                self.audio = None
                self.__class__.instances.append(self)

            def with_audio(self, audio):
                self.audio = audio
                return self

            def with_fps(self, fps):
                self.fps = fps
                return self

            def write_videofile(self, *args, **kwargs):
                self.write_args = args
                self.write_kwargs = kwargs

        shorts_core.AudioFileClip = FakeAudioClip
        shorts_core.ColorClip = lambda *_, **kwargs: FakeVideoClip(kwargs.get("duration"))
        shorts_core.CompositeAudioClip = FakeCompositeAudioClip
        shorts_core.CompositeVideoClip = FakeCompositeVideoClip

        with tempfile.TemporaryDirectory() as tmp:
            music_path = Path(tmp) / "music.mp3"
            music_path.write_bytes(b"fake")
            narration_path = Path(tmp) / "voice.mp3"
            output_path = Path(tmp) / "out.mp4"
            config = shorts_core.PipelineConfig(
                gemini_api_key="gemini-key",
                pexels_api_key="pexels-key",
                background_music_path=music_path,
                background_music_volume=0.18,
            )

            shorts_core.MoviePyShortCompiler(config).compile(narration_path, [], [], output_path)

        mixed_audio = FakeCompositeAudioClip.instances[-1]
        self.assertEqual([clip.path for clip in mixed_audio.clips], [str(narration_path), str(music_path)])
        self.assertEqual(mixed_audio.clips[1].volume, 0.18)
        self.assertEqual(mixed_audio.duration, 3.0)
        self.assertIs(FakeCompositeVideoClip.instances[-1].audio, mixed_audio)

    def test_cli_help_allows_tts_percent_examples(self):
        shorts_core = import_shorts_core_with_fakes()

        with patch.object(sys, "argv", ["shorts_core.py", "--help"]):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                with self.assertRaises(SystemExit) as raised:
                    shorts_core.parse_args()

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("+7% or", stdout.getvalue())
        self.assertIn("+0% or", stdout.getvalue())

    def test_cli_allows_no_args_for_interactive_mode(self):
        shorts_core = import_shorts_core_with_fakes()

        with patch.object(sys, "argv", ["shorts_core.py"]):
            args = shorts_core.parse_args()

        self.assertIsNone(args.topic)
        self.assertIsNone(args.output)
        self.assertFalse(args.upload)

    def test_cli_accepts_upload_flag(self):
        shorts_core = import_shorts_core_with_fakes()

        with patch.object(sys, "argv", ["shorts_core.py", "--topic", "AI history", "--upload"]):
            args = shorts_core.parse_args()

        self.assertTrue(args.upload)

    def test_youtube_uploader_builds_video_insert_request(self):
        shorts_core = import_shorts_core_with_fakes()

        class FakeMediaFileUpload:
            def __init__(self, path, mimetype, resumable):
                self.path = path
                self.mimetype = mimetype
                self.resumable = resumable

        class FakeCredentials:
            valid = True
            expired = False
            refresh_token = None

            def to_json(self):
                return "{}"

        class FakeFlow:
            @classmethod
            def from_client_secrets_file(cls, path, scopes):
                cls.path = path
                cls.scopes = scopes
                return cls()

            def run_local_server(self, port):
                self.port = port
                return FakeCredentials()

        class FakeInsertRequest:
            def execute(self):
                return {"id": "video-123"}

        class FakeVideos:
            def insert(self, **kwargs):
                self.insert_kwargs = kwargs
                return FakeInsertRequest()

        class FakeService:
            def __init__(self):
                self.videos_resource = FakeVideos()

            def videos(self):
                return self.videos_resource

        fake_service = FakeService()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            client_secrets = tmp_path / "client_secrets.json"
            client_secrets.write_text("{}", encoding="utf-8")
            video_path = tmp_path / "short.mp4"
            video_path.write_bytes(b"fake mp4")
            token_path = tmp_path / "token.json"

            config = shorts_core.PipelineConfig(
                gemini_api_key="gemini-key",
                pexels_api_key="pexels-key",
                youtube_client_secrets_file=client_secrets,
                youtube_token_file=token_path,
                youtube_privacy_status="private",
                youtube_category_id="22",
            )

            with patch.object(shorts_core, "MediaFileUpload", FakeMediaFileUpload):
                with patch.object(shorts_core, "InstalledAppFlow", FakeFlow):
                    with patch.object(shorts_core, "google_api_build", return_value=fake_service):
                        video_id = shorts_core.YouTubeShortsUploader(config).upload(
                            video_path,
                            title="AI history",
                            description="AI history\n\n#Shorts",
                        )
            token_written = token_path.exists()

        insert_kwargs = fake_service.videos_resource.insert_kwargs
        self.assertEqual(video_id, "video-123")
        self.assertEqual(insert_kwargs["part"], "snippet,status")
        self.assertEqual(insert_kwargs["body"]["snippet"]["title"], "AI history")
        self.assertEqual(insert_kwargs["body"]["snippet"]["tags"], ["Shorts"])
        self.assertEqual(insert_kwargs["body"]["snippet"]["categoryId"], "22")
        self.assertEqual(insert_kwargs["body"]["status"]["privacyStatus"], "private")
        self.assertFalse(insert_kwargs["body"]["status"]["selfDeclaredMadeForKids"])
        self.assertEqual(insert_kwargs["media_body"].path, str(video_path))
        self.assertEqual(insert_kwargs["media_body"].mimetype, "video/mp4")
        self.assertTrue(insert_kwargs["media_body"].resumable)
        self.assertTrue(token_written)

    def test_upload_rendered_short_uses_generated_title_from_metadata(self):
        shorts_core = import_shorts_core_with_fakes()

        class FakeUploader:
            calls = []

            def __init__(self, config):
                self.config = config

            def upload(self, video_path, *, title, description):
                self.__class__.calls.append(
                    {
                        "video_path": video_path,
                        "title": title,
                        "description": description,
                    }
                )
                return "video-123"

        topic = (
            "The '8-hour workday' is actually a scam designed for the 1800s, "
            "and it is destroying your productivity."
        )

        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "short.mp4"
            video_path.write_bytes(b"fake mp4")
            video_path.with_suffix(".metadata.json").write_text(
                json.dumps(
                    {
                        "topic": topic,
                        "script_payload": {
                            "title": "Why Your 8-Hour Workday Is Failing",
                            "script": "Test script.",
                            "keywords": ["workday"],
                            "pexels_query": "office workday",
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = shorts_core.PipelineConfig(
                gemini_api_key="gemini-key",
                pexels_api_key="pexels-key",
            )

            with patch.object(shorts_core, "YouTubeShortsUploader", FakeUploader):
                video_id = shorts_core.upload_rendered_short(video_path, topic, config)

        self.assertEqual(video_id, "video-123")
        self.assertEqual(FakeUploader.calls[0]["title"], "Why Your 8-Hour Workday Is Failing")
        self.assertEqual(FakeUploader.calls[0]["description"], shorts_core.youtube_description(topic))

    def test_cli_reports_pipeline_errors_without_traceback(self):
        shorts_core = import_shorts_core_with_fakes()

        with patch.object(shorts_core, "main", side_effect=shorts_core.PipelineError("Gemini unavailable")):
            with self.assertRaises(SystemExit) as raised:
                shorts_core.cli()

        self.assertEqual(str(raised.exception), "error: Gemini unavailable")
        self.assertTrue(raised.exception.__suppress_context__)

    def test_cli_reports_keyboard_interrupt_without_traceback(self):
        shorts_core = import_shorts_core_with_fakes()

        with patch.object(shorts_core, "main", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit) as raised:
                shorts_core.cli()

        self.assertEqual(str(raised.exception), "interrupted")
        self.assertTrue(raised.exception.__suppress_context__)

    def test_interactive_mode_prompts_for_topic_and_output_name(self):
        shorts_core = import_shorts_core_with_fakes()
        args = types.SimpleNamespace(topic=None, output=None)
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
            output_dir=Path("outputs"),
        )

        with patch("builtins.input", side_effect=["AI history", "history-short"]):
            topic, output_path = shorts_core.resolve_run_inputs(args, config)

        self.assertEqual(topic, "AI history")
        self.assertEqual(output_path, Path("outputs/history-short.mp4"))

    def test_cli_output_path_stays_explicit_when_flags_are_used(self):
        shorts_core = import_shorts_core_with_fakes()
        args = types.SimpleNamespace(topic="AI history", output="custom-name")
        config = shorts_core.PipelineConfig(
            gemini_api_key="gemini-key",
            pexels_api_key="pexels-key",
            output_dir=Path("outputs"),
        )

        topic, output_path = shorts_core.resolve_run_inputs(args, config)

        self.assertEqual(topic, "AI history")
        self.assertEqual(output_path, Path("custom-name"))

    def test_system_font_candidates_include_arch_ttf_directory(self):
        shorts_core = import_shorts_core_with_fakes()

        self.assertIn("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", shorts_core.system_font_candidates())

    def test_local_font_loader_uses_scalable_font_when_available(self):
        real_pil = importlib.import_module("PIL.ImageFont")
        self.assertTrue(Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf").exists())
        font = real_pil.truetype("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", size=220)

        self.assertGreater(font.getbbox("HUMILIATION")[3], 150)


if __name__ == "__main__":
    unittest.main()
