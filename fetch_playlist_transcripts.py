"""
Download transcripts for every video in a YouTube playlist.

Setup (run once):
    pip install yt-dlp youtube-transcript-api

Usage:
    python fetch_playlist_transcripts.py "<playlist_url>" --limit 10

Output:
    Creates a folder ./transcripts/ with one .txt file per video,
    named "<index>_<video_id>_<title>.txt", plus a playlist_index.json
    listing all videos found (title, id, url) so you can see what's
    in the full playlist even if you only fetch a subset.
"""

import argparse
import json
import os
import re
import sys
import time

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)


def sanitize_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip().replace("\n", " ")
    return name[:max_len]


def normalize_playlist_url(url: str) -> str:
    """Extract the playlist ID from any YouTube URL form (watch?v=...&list=...,
    playlist?list=..., etc.) and return a clean playlist-only URL. This avoids
    yt-dlp treating a combined watch+list URL as a single-video request."""
    match = re.search(r"[?&]list=([a-zA-Z0-9_-]+)", url)
    if match:
        return f"https://www.youtube.com/playlist?list={match.group(1)}"
    return url  # fall back to whatever was passed in


def get_playlist_entries(playlist_url: str):
    """Return a list of dicts: {id, title, url} for every video in the playlist,
    without downloading video files (flat extraction)."""
    clean_url = normalize_playlist_url(playlist_url)
    ydl_opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "skip_download": True,
        "noplaylist": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(clean_url, download=False)

    raw_entries = info.get("entries")

    if raw_entries is None:
        # Not a playlist at all -- a single video URL. Treat it as a
        # one-item "playlist" so the rest of the script works unchanged.
        vid = info.get("id")
        title = info.get("title") or vid
        return (
            [
                {
                    "id": vid,
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                }
            ],
            title,
        )

    entries = []
    for e in raw_entries or []:
        if not e:
            continue
        vid = e.get("id")
        title = e.get("title") or vid
        entries.append(
            {
                "id": vid,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={vid}",
            }
        )
    return entries, info.get("title", "playlist")


def fetch_transcript_text(video_id: str, languages=("en", "en-US", "en-GB", "hi")):
    """Fetch transcript, preferring manually created ones, falling back to
    auto-generated, then to any available translated transcript."""
    ytt_api = YouTubeTranscriptApi()
    try:
        transcript_list = ytt_api.list(video_id)
    except (TranscriptsDisabled, VideoUnavailable) as ex:
        return None, f"No transcript available ({ex.__class__.__name__})"

    transcript = None
    try:
        transcript = transcript_list.find_manually_created_transcript(languages)
    except NoTranscriptFound:
        try:
            transcript = transcript_list.find_generated_transcript(languages)
        except NoTranscriptFound:
            # last resort: grab whatever exists and translate to English if possible
            for t in transcript_list:
                try:
                    transcript = t.translate("en") if t.language_code != "en" else t
                    break
                except Exception:
                    continue

    if transcript is None:
        return None, "No transcript found in requested or fallback languages"

    try:
        fetched = transcript.fetch()
    except Exception as ex:
        return None, f"Fetch failed: {ex}"

    text = " ".join(snippet.text for snippet in fetched)
    return text, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("playlist_url", help="Full YouTube playlist URL")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only fetch the first N videos (ignored if --start/--end given)",
    )
    parser.add_argument(
        "--start", type=int, default=None,
        help="1-based playlist position to start from, e.g. 11 to resume after fetching 1-10",
    )
    parser.add_argument(
        "--end", type=int, default=None,
        help="1-based playlist position to stop at (inclusive), e.g. 41 for the last video",
    )
    parser.add_argument(
        "--out", default="transcripts", help="Output directory (default: transcripts)"
    )
    parser.add_argument(
        "--delay", type=float, default=4.0,
        help="Seconds to wait between transcript fetches, to avoid YouTube IP rate-limiting (default: 4)",
    )
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    print(f"Reading playlist: {args.playlist_url}")
    entries, playlist_title = get_playlist_entries(args.playlist_url)
    print(f"Playlist: {playlist_title}  |  {len(entries)} videos found")

    with open(os.path.join(args.out, "playlist_index.json"), "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)

    if args.start or args.end:
        start_idx = (args.start or 1) - 1  # convert to 0-based
        end_idx = args.end if args.end else len(entries)  # inclusive, still 1-based here
        # zip each entry with its TRUE playlist position (1-based) before slicing
        numbered = list(enumerate(entries, start=1))
        targets = numbered[start_idx:end_idx]
    else:
        sliced = entries[: args.limit] if args.limit else entries
        targets = list(enumerate(sliced, start=1))

    for n, (playlist_pos, e) in enumerate(targets):
        vid, title, url = e["id"], e["title"], e["url"]
        print(f"[{playlist_pos}/{len(entries)}] {title} ({vid}) ... ", end="", flush=True)

        text, err = fetch_transcript_text(vid)

        # One retry with a longer backoff if it looks like a rate-limit block
        if text is None and err and ("block" in err.lower() or "429" in err):
            print("rate-limited, waiting 20s and retrying once... ", end="", flush=True)
            time.sleep(20)
            text, err = fetch_transcript_text(vid)

        fname = f"{playlist_pos:02d}_{vid}_{sanitize_filename(title)}.txt"
        fpath = os.path.join(args.out, fname)

        with open(fpath, "w", encoding="utf-8") as f:
            f.write(f"Title: {title}\nURL: {url}\n\n")
            if text:
                f.write(text)
            else:
                f.write(f"[TRANSCRIPT UNAVAILABLE: {err}]")

        print("done" if text else f"SKIPPED ({err})")

        # pause between requests (skip after the very last one)
        if n < len(targets) - 1:
            time.sleep(args.delay)

    print(f"\nAll files written to ./{args.out}/")
    print("Next: paste the .txt contents (or the whole folder) back to Claude for synthesis.")


if __name__ == "__main__":
    main()
