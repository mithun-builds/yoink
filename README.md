# Yoink

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Grab **transcripts, audio, or video** from a YouTube playlist (or a single
video) and save them to a local folder. Uses `yt-dlp` for playlist metadata and
media, and `youtube-transcript-api` for transcript text.

Comes with a local web UI and three command-line entry points — use whichever
suits the job.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`ffmpeg` is optional but recommended — without it, video quality is capped to
whatever single pre-muxed stream YouTube offers, and audio stays in its source
format instead of being converted to mp3.

```bash
brew install ffmpeg
```

## Web UI

```bash
python app.py
```

Opens <http://127.0.0.1:5000> in your browser. Three tabs — Transcripts, Video,
Audio — sharing the same URL, range, delay, and output-folder controls. Shows a
live queue with per-video status, download progress with speed and ETA, a Stop
button, and a file browser for the output folder with inline transcript viewing.

The server binds to `127.0.0.1` only and runs one job at a time. It's a local
tool, not something to expose to a network.

## Command line

Transcripts:
```bash
python fetch_playlist_transcripts.py "<playlist_or_watch_url>"
```

Video (capped at 720p):
```bash
python download_videos.py "<playlist_url>" --quality 720
```

Audio as mp3:
```bash
python download_videos.py "<playlist_url>" --audio-only
```

Both scripts accept the same selection flags:

| Flag | Meaning |
| --- | --- |
| `--limit N` | Only the first N videos |
| `--start N` / `--end N` | 1-based inclusive range, for resuming |
| `--out DIR` | Output directory (default: `downloads`) |
| `--delay S` | Seconds between requests |

Filenames keep their true playlist position (`<position>_<video_id>_<title>`),
so resuming with `--start` won't renumber anything you already have.

Works on standalone video URLs too — no `list=` parameter needed.

## Output

Everything lands in `downloads/` by default:
- `playlist_index.json` — metadata (title, id, url) for every video in the playlist
- One `.txt`, `.mp4`, or `.mp3` per fetched video

## Troubleshooting

**"YouTube is blocking requests from your IP"**
A rate-limit/IP block from YouTube, not a bug. It typically happens after
fetching many transcripts in a short time. The transcript fetcher detects this
precisely and stops after 3 consecutive IP-block errors rather than burning
through the rest of the playlist on doomed requests. When it stops:
- Wait 15–60+ minutes before retrying
- Switch networks (e.g. mobile hotspot) or toggle a VPN to get a new IP
- Increase `--delay` to space out requests further
- Resume from where it stopped using `--start <n>`

**`HTTP Error 403: Forbidden` on video/audio downloads**
Almost always an out-of-date `yt-dlp` — YouTube changes its player often and
`yt-dlp` ships fixes quickly. Update it first:
```bash
pip install -U yt-dlp
```

**Playlist shows "0 videos found"**
Make sure the URL includes a `list=` parameter. Combined
`watch?v=...&list=...` URLs are normalized automatically, but a bare video URL
will only ever return that one video (by design).

## License

[MIT](LICENSE)
