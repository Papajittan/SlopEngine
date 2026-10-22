# Paired render benchmark

Production rendering stays limited to one render at a time. The opt-in benchmark
compares the same two cached production inputs sequentially and concurrently,
using independent processes and the existing, unmodified compiler. It does not
generate narration, download footage, call APIs, or upload videos.

Prepare a JSON manifest using local files. Paths are relative to the manifest.
Supply the complete word timing list from each cached generation; the short list
below illustrates the format only. Use the same production rendering settings in
`config` for both modes.

```json
{
  "config": {"width": 1080, "height": 1920, "fps": 30, "threads": 8},
  "videos": [
    {
      "audio": "first/narration.mp3",
      "backgrounds": ["first/footage.mp4"],
      "word_events": [{"start_ms": 0, "end_ms": 300, "text": "Dreams"}]
    },
    {
      "audio": "second/narration.mp3",
      "backgrounds": ["second/footage.mp4"],
      "word_events": [{"start_ms": 0, "end_ms": 400, "text": "Tomorrow"}]
    }
  ]
}
```

Optional config fields: `caption_font_path`, `caption_font_size`, `caption_color`,
`caption_stroke_color`, `caption_stroke_width`, `caption_y_ratio`,
`background_music_path`, and `background_music_volume`. Other settings are rejected.
Credentials and environment settings are not read. Backgrounds must contain real
cached footage; this tool does not synthesize replacement assets. Ensure enough
temporary disk space for four rendered videos.

Run with the project's media Python environment, after stopping other generation
jobs for a controlled comparison:

```bash
rtk python3 scripts/benchmark_paired_render.py \
  --manifest /tmp/cached-pair/manifest.json --run \
  --result outputs/paired-render-benchmark.json
```

The command renders four videos. Temporary outputs and logs are cleaned on exit;
the JSON report retains log tails. It records total pair wall time, each video's render
time and completion time from pair start, peak combined process-tree RSS, mean
and peak CPU usage (100% per core), available RAM, swap activity, worker failures,
and full decode/stream checks. Output checks cover dimensions, frame rate, frame
count, duration, audio presence, and decode errors. They do not replace visual
inspection of captions or subjective audio review.

Default admission reserves 6.5 GiB per simultaneous render, above the measured
5.99 GiB peak, plus 2 GiB system headroom. Both estimates are configurable with
`--render-gib` and `--headroom-gib`. The concurrent mode requires 15 GiB available
at admission with these defaults. Sampling every 250 ms aborts the benchmark if
available RAM drops below headroom, combined worker RSS exceeds
`--max-combined-gib` (14 GiB), or the system starts swapping out pages. Abort
terminates only the benchmark's process groups and their FFmpeg children. These
sampled safeguards cannot guarantee protection against abrupt system-wide
allocation spikes or kernel OOM kills. No production processes are evicted.

The sequential pair runs first, so filesystem cache warmth can favor the
concurrent pair. Repeat controlled comparisons and examine memory samples before
interpreting a small speedup. Concurrent mode must finish both videos with no
failures, no swap pressure and ample remaining headroom, and improve total
throughput before considering a production render limit of two. The report does
not change production settings or automatically declare that limit safe. An
aborted or unexecuted benchmark provides no evidence for increasing it.
