# SlopEngine

SlopEngine generates vertical short-form videos from a topic.

The pipeline asks Gemini for a rewritten YouTube title, spoken narration, Pexels search keywords, and a visual search query. It then uses Edge TTS for narration, Whisper for word-level captions, Pexels for portrait background video, and MoviePy to render a 9:16 MP4. It can also upload the finished video to YouTube Shorts.

## Outputs

Generated files go under `outputs/` by default:

- `<timestamp>-<topic>.mp4`: rendered vertical video
- `<timestamp>-<topic>.metadata.json`: topic, generated title, script payload, word events, background files, output path, and YouTube upload metadata when present

Temporary media is created under `/tmp/shorts_core` by default and is cleaned after each run unless you pass `--keep-temp`.

## Requirements

- Python 3.11+ recommended
- FFmpeg installed and available on `PATH`
- A Gemini API key
- A Pexels API key
- Optional: YouTube OAuth client secrets for upload

Install Python packages:

```bash
python3 -m pip install -r requirements.txt
```

For Whisper CUDA, avoid Python 3.14. Use a Python 3.12 environment and keep
`WHISPER_DEVICE=cuda` in `.env`:

```bash
python3.12 -m venv .venv312
.venv312/bin/python -m pip install --upgrade pip
.venv312/bin/python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130
.venv312/bin/python -m pip install -r requirements.txt
.venv312/bin/python shorts_core.py --upload
```

If your GPU driver needs a different CUDA wheel, choose the matching pip command
from the PyTorch install selector.

If Whisper or MoviePy cannot read media, check FFmpeg first:

```bash
ffmpeg -version
```

## Configuration

Copy the example env file and fill in your own keys:

```bash
cp .env.example .env
```

Do not commit real credentials. `.env`, `client_secrets.json`, and `.youtube_token.json` are ignored by git.

Required keys:

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Google AI Studio key for script, title, keywords, and Pexels query generation |
| `PEXELS_API_KEY` | Pexels API key for background videos |

Common optional keys:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model for content generation |
| `GEMINI_TIMEOUT_SECONDS` | `60` | Gemini content generation timeout |
| `EDGE_TTS_VOICE` | `en-US-AriaNeural` | Edge TTS voice |
| `EDGE_TTS_RATE` | `+7%` | Speech rate |
| `EDGE_TTS_PITCH` | `-3Hz` | Speech pitch |
| `EDGE_TTS_VOLUME` | `+0%` | Speech volume |
| `WHISPER_MODEL` | `base` | Whisper model name |
| `WHISPER_DEVICE` | `cpu` | Whisper device. Keep `cpu` on Python 3.14 to avoid CUDA/Triton word-timestamp failures |
| `SHORTS_OUTPUT_DIR` | `outputs` | Rendered video and metadata directory |
| `SHORTS_TEMP_ROOT` | `/tmp/shorts_core` | Temporary media directory |
| `CAPTION_FONT_PATH` | auto-detected | Path to a scalable `.ttf` or `.otf` font |
| `CAPTION_FONT_SIZE` | `220` | Caption font size |
| `BACKGROUND_MUSIC_PATH` | blank | Optional local music file |
| `BACKGROUND_MUSIC_VOLUME` | `0.12` | Music volume multiplier |

YouTube upload keys:

| Variable | Default | Purpose |
| --- | --- | --- |
| `YOUTUBE_UPLOAD_ENABLED` | `false` | Upload after rendering when true |
| `YOUTUBE_CLIENT_SECRETS_FILE` | `client_secrets.json` | OAuth client secrets file |
| `YOUTUBE_TOKEN_FILE` | `.youtube_token.json` | Stored OAuth token |
| `YOUTUBE_PRIVACY_STATUS` | `private` | `private`, `unlisted`, or `public` |
| `YOUTUBE_CATEGORY_ID` | `22` | YouTube category ID |

## Usage

Generate a video:

```bash
python3 shorts_core.py --topic "Why your 8-hour workday is failing"
```

Choose an output file:

```bash
python3 shorts_core.py --topic "The hidden cost of context switching" --output outputs/context-switching.mp4
```

Keep temporary media for debugging:

```bash
python3 shorts_core.py --topic "How cities became louder" --keep-temp
```

Use background music for one run:

```bash
python3 shorts_core.py --topic "Why old malls feel strange" --music ost.mp3 --music-volume 0.12
```

Upload after rendering:

```bash
python3 shorts_core.py --topic "Why your 8-hour workday is failing" --upload
```

If you omit `--topic`, the script prompts for a topic and optional output file.

## Generated Titles

The YouTube title is not the raw topic. Gemini rewrites the topic into a shorter, more engaging title and stores it in `script_payload.title` inside the metadata file. Upload uses that generated title when metadata is present.

Example:

```text
Topic: The '8-hour workday' is actually a scam designed for the 1800s, and it is destroying your productivity.
Generated title: Why Your 8-Hour Workday Is Failing
```

If a generated title is missing, SlopEngine falls back to the normalized topic and caps it at YouTube's 100-character limit without adding ellipses.

## YouTube Upload

To upload, create an OAuth desktop client in Google Cloud and save the downloaded secrets JSON as `client_secrets.json`, or point `YOUTUBE_CLIENT_SECRETS_FILE` at the file.

On the first upload, the script opens a local OAuth flow and writes `.youtube_token.json`. Later uploads reuse that token when possible.

Upload metadata is merged into the rendered video's `.metadata.json` file:

```json
{
  "youtube": {
    "video_id": "example-id",
    "url": "https://www.youtube.com/watch?v=example-id"
  }
}
```

## Project Layout

| Path | Purpose |
| --- | --- |
| `shorts_core.py` | Main pipeline, CLI, rendering, and upload code |
| `requirements.txt` | Python dependencies |
| `.env.example` | Safe config template |
| `tests/` | Unit tests |
| `outputs/` | Rendered videos and metadata |

## Verification

Run the project checks after Python changes:

```bash
python3 -m unittest discover
python3 -m py_compile shorts_core.py
```

For caption or visual changes, render a short sample and inspect at least one frame or preview video.

## Troubleshooting

- `GEMINI_API_KEY is required` or `PEXELS_API_KEY is required`: set the missing key in `.env`.
- `Gemini generate_content exceeded ... seconds`: increase `GEMINI_TIMEOUT_SECONDS` in `.env` or retry later if Gemini is slow.
- Gemini high-demand warning: the script retries with another available generateContent model when possible.
- YouTube dependency error: install the packages in `requirements.txt`; upload needs `google-api-python-client`, `google-auth-oauthlib`, and `google-auth-httplib2`.
- Whisper CUDA or Triton timestamp errors on Python 3.14: use the Python 3.12 CUDA setup above, or set `WHISPER_DEVICE=cpu`.
- Captions render with the wrong font: set `CAPTION_FONT_PATH` to a real scalable font file.
