"""Safety and lifecycle checks for the one-frame fitted background cache."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
from moviepy.video.VideoClip import VideoClip

import shorts_core as core


class FittedLandscapeFrameCacheTests(unittest.TestCase):
    def setUp(self):
        self.compiler = core.MoviePyShortCompiler(core.PipelineConfig(
            gemini_api_key='', pexels_api_key='', width=90, height=160))

    @staticmethod
    def _immutable_frame(seed=1, shape=(4, 8, 3)):
        rng = np.random.default_rng(seed)
        return np.frombuffer(rng.integers(0, 256, np.prod(shape), dtype=np.uint8).tobytes(),
                             dtype=np.uint8).reshape(shape)

    def test_immutable_same_frame_hits_and_returned_arrays_cannot_mutate_cache(self):
        source = self._immutable_frame()
        calls = []

        def transform(frame):
            calls.append(frame)
            return frame[::-1].copy()

        cache = core._FittedLandscapeFrameCache(transform)
        first = cache(source)
        expected = first.copy()
        first[:] = 0
        second = cache(source)

        np.testing.assert_array_equal(second, expected)
        self.assertEqual(calls, [source])
        self.assertEqual((cache.requests, cache.transforms, cache.hits, cache.misses),
                         (2, 1, 1, 1))

    def test_different_array_object_with_same_immutable_backing_misses(self):
        source = self._immutable_frame()
        view = source.view()
        calls = []
        cache = core._FittedLandscapeFrameCache(lambda frame: calls.append(frame) or frame.copy())

        cache(source)
        cache(view)

        self.assertEqual(calls, [source, view])
        self.assertEqual(cache.hits, 0)
        self.assertEqual(cache.misses, 2)
        self.assertIs(cache._entry[0], view)

    def test_recycled_writable_source_is_transformed_each_time(self):
        source = np.zeros((4, 8, 3), dtype=np.uint8)
        cache = core._FittedLandscapeFrameCache(lambda frame: frame.copy())

        first = cache(source)
        source[:] = 255
        second = cache(source)

        self.assertFalse(np.array_equal(first, second))
        self.assertEqual((cache.hits, cache.misses, cache.transforms), (0, 2, 2))
        self.assertIsNone(cache._entry)

    def test_readonly_view_over_mutable_array_is_not_cacheable(self):
        owner = np.zeros((4, 8, 3), dtype=np.uint8)
        source = owner.view()
        source.flags.writeable = False
        cache = core._FittedLandscapeFrameCache(lambda frame: frame.copy())

        first = cache(source)
        owner[:] = 255
        second = cache(source)

        self.assertFalse(np.array_equal(first, second))
        self.assertEqual((cache.hits, cache.misses, cache.transforms), (0, 2, 2))

    def test_ndarray_subclass_is_not_cacheable(self):
        class CustomFrame(np.ndarray):
            pass

        source = self._immutable_frame().view(CustomFrame)
        cache = core._FittedLandscapeFrameCache(lambda frame: np.asarray(frame).copy())

        cache(source)
        cache(source)

        self.assertEqual((cache.hits, cache.misses, cache.transforms), (0, 2, 2))

    def test_changed_array_metadata_misses_even_when_backing_is_immutable(self):
        source = self._immutable_frame(shape=(2, 16, 3))
        cache = core._FittedLandscapeFrameCache(lambda frame: frame.copy())

        cache(source)
        source.shape = (4, 8, 3)
        cache(source)

        self.assertEqual((cache.hits, cache.misses, cache.transforms), (0, 2, 2))
        self.assertEqual(cache._entry[2][0], source.shape)

    def test_actual_landscape_fit_reuses_only_while_cache_is_open(self):
        source = self._immutable_frame(seed=3, shape=(359, 641, 3))
        clip = VideoClip(frame_function=lambda _t: source, duration=1.0)
        clip.fps = 24
        opened = []
        fitted = self.compiler._fit_vertical(clip, opened)
        cache = opened[0]

        first = fitted.get_frame(0.0)
        expected = first.copy()
        first[:] = 0
        second = fitted.get_frame(0.5)
        np.testing.assert_array_equal(second, expected)
        self.assertGreaterEqual(cache.requests, 2)
        self.assertEqual((cache.transforms, cache.hits), (1, cache.requests - 1))
        self.assertEqual(len(cache._entry[3].shape), 3)

        core.close_all(opened)
        self.assertTrue(cache.closed)
        self.assertIsNone(cache._entry)
        fitted.close()
        clip.close()

    def test_cache_is_disabled_for_matched_faster_unknown_or_invalid_cadence(self):
        source = self._immutable_frame(seed=4, shape=(359, 641, 3))
        for source_fps in (30, 60, None, 0, -1, float('nan'), float('inf'), '24'):
            with self.subTest(source_fps=source_fps):
                clip = VideoClip(frame_function=lambda _t: source, duration=1.0)
                clip.fps = source_fps
                opened = []
                with patch.object(core, '_fit_horizontal_landscape_frame',
                                  wraps=core._fit_horizontal_landscape_frame) as transform:
                    fitted = self.compiler._fit_vertical(clip, opened)
                    first = fitted.get_frame(0.0)
                    second = fitted.get_frame(0.5)
                self.assertEqual(opened, [])
                self.assertGreaterEqual(transform.call_count, 2)
                np.testing.assert_array_equal(first, second)
                fitted.close()
                clip.close()


if __name__ == '__main__':
    unittest.main()
