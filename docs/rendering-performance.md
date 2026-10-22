# SlopEngine rendering performance

Follow-up: [real-workload validation](real-workload-validation.md) completed one
138-second API-backed production video at 20.94 FPS and confirmed a 2.83×
writing speedup on matched real assets. Its 5.99 GiB peak RSS means the synthetic
two-slot recommendation below remains experimental; keep one enabled job until
resource admission and paired full-length testing are in place.

Investigation on 2026-10-21. This measures an offline synthetic workload on the
local laptop; it does not reproduce the screenshot's particular assets or its
complete API/Whisper pipeline. Existing uncommitted TUI/pipeline work was retained.

## Architecture and defaults

`ShortsPipeline.run` performs Gemini shaping → Edge TTS → Whisper word timestamps
→ Pexels footage downloads → `MoviePyShortCompiler.compile` → atomic publication
of the MP4 and metadata. The TUI launches an independent Python worker and consumes
events. `JobManager` currently permits one job; this investigation does not change
that policy, the theme, or the UI.

The compiler reads narration duration, opens every background with audio disabled,
fits each to 1080×1920 using Pillow Lanczos resize and center crop, concatenates
sources, and loops/trims to the maximum of narration duration, caption end, and
one second. Sources are decoded by FFmpeg readers, with frame access driven by
MoviePy. Sequential frames reuse the reader; returning to the beginning of a loop
can restart/seek decoding. Sources remain open for the render. There is no
intermediate background encode or cache of scaled frames.

Each valid word creates one PIL RGBA caption image and one timed `ImageClip`.
There is no `TextClip` or per-frame font rasterization. Captions use uppercase
text, configured font/stroke/color, horizontal centering, configured vertical
position, and the existing minimum 50 ms display duration. MoviePy checks active
layers every frame; overlapping words remain separate layers. All caption images
and float masks stay resident until completion. There are no custom animated
per-frame effects.

Narration and optional looped music are mixed through `CompositeAudioClip`; music
volume is applied before AAC encoding. The video writer receives frames through
a Python pipe. Defaults are 30 FPS, `libx264`, `medium`, `6500k`, AAC, and
`max(1, os.cpu_count()-1)` encoder threads: 11 here. Codec, preset and bitrate
were not changed in production. The output is H.264 High, yuv420p, 1080×1920.
For the screenshot, 3,759 frames at the default FPS would represent 125.3 seconds;
its reported elapsed generation time is not a controlled render measurement.

Normal pipeline assets live in a unique `/tmp/shorts_core` run directory and
are cleaned in `finally` unless retained by configuration. Final output and its
temporary AAC encode are written in a unique hidden destination staging directory,
then exclusively published; that staging directory is also cleaned. The compiler
closes final, audio, caption and opened source clips in `finally`. Benchmarks
use their own automatically cleaned system temporary directory and never publish
or upload anything. Explicit preview copies under `/tmp` are retained for review.

## Hardware and measurement method

* AMD Ryzen 5 5600H: 6 physical cores / 12 logical CPUs.
* 27.25 GiB installed RAM; roughly 16–17 GiB available during initial checks.
* NVIDIA RTX 3050 Laptop GPU, 4 GiB VRAM; driver 615.78.08.
* Python 3.12.15, MoviePy 2.1.2 in `.venv312`.
* MoviePy uses bundled imageio FFmpeg 7.0.2. Its encoder list has `libx264`
  but no NVENC. System FFmpeg is n9.0.2 and a real `h264_nvenc` encode probe
  succeeded. Merely finding the encoder name was not treated as proof of support.
* `/tmp` is tmpfs. Physical storage I/O counters are zero for this workload;
  this says nothing about throughput on a slow disk holding real downloaded clips.

The fixed workload is four seconds / 120 frames at full 1080×1920 and 30 FPS.
Two local H.264 `testsrc2` sources, 1920×1080 and 1080×1920, each one second,
are joined and repeated twice. There is one active background plus normally one
caption, with 16 word clips, configured 220 px font, 10 px outline, and a 993×495
caption canvas. Deterministic 44.1 kHz 440 Hz narration and 220 Hz music are mixed
at the default 0.12 music volume. No credentials or environment configuration
are loaded by the benchmark. The local application's music volume/device
overrides are therefore not silently included in these measurements.

One complete warm-up render precedes each batch series. Each measured trial runs
fresh worker processes, just as independent generation jobs would. The inner
`compile` timer includes duration probing, setup, audio encode, frame generation,
video encode/mux and cleanup; it excludes Python imports and source creation.
Batch time also includes worker startup/imports and output probing. CPU/RSS
sampling at about 200 ms covers the batch's entire process tree; GPU sampling
is about once per second and is device-wide, including desktop activity.
RSS sums can double-count shared pages. CPU uses 100% per logical CPU (1200%
is the machine's logical-core ceiling). Short-lived subprocesses between samples
can be missed. Logical I/O includes FFmpeg pipes and is not physical disk traffic;
its recorded peak counters are not exact lifetime totals.

Three profiled baseline/final trials use identical media, 11 threads, preset and
bitrate. `cProfile` times are overlapping call-tree measurements, not additive
stage percentages. Both sets have the same profiling overhead. Additional
concurrency/encoder experiments run without cProfile. This is an active desktop,
not an isolated or thermally controlled benchmark server.

## Before and after

| Metric | Before | After |
| --- | ---: | ---: |
| Compile wall time, mean ± sample standard deviation | 28.35 ± 0.21 s | 12.42 ± 0.21 s |
| Compile wall time, min–max | 28.11–28.50 s | 12.22–12.63 s |
| Effective output FPS, 120 / mean time | 4.23 | 9.66 |
| Mean sampled process-tree CPU, includes startup | 117% | 150% |
| Peak sampled process-tree CPU, includes startup | 605% | 563% |
| Peak aggregate RSS | 2.15 GiB | 1.86 GiB |
| Device-wide GPU utilization in recorded samples | 0–17% | 0% |
| Device-wide GPU memory, includes desktop | 51 MiB | 51 MiB |
| Physical disk reads / writes in tmpfs | 0 / 0 bytes | 0 / 0 bytes |
| Output size | 3,806,649 bytes | 3,806,649 bytes |
| Video duration / resolution / FPS | 4 s / 1080×1920 / 30 | 4 s / 1080×1920 / 30 |
| Output frames | 120 | 120 |

**Speedup = baseline time / optimized time = 28.3463 / 12.4239 = 2.28×**,
a 56.2% reduction in compile wall time. The smaller duration with higher average
CPU reflects less time spent in serial frame processing, not saturation of all
12 logical CPUs. The initial 2.45× estimate used the first baseline/final series;
the table uses the final repeated controlled series after the edge-case fix.

The baseline is the working-tree compiler saved before modifications, not the
repository's older HEAD (which predates the user's pending TUI changes). Its
SHA-256 is recorded in `validation.json`. Raw data are in
[`benchmarks/baseline.json`](benchmarks/baseline.json) and
[`benchmarks/optimized.json`](benchmarks/optimized.json). The intermediate pass
is retained in `benchmarks/intermediate.json` to show the diagnostic progression.

## Bottlenecks and changes

| Profile region, mean cumulative time | Baseline | Final |
| --- | ---: | ---: |
| Frame iteration, excluding encoder writes | 20.13 s | 9.26 s |
| Pillow resize wrapper | 7.00 s | 6.90 s |
| Full-frame mask composition | 3.64 s | Absent |
| PIL alpha compositing | 3.25 s | 0.29 s |
| Encoder `write_frame` including serialization/pipe waits | 0.52 s | 1.76 s |

These regions overlap. The biggest baseline issue was unnecessary full-frame
compositing, float-mask operations, array conversions and RGBA serialization;
Python/Pillow frame production starved the encoder. More encoder threads alone
cannot address this. The slower writer calls after optimization are consistent
with feeding the encoder faster; they include NumPy serialization and blocking
on the pipe and are not a measurement of FFmpeg CPU time.

Implemented targeted changes in `MoviePyShortCompiler`:

1. Chain fitted backgrounds of equal dimensions instead of wrapping them in
   another `CompositeVideoClip`. Compare dimensions as tuples because MoviePy
   mixes lists and tuples. Retain composition for genuinely mismatched sizes.
2. Use the opaque fitted background directly in the final composite, avoiding
   the redundant full-frame mask and RGBA writer input. Explicit black background
   preserves the no-caption case: MoviePy 2.1 cannot use `use_bgclip` with zero
   overlays. Caption transparency itself remains intact.
3. Return already correctly sized footage directly, avoiding a no-op resize.
4. Resolve font candidates once per compiler and cache loaded fonts by size.
   Baseline font loading/discovery took about 2.14 seconds for 16 words, largely
   repeated `fc-match` subprocesses; final total word-layer setup is about 0.16 s.

The first pass improved 29.38 s to 23.85 s, but its profile still showed masks:
list/tuple geometry comparisons had selected the fallback composite. Correcting
that condition removed the second composite. Final results above include the
no-caption regression fix and a fresh baseline repeat with the same resource
sampler. No filter, visual dimensions, timing rule or encoding-quality default
was relaxed. MoviePy's documented behavior corroborates the opaque-background
choice: [CompositeVideoClip source](https://zulko.github.io/moviepy/_modules/moviepy/video/compositing/CompositeVideoClip.html).

## Concurrency and encoder experiments

The concurrency series uses the optimized compiler, bundled FFmpeg, four
encoder threads per worker, no profiler, two trials per setting, and the same
four-second workload. Each worker owns independent readers, audio, captions and
encoder processes. Setup and probe overhead are included in batch throughput.

| Active renders | Mean compile latency/video | Mean batch time | Short clips/hour | Peak RSS, all workers | Mean CPU | Peak CPU | Failures |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 12.31 s | 16.18 s | 222 | 1.71 GiB | 149% | 410% | 0 |
| 2 | 14.05 s | 18.34 s | 393 | 3.40 GiB | 296% | 701% | 0 |
| 3 | 16.62 s | 21.00 s | 514 | 5.06 GiB | 428% | 846% | 0 |

Two workers improve batch throughput 1.77×, and three improve it 2.31× relative
to one, while worsening each video's latency. GPU samples remain 0% / 51 MiB.
These are **four-second synthetic clips/hour**, not a forecast for full-length
generated videos. There is CPU headroom for multiple processes; serial Python
frame production limits one render even when most machine cores are idle.
The tested four-thread setting has similar single-render latency to the
11-thread default (12.31 s unprofiled versus 12.42 s profiled; the small difference
cannot be attributed solely to thread count).

Recommended policy for this laptop:

* Keep the current **one active generation** behavior until a stage-aware queue
  exists. A future queue can admit **two active generation jobs**, serialize
  Whisper/model loading, and allow at most **two render stages** simultaneously.
  API concurrency is separate from rendering and was not tested here.
* Use **four FFmpeg encoder threads per render** for that future two-render
  policy. This setting was measured; production defaults were left intact.
  Decoder thread counts are separate and currently automatic.
* Three concurrent renders were stable and fastest in this short test, but are
  **not the default recommendation for long jobs**. Hundreds of resident word
  images and additional background readers can consume several extra GiB per
  worker. Validate a real long-job RSS peak before permitting three.
* Gate starts by available memory, reserving at least 4 GiB for the desktop and
  headroom, plus each new job's measured/estimated incremental peak with a 25%
  margin. Include caption count, source count/resolution and any live Whisper
  model. Pause admission on swap growth or sustained CPU contention; queue work
  rather than spawning more workers. This scheduling policy is a recommendation,
  not an untested scheduler implementation.

The hardware comparison uses **system FFmpeg for both cases**, four threads,
two trials each, identical compiler inputs, 6.5 Mb/s target bitrate and yuv420p.
NVENC's preset is `p4`; x264's is `medium`. Preset names and target bitrate
do not establish equivalent perceptual quality across encoders.

| Encoder experiment | Mean compile time | Range | Peak GPU utilization | Device GPU memory peak | Output bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| System FFmpeg x264 medium | 13.63 s | 13.50–13.77 s | 0% | 51 MiB | 3,766,717 |
| System FFmpeg NVENC p4 | 12.96 s | 12.83–13.09 s | 20% | 338 MiB | 4,472,953 |

NVENC gains only **1.05×** over the same system binary's software encoder here,
and emits a larger file. Both remain dominated by CPU frame production. This
small two-trial gain, without a perceptual-quality comparison, does not justify
changing the production encoder. Bundled x264 medium with four threads already
measured 12.31 s, faster than either system-binary experiment; changing FFmpeg
builds is a separate confounding variable. NVENC is an explicit benchmark option,
not a new automatic production backend. GPU encoding does not accelerate the
existing Python/Pillow composition.

With bundled FFmpeg and four threads, x264 `fast` measured 12.42 s mean
(12.33–12.52 s, two trials), versus `medium` at 12.31 s. There is no measured
speed benefit here, and preset changes can alter quality at the same bitrate.
The default remains `medium`. Full per-experiment JSON, including encoder
versions and stream metadata, is alongside the baseline results.

## Correctness and limits

[`benchmarks/validation.json`](benchmarks/validation.json) records comparison
against the saved original compiler. All 120 raw RGB frames are exactly equal
(maximum channel difference 0). All 176,400 mixed audio samples are exactly equal.
The special comparison includes gaps, overlapping words, a 10 ms event exercising
the existing 50 ms floor, a caption crossing a source join, and loop boundaries.
Both implementations handle empty captions plus missing-footage fallback.

The separately encoded regular benchmark files fully decode without FFmpeg
errors. All 120 decoded frame checksums and decoded PCM audio match exactly,
with 4.0-second video, 30/1 FPS, 1080×1920 and matching audio stream timing.
An extracted encoded frame and a raw comparison frame were visually inspected:
white text, dark outline, placement and legibility are preserved. Preview files
are under `/tmp/slopengine-validation`, not tracked. This proves equivalence for
the tested cases, not for every possible source video, font or MoviePy version.

Final verification: `rtk python3 -m unittest discover` passed **72 tests**, including
the Textual TUI tests; Python compilation and `git diff --check` passed. New
regression checks cover size-list/tuple matching, composition fallback, no-op
resize and font reuse. Stream metadata for every retained trial was checked for
120 frames, 1080×1920, 30 FPS, four-second audio/video durations and zero start
offsets. The reverse baseline patch was applied to a temporary copy and its
SHA-256 matched the recorded original. MoviePy 1.x compatibility was not runtime
tested; the tested media environment uses MoviePy 2.1.2.

No live Gemini, TTS, Whisper inference, Pexels, or upload calls were made. Those
stages' runtime behavior and complete API-backed generation were not verified.
The local environment overrides Whisper to CUDA; the code's CPU default remains
unchanged. Rendering here uses no Whisper model or GPU compute. Generation
concurrency must separately account for Whisper model memory and API quotas.

## Further opportunities, ranked

1. **Move static background fitting out of the frame loop.** Final Pillow resize
   still consumes about 6.9 s of a 12.4 s render, predominantly enlargement of
   landscape footage before cropping to portrait. Test FFmpeg decode-side scaling
   or a reusable fitted intermediate. Preserve crop coordinates and evaluate
   Lanczos/color differences, I/O and total latency before adopting it. No such
   quality change is included here.
2. **Trim/copy caption rasters and share repeated glyph assets.** Captions already
   rasterize once, but a 993×495 RGBA backing array plus float64 mask can retain
   roughly 5.6 MiB per word. 500 words can therefore add roughly 2.7 GiB before
   decoder/encoder/import memory (an allocation estimate, not a measured long-job
   peak). Preserve exact positioning with crop offsets, preserve overlap, and
   benchmark a long narration before raising concurrency. Raster caching should
   be bounded; caching every unique word indefinitely would worsen memory.
3. **Lazy/open fewer source readers and cap decoder threads.** Every downloaded
   clip is currently opened before rendering. More sources or higher-resolution
   Pexels footage can increase RSS/decoder contention beyond this two-source test.
   Source count and long-loop decoding need a separate representative benchmark.
4. **Consider an optional FFmpeg composition backend after these measurements.**
   It could avoid Python raw-frame pipes and repeated full-frame allocations.
   Caption alpha/font metrics, word timing, music looping, colors and metadata
   publication must match the existing implementation. The current measurements
   justify investigating it, not replacing the working backend without that test.

## Reproducing

Use the existing environment containing the project's media dependencies:

```bash
rtk proxy .venv312/bin/python scripts/benchmark_render.py --label optimized --profile --result docs/benchmarks/optimized.json --keep-media /tmp/slopengine-after
rtk proxy .venv312/bin/python scripts/benchmark_render.py --label concurrency-2 --trials 2 --threads 4 --jobs 2 --result docs/benchmarks/concurrency-2.json
```

For a new before/after experiment, first save the actual starting `shorts_core.py`
under `/tmp` and run the same command with `--compiler-source /tmp/reference.py`
and a different result filename, then change rendering code and rerun without
that option. The saved pre-change source from this investigation remains at
`/tmp/slopengine-render-baseline.py` for this session. To reconstruct it from the
matching optimized compiler snapshot without changing the working tree, use:

```bash
rtk proxy cp shorts_core.py /tmp/slopengine-render-baseline.py
rtk proxy patch /tmp/slopengine-render-baseline.py docs/benchmarks/baseline-compiler.patch
```

The reverse patch contains only this investigation's compiler changes. The source
hash must match `baseline.json` for an exact reproduction. The optional source
argument must point to trusted Python code. To compare every raw frame, mixed
audio and encoded output:

```bash
rtk proxy .venv312/bin/python scripts/validate_render.py --reference-core /tmp/slopengine-render-baseline.py --media /tmp/slopengine-after --before-video /tmp/slopengine-before/0.mp4 --after-video /tmp/slopengine-after/0.mp4
rtk python3 -m unittest discover
rtk python3 -m py_compile shorts_core.py
```

JSON is the durable artifact; temporary videos are deliberately not committed.
Use fresh result/preview names if retaining an earlier experiment. Diagnostics
skip missing NVIDIA tooling; Linux process sampling is specific to this host.

Explicit encoder comparisons (these alter only the benchmark):

```bash
rtk proxy env FFMPEG_BINARY=/usr/bin/ffmpeg .venv312/bin/python scripts/benchmark_render.py --label system-x264 --trials 2 --threads 4 --result docs/benchmarks/system-x264.json
rtk proxy env FFMPEG_BINARY=/usr/bin/ffmpeg .venv312/bin/python scripts/benchmark_render.py --label system-nvenc --trials 2 --threads 4 --codec h264_nvenc --preset p4 --result docs/benchmarks/system-nvenc.json
```
