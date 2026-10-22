#!/usr/bin/env python3
"""Generate ONE real production video, without upload, and record stage resources.

This command calls the configured Gemini, Edge TTS and Pexels services. Unlike
benchmark_render.py it is not an offline benchmark. Temporary captured inputs
are automatically removed; final videos/diagnostic previews belong in outputs/.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.benchmark_render import diagnostic, snapshot


def worker(args):
    import shutil
    from slopengine.settings import SettingsStore, build_config, redact, validate_config

    work = Path(args.work)
    events_path = work / 'events.jsonl'
    def event(kind, **values):
        record = dict(type=kind, monotonic=time.perf_counter(), **values)
        with events_path.open('a') as handle:
            handle.write(json.dumps(record) + '\n')

    config = build_config(SettingsStore().load(), {'youtube_upload_enabled': False})
    errors = validate_config(config, topic=args.topic, output=args.output, require_keys=True)
    if errors:
        raise ValueError(' '.join(errors))
    safe_fields = ('gemini_model', 'edge_voice', 'edge_rate', 'edge_pitch', 'edge_volume',
                   'whisper_model', 'whisper_device', 'width', 'height', 'fps', 'threads',
                   'caption_font_size', 'caption_font_path', 'caption_color', 'caption_stroke_color',
                   'caption_stroke_width', 'caption_y_ratio', 'background_music_volume', 'keep_temp',
                   'youtube_upload_enabled')
    result = dict(config={key: getattr(config, key) for key in safe_fields},
                  music_enabled=config.background_music_path is not None,
                  compiler_sha256=hashlib.sha256(Path('shorts_core.py').read_bytes()).hexdigest(),
                  topic=args.topic, stages_seconds={}, timings={}, sources=[], upload_invoked=False)

    class SafeLog(io.TextIOBase):
        def write(self, value):
            with (work / 'generation.log').open('a') as handle:
                handle.write(redact(value))
            return len(value)
        def flush(self):
            pass

    stage_starts = {}
    def progress(record):
        event(**{'kind': record['type'], 'stage': record.get('stage')})
        stage = record.get('stage')
        if record['type'] == 'stage_started':
            stage_starts[stage] = time.perf_counter()
        elif record['type'] == 'stage_completed':
            result['stages_seconds'][stage] = time.perf_counter() - stage_starts[stage]

    started = time.perf_counter()
    try:
        with redirect_stdout(SafeLog()), redirect_stderr(SafeLog()):
            import shorts_core as core
            import moviepy
            from moviepy.config import FFMPEG_BINARY
            from moviepy.video.VideoClip import VideoClip
            from moviepy.audio.AudioClip import AudioClip
            from moviepy.video.io.ffmpeg_writer import FFMPEG_VideoWriter
            result.update(python=sys.version, moviepy=moviepy.__version__, ffmpeg_binary=FFMPEG_BINARY,
                          ffmpeg_version=diagnostic([FFMPEG_BINARY, '-version']))
            pipeline = core.ShortsPipeline(config, event_handler=progress)
            compiler = pipeline.compiler

            for name in ('_build_background', '_word_layer', '_build_audio'):
                original = getattr(compiler, name)
                def timed(*a, _name=name, _original=original, **kw):
                    start = time.perf_counter()
                    try:
                        answer = _original(*a, **kw)
                        if _name == '_build_background':
                            for clip in a[2]:
                                reader = getattr(clip, 'reader', None)
                                if reader:
                                    result['sources'].append(dict(
                                        size=list(clip.size), duration=clip.duration, fps=clip.fps,
                                        decoder_command=getattr(reader.proc, 'args', None)))
                            result['background_size'] = list(answer.size)
                            result['background_mask_present'] = answer.mask is not None
                        return answer
                    finally:
                        result['timings'][_name] = result['timings'].get(_name, 0) + time.perf_counter() - start
                setattr(compiler, name, timed)

            original_writer_init = FFMPEG_VideoWriter.__init__
            def writer_init(writer, *a, **kw):
                original_writer_init(writer, *a, **kw)
                result['encoder_command'] = writer.proc.args
            FFMPEG_VideoWriter.__init__ = writer_init
            original_frame = FFMPEG_VideoWriter.write_frame
            def write_frame(writer, frame):
                now = time.perf_counter()
                result.setdefault('first_frame_monotonic', now)
                start = now
                original_frame(writer, frame)
                result['last_frame_monotonic'] = time.perf_counter()
                result['frames_written'] = result.get('frames_written', 0) + 1
                result['timings']['encoder_write_frame'] = result['timings'].get('encoder_write_frame', 0) + time.perf_counter() - start
            FFMPEG_VideoWriter.write_frame = write_frame

            for cls, name in ((VideoClip, 'write_videofile'), (AudioClip, 'write_audiofile')):
                original = getattr(cls, name)
                def timed_write(clip, *a, _name=name, _original=original, **kw):
                    if _name == 'write_videofile':
                        result['final_mask_present'] = clip.mask is not None
                        result['final_duration_seconds'] = clip.duration
                        result['writer_settings'] = {k: kw.get(k) for k in ('fps','codec','audio_codec','preset','threads','bitrate')}
                    start = time.perf_counter()
                    try:
                        return _original(clip, *a, **kw)
                    finally:
                        result['timings'][_name] = time.perf_counter() - start
                setattr(cls, name, timed_write)

            original_compile = compiler.compile
            def compile_capture(audio_path, word_events, background_paths, output_path):
                event('render_measurement_started')
                start = time.perf_counter()
                try:
                    output = original_compile(audio_path, word_events, background_paths, output_path)
                finally:
                    result['render_wall_seconds'] = time.perf_counter() - start
                    event('render_measurement_completed')
                # Copy only after the production render timer stops. Keep normal
                # pipeline cleanup enabled; the enclosing TemporaryDirectory owns
                # these diagnostic copies until offline inspection is finished.
                capture_start = time.perf_counter()
                captured_audio = work / ('narration' + audio_path.suffix)
                shutil.copy2(audio_path, captured_audio)
                backgrounds = []
                for i, path in enumerate(background_paths):
                    target = work / f'background-{i}{path.suffix}'
                    shutil.copy2(path, target)
                    backgrounds.append(str(target))
                result['captured_inputs'] = dict(audio=str(captured_audio), backgrounds=backgrounds,
                                                word_events=[asdict(e) for e in word_events])
                result['diagnostic_copy_seconds'] = time.perf_counter() - capture_start
                return output
            compiler.compile = compile_capture
            output = pipeline.run(args.topic, Path(args.output))
            result['output'] = str(output)
            result['generation_wall_seconds_including_diagnostic_copy'] = time.perf_counter() - started
            result['effective_fps'] = result['frames_written'] / result['render_wall_seconds']
            result['frame_loop_fps'] = (result['frames_written'] - 1) / (result['last_frame_monotonic'] - result['first_frame_monotonic'])
            result['font_paths'] = sorted({str(getattr(font, 'path', 'unknown')) for font in compiler._font_cache.values()})
    except Exception as exc:
        result['error'] = redact(f'{type(exc).__name__}: {exc}')
        event('validation_failed', message=result['error'])
    finally:
        result['worker_wall_seconds'] = time.perf_counter() - started
        (work / 'worker.json').write_text(json.dumps(result, indent=2) + '\n')
    return 1 if 'error' in result else 0


def summarize_samples(samples, start, end):
    selected = [s for s in samples if start <= s['monotonic'] <= end]
    if not selected:
        return None
    return dict(samples=len(selected), mean_cpu_percent=sum(s['cpu_percent'] * s['interval'] for s in selected) / sum(s['interval'] for s in selected),
                peak_cpu_percent=max(s['cpu_percent'] for s in selected),
                peak_rss_bytes=max(s['rss_bytes'] for s in selected),
                minimum_mem_available_bytes=min(s['mem_available_bytes'] for s in selected),
                gpu_samples=[s['gpu'] for s in selected if s.get('gpu')])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--topic', default='How a forest changes through the four seasons')
    parser.add_argument('--result', type=Path, default=Path('docs/benchmarks/production.json'))
    parser.add_argument('--work', help=argparse.SUPPRESS)
    parser.add_argument('--output', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.work:
        raise SystemExit(worker(args))
    from slopengine.settings import SettingsStore, build_config
    config = build_config(SettingsStore().load(), {'youtube_upload_enabled': False})
    config.temp_root.mkdir(parents=True, exist_ok=True)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    samples = []
    events = []
    with tempfile.TemporaryDirectory(prefix='real-validation-', dir=config.temp_root) as temporary:
        work = Path(temporary)
        output = config.output_dir / f'{time.time_ns()}-forest-render-validation.mp4'
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--work', temporary,
                                    '--output', str(output), '--topic', args.topic])
        previous = snapshot([process.pid])
        last = time.perf_counter()
        started = last
        next_gpu = last
        gpu = None
        last_event_count = 0
        last_status = last
        try:
            while process.poll() is None:
                current = snapshot([process.pid])
                now = time.perf_counter()
                if now >= next_gpu:
                    gpu = diagnostic(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,utilization.encoder,utilization.decoder', '--format=csv,noheader,nounits'])
                    next_gpu = now + 1
                cpu_delta = sum(max(0, value - previous['per_pid_cpu'].get(pid, 0)) for pid, value in current['per_pid_cpu'].items())
                meminfo = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
                samples.append(dict(monotonic=now, interval=now-last, cpu_percent=cpu_delta/(now-last)*100,
                                    rss_bytes=current['rss_bytes'], mem_available_bytes=int(meminfo['MemAvailable'].split()[0])*1024,
                                    gpu=gpu.strip() if gpu else None))
                previous, last = current, now
                if (work / 'events.jsonl').exists():
                    events = [json.loads(line) for line in (work / 'events.jsonl').read_text().splitlines(keepends=True)
                              if line.endswith('\n')]
                    for e in events[last_event_count:]:
                        if e['type'] in ('stage_started', 'stage_completed', 'validation_failed'):
                            print(f"{now-started:.1f}s: {e['type']} {e.get('stage', '')} {e.get('message', '')}", flush=True)
                    last_event_count = len(events)
                if now-last_status >= 30:
                    print(f'{now-started:.0f}s: job active; CPU {samples[-1]["cpu_percent"]:.0f}%, RSS {current["rss_bytes"]/2**30:.2f} GiB', flush=True)
                    last_status = now
                time.sleep(0.25)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=30)
        result = json.loads((work / 'worker.json').read_text())
        result['resources'] = dict(whole_job=summarize_samples(samples, started, time.perf_counter()))
        for e in events:
            if e['type'] == 'render_measurement_started':
                render_start = e['monotonic']
            if e['type'] == 'render_measurement_completed':
                result['resources']['render'] = summarize_samples(samples, render_start, e['monotonic'])
        result['resource_samples'] = samples
        result['events'] = events
        result['exit_code'] = process.returncode
        log = (work / 'generation.log').read_text() if (work / 'generation.log').exists() else ''
        result['warnings'] = [line for line in log.splitlines() if any(term in line.lower() for term in ('[warn]', 'error', 'warning'))]
        args.result.write_text(json.dumps(result, indent=2) + '\n')
        print(f'Results: {args.result}', flush=True)
        if process.returncode:
            raise SystemExit(result.get('error', 'Generation failed'))
        # Inspection executes before the captured inputs are automatically removed.
        from inspect_production_render import inspect_output
        result['inspection'] = inspect_output(result, config, work)
        args.result.write_text(json.dumps(result, indent=2) + '\n')
        print(f'Video: {output}; render {result["render_wall_seconds"]:.2f}s, {result["effective_fps"]:.2f} FPS', flush=True)


if __name__ == '__main__':
    main()
