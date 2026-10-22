#!/usr/bin/env python3
"""Compare every raw benchmark frame/audio sample against a saved compiler source.

Use benchmark --keep-media to supply the deterministic source videos. All scratch
arrays are automatically deleted; the result JSON and two preview PNGs remain.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-core', required=True, type=Path)
    parser.add_argument('--media', required=True, type=Path)
    parser.add_argument('--result', type=Path, default=Path('docs/benchmarks/validation.json'))
    parser.add_argument('--previews', type=Path, default=Path('/tmp/slopengine-validation'))
    parser.add_argument('--before-video', type=Path)
    parser.add_argument('--after-video', type=Path)
    args = parser.parse_args()
    import numpy as np
    from PIL import Image
    from moviepy.video.VideoClip import VideoClip
    import shorts_core

    spec = importlib.util.spec_from_file_location('reference_core', args.reference_core)
    reference = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reference
    spec.loader.exec_module(reference)
    frames = 120
    result = dict(reference_sha256=hashlib.sha256(args.reference_core.read_bytes()).hexdigest(),
                  frames_compared=frames, fps=30, duration_seconds=4, width=1080, height=1920,
                  raw_pixels_equal=True, raw_audio_equal=True, max_pixel_difference=0,
                  caption_events=[dict(text='measure', start_ms=0, end_ms=250),
                                  dict(text='every', start_ms=250, end_ms=500),
                                  dict(text='overlap', start_ms=450, end_ms=700),
                                  dict(text='tiny', start_ms=1000, end_ms=1010),
                                  dict(text='synchronized', start_ms=1999, end_ms=2500),
                                  dict(text='end', start_ms=3500, end_ms=4000)])
    args.previews.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='slopengine-validate-') as temporary:
        directory = Path(temporary)
        from moviepy.config import FFMPEG_BINARY
        for name, frequency in [('narration', 440), ('music', 220)]:
            subprocess.run([FFMPEG_BINARY, '-v', 'error', '-f', 'lavfi', '-i',
                            f'sine=frequency={frequency}:sample_rate=44100:duration=4',
                            str(directory / f'{name}.wav')], check=True)
        # Two source clips repeat twice, exercising joins and loop boundaries.
        for core, label in [(reference, 'before'), (shorts_core, 'after')]:
            compiler = core.MoviePyShortCompiler(core.PipelineConfig(
                gemini_api_key='', pexels_api_key='', background_music_path=directory / 'music.wav'))
            events = [core.WordEvent(**event) for event in result['caption_events']]
            def capture(clip, *a, **kw):
                result[label + '_mask_present'] = clip.mask is not None
                audio = clip.audio.to_soundarray(fps=44100)
                if label == 'before':
                    np.save(directory / 'audio.npy', audio)
                else:
                    result['raw_audio_equal'] = bool(np.array_equal(np.load(directory / 'audio.npy'), audio))
                result['audio_samples'] = len(audio)
                for index in range(frames):
                    frame = clip.get_frame(index / 30).astype('uint8')
                    path = directory / f'{index}.npy'
                    if label == 'before':
                        np.save(path, frame)
                    else:
                        old = np.load(path)
                        difference = int(np.abs(old.astype('int16') - frame.astype('int16')).max())
                        result['max_pixel_difference'] = max(result['max_pixel_difference'], difference)
                        result['raw_pixels_equal'] &= bool(np.array_equal(old, frame))
                    if index == 60:
                        Image.fromarray(frame).save(args.previews / f'{label}.png')
            with patch.object(VideoClip, 'write_videofile', capture):
                compiler.compile(directory / 'narration.wav', events,
                                 [args.media / 'landscape.mp4', args.media / 'portrait.mp4'], directory / 'unused.mp4')
            def capture_fallback(clip, *a, **kw):
                frame = clip.get_frame(0)
                result[label + '_empty_captions_fallback_correct'] = bool(
                    frame.shape == (1920, 1080, 3) and np.all(frame == (18, 20, 24))
                    and clip.duration == 4)
            with patch.object(VideoClip, 'write_videofile', capture_fallback):
                compiler.compile(directory / 'narration.wav', [], [], directory / 'unused.mp4')
    if args.before_video and args.after_video:
        hashes = []
        for video in (args.before_video, args.after_video):
            subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-f', 'null', '-'], check=True)
            checksums = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(video),
                                                '-map', '0:v:0', '-f', 'framemd5', '-'], text=True)
            frames_md5 = [line for line in checksums.splitlines() if not line.startswith('#')]
            audio = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(video),
                                             '-map', '0:a:0', '-f', 's16le', '-'])
            hashes.append((frames_md5, hashlib.sha256(audio).hexdigest()))
        result['encoded_videos_decode_without_errors'] = True
        result['decoded_video_frames_equal'] = hashes[0][0] == hashes[1][0]
        result['decoded_audio_pcm_equal'] = hashes[0][1] == hashes[1][1]
        result['decoded_frame_count'] = [len(h[0]) for h in hashes]
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    if not result['raw_pixels_equal'] or not result['raw_audio_equal']:
        raise SystemExit('Raw output differs; investigate before accepting the optimization')


if __name__ == '__main__':
    main()
