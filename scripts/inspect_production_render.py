"""Offline checks for the one production validation run; no generation API calls."""
from pathlib import Path
import json
import math
import subprocess
import time


def inspect_output(result, config, work):
    import numpy as np
    from PIL import Image
    import shorts_core as core

    output = Path(result['output'])
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-count_frames',
                       '-show_streams', '-show_format', '-of', 'json', str(output)], text=True))
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(output), '-f', 'null', '-'], check=True,
                   capture_output=True)
    inspection = dict(probe=probe, full_decode_passed=True, frames=[], audio_windows=[])
    inputs = result['captured_inputs']
    events = [core.WordEvent(**item) for item in inputs['word_events']]
    duration = result['final_duration_seconds']
    fps = config.fps
    inspection['word_count'] = len(events)
    inspection['caption_timing'] = dict(
        first_start_seconds=events[0].start_ms / 1000 if events else None,
        last_end_seconds=events[-1].end_ms / 1000 if events else None,
        nonpositive_events=sum(e.end_ms <= e.start_ms for e in events),
        events_exceeding_video=sum(e.start_ms / 1000 >= duration for e in events),
        out_of_order_starts=sum(b.start_ms < a.start_ms for a, b in zip(events, events[1:])))
    previews = output.parent / (output.stem + '.validation')
    previews.mkdir()
    compiler = core.MoviePyShortCompiler(config)
    audio_opened = []
    video = None
    actual_audio = None
    try:
        video = core.VideoFileClip(str(output), audio=False)
        indices = sorted({min(len(events)-1, i) for i in (2, len(events)//4, len(events)//2,
                                                        len(events)*3//4, len(events)-2)}) if events else []
        for i in indices:
            event = events[i]
            timestamp = (event.start_ms + event.end_ms) / 2000
            frame_index = min(int(duration * fps)-1, int(timestamp * fps))
            timestamp = frame_index / fps
            actual = video.get_frame(timestamp).astype('uint8')
            # MoviePy random seeks can return the next source frame at fractional
            # frame rates. Decode by sequential frame index for a reliable oracle.
            if result['sources']:
                position = timestamp % sum(s['duration'] for s in result['sources'])
                source_index = 0
                while position >= result['sources'][source_index]['duration']:
                    position -= result['sources'][source_index]['duration']
                    source_index += 1
                info = result['sources'][source_index]
                source_frame = int(position*info['fps']+1e-5)
                from moviepy.config import FFMPEG_BINARY
                raw = subprocess.check_output([FFMPEG_BINARY, '-v', 'error', '-i', inputs['backgrounds'][source_index],
                    '-map', '0:v:0', '-vf', f'select=eq(n\\,{source_frame})', '-frames:v', '1',
                    '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'])
                source = np.frombuffer(raw, dtype=np.uint8).reshape(info['size'][1], info['size'][0], 3)
                fitted = compiler._fit_vertical(core.ImageClip(source))
                background_frame = fitted.get_frame(0).astype('uint8')
            else:
                background_frame = np.full((config.height, config.width, 3), (18, 20, 24), dtype=np.uint8)
            base = Image.fromarray(background_frame).convert('RGBA')
            glyph_mask = np.zeros((config.height, config.width), dtype=bool)
            active = []
            for word in events:
                start = word.start_ms / 1000
                end = start + max(.05, (word.end_ms-word.start_ms)/1000)
                if start <= timestamp < end:
                    active.append(word.text)
                    image = compiler._caption_image(word.text)
                    x = int((config.width-image.width)/2)
                    y = max(0, int(config.height*config.caption_y_ratio)-image.height//2)
                    canvas = Image.new('RGBA', base.size)
                    canvas.paste(image, (x, y))
                    glyph_mask |= np.asarray(canvas)[:, :, 3] > 0
                    base = Image.alpha_composite(base, canvas)
            expected = np.asarray(base.convert('RGB'))
            difference = actual.astype(float)-expected.astype(float)
            mse = float(np.mean(difference**2))
            preview = previews / f'frame-{frame_index:05d}.png'
            Image.fromarray(actual).save(preview)
            inspection['frames'].append(dict(index=frame_index, seconds=timestamp, expected_active_words=active,
                preview=str(preview), mean_absolute_pixel_error=float(np.abs(difference).mean()),
                psnr_db=10*math.log10(255**2/mse) if mse else None,
                caption_glyph_mean_absolute_error=float(np.abs(difference[glyph_mask]).mean()) if glyph_mask.any() else None))
        mixed = compiler._build_audio(Path(inputs['audio']), duration, audio_opened)
        actual_audio = core.AudioFileClip(str(output))
        sample_rate = 44100
        for start in (0.5, max(.5, duration/2-1), max(.5, duration-3)):
            times = start + np.arange(int(min(2, duration-start-.05)*sample_rate))/sample_rate
            expected = mixed.get_frame(times)
            actual = actual_audio.get_frame(times)
            a = np.mean(actual, axis=1) if actual.ndim > 1 else actual
            b = np.mean(expected, axis=1) if expected.ndim > 1 else expected
            a, b = a-a.mean(), b-b.mean()
            fft_size = 1 << (2*len(a)-1).bit_length()
            cross = np.fft.irfft(np.fft.rfft(a, fft_size)*np.conj(np.fft.rfft(b, fft_size)), fft_size)
            lags = np.arange(-4410, 4411)
            lag = int(lags[np.argmax(cross[lags % fft_size])])
            inspection['audio_windows'].append(dict(start_seconds=start, samples=len(a),
                best_lag_samples=lag, best_lag_ms=lag/sample_rate*1000,
                zero_lag_correlation=float(np.corrcoef(a, b)[0, 1]),
                rms_difference=float(np.sqrt(np.mean((actual-expected)**2))),
                decoded_peak_amplitude=float(np.abs(actual).max())))
    finally:
        core.close_all([video, actual_audio])
        core.close_all(audio_opened)
    inspection['metadata_exists'] = output.with_suffix('.metadata.json').exists()
    inspection['output_bytes'] = output.stat().st_size
    inspection['previews_directory'] = str(previews)
    (previews / 'inspection.json').write_text(json.dumps(inspection, indent=2) + '\n')
    inspection['matched_excerpt'] = compare_excerpt(result, config, work)
    return inspection


def compare_excerpt(result, config, work):
    """Two sequential matched trials per compiler, using only the generated inputs.

    Keep every production word layer/source in setup but write only six seconds.
    This diagnoses causal speedup without another API call or a second full video.
    """
    import cProfile
    import gc
    import hashlib
    import importlib.util
    import io
    import pstats
    import sys
    from contextlib import redirect_stderr, redirect_stdout
    from unittest.mock import patch
    import shorts_core as current
    from moviepy.video.VideoClip import VideoClip

    root = Path(__file__).resolve().parents[1]
    source = work / 'baseline_core.py'
    source.write_bytes((root / 'shorts_core.py').read_bytes())
    subprocess.run(['patch', str(source), str(root / 'docs/benchmarks/baseline-compiler.patch')],
                   check=True, capture_output=True)
    original_hash = json.loads((root / 'docs/benchmarks/baseline.json').read_text())['hardware']['compiler_sha256']
    if hashlib.sha256(source.read_bytes()).hexdigest() != original_hash:
        raise RuntimeError('Reconstructed baseline compiler does not match the recorded source hash')
    spec = importlib.util.spec_from_file_location('production_reference_core', source)
    reference = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reference
    spec.loader.exec_module(reference)
    seconds = min(6, result['final_duration_seconds'])
    original_write = VideoClip.write_videofile
    def write_excerpt(clip, *a, **kw):
        trimmed = current.clip_with_duration(clip, seconds)
        trimmed = current.clip_with_audio(trimmed, current.clip_with_duration(clip.audio, seconds))
        kw['logger'] = None
        return original_write(trimmed, *a, **kw)
    records = []
    inputs = result['captured_inputs']
    for trial in range(2):
        for core, label in ((reference, 'baseline'), (current, 'optimized')):
            print(f'Offline matched real-asset excerpt: {label} trial {trial+1}', flush=True)
            gc.collect()
            events = [core.WordEvent(**item) for item in inputs['word_events']]
            compiler = core.MoviePyShortCompiler(config)
            profile = cProfile.Profile()
            output = work / f'{label}-{trial}.mp4'
            started = time.perf_counter()
            with patch.object(VideoClip, 'write_videofile', write_excerpt), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                profile.enable()
                compiler.compile(Path(inputs['audio']), events, [Path(p) for p in inputs['backgrounds']], output)
                profile.disable()
            elapsed = time.perf_counter()-started
            stats = pstats.Stats(profile)
            records.append(dict(compiler=label, trial=trial+1, seconds_written=seconds,
                frames_written=int(seconds*config.fps), wall_seconds=elapsed,
                effective_fps=int(seconds*config.fps)/elapsed, output_bytes=output.stat().st_size,
                full_project_word_count=len(events), profile=[dict(file=k[0], line=k[1], function=k[2],
                    calls=v[1], self_seconds=v[2], cumulative_seconds=v[3])
                    for k,v in sorted(stats.stats.items(), key=lambda item:-item[1][3])[:85]]))
    hashes = []
    for label in ('baseline','optimized'):
        output = work / f'{label}-1.mp4'
        frames_md5 = subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-map','0:v:0',
                                             '-f','framemd5','-'],text=True)
        frames_md5 = [line for line in frames_md5.splitlines() if not line.startswith('#')]
        pcm = subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-map','0:a:0','-f','s16le','-'])
        hashes.append((frames_md5, hashlib.sha256(pcm).hexdigest()))
    return dict(duration_seconds=seconds, trials=records, baseline_source_sha256=original_hash,
                decoded_frames_equal=hashes[0][0]==hashes[1][0], decoded_audio_equal=hashes[0][1]==hashes[1][1])
