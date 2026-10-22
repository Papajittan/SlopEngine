"""Pipeline integration tests using inexpensive, local providers."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_env_loading import import_shorts_core_with_fakes
from slopengine.events import STAGES, redact


class PipelineEventTests(unittest.TestCase):
    def make_pipeline(self, root, observer=None, fail=None):
        core = import_shorts_core_with_fakes()
        config = core.PipelineConfig("secret-gemini", "secret-pexels", output_dir=root / "outputs", temp_root=root / "temp")
        pipeline = core.ShortsPipeline.__new__(core.ShortsPipeline)
        pipeline.config = config
        pipeline.event_handler = observer

        class Script:
            def synthesize(self, topic):
                if fail == "script":
                    raise RuntimeError("provider failed secret-gemini")
                return core.ScriptPayload("Title", "script", ["test"], "test")

        class TTS:
            def synthesize(self, text, directory):
                path = directory / "voice.mp3"
                path.write_bytes(b"audio")
                return path

        class Tracker:
            def transcribe_words(self, audio, script):
                return [core.WordEvent("script", 0, 100)]
            def release(self):
                pass

        class Visuals:
            def fetch_backgrounds(self, query, duration, directory):
                return []

        class Compiler:
            def compile(self, audio, words, backgrounds, output):
                output.write_bytes(b"video")
                if fail == "render":
                    raise RuntimeError("render failure")
                if fail == "cancel":
                    raise KeyboardInterrupt()
                return output

        pipeline.script_engine = Script()
        pipeline.tts_engine = TTS()
        pipeline.word_tracker = Tracker()
        pipeline.visual_client = Visuals()
        pipeline.compiler = Compiler()
        return core, pipeline

    def test_real_stage_order_and_completed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            root = Path(directory)
            core, pipeline = self.make_pipeline(root, events.append)
            with patch.object(core, "get_audio_duration", return_value=1):
                output = pipeline.run("topic")
            self.assertTrue(output.is_file())
            self.assertTrue(output.with_suffix(".metadata.json").is_file())
            self.assertEqual([e["stage"] for e in events if e["type"] == "stage_completed"], list(STAGES[:-1]))
            self.assertEqual(events[0]["type"], "job_started")
            self.assertEqual(events[-1]["type"], "job_completed")
            self.assertFalse(list(output.parent.glob(".slopengine-*")))

    def test_failure_and_cancellation_remove_partial_video(self):
        for failure, exception, terminal in [("render", RuntimeError, "job_failed"), ("cancel", KeyboardInterrupt, "job_cancelled")]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                events = []
                core, pipeline = self.make_pipeline(root, events.append, failure)
                with patch.object(core, "get_audio_duration", return_value=1), self.assertRaises(exception):
                    pipeline.run("topic")
                self.assertEqual(events[-1]["type"], terminal)
                self.assertEqual(list(pipeline.config.output_dir.iterdir()), [])
                self.assertEqual(list(pipeline.config.temp_root.iterdir()), [])

    def test_failures_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            _, pipeline = self.make_pipeline(Path(directory), events.append, "script")
            with self.assertRaises(RuntimeError):
                pipeline.run("topic")
            self.assertNotIn("secret-gemini", str(events))
            self.assertIn("[REDACTED]", events[-1]["message"])

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, pipeline = self.make_pipeline(root)
            target = root / "existing.mp4"
            target.write_bytes(b"original")
            with self.assertRaises(core.PipelineError):
                pipeline.run("topic", target)
            self.assertEqual(target.read_bytes(), b"original")

    def test_publication_race_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, pipeline = self.make_pipeline(root)
            target = root / "target.mp4"
            original = pipeline.compiler.compile
            def racing_compile(*args):
                rendered = original(*args)
                target.write_bytes(b"other job")
                return rendered
            pipeline.compiler.compile = racing_compile
            with patch.object(core, "get_audio_duration", return_value=1), self.assertRaises(FileExistsError):
                pipeline.run("topic", target)
            self.assertEqual(target.read_bytes(), b"other job")
            self.assertFalse(target.with_suffix(".metadata.json").exists())

    def test_cancellation_during_publication_keeps_complete_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, pipeline = self.make_pipeline(root)
            target = root / "target.mp4"
            masks = []
            def cancel_when_unblocked(operation, mask):
                masks.append(operation)
                if operation == core.signal.SIG_SETMASK:
                    raise KeyboardInterrupt()
                return set()
            with patch.object(core, "get_audio_duration", return_value=1):
                with patch.object(core.signal, "pthread_sigmask", side_effect=cancel_when_unblocked, create=True):
                    with self.assertRaises(KeyboardInterrupt):
                        pipeline.run("topic", target)
            self.assertEqual(masks, [core.signal.SIG_BLOCK, core.signal.SIG_SETMASK])
            self.assertTrue(target.is_file())
            self.assertTrue(target.with_suffix(".metadata.json").is_file())
            self.assertFalse(list(root.glob(".slopengine-*")))

    def test_observer_failure_does_not_break_generation(self):
        def broken_observer(event):
            raise ValueError("observer")
        with tempfile.TemporaryDirectory() as directory:
            core, pipeline = self.make_pipeline(Path(directory), broken_observer)
            with patch.object(core, "get_audio_duration", return_value=1):
                self.assertTrue(pipeline.run("topic").exists())

    def test_redacts_credential_fields(self):
        message = redact('Authorization: Bearer xyz access_token=abc api_key=def')
        for secret in ("xyz", "abc", "def"):
            self.assertNotIn(secret, message)
