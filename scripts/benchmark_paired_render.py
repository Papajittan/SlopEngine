#!/usr/bin/env python3
"""Opt-in offline comparison of two sequential versus two simultaneous renders."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RENDER_FIELDS = {
    'width', 'height', 'fps', 'threads', 'caption_font_path', 'caption_font_size',
    'caption_color', 'caption_stroke_color', 'caption_stroke_width',
    'caption_y_ratio', 'background_music_path', 'background_music_volume',
}


def load_manifest(path):
    """Resolve only local assets; credentials and network settings are rejected."""
    data = json.loads(path.read_text())
    if len(data.get('videos', [])) != 2:
        raise ValueError('Manifest must contain exactly two videos')
    settings = data.get('config', {})
    if set(settings) - RENDER_FIELDS:
        raise ValueError('Manifest config accepts only documented rendering fields')
    def local(value):
        result = (path.parent / value).resolve()
        if not result.is_file():
            raise ValueError(f'Cached asset not found: {result}')
        return str(result)
    for video in data['videos']:
        video['audio'] = local(video['audio'])
        if not video.get('backgrounds') or not video.get('word_events'):
            raise ValueError('Each video needs cached backgrounds and word_events')
        video['backgrounds'] = [local(p) for p in video['backgrounds']]
        for event in video['word_events']:
            if event['start_ms'] < 0 or event['end_ms'] <= event['start_ms']:
                raise ValueError('Caption timings must be positive ordered intervals')
    for key in ('background_music_path', 'caption_font_path'):
        if settings.get(key):
            settings[key] = local(settings[key])
    for key in ('width', 'height', 'fps', 'threads', 'caption_font_size'):
        if key in settings and (not isinstance(settings[key], int) or settings[key] < 1):
            raise ValueError(f'{key} must be a positive integer')
    return data


def worker(manifest, index, work):
    # Import only in the isolated worker. Never construct ShortsPipeline, TTS,
    # Whisper, downloaders or uploaders; the compiler reads local files only.
    import shorts_core as core
    settings = dict(manifest.get('config', {}))
    if settings.get('background_music_path'):
        settings['background_music_path'] = Path(settings['background_music_path'])
    config = core.PipelineConfig(gemini_api_key='', pexels_api_key='', **settings)
    item = manifest['videos'][index]
    started = time.perf_counter()
    core.MoviePyShortCompiler(config).compile(
        Path(item['audio']), [core.WordEvent(**e) for e in item['word_events']],
        [Path(p) for p in item['backgrounds']], work / 'video.partial.mp4')
    (work / 'video.partial.mp4').rename(work / 'video.mp4')
    (work / 'worker.json').write_text(json.dumps({
        'render_wall_seconds': time.perf_counter() - started,
        'config': {key: str(value) if isinstance(value, Path) else value
                   for key, value in asdict(config).items() if key in RENDER_FIELDS},
    }))


def stop_groups(processes):
    """Kill only new sessions launched by this benchmark, including FFmpeg."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for process in processes:
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and any(p.poll() is None for p in processes):
                time.sleep(.05)
    for process in processes:
        process.wait()


def tree_sample(processes, psutil):
    selected = {}
    for process in processes:
        try:
            root = psutil.Process(process.pid)
            for child in [root, *root.children(recursive=True)]:
                selected[child.pid] = child
        except psutil.Error:
            pass
    rss, cpu = 0, {}
    for pid, process in selected.items():
        try:
            rss += process.memory_info().rss
            times = process.cpu_times()
            cpu[(pid, process.create_time())] = times.user + times.system
        except psutil.Error:
            pass
    return rss, cpu


def verify_video(path, video, config):
    """Decode the entire output and verify streams, dimensions, fps and duration."""
    result = subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_streams',
                             '-show_format', '-of', 'json', str(path)],
                            check=True, capture_output=True, text=True)
    probe = json.loads(result.stdout)
    stream = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    numerator, denominator = map(int, stream['avg_frame_rate'].split('/'))
    audio = json.loads(subprocess.run(
        ['ffprobe', '-v', 'error', '-show_format', '-of', 'json', video['audio']],
        check=True, capture_output=True, text=True).stdout)
    duration = max(float(audio['format']['duration']),
                   max(e['end_ms'] for e in video['word_events']) / 1000, 1)
    errors = []
    if (stream['width'], stream['height']) != (config['width'], config['height']):
        errors.append('incorrect dimensions')
    if denominator == 0 or abs(numerator / denominator - config['fps']) > .01:
        errors.append('incorrect frame rate')
    if abs(float(probe['format']['duration']) - duration) > .25:
        errors.append('incorrect duration')
    if abs(int(stream['nb_read_frames']) - duration * config['fps']) > 2:
        errors.append('incorrect decoded frame count')
    if not any(s['codec_type'] == 'audio' for s in probe['streams']):
        errors.append('missing audio')
    # ffprobe counts frames, while ffmpeg also checks the audio decode path.
    subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path),
                    '-f', 'null', '-'], check=True, capture_output=True)
    return {'passed': not errors, 'errors': errors, 'probe': probe}


def run_pair(mode, manifest, path, work, args, psutil):
    parallel = 1 if mode == 'sequential' else 2
    record = {'mode': mode, 'failures': [], 'videos': [], 'samples': [], 'aborted': None}
    processes, handles, active, launched = [], [], {}, 0
    start = last = time.perf_counter()
    previous = {}
    swap_start = psutil.swap_memory()
    try:
        if psutil.virtual_memory().available < (args.headroom_gib + parallel * args.render_gib) * 2**30:
            record['aborted'] = 'Insufficient available RAM for measured render estimates plus headroom'
            return record
        while active or launched < 2:
            available = psutil.virtual_memory().available
            rss, cpu = tree_sample(processes, psutil)
            swap = psutil.swap_memory()
            now = time.perf_counter()
            interval = now - last
            sample = {'elapsed_seconds': now - start, 'interval_seconds': interval,
                      'rss_bytes': rss, 'available_bytes': available,
                      'cpu_percent': sum(max(0, value - previous.get(pid, 0))
                                         for pid, value in cpu.items()) / max(interval, .0001) * 100,
                      'swap_in_bytes': swap.sin - swap_start.sin,
                      'swap_out_bytes': swap.sout - swap_start.sout}
            record['samples'].append(sample)
            previous, last = cpu, now
            if available < args.headroom_gib * 2**30 or rss > args.max_combined_gib * 2**30:
                record['aborted'] = 'Memory safety limit reached'
                break
            if swap.sout > swap_start.sout:
                record['aborted'] = 'System started swapping during benchmark'
                break
            for index, process in list(active.items()):
                code = process.poll()
                if code is None:
                    continue
                video_record = {'index': index, 'exit_code': code,
                                'completion_seconds': now - start}
                if code:
                    record['failures'].append({'index': index, 'exit_code': code})
                else:
                    video_record.update(json.loads((work / f'{mode}-{index}' / 'worker.json').read_text()))
                record['videos'].append(video_record)
                del active[index]
            if record['failures']:
                break
            while len(active) < parallel and launched < 2:
                directory = work / f'{mode}-{launched}'
                directory.mkdir()
                handle = (directory / 'worker.log').open('w')
                handles.append(handle)
                process = subprocess.Popen([
                    sys.executable, str(Path(__file__).resolve()), '--worker',
                    '--manifest', str(path), '--index', str(launched), '--work', str(directory)],
                    stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
                processes.append(process)
                active[launched] = process
                launched += 1
            if active:
                time.sleep(.25)
    finally:
        stop_groups(processes)
        for handle in handles:
            handle.close()
        record['wall_seconds'] = time.perf_counter() - start
        samples = record['samples']
        record['peak_combined_rss_bytes'] = max((s['rss_bytes'] for s in samples), default=0)
        record['minimum_available_bytes'] = min((s['available_bytes'] for s in samples), default=None)
        record['mean_cpu_percent'] = (sum(s['cpu_percent'] * s['interval_seconds'] for s in samples)
                                      / max(sum(s['interval_seconds'] for s in samples), .0001))
        record['peak_cpu_percent'] = max((s['cpu_percent'] for s in samples), default=0)
        record['logs'] = {
            directory.name: (directory / 'worker.log').read_text(errors='replace')[-12000:]
            for directory in work.glob(f'{mode}-*') if (directory / 'worker.log').is_file()
        }
    if not record['aborted'] and not record['failures']:
        for video in record['videos']:
            index = video['index']
            try:
                video['correctness'] = verify_video(work / f'{mode}-{index}' / 'video.mp4',
                                                     manifest['videos'][index], video['config'])
                if not video['correctness']['passed']:
                    record['failures'].append({'index': index, 'reason': 'Video correctness failed'})
            except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
                record['failures'].append({'index': index, 'reason': f'Validation failed: {exc}'})
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--run', action='store_true', help='Explicitly opt into four real renders')
    parser.add_argument('--result', type=Path, default=Path('outputs/paired-render-benchmark.json'))
    parser.add_argument('--headroom-gib', type=float, default=2)
    parser.add_argument('--render-gib', type=float, default=6.5, help='Conservative per-render admission estimate')
    parser.add_argument('--max-combined-gib', type=float, default=14)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--index', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--work', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest.resolve())
    if args.worker:
        worker(manifest, args.index, args.work)
        return
    if not args.run:
        parser.error('--run is required: this benchmark renders four videos')
    if os.name != 'posix':
        parser.error('POSIX process groups are required for safe cancellation')
    if any(not math.isfinite(value) or value <= 0 for value in
           (args.headroom_gib, args.render_gib, args.max_combined_gib)):
        parser.error('Memory limits must be finite and positive')
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    import psutil
    result = {'production_render_limit_changed': False, 'runs': [],
              'compiler_sha256': hashlib.sha256((Path(__file__).resolve().parents[1] / 'shorts_core.py').read_bytes()).hexdigest(),
              'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              'headroom_gib': args.headroom_gib, 'render_estimate_gib': args.render_gib,
              'max_combined_gib': args.max_combined_gib,
              'system_total_bytes': psutil.virtual_memory().total}
    args.result.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='slopengine-paired-') as temporary:
            work = Path(temporary)
            normalized = work / 'manifest.json'
            normalized.write_text(json.dumps(manifest))
            for mode in ('sequential', 'concurrent'):
                record = run_pair(mode, manifest, normalized, work, args, psutil)
                result['runs'].append(record)
                print(f'{mode}: {record["wall_seconds"]:.2f}s; abort={record["aborted"]}; failures={record["failures"]}', flush=True)
                if record['aborted'] or record['failures']:
                    break
            if len(result['runs']) == 2 and all(not r['aborted'] and not r['failures'] for r in result['runs']):
                result['throughput_speedup'] = result['runs'][0]['wall_seconds'] / result['runs'][1]['wall_seconds']
    except KeyboardInterrupt:
        result['interrupted'] = True
        raise
    finally:
        args.result.write_text(json.dumps(result, indent=2) + '\n')
    if len(result['runs']) != 2 or any(r['aborted'] or r['failures'] for r in result['runs']):
        raise SystemExit('Benchmark incomplete or failed; see result JSON')


if __name__ == '__main__':
    main()
