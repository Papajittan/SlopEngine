from __future__ import annotations

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from slopengine.config_core import PipelineConfig
from slopengine.jobs import JobManager, read_logs
from slopengine.library import discover_outputs
from slopengine.settings import SettingsStore, build_config, redact, validate_config


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.config = PipelineConfig(gemini_api_key="", pexels_api_key="")

    def test_precedence_and_upload_consent(self):
        with patch("slopengine.settings.config_from_env", return_value=replace(self.config, edge_rate="+1%", youtube_upload_enabled=True)):
            config = build_config({"edge_rate": "+2%"}, {"edge_rate": "+3%"})
            self.assertEqual(config.edge_rate, "+3%")
            self.assertFalse(config.youtube_upload_enabled)
            self.assertTrue(build_config(overrides={"youtube_upload_enabled": True}).youtube_upload_enabled)

    def test_settings_persist_and_reject_secrets(self):
        with tempfile.TemporaryDirectory() as temporary, patch("slopengine.settings.config_from_env", return_value=self.config):
            store = SettingsStore(Path(temporary) / "settings.json")
            store.save({"edge_rate": "+4%", "output_dir": "outputs"})
            self.assertEqual(store.load()["edge_rate"], "+4%")
            with self.assertRaises(ValueError):
                store.save({"gemini_api_key": "secret"})
            store.path.write_text("broken")
            self.assertEqual(store.load(), {})

    def test_voice_choice_and_empty_font_persist(self):
        with tempfile.TemporaryDirectory() as temporary, patch("slopengine.settings.config_from_env", return_value=self.config):
            store = SettingsStore(Path(temporary) / "settings.json")
            store.save({"tts_voice_overridden": True, "caption_font_path": ""})
            self.assertTrue(build_config(store.load()).tts_voice_overridden)
            self.assertIsNone(build_config(store.load()).caption_font_path)

    def test_corrupted_preference_types_report_validation_errors(self):
        with patch("slopengine.settings.config_from_env", return_value=self.config):
            for preferences in ({"whisper_device": []}, {"gemini_model": {}}, {"caption_font_size": "large"}, {"output_dir": []}):
                with self.subTest(preferences=preferences), self.assertRaises(ValueError):
                    build_config(preferences)
        self.assertTrue(validate_config(replace(self.config, whisper_device=[])))

    def test_validation(self):
        invalid = replace(self.config, edge_rate="fast", whisper_device="metal", background_music_volume=2)
        errors = validate_config(invalid, topic=" ", output="bad.txt")
        self.assertGreaterEqual(len(errors), 5)
        with patch("slopengine.settings.shutil.which", return_value=None):
            errors = validate_config(self.config, require_keys=True)
        self.assertEqual(len(errors), 3)
        self.assertIn(".env.example", " ".join(errors))

    def test_existing_output_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "existing.mp4"
            target.touch()
            self.assertIn("Output already exists", " ".join(validate_config(self.config, output=target)))

    def test_redacts_secrets_and_provider_query_parameters(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "supersecret"}):
            result = redact("supersecret https://provider/?key=abcde&x=2 access_token=tokenvalue")
        self.assertNotIn("supersecret", result)
        self.assertNotIn("abcde", result)
        self.assertNotIn("tokenvalue", result)
        oauth = json.loads(redact('{"access_token": "tokenvalue", "refresh_token": "refresh"}'))
        self.assertEqual(oauth["access_token"], "[REDACTED]")
        self.assertEqual(oauth["refresh_token"], "[REDACTED]")
        self.assertNotIn("private-token", redact("{'access_token': 'private-token'}"))
        self.assertNotIn("Bearer private-token", redact('{"Authorization": "Bearer private-token"}'))


class LibraryTests(unittest.TestCase):
    def test_metadata_missing_invalid_and_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for name in ("good", "missing", "broken", "empty", "render.partial"):
                (folder / f"{name}.mp4").write_bytes(b"video" if name != "empty" else b"")
            (folder / "good.metadata.json").write_text(json.dumps({"topic": "Space", "script_payload": {"title": "Stars"}, "youtube": {"video_id": "abc"}}))
            (folder / "broken.metadata.json").write_text("[")
            entries = discover_outputs(folder)
            self.assertEqual(len(entries), 3)
            good = next(entry for entry in entries if entry.path.stem == "good")
            self.assertEqual((good.title, good.topic, good.youtube), ("Stars", "Space", "abc"))
            self.assertTrue(next(entry for entry in entries if entry.path.stem == "broken").metadata_error)


class JobTests(unittest.TestCase):
    def collect(self, manager):
        events = []
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            events.extend(manager.poll())
            if not manager.running:
                events.extend(manager.poll())
                return events
            time.sleep(0.02)
        self.fail("Worker did not finish promptly")

    def fake_process(self, events, exit_code=0):
        process = unittest.mock.Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
        process.stderr = io.StringIO()
        process.poll.return_value = exit_code
        process.returncode = exit_code
        return process

    def test_worker_completion_and_redacted_persistent_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = JobManager(Path(temporary))
            destination = Path(temporary) / "output.mp4"
            process = self.fake_process([{"type": "stage_started", "stage": "script"}, {"type": "job_completed", "output": str(destination)}])
            with patch("slopengine.processes.subprocess.Popen", return_value=process), patch.object(manager, "_signal_process"):
                manager.start("Topic", output=destination)
                destination.write_bytes(b"fake video")
                destination.with_suffix(".metadata.json").write_text("{}")
                events = self.collect(manager)
            self.assertEqual(events[-1]["type"], "job_completed")
            self.assertEqual(len(read_logs(manager.log_path)), 2)
            manager.close()

    def test_worker_crash_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = JobManager(Path(temporary))
            with patch("slopengine.processes.subprocess.Popen", return_value=self.fake_process([], 7)), patch.object(manager, "_signal_process"):
                manager.start("Topic")
                events = self.collect(manager)
            self.assertEqual(events[-1]["type"], "job_failed")
            self.assertIn("7", events[-1]["message"])
            manager.close()

    def test_real_worker_invalid_payload_fails_without_provider_calls(self):
        process = subprocess.run([os.sys.executable, "-m", "slopengine.worker"], input="not json\n", text=True, capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(json.loads(process.stdout.splitlines()[-1])["type"], "job_failed")

    def test_upload_failure_keeps_rendered_output_and_json_protocol(self):
        from slopengine import worker
        class Pipeline:
            def __init__(self, config, event_handler):
                self.emit = event_handler
            def run(self, topic, output):
                self.emit({"type": "job_completed", "output": "saved.mp4"})
                return Path("saved.mp4")
        core = types.SimpleNamespace(ShortsPipeline=Pipeline, upload_rendered_short=unittest.mock.Mock(side_effect=ValueError('{"access_token": "secret-token"}')))
        output = io.StringIO()
        config = PipelineConfig(gemini_api_key="", pexels_api_key="", youtube_upload_enabled=True)
        with patch.dict(os.sys.modules, {"shorts_core": core}), patch("slopengine.worker.build_config", return_value=config), patch("slopengine.worker.validate_config", return_value=[]), patch("slopengine.worker.signal.signal"), patch("sys.stdin", io.StringIO('{"topic":"Topic"}\n')), patch("sys.stdout", output):
            result = worker.main()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(result, 1)
        self.assertEqual([event["type"] for event in events], ["stage_started", "job_failed"])
        self.assertEqual(events[-1]["output"], "saved.mp4")
        self.assertNotIn("secret-token", output.getvalue())

    def test_cancel_reaps_real_process_with_another_active_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = JobManager(Path(temporary))
            # Real process isolates cancellation from provider availability or ML packages.
            real_popen = subprocess.Popen
            def sleeping_worker(*args, **kwargs):
                return real_popen([os.sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            with patch("slopengine.processes.subprocess.Popen", side_effect=sleeping_worker):
                first = manager.start("Topic")
                second = manager.start("Another topic")
                manager.cancel(first)
                manager.cancel(second)
                events = self.collect(manager)
            manager.close()
            self.assertEqual(events[-1]["type"], "job_cancelled")
            self.assertIsNotNone(manager.process.poll())

    @unittest.skipUnless(os.name == "posix", "Process-group cleanup requires POSIX")
    def test_crash_detected_when_descendant_holds_worker_pipes_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = JobManager(Path(temporary))
            real_popen = subprocess.Popen
            child_pid_path = Path(temporary) / "child.pid"
            source = ("import pathlib, subprocess, sys; "
                      "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                      f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
                      "sys.exit(7)")
            def crashing_worker(*args, **kwargs):
                return real_popen([os.sys.executable, "-c", source], **kwargs)
            try:
                started = time.monotonic()
                with patch("slopengine.processes.subprocess.Popen", side_effect=crashing_worker):
                    manager.start("Topic")
                    events = self.collect(manager)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(events[-1]["type"], "job_failed")
                self.assertIn("7", events[-1]["message"])
                child_pid = int(child_pid_path.read_text())
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    status_path = Path(f"/proc/{child_pid}/stat")
                    if status_path.exists() and status_path.read_text().split(")", 1)[1].split()[0] == "Z":
                        break
                    time.sleep(0.02)
                else:
                    self.fail("Crashed worker left a live descendant")
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
