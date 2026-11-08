# Milestone 1.2: indexed caption selection

On 2026-10-28, two alternating baseline and optimized trials used the same offline fixtures and settings as [Milestone 1.1](milestone-1.1-performance.md). The optimized compiler indexes caption intervals and returns active layers in their original order; unsupported intervals or query times fall back to MoviePy selection. The four full benchmark runs passed their output checks. The 60-second cases were faster in both optimized trials, but the 30-second results varied in both directions. Two trials on a shared machine are insufficient to establish a general speedup.

## Cause and implementation

MoviePy's composite asks `playing_clips` for each frame. Its default implementation calls `is_playing` for every caption, including inactive captions. In the separate Milestone 1.1 synthetic profile, that method accumulated 18.15 seconds under profiling; that cumulative figure is not an estimate of removable time. The opaque background prevents a separate composite mask from being generated, while each active caption's own mask and compositing are still evaluated normally. This change targets selection only; it does not eliminate active mask evaluation, Pillow compositing, resizing, or FFmpeg work.

The compiler installs a static interval tree on the individual composite, built from actual clip start/end times after caption creation. It stores O(n) interval references, makes no per-frame table or frame cache, handles arbitrary seek order, and returns overlapping clips in MoviePy's existing layer order. Start times are inclusive and ends exclusive. The existing minimum caption duration is preserved, and invalid word intervals are still filtered before clip creation. Unsupported or non-finite intervals and non-finite requests use the original selector. Installation precedes MoviePy's copy-producing setters, including for the 1.x fallback; MoviePy 1.x was reviewed but not runtime-tested.

## Render results

Each cell lists trial 1 / trial 2. Wall and CPU are seconds; memory and disk are MiB (bytes divided by 1,048,576). Baseline and optimized trials ran in order B1, O1, B2, O2. Raw samples and exact bytes are in [baseline 1](benchmarks/milestone12-baseline1.json), [optimized 1](benchmarks/milestone12-optimized1.json), [baseline 2](benchmarks/milestone12-baseline2.json), and [optimized 2](benchmarks/milestone12-optimized2.json).

| Workload | Version | Render wall | Python CPU | FFmpeg CPU | Tree peak RSS | Tree peak PSS | Peak temp disk | Output size |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 30 s, 77 captions | Baseline | 32.56 / 34.91 | 32.23 / 33.35 | 92.65 / 95.14 | 1969.48 / 1979.29 | 1913.46 / 1920.73 | 25.56 / 25.09 | 25.09 / 25.09 |
| 30 s, 77 captions | Indexed | 37.13 / 32.69 | 33.43 / 31.51 | 96.00 / 92.95 | 1991.26 / 1979.69 | 1934.18 / 1923.77 | 25.09 / 25.09 | 25.09 / 25.09 |
| 30 s, 39 captions | Baseline | 24.72 / 27.42 | 23.81 / 25.51 | 93.00 / 99.38 | 1946.88 / 1947.02 | 1890.32 / 1889.48 | 25.59 / 25.22 | 25.12 / 25.12 |
| 30 s, 39 captions | Indexed | 29.33 / 24.07 | 25.99 / 23.25 | 101.27 / 90.23 | 1949.95 / 1948.45 | 1892.79 / 1893.88 | 25.12 / 25.12 | 25.12 / 25.12 |
| 60 s, 159 captions | Baseline | 64.14 / 62.75 | 57.93 / 57.75 | 216.60 / 210.63 | 2115.93 / 2113.59 | 2060.86 / 2058.57 | 56.94 / 56.69 | 56.62 / 56.62 |
| 60 s, 159 captions | Indexed | 62.65 / 59.72 | 57.39 / 55.38 | 216.52 / 207.88 | 2116.93 / 2121.97 | 2061.81 / 2066.23 | 56.94 / 56.94 | 56.62 / 56.62 |
| 60 s, 80 captions | Baseline | 53.42 / 56.17 | 46.70 / 48.97 | 233.33 / 240.23 | 2018.15 / 2023.72 | 1962.66 / 1967.25 | 57.44 / 56.94 | 56.63 / 56.63 |
| 60 s, 80 captions | Indexed | 51.01 / 46.34 | 45.46 / 41.83 | 232.22 / 215.38 | 2033.82 / 2022.27 | 1977.06 / 1965.67 | 57.19 / 57.44 | 56.63 / 56.63 |

The fixture is one already captioned, mixed production output used as the background and as the source of extracted audio; music is mixed again. Original isolated narration and six footage files are unavailable. Stride 2 intentionally omits alternate captions and is a different visual workload, not a before/after comparison. Both versions of each workload use identical timings and media. All cases use 1080 × 1920, 30 fps, libx264 medium at 6500k, 11 threads and AAC. No API, TTS, Whisper or download is involved. Each output was fully decoded and checked for dimensions, frame rate, duration and frame count. These checks alone do not prove pixel or audio equality.

RSS counts shared pages more than once; PSS apportions them. Peaks are sampled at nominal 250 ms and can miss transients; CPU totals use last observed process values and can miss short-lived work or final increments. FFmpeg CPU includes readers, audio and encoder processes. Temp disk includes active worker output and intermediate files, excludes fixtures and completed workers, and is on tmpfs. It may miss short-lived files. The machine was not isolated; order, caching and background load can influence results. One reader and already finished footage cannot establish performance for clean, multi-source jobs or safe concurrency. Production render limit remains one.

## Selection-only diagnostic

[Raw selection results](benchmarks/milestone12-selection.json) compare MoviePy selection and the index over every frame timestamp for evenly spaced, contiguous synthetic captions, with identical selection verified. Five repetitions alternate execution order, each covering ten passes. Fixture and index construction, compositing and encoding are excluded; these ratios are not render speedups. The recorded MoviePy version string was 2.1.2.

| Duration, captions | Baseline median CPU s | Indexed median CPU s | Ratio |
|---|---:|---:|---:|
| 30 s, 39 | 1.642207 | 0.011027 | 148.9× |
| 30 s, 77 | 3.219191 | 0.011857 | 271.5× |
| 60 s, 80 | 6.768244 | 0.023474 | 288.3× |
| 60 s, 159 | 13.398359 | 0.026034 | 514.6× |

## Correctness and validation

Tests compare MoviePy frames and active clip order for sequential and nonmonotonic seeks, adjacent boundaries, overlaps and gaps, different densities, long timelines, odd dimensions, partially transparent masks and off-frame clipping. They cover the existing handling of zero-length and reversed events, non-finite times and copy-producing setters. The full `rtk python3 -m unittest discover` passed 139 tests, and `rtk python3 -m py_compile shorts_core.py` passed.

The separate [encoded parity check](benchmarks/milestone12-parity.json) used matched generated video and narration, 16 captions, 540 × 960 at 24 fps and two threads. All 97 decoded video frame checksums match exactly. The 97th frame results from the encoded MP3 duration. Decoded stereo 44.1 kHz PCM also matches byte-for-byte, with the original output channel count and sample rate retained. A 1.1-second frame was visually inspected and the caption was readable. This short synthetic fixture does not establish fidelity for every production input; the longer cached renders were decoded and checked but their frame and audio content was not compared across versions.

## Next work

Investigate repeated full-frame resize next, with matched clean media and frame parity. The separate resize-heavy profile in Milestone 1.1 attributed 44.05 cumulative seconds to Pillow resize under profiling; this is not a forecast for the continuous native-size fixture here. FFmpeg remains a major CPU and memory consumer, but the aggregate combines readers and encoder; measure those separately before tuning. Caption retention and longer clean multi-source workloads also warrant measurement. None of these offline measurements covers network or inference stages.

## Reproduce

Requires the local saved production MP4 and metadata, `ost.mp3`, FFmpeg/ffprobe, psutil and project dependencies. The files are ignored local assets; use the same files and check fixture hashes when comparing. This creates isolated temporary compiler mirrors, applies the [baseline patch](benchmarks/milestone12-baseline.patch) to the optimized copy, and removes fixtures and mirrors on exit. Run from the project root; retain results elsewhere if desired. The patch is tied to the measured optimized compiler; a changed compiler may require updating it. Outputs are outside the temporary directory so they survive cleanup.

```bash
rtk proxy bash -c 'set -eu
root=$PWD
work=$(mktemp -d /tmp/slopengine-m12-XXXXXX)
trap "rm -rf \"$work\"" EXIT
mkdir -p "$work/fixture" "$work/baseline/scripts" "$work/optimized/scripts"
cp shorts_core.py "$work/baseline/"
cp shorts_core.py "$work/optimized/"
cp scripts/*.py "$work/baseline/scripts/"
cp scripts/*.py "$work/optimized/scripts/"
cp -r slopengine "$work/baseline/"
cp -r slopengine "$work/optimized/"
(cd "$work/baseline" && patch -p0 < "$root/docs/benchmarks/milestone12-baseline.patch")
python3 scripts/prepare_cached_profile.py --video "$root/outputs/1792592944709041190-forest-render-validation.mp4" --metadata "$root/outputs/1792592944709041190-forest-render-validation.metadata.json" --music "$root/ost.mp3" --folder "$work/fixture"
for run in baseline1 optimized1 baseline2 optimized2; do
  case "$run" in baseline*) version=baseline ;; *) version=optimized ;; esac
  (cd "$work/$version" && python3 scripts/benchmark_paired_render.py --manifest "$work/fixture/manifest.json" --run --single --caption-strides 1 2 --result "$root/docs/benchmarks/repro-$run.json")
done
python3 scripts/benchmark_caption_selection.py > "$root/docs/benchmarks/repro-selection.json"'
```

Manifest paths and its hash depend on the temporary directory, so compare asset hashes as well as settings. Results include absolute local paths. The selection diagnostic can also be run independently with `rtk python3 scripts/benchmark_caption_selection.py`.

To repeat the separate synthetic decoded parity check, run the following from the repository root. The two generated inputs must have matching hashes before interpreting the output comparison:

```bash
rtk proxy bash -c 'set -eu
work=$(mktemp -d /tmp/slopengine-parity-XXXXXX)
trap "rm -rf \"$work\"" EXIT
cp shorts_core.py "$work/baseline.py"
patch "$work/baseline.py" < docs/benchmarks/milestone12-baseline.patch
for version in baseline optimized; do
  if [ "$version" = baseline ]; then source="$work/baseline.py"; else source=shorts_core.py; fi
  python3 scripts/benchmark_offline_render.py --compiler-source "$source" --runs 1 --seconds 4 --width 540 --height 960 --fps 24 --threads 2 --keep-media "$work/$version" > "$work/$version.json"
  ffmpeg -v error -i "$work/$version/out.mp4" -map 0:v:0 -f framemd5 "$work/$version.frames"
  ffmpeg -v error -i "$work/$version/out.mp4" -map 0:a:0 -c:a pcm_s16le -f s16le "$work/$version.audio"
done
sha256sum "$work"/{baseline,optimized}/{source.mp4,voice.mp3}
cmp "$work/baseline/source.mp4" "$work/optimized/source.mp4"
cmp "$work/baseline/voice.mp3" "$work/optimized/voice.mp3"
cmp "$work/baseline.frames" "$work/optimized.frames"
cmp "$work/baseline.audio" "$work/optimized.audio"
echo "Decoded frames and native-channel audio match"'
```
