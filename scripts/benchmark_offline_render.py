#!/usr/bin/env python3
"""Offline renderer benchmark; requires ffmpeg, ffprobe, psutil and project dependencies.

Example: python3 scripts/benchmark_offline_render.py --compiler-source /tmp/original.py --runs 3
The fixtures are generated locally; no API calls or uploads are made.
RSS, CPU and temporary-file size are sampled every 50 ms. Short-lived subprocesses
and temporary files may be missed; RSS sums shared pages across processes. CPU
is the sum of the last sampled CPU time for each observed process. Output and
fixture files are excluded from temporary-file size. Process metrics include
worker startup and imports; compile wall time is recorded separately. This does
not measure speech transcription or the other pipeline stages.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time

import psutil

ROOT = Path(__file__).resolve().parents[1]


def unblock_child_signal_mask():
    if hasattr(signal, 'pthread_sigmask'):
        signal.pthread_sigmask(signal.SIG_SETMASK, set())


def stop_worker_group(process):
    """Stop leftover FFmpeg descendants only while this worker group exists."""
    def exists():
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    if exists():
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 3
        while exists() and time.monotonic() < deadline:
            time.sleep(.05)
        if exists():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    process.wait()


def generate_fixtures(folder, seconds, fps):
    source, voice = folder / 'source.mp4', folder / 'voice.mp3'
    commands = [
        ['-f', 'lavfi', '-i', f'testsrc2=size=640x360:rate={fps}', '-t', str(seconds),
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)],
        ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', str(seconds),
         '-c:a', 'libmp3lame', str(voice)],
    ]
    for args in commands:
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *args], check=True)
    return source, voice


def worker(args):
    import importlib.util
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location('shorts_core_benchmark', args.compiler_source)
    core = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = core
    spec.loader.exec_module(core)
    config = core.PipelineConfig(gemini_api_key='', pexels_api_key='', width=args.width,
                                 height=args.height, fps=args.fps, threads=args.threads)
    words = 'THIS IS AN OFFLINE SYNTHETIC BENCHMARK FOR CAPTION VIDEO RENDER TESTING TODAY'.split()
    events = [core.WordEvent(text=words[i % len(words)], start_ms=i * 250, end_ms=(i + 1) * 250)
              for i in range(args.seconds * 4)]
    folder = Path(args.folder)
    start = time.perf_counter()
    core.MoviePyShortCompiler(config).compile(folder / 'voice.mp3', events, [folder / 'source.mp4'], folder / 'out.mp4')
    (folder / 'timing.json').write_text(json.dumps({'render_seconds': time.perf_counter() - start}))


def run_once(args, folder):
    output = folder / 'out.mp4'
    output.unlink(missing_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--folder', str(folder),
               '--compiler-source', str(args.compiler_source), '--width', str(args.width),
               '--height', str(args.height), '--fps', str(args.fps), '--threads', str(args.threads),
               '--seconds', str(args.seconds)]
    start = time.monotonic()
    p = None
    try:
        # Keep SIGINT/SIGTERM pending until the new process group is registered in
        # this frame. A pending signal is then delivered inside this try/finally.
        previous_mask = None
        if hasattr(signal, 'pthread_sigmask'):
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK,
                                                   {signal.SIGINT, signal.SIGTERM})
        try:
            p = subprocess.Popen(command, stdout=subprocess.DEVNULL, start_new_session=True,
                                 preexec_fn=unblock_child_signal_mask if hasattr(signal, 'pthread_sigmask') else None)
        finally:
            if previous_mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        peak = peak_child = peak_disk = 0
        cpu_times = {}
        parent = psutil.Process(p.pid)
        while p.poll() is None:
            try:
                processes = [parent] + parent.children(recursive=True)
                rss = children_rss = 0
                for process in processes:
                    try:
                        memory = process.memory_info().rss
                        rss += memory
                        if process.pid != p.pid:
                            children_rss += memory
                        cpu = process.cpu_times()
                        cpu_times[process.pid] = max(cpu_times.get(process.pid, 0), cpu.user + cpu.system)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                peak, peak_child = max(peak, rss), max(peak_child, children_rss)
                files = (f for f in folder.rglob('*') if f.is_file() and f.name not in {'source.mp4', 'voice.mp3', 'out.mp4', 'timing.json'})
                peak_disk = max(peak_disk, sum(f.stat().st_size for f in files))
            except (psutil.NoSuchProcess, FileNotFoundError):
                pass
            time.sleep(0.05)
    finally:
        # A successful worker has already reaped its FFmpeg children. On
        # interruption or worker failure, stop only its still-live process group.
        if p is not None:
            stop_worker_group(p)
    elapsed = time.monotonic() - start
    if p.wait() != 0:
        raise RuntimeError('Renderer failed; see stderr above')
    probe = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
        'stream=codec_name,width,height,r_frame_rate,duration:format=duration,size', '-of', 'json', str(output)], text=True)
    timing = json.loads((folder / 'timing.json').read_text())
    return {'wall_seconds_including_import': round(elapsed, 3), 'render_seconds': round(timing['render_seconds'], 3),
            'sampled_cpu_seconds': round(sum(cpu_times.values()), 3),
            'peak_tree_rss_bytes': peak, 'peak_subprocess_rss_bytes': peak_child,
            'peak_extra_temp_bytes': peak_disk, 'output_bytes': output.stat().st_size, 'probe': json.loads(probe)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler-source', type=Path, default=ROOT / 'shorts_core.py')
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--seconds', type=int, default=8)
    parser.add_argument('--width', type=int, default=540)
    parser.add_argument('--height', type=int, default=960)
    parser.add_argument('--fps', type=int, default=24)
    parser.add_argument('--threads', type=int, default=2)
    parser.add_argument('--keep-media', type=Path, help='Copy last trial and fixture media for inspection (use /tmp)')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--folder', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    if min(args.runs, args.seconds, args.width, args.height, args.fps, args.threads) < 1:
        parser.error('numeric arguments must be positive')
    args.compiler_source = args.compiler_source.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='slop-benchmark-') as temp:
        folder = Path(temp)
        generate_fixtures(folder, args.seconds, args.fps)
        results = [run_once(args, folder) for _ in range(args.runs)]
        if args.keep_media:
            args.keep_media.mkdir(parents=True, exist_ok=True)
            for name in ('source.mp4', 'voice.mp3', 'out.mp4'):
                shutil.copy2(folder / name, args.keep_media / name)
    print(json.dumps({'source': str(args.compiler_source),
          'source_sha256': hashlib.sha256(args.compiler_source.read_bytes()).hexdigest(),
          'settings': {'seconds': args.seconds,
          'caption_count': args.seconds * 4, 'width': args.width, 'height': args.height,
          'fps': args.fps, 'threads': args.threads}, 'samples': results}, indent=2))


if __name__ == '__main__':
    main()
