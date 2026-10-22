"""Offline safety checks; no media imports or real renders required."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.benchmark_paired_render import load_manifest, main, run_pair, stop_groups


class PairedBenchmarkTests(unittest.TestCase):
    def test_nonfinite_memory_limits_are_rejected(self):
        for value in ('nan', 'inf'):
            with patch('scripts.benchmark_paired_render.sys.argv',
                       ['benchmark', '--manifest', 'manifest.json', '--run', '--headroom-gib', value]), \
                 patch('scripts.benchmark_paired_render.load_manifest', return_value={}), \
                 patch('sys.stderr'):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)

    def test_manifest_resolves_assets_and_rejects_network_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'audio.wav').touch()
            (directory / 'footage.mp4').touch()
            video = dict(audio='audio.wav', backgrounds=['footage.mp4'],
                         word_events=[dict(start_ms=0, end_ms=100, text='hello')])
            manifest = directory / 'manifest.json'
            data = dict(videos=[video, video], config={'width': 1080})
            manifest.write_text(json.dumps(data))
            loaded = load_manifest(manifest)
            self.assertEqual(loaded['videos'][0]['audio'], str(directory / 'audio.wav'))
            data['config']['youtube_upload_enabled'] = True
            manifest.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                load_manifest(manifest)

    def test_low_memory_does_not_launch_worker(self):
        psutil = Mock()
        psutil.virtual_memory.return_value.available = 8 * 2**30
        args = SimpleNamespace(headroom_gib=2, render_gib=6.5, max_combined_gib=14)
        with patch('scripts.benchmark_paired_render.subprocess.Popen') as popen:
            result = run_pair('concurrent', {}, Path('manifest'), Path('/tmp'), args, psutil)
        self.assertIn('Insufficient', result['aborted'])
        self.assertEqual(result['videos'], [])
        popen.assert_not_called()

    def test_cleanup_targets_only_owned_groups_even_after_parent_exit(self):
        process = Mock(pid=45678)
        process.poll.return_value = 0
        with patch('scripts.benchmark_paired_render.os.killpg') as killpg:
            stop_groups([process])
        self.assertEqual([call.args[0] for call in killpg.call_args_list], [45678, 45678])
        process.wait.assert_called_once()

    def test_memory_pressure_after_admission_aborts_before_launch(self):
        psutil = Mock()
        psutil.virtual_memory.side_effect = [SimpleNamespace(available=20 * 2**30),
                                            SimpleNamespace(available=1 * 2**30)]
        psutil.swap_memory.return_value = SimpleNamespace(sin=0, sout=0)
        args = SimpleNamespace(headroom_gib=2, render_gib=6.5, max_combined_gib=14)
        with tempfile.TemporaryDirectory() as temporary:
            with patch('scripts.benchmark_paired_render.subprocess.Popen') as popen:
                result = run_pair('concurrent', {}, Path('manifest'), Path(temporary), args, psutil)
        self.assertEqual(result['aborted'], 'Memory safety limit reached')
        self.assertEqual(result['minimum_available_bytes'], 2**30)
        popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
