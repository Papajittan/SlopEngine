# Milestone 1.1: offline render profiling

Measured on 2026-10-27 on the machine described in [Phase 1](phase1-performance.md), with Python 3.14 and MoviePy 2.2.1. The working compiler hash was `0d84f1ade42ad470de5cacc27aab24222edfb9db88465ae03a376b855bd27457`. Each cached-media case was run once, serially; the machine was not isolated. Treat timing differences as observations, not established speedups. The [raw results and time series](benchmarks/milestone11-cached.json) include asset hashes, sample coverage, stage markers, CPU, memory, disk and decoded-stream checks.

## Workloads and limits

The 30- and 60-second cases use audio extracted from the saved 138-second forest production MP4, that video's original word timings, the saved video as a background, and local `ost.mp3` as a second music source. The input contains real speech and footage but is a **finished, already captioned and mixed output**: music is mixed twice, and old captions remain burned into the background. The original six footage files and isolated narration were cleaned up. Consequently this exercises the compiler with realistic speech timing and changing images, but it cannot validate visual/audio fidelity to the original job or six-reader memory. No API, TTS, Whisper or download is called. One source reader was used. The stride-2 cases omit every second caption; speech and other media are unchanged. They probe sensitivity to layer count and deliberately differ in visual content.

All four cases used 1080 × 1920, 30 fps, libx264 medium at 6500k, 11 encoder threads and AAC. Each output was fully decoded and checked for dimensions, fps, duration and frames. Captions were not visually validated against clean original media. PSS was available; peaks below use complete samples. Memory categories are independent maxima, which need not occur simultaneously. Temp disk is active benchmark output and intermediate files, excluding input fixtures and completed workers. `/tmp` is tmpfs, so disk use also consumes memory.

| Duration | Captions | Compile wall s | Tree peak RSS MiB | Tree peak PSS MiB | Python peak RSS MiB | FFmpeg peak RSS MiB | Python CPU s | FFmpeg CPU s | Peak temp MiB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 30 s | 77 | 32.41 | 1,980 | 1,925 | 860 | 1,121 | 31.44 | 91.79 | 25 |
| 30 s | 39 | 25.21 | 1,944 | 1,887 | 811 | 1,144 | 24.38 | 92.59 | 26 |
| 60 s | 159 | 61.58 | 2,114 | 2,056 | 980 | 1,138 | 57.03 | 209.67 | 57 |
| 60 s | 80 | 50.43 | 2,026 | 1,969 | 873 | 1,158 | 45.48 | 222.92 | 57 |

The 60-second full-density case had 2.65 caption events per second. In that case Python's independent peak RSS was 107 MiB higher with 159 than with 80 captions, while FFmpeg's peak was similar. The full-density compile took 11.15 seconds longer, but each case ran once in a fixed order and changed the visible captions; caching and load can affect timing. This is evidence to investigate caption-layer scaling, not a promise that halving captions produces the observed speedup. FFmpeg CPU includes all observed FFmpeg readers, audio and encoder processes; it is not encoder-only CPU. CPU totals are the last observed process values, so short-lived activity and final increments can be missed.

## Stage observations

For the 60-second full-density run, stage markers aligned to the common monotonic clock give the following sampled peaks. The 250 ms nominal interval and sampling overhead make short-stage peaks especially incomplete; a dash means no sample landed in that stage, not zero consumption. RSS values are process-tree totals.

| Region | Wall s | Samples | Peak tree RSS MiB | Peak tree PSS MiB |
|---|---:|---:|---:|---:|
| Background setup | 0.265 | 1 | 686 | 669 |
| Caption layer preparation | 0.810 | 3 | 1,158 | 1,135 |
| Audio clip setup | 0.067 | 0 | — | — |
| MoviePy write, including composition, audio encoding, FFmpeg pipe waits and mux | 60.290 | 204 | 2,114 | 2,056 |
| Entire compile | 61.579 | 208 | 2,114 | 2,056 |

These regions overlap and are not additive. Audio setup does not measure the deferred audio mix/encode inside `write_videofile`; composition and encoding also overlap. This run does not isolate encoder wall time or audio mixing CPU. Process-tree RSS double-counts shared pages; PSS allocates them proportionally, and incomplete PSS samples are marked as such. Each sample is an approximate snapshot and can miss transient subprocesses and disk files.

An additional **separate cProfile diagnostic** used 30 seconds of synthetic tones and alternating one-second landscape/portrait test sources, 120 caption layers, at 1080 × 1920/30 fps. It rendered 900 frames in 106.97 seconds under profiling. MoviePy's frame iteration accumulated 89.11 seconds, and Pillow's underlying resize accumulated 44.05 seconds across 454 calls; `playing_clips` accumulated 18.15 seconds. Writer `write_frame` accumulated 13.90 seconds including serialization and pipe waits. These cumulative times overlap and include profiler overhead; they are not independent CPU totals or predicted savings. Its short looping sources exaggerate reader restarts relative to continuous footage. See [profile data](benchmarks/milestone11-synthetic-profile.json). The prior harness warm-up unintentionally rendered the full narration with four captions; it has now been fixed to use a one-second narration. The warm-up was not included in the profiled timing.

## Comparison and priorities

Phase 1 measured three 8-second synthetic 540 × 960/24 fps runs, 32 captions and two threads; its optimized median was 1,010,851,840 bytes tree RSS and 7.477 seconds compile wall. A repeat on the same compiler and settings gave 1,011,662,848 bytes and 9.146 seconds for one run. The new full-resolution cached workloads are longer, have speech, music, different source images and 11 threads. Their larger memory and duration are **not** an optimization regression or a controlled before/after comparison. Phase 1's measured 6.2% RSS improvement applies only to its matched inputs. The earlier 138-second, six-reader production observation reached 5.99 GiB RSS; it too is a different workload.

Ranked next experiments, without changing the renderer in this milestone:

1. **Reduce per-frame caption-layer selection work.** Full-density cases have higher Python CPU and wall time than their sparse counterparts, and the separate diagnostic spent 18.15 cumulative seconds selecting active clips. Test an indexed active-caption approach with identical word timings and frame/audio equality, repeated and alternated against the current compiler. Do not remove captions to obtain the gain.
2. **Reduce repeated full-frame resizing.** Pillow resize consumed 44.05 cumulative seconds in the resize-heavy diagnostic. Test preprocessing/caching or an equivalent fit path with matched media and visual parity; the continuous native-size cached video does not establish the benefit for all footage.
3. **Measure and reduce FFmpeg reader/encoder resource use.** FFmpeg accounted for roughly 1.1 GiB peak RSS and 210 sampled CPU seconds in the 60-second full case. Split readers from encoder and repeat a real six-source run before tuning threads, decoder lifetime or encoder settings; this measurement does not establish which FFmpeg process dominates.
4. **Reduce retained caption memory and eager model imports.** The density comparison shows a 107 MiB Python peak difference at 60 seconds; the shorter Phase 1 comparison already demonstrated benefit from cropped caption assets. Measure longer clean inputs and imports separately before changing retention or lazy-loading behavior.

The render limit remains one. Do not infer safe multi-job capacity from this one-reader benchmark. TTS, Whisper, Gemini and download stages were not profiled; no inference-stage, network or end-to-end claim is made.

## Reproduce

Requires local saved MP4, its metadata, local music, FFmpeg/ffprobe, psutil and project dependencies. The saved output used here is ignored by Git; obtain and retain suitable local media to reproduce it. These commands create only temporary fixtures and clean them on exit; no services or credentials are used. Change the paths to your own cached assets if necessary and retain their hashes when comparing runs.

```bash
rtk proxy bash -c 'set -eu
work=$(mktemp -d /tmp/slopengine-profile-XXXXXX)
trap '\''rm -rf "$work"'\'' EXIT
python3 scripts/prepare_cached_profile.py --video outputs/1792592944709041190-forest-render-validation.mp4 --metadata outputs/1792592944709041190-forest-render-validation.metadata.json --music ost.mp3 --folder "$work"
python3 scripts/benchmark_paired_render.py --manifest "$work/manifest.json" --run --single --caption-strides 1 2 --result docs/benchmarks/milestone11-cached.json'
```

The original diagnostic can be repeated with `rtk python3 scripts/benchmark_render.py --duration 30 --trials 1 --jobs 1 --threads 11 --profile --result /tmp/slopengine-profile.json`. Its sources and audio are synthetic. Profiled and unprofiled wall times should not be compared as equivalent.
