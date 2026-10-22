# Real SlopEngine workload validation

2026-10-21, on the same Ryzen 5 5600H / 27.25 GiB RAM / RTX 3050 4 GiB laptop
as the [synthetic investigation](rendering-performance.md).

**The rendering improvement translates to a real workload.** One complete,
API-backed generation rendered at **20.94 FPS** with unchanged production quality
settings. A separate controlled comparison on its actual cached inputs showed
**2.83× faster video writing**, with identical decoded frames and audio.

**Keep production concurrency at one for now.** This job peaked at **5.99 GiB
aggregate process-tree RSS**, substantially above the short synthetic benchmark.
The optimizations are suitable for a guarded scheduler implementation, but two
full-length simultaneous jobs still need validation before enabling parallel
generation. No concurrency or TUI changes were implemented.

## What was reviewed and run

The renderer's SHA-256 exactly matches the previously measured optimized source.
The review covered opaque background composition, equal-size chaining (including
list/tuple size normalization and the mismatched-size fallback), exact-size
resize bypass, font caching, empty-caption behavior, timing and cleanup.
Those changes leave codec, bitrate, preset, output dimensions, FPS, caption
placement/style and audio mixing intact. No additional rendering optimization
was made during this validation.

Exactly **one** complete generation called the normal Gemini, Edge TTS, Whisper
and Pexels pipeline. Topic: “How a forest changes through the four seasons.”
The generated title is “The hidden drama of a forest's changing year 🍁”.
Credentials came from existing configuration; their values were not logged.
Environment settings and saved TUI preferences were loaded normally. Upload was
explicitly disabled, and no uploader was called.

The workload contained **337 timed words**, six real Pexels videos and background
music at the configured 0.1 multiplier. The first source was 720×1280 at 29.97 FPS;
the other five were 1080×1920 at 23.976, 24, 59.94, 30 and 30 FPS. Only the first
source needed enlargement. This differs materially from the synthetic workload,
which repeatedly enlarged landscape footage to portrait height.

Production settings:

| Setting | Actual value |
| --- | --- |
| Output | 1080×1920, 30 FPS |
| Video | libx264, medium, target 6500k, 11 encoder threads |
| Actual encoded format | H.264 High, yuv420p, about 6.70 Mb/s |
| Audio | AAC, 44.1 kHz stereo, about 130 kb/s |
| Captions | DejaVu Sans Bold, configured 220 px, white, 10 px dark outline, vertical ratio 0.5 |
| Narration | en-US-AriaNeural; rate +7%, pitch -3Hz, volume +0% |
| Whisper | base, configured CUDA; inference completed without CPU fallback |
| Media environment | Python 3.12.15, MoviePy 2.1.2, bundled FFmpeg 7.0.2 |

The actual FFmpeg command and reader commands are recorded in
[`benchmarks/production.json`](benchmarks/production.json). The writer used an
RGB24 pipe; the final composite and concatenated background had no redundant
full-frame mask. MoviePy's writer requested `yuva420p` for x264; FFmpeg resolved
the final stream to yuv420p, verified with ffprobe. That existing writer behavior
was not changed.

## Full-generation measurements

| Metric | Measured result |
| --- | ---: |
| Complete generation, including imports and 0.14 s diagnostic copying | 251.20 s |
| Render/compile duration, including setup, audio encoding and cleanup | **198.27 s** |
| Output frames | **4,152** |
| Effective rendering speed, frames / compile duration | **20.94 FPS** |
| Frame-loop speed, excluding setup and final flush | 21.68 FPS |
| Mean render CPU | 384.8% |
| Peak sampled render CPU | 848.1% |
| Peak render process-tree RSS | **5.99 GiB** |
| Minimum system MemAvailable | 11.41 GiB |
| Render GPU compute / encoder / decoder utilization | 0% / 0% / 0% |
| Device-wide GPU memory during rendering | 173 MiB |
| Whole-generation GPU peak, including Whisper | 87% utilization / 719 MiB device memory |
| Output size | 118,380,423 bytes |
| Video duration | 138.400 s |
| Audio duration | 138.410 s |

CPU percentages use 100% per logical CPU; this machine has 12 logical CPUs.
Resources were sampled about every 250 ms, with device-wide GPU queries about
once per second. Aggregate RSS can double-count shared pages. GPU memory includes
desktop/context usage, not just this process. The active desktop and variable
footage make these observational measurements, not an isolated-machine benchmark.

Pipeline stage times were: Gemini **13.02 s**, narration **15.45 s**, Whisper
**6.69 s**, Pexels search/download **14.99 s**, rendering **198.27 s**, and final
publication below 0.01 s. The render-stage event includes an additional 0.14 s
diagnostic copy after the measured render timer stopped. Normal temporary cleanup
remained enabled. Captured inputs and offline comparison videos were removed
automatically; the completed video, normal metadata and review PNGs remain in
`outputs/`.

### Comparison with the earlier 3.32 FPS observation

20.94 / 3.32 = **6.31× higher observed FPS**. This is a useful operational result,
but **not a causal 6.31× optimization claim**: the earlier number was an
approximate progress reading from another job, with different media, rather
than a whole-render average on these same inputs. The new video's length is
138.4 s; the earlier 3,759-frame workload would be about 125.3 s at 30 FPS.

The controlled comparison below establishes causality without making any more
generation API calls.

## Controlled comparison on the real inputs

After the real generation, the captured narration, all six sources, configured
music and **all 337 word layers** were reused. The original compiler was restored
only in a temporary module using the recorded reverse patch; its source hash
matched the earlier baseline. Neither the working renderer nor user assets were
modified.

Two sequential, alternating trials per compiler wrote the **same first six
seconds / 180 frames**, at the same resolution, FPS, bitrate, preset and 11
threads. Both were profiled. Keeping every caption in setup represents the real
project's preparation cost, while bounding video writing avoids another pair
of full-length renders.

| Mean metric | Original compiler | Optimized compiler |
| --- | ---: | ---: |
| Compile including full-project setup | 76.46 s | 17.80 s |
| Compile min–max | 76.00–76.91 s | 17.34–18.26 s |
| Video writing including six-second audio encode | 38.98 s | 13.78 s |
| Video-writing min–max | 38.57–39.39 s | 13.35–14.22 s |
| Writing throughput, 180 / mean writing time | 4.62 FPS | 13.06 FPS |
| Frame iteration, excluding encoder writes | 29.04 s | 10.67 s |
| Mask composition, cumulative profile time | 7.05 s | Absent |
| Font loading, cumulative profile time | 32.28 s | Below the retained top profile entries |
| Output size | 3,969,057 bytes | 3,969,057 bytes |

Video writing improved **38.98 / 13.78 = 2.83×**. Total compile time improved
**76.46 / 17.80 = 4.29×**, largely because repeated font discovery/loading was
removed. The optimized full project's word-layer preparation took only 2.41 s.
Profile regions overlap and should not be added together.

The six-second excerpt consists of the enlarged 720p first source, whereas most
of the full video uses native output-size footage. Its FPS also includes profiler
overhead. Neither its throughput nor its full-project setup cost should be
extrapolated directly to the 138-second video. Nevertheless, the real-input
writing speedup exceeds the earlier **2.28× synthetic compile speedup** and
confirms that the benefit is not confined to synthetic imagery.

**All 180 decoded frames and decoded PCM audio in the matched excerpt were
identical between compilers.** Results and profiles are in `production.json`;
[`production-summary.json`](benchmarks/production-summary.json) contains the
calculated comparison.

## Quality, captions and synchronization

The full MP4 decoded without FFmpeg errors. ffprobe confirmed 1080×1920, 30/1 FPS,
4,152 frames, H.264 High/yuv420p and stereo AAC. Both streams begin at zero.
The 10 ms audio/video end difference is below one 33.3 ms video frame and follows
the existing floor-to-whole-frame behavior.

Five encoded frames were extracted near the beginning, quarter, middle,
three-quarter and end positions. Caption glyphs, white fill, outline, centering,
size and readability were checked against their expected active words. Sampled
frames were visually inspected. Caption events have positive durations, ordered
starts, no starts beyond the video, and a final end at 137.46 s. Normalized
generated-script versus Whisper-word sequence similarity was 98.1% (336 script
tokens / 337 caption tokens); this is a transcription consistency check, not an
independent word-alignment ground truth.

Three two-second windows at 0.5 s, 68.205 s and 135.41 s compared decoded AAC
against the original narration plus the configured music mix. Cross-correlation
found **zero-sample offset in every window**, with zero-lag correlations
**0.99970, 0.99935 and 0.99738**. Sampled peaks were below clipping. This rules out
an observed encoding offset/drift in these windows; a complete human listening
review was not performed.

One initially low frame-comparison PSNR at 102.4 s was investigated. MoviePy's
random seek into the 59.94-FPS source returned frame **1209**; production sequential
rendering correctly used frame **1208**. Independently decoding neighboring
source frames established frame 1208 as the best match: **33.17 dB PSNR** and
mean absolute channel difference **3.87**, versus 21.09 dB for the incorrectly
sought frame. This was a validation-reader discrepancy, not degraded output or
shifted captions. The inspector now decodes source frames by sequential index.
The original measurements and correction are retained explicitly in
[`production-framecheck.json`](benchmarks/production-framecheck.json) and
`production.json`.

## Remaining bottlenecks and concurrency readiness

No rendering regression was found. The main new constraint is **memory**:
337 full caption backing images/masks alone can account for approximately
1.85 GiB, before six source readers, encoder buffers, Python/Torch and composite
allocations. The real 5.99 GiB peak is over three times the earlier optimized
short synthetic job's 1.86 GiB peak. All readers are opened before rendering;
word-asset retention and source-reader count need to inform admission estimates.

CPU behavior also varies substantially by scene. Mean sampled CPU was about
193–204% during the first minute of rendering, and about 498–517% in later
30–50 second windows. The encoder's frame-write calls consumed 54.80 s in total,
including RGB serialization and pipe waits; this is **not** exclusively encoder
CPU time. The enlarged-source matched profile still spends about 6.68 s resizing
six seconds of footage. Encoding/decoder contention will matter more once the
frame-production bottleneck is reduced.

Three oversized Pexels downloads were rejected by the existing byte cap, and
alternative videos downloaded successfully. These warnings explain additional
footage preparation, not a render failure. Whisper CUDA completed successfully;
GPU encoding was not used or needed for correctness.

Recommendation:

* **Current enabled generation/render limit: one.** Do not enable a fixed two- or
  three-job policy on the basis of the synthetic test.
* A future resource-aware scheduler can have an **experimental ceiling of two
  renders**, one Whisper/model-loading stage at a time, and the previously tested
  four encoder threads per render as its initial tuning candidate. Four threads
  have not yet been validated on simultaneous full-length production workloads.
* Budget the measured render peak plus at least 25% margin, and reserve at least
  4 GiB for desktop/headroom. Two jobs of this size require a conservative
  aggregate admission budget of about **19 GiB available before either starts**
  (2 × 5.99 × 1.25 + 4). Only **16.89 GiB** was available at this run's start;
  this conservative policy would keep it at one. RSS sharing can make the estimate
  pessimistic, so calibrate using measured system MemAvailable as well.
* Reassess CPU, memory, swap growth, source count, caption count and Whisper VRAM
  before each stage. Queue when capacity is insufficient. Validate two real-length
  jobs together, including quality and cleanup, before enabling the second slot.
  **Three production renders are not recommended** on the evidence collected.

The renderer is validated for this single real workload. It is ready to support
development of those guards; production concurrent generation is not yet
validated. The existing TUI theme, functionality and single-job behavior remain
unchanged.

## Reproduction and artifacts

[`scripts/validate_production_render.py`](../scripts/validate_production_render.py)
performs one real generation with uploads disabled, captures stage resources,
then inspects it and compares bounded cached excerpts. **It uses the configured
generation services; it is not an offline benchmark.** All subsequent excerpt
comparisons are offline and temporary media are automatically cleaned.

```bash
rtk proxy .venv312/bin/python scripts/validate_production_render.py --topic "How a forest changes through the four seasons" --result docs/benchmarks/production.json
```

New generated scripts/footage can differ on another run, so preserve the JSON
when comparing results. The recording run preceded the inspector's sequential
frame-oracle correction, which is documented above. The renderer itself remained
unchanged throughout.

Video and its normal metadata:
`outputs/1792592944709041190-forest-render-validation.mp4` and the corresponding
`.metadata.json`. Review PNGs and an inspection checkpoint are in
`outputs/1792592944709041190-forest-render-validation.validation/`.
These generated artifacts are ignored by Git; no video is committed.

Verification: the final `rtk python3 -m unittest discover` run passed **72 tests**;
Python compilation and `git diff --check` passed. An earlier suite run hit an
existing test race between checking and reading `/proc/<child>/stat` after a
worker descendant exited. The specific test and then the complete suite passed
on rerun; no TUI/worker code was changed to accommodate it. The completed real
generation, matched output comparison and zero-offset audio checks succeeded.
