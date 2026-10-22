from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import DataTable, Input, Static, TabbedContent, TextArea

from slopengine.app import SlopEngineApp
from slopengine.config_core import PipelineConfig
from slopengine.settings import SettingsStore


@dataclass
class Limits:
    max_active_jobs: int = 2
    max_concurrent_renders: int = 1
    max_concurrent_gpu_whisper: int = 1


class QueueJobs:
    def __init__(self, folder):
        self.log_dir = folder / "logs"
        self.limits = Limits()
        self.rows = []
        self.events = []
        self.cancelled = []
        self.retried = []
        self.moved = []
        self.starts = []

    @property
    def running(self):
        return bool(self.rows)

    def start(self, topic, output, overrides):
        job_id = str(len(self.rows) + 1)
        self.starts.append((topic, output, overrides))
        self.rows.append(dict(id=job_id, topic=topic, status="QUEUED", stage="render",
                              waiting_reason="Waiting for render slot", output=f"{job_id}.mp4"))
        return job_id

    def list_jobs(self):
        return [dict(row) for row in self.rows]

    def poll(self):
        events, self.events = self.events, []
        return events

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        self.events.append(dict(type="job_cancelled", job_id=job_id))

    def retry(self, job_id):
        self.retried.append(job_id)

    def reorder(self, job_id, direction):
        self.moved.append((job_id, direction))

    def configure(self, **limits):
        self.limits = Limits(**limits)

    def close(self):
        pass


class QueueTuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.config = PipelineConfig(gemini_api_key="test", pexels_api_key="test", output_dir=self.folder)
        self.config_patch = patch("slopengine.settings.config_from_env", return_value=self.config)
        self.config_patch.start()
        self.jobs = QueueJobs(self.folder)
        self.app = SlopEngineApp(settings_store=SettingsStore(self.folder / "preferences.json"), jobs=self.jobs)

    def tearDown(self):
        self.config_patch.stop()
        self.temp.cleanup()

    async def test_queue_keyboard_selection_cancel_retry_and_reorder(self):
        self.jobs.start("One", None, {})
        self.jobs.start("Two", None, {})
        async with self.app.run_test(size=(80, 28)) as pilot:
            await pilot.press("ctrl+7", "down")
            await pilot.pause()
            self.assertEqual(self.app.query_one("#pages", TabbedContent).active, "queue")
            self.assertEqual(self.app.selected_job_id, "2")
            self.assertIn("Waiting for render slot", str(self.app.query_one("#queue-detail", Static).render()))
            await pilot.press("ctrl+r", "alt+up", "ctrl+x")
            self.assertEqual(self.jobs.retried, ["2"])
            self.assertEqual(self.jobs.moved, [("2", -1)])
            self.assertEqual(self.jobs.cancelled, ["2"])
            self.app.query_one("#queue-table", DataTable).focus()
            await pilot.pause()
            self.assertIs(self.app.focused, self.app.query_one("#queue-table", DataTable))
            table = self.app.query_one("#queue-table", DataTable)
            self.assertEqual(table.cursor_type, "row")
            self.assertTrue(table.show_cursor)
            self.assertEqual(table.row_count, 2)
            self.assertLess(table.cursor_row, table.row_count)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.app.query_one("#pages", TabbedContent).active, "monitor")

    async def test_new_generation_during_active_job_and_bulk_configuration(self):
        self.jobs.start("Existing", None, {})
        async with self.app.run_test() as pilot:
            self.app.query_one("#topic", TextArea).load_text("Another")
            with patch("slopengine.app.shutil.which", return_value="/usr/bin/ffmpeg"):
                await self.app.action_generate()
                self.app.query_one("#bulk-topics", TextArea).load_text("Bulk one\n\nBulk two")
                self.app.add_bulk_jobs()
            self.assertEqual(len(self.jobs.rows), 4)
            self.assertEqual([row[0] for row in self.jobs.starts], ["Existing", "Another", "Bulk one", "Bulk two"])
            self.assertTrue(all(not row[2]["youtube_upload_enabled"] for row in self.jobs.starts[1:]))
            self.app.query_one("#limit-active", Input).value = "3"
            self.app.apply_concurrency_limits()
            self.assertEqual(self.jobs.limits.max_active_jobs, 3)
            self.assertEqual(self.jobs.limits.max_concurrent_renders, 1)
            self.assertEqual(self.app.query_one("#queue-table", DataTable).row_count, 4)

    async def test_progress_for_other_job_does_not_overwrite_selected_monitor(self):
        self.jobs.start("One", None, {})
        self.jobs.start("Two", None, {})
        async with self.app.run_test() as pilot:
            self.app.select_queue_job("1")
            self.jobs.events = [dict(type="stage_started", stage="render", job_id="1", message="First rendering"),
                                dict(type="job_failed", job_id="2", message="Second failed")]
            self.app.poll_job()
            self.assertEqual(self.app.selected_job_id, "1")
            self.assertEqual(self.app.job.stages["render"], "Running")
            self.assertEqual(self.app.job.detail, "First rendering")
            self.assertEqual(self.app.job_states["2"].status, "Failed")

    async def test_recovery_and_waiting_details_follow_persistent_state(self):
        self.jobs.start("Recovered", None, {})
        self.jobs.rows[0].update(status="HELD", stage_states={"script": "COMPLETED", "render": "WAITING"},
                                 waiting_reason="Restored queue; explicit retry required")
        async with self.app.run_test() as pilot:
            self.app.select_queue_job("1")
            self.assertEqual(self.app.job.status, "Held")
            self.assertEqual(self.app.job.stages["script"], "Completed")
            self.assertEqual(self.app.job.elapsed, 0)
            self.assertEqual(self.app.job.output, "")
            self.jobs.rows[0].update(status="WAITING", waiting_reason="Waiting for available memory")
            self.app.poll_job()
            self.assertEqual(self.app.job.status, "Waiting")
            self.assertEqual(self.app.job.detail, "Waiting for available memory")
