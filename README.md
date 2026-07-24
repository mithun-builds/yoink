# YouTube (Incl. Playlist) Transcript Fetcher

Download transcripts for every video in a YouTube playlist (or a single video)
using `yt-dlp` for playlist/video metadata and `youtube-transcript-api` for
transcript text.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Usage

Fetch all videos in a playlist:
```bash
python fetch_playlist_transcripts.py "<playlist_or_watch_url>"
```

Fetch only the first N videos:
```bash
python fetch_playlist_transcripts.py "<playlist_url>" --limit 10
```

Fetch a specific range (1-based, inclusive) — useful for resuming without
re-fetching videos you already have:
```bash
python fetch_playlist_transcripts.py "<playlist_url>" --start 11 --end 41
```

Adjust the delay between requests (default 4 seconds) to reduce the chance of
being rate-limited by YouTube:
```bash
python fetch_playlist_transcripts.py "<playlist_url>" --delay 8
```

Works on standalone video URLs too (no `list=` parameter needed) — the script
detects there's no playlist and treats it as a single video.

## Output

Creates a `transcripts/` folder containing:
- `playlist_index.json` — metadata (title, id, url) for every video found in the playlist
- One `.txt` file per fetched video, named `<position>_<video_id>_<title>.txt`

## Troubleshooting

**"YouTube is blocking requests from your IP"**
This is a rate-limit/IP block from YouTube, not a bug in the script. It
typically happens after fetching many transcripts in a short time. Options:
- Wait 15–60+ minutes before retrying
- Switch networks (e.g. mobile hotspot) or toggle a VPN to get a new IP
- Increase `--delay` to space out requests further
- As a last resort, fetch transcripts manually via YouTube's UI
  ("..." menu under a video → Show transcript)

**Playlist shows "0 videos found"**
Make sure you're passing a URL that includes a `list=` parameter. The script
normalizes combined `watch?v=...&list=...` URLs automatically, but a bare
video URL with no playlist reference will only ever return that one video (by
design — see standalone-video support above).
