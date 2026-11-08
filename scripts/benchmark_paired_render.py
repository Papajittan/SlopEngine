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


def unblock_child_signal_mask():
    if hasattr(signal, 'pthread_sigmask'):
        signal.pthread_sigmask(signal.SIG_SETMASK, set())


def load_manifest(path):
    """Resolve only local assets; credentials and network settings are rejected."""
    data = json.loads(path.read_text())
    if not data.get('videos'):
        raise ValueError('Manifest must contain at least one video')
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


def media_duration(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                             'format=duration', '-of', 'json', str(path)],
                            check=True, capture_output=True, text=True)
    return float(json.loads(result.stdout)['format']['duration'])


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def workload_info(video, config, caption_stride=1, include_hashes=True):
    audio = Path(video['audio'])
    duration = video.get('_benchmark_audio_duration_seconds')
    if duration is None:
        duration = media_duration(audio)
    events = video['word_events'][::caption_stride]
    backgrounds = []
    for path in video['backgrounds']:
        info = {'path': path, 'bytes': Path(path).stat().st_size}
        if include_hashes:
            info['sha256'] = sha256_file(path)
        backgrounds.append(info)
    extra_assets = {}
    for key in ('background_music_path', 'caption_font_path'):
        value = config.get(key)
        if value:
            asset = Path(value)
            info = {'path': str(asset), 'bytes': asset.stat().st_size}
            if include_hashes:
                info['sha256'] = sha256_file(asset)
            extra_assets[key] = info
    audio_info = {'path': str(audio), 'bytes': audio.stat().st_size}
    if include_hashes:
        audio_info['sha256'] = sha256_file(audio)
    return {
        'name': video.get('name', audio.stem),
        'audio': audio_info,
        'backgrounds': backgrounds,
        'extra_assets': extra_assets,
        'audio_duration_seconds': duration,
        'caption_count': len(events),
        'caption_events_per_second': len(events) / max(duration, .0001),
        'caption_stride': caption_stride,
        'frame_size': [config.get('width', 1080), config.get('height', 1920)],
        'fps': config.get('fps', 30),
        'frame_pixels': config.get('width', 1080) * config.get('height', 1920),
        'frame_pixels_per_second': config.get('width', 1080) * config.get('height', 1920) * config.get('fps', 30),
        'background_count': len(video['backgrounds']),
    }


def worker(manifest, index, work, caption_stride=1):
    # Import only in the isolated worker. Never construct ShortsPipeline, TTS,
    # Whisper, downloaders or uploaders; the compiler reads local files only.
    import shorts_core as core
    settings = dict(manifest.get('config', {}))
    if settings.get('background_music_path'):
        settings['background_music_path'] = Path(settings['background_music_path'])
    config = core.PipelineConfig(gemini_api_key='', pexels_api_key='', **settings)
    item = manifest['videos'][index]
    events = [core.WordEvent(**event) for event in item['word_events'][::caption_stride]]
    timings = {}
    stage_events_path = work / 'stage-events.jsonl'
    stage_started = time.monotonic()

    def mark(event_type, stage, **values):
        record = {'type': event_type, 'stage': stage,
                  'monotonic_seconds': time.monotonic(),
                  'elapsed_seconds': time.monotonic() - stage_started, **values}
        with stage_events_path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def timed(stage, operation):
        def wrapped(*args, **kwargs):
            started = time.monotonic()
            mark('stage_started', stage)
            try:
                return operation(*args, **kwargs)
            finally:
                elapsed = time.monotonic() - started
                timings[stage] = timings.get(stage, 0) + elapsed
                mark('stage_completed', stage, duration_seconds=elapsed)
        return wrapped

    compiler = core.MoviePyShortCompiler(config)
    for method in ('_build_background', '_build_audio'):
        setattr(compiler, method, timed(method.removeprefix('_build_'), getattr(compiler, method)))

    caption_count = len(events)
    caption_seen = 0
    caption_started = None
    original_word_layer = compiler._word_layer

    def timed_word_layer(*args, **kwargs):
        nonlocal caption_seen, caption_started
        if caption_started is None:
            caption_started = time.monotonic()
            mark('stage_started', 'captions')
        try:
            return original_word_layer(*args, **kwargs)
        finally:
            caption_seen += 1
            if caption_seen == caption_count:
                elapsed = time.monotonic() - caption_started
                timings['captions'] = elapsed
                mark('stage_completed', 'captions', duration_seconds=elapsed,
                     caption_count=caption_count)

    compiler._word_layer = timed_word_layer
    from moviepy.video.VideoClip import VideoClip
    original_write = VideoClip.write_videofile
    VideoClip.write_videofile = timed('write_videofile_including_composition_audio_mux', original_write)
    started = time.perf_counter()
    mark('stage_started', 'render')
    compiler.compile(
        Path(item['audio']), events,
        [Path(p) for p in item['backgrounds']], work / 'video.partial.mp4')
    mark('stage_completed', 'render', duration_seconds=time.perf_counter() - started)
    (work / 'video.partial.mp4').rename(work / 'video.mp4')
    (work / 'worker.json').write_text(json.dumps({
        'render_wall_seconds': time.perf_counter() - started,
        'stages_seconds': timings,
        'workload': workload_info(item, {'width': config.width, 'height': config.height,
                                          'fps': config.fps}, caption_stride, include_hashes=False),
        'config': {key: str(value) if isinstance(value, Path) else value
                   for key, value in asdict(config).items() if key in RENDER_FIELDS},
    }))
    VideoClip.write_videofile = original_write


def stop_groups(processes):
    """Stop live members of only the new sessions launched by this benchmark."""
    groups = list(dict.fromkeys(process.pid for process in processes))

    def exists(group):
        try:
            os.killpg(group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    active = [group for group in groups if exists(group)]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for group in active:
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and any(exists(group) for group in active):
                time.sleep(.05)
            active = [group for group in active if exists(group)]
            if not active:
                break
    for process in processes:
        process.wait()


def process_category(process, psutil):
    fields = []
    for getter in (process.name, process.exe, process.cmdline):
        try:
            value = getter()
        except (psutil.Error, OSError):
            continue
        fields.extend(value if isinstance(value, list) else [value])
    text = ' '.join(str(value) for value in fields).lower()
    if 'ffmpeg' in text:
        return 'ffmpeg'
    if 'python' in text:
        return 'python'
    return 'other'


class TreeSampler:
    """Sample tree memory and retain per-process CPU totals by PID identity."""

    def __init__(self, psutil):
        self.psutil = psutil
        self.previous_cpu = {}
        self.seen_cpu = {}

    def sample(self, processes):
        selected = {}
        for process in processes:
            try:
                root = self.psutil.Process(process.pid)
                for child in [root, *root.children(recursive=True)]:
                    selected[(child.pid, child.create_time())] = child
            except self.psutil.Error:
                pass
        categories = {name: {'rss_bytes': 0, 'pss_bytes_observed': 0,
                             'process_count': 0, 'pss_process_count': 0}
                      for name in ('python', 'ffmpeg', 'other')}
        cpu_delta = {name: 0.0 for name in categories}
        for identity, process in selected.items():
            category = process_category(process, self.psutil)
            try:
                categories[category]['process_count'] += 1
                categories[category]['rss_bytes'] += process.memory_info().rss
                try:
                    categories[category]['pss_bytes_observed'] += process.memory_full_info().pss
                    categories[category]['pss_process_count'] += 1
                except (self.psutil.Error, AttributeError):
                    pass
                times = process.cpu_times()
                total = times.user + times.system
                cpu_delta[category] += max(0.0, total - self.previous_cpu.get(identity, 0.0))
                self.previous_cpu[identity] = total
                old_category, old_total = self.seen_cpu.get(identity, (category, 0.0))
                self.seen_cpu[identity] = (old_category, max(total, old_total))
            except self.psutil.Error:
                pass
        for values in categories.values():
            values['pss_complete'] = values['process_count'] == values['pss_process_count']
            values['pss_bytes'] = (values['pss_bytes_observed']
                                   if values['pss_complete'] else None)
        return categories, cpu_delta

    def cpu_totals(self):
        totals = {name: 0.0 for name in ('python', 'ffmpeg', 'other')}
        for category, seconds in self.seen_cpu.values():
            totals[category] += seconds
        return totals


def temp_tree_bytes(directories):
    total = 0
    for directory in directories:
        for path in directory.rglob('*'):
            if path.is_file() and path.name not in {'worker.log', 'worker.json', 'stage-events.jsonl'}:
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    return total


def tree_sample(processes, sampler):
    return sampler.sample(processes)


def verify_video(path, video, config, caption_stride=1):
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
    events = video['word_events'][::caption_stride]
    duration = max(float(audio['format']['duration']),
                   max(e['end_ms'] for e in events) / 1000, 1)
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


def run_pair(mode, manifest, path, work, args, psutil, count=2, caption_stride=1):
    parallel = min(2, count) if mode == 'concurrent' else 1
    record = {'mode': mode, 'failures': [], 'videos': [], 'samples': [], 'aborted': None}
    processes, handles, active, launched = [], [], {}, 0
    start = last = time.monotonic()
    sampler = TreeSampler(psutil)
    swap_start = psutil.swap_memory()
    try:
        if psutil.virtual_memory().available < (args.headroom_gib + parallel * args.render_gib) * 2**30:
            record['aborted'] = 'Insufficient available RAM for measured render estimates plus headroom'
            return record
        while active or launched < count:
            available = psutil.virtual_memory().available
            categories, cpu_delta = tree_sample(processes, sampler)
            rss = sum(values['rss_bytes'] for values in categories.values())
            pss_complete = all(values['pss_complete'] for values in categories.values())
            pss = (sum(values['pss_bytes'] for values in categories.values())
                   if pss_complete else None)
            pss_observed = sum(values['pss_bytes_observed'] for values in categories.values())
            swap = psutil.swap_memory()
            now = time.monotonic()
            interval = now - last
            cpu_percent = {name: value / max(interval, .0001) * 100
                           for name, value in cpu_delta.items()}
            sample = {'monotonic_seconds': now,
                      'elapsed_seconds': now - start, 'interval_seconds': interval,
                      'rss_bytes': rss, 'pss_bytes': pss,
                      'pss_bytes_observed': pss_observed,
                      'pss_complete': pss_complete,
                      'pss_process_coverage': {
                          'observed': sum(values['pss_process_count'] for values in categories.values()),
                          'total': sum(values['process_count'] for values in categories.values())},
                      'processes': categories,
                      'cpu_seconds_by_category_interval': cpu_delta,
                      'cpu_percent_by_category': cpu_percent,
                      'cpu_percent': sum(cpu_percent.values()),
                      'temp_disk_bytes': temp_tree_bytes(
                          [work / f'{mode}-{index}' for index in active]),
                      'available_bytes': available,
                      'swap_in_bytes': swap.sin - swap_start.sin,
                      'swap_out_bytes': swap.sout - swap_start.sout}
            record['samples'].append(sample)
            last = now
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
            while len(active) < parallel and launched < count:
                directory = work / f'{mode}-{launched}'
                directory.mkdir()
                handle = (directory / 'worker.log').open('w')
                handles.append(handle)
                previous_mask = None
                if hasattr(signal, 'pthread_sigmask'):
                    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK,
                                                           {signal.SIGINT, signal.SIGTERM})
                try:
                    process = subprocess.Popen([
                        sys.executable, str(Path(__file__).resolve()), '--worker',
                        '--manifest', str(path), '--index', str(launched), '--work', str(directory),
                        '--caption-stride', str(caption_stride)],
                        stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
                        preexec_fn=unblock_child_signal_mask if hasattr(signal, 'pthread_sigmask') else None)
                    processes.append(process)
                    active[launched] = process
                    launched += 1
                finally:
                    if previous_mask is not None:
                        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            if active:
                time.sleep(getattr(args, 'sample_interval', .25))
    finally:
        stop_groups(processes)
        for handle in handles:
            handle.close()
        record['wall_seconds'] = time.monotonic() - start
        samples = record['samples']
        record['peak_combined_rss_bytes'] = max((s['rss_bytes'] for s in samples), default=0)
        record['peak_combined_pss_bytes'] = max(
            (s['pss_bytes'] for s in samples if s['pss_bytes'] is not None), default=None)
        record['peak_observed_pss_bytes'] = max((s['pss_bytes_observed'] for s in samples), default=0)
        record['peak_temp_disk_bytes'] = max((s['temp_disk_bytes'] for s in samples), default=0)
        record['temp_disk_scope'] = 'Active worker directories; includes partial/final in-progress video and temp audio, excludes fixture inputs and completed workers.'
        record['peak_memory_by_category'] = {
            name: {'rss_bytes': max((s['processes'][name]['rss_bytes'] for s in samples), default=0),
                   'pss_bytes': max((s['processes'][name]['pss_bytes'] for s in samples
                                     if s['processes'][name]['pss_bytes'] is not None), default=None),
                   'peak_observed_pss_bytes': max((s['processes'][name]['pss_bytes_observed']
                                                   for s in samples), default=0),
                   'pss_coverage_samples': [
                       {'observed': s['processes'][name]['pss_process_count'],
                        'total': s['processes'][name]['process_count']}
                       for s in samples]}
            for name in ('python', 'ffmpeg', 'other')}
        record['cpu_seconds_by_category'] = sampler.cpu_totals()
        record['minimum_available_bytes'] = min((s['available_bytes'] for s in samples), default=None)
        record['mean_cpu_percent'] = (sum(s['cpu_percent'] * s['interval_seconds'] for s in samples)
                                      / max(sum(s['interval_seconds'] for s in samples), .0001))
        record['peak_cpu_percent'] = max((s['cpu_percent'] for s in samples), default=0)
        record['logs'] = {
            directory.name: (directory / 'worker.log').read_text(errors='replace')[-12000:]
            for directory in work.glob(f'{mode}-*') if (directory / 'worker.log').is_file()
        }
        for video_record in record['videos']:
            stage_events = work / f"{mode}-{video_record['index']}" / 'stage-events.jsonl'
            if stage_events.is_file():
                video_record['stage_events'] = [json.loads(line) for line in stage_events.read_text().splitlines()]
    if not record['aborted'] and not record['failures']:
        for video in record['videos']:
            index = video['index']
            try:
                video['correctness'] = verify_video(work / f'{mode}-{index}' / 'video.mp4',
                                                     manifest['videos'][index], video['config'],
                                                     caption_stride)
                if not video['correctness']['passed']:
                    record['failures'].append({'index': index, 'reason': 'Video correctness failed'})
            except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
                record['failures'].append({'index': index, 'reason': f'Validation failed: {exc}'})
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--run', action='store_true', help='Explicitly opt into four real renders')
    parser.add_argument('--single', action='store_true', help='Run each manifest input serially')
    parser.add_argument('--caption-strides', type=int, nargs='+', default=[1],
                        help='Single mode variants: 1 keeps every word, 2 every second word, etc.')
    parser.add_argument('--sample-interval', type=float, default=.25)
    parser.add_argument('--result', type=Path, default=Path('outputs/paired-render-benchmark.json'))
    parser.add_argument('--headroom-gib', type=float, default=2)
    parser.add_argument('--render-gib', type=float, default=6.5, help='Conservative per-render admission estimate')
    parser.add_argument('--max-combined-gib', type=float, default=14)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--index', type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument('--work', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--caption-stride', type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest.resolve())
    if args.worker:
        worker(manifest, args.index, args.work, args.caption_stride)
        return
    if not args.run:
        parser.error('--run is required: this benchmark renders four videos')
    if os.name != 'posix':
        parser.error('POSIX process groups are required for safe cancellation')
    if any(stride < 1 for stride in args.caption_strides):
        parser.error('caption strides must be positive integers')
    if not math.isfinite(args.sample_interval) or args.sample_interval <= 0:
        parser.error('sample interval must be finite and positive')
    if any(not math.isfinite(value) or value <= 0 for value in
           (args.headroom_gib, args.render_gib, args.max_combined_gib)):
        parser.error('Memory limits must be finite and positive')
    if not args.single and len(manifest['videos']) != 2:
        parser.error('paired mode requires exactly two manifest videos')
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    import psutil
    fixtures = [workload_info(video, manifest.get('config', {})) for video in manifest['videos']]
    for video, fixture in zip(manifest['videos'], fixtures):
        video['_benchmark_audio_duration_seconds'] = fixture['audio_duration_seconds']
    result = {'mode': 'single' if args.single else 'paired',
              'production_render_limit_changed': False, 'runs': [], 'fixtures': fixtures,
              'fixture_limitations': manifest.get('fixture_limitations', []),
              'stage_timing_note': (
                  'Stage durations overlap: render encloses child stages. '
                  'write_videofile_including_composition_audio_mux includes MoviePy frame composition, '
                  'audio processing, FFmpeg pipe waits and muxing; do not sum stage durations.'),
              'cpu_metric_note': 'CPU percentages use 100% per logical core; CPU seconds are sampled process-tree totals.',
              'sample_interval_seconds': args.sample_interval,
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
            if args.single:
                case_number = 0
                for index, video in enumerate(manifest['videos']):
                    for stride in args.caption_strides:
                        case_number += 1
                        case = {'config': manifest.get('config', {}), 'videos': [video]}
                        case_path = work / f'manifest-{case_number:03d}.json'
                        case_path.write_text(json.dumps(case))
                        run_work = work / f'case-{case_number:03d}'
                        run_work.mkdir()
                        record = run_pair('single', case, case_path, run_work, args, psutil,
                                          count=1, caption_stride=stride)
                        record['fixture_name'] = video.get('name', f'video-{index + 1}')
                        record['caption_stride'] = stride
                        result['runs'].append(record)
                        print(f"{record['fixture_name']} stride {stride}: {record['wall_seconds']:.2f}s; "
                              f"abort={record['aborted']}; failures={record['failures']}", flush=True)
                        if record['aborted'] or record['failures']:
                            break
                    if result['runs'][-1]['aborted'] or result['runs'][-1]['failures']:
                        break
            else:
                for mode in ('sequential', 'concurrent'):
                    run_work = work / mode
                    run_work.mkdir()
                    record = run_pair(mode, manifest, normalized, run_work, args, psutil)
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
    expected_runs = (len(manifest['videos']) * len(args.caption_strides)) if args.single else 2
    if len(result['runs']) != expected_runs or any(r['aborted'] or r['failures'] for r in result['runs']):
        raise SystemExit('Benchmark incomplete or failed; see result JSON')


if __name__ == '__main__':
    main()
