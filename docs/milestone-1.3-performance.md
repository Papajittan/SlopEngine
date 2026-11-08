# Milestone 1.3: full-frame background resizing

The selected exact-pixel Pillow optimization reduced median render time by
**33.79–37.66%** across the four full-resolution resize-heavy workloads. Summing
those workload medians gives **36.46% less render time and 36.65% less sampled
Python CPU work**. All 32 renders passed full decoding checks, and each workload's
four MP4s were byte-identical. Sampled memory peaks did not meaningfully regress.

## Original architecture and bottleneck

This milestone compares against the **local Milestone 1.2 compiler**, including its
uncommitted caption-selection work, rather than the older Git HEAD. Its SHA-256 is
`d2d47f05b75f6ba6d18e59ebf69d2499c224dc29be98720ce477b08d98b60e38`.
The indexed caption selector, audio pipeline, encoder settings and production
concurrency limit are outside this change.

The original background path is:

`source → FFmpeg decoder → RGB NumPy frame → MoviePy Resize/Pillow Lanczos → NumPy → center-crop view → concatenation/loop → caption composite → FFmpeg encoder`.

`MoviePyShortCompiler._build_background` opens sources without audio and calls
`_fit_vertical`. The latter preserves aspect ratio by scaling to cover the output,
then center-crops using integer-truncated coordinates. Already-sized footage
returns directly. The transformations are lazy and run whenever MoviePy requests
a frame; they are not preprocessing operations. Equal-sized fitted clips are
chained, then looped or trimmed to the narration/caption duration. Source readers
remain owned by the existing compiler cleanup.

For 1920 × 1080 footage fitted to 1080 × 1920, the old path creates a
3413 × 1920 resized RGB frame before discarding 2333 columns. It converts the
entire resized image back to NumPy. MoviePy additionally casts the input to
`uint8` with a copy. These operations repeat during rendering and looping.

The [Milestone 1.1 profile](milestone-1.1-performance.md) recorded 44.05 cumulative
seconds in Pillow's underlying resize in its separate 30-second synthetic
diagnostic. The earlier [rendering profile](rendering-performance.md) recorded
6.90 cumulative seconds in resize out of a 12.42-second compile. These overlapping,
profiled historical measurements identify a candidate; they are not predicted
savings or controlled Milestone 1.3 results.

## Candidate experiments

The [reproducible isolated experiment](../scripts/experiment_background_resize.py)
and [raw results](benchmarks/milestone13-resize-experiment.json) compare RGB Pillow
operations with deterministic random images, including 300 small randomized
geometries and representative landscape, portrait, square, 4K, odd and native
dimensions. Eight trials alternate the two timed methods.

* **Prepared media:** fitting into a temporary video would remove Python resize
  work from the final write, but requires preprocessing, additional encoding and
  decoding, temporary storage and lifecycle handling. Lossy intermediates change
  pixels; lossless RGB intermediates have storage and bandwidth costs. No prepared
  media implementation or full-render performance claim is made here.
* **FFmpeg scaling:** the installed MoviePy reader supports `target_resolution`
  and `resize_algo`. Its filter scales decoded video before the RGB pipe
  conversion and does not expose center cropping through those arguments.
  FFmpeg Lanczos and Pillow RGB Lanczos are different processing paths. Moving
  fitting there would require a separate quality decision and validation.
* **Pillow source-box resize:** directly requesting the visible source rectangle
  is faster in principle, but changes sampling coordinates. At 1920 × 1080 to
  1080 × 1920, the retained experiment found 21,576 differing channel values,
  maximum difference 2 and mean absolute error 0.00347. It was rejected.
* **Avoid the input cast copy only:** safe for decoded `uint8` frames but removes
  very little of the dominant resize work. It is included within the selected
  path rather than introduced as a separate rendering backend.
* **Separate Pillow passes and crop between them:** Pillow's RGB Lanczos resize
  applies its horizontal pass before its vertical pass, with an 8-bit intermediate.
  The pinned [Pillow 11.3.0 resampling implementation](https://github.com/python-pillow/Pillow/blob/11.3.0/src/libImaging/Resample.c#L675)
  confirms this ordering and the same-mode intermediate.
  Performing the same horizontal pass, cropping unused columns, then applying the
  same vertical pass retains the quantization and sample coordinates while
  avoiding vertical filtering of invisible columns. Only the visible result is
  converted to NumPy. All 300 randomized and nine representative comparisons
  were pixel-exact in Pillow 11.3.0. The isolated 1920 × 1080 case measured median
  CPU 0.11133 → 0.07133 seconds per operation. This is **not** a render speedup.

The split operation has little benefit for tall or native-size sources, so those
paths retain their existing behavior. No frame cache or duration-proportional
decoded buffer is needed.

## Implemented change

The production change adds one frame helper and one branch in `_fit_vertical`.
For an opaque, non-mask MoviePy 2.x clip with horizontal overflow, it calculates
the original integer resize and center-crop coordinates once. For RGB `uint8`
frames, it performs horizontal Lanczos resize → PIL center crop → vertical
Lanczos resize → final NumPy conversion. The input cast uses `copy=False`.
Alpha, grayscale and other dtypes retain the original single Pillow resize
semantics; clips with masks, standalone mask clips, and the legacy MoviePy API
retain the original MoviePy resize/crop path. Native-size and tall-source branches
are unchanged. In particular, the 1.x fallback does not replace its potentially
different resampling backend with Pillow.

This still resizes each requested frame. It removes **unnecessary full-width
vertical filtering and output conversion**, rather than caching a video or
eliminating all resizing. It adds no preprocessing stage, dependencies,
intermediate video, reader processes or retained frame cache. Sequencing, looping,
composition, caption selection/placement, audio, encoding, temporary staging and
publication code are unchanged.

A fresh four-second profile of the saved baseline confirms the bottleneck in
this environment: compile 12.944 s; frame iteration 9.764 cumulative s; MoviePy
resizer 7.740 cumulative s; Pillow resize 6.096 cumulative s in 64 calls; writer
1.847 cumulative s. See [baseline profile](benchmarks/milestone13-baseline-profile.json).
These regions overlap and the profiler affects timings.

The same separate four-second diagnostic after the change produced:

| Profile region | Baseline s | Optimized s |
|---|---:|---:|
| Complete compile wall | 12.944 | 9.034 |
| Frame iteration cumulative | 9.764 | 6.049 |
| Pillow resize cumulative | 6.096 | 3.609 |
| Writer frame calls cumulative, including pipe waits | 1.847 | 1.903 |

The [optimized profile](benchmarks/milestone13-optimized-profile.json) makes 126
individual Pillow resize calls versus 64, because the selected operation is now
split. Its lower cumulative time comes from doing less pixel work. This is one
profiled pair with separate synthetic media, not the repeated full-render result.

## Correctness and independent review

The [independent validator](../scripts/validate_background_resize.py) compares the
saved baseline against the final compiler, whose SHA-256 is
`7ae497294b88a252a5302bcce397c87f13f4413d549afb2361e932f6a11b3c3a`.
[Raw validation results](benchmarks/milestone13-parity.json) record:

* Exact raw frame hashes at 34 sequential and non-monotonic timestamps for each
  of six cases: odd landscape, odd portrait, mixed square/landscape/portrait
  concatenation, fallback, native-size and a longer source trimmed to output.
* Exact audio hashes for five bounded chunks per case, with narration alone and
  narration plus music. The mixed case includes caption overlaps, gaps, the
  existing short-event duration floor and boundary seeks.
* Exact frame hashes for 14 seeks across a 180-second three-source loop timeline,
  including explicit joins and late seeks. This checks timeline behavior without
  retaining or rendering an entire long video.
* Exact decoded RGB24 video and PCM audio for a separately encoded 540 × 960,
  8 fps, three-second fixture: 24 frames in each output, with stream hashes and
  metadata retained.
* Propagation of an injected `KeyboardInterrupt` and matching source/audio/caption/
  final-clip cleanup groups.

The 540 × 960 raw and extracted encoded before/after previews were visually
inspected; the caption is readable and its placement is preserved. Previews remain in `/tmp` rather than
the repository. Deterministic RGB noise tests additionally expose subtle filter
and color differences that a visual inspection alone would miss; selected-path
channel differences are zero.

Independent review found that a standalone `is_mask=True` clip needed to bypass
the new path because MoviePy scales floating masks through 255 before resizing.
The guard was corrected before benchmarking and a regression test added. An
additional independent floating-point mask-gradient comparison is exact. Review
also corrected benchmark worker argument handling, trial enumeration, snapshot
provenance, and exception/cancellation cleanup before measured trials.

## Controlled benchmark method

The [benchmark driver](../scripts/benchmark_background_resize.py) reuses the
existing paired-render manifest loader, asset hashing, process-tree sampler and
full output verifier. It starts a fresh isolated worker for every render and
snapshots the optimized compiler before a batch, recording both compiler hashes.
Workers run serially. For each case, the order is **B1, O1, O2, B2**; each variant
therefore has two trials, with the second pair reversing order. No other
benchmark or test process runs concurrently. The desktop is not otherwise isolated.

There are eight matched cases:

| Duration | Density | Caption events | Source geometry |
|---|---|---:|---|
| 30 s | Low | 39 | Landscape resize / native control |
| 30 s | High | 77 | Landscape resize / native control |
| 60 s | Low | 80 | Landscape resize / native control |
| 60 s | High | 159 | Landscape resize / native control |

The narration, word timings, music and native background come from the same
cached fixtures as Milestones 1.1/1.2. The landscape fixture is prepared once by
scaling/center-cropping the first six seconds of that saved output into a
1920 × 1080 H.264 source; both variants receive that exact file and loop it. Native
controls receive the original 1080 × 1920 saved output. These are separate
workloads; differences between landscape and native rows are not optimization
comparisons. The input is a finished video with burned-in captions and mixed
audio, and configured music is mixed again. Clean synthetic media in the
independent validator supplements this limitation. The original clean production
footage and isolated narration are unavailable.

Every case uses 1080 × 1920 output, 30 fps, `libx264`, `medium`, `6500k`, 11 encoder
threads, AAC, the same narration/music configuration and identical caption events
within each comparison. No production concurrency or encoding setting changes.
The generated common landscape fixture's creation time and bytes are recorded
separately as input preparation, **not** renderer preprocessing. Neither renderer
preprocesses media, so its preprocessing cost is zero and compile wall time is
the complete renderer comparison, including setup, audio write, video write/mux
and cleanup. Imports are excluded from compile wall time and included in the
separately recorded worker wall/CPU/memory metrics.

Lightweight counters measure all Pillow resize calls and each fitted-background
frame function using both wall and Python process time. Counts are accumulated,
without retaining frames or per-frame timing arrays. The same instrumentation is
enabled in both variants and its overhead is included in the compile time.
Fitted-frame wall includes decoder pipe waits; its CPU includes conversions and
transform work but excludes decoder subprocess CPU. Resize CPU covers Pillow
calls, not NumPy conversion or cropping. These nested regions overlap and must
not be summed or presented as full-render speedups. The two-pass implementation
makes more individual Pillow calls while processing fewer invisible pixels.

The reused sampler records Python and FFmpeg subprocess CPU separately and
process-tree RSS/PSS at nominal 250 ms. CPU sums last observed process totals;
short-lived processes and final increments can be missed. RSS double-counts
shared pages; PSS apportions them. Sampling misses transient peaks. Temporary
intermediate disk measurements include encoded audio and exclude final MP4s and
input fixtures. Output size is recorded separately. `/tmp` is tmpfs, so these
temporary files also consume RAM. Verification runs after each measured worker:
full video/audio decode, duration, frame count, dimensions, FPS and audio stream
presence. Each output's complete MP4 SHA-256 is retained before temporary deletion.

## Before/after results

Measured on 2026-11-08 with Python 3.14.8, MoviePy 2.1.2, Pillow 11.3.0,
NumPy 2.4.4 and the recorded bundled FFmpeg build on the 12-logical-CPU machine.
[Raw trials and resource time series](benchmarks/milestone13-render.json) retain
asset/compiler hashes, configuration, environment, output hashes and decode
results. [Machine-readable summary](benchmarks/milestone13-summary.json) retains
every metric's two trial values, median, range and sample standard deviation.
The [summary script](../scripts/summarize_background_resize.py) regenerates it.

Each timing cell lists trial 1 / trial 2 in seconds. Reduction compares the two
medians; a negative reduction means a slower optimized median. Landscape and
native rows have different input geometry and must only be compared within a row.

| Workload | Baseline trials s | Optimized trials s | Median reduction |
|---|---:|---:|---:|
| 30 s, low, landscape | 130.08 / 124.07 | 79.90 / 80.72 | 36.80% |
| 30 s, low, native | 25.36 / 25.33 | 24.63 / 25.45 | 1.21% |
| 30 s, high, landscape | 128.44 / 132.72 | 86.93 / 85.98 | 33.79% |
| 30 s, high, native | 33.85 / 33.75 | 34.02 / 34.10 | −0.76% |
| 60 s, low, landscape | 253.41 / 245.08 | 157.93 / 152.82 | 37.66% |
| 60 s, low, native | 53.07 / 54.20 | 53.46 / 49.75 | 3.79% |
| 60 s, high, landscape | 263.14 / 259.32 | 165.19 / 166.63 | 36.49% |
| 60 s, high, native | 63.70 / 67.92 | 64.38 / 63.43 | 2.90% |

The following aggregate covers **only the four landscape workloads**. Time/CPU
rows sum their workload medians, representing one of each 30/60-second density
configuration; this is not an independently observed batch timer. Memory and disk
rows use the **maximum observed peak** across those trials, never summed peaks.
Differences are optimized minus baseline. RSS/PSS and disk units are MiB.

| Metric | Baseline | Optimized | Difference |
|---|---:|---:|---:|
| Total render wall s, including renderer preprocessing | 768.126 | 488.050 | −280.076 (−36.46%) |
| Sampled Python CPU s, including imports | 747.060 | 473.255 | −273.805 (−36.65%) |
| Sampled FFmpeg CPU s | 490.085 | 490.745 | +0.660 (+0.13%) |
| Peak tree RSS MiB | 2131.883 | 2086.414 | −45.469 (−2.13%) |
| Peak tree PSS MiB | 2074.698 | 2026.320 | −48.378 (−2.33%) |
| Fitted-background frame processing wall s | 642.071 | 373.792 | −268.279 (−41.78%) |
| Fitted-background frame processing Python CPU s | 623.159 | 360.816 | −262.343 (−42.10%) |
| Pillow resizing CPU s | 529.405 | 312.029 | −217.376 (−41.06%) |
| Peak intermediate disk MiB | 0.941 | 0.941 | 0.000 |
| Renderer preprocessing s | 0.000 | 0.000 | 0.000 |

The common input-fixture preparation took **2.83 s** once before both versions;
its bytes and hash are recorded. It does not fit production backgrounds in either
renderer. All renderer setup and first-frame fitting remain inside compile wall
time. The comparison therefore includes the optimization's entire preprocessing
overhead: none. Frame-processing and resizing measurements are nested diagnostic
regions, not additional savings to add to total wall time.

FFmpeg CPU work was essentially unchanged, consistent with preserving decoding,
audio and encoding while reducing Python work. The native controls show small
differences in both directions; they do not establish a native-footage speedup.
Two trials on an active desktop do not provide statistical confidence intervals.
The landscape wall ranges and per-trial CPU values in the summary expose the
observed variability instead of hiding it behind an isolated resize ratio.

## Peak memory and output parity

These are maxima of the sampled trial peaks for each version, in MiB:

| Workload | Baseline RSS | Optimized RSS | Baseline PSS | Optimized PSS |
|---|---:|---:|---:|---:|
| 30 s, low, landscape | 1954.50 | 1924.13 | 1902.48 | 1864.49 |
| 30 s, low, native | 1949.88 | 1957.38 | 1890.93 | 1898.47 |
| 30 s, high, landscape | 2008.38 | 1985.27 | 1950.21 | 1925.78 |
| 30 s, high, native | 1989.55 | 1982.71 | 1929.69 | 1922.81 |
| 60 s, low, landscape | 2023.61 | 1989.30 | 1966.38 | 1930.48 |
| 60 s, low, native | 2024.06 | 2028.86 | 1964.06 | 1967.23 |
| 60 s, high, landscape | 2131.88 | 2086.41 | 2074.70 | 2026.32 |
| 60 s, high, native | 2130.03 | 2117.17 | 2071.55 | 2058.22 |

All four landscape cases had lower observed RSS/PSS maxima. Native-control
differences are small and include both directions. This supports **no meaningful
memory regression in the tested workloads**, not a guaranteed RAM reduction for
every input. PSS coverage was complete in 11,586 of 11,610 samples (99.79%); the
remaining samples are explicitly incomplete. Sampling can miss transient maxima.

All **32 outputs / 43,200 video frames** passed the full decode/stream checks.
For each of the eight workloads, all four complete MP4 SHA-256 hashes match.
That is stronger than a decoded-video-only comparison for these encoded cases:
the encoded picture, audio and container bytes are identical. It does not alone
prove raw pre-encode RGB equality; the independent raw-frame tests and noise
experiments provide that separate evidence. No alternative resampling/color
deviation, caption-placement change or audio offset was accepted.

The independent final review recomputed every summary trial array, median,
minimum, maximum, sample standard deviation and aggregate difference directly
from the raw records, with zero discrepancies. It also checked all 32 probe
results, case hashes/sizes and both compiler hashes against the tested sources.

## Tests and compatibility scope

The final fresh `rtk python3 -m unittest discover` run passed **145 tests in
44.845 s**, exit 0; the saved
baseline passed 139. The six new tests characterize actual MoviePy output across
RGB geometry/seek cases, alpha/grayscale/non-`uint8` fallback, standalone mask
scaling, masked/legacy API fallback, and the tall-source path.
`rtk python3 -m py_compile shorts_core.py` and `git diff --check` pass. All four
new benchmark/validation/summary scripts also pass `py_compile`.
The existing CLI, Textual TUI, queue, cancellation/recovery, configuration and
publication tests remain part of the full suite. No live API, TTS, transcription,
download or upload was called; this milestone validates offline rendering.

## Known limitations

* Results apply to the recorded Python 3.14, MoviePy 2.1.2, Pillow 11.3.0 and
  bundled FFmpeg environment on this machine. MoviePy 1.x retains its existing
  path but was not installed for runtime validation. Pillow's separable RGB
  behavior is tested explicitly; rerun parity checks when changing Pillow.
* Two trials per variant on an active desktop establish observations with
  variability, not a universal speedup or statistical confidence interval.
  CPU utilization can stay near one core while Python produces frames; the
  optimization reduces total CPU work and elapsed time rather than promising a
  lower instantaneous utilization percentage.
* The native controls do not exercise resize work and should not be interpreted
  as a resize speedup. The landscape fixture is one looped source, not the
  original clean six-reader production workload. Multi-source timeline behavior
  is checked separately with synthetic media; full six-reader peak memory is
  not established.
* The selected path remains Python/Pillow based. It still decodes full source
  frames and computes the full horizontal resize before discarding columns.
  Portrait/tall fits do not receive this optimization. No new experimental
  resampler or lossy intermediate is enabled.
* During validator development, an unchanged-baseline encode with one-second
  music looped over three-second narration raised a MoviePy audio-reader
  `IndexError`. The encoded parity fixture uses music spanning its duration;
  raw audio chunk comparisons still exercise the shorter looping music.
  This pre-existing audio issue was not modified in a resize milestone.

## Recommended next optimization

Investigate **bounded reuse of the last fitted frame when decoding returns the
same unchanged source frame**, especially for 24/25 fps footage rendered at
30 fps. The installed FFmpeg reader already returns `last_read` when a request
addresses its current decoded frame, but lazy transformations can still repeat
their work. A one-frame cache could reuse that fitted result without buffering a
video. Before adopting it, test immutable versus mutable frame buffers,
non-monotonic seeks, joins, loops, reader cleanup and the memory cost per source;
benchmark mismatched source/output FPS against matched-FPS controls. No such cache
or speedup is included or claimed in this milestone.

## Reproduce

The [reverse patch](benchmarks/milestone13-baseline.patch) reconstructs the exact
Milestone 1.2 reference from this milestone's recorded optimized compiler without
changing the working tree. It was applied to a temporary copy and its SHA-256
matched the baseline hash above. Use a trusted compiler source: benchmark source
arguments execute that Python module.

```bash
rtk proxy cp shorts_core.py /tmp/slopengine-m13-reference.py
rtk proxy patch /tmp/slopengine-m13-reference.py docs/benchmarks/milestone13-baseline.patch
rtk proxy python3 scripts/experiment_background_resize.py --runs 8 --random-cases 300 --result /tmp/m13-resize-experiment.json
rtk proxy python3 scripts/validate_background_resize.py --reference /tmp/slopengine-m13-reference.py --result /tmp/m13-parity.json
```

Use the same cached media files (or record new hashes and treat them as a new
workload). The wrapper cleans its prepared narration/manifest automatically;
the benchmark cleans generated landscape input, worker outputs and encoded audio
automatically. JSON results and explicitly generated preview PNGs are retained.

```bash
rtk proxy bash -c 'set -eu
benchmark_dir=$(mktemp -d /tmp/slopengine-m13-reproduce-XXXXXX)
trap '\''rm -rf "$benchmark_dir"'\'' EXIT
python3 scripts/prepare_cached_profile.py --video outputs/1792592944709041190-forest-render-validation.mp4 --metadata outputs/1792592944709041190-forest-render-validation.metadata.json --music ost.mp3 --folder "$benchmark_dir"
python3 scripts/benchmark_background_resize.py --manifest "$benchmark_dir/manifest.json" --baseline-source /tmp/slopengine-m13-reference.py --run --trials 2 --sample-interval 0.25 --result /tmp/m13-render.json'
rtk proxy python3 scripts/summarize_background_resize.py --input /tmp/m13-render.json --result /tmp/m13-summary.json
```

The default command runs 32 serial full-resolution renders and complete output
verification. `--geometries resize-heavy` or `--geometries native-control` selects
a subset; `--durations` and `--densities` can select narrower diagnostics. The
separate profiled diagnostic uses the existing infrastructure:

```bash
rtk proxy python3 scripts/benchmark_render.py --label m13-baseline --duration 4 --trials 1 --jobs 1 --threads 11 --profile --compiler-source /tmp/slopengine-m13-reference.py --result /tmp/m13-baseline-profile.json
rtk proxy python3 scripts/benchmark_render.py --label m13-optimized --duration 4 --trials 1 --jobs 1 --threads 11 --profile --result /tmp/m13-optimized-profile.json
rtk python3 -m unittest discover
rtk python3 -m py_compile shorts_core.py
```
