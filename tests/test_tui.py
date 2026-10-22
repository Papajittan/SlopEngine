import json
import os
from pathlib import Path
import tempfile
import signal
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Checkbox, Input, Static, TabbedContent, TextArea

from slopengine.app import SlopEngineApp
from slopengine.config_core import PipelineConfig
from slopengine.settings import SettingsStore
from slopengine.state import JobState


class FakeJobs:
    def __init__(self, folder):
        self.running = False
        self.log_dir = folder / "logs"
        self.events = []
        self.starts = []

    def start(self, topic, output, overrides):
        self.starts.append((topic, output, overrides))
        self.running = True
        self.events = [{"type": "job_started"}, {"type": "stage_started", "stage": "script"}]

    def poll(self):
        events, self.events = self.events, []
        return events

    def cancel(self):
        self.running = False
        self.events.append({"type": "job_cancelled", "message": "Cancelled safely"})

    def close(self):
        if self.running:
            self.cancel()


class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.config = PipelineConfig(gemini_api_key="test", pexels_api_key="test", output_dir=self.folder)
        self.config_patch = patch("slopengine.settings.config_from_env", return_value=self.config)
        self.config_patch.start()
        self.jobs = FakeJobs(self.folder)
        self.store = SettingsStore(self.folder / "preferences.json")

    def tearDown(self):
        self.config_patch.stop()
        self.temp.cleanup()

    def app(self):
        return SlopEngineApp(settings_store=self.store, jobs=self.jobs)

    async def test_keyboard_navigation_at_small_size_and_missing_keys(self):
        from dataclasses import replace
        with patch("slopengine.settings.config_from_env", return_value=replace(self.config, gemini_api_key="", pexels_api_key="")):
            app = self.app()
            async with app.run_test(size=(64, 22)) as pilot:
                for key, page in [("ctrl+2", "create"), ("ctrl+3", "library"), ("ctrl+4", "settings"), ("ctrl+5", "logs"), ("ctrl+6", "monitor"), ("ctrl+1", "dashboard")]:
                    await pilot.press(key)
                    self.assertEqual(app.query_one("#pages", TabbedContent).active, page)
                self.assertIn("missing", str(app.query_one("#readiness", Static).render()))
                self.assertTrue(app.query_one("#gemini-setup").display)
                await pilot.resize_terminal(100, 35)
                await pilot.press("ctrl+q")

    async def test_configured_gemini_key_hides_input(self):
        app = self.app()
        async with app.run_test():
            self.assertFalse(app.query_one("#gemini-setup").display)
            self.assertEqual(app.query_one("#gemini-api-key", Input).value, "")

    async def test_missing_gemini_key_can_be_entered_for_generation(self):
        from dataclasses import replace
        with patch.dict(os.environ, {"GEMINI_API_KEY": "your_gemini_key_here"}), patch(
            "slopengine.settings.config_from_env",
            side_effect=lambda **kwargs: replace(self.config, gemini_api_key=os.environ["GEMINI_API_KEY"]),
        ):
            app = self.app()
            async with app.run_test(size=(100, 40)) as pilot:
                self.assertTrue(app.query_one("#gemini-setup").display)
                field = app.query_one("#gemini-api-key", Input)
                self.assertTrue(field.password)
                field.focus()
                field.value = " "
                await pilot.press("enter")
                self.assertTrue(app.query_one("#gemini-setup").display)
                self.assertIn("Enter a Gemini", str(app.query_one("#gemini-key-error", Static).render()))
                field.value = "your_gemini_key_here"
                await pilot.click("#use-gemini-key")
                self.assertTrue(app.query_one("#gemini-setup").display)
                field.value = "fake-gemini-session-key"
                field.focus()
                await pilot.press("enter")
                self.assertEqual(os.environ["GEMINI_API_KEY"], "fake-gemini-session-key")
                self.assertEqual(app.config.gemini_api_key, "fake-gemini-session-key")
                self.assertEqual(field.value, "")
                self.assertFalse(app.query_one("#gemini-setup").display)
                self.assertIn("Gemini: configured", str(app.query_one("#readiness", Static).render()))
                app.query_one("#topic", TextArea).load_text("Stars")
                with patch("slopengine.settings.shutil.which", return_value="/usr/bin/ffmpeg"):
                    await app.action_generate()
                self.assertEqual(len(self.jobs.starts), 1)
                self.assertNotIn("gemini_api_key", self.jobs.starts[0][2])
                self.assertFalse(self.store.path.exists())

    async def test_generation_validation_progress_duplicate_guard_and_cancel(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+2", "ctrl+g")
            self.assertFalse(self.jobs.starts)
            self.assertIn("topic", str(app.query_one("#form-error", Static).render()))
            app.query_one("#topic", TextArea).load_text("A history of stars")
            app.query_one("#output", Input).value = "stars.mp4"
            with patch("slopengine.settings.shutil.which", return_value="/usr/bin/ffmpeg"):
                await pilot.press("ctrl+g")
            await pilot.pause()
            self.assertEqual(len(self.jobs.starts), 1)
            self.assertFalse(self.jobs.starts[0][2]["youtube_upload_enabled"])
            self.assertEqual(self.jobs.starts[0][1], self.folder / "stars.mp4")
            self.assertEqual(app.job.stages["script"], "Running")
            await pilot.press("ctrl+g")
            self.assertEqual(len(self.jobs.starts), 1)
            await pilot.press("ctrl+x")
            await pilot.pause()
            self.assertEqual(app.job.status, "Cancelled")

    async def test_settings_persist_and_validate(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+4")
            app.query_one("#setting-edge_rate", Input).value = "fast"
            app.save_settings()
            self.assertFalse(self.store.path.exists())
            app.query_one("#setting-edge_rate", Input).value = "+3%"
            app.save_settings()
            self.assertTrue(self.store.path.exists())
            self.assertEqual(self.store.load()["edge_rate"], "+3%")
            self.assertNotIn("api_key", self.store.path.read_text())
        restarted = self.app()
        async with restarted.run_test():
            self.assertEqual(restarted.query_one("#job-edge_rate", Input).value, "+3%")

    async def test_completion_library_metadata_and_reuse(self):
        app = self.app()
        async with app.run_test() as pilot:
            path = self.folder / "finished.mp4"
            path.write_bytes(b"finished video")
            path.with_suffix(".metadata.json").write_text(json.dumps({"topic": "Stars", "script_payload": {"title": "The night sky"}}))
            app.job.begin()
            self.jobs.events = [{"type": "stage_completed", "stage": "save"}, {"type": "job_completed", "output": str(path)}]
            app.poll_job()
            self.assertEqual(app.job.status, "Completed")
            self.assertEqual(len(app.entries), 1)
            await pilot.press("ctrl+3")
            await pilot.pause()
            self.assertEqual(app.selected_entry.title, "The night sky")
            await pilot.click("#reuse-topic")
            self.assertEqual(app.query_one("#topic", TextArea).text, "Stars")

    async def test_exit_confirmation_keeps_running_job(self):
        app = self.app()
        async with app.run_test() as pilot:
            self.jobs.running = True
            await pilot.press("ctrl+q")
            await pilot.click("#stay")
            self.assertTrue(self.jobs.running)
            await pilot.press("ctrl+q")
            await pilot.click("#leave")
            self.assertFalse(self.jobs.running)


class StateTests(unittest.TestCase):
    def test_environment_template_is_not_a_configured_gemini_key(self):
        from slopengine.config_core import config_from_env
        with patch.dict(os.environ, {"GEMINI_API_KEY": "your_gemini_key_here"}), patch("slopengine.config_core.load_environment"):
            self.assertEqual(config_from_env(require_keys=False).gemini_api_key, "")

    def test_failure_and_cancellation_are_honest(self):
        state = JobState()
        state.begin()
        state.apply({"type": "stage_started", "stage": "render"})
        state.apply({"type": "job_failed", "message": "FFmpeg failed"})
        self.assertEqual(state.stages["render"], "Failed")
        self.assertEqual(state.stages["save"], "Not run")
        self.assertEqual(state.detail, "FFmpeg failed")
        self.assertEqual(state.output, "")
        state.begin()
        state.apply({"type": "job_cancelled"})
        self.assertEqual(state.stages["script"], "Cancelled")

    def test_termination_handler_closes_jobs_and_restores_signal(self):
        from slopengine.app import main
        previous = signal.getsignal(signal.SIGTERM)
        fake_app = Mock()
        fake_app.run.side_effect = lambda: signal.raise_signal(signal.SIGTERM)
        with patch("slopengine.app.SlopEngineApp", return_value=fake_app):
            main()
        fake_app.exit.assert_called_once_with(return_code=128 + signal.SIGTERM)
        fake_app.jobs.close.assert_called_once()
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
