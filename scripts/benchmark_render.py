#!/usr/bin/env python3
"""Offline full-resolution compiler benchmark. Videos live only in a temporary directory.

Run with the media environment's Python; no credentials, downloads or uploads are used.
Linux /proc diagnostics are optional. CPU percentages use 100% per logical core.
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import os
from pathlib import Path
import pstats
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def command(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def diagnostic(args):
    try:
        return command(args)
    except (OSError, subprocess.CalledProcessError):
        return None


def snapshot(roots):
    """Sample aggregate process-tree RSS, CPU and physical/storage I/O counters."""
    entries = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            entries[int(path.parent.name)] = (int(fields[1]), fields, path.parent)
        except (OSError, ValueError, IndexError):
            pass
    selected = set(roots)
    while True:
        children = {pid for pid, (ppid, _, _) in entries.items() if ppid in selected}
        enlarged = selected | children
        if enlarged == selected:
            break
        selected = enlarged
    cpu = rss = read = written = logical_read = logical_write = 0
    per_pid_cpu = {}
    ticks = os.sysconf('SC_CLK_TCK')
    for pid in selected & entries.keys():
        _, fields, directory = entries[pid]
        per_pid_cpu[pid] = (int(fields[11]) + int(fields[12])) / ticks
        cpu += per_pid_cpu[pid]
        rss += int(fields[21]) * os.sysconf('SC_PAGE_SIZE')
        try:
            io = dict(line.split(': ') for line in (directory / 'io').read_text().splitlines())
            read += int(io['read_bytes'])
            written += int(io['write_bytes'])
            logical_read += int(io['rchar'])
            logical_write += int(io['wchar'])
        except (OSError, KeyError):
            pass
    return dict(cpu_seconds=cpu, per_pid_cpu=per_pid_cpu, rss_bytes=rss, read_bytes=read,
                write_bytes=written, logical_read_bytes=logical_read, logical_write_bytes=logical_write)


def worker(args):
    import shorts_core as core
    if args.compiler_source:
        import importlib.util
        spec = importlib.util.spec_from_file_location('benchmark_reference_core', args.compiler_source)
        core = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = core
        spec.loader.exec_module(core)
    from moviepy.video.VideoClip import VideoClip

    directory = Path(args.work)
    config = core.PipelineConfig(gemini_api_key='', pexels_api_key='', threads=args.threads,
                                 background_music_path=directory / 'music.wav')
    compiler = core.MoviePyShortCompiler(config)
    timings = {}
    for name in ('_build_background', '_word_layer', '_build_audio'):
        original = getattr(compiler, name)
        def timed(*a, _name=name, _original=original, **kw):
            start = time.perf_counter()
            try:
                return _original(*a, **kw)
            finally:
                timings[_name] = timings.get(_name, 0) + time.perf_counter() - start
        setattr(compiler, name, timed)
    original_write = VideoClip.write_videofile
    def write(clip, *a, **kw):
        kw['logger'] = None
        kw['preset'] = args.preset
        kw['codec'] = args.codec
        if args.codec != 'libx264':
            # Match the production H.264 output pixel format in encoder experiments.
            kw['ffmpeg_params'] = [*kw.get('ffmpeg_params', []), '-pix_fmt', 'yuv420p']
        start = time.perf_counter()
        try:
            return original_write(clip, *a, **kw)
        finally:
            timings['write_videofile_including_audio'] = time.perf_counter() - start
    VideoClip.write_videofile = write
    words = ['measure', 'every', 'frame', 'render', 'clearly', 'synchronized', 'video', 'captions']
    events = [core.WordEvent(start_ms=i * 250, end_ms=min(i * 250 + 250, int(args.duration * 1000)),
                             text=words[i % len(words)])
              for i in range(int(args.duration * 4))]
    output = directory / f'{args.index}.mp4'
    profile = cProfile.Profile()
    start = time.perf_counter()
    if args.profile:
        profile.enable()
    compiler.compile(directory / 'narration.wav', events,
                     [directory / 'landscape.mp4', directory / 'portrait.mp4'], output)
    if args.profile:
        profile.disable()
    elapsed = time.perf_counter() - start
    probe = json.loads(command(['ffprobe', '-v', 'error', '-count_frames', '-show_streams',
                               '-show_format', '-of', 'json', str(output)]))
    result = dict(wall_seconds=elapsed, effective_fps=args.duration * 30 / elapsed,
                  stages_seconds=timings, output_bytes=output.stat().st_size, probe=probe)
    if args.profile:
        stats = pstats.Stats(profile)
        result['profile'] = [dict(file=key[0], line=key[1], function=key[2], calls=value[1],
                                  self_seconds=value[2], cumulative_seconds=value[3])
                             for key, value in sorted(stats.stats.items(), key=lambda kv: -kv[1][3])[:70]]
    (directory / f'{args.index}.json').write_text(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', default='benchmark')
    parser.add_argument('--result', type=Path, default=Path('docs/benchmarks/render.json'))
    parser.add_argument('--duration', type=float, default=4)
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--jobs', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--threads', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument('--preset', default='medium')
    parser.add_argument('--codec', default='libx264')
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--compiler-source', type=Path, help='Optional saved shorts_core.py for baseline comparisons')
    parser.add_argument('--keep-media', type=Path, help='Copy last trial media for inspection; use /tmp')
    parser.add_argument('--work', help=argparse.SUPPRESS)
    parser.add_argument('--index', default='0', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.work:
        worker(args)
        return
    if args.duration < 1 or args.trials < 1 or args.threads < 1:
        parser.error('duration >= 1, trials >= 1 and threads >= 1 required')
    import moviepy
    from moviepy.config import FFMPEG_BINARY
    hardware = dict(python=sys.version, moviepy=moviepy.__version__, cpu_count=os.cpu_count(),
                    cpu=diagnostic(['lscpu']),
                    meminfo=Path('/proc/meminfo').read_text() if Path('/proc/meminfo').exists() else None,
                    process_metrics_available=Path('/proc/self/stat').exists(),
                    ffmpeg=command([FFMPEG_BINARY, '-version']), ffmpeg_binary=FFMPEG_BINARY,
                    encoders=command([FFMPEG_BINARY, '-hide_banner', '-encoders']))
    source = args.compiler_source or Path(__file__).resolve().parents[1] / 'shorts_core.py'
    hardware['compiler_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    hardware['gpu'] = diagnostic(['nvidia-smi', '--query-gpu=name,driver_version,memory.total', '--format=csv'])
    records = []
    with tempfile.TemporaryDirectory(prefix='slopengine-bench-') as temporary:
        directory = Path(temporary)
        for name, size in [('landscape', '1920x1080'), ('portrait', '1080x1920')]:
            command([FFMPEG_BINARY, '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=s={size}:r=30',
                     '-t', '1', '-c:v', 'libx264', '-preset', 'fast', '-threads', '2', '-pix_fmt', 'yuv420p',
                     str(directory / f'{name}.mp4')])
        for name, frequency in [('narration', 440), ('music', 220)]:
            command([FFMPEG_BINARY, '-v', 'error', '-f', 'lavfi', '-i',
                     f'sine=frequency={frequency}:sample_rate=44100:duration={args.duration}',
                     str(directory / f'{name}.wav')])
        # One complete render warms filesystem caches and encoders. Workers still
        # import dependencies separately, as independently launched generation jobs do.
        base = [sys.executable, str(Path(__file__).resolve()), '--work', temporary,
                '--threads', str(args.threads), '--preset', args.preset, '--codec', args.codec]
        if args.compiler_source:
            base += ['--compiler-source', str(args.compiler_source.resolve())]
        subprocess.run(base + ['--duration', '1', '--index', 'warmup'], check=True,
                       stdout=subprocess.DEVNULL)
        for trial in range(args.trials):
            start = time.perf_counter()
            workers = [subprocess.Popen(base + ['--duration', str(args.duration), '--index', str(i)] +
                                        (['--profile'] if args.profile else []), stdout=subprocess.DEVNULL)
                       for i in range(args.jobs)]
            peak = dict(cpu_percent=0, rss_bytes=0, read_bytes=0, write_bytes=0,
                        logical_read_bytes=0, logical_write_bytes=0)
            cpu_samples = []
            gpu_samples = []
            previous = snapshot([p.pid for p in workers])
            last = time.perf_counter()
            next_gpu = last
            while any(p.poll() is None for p in workers):
                current = snapshot([p.pid for p in workers])
                now = time.perf_counter()
                cpu_delta = sum(max(0, value - previous['per_pid_cpu'].get(pid, 0))
                                for pid, value in current['per_pid_cpu'].items())
                cpu = cpu_delta / (now - last) * 100
                cpu_samples.append((cpu, now - last))
                peak['cpu_percent'] = max(peak['cpu_percent'], cpu)
                for key in ('rss_bytes', 'read_bytes', 'write_bytes', 'logical_read_bytes', 'logical_write_bytes'):
                    peak[key] = max(peak[key], current[key])
                previous, last = current, now
                if hardware['gpu'] and now >= next_gpu:
                    try:
                        gpu_samples.append(command(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used',
                                                    '--format=csv,noheader,nounits']).strip())
                    except (OSError, subprocess.CalledProcessError):
                        pass
                    next_gpu = time.perf_counter() + 1
                time.sleep(0.2)
            failures = [p.wait() for p in workers if p.wait() != 0]
            batch_wall = time.perf_counter() - start
            outputs = [json.loads((directory / f'{i}.json').read_text()) for i in range(args.jobs)] if not failures else []
            record = dict(trial=trial, batch_wall_seconds=batch_wall, peak=peak, gpu_samples=gpu_samples,
                          mean_cpu_percent=(sum(c * dt for c, dt in cpu_samples) / sum(dt for _, dt in cpu_samples)
                                            if cpu_samples and hardware['process_metrics_available'] else None),
                          failures=failures, videos_per_hour=3600 * len(outputs) / batch_wall, renders=outputs)
            records.append(record)
            print(f'{args.label} trial {trial + 1}: {batch_wall:.2f}s batch, '
                  f'{[round(r["wall_seconds"], 2) for r in outputs]}s render', flush=True)
            if failures:
                break
        if args.keep_media:
            args.keep_media.mkdir(parents=True, exist_ok=True)
            for pattern in ('*.mp4', '*.wav'):
                for path in directory.glob(pattern):
                    shutil.copy2(path, args.keep_media / path.name)
    params = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items() if k not in ('work', 'index')}
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(dict(label=args.label, parameters=params, hardware=hardware,
                                          trials=records), indent=2) + '\n')
    if any(r['failures'] for r in records):
        raise SystemExit('Render worker failed; see stderr and result JSON')


if __name__ == '__main__':
    main()
