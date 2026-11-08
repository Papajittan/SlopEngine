# Phase 1 performance measurements

Follow-up: [Milestone 1.1](milestone-1.1-performance.md) records longer offline cached-media runs, per-stage sampling, and the limits of comparing them with this synthetic baseline.

The existing MoviePy compiler creates a caption image and mask for every word before rendering, retaining large transparent image regions. It also composites frames in Python and eagerly imports Whisper/PyTorch. The measured change crops transparent margins before creating MoviePy clips while preserving the original caption coordinates. It does not change the renderer or encoder. Other suspected costs have not been separately measured here.

## Offline comparison

Three runs per source used identical generated 8-second test-pattern video and sine-wave narration, 32 timed captions, 540 × 960 at 24 fps, two encoding threads, and the existing libx264 medium/6500k and AAC settings. The video and audio fixture files had identical SHA-256 hashes across the two sets. There were no network calls. Results are medians on a Ryzen 5 5600H (6 cores/12 threads), 27 GiB RAM, Linux x86-64, Python 3.14.8, MoviePy 2.2.1, Pillow 11.3.0, NumPy 2.4.4, Whisper 20250625 and torch 2.9.1+cu130. Fixtures and probing used system FFmpeg n9.0.2; MoviePy used imageio-ffmpeg 7.0.2. The baseline source SHA-256 was `2707b42cde5257b16ac251707545a2e04ba070979f2a0abd5dc981071861ee36`; optimized source SHA-256 was `0d84f1ade42ad470de5cacc27aab24222edfb9db88465ae03a376b855bd27457`.

| Metric | Original | Optimized | Difference |
|---|---:|---:|---:|
| Peak process-tree RSS | 1,077,841,920 B | 1,010,851,840 B | −6.2% |
| Compile wall time | 7.859 s | 7.477 s | −4.9% |
| Sampled process-tree CPU time | 14.73 s | 14.44 s | −2.0% |
| Output size | 4,806,964 B | 4,806,964 B | 0% |
| Peak intermediate file size | 130,551 B | 130,551 B | 0% |

Whole-worker wall time including imports was 10.926 versus 10.588 seconds. Peak child-process RSS was 259,608,576 versus 267,042,816 bytes. These measurements cover a short synthetic render, not a full production job or Whisper transcription. Differences in small time samples can reflect machine load, and cannot predict every workload.

Both output files were byte-identical in the retained runs, fully decoded successfully, and had identical per-frame decoded checksums and decoded audio SHA-256. Both reported H.264 540 × 960 at 24 fps, video duration 8.041667 s, and AAC duration 8.050 s. The extracted caption frame was visually inspected. A synthetic tone cannot establish real speech synchronization, multilingual caption quality, or music-mixing parity.

## Remaining investigation

MoviePy drives FFmpeg readers and encoders but performs fitting and composition in Python; existing profiling in [rendering-performance.md](rendering-performance.md) measured substantial Pillow resize cost. A native FFmpeg filter graph could reduce that work, but it needs equivalent crop, rasterized caption placement, looping, audio mixing, error handling and cancellation tests before it can be offered as a backend. No native renderer or renderer-selection option was implemented in this pass.

Edge TTS already saves word-boundary JSONL. The installed edge-tts 7.3.1 uses 10,000,000 ticks per second and compensates offsets across chunks using audio byte counts. The pipeline still transcribes with Whisper and imports it eagerly: the available synthetic fixture cannot validate boundary coverage, pronunciation, multilingual text, or drift against actual speech. Edge timing, Whisper fallback and lazy imports remain future work; there is no measured transcription gain in this comparison.

The scheduler still permits one render at a time with its existing memory reservations. Full-length clean cached-media runs, process cleanup after production cancellation, and playback-based synchronization checks remain necessary before broader parity or concurrency claims. The benchmark launch interruption window noted in this original investigation has since been addressed.

## Reproduce

Run from the repository root with Python dependencies installed, including `psutil`, and `ffmpeg`/`ffprobe` on PATH. The benchmark creates and automatically removes temporary fixtures and intermediate media. Optional `--keep-media /tmp/slopengine-media-baseline` retains files for inspection; remove that directory when finished. Export `BASELINE_COMMIT` as an explicit pre-change commit and verify the resulting source hash matches the original hash above; `HEAD` is not necessarily the baseline.

```bash
rtk proxy sh -c 'git show "${BASELINE_COMMIT}:shorts_core.py" > /tmp/slopengine-original.py'
rtk proxy sh -c 'python3 scripts/benchmark_offline_render.py --compiler-source /tmp/slopengine-original.py --runs 3 > /tmp/slopengine-original.json'
rtk proxy sh -c 'python3 scripts/benchmark_offline_render.py --compiler-source shorts_core.py --runs 3 > /tmp/slopengine-optimized.json'
```

The harness samples RSS, per-process CPU time and intermediate disk usage every 50 ms; very short-lived children or files can be missed, and summing RSS counts shared pages more than once. CPU time sums the last observed value for each process. Intermediate disk excludes fixtures and the output file and therefore is not total working-directory occupancy. Process startup and imports are included in process-tree metrics; compile time is measured separately. The original and optimized tests must run without competing heavy workloads. The tracked `scripts/benchmark_render.py` provides another full-resolution benchmark with stage timings and optional media retention.

The harness blocks interruption signals around worker launch and registration, restores the child signal mask before exec, and terminates the owned process group on interruption or exit.
