#!/usr/bin/env python3
"""Serial synthetic full-render benchmark for same-frame background reuse.

Fixtures are generated directly at each input rational frame rate. The six
modest-size cadence pairs and two full-resolution 24-to-30 / 30-to-30 controls
run in B1, O1, O2, B2 order. No project APIs or network services are used.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark_paired_render import TreeSampler, verify_video  # noqa: E402

FPS_PAIRS = (
    ("24-to-30", "24/1", 30),
    ("25-to-30", "25/1", 30),
    ("30-to-30", "30/1", 30),
    ("30-to-60", "30/1", 60),
    ("60-to-30", "60/1", 30),
    ("24000-1001-to-30", "24000/1001", 30),
)
TRIALS = (("baseline", 1), ("optimized", 1), ("optimized", 2), ("baseline", 2))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_video(path: Path) -> dict:
    raw = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate,duration,nb_frames",
        "-of", "json", str(path),
    ], text=True)
    return json.loads(raw)["streams"][0]


def make_fixture(folder: Path, source_fps: str, size: tuple[int, int], seconds: float) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    width, height = size
    source = folder / "source.mp4"
    audio = folder / "voice.wav"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        f"testsrc2=size={width}x{height}:rate={source_fps}", "-t", str(seconds), "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
        str(source),
    ], check=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=44100", "-t", str(seconds), "-c:a", "pcm_s16le",
        str(audio),
    ], check=True)
    return {
        "source": str(source), "audio": str(audio),
        "source_sha256": sha256_file(source), "audio_sha256": sha256_file(audio),
        "source_probe": probe_video(source), "source_bytes": source.stat().st_size,
        "audio_bytes": audio.stat().st_size,
    }


def _immutable_rgb_backing(frame):
    if (type(frame).__name__ != "ndarray" or getattr(frame, "dtype", None) is None
            or frame.dtype.name != "uint8" or frame.ndim != 3 or frame.shape[2] != 3):
        return None
    current = frame
    visited = set()
    while type(current).__name__ == "ndarray":
        if id(current) in visited:
            return None
        visited.add(id(current))
        current = current.base
    return current if isinstance(current, bytes) else None


def import_compiler(source: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load compiler module from {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def instrument(core) -> dict:
    import moviepy.video.io.ffmpeg_reader as ffmpeg_reader

    counters = {
        "pillow_resize_calls": 0, "pillow_resize_wall_seconds": 0.0,
        "pillow_resize_process_cpu_seconds": 0.0,
        "fitted_transform_calls": 0, "fitted_transform_wall_seconds": 0.0,
        "fitted_transform_process_cpu_seconds": 0.0,
        "fitted_frame_requests": 0, "source_reader_frame_requests": 0,
        "source_reader_same_array_requests": 0,
        "source_reader_immutable_bytes_requests": 0,
    }
    original_resize = core.Image.Image.resize

    def measured_resize(image, *args, **kwargs):
        wall = time.perf_counter()
        cpu = time.process_time()
        try:
            return original_resize(image, *args, **kwargs)
        finally:
            counters["pillow_resize_calls"] += 1
            counters["pillow_resize_wall_seconds"] += time.perf_counter() - wall
            counters["pillow_resize_process_cpu_seconds"] += time.process_time() - cpu

    core.Image.Image.resize = measured_resize
    original_transform = core._fit_horizontal_landscape_frame

    def measured_transform(*args, **kwargs):
        wall = time.perf_counter()
        cpu = time.process_time()
        try:
            return original_transform(*args, **kwargs)
        finally:
            counters["fitted_transform_calls"] += 1
            counters["fitted_transform_wall_seconds"] += time.perf_counter() - wall
            counters["fitted_transform_process_cpu_seconds"] += time.process_time() - cpu

    core._fit_horizontal_landscape_frame = measured_transform
    reader_previous = {}
    reader_instances = {}
    reader_processes = {}
    original_get_frame = ffmpeg_reader.FFMPEG_VideoReader.get_frame

    def measured_reader_get_frame(reader, timestamp):
        before_process = getattr(reader, "proc", None)
        frame = original_get_frame(reader, timestamp)
        identity = id(reader)
        reader_instances[identity] = reader
        for process in (before_process, getattr(reader, "proc", None)):
            if process is not None:
                reader_processes[id(process)] = process
        counters["source_reader_frame_requests"] += 1
        if reader_previous.get(identity) == id(frame):
            counters["source_reader_same_array_requests"] += 1
        reader_previous[identity] = id(frame)
        if _immutable_rgb_backing(frame) is not None:
            counters["source_reader_immutable_bytes_requests"] += 1
        return frame

    ffmpeg_reader.FFMPEG_VideoReader.get_frame = measured_reader_get_frame

    cache_instances = []
    if hasattr(core, "_FittedLandscapeFrameCache"):
        cache_type = core._FittedLandscapeFrameCache
        original_init = cache_type.__init__
        original_call = cache_type.__call__

        def track_init(cache, *args, **kwargs):
            original_init(cache, *args, **kwargs)
            cache_instances.append(cache)

        def track_call(cache, *args, **kwargs):
            result = original_call(cache, *args, **kwargs)
            entry = cache._entry
            if entry is not None:
                source_bytes = len(entry[1])
                fitted_bytes = entry[3].nbytes
                counters["cache_peak_retained_source_bytes"] = max(
                    counters.get("cache_peak_retained_source_bytes", 0), source_bytes)
                counters["cache_peak_retained_fitted_bytes"] = max(
                    counters.get("cache_peak_retained_fitted_bytes", 0), fitted_bytes)
                counters["cache_peak_entries"] = max(counters.get("cache_peak_entries", 0), 1)
            return result

        cache_type.__init__ = track_init
        cache_type.__call__ = track_call

    original_fit = core.MoviePyShortCompiler._fit_vertical

    accepts_opened_clips = len(inspect.signature(original_fit).parameters) > 2

    def track_fitted_frame_requests(self, clip, opened_clips=None):
        if accepts_opened_clips:
            fitted = original_fit(self, clip, opened_clips)
        else:
            fitted = original_fit(self, clip)
        frame_function = getattr(fitted, "frame_function", None)
        if callable(frame_function):
            def measured_frame(timestamp):
                counters["fitted_frame_requests"] += 1
                return frame_function(timestamp)
            fitted.frame_function = measured_frame
        return fitted

    core.MoviePyShortCompiler._fit_vertical = track_fitted_frame_requests
    return counters, cache_instances, reader_instances, reader_processes


def worker(args) -> None:
    import numpy  # noqa: F401

    core = import_compiler(args.compiler_source, f"shorts_core_m14_{os.getpid()}")
    config = core.PipelineConfig(
        gemini_api_key="", pexels_api_key="", width=args.width, height=args.height,
        fps=args.output_fps, threads=args.threads)
    words = ("FRAME CACHE KEEPS RESIZE WORK BOUNDED WHILE CAPTIONS AUDIO AND "
             "TIMELINE OUTPUT STAY THE SAME").split()
    events = [core.WordEvent(text=words[i % len(words)], start_ms=i * 250,
                             end_ms=(i + 1) * 250)
              for i in range(int(args.seconds * 4))]
    counters, caches, readers, reader_processes = instrument(core)
    folder = args.folder
    started = time.perf_counter()
    core.MoviePyShortCompiler(config).compile(
        folder / "voice.wav", events, [folder / "source.mp4"], folder / "video.mp4")
    render_wall = time.perf_counter() - started
    counters.update({
        "cache_requests": sum(cache.requests for cache in caches),
        "cache_hits": sum(cache.hits for cache in caches),
        "cache_misses": sum(cache.misses for cache in caches),
        "cache_transforms": sum(cache.transforms for cache in caches),
        "cache_instances": len(caches),
        "cache_closed_after_compile": all(cache.closed and cache._entry is None for cache in caches),
        "cache_entry_count_after_compile": sum(cache._entry is not None for cache in caches),
        "source_reader_instances": len(readers),
        "source_reader_objects_closed_after_compile": all(
            reader.proc is None for reader in readers.values()),
        "source_reader_processes_exited_after_compile": all(
            process.poll() is not None for process in reader_processes.values()),
    })
    (folder / "worker.json").write_text(json.dumps({
        "render_wall_seconds": render_wall,
        "measurements": counters,
        "output_bytes": (folder / "video.mp4").stat().st_size,
        "output_sha256": sha256_file(folder / "video.mp4"),
    }, indent=2) + "\n")


def run_once(variant: str, trial: int, compiler: Path, fixture: dict, case: dict,
             folder: Path, psutil, sample_interval: float) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    # Each renderer sees the same filename layout and immutable input bytes.
    for name in ("source.mp4", "voice.wav"):
        source = Path(fixture["source"] if name == "source.mp4" else fixture["audio"])
        (folder / name).symlink_to(source)
    command = [
        sys.executable, str(Path(__file__).resolve()), "--worker",
        "--compiler-source", str(compiler), "--folder", str(folder),
        "--width", str(case["width"]), "--height", str(case["height"]),
        "--output-fps", str(case["output_fps"]), "--seconds", str(case["seconds"]),
        "--threads", str(case["threads"]),
    ]
    process = None
    sampler = TreeSampler(psutil)
    samples = []
    started = time.monotonic()
    try:
        previous_mask = None
        if hasattr(signal, "pthread_sigmask"):
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            from scripts.benchmark_offline_render import unblock_child_signal_mask
            with (folder / "worker.log").open("w") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True,
                                           preexec_fn=unblock_child_signal_mask
                                           if hasattr(signal, "pthread_sigmask") else None)
        finally:
            if previous_mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        with (folder / "worker.log").open("a") as log:
            while process.poll() is None:
                categories, cpu_delta = sampler.sample([process])
                disk_bytes = sum(path.stat().st_size for path in folder.rglob("*")
                                 if path.is_file() and not path.is_symlink()
                                 and path.name not in {"worker.log", "worker.json", "video.mp4"})
                samples.append({
                    "elapsed_seconds": time.monotonic() - started,
                    "rss_bytes": sum(item["rss_bytes"] for item in categories.values()),
                    "pss_bytes": (sum(item["pss_bytes"] for item in categories.values())
                                  if all(item["pss_bytes"] is not None
                                         for item in categories.values()) else None),
                    "pss_complete_processes": sum(item["pss_process_count"]
                                                   for item in categories.values()),
                    "processes": categories, "cpu_seconds_by_category_interval": cpu_delta,
                    "temporary_intermediate_bytes": disk_bytes,
                })
                time.sleep(sample_interval)
    finally:
        if process is not None:
            from scripts.benchmark_offline_render import stop_worker_group
            stop_worker_group(process)
    wall = time.monotonic() - started
    if process is None or process.returncode:
        log_text = (folder / "worker.log").read_text(errors="replace")[-12000:]
        raise RuntimeError(f"{variant} render failed ({None if process is None else process.returncode}):\n{log_text}")
    data = json.loads((folder / "worker.json").read_text())
    video = {
        "audio": fixture["audio"],
        "word_events": [{"start_ms": i * 250, "end_ms": (i + 1) * 250}
                         for i in range(int(case["seconds"] * 4))],
    }
    config = {"width": case["width"], "height": case["height"], "fps": case["output_fps"]}
    verification = verify_video(folder / "video.mp4", video, config)
    if not verification["passed"]:
        raise RuntimeError(f"Output verification failed: {verification['errors']}")
    pss_samples = [sample["pss_bytes"] for sample in samples if sample["pss_bytes"] is not None]
    data.update({
        "variant": variant, "trial": trial,
        "process_wall_seconds_including_import": wall,
        "process_cpu_seconds_by_category": sampler.cpu_totals(),
        "peak_tree_rss_bytes": max((sample["rss_bytes"] for sample in samples), default=0),
        "peak_tree_pss_bytes": max(pss_samples, default=None),
        "pss_coverage": {"complete_samples": len(pss_samples), "total_samples": len(samples)},
        "peak_temporary_intermediate_bytes": max(
            (sample["temporary_intermediate_bytes"] for sample in samples), default=0),
        "resource_samples": samples,
        "verification": verification,
        "output_probe": probe_video(folder / "video.mp4"),
    })
    return data


def stream_compare(left: Path, right: Path, selector: list[str]) -> dict:
    def launch(path):
        return subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                                 *selector], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    processes = (launch(left), launch(right))
    hashes = [hashlib.sha256(), hashlib.sha256()]
    mismatch = None
    offset = 0
    try:
        while True:
            chunks = [process.stdout.read(1024 * 1024) for process in processes]
            for digest, chunk in zip(hashes, chunks):
                digest.update(chunk)
            if mismatch is None and chunks[0] != chunks[1]:
                limit = min(map(len, chunks))
                mismatch = offset + next((i for i, pair in enumerate(zip(chunks[0][:limit], chunks[1][:limit]))
                                          if pair[0] != pair[1]), limit)
            offset += max(map(len, chunks))
            if not chunks[0] and not chunks[1]:
                break
        errors = [process.communicate()[1] for process in processes]
        if any(process.returncode for process in processes):
            raise RuntimeError(f"Decoded stream comparison failed: {errors!r}")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    return {
        "equal": mismatch is None and hashes[0].digest() == hashes[1].digest(),
        "first_differing_byte": mismatch, "bytes_compared": offset,
        "baseline_sha256": hashes[0].hexdigest(), "optimized_sha256": hashes[1].hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--only", nargs="+", choices=(*[pair[0] for pair in FPS_PAIRS],
                                                              "full-resolution-24-to-30",
                                                              "full-resolution-30-to-30"),
                        help="Restrict execution to selected workloads")
    parser.add_argument("--result", type=Path, default=ROOT / "docs/benchmarks/milestone14-render.json")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--compiler-source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--folder", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--width", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--height", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--output-fps", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    if args.baseline_source is None:
        parser.error("--baseline-source is required")
    if args.seconds <= 0 or args.threads < 1 or args.sample_interval <= 0:
        parser.error("seconds, threads and sample interval must be positive")
    baseline = args.baseline_source.resolve(strict=True)
    optimized = (ROOT / "shorts_core.py").resolve(strict=True)
    import psutil
    import platform
    import moviepy
    import numpy
    import PIL
    from moviepy.config import FFMPEG_BINARY

    args.result.parent.mkdir(parents=True, exist_ok=True)
    case_specs = []
    for name, source_fps, output_fps in FPS_PAIRS:
        case_specs.append({"name": name, "source_fps": source_fps, "output_fps": output_fps,
                           "width": 540, "height": 960, "seconds": args.seconds,
                           "threads": args.threads, "scope": "all-six-pair-modest-render"})
    for name, source_fps in (("full-resolution-24-to-30", "24/1"),
                             ("full-resolution-30-to-30", "30/1")):
        case_specs.append({"name": name, "source_fps": source_fps, "output_fps": 30,
                           "width": 1080, "height": 1920, "seconds": args.seconds,
                           "threads": args.threads, "scope": "full-resolution-resize-heavy"})
    if args.only:
        case_specs = [case for case in case_specs if case["name"] in args.only]

    result = {
        "benchmark": "milestone-1.4-repeated-background-frame-cache",
        "method": "Each fixture is generated directly with testsrc2 at its exact requested rational input fps; render order per case is B1, O1, O2, B2.",
        "baseline_source": str(baseline), "baseline_sha256": sha256_file(baseline),
        "optimized_source": str(optimized), "optimized_sha256": sha256_file(optimized),
        "environment": {"python": sys.version, "platform": platform.platform(),
                        "cpu_count": os.cpu_count(), "moviepy": moviepy.__version__,
                        "pillow": PIL.__version__, "numpy": numpy.__version__,
                        "ffmpeg_binary": FFMPEG_BINARY,
                        "ffmpeg_version": subprocess.check_output([FFMPEG_BINARY, "-version"], text=True)},
        "render_settings": {"seconds": args.seconds, "threads": args.threads,
                            "sample_interval_seconds": args.sample_interval},
        "resource_note": "Tree RSS sums shared pages. PSS is sampled and can be unavailable for individual processes. Sampling can miss short-lived peaks and final CPU increments. FFmpeg CPU includes source readers and encoder. Temporary intermediate samples exclude fixture symlinks, logs and the completed output; output bytes are recorded separately. /tmp may be tmpfs.",
        "measurement_note": "Reader request counts are MoviePy FFMPEG_VideoReader.get_frame calls, not decoder packet counts. Same-array reuse is identity observed at consecutive reader calls. Fitted transform and Pillow timings are diagnostic regions and overlap render wall time.",
        "cases": [],
    }
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    with tempfile.TemporaryDirectory(prefix="slopengine-m14-") as temporary:
        root = Path(temporary)
        for case in case_specs:
            source_size = (case["width"] * 16 // 9, case["width"])
            # Keep the input exactly 16:9 while matching the requested fitted output height.
            if case["width"] == 540:
                source_size = (960, 540)
            else:
                source_size = (1920, 1080)
            fixture = make_fixture(root / f"fixture-{case['name']}", case["source_fps"],
                                   source_size, case["seconds"])
            if fixture["source_probe"]["avg_frame_rate"] != case["source_fps"]:
                raise RuntimeError(f"Fixture frame rate mismatch for {case['name']}: {fixture['source_probe']}")
            case_record = {**case, "fixture": fixture, "runs": []}
            outputs = []
            case_dir = root / f"runs-{case['name']}"
            result["cases"].append(case_record)
            for index, (variant, trial) in enumerate(TRIALS):
                work = case_dir / f"{variant}-{trial}"
                compiler = baseline if variant == "baseline" else optimized
                record = run_once(variant, trial, compiler, fixture, case, work,
                                  psutil, args.sample_interval)
                record["order"] = index + 1
                case_record["runs"].append(record)
                outputs.append(work / "video.mp4")
                args.result.write_text(json.dumps(result, indent=2) + "\n")
                print(f"{case['name']} {variant}{trial}: {record['render_wall_seconds']:.2f}s", flush=True)
            parity = {
                "decoded_rgb24": stream_compare(outputs[0], outputs[1],
                    ["-map", "0:v:0", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]),
                "decoded_pcm_s16le": stream_compare(outputs[0], outputs[1],
                    ["-map", "0:a:0", "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"]),
                "all_four_encoded_files_equal": len({sha256_file(path) for path in outputs}) == 1,
            }
            # Compare every output against B1, including the second run of each variant.
            parity["all_decoded_rgb24_equal"] = all(stream_compare(
                outputs[0], path, ["-map", "0:v:0", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"])["equal"]
                for path in outputs[1:])
            parity["all_decoded_pcm_equal"] = all(stream_compare(
                outputs[0], path, ["-map", "0:a:0", "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"])["equal"]
                for path in outputs[1:])
            case_record["parity"] = parity
            case_record["passed"] = (parity["decoded_rgb24"]["equal"]
                                     and parity["decoded_pcm_s16le"]["equal"]
                                     and parity["all_four_encoded_files_equal"]
                                     and parity["all_decoded_rgb24_equal"]
                                     and parity["all_decoded_pcm_equal"]
                                     and all(run["measurements"]["cache_closed_after_compile"]
                                             for run in case_record["runs"] if run["variant"] == "optimized"))
            case_record["passed"] = case_record["passed"] and all(
                run["measurements"]["source_reader_objects_closed_after_compile"]
                and run["measurements"]["source_reader_processes_exited_after_compile"]
                for run in case_record["runs"])
            args.result.write_text(json.dumps(result, indent=2) + "\n")
            for child in case_dir.iterdir():
                if child.is_dir():
                    import shutil
                    shutil.rmtree(child)
            print(f"{case['name']} parity={'PASS' if case_record['passed'] else 'FAIL'}", flush=True)
    result["passed"] = all(case["passed"] for case in result["cases"])
    args.result.write_text(json.dumps(result, indent=2) + "\n")
    if not result["passed"]:
        raise SystemExit("M1.4 benchmark parity or cleanup check failed")


if __name__ == "__main__":
    main()
