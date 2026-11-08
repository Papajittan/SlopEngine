#!/usr/bin/env python3
"""Derive compact per-workload M1.4 results from retained raw render samples."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def median(records, path):
    values = []
    for record in records:
        value = record
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is not None:
            values.append(value)
    return statistics.median(values) if values else None


def summarize_case(case):
    baseline = [run for run in case["runs"] if run["variant"] == "baseline"]
    optimized = [run for run in case["runs"] if run["variant"] == "optimized"]
    metrics = {
        "render_wall_seconds": ("render_wall_seconds",),
        "process_wall_seconds_including_import": ("process_wall_seconds_including_import",),
        "python_cpu_seconds": ("process_cpu_seconds_by_category", "python"),
        "ffmpeg_cpu_seconds": ("process_cpu_seconds_by_category", "ffmpeg"),
        "pillow_resize_calls": ("measurements", "pillow_resize_calls"),
        "pillow_resize_process_cpu_seconds": ("measurements", "pillow_resize_process_cpu_seconds"),
        "fitted_transform_calls": ("measurements", "fitted_transform_calls"),
        "fitted_transform_process_cpu_seconds": ("measurements", "fitted_transform_process_cpu_seconds"),
        "source_reader_frame_requests": ("measurements", "source_reader_frame_requests"),
        "source_reader_same_array_requests": ("measurements", "source_reader_same_array_requests"),
        "source_reader_immutable_bytes_requests": ("measurements", "source_reader_immutable_bytes_requests"),
        "cache_requests": ("measurements", "cache_requests"),
        "cache_hits": ("measurements", "cache_hits"),
        "cache_misses": ("measurements", "cache_misses"),
        "peak_tree_rss_bytes": ("peak_tree_rss_bytes",),
        "peak_tree_pss_bytes": ("peak_tree_pss_bytes",),
        "peak_temporary_intermediate_bytes": ("peak_temporary_intermediate_bytes",),
        "output_bytes": ("output_bytes",),
    }
    values = {name: {"baseline": median(baseline, path),
                     "optimized": median(optimized, path)}
              for name, path in metrics.items()}
    wall = values["render_wall_seconds"]
    py_cpu = values["python_cpu_seconds"]
    transforms = values["fitted_transform_calls"]
    cache_requests = values["cache_requests"]["optimized"]
    cache_hits = values["cache_hits"]["optimized"]
    return {
        "name": case["name"], "scope": case["scope"],
        "source_fps": case["source_fps"], "output_fps": case["output_fps"],
        "output_size": [case["width"], case["height"]],
        "seconds": case["seconds"], "trials_per_variant": {"baseline": len(baseline),
                                                               "optimized": len(optimized)},
        "medians": values,
        "render_wall_reduction_percent": (100 * (wall["baseline"] - wall["optimized"])
                                           / wall["baseline"] if wall["baseline"] else None),
        "python_cpu_reduction_percent": (100 * (py_cpu["baseline"] - py_cpu["optimized"])
                                          / py_cpu["baseline"] if py_cpu["baseline"] else None),
        "fitted_transform_reduction_percent": (100 * (transforms["baseline"] - transforms["optimized"])
                                                / transforms["baseline"]
                                                if transforms["baseline"] else None),
        "cache_hit_fraction": (cache_hits / cache_requests if cache_requests else None),
        "peak_cache_source_bytes": max((run["measurements"].get("cache_peak_retained_source_bytes", 0)
                                        for run in optimized), default=0),
        "peak_cache_fitted_bytes": max((run["measurements"].get("cache_peak_retained_fitted_bytes", 0)
                                        for run in optimized), default=0),
        "peak_cache_entries": max((run["measurements"].get("cache_peak_entries", 0)
                                   for run in optimized), default=0),
        "all_four_encoded_files_equal": case["parity"]["all_four_encoded_files_equal"],
        "decoded_rgb24_equal": case["parity"]["all_decoded_rgb24_equal"],
        "decoded_pcm_equal": case["parity"]["all_decoded_pcm_equal"],
        "reader_cleanup_passed": all(
            run["measurements"]["source_reader_objects_closed_after_compile"]
            and run["measurements"]["source_reader_processes_exited_after_compile"]
            for run in case["runs"]),
        "passed": case["passed"],
    }


def aggregate(cases):
    def total(group, metric, variant):
        values = [case["medians"][metric][variant] for case in group]
        return sum(value for value in values if value is not None)

    baseline_wall = total(cases, "render_wall_seconds", "baseline")
    optimized_wall = total(cases, "render_wall_seconds", "optimized")
    baseline_cpu = total(cases, "python_cpu_seconds", "baseline")
    optimized_cpu = total(cases, "python_cpu_seconds", "optimized")
    return {
        "workload_count": len(cases),
        "summed_workload_medians": {
            "render_wall_seconds": {"baseline": baseline_wall, "optimized": optimized_wall},
            "python_cpu_seconds": {"baseline": baseline_cpu, "optimized": optimized_cpu},
        },
        "render_wall_reduction_percent": (100 * (baseline_wall - optimized_wall) / baseline_wall
                                           if baseline_wall else None),
        "python_cpu_reduction_percent": (100 * (baseline_cpu - optimized_cpu) / baseline_cpu
                                          if baseline_cpu else None),
        "sum_note": "Sum of each workload's two-trial median; workloads have different output frame rates and are not a single production job.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "docs/benchmarks/milestone14-render.json")
    parser.add_argument("--result", type=Path, default=ROOT / "docs/benchmarks/milestone14-summary.json")
    parser.add_argument("--lookup", type=Path, default=ROOT / "docs/benchmarks/milestone14-lookup.json")
    args = parser.parse_args()
    raw = json.loads(args.input.read_text())
    lookup = json.loads(args.lookup.read_text())
    cases = [summarize_case(case) for case in raw["cases"]]
    result = {
        "benchmark": raw["benchmark"],
        "source_hashes": {"baseline": raw["baseline_sha256"],
                          "optimized": raw["optimized_sha256"]},
        "total_render_count": sum(sum(case["trials_per_variant"].values()) for case in cases),
        "passed": raw["passed"] and all(case["passed"] for case in cases),
        "modest_fps_matrix": aggregate([case for case in cases if case["scope"] == "all-six-pair-modest-render"]),
        "full_resolution_controls": aggregate([case for case in cases if case["scope"] == "full-resolution-resize-heavy"]),
        "isolated_lookup": {
            "result_file": str(args.lookup),
            "medians": lookup["medians"],
            "all_outputs_equal": lookup["all_outputs_equal"],
            "all_cache_entries_cleared": lookup["all_cache_entries_cleared"],
            "note": "Separate no-decode/no-encode diagnostic; its timings are not render speedups.",
        },
        "cases": cases,
        "interpretation_note": "Observed values from short synthetic full renders on this machine. Two trials per variant do not establish general performance. The separate isolated lookup diagnostic is reported for mechanism-level context and is not a render-speedup estimate.",
    }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit("Summary contains a failed case")


if __name__ == "__main__":
    main()
