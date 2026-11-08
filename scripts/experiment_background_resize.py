#!/usr/bin/env python3
"""Compare Pillow fit strategies without decoding, composition, or encoding.

These CPU timings are isolated operations, not full-render speedups. RGB fixtures
are deterministic random noise, which exposes sampling phase and rounding changes.
Run from the project root with the media environment's Python.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time

import numpy as np
from PIL import Image, __version__ as pillow_version


def geometry(width, height, target_width, target_height):
    if width / height > target_width / target_height:
        new_width, new_height = int(width * target_height / height), target_height
    else:
        new_width, new_height = target_width, int(height * target_width / width)
    return new_width, new_height, int((new_width - target_width) / 2), int((new_height - target_height) / 2)


def fit(image, target, method):
    width, height = image.size
    target_width, target_height = target
    new_width, new_height, x, y = geometry(width, height, *target)
    lanczos = Image.Resampling.LANCZOS
    if method == "baseline":
        return np.array(image.resize((new_width, new_height), lanczos))[
            y:y + target_height, x:x + target_width]
    if method == "direct_box":
        box = (x * width / new_width, y * height / new_height,
               (x + target_width) * width / new_width,
               (y + target_height) * height / new_height)
        return np.array(image.resize(target, lanczos, box))
    if method == "separable":
        horizontal = image.resize((new_width, height), lanczos)
        visible = horizontal.crop((x, 0, x + target_width, height))
        vertical = visible.resize((target_width, new_height), lanczos)
        return np.array(vertical.crop((0, y, target_width, y + target_height)))
    raise ValueError(method)


def difference(before, after):
    delta = np.abs(before.astype(np.int16) - after.astype(np.int16))
    return dict(max_channel_difference=int(delta.max()),
                changed_channels=int(np.count_nonzero(delta)),
                mean_absolute_error=float(delta.mean()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--random-cases", type=int, default=300)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1 or args.random_cases < 0:
        parser.error("runs must be positive and random-cases nonnegative")
    rng = np.random.default_rng(8)
    failures = []
    for _ in range(args.random_cases):
        width, height = rng.integers(1, 160, 2).tolist()
        target = tuple(rng.integers(1, 200, 2).tolist())
        image = Image.fromarray(rng.integers(0, 256, (height, width, 3), dtype=np.uint8))
        delta = difference(fit(image, target, "baseline"), fit(image, target, "separable"))
        if delta["changed_channels"]:
            failures.append(dict(source=[width, height], target=target, **delta))
    cases = []
    representatives = [(1920, 1080, 1080, 1920), (641, 359, 1080, 1920),
                       (3840, 2160, 1080, 1920), (1920, 1080, 540, 960),
                       (100, 100, 54, 96), (359, 641, 540, 960),
                       (1921, 1081, 1080, 1920), (1080, 1920, 1080, 1920),
                       (1080, 4000, 1080, 1920)]
    for width, height, target_width, target_height in representatives:
        image = Image.fromarray(rng.integers(0, 256, (height, width, 3), dtype=np.uint8))
        target = (target_width, target_height)
        original = fit(image, target, "baseline")
        comparisons = {method: difference(original, fit(image, target, method))
                       for method in ("direct_box", "separable")}
        samples = {method: [] for method in ("baseline", "separable")}
        for trial in range(args.runs):
            order = ("baseline", "separable") if trial % 2 == 0 else ("separable", "baseline")
            for method in order:
                start = time.process_time()
                fit(image, target, method)
                samples[method].append(time.process_time() - start)
        cases.append(dict(source=[width, height], target=target, comparisons=comparisons,
                          cpu_seconds=samples,
                          cpu_medians={method: statistics.median(values)
                                       for method, values in samples.items()}))
    result = dict(pillow=pillow_version, numpy=np.__version__, seed=8,
                  random_cases=args.random_cases, failures=failures, timings=cases,
                  timing_scope="isolated RGB array/Pillow operations; no full-render claim",
                  caveat="Identity and tall-source cases deliberately test the primitive; production retains its no-op/fallback paths.")
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if failures or any(case["comparisons"]["separable"]["changed_channels"] for case in cases):
        raise SystemExit("Separable output differs")


if __name__ == "__main__":
    main()
