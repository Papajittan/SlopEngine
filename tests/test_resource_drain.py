from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from slopengine.config_core import PipelineConfig
from slopengine.jobs import GiB, JobManager, ResourceLimits, ResourceManager


class ResourceDrainTests(unittest.TestCase):
    def test_light_stage_drains_behind_older_memory_waiter(self):
        manager = object.__new__(JobManager)
        manager.limits = ResourceLimits()
        manager.resources = ResourceManager(manager.limits, lambda: 9 * GiB)
        manager.resources.leases = {'A': ('narration', int(1.5 * GiB), False),
                                    'B': ('render', 7 * GiB, False)}
        manager._usage = lambda: {'A': GiB, 'B': GiB}
        rows = [dict(id='A', status='WAITING', stage='timestamps', config={},
                     waiting_reason='Waiting for resource admission', stage_states={'timestamps': 'WAITING'}),
                dict(id='B', status='WAITING', stage='save', config={},
                     waiting_reason='Waiting for resource admission', stage_states={'save': 'WAITING'})]
        manager.list_jobs = lambda: rows
        manager._workers = {key: SimpleNamespace(process=SimpleNamespace(stdin=StringIO())) for key in ('A', 'B')}
        manager._update = lambda key, **values: next(row for row in rows if row['id'] == key).update(values)
        manager._schedule()
        self.assertEqual(rows[0]['waiting_reason'], 'Waiting for available memory')
        self.assertEqual(rows[1]['status'], 'RUNNING')
        self.assertEqual(manager._workers['B'].process.stdin.getvalue(), '{"permit": "save"}\n')
        manager._schedule()
        self.assertEqual(rows[0]['status'], 'RUNNING')

    def test_reserved_save_can_finish_under_pressure_but_heavy_stages_cannot(self):
        resources = ResourceManager(ResourceLimits(), lambda: GiB)
        resources.leases['A'] = ('idle', 7 * GiB, False)
        self.assertEqual(resources.reserve('A', 'save', {}, {'A': GiB}, allow_drain=True), '')
        for stage in ('render', 'timestamps'):
            resources.leases['A'] = ('idle', 7 * GiB, False)
            self.assertEqual(resources.reserve('A', stage, {}, {'A': GiB}, allow_drain=True),
                             'Waiting for available memory')

    def test_completed_render_releases_slot_and_peak_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = JobManager(root / 'logs', memory_available=lambda: 0)
            try:
                job = manager.start('topic', config=PipelineConfig(
                    gemini_api_key='', pexels_api_key='', output_dir=root / 'outputs', temp_root=root / 'temp'))
                manager._update(job, status='RUNNING', stage='render')
                worker = Mock()
                worker.running = True
                worker.poll.return_value = [{'type': 'stage_completed', 'stage': 'render'}]
                manager._workers[job] = worker
                manager.resources.leases[job] = ('render', 7 * GiB, False)
                with patch.object(manager, '_usage', return_value={job: 2 * GiB}), patch.object(manager, '_schedule'):
                    manager.poll()
                self.assertEqual(manager.resources.leases[job], ('idle', 2 * GiB, False))
            finally:
                manager.close()

    def test_upload_token_mutation_is_serialized(self):
        resources = ResourceManager(ResourceLimits(), lambda: 64 * GiB)
        self.assertEqual(resources.reserve('A', 'upload', {}, {}), '')
        resources.leases['B'] = ('idle', 3 * GiB, False)
        self.assertEqual(resources.reserve('B', 'upload', {}, {}, allow_drain=True), 'Waiting for upload slot')
        resources.leases['A'] = ('idle', 2 * GiB, False)
        self.assertEqual(resources.reserve('B', 'upload', {}, {}, allow_drain=True), '')
