"""Auto Mode controller and duplicate-history tests; all topic responses are local fakes."""
from dataclasses import replace
import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from slopengine.auto_mode import AutoBudgetError, AutoMode, AutoModeError, AutoSettings, TopicGenerator
from slopengine.app import SlopEngineApp
from slopengine.config_core import PipelineConfig
from slopengine.jobs import GiB, JobManager, ResourceLimits
from slopengine.topic_history import TopicHistory, compare_topics, fingerprint
from slopengine.settings import SettingsStore, build_config
from textual.widgets import Checkbox, Input, Select, Static, TabbedContent, TextArea


class FakeGenerator:
    def __init__(self, config, batches):
        self.batches = batches
        self.calls = []

    def generate(self, niche, count, **context):
        self.calls.append((niche, count, context))
        permit = context["permit_request"]
        if not permit(0):
            from slopengine.auto_mode import AutoRequestPaused
            raise AutoRequestPaused()
        batch = self.batches.pop(0)
        return batch[:count]


class AutoModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = PipelineConfig(gemini_api_key="local-test-key", pexels_api_key="local-test-pexels",
                                     output_dir=self.root / "outputs", temp_root=self.root / "temp")
        self.config.output_dir.mkdir()
        self.config.temp_root.mkdir()
        self.manager = JobManager(
            self.root / "logs", db_path=self.root / "jobs.sqlite3",
            limits=ResourceLimits(max_active_jobs=1, system_headroom_gib=0),
            memory_available=lambda: 0,
        )
        self.addCleanup(self.manager.close)
        self.settings = AutoSettings(requests_per_minute=100, request_budget=0, cooldown_seconds=0,
                                     min_free_disk_gib=0, max_consecutive_failures=3,
                                     duplicate_batches_before_error=2, similarity_threshold=0.9)

    def make_controller(self, batches):
        fake = FakeGenerator(self.config, list(batches))
        controller = AutoMode(self.manager, generator_factory=lambda config: fake, settings=self.settings)
        self.addCleanup(controller.close)
        return controller, fake

    def begin(self, controller, niche="space science", target=2):
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            controller.start(niche, target, config=self.config)

    def until_topics(self, controller, count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            controller.tick()
            rows = controller.snapshot()["topics"]
            if len(rows) >= count:
                return rows
            time.sleep(0.005)
        self.fail(f"Timed out waiting for {count} topics; got {controller.snapshot()}")

    def complete_job(self, row):
        output = Path(row["job_output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"local test video")
        output.with_suffix(".metadata.json").write_text(json.dumps({"topic": row["title"], "script_payload": {"title": row["title"]}}))
        self.manager.db.execute("UPDATE jobs SET status='COMPLETED',video_finalized=1 WHERE id=?", (row["job_id"],))
        self.manager.db.commit()
        self.manager.update_auto_topic(row["job_id"], "COMPLETED", str(output), title=row["title"], outcome="test output")

    def fail_job(self, row):
        self.manager.db.execute("UPDATE jobs SET status='FAILED',message='simulated render failure' WHERE id=?", (row["job_id"],))
        self.manager.db.commit()
        self.manager.update_auto_topic(row["job_id"], "FAILED", outcome="simulated render failure")

    def test_exact_finite_successes_with_failure_replacement(self):
        controller, fake = self.make_controller([
            [{"title": "How pulsars keep time", "angle": "A rotating neutron star creates an unusually stable radio signal."},
             {"title": "Why Saturn has a hexagon", "angle": "A polar jet stream forms a persistent six-sided wave."}],
            [{"title": "The Moon's shrinking interior", "angle": "Cooling changes the lunar surface through thrust faults."}],
        ])
        self.begin(controller, target=2)
        first = self.until_topics(controller, 2)
        self.assertEqual([row["status"] for row in first], ["QUEUED", "QUEUED"])
        self.fail_job(first[0])
        self.complete_job(first[1])
        controller.tick()
        replacement = self.until_topics(controller, 3)[2]
        self.assertEqual(replacement["title"], "The Moon's shrinking interior")
        self.complete_job(replacement)
        controller.tick()
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["state"], "COMPLETED")
        self.assertEqual(snapshot["generated"], 2)
        self.assertEqual(self.manager.db.execute("SELECT COUNT(*) FROM jobs WHERE auto_session_id=? AND status='COMPLETED'",
                                                 (controller.session_id,)).fetchone()[0], 2)
        self.assertEqual(self.manager.auto_session(controller.session_id)["consecutive_failures"], 0)

    def test_auto_upload_consent_and_pipeline_paths_are_saved_in_session_and_jobs(self):
        music_path = self.root / "music" / "track.mp3"
        music_path.parent.mkdir(parents=True, exist_ok=True)
        music_path.write_bytes(b"local test music")
        upload_config = replace(
            self.config,
            youtube_upload_enabled=True,
            youtube_privacy_status="unlisted",
            youtube_client_secrets_file=self.root / "oauth" / "client.json",
            youtube_token_file=self.root / "state" / "token.json",
            background_music_path=music_path,
            edge_voice="en-US-GuyNeural",
            tts_voice_overridden=True,
        )
        controller, _ = self.make_controller([[]])
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)), \
             patch.dict(os.environ, {"YOUTUBE_UPLOAD_ENABLED": "true"}):
            controller.start("space science", 1, config=upload_config)
        session_config = json.loads(self.manager.auto_session(controller.session_id)["config"])
        self.assertTrue(session_config["youtube_upload_enabled"])
        self.assertEqual(session_config["youtube_privacy_status"], "unlisted")
        for key, path in (("output_dir", upload_config.output_dir), ("temp_root", upload_config.temp_root),
                          ("background_music_path", upload_config.background_music_path),
                          ("youtube_client_secrets_file", upload_config.youtube_client_secrets_file),
                          ("youtube_token_file", upload_config.youtube_token_file)):
            self.assertEqual(session_config[key], str(path.expanduser().resolve()))
        self.assertNotIn("gemini_api_key", session_config)
        self.assertNotIn("pexels_api_key", session_config)
        result = self.manager.start_auto("A distinct Auto topic", controller.session_id,
                                         "A distinct Auto topic", "A unique mechanism",
                                         config=controller.config, niche="space science")
        self.assertTrue(result["accepted"])
        job = next(row for row in self.manager.list_jobs() if row["id"] == result["job_id"])
        self.assertTrue(job["config"]["youtube_upload_enabled"])
        self.assertEqual(job["config"]["youtube_privacy_status"], "unlisted")
        self.assertEqual(job["config"]["youtube_token_file"], str(upload_config.youtube_token_file.resolve()))
        self.assertFalse(build_config(overrides={"youtube_upload_enabled": False}).youtube_upload_enabled)

    def test_resume_preserves_saved_auto_upload_and_pipeline_options_but_uses_current_keys(self):
        music_path = self.root / "saved" / "music.mp3"
        music_path.parent.mkdir(parents=True, exist_ok=True)
        music_path.write_bytes(b"local test music")
        saved_config = replace(
            self.config,
            youtube_upload_enabled=True,
            youtube_privacy_status="public",
            youtube_client_secrets_file=self.root / "saved" / "client.json",
            youtube_token_file=self.root / "saved" / "token.json",
            background_music_path=music_path,
            edge_voice="en-GB-SoniaNeural",
            tts_voice_overridden=True,
        )
        controller, _ = self.make_controller([[]])
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            controller.start("space science", 2, config=saved_config)
        controller.pause()
        controller.tick()
        resumed = AutoMode(self.manager,
                           generator_factory=lambda config: FakeGenerator(config, []),
                           settings=self.settings)
        self.addCleanup(resumed.close)
        current_config = replace(self.config, gemini_api_key="current-gemini", pexels_api_key="current-pexels",
                                 youtube_upload_enabled=False,
                                 youtube_client_secrets_file=self.root / "current" / "client.json")
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            resumed.resume(config=current_config)
        self.assertTrue(resumed.config.youtube_upload_enabled)
        self.assertEqual(resumed.config.youtube_privacy_status, "public")
        self.assertEqual(resumed.config.edge_voice, "en-GB-SoniaNeural")
        self.assertEqual(resumed.config.background_music_path, saved_config.background_music_path.resolve())
        self.assertEqual(resumed.config.youtube_client_secrets_file, saved_config.youtube_client_secrets_file.resolve())
        self.assertEqual(resumed.config.youtube_token_file, saved_config.youtube_token_file.resolve())
        self.assertEqual(resumed.config.gemini_api_key, "current-gemini")
        self.assertEqual(resumed.config.pexels_api_key, "current-pexels")

    def test_duplicate_batches_get_stronger_guidance_and_stop_boundedly(self):
        self.manager.start("Why light bends around stars", config=self.config)
        ideas = [{"title": "How starlight warps near stars", "angle": "Gravity changes the path light follows."}]
        controller, fake = self.make_controller([ideas, ideas])
        self.begin(controller, target=1)
        controller.tick()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and controller.state != "ERROR":
            controller.tick()
            time.sleep(0.005)
        self.assertEqual(controller.state, "ERROR")
        self.assertIn("no unique topics", controller.message)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("last 1 batch", fake.calls[1][2]["diversity_guidance"])
        self.assertEqual(self.manager.db.execute("SELECT COUNT(*) FROM auto_topics WHERE session_id=? AND status='REJECTED'",
                                                 (controller.session_id,)).fetchone()[0], 2)

    def test_unlimited_session_refills_after_initial_queue_finishes(self):
        batch_one = [
            {"title": "Pulsar timing arrays", "angle": "Use rotating stars to detect gravitational waves."},
            {"title": "Saturn hexagon jet", "angle": "Explain the polar atmospheric standing wave."},
            {"title": "Europa hidden ocean", "angle": "Magnetic induction reveals salty water."},
            {"title": "Lunar thrust faults", "angle": "Cooling leaves scarps across the Moon."},
            {"title": "Neutrino observatories", "angle": "Ice detects particles from distant explosions."},
        ]
        batch_two = [
            {"title": "Comet dust tails", "angle": "Solar radiation pushes grains away from the nucleus."},
            {"title": "Jupiter aurora storms", "angle": "Magnetospheric particles light up the poles."},
            {"title": "Titan methane rainfall", "angle": "A cold moon runs a hydrocarbon weather cycle."},
            {"title": "Venus slow rotation", "angle": "Radar tracked a day longer than its year."},
            {"title": "Asteroid binary orbits", "angle": "A tiny moon reveals how rubble piles hold together."},
        ]
        controller, _ = self.make_controller([batch_one, batch_two])
        self.begin(controller, target=None)
        initial = self.until_topics(controller, 3)
        self.assertEqual(sum(row["job_status"] == "QUEUED" for row in initial), 3)
        for row in initial:
            self.complete_job(row)
        controller.tick()
        refilled = self.until_topics(controller, 6)
        self.assertEqual(controller.state, "RUNNING")
        self.assertEqual(controller.snapshot()["generated"], 3)
        self.assertEqual(sum(row["job_status"] == "QUEUED" for row in refilled), 3)

    def test_long_finite_target_counts_only_finalized_successes(self):
        titles = ["Pulsar timing", "Saturn's hexagon", "Europa ocean", "Lunar thrust faults", "Neutrino astronomy",
                  "Comet dust tails", "Jupiter auroras", "Solar wind", "Titan methane rain", "Venus rotation",
                  "Mars dust storms", "Exoplanet spectra", "Asteroid binaries", "Earth's magnetic flip",
                  "Fast radio bursts", "Solar eclipses", "Kuiper belt objects", "Tidal heating", "Meteor airbursts",
                  "Planetary rings"]
        batches = [[{"title": title, "angle": f"Specific mechanism and evidence for {title} number {index}"}]
                   for index, title in enumerate(titles)]
        controller, _ = self.make_controller(batches)
        self.begin(controller, target=10)
        processed = 0
        failed = 0
        while controller.state == "RUNNING" and processed < 20:
            rows = self.until_topics(controller, max(3, len(controller.snapshot()["topics"])))
            queued = [row for row in rows if row["job_status"] == "QUEUED"]
            if not queued:
                controller.tick()
                time.sleep(0.005)
                processed += 1
                continue
            for row in queued:
                if failed < 2:
                    self.fail_job(row)
                    failed += 1
                else:
                    self.complete_job(row)
            controller.tick()
            processed += 1
            if controller.state == "RUNNING" and controller.snapshot()["generated"] < 10:
                self.until_topics(controller, len(rows) + 3)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["state"], "COMPLETED")
        self.assertEqual(snapshot["generated"], 10)
        self.assertEqual(self.manager.db.execute("SELECT COUNT(*) FROM jobs WHERE auto_session_id=? AND status='COMPLETED'",
                                                 (controller.session_id,)).fetchone()[0], 10)
        self.assertEqual(self.manager.db.execute("SELECT COUNT(*) FROM jobs WHERE auto_session_id=? AND status='FAILED'",
                                                 (controller.session_id,)).fetchone()[0], 2)

    def test_bounded_pending_queue_under_memory_pressure(self):
        controller, _ = self.make_controller([[{"title": f"Topic {i}", "angle": f"Angle {i}"} for i in range(5)]])
        self.begin(controller, target=None)
        rows = self.until_topics(controller, 3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row["job_status"] == "QUEUED" for row in rows), 3)
        self.assertEqual(len([row for row in self.manager.list_jobs() if row["auto_session_id"] == controller.session_id]), 3)
        controller.tick()
        self.assertEqual(len(fake_batches := controller.snapshot()["topics"]), 3)

    def test_pause_resume_graceful_stop_and_cancel_are_session_scoped(self):
        controller, _ = self.make_controller([[{"title": "Topic one", "angle": "One angle"},
                                               {"title": "Topic two", "angle": "Two angle"}]])
        manual_id = self.manager.start("manual remains queued", config=self.config)
        self.begin(controller, target=2)
        rows = self.until_topics(controller, 2)
        ids = [row["job_id"] for row in rows]
        controller.pause()
        controller.tick()
        self.assertEqual(controller.state, "PAUSED")
        self.assertTrue(all(self.manager.db.execute("SELECT auto_paused FROM jobs WHERE id=?", (job_id,)).fetchone()[0] for job_id in ids))
        controller.resume(config=self.config)
        self.assertTrue(all(self.manager.db.execute("SELECT auto_paused FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == 0 for job_id in ids))
        controller.stop()
        controller.tick()
        self.assertEqual(controller.state, "STOPPED")
        self.assertEqual(self.manager.db.execute("SELECT status FROM jobs WHERE id=?", (manual_id,)).fetchone()[0], "QUEUED")
        self.assertTrue(all(self.manager.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "CANCELLED" for job_id in ids))
        cancelling, _ = self.make_controller([[{"title": "Cancel one", "angle": "One"},
                                                {"title": "Cancel two", "angle": "Two"}]])
        self.begin(cancelling, target=2)
        cancel_rows = self.until_topics(cancelling, 2)
        cancelling.stop(cancel=True)
        cancelling.tick()
        self.assertEqual(cancelling.state, "STOPPED")
        self.assertTrue(all(self.manager.db.execute("SELECT status FROM jobs WHERE id=?", (row["job_id"],)).fetchone()[0] == "CANCELLED" for row in cancel_rows))
        self.assertEqual(self.manager.db.execute("SELECT status FROM jobs WHERE id=?", (manual_id,)).fetchone()[0], "QUEUED")

    def test_restart_requires_explicit_resume_and_reuses_held_job_id(self):
        controller, _ = self.make_controller([[{"title": "Topic one", "angle": "One angle"}]])
        self.begin(controller, target=2)
        row = self.until_topics(controller, 1)[0]
        old_session, old_job = controller.session_id, row["job_id"]
        finalized = self.manager.start_auto("Already finalized", old_session, "Already finalized title",
                                            "Recovered after the output commit.", config=self.config,
                                            niche="space science")
        finalized_row = next(item for item in self.manager.auto_topic_rows(old_session)
                             if item["job_id"] == finalized["job_id"])
        output = Path(finalized_row["job_output"])
        output.write_bytes(b"finalized before crash")
        output.with_suffix(".metadata.json").write_text(json.dumps({"topic": "Already finalized",
                                                                      "script_payload": {"title": "Recovered title"}}))
        final_states = next(job for job in self.manager.list_jobs() if job["id"] == finalized["job_id"])["stage_states"]
        final_states["save"] = "RUNNING"
        self.manager.db.execute("UPDATE jobs SET status='RUNNING',stage='save',stage_states=? WHERE id=?",
                                (json.dumps(final_states), finalized["job_id"]))
        self.manager.db.commit()
        controller.close()
        self.manager.close()
        resumed_manager = JobManager(
            self.root / "logs", db_path=self.root / "jobs.sqlite3",
            limits=ResourceLimits(max_active_jobs=1, system_headroom_gib=0),
            memory_available=lambda: 0,
        )
        self.addCleanup(resumed_manager.close)
        self.manager = resumed_manager
        restored = AutoMode(resumed_manager, generator_factory=lambda config: FakeGenerator(config, []), settings=self.settings)
        self.addCleanup(restored.close)
        self.assertEqual(restored.state, "PAUSED")
        self.assertEqual(resumed_manager.db.execute("SELECT status FROM jobs WHERE id=?", (old_job,)).fetchone()[0], "HELD")
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            restored.resume(config=self.config)
        self.assertEqual(restored.state, "RUNNING")
        self.assertEqual(restored.session_id, old_session)
        self.assertEqual(resumed_manager.db.execute("SELECT COUNT(*) FROM jobs WHERE auto_session_id=?", (old_session,)).fetchone()[0], 2)
        self.assertEqual(resumed_manager.db.execute("SELECT status FROM jobs WHERE id=?", (old_job,)).fetchone()[0], "QUEUED")
        self.assertEqual(resumed_manager.db.execute("SELECT status FROM jobs WHERE id=?", (finalized["job_id"],)).fetchone()[0], "COMPLETED")
        self.assertEqual(restored.snapshot()["generated"], 1)

    def test_error_session_with_held_work_requires_resume_or_stop(self):
        controller, _ = self.make_controller([[{"title": "Held topic", "angle": "Held angle"}]])
        self.begin(controller, target=2)
        row = self.until_topics(controller, 1)[0]
        session_id, job_id = controller.session_id, row["job_id"]
        controller._fail("simulated disk error")
        controller.close()
        self.manager.close()
        resumed_manager = JobManager(
            self.root / "logs", db_path=self.root / "jobs.sqlite3",
            limits=ResourceLimits(max_active_jobs=1, system_headroom_gib=0),
            memory_available=lambda: 0,
        )
        self.addCleanup(resumed_manager.close)
        self.manager = resumed_manager
        restored = AutoMode(resumed_manager, generator_factory=lambda config: FakeGenerator(config, []), settings=self.settings)
        self.addCleanup(restored.close)
        self.assertEqual(restored.state, "ERROR")
        self.assertEqual(restored.snapshot()["queued"], 1)
        with self.assertRaisesRegex(ValueError, "queued or active jobs"):
            self.begin(restored, niche="new niche", target=1)
        restored.stop(cancel=True)
        restored.tick()
        self.assertEqual(restored.state, "STOPPED")
        self.assertEqual(resumed_manager.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0], "CANCELLED")

    def test_resume_resets_error_ceiling_after_the_user_fixes_the_cause(self):
        controller, _ = self.make_controller([[{"title": "Pulsar timing arrays", "angle": "Radio pulses track gravity."},
                                               {"title": "Saturn polar jet", "angle": "A standing wave forms a hexagon."}]])
        controller.settings = replace(self.settings, max_consecutive_failures=2)
        self.begin(controller, target=2)
        rows = self.until_topics(controller, 2)
        for row in rows:
            self.fail_job(row)
        controller.tick()
        self.assertEqual(controller.state, "ERROR")
        self.assertEqual(self.manager.auto_session(controller.session_id)["consecutive_failures"], 2)
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            controller.resume(config=self.config)
        self.assertEqual(controller.state, "RUNNING")
        self.assertEqual(self.manager.auto_session(controller.session_id)["consecutive_failures"], 0)

    def test_parallel_producers_reserve_at_most_pending_target(self):
        controller, _ = self.make_controller([[]])
        self.begin(controller, target=None)
        barrier = threading.Barrier(7, timeout=3)
        results = []
        result_lock = threading.Lock()

        def reserve(index):
            barrier.wait()
            candidates = [
                ("Pulsar timing arrays", "Gravitational-wave timing signals"),
                ("Saturn's hexagon jet", "A polar standing atmospheric wave"),
                ("Europa subsurface ocean", "Magnetic evidence for salty water"),
                ("Lunar thrust scarps", "Cooling and contraction of the Moon"),
                ("Neutrino ice observatory", "Particles tracked in Antarctic ice"),
                ("Titan methane rainfall", "Weather on Saturn's largest moon"),
            ]
            title, angle = candidates[index]
            value = self.manager.start_auto(title, controller.session_id,
                                            title, angle,
                                            config=self.config, niche="space science")
            with result_lock:
                results.append(value)

        threads = [threading.Thread(target=reserve, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(result["accepted"] for result in results), 3)
        self.assertEqual(sum(result["reason"] == "capacity" for result in results if not result["accepted"]), 3)
        reader = sqlite3.connect(self.manager.db_path)
        self.addCleanup(reader.close)
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM jobs WHERE auto_session_id=?", (controller.session_id,)).fetchone()[0], 3)

    def test_external_sqlite_writer_serializes_with_duplicate_reservation(self):
        controller, _ = self.make_controller([[]])
        self.begin(controller, target=None)
        external = sqlite3.connect(self.manager.db_path, timeout=3, check_same_thread=False)
        external.row_factory = sqlite3.Row
        self.addCleanup(external.close)
        external_history = TopicHistory(external)
        external.execute("BEGIN IMMEDIATE")
        external_history.record_manual("Same title from another connection", "external-writer")
        results = []
        started = threading.Event()

        def reserve():
            started.set()
            results.append(self.manager.start_auto("same topic", controller.session_id,
                                                   "Same title from another connection", "A matching angle",
                                                   config=self.config, niche="space science"))

        thread = threading.Thread(target=reserve)
        thread.start()
        self.assertTrue(started.wait(1))
        time.sleep(0.05)
        external.commit()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["reason"], "duplicate")
        self.assertEqual(self.manager.db.execute("SELECT COUNT(*) FROM jobs WHERE auto_session_id=?",
                                                 (controller.session_id,)).fetchone()[0], 0)

    def test_two_job_manager_connections_cannot_reserve_the_same_topic(self):
        controller, _ = self.make_controller([[]])
        self.begin(controller, target=None)
        second = copy.copy(self.manager)
        second.db = sqlite3.connect(self.manager.db_path, timeout=3, check_same_thread=False)
        second.db.row_factory = sqlite3.Row
        second._lock = threading.RLock()
        second._workers = {}
        second._credentials = {}
        second._closed = False
        second._schedule = lambda: None
        second.history = TopicHistory(second.db)
        self.addCleanup(second.db.close)
        barrier = threading.Barrier(2)
        results = []

        def reserve(manager):
            barrier.wait()
            results.append(manager.start_auto("same topic", controller.session_id, "Same title",
                                              "Same angle", config=self.config, niche="space science"))

        threads = [threading.Thread(target=reserve, args=(manager,)) for manager in (self.manager, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=4)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(result["accepted"] for result in results), 1)
        self.assertEqual(sum(result.get("reason") == "duplicate" for result in results), 1)
        self.assertEqual(self.manager.db.execute("SELECT COUNT(*) FROM jobs WHERE auto_session_id=?",
                                                 (controller.session_id,)).fetchone()[0], 1)

    def test_topic_request_budget_and_pause_interrupt_rate_wait(self):
        limited = AutoMode(self.manager, generator_factory=lambda config: FakeGenerator(config, []),
                           settings=replace(self.settings, request_budget=1))
        self.addCleanup(limited.close)
        self.begin(limited, target=1)
        epoch = limited._epoch
        self.assertTrue(limited._request_permit(epoch))
        with self.assertRaises(AutoBudgetError):
            limited._request_permit(epoch)
        self.assertEqual(limited.snapshot()["request_count"], 1)
        limited.stop()
        limited.tick()

        rate = AutoMode(self.manager, generator_factory=lambda config: FakeGenerator(config, []),
                        settings=replace(self.settings, requests_per_minute=1))
        self.addCleanup(rate.close)
        self.begin(rate, target=1)
        # The same manager restores the previous session's persisted settings.
        rate.settings = replace(self.settings, requests_per_minute=1)
        self.assertTrue(rate._request_permit(rate._epoch))
        result = []
        waiting = threading.Thread(target=lambda: result.append(rate._request_permit(rate._epoch)))
        waiting.start()
        time.sleep(0.05)
        rate.pause()
        waiting.join(timeout=1)
        self.assertEqual(result, [False])
        self.assertEqual(rate.snapshot()["request_count"], 1)

    def test_late_topic_response_is_discarded_after_pause_and_explicit_resume(self):
        entered, release = threading.Event(), threading.Event()

        class SlowGenerator:
            def __init__(self):
                self.calls = 0

            def generate(self, niche, count, **context):
                if not context["permit_request"](0):
                    from slopengine.auto_mode import AutoRequestPaused
                    raise AutoRequestPaused()
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    release.wait(2)
                    return [{"title": "Stale response", "angle": "Must never be reserved."}]
                return [{"title": "Fresh response", "angle": "Only returned after explicit resume."}]

        generator = SlowGenerator()
        controller = AutoMode(self.manager, generator_factory=lambda config: generator, settings=self.settings)
        self.addCleanup(controller.close)
        self.begin(controller, target=2)
        controller.tick()
        self.assertTrue(entered.wait(1))
        controller.pause()
        controller.tick()
        self.assertEqual(controller.state, "PAUSED")
        release.set()
        deadline = time.monotonic() + 2
        while controller._future and not controller._future.done() and time.monotonic() < deadline:
            time.sleep(0.005)
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            controller.resume(config=self.config)
        controller.tick()  # consumes and discards the stale future epoch
        self.assertEqual(controller.snapshot()["topics"], [])
        self.until_topics(controller, 1)
        self.assertEqual(controller.snapshot()["topics"][0]["title"], "Fresh response")

    def test_late_response_from_stopped_session_cannot_enter_new_session(self):
        entered, release = threading.Event(), threading.Event()

        class SlowGenerator:
            def __init__(self):
                self.calls = 0

            def generate(self, niche, count, **context):
                if not context["permit_request"](0):
                    from slopengine.auto_mode import AutoRequestPaused
                    raise AutoRequestPaused()
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    release.wait(2)
                    return [{"title": "Old session title", "angle": "Must not cross sessions."}]
                return [{"title": "New session title", "angle": "Belongs to new niche only."}]

        generator = SlowGenerator()
        controller = AutoMode(self.manager, generator_factory=lambda config: generator, settings=self.settings)
        self.addCleanup(controller.close)
        self.begin(controller, niche="old niche", target=1)
        old_session = controller.session_id
        controller.tick()
        self.assertTrue(entered.wait(1))
        controller.stop()
        controller.tick()
        self.assertEqual(controller.state, "STOPPED")
        self.begin(controller, niche="new niche", target=1)
        self.assertNotEqual(controller.session_id, old_session)
        release.set()
        self.until_topics(controller, 1)
        self.assertEqual(controller.snapshot()["topics"][0]["title"], "New session title")
        self.assertEqual(self.manager.db.execute("SELECT COUNT(*) FROM auto_topics WHERE session_id=? AND title='Old session title'",
                                                 (controller.session_id,)).fetchone()[0], 0)

    def test_invalid_count_and_low_disk_fail_before_persisting_a_session(self):
        controller, _ = self.make_controller([[]])
        controller.settings = replace(self.settings, min_free_disk_gib=1)
        for invalid in (0, -1, 1.5, "2", True):
            with self.assertRaises(ValueError):
                controller.start("science", invalid, config=self.config)
        with self.assertRaisesRegex(ValueError, "Enter a niche"):
            controller.start("  ", None, config=self.config)
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(ValueError, "free"):
                controller.start("science", 2, config=self.config)
        self.assertEqual(controller.state, "ERROR")
        self.assertIsNone(controller.session_id)
        self.assertEqual(self.manager.auto_sessions(), [])
        with self.assertRaisesRegex(ValueError, "no persisted Auto session"):
            controller.resume(config=self.config)

    def test_final_script_title_overlap_is_rejected_before_render(self):
        self.manager.start("Why light bends around stars", config=self.config)
        session_id = "pre-render-check"
        self.manager.create_auto_session(session_id=session_id, niche="space science", target=1, config={})
        result = self.manager.start_auto("A solar topic", session_id, "Why solar flares erupt",
                                         "Magnetic fields reconnect.", config=self.config, niche="space science")
        job_id = result["job_id"]

        class FakeProcess:
            pid = __import__("os").getpid()
            stdin = None
            returncode = None

        class FakeWorker:
            process = FakeProcess()
            running = True

            def poll(self):
                return [{"type": "stage_completed", "stage": "script", "title": "How starlight warps near stars"}]

            def cancel(self):
                self.cancelled = True

            def close(self):
                pass

        worker = FakeWorker()
        self.manager.db.execute("UPDATE jobs SET status='RUNNING',stage_states=? WHERE id=?",
                                (json.dumps({"script": "RUNNING"}), job_id))
        self.manager.db.commit()
        self.manager._workers[job_id] = worker
        events = self.manager.poll()
        self.assertTrue(worker.cancelled)
        self.assertEqual(self.manager.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0], "CANCELLING")
        topic = self.manager.db.execute("SELECT status,outcome,matched_id FROM auto_topics WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(topic["status"], "REJECTED")
        self.assertIn("script title duplicate", topic["outcome"])
        self.assertIsNotNone(topic["matched_id"])
        self.assertTrue(any("stopped before narration/render" in event.get("message", "") for event in events))
        worker.poll = lambda: [{"type": "job_cancelled", "message": "Cancelled after duplicate title."}]
        worker.running = False
        worker.process.returncode = 0
        self.manager.poll()
        terminal = self.manager.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        topic = self.manager.db.execute("SELECT status,outcome,matched_id FROM auto_topics WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(terminal, "CANCELLED")
        self.assertEqual(topic["status"], "REJECTED")
        self.assertIn("script title duplicate", topic["outcome"])
        self.assertIsNotNone(topic["matched_id"])

    def test_auto_upload_failure_counts_verified_video_once_and_pauses_without_replacement(self):
        upload_config = replace(self.config, youtube_upload_enabled=True, youtube_privacy_status="private")
        controller, _ = self.make_controller([[]])
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            controller.start("space science", 1, config=upload_config)
        result = self.manager.start_auto("A unique upload topic", controller.session_id,
                                         "A unique upload topic", "A distinct physical mechanism",
                                         config=upload_config, niche="space science")
        output = Path(next(row for row in self.manager.auto_topic_rows(controller.session_id)
                           if row["job_id"] == result["job_id"])["job_output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"verified finalized mp4")
        output.with_suffix(".metadata.json").write_text(json.dumps({
            "topic": "A unique upload topic", "script_payload": {"title": "A unique upload topic"}}))
        row = next(job for job in self.manager.list_jobs() if job["id"] == result["job_id"])
        stage_states = row["stage_states"]
        stage_states["save"] = "COMPLETED"
        stage_states["upload"] = "RUNNING"
        self.manager.db.execute("UPDATE jobs SET status='RUNNING',stage='upload',stage_states=? WHERE id=?",
                                (json.dumps(stage_states), result["job_id"]))
        self.manager.db.commit()

        class FakeProcess:
            pid = __import__("os").getpid()
            returncode = 1
            stdin = None

        class FakeWorker:
            process = FakeProcess()
            running = False

            def poll(self):
                return [{"type": "job_failed", "stage": "upload",
                         "message": f"ValueError: OAuth failed. Video was saved at {output}; upload failed.",
                         "output": str(output)}]

            def close(self):
                pass

        self.manager._workers[result["job_id"]] = FakeWorker()
        self.manager.poll()
        failed_job = next(job for job in self.manager.list_jobs() if job["id"] == result["job_id"])
        self.assertEqual(failed_job["status"], "FAILED")
        self.assertEqual(failed_job["video_finalized"], 1)
        auto_row = self.manager.auto_topic_rows(controller.session_id)[0]
        self.assertEqual(auto_row["output"], str(output))
        controller.tick()
        self.assertEqual(controller.state, "ERROR")
        self.assertEqual(controller.snapshot()["generated"], 1)
        self.assertEqual(controller.snapshot()["topics"][0]["job_status"], "FAILED")
        self.assertIn("OAuth failed", controller.message)
        self.assertEqual(self.manager.db.execute("SELECT COUNT(*) FROM jobs WHERE auto_session_id=?",
                                                 (controller.session_id,)).fetchone()[0], 1)
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            controller.resume(config=self.config)
        controller.tick()
        self.assertEqual(controller.state, "COMPLETED")
        self.assertEqual(self.manager.db.execute("SELECT COUNT(*) FROM jobs WHERE auto_session_id=?",
                                                 (controller.session_id,)).fetchone()[0], 1)

    def test_restart_during_auto_upload_keeps_video_and_pauses_for_delivery_error(self):
        upload_config = replace(self.config, youtube_upload_enabled=True, youtube_privacy_status="unlisted")
        controller, _ = self.make_controller([[]])
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            controller.start("space science", 1, config=upload_config)
        result = self.manager.start_auto("A recovery upload topic", controller.session_id,
                                         "A recovery upload topic", "A distinct orbital mechanism",
                                         config=upload_config, niche="space science")
        row = next(item for item in self.manager.auto_topic_rows(controller.session_id)
                   if item["job_id"] == result["job_id"])
        output = Path(row["job_output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"verified saved video")
        output.with_suffix(".metadata.json").write_text(json.dumps({
            "topic": row["title"], "script_payload": {"title": row["title"]}}))
        job = next(item for item in self.manager.list_jobs() if item["id"] == result["job_id"])
        states = job["stage_states"]
        states["save"] = "COMPLETED"
        states["upload"] = "RUNNING"
        self.manager.db.execute("UPDATE jobs SET status='RUNNING',stage='upload',stage_states=? WHERE id=?",
                                (json.dumps(states), result["job_id"]))
        self.manager.db.commit()
        old_session = controller.session_id
        controller.close()
        self.manager.close()
        resumed_manager = JobManager(
            self.root / "logs", db_path=self.root / "jobs.sqlite3",
            limits=ResourceLimits(max_active_jobs=1, system_headroom_gib=0),
            memory_available=lambda: 0,
        )
        self.addCleanup(resumed_manager.close)
        self.manager = resumed_manager
        resumed = AutoMode(resumed_manager, generator_factory=lambda config: FakeGenerator(config, []),
                           settings=self.settings)
        self.addCleanup(resumed.close)
        self.assertEqual(resumed.state, "PAUSED")
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            resumed.resume(config=self.config)
        self.assertEqual(tuple(resumed_manager.db.execute("SELECT status,video_finalized FROM jobs WHERE id=?",
                                                          (result["job_id"],)).fetchone()), ("FAILED", 1))
        resumed.tick()
        self.assertEqual(resumed.state, "ERROR")
        self.assertEqual(resumed.snapshot()["generated"], 1)
        self.assertIn("upload was interrupted", resumed.message)
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            resumed.resume(config=self.config)
        resumed.tick()
        self.assertEqual(resumed.state, "COMPLETED")
        self.assertEqual(resumed.session_id, old_session)
        self.assertTrue(output.is_file())
        self.assertEqual(resumed_manager.db.execute("SELECT COUNT(*) FROM jobs WHERE auto_session_id=?",
                                                    (old_session,)).fetchone()[0], 1)

    def test_repeated_final_script_title_rejections_stop_and_remain_classified(self):
        self.manager.start("Why light bends around stars", config=self.config)
        controller, _ = self.make_controller([[]])
        controller.settings = replace(self.settings, duplicate_batches_before_error=2)
        self.begin(controller, target=None)
        for index, proposed in enumerate(("Solar flare physics", "Lunar crater ejecta")):
            result = self.manager.start_auto(proposed, controller.session_id, proposed,
                                             f"Unique mechanism {index}", config=self.config,
                                             niche="space science")
            self.assertTrue(result["accepted"])
            final_title = ("How starlight warps near stars" if index == 0
                           else "Why light bends near a star")
            matched = self.manager.history.find_duplicate(final_title, f"Unique mechanism {index}")
            self.assertIsNotNone(matched)
            self.manager.history.reject_final_title(result["topic_id"], final_title, matched)
            self.manager.db.execute("UPDATE jobs SET status='CANCELLED' WHERE id=?", (result["job_id"],))
            self.manager.db.commit()
        controller.tick()
        session = self.manager.auto_session(controller.session_id)
        self.assertEqual(controller.state, "ERROR")
        self.assertEqual(session["consecutive_rejections"], 2)
        topics = self.manager.auto_topic_rows(controller.session_id)
        self.assertEqual(sum(row["status"] == "REJECTED" and row["accounted"] for row in topics), 2)
        self.assertEqual(sum(event["kind"] == "final_title_rejection_accounted"
                             for event in self.manager.auto_events(controller.session_id)), 2)

    async def test_nested_create_mode_shortcuts_and_validation(self):
        app = SlopEngineApp(settings_store=SettingsStore(self.root / "preferences.json"), jobs=self.manager)
        async with app.run_test(size=(80, 28)) as pilot:
            await pilot.press("ctrl+8")
            self.assertEqual(app.query_one("#pages", TabbedContent).active, "create")
            self.assertEqual(app.create_mode(), "auto")
            niche = app.query_one("#auto-niche", Input)
            await pilot.click("#auto-niche")
            self.assertIs(app.focused, niche)
            await pilot.press("s", "p", "a", "c", "e", "space", "s", "c", "i", "e", "n", "c", "e")
            self.assertEqual(niche.value, "space science")
            count = app.query_one("#auto-count", Input)
            await pilot.click("#auto-count")
            self.assertIs(app.focused, count)
            await pilot.press("0")
            self.assertEqual(count.value, "0")
            await pilot.press("ctrl+g")
            self.assertIn("positive whole number", str(app.query_one("#auto-form-error", Static).render()))
            self.assertEqual(self.manager.auto_sessions(), [])
            await pilot.press("ctrl+2")
            self.assertEqual(app.query_one("#pages", TabbedContent).active, "create")
            self.assertEqual(app.create_mode(), "manual")
            output = app.query_one("#output", Input)
            await pilot.click("#output")
            self.assertIs(app.focused, output)
            await pilot.press("ctrl+a")
            await pilot.press(*tuple("visible.mp4"))
            self.assertEqual(output.value, "visible.mp4")
            await pilot.press("ctrl+1")
            self.assertEqual(app.query_one("#pages", TabbedContent).active, "dashboard")
            await pilot.press("ctrl+5")
            self.assertEqual(app.focused, app.query_one("#log-select", Select))

    async def test_auto_create_ignores_manual_topic_output_and_uses_shared_upload_config(self):
        app = SlopEngineApp(settings_store=SettingsStore(self.root / "preferences.json"), jobs=self.manager)
        captured = {}
        app.auto_mode.start = lambda niche, target, config: captured.update(
            niche=niche, target=target, config=config) or "fake-session"
        async with app.run_test(size=(80, 28)):
            app.query_one("#create-mode", Select).value = "auto"
            app.query_one("#auto-niche", Input).value = "space science"
            app.query_one("#auto-count", Input).value = "3"
            app.query_one("#topic", TextArea).load_text("")
            app.query_one("#output", Input).value = "invalid-output.txt"
            app.query_one("#job-edge_voice", Input).value = "en-US-GuyNeural"
            app.query_one("#upload", Checkbox).value = True
            with patch("slopengine.app.validate_config", return_value=[]), \
                 patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
                 patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
                await app.action_auto_start()
            self.assertEqual(captured["niche"], "space science")
            self.assertEqual(captured["target"], 3)
            self.assertTrue(captured["config"].youtube_upload_enabled)
            self.assertEqual(captured["config"].edge_voice, "en-US-GuyNeural")
            self.assertFalse(app.query_one("#upload", Checkbox).value)

    async def test_upload_checkbox_updates_shared_manual_and_auto_summaries(self):
        store = SettingsStore(self.root / "preferences.json")
        store.save({"youtube_privacy_status": "unlisted"})
        app = SlopEngineApp(settings_store=store, jobs=self.manager)
        async with app.run_test(size=(80, 28)) as pilot:
            await pilot.press("ctrl+2")
            checkbox = app.query_one("#upload", Checkbox)
            checkbox.focus()
            await pilot.press("space")
            self.assertTrue(checkbox.value)
            self.assertIn("UPLOAD REQUESTED · unlisted", str(app.query_one("#summary", Static).render()))
            await pilot.press("ctrl+8")
            self.assertEqual(app.create_mode(), "auto")
            self.assertIn("UPLOAD REQUESTED · unlisted", str(app.query_one("#summary", Static).render()))
            checkbox.focus()
            await pilot.press("space")
            self.assertFalse(checkbox.value)
            self.assertIn("local only", str(app.query_one("#summary", Static).render()))

    async def test_resume_uses_current_request_limits_and_rebuilds_recent_request_tail(self):
        first, _ = self.make_controller([[]])
        self.begin(first, target=None)
        first.pause()
        first.tick()
        now = datetime.now(timezone.utc).isoformat()
        with self.manager._lock:
            self.manager.db.execute(
                "INSERT INTO auto_events(session_id,time,level,kind,message) VALUES(?,?,?,?,?)",
                (first.session_id, now, "INFO", "request", "Gemini topic discovery request 1"),
            )
            self.manager.db.executemany(
                "INSERT INTO auto_events(session_id,time,level,kind,message) VALUES(?,?,?,?,?)",
                [(first.session_id, now, "INFO", "progress", f"Progress event {index}") for index in range(1001)],
            )
            self.manager.db.execute("UPDATE auto_sessions SET request_count=1 WHERE id=?", (first.session_id,))
            self.manager.db.commit()
        current = replace(self.settings, request_budget=2, requests_per_minute=2)
        resumed = AutoMode(self.manager, generator_factory=lambda config: FakeGenerator(config, []), settings=current)
        self.addCleanup(resumed.close)
        self.assertEqual(resumed.state, "PAUSED")
        with patch("slopengine.auto_mode.shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("slopengine.auto_mode.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GiB)):
            resumed.resume(config=self.config)
        self.assertEqual(resumed.settings.request_budget, 2)
        self.assertEqual(len(resumed._request_times), 1)
        self.assertTrue(resumed._request_permit(resumed._epoch))
        with self.assertRaises(AutoBudgetError):
            resumed._request_permit(resumed._epoch)


class TopicHistoryTests(unittest.TestCase):
    def test_normalization_semantics_and_distinct_actions(self):
        self.assertTrue(compare_topics("ＦＯＲＭ—A HOLE", "", "form a hole", "")[0])
        self.assertTrue(compare_topics("Why light bends around stars", "", "How starlight warps near stars", "Gravity curves its path.")[0])
        self.assertTrue(compare_topics("Why sleep improves memory", "", "How sleeping boosts recall", "")[0])
        self.assertTrue(compare_topics("Why metals rust in salty air", "", "How iron corrodes near the ocean", "")[0])
        self.assertFalse(compare_topics("How black holes form", "", "How black holes merge", "")[0])
        self.assertEqual(fingerprint("Why LIGHT bends!"), fingerprint("Why light bends"))

    def test_history_keeps_proposed_title_when_final_title_changes(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        history = TopicHistory(connection)
        history.record_manual("How light bends around stars", "m1")
        connection.commit()
        duplicate = history.find_duplicate("Why starlight warps around a star", "Gravity curves light.")
        self.assertIsNotNone(duplicate)
        connection.execute("INSERT INTO auto_topics (id,session_id,niche,title,proposed_title,angle,fingerprint,status,source,job_id,created,updated) VALUES ('auto1','s','space','Final title','Proposed spacecraft topic','fresh orbital angle','hash','COMPLETED','auto','j1','1','1')")
        connection.commit()
        history.update_job("j1", "COMPLETED", title="Unrelated final title")
        self.assertIsNotNone(history.find_duplicate("Proposed spacecraft topic", "fresh orbital angle"))

    def test_legacy_output_import_uses_mp4_path_and_never_needs_video(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "old.metadata.json").write_text(json.dumps({"topic": "Old topic", "script_payload": {"title": "Old title"}}))
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            history = TopicHistory(connection)
            self.assertEqual(history.import_outputs(root), 1)
            row = connection.execute("SELECT output FROM auto_topics").fetchone()
            self.assertEqual(row["output"], str((root / "old.mp4").resolve()))

    def test_topic_generator_structured_json_validation_is_offline(self):
        class FakeModels:
            def __init__(self, response):
                self.response = response
                self.calls = 0

            def generate_content(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(text=self.response)

        class FakeTypes:
            @staticmethod
            def GenerateContentConfig(**kwargs):
                return kwargs

        generator = TopicGenerator(PipelineConfig(gemini_api_key="test-key", pexels_api_key="test"))
        models = FakeModels('{"topics":[{"title":"A concise topic","angle":"A concrete mechanism."}]}')
        generator._client = SimpleNamespace(models=models)
        generator._types = FakeTypes
        permits = []
        result = generator.generate("science", 1, accepted=[], rejected=[], completed=[],
                                    permit_request=lambda delay: permits.append(delay) or True)
        self.assertEqual(result, [{"title": "A concise topic", "angle": "A concrete mechanism."}])
        self.assertEqual(models.calls, 1)
        self.assertEqual(len(permits), 1)
        generator._client.models.response = '{"topics":[]}'
        with self.assertRaisesRegex(AutoModeError, "empty or malformed"):
            generator.generate("science", 1, accepted=[], rejected=[], completed=[], permit_request=lambda delay: True)

    def test_topic_generator_429_retries_once_per_metered_call(self):
        class RateLimit(Exception):
            status_code = 429

        class FakeModels:
            def __init__(self):
                self.responses = [RateLimit("slow down"), SimpleNamespace(text='{"topics":[{"title":"Topic","angle":"Angle"}]}')]
                self.calls = 0

            def generate_content(self, **kwargs):
                self.calls += 1
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        class FakeTypes:
            @staticmethod
            def GenerateContentConfig(**kwargs):
                return kwargs

        generator = TopicGenerator(PipelineConfig(gemini_api_key="test-key", pexels_api_key="test"))
        models = FakeModels()
        generator._client = SimpleNamespace(models=models)
        generator._types = FakeTypes
        delays = []
        with patch("slopengine.auto_mode.random.uniform", return_value=0):
            result = generator.generate("science", 1, accepted=[], rejected=[], completed=[],
                                        permit_request=lambda delay: delays.append(delay) or True)
        self.assertEqual(result[0]["title"], "Topic")
        self.assertEqual(models.calls, 2)
        self.assertEqual(delays, [0.0, 0.0])

    def test_topic_generator_falls_back_after_missing_model_and_counts_lookup(self):
        class Missing(Exception):
            status_code = 404

        class FakeModels:
            def __init__(self):
                self.calls = []

            def generate_content(self, **kwargs):
                self.calls.append(kwargs["model"])
                if kwargs["model"] == "bad-model":
                    raise Missing("not found")
                return SimpleNamespace(text='{"topics":[{"title":"Fallback topic","angle":"A mechanism."}]}')

            def list(self):
                return [SimpleNamespace(name="gemini-2.5-flash", supported_actions=["generateContent"])]

        class FakeTypes:
            @staticmethod
            def GenerateContentConfig(**kwargs):
                return kwargs

        config = PipelineConfig(gemini_api_key="test-key", pexels_api_key="test", gemini_model="bad-model")
        generator = TopicGenerator(config)
        models = FakeModels()
        generator._client = SimpleNamespace(models=models)
        generator._types = FakeTypes
        permits = []
        result = generator.generate("science", 1, accepted=[], rejected=[], completed=[],
                                    permit_request=lambda delay: permits.append(delay) or True)
        self.assertEqual(result[0]["title"], "Fallback topic")
        self.assertEqual(models.calls, ["bad-model", "gemini-2.5-flash"])
        self.assertEqual(len(permits), 3)

    def test_topic_transport_sets_real_deadline_and_disables_hidden_retries(self):
        probe = r'''import json
from unittest.mock import patch
from slopengine.auto_mode import TopicGenerator
from slopengine.config_core import PipelineConfig
config = PipelineConfig(gemini_api_key="local-test-key", pexels_api_key="test", gemini_timeout_seconds=17)
generator = TopicGenerator(config)
with patch("google.genai.Client") as client:
    generator._client_for_config()
options = client.call_args.kwargs["http_options"]
print(json.dumps({"timeout": options.timeout, "attempts": options.retry_options.attempts}))
'''
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1])
        if result.returncode and "No module named 'google.genai'" in result.stderr:
            self.skipTest("google-genai is not installed")
        self.assertEqual(result.returncode, 0, result.stderr)
        options = json.loads(result.stdout)
        self.assertEqual(options["timeout"], 17000)
        self.assertEqual(options["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
