"""Small regression checks for opaque background and font reuse decisions."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_env_loading import import_shorts_core_with_fakes


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.core = import_shorts_core_with_fakes()
        self.compiler = self.core.MoviePyShortCompiler(
            self.core.PipelineConfig(gemini_api_key='', pexels_api_key=''))

    def test_exact_size_bypasses_resize(self):
        clip = SimpleNamespace(w=1080, h=1920)
        with patch.object(self.core, 'clip_resized') as resize:
            self.assertIs(self.compiler._fit_vertical(clip), clip)
            resize.assert_not_called()

    def test_equal_geometry_chains_despite_list_tuple_sizes(self):
        clips = [SimpleNamespace(size=[1080, 1920]), SimpleNamespace(size=(1080, 1920))]
        join = Mock(return_value=object())
        with patch.object(self.core, 'VideoFileClip', side_effect=clips), \
             patch.object(self.core, 'clip_without_audio', side_effect=lambda clip: clip), \
             patch.object(self.compiler, '_fit_vertical', side_effect=lambda clip: clip), \
             patch.object(self.core, 'concatenate_videoclips', join), \
             patch.object(self.core, 'loop_clip_to_duration', side_effect=lambda clip, _: clip):
            opened = []
            self.compiler._build_background([Path('a.mp4'), Path('b.mp4')], 4, opened)
        join.assert_called_once_with(clips, method='chain')
        self.assertEqual(len(opened), 3)

    def test_mismatched_geometry_retains_composition(self):
        clips = [SimpleNamespace(size=(1080, 1920)), SimpleNamespace(size=(1079, 1920))]
        with patch.object(self.core, 'VideoFileClip', side_effect=clips), \
             patch.object(self.core, 'clip_without_audio', side_effect=lambda clip: clip), \
             patch.object(self.compiler, '_fit_vertical', side_effect=lambda clip: clip), \
             patch.object(self.core, 'concatenate_videoclips') as join, \
             patch.object(self.core, 'loop_clip_to_duration'):
            self.compiler._build_background([Path('a.mp4'), Path('b.mp4')], 4, [])
        join.assert_called_once_with(clips, method='compose')

    def test_font_discovery_and_load_are_reused_by_size(self):
        font = object()
        with patch.object(self.core, 'system_font_candidates', return_value=['/font.ttf']) as discover, \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(self.core.ImageFont, 'truetype', create=True, return_value=font) as load:
            self.assertIs(self.compiler._load_font(220), font)
            self.assertIs(self.compiler._load_font(220), font)
            self.compiler._load_font(216)
        discover.assert_called_once()
        self.assertEqual(load.call_count, 2)
