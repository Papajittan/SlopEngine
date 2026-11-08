#!/usr/bin/env python3
"""Isolate repeated fitted-frame lookup from decoding, composition and encoding."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np  # noqa: E402
import PIL  # noqa: E402
import shorts_core as core  # noqa: E402


def digest(frame):
    return hashlib.sha256(frame.tobytes()).hexdigest()


def timed_outputs(operation, frame, count):
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    outputs = [operation(frame) for _ in range(count)]
    wall = time.perf_counter() - wall_start
    cpu = time.process_time() - cpu_start
    hashes = [digest(output) for output in outputs]
    del outputs
    return wall, cpu, hashes


def run(iterations, repetitions):
    source_height, source_width = 1080, 1920
    target_width, target_height = 1080, 1920
    resized_width = int(source_width * target_height / source_height)
    crop_x1 = int(max(0.0, (resized_width - target_width) / 2.0))
    crop_x2 = int(max(0.0, (resized_width - target_width) / 2.0) + target_width)
    random = np.random.default_rng(140)
    frame = np.frombuffer(
        random.integers(0, 256, source_height * source_width * 3, dtype=np.uint8).tobytes(),
        dtype=np.uint8,
    ).reshape((source_height, source_width, 3))

    counters = {"calls": 0, "wall_seconds": 0.0, "process_cpu_seconds": 0.0}
    original_resize = core.Image.Image.resize

    def measured_resize(image, *args, **kwargs):
        wall_start = time.perf_counter()
        cpu_start = time.process_time()
        try:
            return original_resize(image, *args, **kwargs)
        finally:
            counters["calls"] += 1
            counters["wall_seconds"] += time.perf_counter() - wall_start
            counters["process_cpu_seconds"] += time.process_time() - cpu_start

    core.Image.Image.resize = measured_resize

    def fit(source):
        return core._fit_horizontal_landscape_frame(
            source, target_w=target_width, target_h=target_height,
            resized_w=resized_width, crop_x1=crop_x1, crop_x2=crop_x2)

    records = []
    for trial in range(1, repetitions + 1):
        def measure_baseline():
            before_resize = dict(counters)
            wall, cpu, hashes = timed_outputs(fit, frame, iterations)
            resize = {key: counters[key] - before_resize[key] for key in counters}
            return {"wall": wall, "cpu": cpu, "hashes": hashes, "resize": resize}

        def measure_warmed_cache():
            cache = core._FittedLandscapeFrameCache(fit)
            warmup_start = time.perf_counter()
            warmup_cpu_start = time.process_time()
            warmup = cache(frame)
            warmup_wall = time.perf_counter() - warmup_start
            warmup_cpu = time.process_time() - warmup_cpu_start
            warmup_hash = digest(warmup)
            del warmup
            before_resize = dict(counters)
            wall, cpu, hashes = timed_outputs(cache, frame, iterations)
            resize = {key: counters[key] - before_resize[key] for key in counters}
            state = {
                "requests": cache.requests, "hits": cache.hits, "misses": cache.misses,
                "transforms": cache.transforms,
                "retained_source_bytes": len(cache._entry[1]) if cache._entry else 0,
                "retained_fitted_bytes": cache._entry[3].nbytes if cache._entry else 0,
                "retained_entries": int(cache._entry is not None),
            }
            closed_before = cache.closed
            cache.close()
            return {"wall": wall, "cpu": cpu, "hashes": hashes, "resize": resize,
                    "warmup_wall": warmup_wall, "warmup_cpu": warmup_cpu,
                    "warmup_hash": warmup_hash, "cache": state,
                    "closed_before": closed_before,
                    "cleared_after_close": cache.closed and cache._entry is None}

        order = (("baseline", "warmed-cache") if trial % 2 else
                 ("warmed-cache", "baseline"))
        measured = {}
        for variant in order:
            measured[variant] = (measure_baseline() if variant == "baseline"
                                 else measure_warmed_cache())
        baseline = measured["baseline"]
        warmed = measured["warmed-cache"]

        full_cycle = core._FittedLandscapeFrameCache(fit)
        before_resize = dict(counters)
        cycle_wall, cycle_cpu, cycle_hashes = timed_outputs(full_cycle, frame, iterations)
        cycle_resize = {key: counters[key] - before_resize[key] for key in counters}
        cycle_state = {
            "requests": full_cycle.requests, "hits": full_cycle.hits,
            "misses": full_cycle.misses, "transforms": full_cycle.transforms,
            "retained_source_bytes": len(full_cycle._entry[1]) if full_cycle._entry else 0,
            "retained_fitted_bytes": full_cycle._entry[3].nbytes if full_cycle._entry else 0,
            "retained_entries": int(full_cycle._entry is not None),
        }
        full_cycle.close()
        cycle_cache_cleared = full_cycle.closed and full_cycle._entry is None

        reference_hash = baseline["hashes"][0]
        hashes_equal = (len(set(baseline["hashes"])) == 1
                        and warmed["warmup_hash"] == reference_hash
                        and all(value == reference_hash for value in warmed["hashes"])
                        and all(value == reference_hash for value in cycle_hashes))
        records.append({
            "trial": trial, "order": [*order, "cold-cache-cycle"],
            "iterations_per_measurement": iterations,
            "baseline_uncached_fit": {
                "wall_seconds": baseline["wall"], "process_cpu_seconds": baseline["cpu"],
                "pillow_resize": baseline["resize"],
            },
            "warmed_cache_hit_copy_only": {
                "warmup_miss_wall_seconds": warmed["warmup_wall"],
                "warmup_miss_process_cpu_seconds": warmed["warmup_cpu"],
                "wall_seconds": warmed["wall"], "process_cpu_seconds": warmed["cpu"],
                "pillow_resize": warmed["resize"], "cache": warmed["cache"],
                "cache_cleared_after_close": warmed["cleared_after_close"],
                "was_closed_before_measurement": warmed["closed_before"],
            },
            "cold_cache_cycle": {
                "wall_seconds": cycle_wall, "process_cpu_seconds": cycle_cpu,
                "pillow_resize": cycle_resize, "cache": cycle_state,
                "cache_cleared_after_close": cycle_cache_cleared,
            },
            "outputs_equal": hashes_equal,
            "output_sha256": reference_hash,
        })

    core.Image.Image.resize = original_resize
    def median(values):
        return statistics.median(values)

    return {
        "benchmark": "milestone-1.4-isolated-frame-cache-lookup",
        "method": "Four trials alternate the order of eight uncached M1.3 fits and eight warmed cache hit+copy calls; each trial then measures an eight-call cold cache cycle. All use one immutable RGB frame; no FFmpeg decoding, MoviePy composition, caption selection, audio or video encoding is included.",
        "source_frame": {"size": [source_width, source_height], "dtype": "uint8 RGB",
                         "backing": "deterministically generated Python bytes; setup excluded",
                         "sha256": hashlib.sha256(frame.tobytes()).hexdigest()},
        "output_frame_size": [target_width, target_height],
        "environment": {"python": sys.version, "platform": platform.platform(),
                        "numpy": np.__version__, "pillow": PIL.__version__},
        "trials": records,
        "medians": {
            "uncached_fit_wall_seconds": median([r["baseline_uncached_fit"]["wall_seconds"] for r in records]),
            "warmed_cache_hit_copy_wall_seconds": median([r["warmed_cache_hit_copy_only"]["wall_seconds"] for r in records]),
            "cold_cache_cycle_wall_seconds": median([r["cold_cache_cycle"]["wall_seconds"] for r in records]),
            "uncached_fit_process_cpu_seconds": median([r["baseline_uncached_fit"]["process_cpu_seconds"] for r in records]),
            "warmed_cache_hit_copy_process_cpu_seconds": median([r["warmed_cache_hit_copy_only"]["process_cpu_seconds"] for r in records]),
            "cold_cache_cycle_process_cpu_seconds": median([r["cold_cache_cycle"]["process_cpu_seconds"] for r in records]),
        },
        "all_outputs_equal": all(record["outputs_equal"] for record in records),
        "all_cache_entries_cleared": all(
            trial["warmed_cache_hit_copy_only"]["cache_cleared_after_close"]
            and trial["cold_cache_cycle"]["cache_cleared_after_close"] for trial in records),
        "interpretation_note": "This diagnostic isolates the fitted transform and cache copy path. Ratios are not render speedups and are sensitive to image size, memory bandwidth, Python/Pillow versions and shared machine load.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--repetitions", type=int, default=4)
    parser.add_argument("--result", type=Path,
                        default=ROOT / "docs/benchmarks/milestone14-lookup.json")
    args = parser.parse_args()
    if args.iterations < 1 or args.repetitions < 1:
        parser.error("iterations and repetitions must be positive")
    result = run(args.iterations, args.repetitions)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not (result["all_outputs_equal"] and result["all_cache_entries_cleared"]):
        raise SystemExit("Isolated cache output or cleanup check failed")


if __name__ == "__main__":
    main()
