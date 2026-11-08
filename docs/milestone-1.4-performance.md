# Milestone 1.4: cadence-gated fitted-frame reuse

Milestone 1.4 adds a one-frame cache to the existing exact-pixel landscape fit.
It reuses the fitted frame only when the source cadence is known to be slower
than the output cadence and MoviePy returns the same immutable source array
again. All other inputs use the existing Milestone 1.3 fitting path directly.

## Implementation and safety

The original path is `FFmpeg decode → MoviePy frame retrieval → _fit_vertical →
Pillow resize/crop → background concatenation/loop → caption composite → FFmpeg
encoding`. This change wraps only the M1.3 fitted-background transform; it never
caches the caption-composited output.

The installed MoviePy reader maps timestamps to decoded positions and returns
`last_read` when the requested position is current. Successful reads create a
NumPy array over immutable Python bytes. Backward or large forward seeks restart
the decoder; failed reads can return `last_read` even as the position advances.
Consequently, neither timestamps nor reader positions prove frame identity.
The cache checks the actual returned buffer after retrieval. Restarted readers
naturally miss, while unchanged immutable fallback frames can safely hit.
Separate opened sources own separate readers and caches; concatenation, trimming
and looping retain MoviePy's timestamp mapping.

The cache is created for an opaque landscape source only when both `clip.fps` and
the configured output FPS are finite positive real numbers and
`source_fps < output_fps`. Missing, invalid, matched and faster source FPS values
bypass cache allocation and fitted-output copies. This gate avoids the measured
matched-cadence overhead; it does not guarantee a benefit for every slower
source cadence.

Each request is cacheable only when its frame is an exact NumPy `ndarray` with
`uint8` RGB pixels whose ultimate base is a Python `bytes` object. A hit also
requires the same array object and unchanged dtype, shape, strides and data
pointer. The cache keeps one tuple containing the source array, immutable backing,
metadata snapshot and fitted array. It returns a copy on hits so a downstream
caller cannot alter the retained result. Writable/recycled arrays, readonly
views over mutable storage, subclasses and unknown backing types always run the
normal M1.3 transform and clear the previous entry.

There is one cache per eligible landscape source transform, held in the
compiler's existing opened-resource list. The compiler closes it with the source
clips on success, cancellation and render errors. It is not shared across jobs.

## Full-render matrix

The [raw render records](benchmarks/milestone14-render.json) and
[machine summary](benchmarks/milestone14-summary.json) compare the exact saved
M1.3 compiler (`7ae497294b88a252a5302bcce397c87f13f4413d549afb2361e932f6a11b3c3a`)
with the gated compiler (`2966a1d854444d30a27c134293109f74b2c53efcd095a8e0b6566b601e6760b7`).
Every fixture was generated directly by FFmpeg `testsrc2` at its stated rational
input FPS. Runs were serial, ordered B1, O1, O2, B2, for two trials per version.
Each render lasted 3 seconds, used two encoder threads, synthetic narration and
12 captions. The six cadence pairs rendered at 540 × 960; 30 → 60 used a 60 FPS
output. Two resize-heavy controls rendered at 1080 × 1920.

Values below are the two-trial medians. Wall and CPU are seconds; memory and
temporary intermediate disk are MiB. `Fit transforms` counts calls to the M1.3
landscape fitting helper. `Hits / misses` are cache requests only; `—` means the
gate allocated no cache. FFmpeg CPU includes source readers and the encoder.

| Workload | Wall B / O | Python CPU B / O | FFmpeg CPU B / O | Fit transforms B / O | Cache hits / misses | RSS B / O | PSS B / O | Temp B / O |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 24 → 30, 540 × 960 | 2.75 / 2.48 | 5.58 / 5.36 | 1.30 / 1.34 | 92 / 72 | 20 / 72 | 985.2 / 985.2 | 936.1 / 936.0 | 0.05 / 0.05 |
| 25 → 30, 540 × 960 | 2.67 / 2.40 | 5.60 / 5.29 | 1.21 / 1.21 | 92 / 75 | 17 / 75 | 984.7 / 984.4 | 935.4 / 935.4 | 0.05 / 0.05 |
| 30 → 30, 540 × 960 | 2.77 / 2.86 | 5.61 / 5.71 | 1.60 / 1.56 | 92 / 92 | — | 985.9 / 984.9 | 936.5 / 935.7 | 0.05 / 0.05 |
| 30 → 60, 540 × 960 | 4.55 / 3.38 | 7.48 / 6.14 | 2.17 / 2.37 | 182 / 90 | 92 / 90 | 984.7 / 986.2 | 935.5 / 936.9 | 0.05 / 0.05 |
| 60 → 30, 540 × 960 | 2.94 / 2.96 | 5.70 / 5.76 | 1.88 / 1.81 | 92 / 92 | — | 985.3 / 984.7 | 936.4 / 934.9 | 0.05 / 0.05 |
| 24000/1001 → 30, 540 × 960 | 2.71 / 2.40 | 5.54 / 5.33 | 1.36 / 1.48 | 92 / 72 | 20 / 72 | 984.9 / 986.3 | 935.6 / 937.0 | 0.05 / 0.05 |
| 24 → 30, 1080 × 1920 | 9.66 / 8.79 | 11.99 / 11.16 | 4.66 / 4.88 | 92 / 72 | 20 / 72 | 1603.0 / 1613.8 | 1558.6 / 1569.4 | 0.05 / 0.05 |
| 30 → 30, 1080 × 1920 | 9.48 / 9.58 | 11.81 / 11.98 | 5.36 / 5.20 | 92 / 92 | — | 1587.2 / 1589.2 | 1542.7 / 1544.7 | 0.05 / 0.05 |

Summing the six modest-size workload medians gives **10.45% less render wall
time and 5.39% less Python CPU** with the gate. The two full-resolution workload
medians sum to **4.01% less wall time and 2.79% less Python CPU**. These sums
combine different frame rates and are not one production job. The matched and
faster-source controls have no cache requests; their small wall-time changes
(+3.1%, +0.7%, and +1.1% for 30 → 30 at modest size, 60 → 30, and full-resolution
30 → 30) do not establish an end-to-end improvement for those controls.
The gate removes their per-frame cache work; two trials cannot determine the
cause of the remaining timing differences.

The median of sampled per-trial full-resolution 24 → 30 PSS peaks rose by
10.8 MiB. Comparing the maximum observed trial peaks instead gives an increase
of 7.35 MiB (about 0.5%). The cache retains a 6,220,800-byte source frame and
6,220,800-byte fitted frame (5.93 MiB each). At modest size, each retained array
is 1,555,200 bytes (1.48 MiB). These bounds apply per eligible source, so memory
also depends on the number and dimensions of opened sources. The cache holds one fitted entry;
returned copies and brief decoder/encoder allocations can add transient memory.
Temporary intermediate disk stayed near 0.05 MiB in these short runs; completed
MP4 sizes are recorded separately in the raw results. RSS sums shared pages,
PSS apportions them, and 250 ms sampling can miss short peaks and final CPU
increments.

Across all 32 renders, each workload's four encoded MP4 files were byte-identical.
Full decoded RGB24 and signed 16-bit PCM streams also matched across all runs.
The [independent parity record](benchmarks/milestone14-parity.json) additionally
checks sequential and non-monotonic seeks, a 180-second three-source loop/join,
caption overlaps and gaps, narration plus music, and a 90-frame 30 FPS encode.
For that validator, 8 FPS synthetic sources were rendered at 30 FPS so the gate
was active: the wide case recorded 5 hits in 36 requests, the mixed loop 4 hits
in 27 requests across two landscape sources, the trim case 3 hits in 36 requests,
and the encoded case 47 hits in 63 requests. Raw frames, decoded RGB and PCM
matched. Cache entries cleared after successful renders, injected cancellation
and injected render errors; FFmpeg reader processes exited in the error paths.
The extracted frame was visually inspected and the caption remained readable.

## Isolated lookup diagnostic

The separate [lookup results](benchmarks/milestone14-lookup.json) use a
deterministic immutable 1920 × 1080 RGB frame and four alternating trials. Each
trial compares eight uncached M1.3 fits, eight warmed cache hit-plus-copy calls,
and one cold eight-request cycle. The medians were 0.4882 s for eight repeated
fits, 0.0093 s for eight warmed hits and copies, and 0.0723 s for the cold cycle
(one fit plus seven hits). All output hashes matched. The cache used two Pillow
resizes on the miss and none on hits; it retained one 5.93 MiB source and one
5.93 MiB fitted array, then cleared them on close. This excludes decoding,
composition, captions, audio and encoding; these timings are mechanism-level
measurements, **not render speedups**.

## Gate decision and limitations

An initial ungated candidate allocated a cache for every landscape source. Its
full-resolution matched 30 → 30 control was 3.5% slower in the two observed
trials, with only two repeated source-array requests in 92 reader requests. That
candidate was not retained. The [ungated raw results](benchmarks/milestone14-ungated-render.json),
[summary](benchmarks/milestone14-ungated-summary.json) and
[reverse patch](benchmarks/milestone14-ungated-baseline.patch) preserve the
decision evidence. The current gate bypasses matched and faster sources.

The measurements cover short synthetic clips on this Python 3.14.8, MoviePy
2.1.2, Pillow 11.3.0, NumPy 2.4.4 and FFmpeg 7.0.2 environment. They use two
trials per version on a shared desktop and do not establish confidence intervals
or production-wide gains. A source just below output cadence, such as 29.97 → 30,
may not repeat often enough to offset the cache copy; that near-matched rate was
not measured. The gate is an opportunity check, not a prediction of actual hit
rate. Reader request counts are `FFMPEG_VideoReader.get_frame` calls, not decoded
packet counts. Network services, speech recognition and TTS were not involved.

The full test suite passed **153 tests**. The baseline patch reconstructs the
saved compiler hash above. The raw render, parity and isolated lookup results are
retained under `docs/benchmarks/`.

Affected files are `shorts_core.py`, `tests/test_background_frame_cache.py`,
`tests/test_rendering.py`, the compatible instrumentation wrapper in
`scripts/benchmark_background_resize.py`, and the validator
`scripts/validate_background_resize.py`. New reproducible tooling is in
`scripts/benchmark_background_frame_cache.py`,
`scripts/benchmark_frame_cache_lookup.py`, and
`scripts/summarize_background_frame_cache.py`.

The recommended next optimization study is a longer near-matched cadence
benchmark, including 29.97 → 30 and multiple opened readers, to establish when
copy costs exceed avoided fits before changing the gate further.

## Reproduce

From the project root, reconstruct the exact local M1.3 baseline, run the serial
matrix, isolated diagnostic and parity validator with:

```bash
rtk proxy mkdir -p /tmp/shorts_core
rtk proxy cp shorts_core.py /tmp/shorts_core/milestone14-reference.py
rtk proxy patch /tmp/shorts_core/milestone14-reference.py < docs/benchmarks/milestone14-baseline.patch
rtk proxy sha256sum /tmp/shorts_core/milestone14-reference.py
rtk python3 scripts/benchmark_background_frame_cache.py --baseline-source /tmp/shorts_core/milestone14-reference.py --result /tmp/milestone14-render.json
rtk python3 scripts/benchmark_frame_cache_lookup.py --result /tmp/milestone14-lookup.json
rtk python3 scripts/summarize_background_frame_cache.py --input /tmp/milestone14-render.json --lookup /tmp/milestone14-lookup.json --result /tmp/milestone14-summary.json
rtk python3 scripts/validate_background_resize.py --reference /tmp/shorts_core/milestone14-reference.py --result /tmp/milestone14-parity.json
```

The benchmark scripts generate all media in temporary directories and remove it
after each run. The parity validator can copy a temporary frame to a requested
path with `--preview-copy` for visual inspection. The required repository checks
are:

```bash
rtk python3 -m unittest discover
rtk python3 -m py_compile shorts_core.py
```
