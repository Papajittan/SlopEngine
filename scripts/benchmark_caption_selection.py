#!/usr/bin/env python3
"""Compare MoviePy's caption selection with the indexed selector, without rendering.

Run from the project root: python3 scripts/benchmark_caption_selection.py
Fixtures use evenly spaced, contiguous captions at two observed workload densities.
CPU timings exclude fixture/index construction and do not measure rendering.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import moviepy
import numpy as np
from moviepy import CompositeVideoClip, ImageClip

from shorts_core import _index_caption_clip_selection


def benchmark(seconds, caption_count, fps, repetitions, passes):
    pixel = np.zeros((1, 1, 3), dtype=np.uint8)
    background = ImageClip(pixel).with_duration(seconds)
    clips = [ImageClip(pixel).with_duration(seconds / caption_count)
             .with_start(i * seconds / caption_count)
             for i in range(caption_count)]
    composite = CompositeVideoClip([background, *clips], use_bgclip=True, bg_color=(0, 0, 0))
    times = [frame / fps for frame in range(seconds * fps)]
    baseline = composite.playing_clips
    try:
        _index_caption_clip_selection(composite)
        indexed = composite.playing_clips
        for timestamp in times:
            if baseline(timestamp) != indexed(timestamp):
                raise AssertionError(f"Selection differs at {timestamp} seconds")

        results = {"baseline": [], "indexed": []}
        for repetition in range(repetitions):
            order = (("baseline", baseline), ("indexed", indexed))
            if repetition % 2:
                order = tuple(reversed(order))
            for name, selector in order:
                start = time.process_time()
                for _ in range(passes):
                    for timestamp in times:
                        selector(timestamp)
                results[name].append(time.process_time() - start)
        medians = {name: statistics.median(values) for name, values in results.items()}
        return {
            "duration_seconds": seconds,
            "captions": len(clips),
            "frames_per_pass": len(times),
            "passes": passes,
            "cpu_seconds": results,
            "median_cpu_seconds": medians,
            "median_speedup": medians["baseline"] / medians["indexed"],
            "selection_equal": True,
        }
    finally:
        composite.close()
        background.close()
        for clip in clips:
            clip.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--passes", type=int, default=10)
    args = parser.parse_args()
    if min(args.fps, args.repetitions, args.passes) < 1:
        parser.error("numeric arguments must be positive")
    print(json.dumps({
        "moviepy_version": moviepy.__version__,
        "fps": args.fps,
        "cases": [benchmark(seconds, captions, args.fps, args.repetitions, args.passes)
                  for seconds, captions in ((30, 39), (30, 77), (60, 80), (60, 159))],
    }, indent=2))


if __name__ == "__main__":
    main()
