#!/usr/bin/env python3
"""Prepare an offline profiling manifest from a saved SlopEngine output.

The input is a finished video, so its picture may already contain captions and
its audio may already contain music. This is not the original source media.
"""
import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--music', type=Path, required=True)
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--durations', type=int, nargs='+', default=[30, 60])
    parser.add_argument('--width', type=int, default=1080)
    parser.add_argument('--height', type=int, default=1920)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--threads', type=int, default=11)
    args = parser.parse_args()
    if min(*args.durations, args.width, args.height, args.fps, args.threads) < 1:
        parser.error('durations and render settings must be positive')
    if len(args.durations) != len(set(args.durations)):
        parser.error('durations must be distinct')
    video, music = args.video.resolve(strict=True), args.music.resolve(strict=True)
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                            'stream=codec_type,duration', '-of', 'json', str(video)],
                           check=True, capture_output=True, text=True)
    streams = json.loads(probe.stdout)['streams']
    for kind in ('video', 'audio'):
        matching = [s for s in streams if s['codec_type'] == kind and s.get('duration')]
        if not matching or float(matching[0]['duration']) < max(args.durations):
            parser.error(f'input {kind} stream is shorter than a requested duration')
    events = json.loads(args.metadata.read_text())['word_events']
    args.folder.mkdir(parents=True, exist_ok=True)
    videos = []
    for duration in args.durations:
        audio = args.folder / f'narration-{duration}.wav'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i',
                        str(video), '-t', str(duration), '-vn', '-c:a', 'pcm_s16le',
                        str(audio)], check=True)
        words = [dict(event, end_ms=min(event['end_ms'], duration * 1000))
                 for event in events if event['start_ms'] < duration * 1000]
        if not words:
            parser.error(f'no word timings in first {duration} seconds')
        videos.append({'name': f'{duration}s', 'audio': str(audio.resolve()),
                       'backgrounds': [str(video)], 'word_events': words})
    manifest = {'fixture_limitations': [
                    'Finished output is reused as the only background; it may contain burned-in captions.',
                    'Extracted audio may already contain music; the configured music is mixed again.',
                    'The original source footage and isolated narration are not recovered.'],
                'config': {'width': args.width, 'height': args.height,
                           'fps': args.fps, 'threads': args.threads,
                           'background_music_path': str(music),
                           'background_music_volume': 0.1}, 'videos': videos}
    path = args.folder / 'manifest.json'
    path.write_text(json.dumps(manifest, indent=2) + '\n')
    print(path)


if __name__ == '__main__':
    main()
