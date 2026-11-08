#!/usr/bin/env python3
"""Compare baseline and optimized background fitting on cached local media.

This benchmark renders serially. It profiles Pillow resize calls and the fitted
background frame function separately from end-to-end render/resource metrics.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark_paired_render import (  # noqa: E402
    TreeSampler, load_manifest, sha256_file, verify_video, workload_info,
)


def make_landscape_fixture(source: Path, output: Path) -> None:
    """Create one reusable 6-second 1920x1080 crop for resize-heavy cases."""
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
        '-vf', 'scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080',
        '-an', '-t', '6', '-r', '30', '-c:v', 'libx264', '-preset', 'medium',
        '-threads', '11', '-crf', '18', '-pix_fmt', 'yuv420p', str(output),
    ], check=True)


def import_compiler(source: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f'Cannot load compiler module from {source}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def instrument_compiler(core):
    """Measure Pillow resize calls and fitted-frame calls without retaining frames."""
    resize_samples = {'count': 0, 'wall_seconds': 0., 'process_cpu_seconds': 0.}
    frame_samples = {'count': 0, 'wall_seconds': 0., 'process_cpu_seconds': 0.}
    original_resize = core.Image.Image.resize

    def measured_resize(image, *args, **kwargs):
        wall_start = time.perf_counter()
        cpu_start = time.process_time()
        try:
            return original_resize(image, *args, **kwargs)
        finally:
            resize_samples['count'] += 1
            resize_samples['wall_seconds'] += time.perf_counter() - wall_start
            resize_samples['process_cpu_seconds'] += time.process_time() - cpu_start

    core.Image.Image.resize = measured_resize
    original_fit = core.MoviePyShortCompiler._fit_vertical

    def measured_fit(self, clip, *args, **kwargs):
        fitted = original_fit(self, clip, *args, **kwargs)
        frame_function = getattr(fitted, 'frame_function', None)
        if callable(frame_function):
            def measured_frame(t):
                wall_start = time.perf_counter()
                cpu_start = time.process_time()
                try:
                    return frame_function(t)
                finally:
                    frame_samples['count'] += 1
                    frame_samples['wall_seconds'] += time.perf_counter() - wall_start
                    frame_samples['process_cpu_seconds'] += time.process_time() - cpu_start
            fitted.frame_function = measured_frame
        return fitted

    core.MoviePyShortCompiler._fit_vertical = measured_fit
    return resize_samples, frame_samples


def worker(args) -> None:
    core = import_compiler(args.compiler_source, f'shorts_core_{args.variant}_{os.getpid()}')
    manifest = json.loads(args.manifest_internal.read_text())
    config_data = dict(manifest['config'])
    if config_data.get('background_music_path'):
        config_data['background_music_path'] = Path(config_data['background_music_path'])
    config = core.PipelineConfig(gemini_api_key='', pexels_api_key='', **config_data)
    item = manifest['video']
    events = [core.WordEvent(**event) for event in item['word_events']]
    resize_samples, frame_samples = instrument_compiler(core)
    folder = args.folder
    folder.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    core.MoviePyShortCompiler(config).compile(
        Path(item['audio']), events, [Path(path) for path in item['backgrounds']], folder / 'video.mp4')
    render_wall = time.perf_counter() - started
    (folder / 'worker.json').write_text(json.dumps({
        'variant': args.variant,
        'render_wall_seconds': render_wall,
        'profile_instrumentation': True,
        'profile_note': 'Pillow resize timings and fitted-frame timings are diagnostic; render wall time includes profiler overhead.',
        'resize_count': resize_samples['count'],
        'resize_wall_seconds': resize_samples['wall_seconds'],
        'resize_process_cpu_seconds': resize_samples['process_cpu_seconds'],
        'fitted_frame_count': frame_samples['count'],
        'fitted_frame_wall_seconds': frame_samples['wall_seconds'],
        'fitted_frame_process_cpu_seconds': frame_samples['process_cpu_seconds'],
        'preprocessing_seconds': 0.,
    }))


def unblock_child_signal_mask():
    if hasattr(signal, 'pthread_sigmask'):
        signal.pthread_sigmask(signal.SIG_SETMASK, set())


def stop_worker(process: subprocess.Popen) -> None:
    from scripts.benchmark_offline_render import stop_worker_group
    stop_worker_group(process)


def run_once(variant: str, source: Path, manifest: dict, work: Path, psutil, sample_interval: float):
    work.mkdir(parents=True, exist_ok=True)
    manifest_path = work / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest))
    command = [sys.executable, str(Path(__file__).resolve()), '--worker',
               '--variant', variant, '--compiler-source', str(source),
               '--manifest-internal', str(manifest_path), '--folder', str(work)]
    started = time.monotonic()
    previous_mask = None
    process = None
    samples = []
    try:
        if hasattr(signal, 'pthread_sigmask'):
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            with (work / 'worker.log').open('w') as log_stream:
                process = subprocess.Popen(command, stdout=log_stream,
                                           stderr=subprocess.STDOUT, start_new_session=True,
                                           preexec_fn=unblock_child_signal_mask if hasattr(signal, 'pthread_sigmask') else None)
        finally:
            if previous_mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        sampler = TreeSampler(psutil)
        while process.poll() is None:
            categories, cpu_delta = sampler.sample([process])
            intermediate_bytes = sum(path.stat().st_size for path in work.glob('*.m4a') if path.is_file())
            samples.append({
                'elapsed_seconds': time.monotonic() - started,
                'rss_bytes': sum(values['rss_bytes'] for values in categories.values()),
                'pss_bytes': sum(values['pss_bytes'] for values in categories.values())
                if all(values['pss_bytes'] is not None for values in categories.values()) else None,
                'cpu_seconds_by_category_interval': cpu_delta,
                'processes': categories,
                'intermediate_bytes': intermediate_bytes,
            })
            time.sleep(sample_interval)
    finally:
        if process is not None:
            stop_worker(process)
    wall = time.monotonic() - started
    code = process.wait()
    if code:
        log = (work / 'worker.log').read_text(errors='replace')[-12000:]
        raise RuntimeError(f'{variant} render failed ({code}):\n{log}')
    data = json.loads((work / 'worker.json').read_text())
    data.update({
        'process_wall_seconds_including_import': wall,
        'process_cpu_seconds_by_category': sampler.cpu_totals(),
        'peak_tree_rss_bytes': max((sample['rss_bytes'] for sample in samples), default=0),
        'peak_tree_pss_bytes': max((sample['pss_bytes'] for sample in samples if sample['pss_bytes'] is not None), default=None),
        'resource_samples': samples,
        'peak_intermediate_bytes': max((sample['intermediate_bytes'] for sample in samples), default=0),
        'video_sha256': sha256_file(work / 'video.mp4'),
        'video_bytes': (work / 'video.mp4').stat().st_size,
        'verification': verify_video(work / 'video.mp4', manifest['video'], manifest['config']),
    })
    if not data['verification']['passed']:
        raise RuntimeError(f"Output verification failed: {data['verification']['errors']}")
    return data


def stream_compare(left: Path, right: Path, selector: list[str]) -> dict:
    def launch(path):
        return subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(path),
                                 *selector], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
    left_process, right_process = launch(left), launch(right)
    left_hash, right_hash = hashlib.sha256(), hashlib.sha256()
    mismatch = None
    offset = 0
    try:
        while True:
            a = left_process.stdout.read(1024 * 1024)
            b = right_process.stdout.read(1024 * 1024)
            left_hash.update(a)
            right_hash.update(b)
            if mismatch is None and a != b:
                limit = min(len(a), len(b))
                mismatch = offset + next((i for i, (x, y) in enumerate(zip(a[:limit], b[:limit])) if x != y), limit)
            offset += max(len(a), len(b))
            if not a and not b:
                break
        left_err, right_err = left_process.communicate()[1], right_process.communicate()[1]
        if left_process.returncode or right_process.returncode:
            raise RuntimeError(f'ffmpeg stream decode failed: {left_err!r}; {right_err!r}')
    finally:
        for process in (left_process, right_process):
            if process.poll() is None:
                process.kill()
                process.wait()
    return {'equal': mismatch is None and left_hash.digest() == right_hash.digest(),
            'first_differing_byte': mismatch, 'bytes_compared': offset,
            'baseline_sha256': left_hash.hexdigest(), 'optimized_sha256': right_hash.hexdigest()}


def run_parity(args, manifest, landscape, optimized_source):
    import psutil
    variant_paths = {'baseline': args.baseline_source.resolve(), 'optimized': optimized_source}
    item = dict(manifest['videos'][0])
    item['backgrounds'] = [str(landscape)]
    item['word_events'] = item['word_events']
    parity_manifest = {'config': manifest['config'], 'video': item}
    preview_dir = Path(args.preview_dir).resolve()
    preview_dir.mkdir(parents=True, exist_ok=True)
    records = {}
    with tempfile.TemporaryDirectory(prefix='slopengine-m13-parity-') as temporary:
        work = Path(temporary)
        for variant, source in variant_paths.items():
            records[variant] = run_once(variant, source, parity_manifest,
                                        work / variant, psutil, args.sample_interval)
        paths = {variant: work / variant / 'video.mp4' for variant in variant_paths}
        frames = stream_compare(paths['baseline'], paths['optimized'],
                                ['-map', '0:v:0', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'])
        audio = stream_compare(paths['baseline'], paths['optimized'],
                               ['-map', '0:a:0', '-acodec', 'pcm_s16le', '-f', 's16le', 'pipe:1'])
        previews = {}
        for variant, path in paths.items():
            preview = preview_dir / f'{variant}-30s.png'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', '10',
                            '-i', str(path), '-frames:v', '1', str(preview)], check=True)
            previews[variant] = str(preview)
        return {'records': records, 'video_frame_streams': frames, 'audio_streams': audio,
                'preview_pngs': previews, 'passed': frames['equal'] and audio['equal']}


def make_cases(manifest, landscape):
    by_duration = {int(round(float(item.get('name', '0').rstrip('s')))): item
                   for item in manifest['videos']}
    cases = []
    for duration in (30, 60):
        if duration not in by_duration:
            raise ValueError(f'Manifest is missing cached {duration}s input')
        item = by_duration[duration]
        for density, stride in (('low', 2), ('high', 1)):
            for geometry, background in (('resize-heavy', landscape), ('native-control', Path(item['backgrounds'][0]))):
                case_item = dict(item)
                case_item['backgrounds'] = [str(background)]
                case_item['word_events'] = item['word_events'][::stride]
                cases.append((f'{duration}s-{density}-{geometry}', case_item))
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--baseline-source', type=Path)
    parser.add_argument('--run', action='store_true', help='Render paired benchmark cases')
    parser.add_argument('--parity', action='store_true', help='Stream-compare baseline and optimized output')
    parser.add_argument('--trials', type=int, default=2)
    parser.add_argument('--sample-interval', type=float, default=.25)
    parser.add_argument('--result', type=Path, default=Path('outputs/background-resize-benchmark.json'))
    parser.add_argument('--preview-dir', type=Path, default=Path('/tmp/slopengine-milestone13-preview'))
    parser.add_argument('--geometries', nargs='+', choices=('resize-heavy', 'native-control'),
                        default=['resize-heavy', 'native-control'])
    parser.add_argument('--durations', nargs='+', type=int, choices=(30, 60), default=[30, 60])
    parser.add_argument('--densities', nargs='+', choices=('low', 'high'), default=['low', 'high'])
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--variant', choices=('baseline', 'optimized'), help=argparse.SUPPRESS)
    parser.add_argument('--compiler-source', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--manifest-internal', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--folder', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    if not (args.run or args.parity):
        parser.error('specify --run or --parity')
    if args.manifest is None or args.baseline_source is None:
        parser.error('--manifest and --baseline-source are required')
    import math
    if args.trials < 1 or not math.isfinite(args.sample_interval) or args.sample_interval <= 0:
        parser.error('--trials and --sample-interval must be positive')
    args.baseline_source = args.baseline_source.resolve()
    if not args.baseline_source.is_file():
        parser.error('baseline source file does not exist')
    manifest = load_manifest(args.manifest.resolve())
    for name, value in {'width': 1080, 'height': 1920, 'fps': 30, 'threads': 11}.items():
        if manifest['config'].get(name) != value:
            parser.error(f'milestone benchmark requires {name}={value}')
    import psutil
    import platform
    import moviepy
    import PIL
    import numpy
    from moviepy.config import FFMPEG_BINARY
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='slopengine-m13-fixtures-') as temporary:
        fixture_dir = Path(temporary)
        landscape = fixture_dir / 'landscape-1920x1080-6s.mp4'
        fixture_started = time.perf_counter()
        make_landscape_fixture(Path(manifest['videos'][0]['backgrounds'][0]), landscape)
        fixture_seconds = time.perf_counter() - fixture_started
        optimized_source = fixture_dir / 'optimized.py'
        shutil.copy2(ROOT / 'shorts_core.py', optimized_source)
        base_result = {
            'benchmark': 'milestone-1.3-background-resize',
            'manifest_sha256': sha256_file(args.manifest.resolve()),
            'baseline_sha256': sha256_file(args.baseline_source),
            'optimized_sha256': sha256_file(optimized_source),
            'landscape_fixture_sha256': sha256_file(landscape),
            'landscape_fixture': {'size': [1920, 1080], 'fps': 30, 'seconds': 6},
            'render_config': manifest['config'],
            'environment': {'python': sys.version, 'platform': platform.platform(),
                            'cpu_count': os.cpu_count(), 'moviepy': moviepy.__version__,
                            'pillow': PIL.__version__, 'numpy': numpy.__version__,
                            'ffmpeg_binary': FFMPEG_BINARY,
                            'ffmpeg_version': subprocess.check_output([FFMPEG_BINARY, '-version'], text=True)},
            'fixture_preparation_seconds': fixture_seconds,
            'fixture_preparation_bytes': landscape.stat().st_size,
            'fixture_note': 'The common generated landscape input is prepared once before both variants; it is not renderer preprocessing. Both renderer preprocessing costs are zero.',
            'sample_interval_seconds': args.sample_interval,
            'resource_note': 'CPU uses last observed process totals and can miss short-lived processes/final increments. RSS sums shared pages; PSS apportions them. Sampling can miss transient peaks. Imports are included in CPU/memory; render wall excludes imports. Intermediate bytes include temporary encoded audio, excluding inputs and final output. /tmp is tmpfs.',
            'profiler_note': 'Lightweight wall/process_time counters instrument all Pillow resize calls (not just backgrounds) and fitted-background frame functions. Their regions overlap. The same instrumentation runs in both variants and its overhead is included in render wall; these diagnostic region timings are not independent full-render speedups.',
            'runs': [],
        }
        if args.parity:
            parity = run_parity(args, manifest, landscape, optimized_source)
            base_result['parity'] = parity
            if not parity['passed']:
                args.result.parent.mkdir(parents=True, exist_ok=True)
                args.result.write_text(json.dumps(base_result, indent=2) + '\n')
                raise SystemExit('Parity failed; see result JSON')
        if args.run:
            cases = make_cases(manifest, landscape)
            cases = [(name, item) for name, item in cases
                     if int(name.split('s-', 1)[0]) in args.durations
                     and name.split('-')[1] in args.densities
                     and any(name.endswith(geometry) for geometry in args.geometries)]
            try:
                for case_name, item in cases:
                    case = {'config': manifest['config'], 'video': item}
                    fixture = workload_info(item, manifest['config'])
                    for trial in range(args.trials):
                        variants = ('baseline', 'optimized') if trial % 2 == 0 else ('optimized', 'baseline')
                        for variant in variants:
                            source = args.baseline_source if variant == 'baseline' else optimized_source
                            work = fixture_dir / f'{case_name}-{variant}-{trial}'
                            record = run_once(variant, source, case, work, psutil, args.sample_interval)
                            base_result['runs'].append({'case': case_name, 'variant': variant,
                                                        'trial': trial, 'workload': fixture, **record})
                            prior = [run['video_sha256'] for run in base_result['runs'] if run['case'] == case_name]
                            base_result.setdefault('encoded_file_equality_by_case', {})[case_name] = len(set(prior)) == 1
                            args.result.write_text(json.dumps(base_result, indent=2) + '\n')
                            print(f'{case_name} {variant} trial {trial + 1}: {record["render_wall_seconds"]:.1f}s', flush=True)
                            shutil.rmtree(work)
            finally:
                base_result['expected_runs'] = len(cases) * args.trials * 2
                args.result.write_text(json.dumps(base_result, indent=2) + '\n')
        else:
            args.result.write_text(json.dumps(base_result, indent=2) + '\n')
    if args.parity and not base_result.get('parity', {}).get('passed', True):
        raise SystemExit('Parity failed')
    if not all(base_result.get('encoded_file_equality_by_case', {}).values()):
        raise SystemExit('Encoded files differ; decode and investigate before accepting results')


if __name__ == '__main__':
    main()
