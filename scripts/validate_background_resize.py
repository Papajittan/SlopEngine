#!/usr/bin/env python3
"""Compare the current compiler with a saved source using synthetic local media."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def digest(array):
    return hashlib.sha256(array.tobytes()).hexdigest()


def compile_with_cache_metrics(core, compiler, audio, words, backgrounds, output):
    cache_type = getattr(core, "_FittedLandscapeFrameCache", None)
    caches = []
    real_close = core.close_all

    def capture_close(clips):
        items = list(clips)
        if cache_type is not None:
            caches.extend(item for item in items if isinstance(item, cache_type))
        return real_close(items)

    with patch.object(core, "close_all", capture_close):
        compiler.compile(audio, words, backgrounds, output)
    return {
        "instances": len(caches),
        "requests": sum(cache.requests for cache in caches),
        "hits": sum(cache.hits for cache in caches),
        "misses": sum(cache.misses for cache in caches),
        "transforms": sum(cache.transforms for cache in caches),
        "closed_after_compile": all(cache.closed and cache._entry is None for cache in caches),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--result", type=Path, default=ROOT / "docs/benchmarks/milestone13-parity.json")
    parser.add_argument("--preview-copy", type=Path, help="Copy the gated encoded preview here for visual inspection")
    args = parser.parse_args()
    import numpy as np
    from PIL import Image
    from moviepy.video.VideoClip import VideoClip
    import shorts_core

    reference = load("resize_reference", args.reference)
    result = {"reference_sha256": hashlib.sha256(args.reference.read_bytes()).hexdigest(),
              "current_sha256": hashlib.sha256((ROOT / "shorts_core.py").read_bytes()).hexdigest(),
              "cases": {}}
    with tempfile.TemporaryDirectory(prefix="shorts-resize-parity-") as temp:
        root = Path(temp)
        def ffmpeg(*args):
            subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)
        for name, size in [("wide", "161x91"), ("tall", "75x143"), ("square", "101x101"), ("native", "90x160"), ("long", "161x91")]:
            ffmpeg("-f", "lavfi", "-i", f"testsrc=size={size}:rate=8:duration={5 if name == 'long' else 1}", "-c:v", "libx264", "-pix_fmt", "yuv444p", root / f"{name}.mp4")
        for name, hz, duration in [("voice", 440, 3), ("music", 220, 1), ("music_full", 220, 3)]:
            ffmpeg("-f", "lavfi", "-i", f"sine=frequency={hz}:sample_rate=44100:duration={duration}", root / f"{name}.wav")
        events = [dict(text="FIRST", start_ms=0, end_ms=250),
                  dict(text="OVERLAP", start_ms=125, end_ms=625),
                  dict(text="GAP", start_ms=1000, end_ms=1010),
                  dict(text="BOUNDARY", start_ms=1999, end_ms=2500)]
        # Non-monotonic seeks include exact caption, concatenation and loop boundaries.
        times = [i / 8 for i in range(24)] + [1.0, .125, .25, .625, .999, 1.0, 1.001, 2.0, 0, 2.999]
        cases = [("wide", ["wide"], False, []), ("tall", ["tall"], True, events),
                 ("mixed_loop", ["wide", "tall", "square"], True, events),
                 ("fallback", [], False, []), ("native", ["native"], False, []),
                 ("long_trim", ["long"], False, [])]
        for name, sources, music, words in cases:
            snapshots = []
            for core, label in [(reference, "before"), (shorts_core, "after")]:
                config = core.PipelineConfig(gemini_api_key="", pexels_api_key="", width=90, height=160,
                                             fps=30, threads=1, background_music_path=root / "music.wav" if music else None)
                compiler = core.MoviePyShortCompiler(config)
                sample = {"frames": [], "audio": [], "shapes": [], "duration": None, "cache": {}}
                def capture(clip, *unused_args, **unused_kwargs):
                    sample["duration"] = clip.duration
                    for t in times:
                        frame = np.asarray(clip.get_frame(t))
                        sample["frames"].append(digest(frame))
                        sample["shapes"].append(list(frame.shape))
                        if name == "mixed_loop" and t == 1.0:
                            Image.fromarray(frame.astype("uint8")).save(root / f"{label}-mixed-loop.png")
                    # Hash bounded chunks, including a non-monotonic seek.
                    for start in [0, .5, 1, 2, .25]:
                        ts = start + np.arange(22050) / 44100
                        sample["audio"].append(digest(np.asarray(clip.audio.get_frame(ts))))
                with patch.object(VideoClip, "write_videofile", capture):
                    sample["cache"] = compile_with_cache_metrics(
                        core, compiler, root / "voice.wav", [core.WordEvent(**word) for word in words],
                        [root / f"{source}.mp4" for source in sources], root / "unused.mp4")
                snapshots.append(sample)
            before, after = snapshots
            result["cases"][name] = {"frame_count": len(times), "frames_equal": before["frames"] == after["frames"],
                                     "audio_chunks_equal": before["audio"] == after["audio"],
                                     "geometry_equal": before["shapes"] == after["shapes"],
                                     "duration_equal": before["duration"] == after["duration"],
                                     "cache_metrics": {"reference": before["cache"], "optimized": after["cache"]}}
        # Long timeline: repeated background loops and non-monotonic late seeks,
        # without rendering or storing a duration-sized sequence of frames.
        long_snapshots = []
        for core in (reference, shorts_core):
            compiler = core.MoviePyShortCompiler(core.PipelineConfig(
                gemini_api_key="", pexels_api_key="", width=90, height=160, fps=30))
            opened = []
            try:
                background = compiler._build_background([root / "wide.mp4", root / "tall.mp4", root / "square.mp4"], 180, opened)
                timestamps = [0, 1, 2, 2.999, 3, 3.001, 6, 30, 59.875, 60, 179.875, 4.125, 178, 0]
                frames = [digest(np.asarray(background.get_frame(t))) for t in timestamps]
                cache_type = getattr(core, "_FittedLandscapeFrameCache", None)
                caches = [item for item in opened if cache_type is not None and isinstance(item, cache_type)]
            finally:
                core.close_all(opened)
            long_snapshots.append({
                "frames": frames,
                "cache": {"instances": len(caches),
                          "requests": sum(cache.requests for cache in caches),
                          "hits": sum(cache.hits for cache in caches),
                          "misses": sum(cache.misses for cache in caches),
                          "closed_after_timeline": all(cache.closed and cache._entry is None
                                                        for cache in caches)},
            })
        result["long_timeline"] = {
            "frames_equal": long_snapshots[0]["frames"] == long_snapshots[1]["frames"],
            "duration_seconds": 180, "seeks": 14,
            "cache_metrics": {"reference": long_snapshots[0]["cache"],
                              "optimized": long_snapshots[1]["cache"]},
        }
        for core, label in [(reference, "before"), (shorts_core, "after")]:
            config = core.PipelineConfig(gemini_api_key="", pexels_api_key="", width=540, height=960, fps=30)
            def preview(clip, *unused_args, **unused_kwargs):
                Image.fromarray(np.asarray(clip.get_frame(0)).astype("uint8")).save(
                    root / f"preview-{label}.png")
            with patch.object(VideoClip, "write_videofile", preview):
                compile_with_cache_metrics(
                    core, core.MoviePyShortCompiler(config), root / "voice.wav",
                    [core.WordEvent(**word) for word in events], [root / "wide.mp4"],
                    root / "unused.mp4")
        cleanup_checks = {}
        for label, exception_type in (("cancellation", KeyboardInterrupt), ("render_error", RuntimeError)):
            snapshots = []
            for core in (reference, shorts_core):
                compiler = core.MoviePyShortCompiler(core.PipelineConfig(
                    gemini_api_key="", pexels_api_key="", width=90, height=160, fps=30,
                    background_music_path=root / "music.wav"))
                groups = []
                real_close = core.close_all
                cache_type = getattr(core, "_FittedLandscapeFrameCache", None)

                def record_close(clips):
                    resources = list(clips)
                    caches = [item for item in resources
                              if cache_type is not None and isinstance(item, cache_type)]
                    readers = list({id(reader): reader for item in resources
                                    if (reader := getattr(item, "reader", None)) is not None}.values())
                    processes = [getattr(reader, "proc", None) for reader in readers]
                    record = {"item_count": len(resources), "cache_count": len(caches),
                              "reader_count": len(readers)}
                    real_close(resources)
                    record["cache_closed_after"] = all(
                        item.closed and item._entry is None for item in caches)
                    record["reader_objects_closed_after"] = all(
                        getattr(reader, "proc", None) is None for reader in readers)
                    record["reader_processes_exited_after"] = all(
                        process is None or process.poll() is not None for process in processes)
                    groups.append(record)

                def fail(*unused_args, **unused_kwargs):
                    raise exception_type()

                try:
                    with patch.object(VideoClip, "write_videofile", fail), \
                            patch.object(core, "close_all", record_close):
                        compiler.compile(root / "voice.wav", [core.WordEvent(**word) for word in events],
                                         [root / "wide.mp4", root / "tall.mp4"], root / f"{label}.mp4")
                except exception_type:
                    pass
                else:
                    raise RuntimeError(f"Injected {label} did not propagate")
                snapshots.append(groups)

            before, after = snapshots
            normalized_after = [group["item_count"] - group["cache_count"] for group in after]
            before_counts = [group["item_count"] for group in before]
            cleanup_checks[label] = {
                "exception_propagated": True,
                "baseline_groups": before,
                "optimized_groups": after,
                "non_cache_cleanup_counts_equal": before_counts == normalized_after,
                "cache_resources_closed": all(group["cache_closed_after"] for group in after),
                "reader_objects_closed": all(group["reader_objects_closed_after"] for group in after),
                "reader_processes_exited": all(group["reader_processes_exited_after"] for group in after),
                "readers_observed": sum(group["reader_count"] for group in after),
            }
        result["cleanup"] = cleanup_checks
        # Exercise real encoding and compare decoded frame hashes and PCM, streaming through ffmpeg.
        compile_metrics_by_label = {}
        for core, label in [(reference, "before"), (shorts_core, "after")]:
            config = core.PipelineConfig(gemini_api_key="", pexels_api_key="", width=540, height=960, fps=30, threads=1,
                                         background_music_path=root / "music_full.wav")
            compile_metrics_by_label[label] = compile_with_cache_metrics(
                core, core.MoviePyShortCompiler(config), root / "voice.wav",
                [core.WordEvent(**word) for word in events],
                [root / "wide.mp4", root / "tall.mp4"], root / f"{label}.mp4")
        def stream_hash(path, stream, fmt):
            process = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-map", stream,
                                        *(["-pix_fmt", "rgb24"] if fmt == "rawvideo" else []),
                                        "-f", fmt, "-"], stdout=subprocess.PIPE)
            h = hashlib.sha256()
            try:
                while chunk := process.stdout.read(65536):
                    h.update(chunk)
                if process.wait():
                    raise RuntimeError("ffmpeg decode failed")
                return h.hexdigest()
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        encoded_streams = {}
        for label in ("before", "after"):
            video = root / f"{label}.mp4"
            probe = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                "-show_entries", "stream=width,height,avg_frame_rate,duration,nb_read_frames",
                "-of", "json", str(video)], text=True))["streams"][0]
            preview_path = root / f"encoded-{label}.png"
            ffmpeg("-i", video, "-frames:v", "1", preview_path)
            if label == "after" and args.preview_copy:
                args.preview_copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(preview_path, args.preview_copy)
            encoded_streams[label] = {
                "width": probe["width"], "height": probe["height"],
                "fps": probe["avg_frame_rate"], "duration_seconds": float(probe["duration"]),
                "frame_count": int(probe["nb_read_frames"]),
                "decoded_frames_sha256": stream_hash(video, "0:v:0", "rawvideo"),
                "decoded_pcm_sha256": stream_hash(video, "0:a:0", "s16le"),
                "cache_metrics": compile_metrics_by_label[label],
                "preview_png": "temporary fixture preview; removed after validation",
            }
        before, after = encoded_streams["before"], encoded_streams["after"]
        result["encoded"] = {
            "decoded_frames_equal": before["decoded_frames_sha256"] == after["decoded_frames_sha256"],
            "decoded_pcm_equal": before["decoded_pcm_sha256"] == after["decoded_pcm_sha256"],
            "video_hash_format": "raw RGB24", "audio_hash_format": "signed 16-bit little-endian PCM",
            "streams": encoded_streams,
        }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    cleanup_passed = all(
        item["non_cache_cleanup_counts_equal"] and item["cache_resources_closed"]
        and item["reader_objects_closed"] and item["reader_processes_exited"]
        and item["readers_observed"] > 0
        for item in result["cleanup"].values())
    cache_cases = ("wide", "mixed_loop", "long_trim")
    cache_passed = all(
        result["cases"][name]["cache_metrics"]["optimized"]["hits"] > 0
        and result["cases"][name]["cache_metrics"]["optimized"]["closed_after_compile"]
        for name in cache_cases)
    cache_passed = cache_passed and result["long_timeline"]["cache_metrics"]["optimized"]["hits"] > 0
    cache_passed = cache_passed and result["long_timeline"]["cache_metrics"]["optimized"]["closed_after_timeline"]
    cache_passed = cache_passed and result["encoded"]["streams"]["after"]["cache_metrics"]["hits"] > 0
    cache_passed = cache_passed and result["encoded"]["streams"]["after"]["cache_metrics"]["closed_after_compile"]
    cases_equal = all(all(value for key, value in case.items()
                          if key not in ("frame_count", "cache_metrics"))
                      for case in result["cases"].values())
    if not cases_equal or not (result["encoded"]["decoded_frames_equal"] and result["encoded"]["decoded_pcm_equal"]) or not result["long_timeline"]["frames_equal"] or not cleanup_passed or not cache_passed:
        raise SystemExit("Parity mismatch")


if __name__ == "__main__":
    main()
