"""Pixel-level characterization for the landscape background fit path."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from moviepy.video.VideoClip import VideoClip

import shorts_core as core


class BackgroundResizeTests(unittest.TestCase):
    def setUp(self):
        self.width = 360
        self.height = 640
        self.compiler = core.MoviePyShortCompiler(
            core.PipelineConfig(gemini_api_key='', pexels_api_key='',
                                width=self.width, height=self.height))

    def _clip(self, width, height, *, channels=3, dtype=np.uint8, seed=17):
        def frame_at(t):
            rng = np.random.default_rng(seed + int(t * 1000))
            shape = (height, width) if channels == 1 else (height, width, channels)
            if dtype == np.uint8:
                return rng.integers(0, 256, shape, dtype=dtype)
            if dtype == np.uint16:
                return rng.integers(0, 65536, shape, dtype=dtype)
            return rng.random(shape).astype(dtype)

        return VideoClip(frame_function=frame_at, duration=1.0)

    def _reference(self, clip):
        target_aspect = self.width / self.height
        source_aspect = clip.w / clip.h
        if source_aspect > target_aspect:
            resized = core.clip_resized(clip, height=self.height)
            x1 = max(0.0, (float(resized.w) - self.width) / 2.0)
            return core.clip_cropped(resized, x1=x1, y1=0,
                                     x2=x1 + self.width, y2=self.height)
        resized = core.clip_resized(clip, width=self.width)
        y1 = max(0.0, (float(resized.h) - self.height) / 2.0)
        return core.clip_cropped(resized, x1=0, y1=y1,
                                 x2=self.width, y2=y1 + self.height)

    def test_rgb_landscape_matches_original_moviepy_resize_and_crop(self):
        geometries = ((1920, 1080), (641, 359), (3840, 2160),
                      (1921, 1081), (100, 100), (359, 641))
        for index, (width, height) in enumerate(geometries):
            with self.subTest(source=(width, height)):
                clip = self._clip(width, height, seed=index)
                expected = self._reference(clip)
                actual = self.compiler._fit_vertical(clip)
                np.testing.assert_array_equal(actual.get_frame(0.0), expected.get_frame(0.0))
                actual.close()
                expected.close()
                clip.close()

    def test_rgb_landscape_matches_across_seeks_and_odd_center_crop(self):
        clip = self._clip(641, 359)
        expected = self._reference(clip)
        actual = self.compiler._fit_vertical(clip)
        for timestamp in (0.0, 0.237, 0.811):
            with self.subTest(timestamp=timestamp):
                np.testing.assert_array_equal(actual.get_frame(timestamp),
                                              expected.get_frame(timestamp))
        actual.close()
        expected.close()
        clip.close()

    def test_alpha_grayscale_and_non_uint8_frames_keep_original_resampling(self):
        cases = ((4, np.uint8), (1, np.uint8), (3, np.uint16), (3, np.float32))
        for channels, dtype in cases:
            with self.subTest(channels=channels, dtype=dtype):
                clip = self._clip(641, 359, channels=channels, dtype=dtype)
                expected = self._reference(clip)
                actual = self.compiler._fit_vertical(clip)
                np.testing.assert_array_equal(actual.get_frame(0.0), expected.get_frame(0.0))
                actual.close()
                expected.close()
                clip.close()

    def test_mask_clip_keeps_moviepy_mask_scaling(self):
        def mask_frame(t):
            return np.full((359, 641), 0.5 + 0.1 * t, dtype=np.float32)

        clip = VideoClip(frame_function=mask_frame, is_mask=True, duration=1.0)
        expected = self._reference(clip)
        with patch.object(core, '_fit_horizontal_landscape_frame',
                          wraps=core._fit_horizontal_landscape_frame) as horizontal_fit:
            actual = self.compiler._fit_vertical(clip)
            self.assertEqual(horizontal_fit.call_count, 0)
        np.testing.assert_array_equal(actual.get_frame(0.0), expected.get_frame(0.0))
        actual.close()
        expected.close()
        clip.close()

    def test_masked_and_legacy_clips_use_existing_moviepy_pipeline(self):
        for clip in (SimpleNamespace(w=1920, h=1080, mask=object()),
                     SimpleNamespace(w=1920, h=1080, mask=None)):
            resized = SimpleNamespace(w=3413, h=1920)
            fitted = object()
            with self.subTest(masked=clip.mask is not None), \
                 patch.object(core, 'clip_resized', return_value=resized) as resize, \
                 patch.object(core, 'clip_cropped', return_value=fitted) as crop:
                self.assertIs(self.compiler._fit_vertical(clip), fitted)
                resize.assert_called_once_with(clip, height=self.height)
                crop.assert_called_once_with(resized, x1=1526.5, y1=0,
                                             x2=1886.5, y2=self.height)

    def test_tall_landscape_fit_stays_on_original_width_resize_path(self):
        clip = self._clip(359, 641)
        expected = self._reference(clip)
        with patch.object(core, '_fit_horizontal_landscape_frame',
                          wraps=core._fit_horizontal_landscape_frame) as horizontal_fit:
            actual = self.compiler._fit_vertical(clip)
            self.assertEqual(horizontal_fit.call_count, 0)
        np.testing.assert_array_equal(actual.get_frame(0.0), expected.get_frame(0.0))
        actual.close()
        expected.close()
        clip.close()


if __name__ == '__main__':
    unittest.main()
