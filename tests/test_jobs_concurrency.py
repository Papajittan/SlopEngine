"""Scheduler integration uses independent OS processes, without media or APIs."""
from contextlib import closing
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from slopengine.config_core import PipelineConfig
from slopengine.jobs import GiB, JobManager, ResourceLimits, ResourceManager
from slopengine.processes import WorkerProcess, read_logs


FAKE_WORKER = r"""
import json, pathlib, sys, time
payload = json.loads(sys.stdin.readline())
job_id = payload['job_id']
root = pathlib.Path(sys.argv[1])
def emit(kind, **fields):
    print(json.dumps({'type': kind, **fields}), flush=True)
if payload['topic'] == 'crash':
    sys.exit(7)
stages = ['timestamps'] if payload['topic'] == 'gpu' else ['script', 'render']
for stage in stages:
    emit('stage_requested', stage=stage)
    permit = json.loads(sys.stdin.readline())
    assert permit == {'permit': stage}
    emit('stage_started', stage=stage)
    emit('log_message', message=payload['topic'] + ':' + stage)
    while not (root / (job_id + '.' + stage)).exists():
        time.sleep(.01)
    emit('stage_completed', stage=stage)
if payload['topic'] == 'false-completion':
    emit('job_completed', output=payload['output'])
    sys.exit(0)
pathlib.Path(payload['output']).parent.mkdir(parents=True, exist_ok=True)
pathlib.Path(payload['output']).write_bytes(b'fake video')
pathlib.Path(payload['output']).with_suffix('.metadata.json').write_text('{}')
emit('job_completed', output=payload['output'])
if payload['topic'] == 'bad-exit':
    sys.exit(7)
"""


@unittest.skipUnless(os.name == 'posix', 'Independent process groups require POSIX')
class SchedulerProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = PipelineConfig(gemini_api_key='test-secret', pexels_api_key='',
                                     output_dir=self.root / 'outputs', temp_root=self.root / 'temp')
        real_popen = subprocess.Popen
        root = self.root

        class FakeWorker(WorkerProcess):
            def start(self, *args, **kwargs):
                def spawn(command, **options):
                    return real_popen([sys.executable, '-u', '-c', FAKE_WORKER, str(root)], **options)
                with patch('slopengine.processes.subprocess.Popen', side_effect=spawn):
                    return super().start(*args, **kwargs)

        self.factory = FakeWorker
        self.memory = 64 * GiB
        self.manager = self.make_manager()
        self.events = []

    def make_manager(self, **kwargs):
        manager = JobManager(self.root / 'logs', db_path=self.root / 'queue.sqlite3',
                             memory_available=lambda: self.memory, worker_factory=self.factory, **kwargs)
        self.addCleanup(manager.close)
        return manager

    def enqueue(self, topic='topic', **kwargs):
        return self.manager.start(topic, config=kwargs.pop('config', self.config), **kwargs)

    def row(self, job_id):
        return next(row for row in self.manager.list_jobs() if row['id'] == job_id)

    def until(self, predicate):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            self.events.extend(self.manager.poll())
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out: ' + repr([(r['topic'], r['status'], r['stage'], r['waiting_reason'])
                                      for r in self.manager.list_jobs()]))

    def at(self, job_id, stage, status='RUNNING'):
        return self.row(job_id)['stage'] == stage and self.row(job_id)['status'] == status

    def release(self, job_id, stage):
        (self.root / (job_id + '.' + stage)).touch()

    def test_two_processes_progress_with_one_render_slot_and_resume(self):
        first, second = self.enqueue('first'), self.enqueue('second')
        self.until(lambda: self.at(first, 'script') and self.at(second, 'script'))
        self.assertNotEqual(self.row(first)['pid'], self.row(second)['pid'])
        self.assertNotEqual(self.row(first)['log_path'], self.row(second)['log_path'])
        self.release(first, 'script')
        self.until(lambda: self.at(first, 'render'))
        # Script work in the other process continues while rendering is active.
        self.assertTrue(self.at(second, 'script'))
        self.release(second, 'script')
        self.until(lambda: self.at(second, 'render', 'WAITING'))
        self.assertEqual(self.row(second)['waiting_reason'], 'Waiting for render slot')
        self.assertEqual(sum(lease[0] == 'render' for lease in self.manager.resources.leases.values()), 1)
        self.release(first, 'render')
        self.until(lambda: self.row(first)['status'] == 'COMPLETED' and self.at(second, 'render'))
        self.release(second, 'render')
        self.until(lambda: not self.manager.running)
        self.assertEqual(self.row(second)['status'], 'COMPLETED')
        for job_id, topic in ((first, 'first'), (second, 'second')):
            messages = [event['message'] for event in self.events if event['job_id'] == job_id
                        and event['type'] == 'log_message']
            self.assertEqual(messages, [topic + ':script', topic + ':render'])
            self.assertEqual([e['message'] for e in read_logs(Path(self.row(job_id)['log_path']))
                              if e['type'] == 'log_message'], messages)

    def test_real_managed_worker_waits_for_permits(self):
        source = r"""
import pathlib, sys, types
from slopengine import worker
class Pipeline:
    def __init__(self, config, event_handler, stage_gate, staging_path):
        self.emit, self.gate = event_handler, stage_gate
        assert staging_path.name.startswith('.slopengine-')
    def run(self, topic, output):
        for stage in ('script', 'render'):
            self.gate(stage)
            self.emit({'type': 'stage_started', 'stage': stage})
            self.emit({'type': 'stage_completed', 'stage': stage})
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'video')
        output.with_suffix('.metadata.json').write_text('{}')
        return output
sys.modules['shorts_core'] = types.SimpleNamespace(ShortsPipeline=Pipeline, upload_rendered_short=None)
worker.validate_config = lambda *args, **kwargs: []
sys.exit(worker.main())
"""
        real_popen = subprocess.Popen
        class ManagedWorker(WorkerProcess):
            def start(self, *args, **kwargs):
                with patch('slopengine.processes.subprocess.Popen',
                           side_effect=lambda command, **options: real_popen(
                               [sys.executable, '-u', '-c', source], **options)):
                    return super().start(*args, **kwargs)
        self.manager._factory = ManagedWorker
        job = self.enqueue()
        self.until(lambda: self.row(job)['status'] == 'COMPLETED')
        requested = [event['stage'] for event in self.events
                     if event['job_id'] == job and event['type'] == 'stage_requested']
        self.assertEqual(requested, ['script', 'render'])
        self.assertEqual(self.row(job)['stage_states']['render'], 'COMPLETED')

    def test_managed_worker_parent_pipe_death_cancels_and_reaps_descendant(self):
        import psutil
        import select
        source = r"""
import subprocess, sys, time, types
from slopengine import worker
class Pipeline:
    def __init__(self, config, event_handler, stage_gate, staging_path):
        self.emit, self.gate = event_handler, stage_gate
    def run(self, topic, output):
        self.gate('render')
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.emit({'type': 'stage_started', 'stage': 'render', 'child_pid': child.pid})
        while True:
            time.sleep(.01)
sys.modules['shorts_core'] = types.SimpleNamespace(ShortsPipeline=Pipeline, upload_rendered_short=None)
worker.validate_config = lambda *args, **kwargs: []
sys.exit(worker.main())
"""
        process = subprocess.Popen([sys.executable, '-u', '-c', source], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        def cleanup():
            if process.poll() is None:
                os.killpg(process.pid, 9)
                process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
        self.addCleanup(cleanup)
        payload = {'managed': True, 'job_id': 'parent-death-test', 'topic': 'topic',
                   'output': str(self.root / 'video.mp4'), 'config': asdict(self.config)}
        process.stdin.write(json.dumps(payload, default=str) + '\n')
        process.stdin.flush()
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'No stage request')
        self.assertEqual(json.loads(process.stdout.readline())['type'], 'stage_requested')
        process.stdin.write(json.dumps({'permit': 'render'}) + '\n')
        process.stdin.flush()
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'Stage did not start')
        started = json.loads(process.stdout.readline())
        self.assertEqual(started['type'], 'stage_started')
        # Closing the manager control pipe simulates unexpected parent shutdown.
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 130)
        terminal = [json.loads(line) for line in process.stdout.read().splitlines()]
        self.assertEqual(terminal[-1]['type'], 'job_cancelled')
        self.assertFalse(psutil.pid_exists(started['child_pid']))

    def test_memory_wait_resumes_without_killing_active_worker(self):
        job = self.enqueue()
        self.until(lambda: self.at(job, 'script'))
        pid = self.row(job)['pid']
        self.memory = 4 * GiB
        self.release(job, 'script')
        self.until(lambda: self.at(job, 'render', 'WAITING'))
        self.assertEqual(self.row(job)['waiting_reason'], 'Waiting for available memory')
        self.assertEqual(self.row(job)['pid'], pid)
        self.memory = 64 * GiB
        self.until(lambda: self.at(job, 'render'))
        self.release(job, 'render')
        self.until(lambda: self.row(job)['status'] == 'COMPLETED')

    def test_cancellation_only_reaps_selected_job(self):
        first, second = self.enqueue('first'), self.enqueue('second')
        self.until(lambda: self.at(first, 'script') and self.at(second, 'script'))
        second_pid = self.row(second)['pid']
        self.manager.cancel(first)
        self.until(lambda: self.row(first)['status'] == 'CANCELLED')
        self.assertEqual(self.row(second)['pid'], second_pid)
        self.assertTrue(self.at(second, 'script'))
        self.release(second, 'script')
        self.release(second, 'render')
        self.until(lambda: self.row(second)['status'] == 'COMPLETED')

    def test_crash_isolated_from_other_worker(self):
        failed, healthy = self.enqueue('crash'), self.enqueue('healthy')
        self.until(lambda: self.row(failed)['status'] == 'FAILED' and self.at(healthy, 'script'))
        self.assertTrue(any(e['job_id'] == failed and e['type'] == 'job_failed' for e in self.events))
        self.release(healthy, 'script')
        self.release(healthy, 'render')
        self.until(lambda: self.row(healthy)['status'] == 'COMPLETED')

    def test_gpu_admission_independent_of_render_limit(self):
        config = replace(self.config, whisper_device='cuda')
        first, second = self.enqueue('gpu', config=config), self.enqueue('gpu', config=config)
        self.until(lambda: self.at(first, 'timestamps') and self.at(second, 'timestamps', 'WAITING'))
        self.assertEqual(self.row(second)['waiting_reason'], 'Waiting for GPU resource')
        self.release(first, 'timestamps')
        self.until(lambda: self.at(second, 'timestamps'))
        self.release(second, 'timestamps')
        self.until(lambda: not self.manager.running)

    def test_unique_outputs_snapshot_and_reserved_destination(self):
        first, second = self.enqueue(), self.enqueue()
        row = self.row(first)
        self.assertNotEqual(row['output'], self.row(second)['output'])
        self.assertNotEqual(row['temp_dir'], self.row(second)['temp_dir'])
        self.assertNotIn('gemini_api_key', row['config'])
        self.assertNotIn('test-secret', self.manager.db.execute('SELECT config FROM jobs WHERE id=?', (first,)).fetchone()[0])
        row['config']['fps'] = 99
        self.assertEqual(self.row(first)['config']['fps'], self.config.fps)
        with self.assertRaisesRegex(ValueError, 'reserved'):
            self.enqueue(output=row['output'])

    def test_restart_preserves_pending_and_never_restarts_interrupted(self):
        self.manager.configure(max_active_jobs=1)
        active, pending = self.enqueue('active'), self.enqueue('pending')
        self.until(lambda: self.at(active, 'script'))
        self.manager.close()
        self.manager = self.make_manager()
        self.assertEqual(self.row(active)['status'], 'INTERRUPTED')
        self.assertEqual(self.row(active)['stage_states']['script'], 'INTERRUPTED')
        self.assertEqual(self.row(pending)['status'], 'HELD')
        self.assertEqual(self.manager._workers, {})
        self.manager.poll()
        self.assertEqual(self.manager._workers, {})
        with patch('slopengine.jobs.build_config', return_value=self.config):
            recovered = self.manager.retry(pending)
        self.until(lambda: self.at(recovered, 'script'))
        self.assertNotIn(active, self.manager._workers)
        self.assertNotIn(pending, self.manager._workers)
        self.assertEqual(self.manager.limits.max_active_jobs, 1)

    def test_false_completion_and_nonzero_exit_never_mark_completed(self):
        for topic in ('false-completion', 'bad-exit'):
            with self.subTest(topic=topic):
                job = self.enqueue(topic)
                self.release(job, 'script')
                self.release(job, 'render')
                self.until(lambda: self.row(job)['status'] == 'FAILED')
                self.assertFalse(any(event['type'] == 'job_completed' and event['job_id'] == job
                                     for event in self.events))
                self.assertTrue(any(event['type'] == 'job_failed' and event['job_id'] == job
                                    for event in self.events))

    def test_recovery_cleans_abandoned_job_temp_and_staging(self):
        import sqlite3
        job = self.enqueue()
        self.until(lambda: self.at(job, 'script'))
        row = self.row(job)
        self.manager.close()
        temp = Path(row['temp_dir'])
        staging = Path(row['output']).parent / ('.slopengine-' + job)
        for folder in (temp, staging):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'incomplete').touch()
        with closing(sqlite3.connect(self.root / 'queue.sqlite3')) as db:
            db.execute("UPDATE jobs SET status='RUNNING', pid=NULL WHERE id=?", (job,))
            db.commit()
        self.manager = self.make_manager()
        self.assertEqual(self.row(job)['status'], 'INTERRUPTED')
        self.assertFalse(temp.exists())
        self.assertFalse(staging.exists())
        self.assertFalse(Path(row['output']).exists())

    def test_exclusive_queue_owner(self):
        with self.assertRaisesRegex(RuntimeError, 'already managed'):
            self.make_manager()

    def test_retry_gets_new_id_output_and_upload_consent_disabled(self):
        failed = self.enqueue('crash', config=replace(self.config, youtube_upload_enabled=True))
        self.until(lambda: self.row(failed)['status'] == 'FAILED')
        with patch('slopengine.jobs.build_config', return_value=self.config):
            retried = self.manager.retry(failed)
        self.assertNotEqual(retried, failed)
        self.assertNotEqual(self.row(retried)['output'], self.row(failed)['output'])
        self.assertFalse(self.row(retried)['config']['youtube_upload_enabled'])

    def test_reorder_queued_jobs_and_cancel_before_start(self):
        self.manager.configure(max_active_jobs=1)
        active, second, third = self.enqueue('active'), self.enqueue('second'), self.enqueue('third')
        self.manager.reorder(third, -1)
        self.assertEqual([r['id'] for r in self.manager.list_jobs()], [active, third, second])
        self.manager.cancel(third)
        self.assertEqual(self.row(third)['status'], 'CANCELLED')
        self.assertIsNone(self.row(third)['pid'])


class ResourceAdmissionTests(unittest.TestCase):
    def test_reservations_prevent_simultaneous_over_admission(self):
        resources = ResourceManager(ResourceLimits(max_concurrent_renders=2), lambda: 16 * GiB)
        self.assertEqual(resources.reserve('a', 'render', {}, {}), '')
        self.assertEqual(resources.reserve('b', 'render', {}, {}), 'Waiting for available memory')
        self.assertNotIn('b', resources.leases)

    def test_rss_already_deducted_from_available_is_not_double_charged(self):
        resources = ResourceManager(ResourceLimits(max_concurrent_renders=2), lambda: 12 * GiB)
        resources.leases['a'] = ('render', 7 * GiB, False)
        self.assertEqual(resources.reserve('b', 'render', {}, {'a': 6 * GiB}), '')

    def test_memory_requirements_vary_by_resolution_and_whisper_model(self):
        resources = ResourceManager(ResourceLimits(), lambda: 64 * GiB)
        self.assertEqual(resources.requirement('render', {'width': 2160, 'height': 3840}), 28 * GiB)
        self.assertEqual(resources.requirement('timestamps', {'whisper_model': 'large-v3'}), 12 * GiB)

    def test_invalid_limits_rejected(self):
        for changes in ({'max_active_jobs': 0}, {'max_concurrent_renders': True},
                        {'system_headroom_gib': float('nan')}, {'stage_memory_gib': {'render': 7}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ResourceLimits(**changes)
