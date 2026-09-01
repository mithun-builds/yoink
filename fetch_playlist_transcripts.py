"""
Download transcripts for every video in a YouTube playlist.

Setup (run once):
    pip install yt-dlp youtube-transcript-api

Usage:
    python fetch_playlist_transcripts.py "<playlist_url>" --limit 10

Output:
    Writes one .txt file per video into the configured download folder
    (./downloads/ unless changed in yoink.config.json or via --out), named
    "<index>_<video_id>_<title>.txt", plus a playlist_index.json listing all
    videos found (title, id, url) so you can see what's in the full playlist
    even if you only fetch a subset.
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
    RequestBlocked,
    IpBlocked,
)

from config import default_out


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
            [{"id": vid, "title": title, "url": _entry_url(info, vid, playlist_url)}],
            title,
        )

    entries = []
    for e in raw_entries or []:
        if not e:
            continue
        vid = e.get("id")
        title = e.get("title") or vid
        entries.append({"id": vid, "title": title, "url": _entry_url(e, vid)})
    return entries, info.get("title", "playlist")


def _entry_url(entry: dict, vid: str, fallback: str = None) -> str:
    """Best real URL for an entry.

    yt-dlp reports the actual page URL for each entry, which matters for
    non-YouTube sources -- synthesizing a youtube.com/watch?v= link from the
    id produces a dead link when the id is a page fragment rather than a
    YouTube video id. Only fall back to the YouTube form when nothing else
    is available, since that is still the common case for bare video ids.
    """
    for key in ("webpage_url", "url", "original_url"):
        val = entry.get(key)
        if val and str(val).startswith("http"):
            return val
    if fallback:
        return fallback
    return f"https://www.youtube.com/watch?v={vid}"


def fetch_transcript_text(video_id: str, languages=("en", "en-US", "en-GB", "hi")):
    """Fetch transcript, preferring manually created ones, falling back to
    auto-generated, then to any available translated transcript.

    Returns (text, error_message, was_rate_limited). was_rate_limited is True
    only when YouTube's own IP-block/request-block exceptions are raised, so
    callers can decide to back off/retry precisely (rather than guessing from
    error text).
    """
    ytt_api = YouTubeTranscriptApi()

    try:
        transcript_list = ytt_api.list(video_id)
    except (RequestBlocked, IpBlocked) as ex:
        return None, f"Rate-limited/IP-blocked ({ex.__class__.__name__})", True
    except (TranscriptsDisabled, VideoUnavailable) as ex:
        return None, f"No transcript available ({ex.__class__.__name__})", False

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
        return None, "No transcript found in requested or fallback languages", False

    try:
        fetched = transcript.fetch()
    except (RequestBlocked, IpBlocked) as ex:
        return None, f"Rate-limited/IP-blocked ({ex.__class__.__name__})", True
    except Exception as ex:
        return None, f"Fetch failed: {ex}", False

    text = " ".join(snippet.text for snippet in fetched)
    return text, None, False


def select_targets(entries, limit=None, start=None, end=None):
    """Return [(playlist_position, entry), ...] for the requested slice.

    Positions are the TRUE 1-based playlist positions, preserved across
    slicing so filenames stay stable when resuming with --start/--end.
    """
    if start or end:
        start_idx = (start or 1) - 1  # convert to 0-based
        end_idx = end if end else len(entries)  # inclusive, still 1-based here
        numbered = list(enumerate(entries, start=1))
        return numbered[start_idx:end_idx]
    sliced = entries[:limit] if limit else entries
    return list(enumerate(sliced, start=1))


def run_fetch(
    playlist_url,
    out_dir="downloads",
    limit=None,
    start=None,
    end=None,
    delay=4.0,
    on_event=None,
    should_stop=None,
):
    """Fetch transcripts for a playlist, reporting progress as it goes.

    This holds the actual work loop so the CLI and the web UI share one
    implementation of the retry/backoff behaviour rather than duplicating it.

    on_event(event_dict) is called as work proceeds. Event types:
        {"type": "reading",     "url"}
        {"type": "playlist",    "title", "total", "targets", "entries"}
        {"type": "video_start", "pos", "total", "title", "video_id", "url"}
        {"type": "rate_limited","pos", "wait"}
        {"type": "video_done",  "pos", "ok", "error", "path"}
        {"type": "blocked_abort"}
        {"type": "stopped"}
        {"type": "finished",    "out_dir", "written"}

    should_stop() is polled between videos (and during the inter-request
    delay); return True to abort early. A fetch already in flight is allowed
    to finish -- transcript requests aren't cancellable mid-call.
    """
    emit = on_event or (lambda ev: None)
    stop = should_stop or (lambda: False)

    os.makedirs(out_dir, exist_ok=True)

    emit({"type": "reading", "url": playlist_url})
    entries, playlist_title = get_playlist_entries(playlist_url)

    with open(os.path.join(out_dir, "playlist_index.json"), "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)

    targets = select_targets(entries, limit=limit, start=start, end=end)
    emit({
        "type": "playlist",
        "title": playlist_title,
        "total": len(entries),
        "targets": len(targets),
        "entries": [
            {"pos": pos, "title": e["title"], "video_id": e["id"], "url": e["url"]}
            for pos, e in targets
        ],
    })

    consecutive_blocks = 0
    written = 0

    for n, (playlist_pos, e) in enumerate(targets):
        if stop():
            emit({"type": "stopped"})
            break

        vid, title, url = e["id"], e["title"], e["url"]
        emit({
            "type": "video_start",
            "pos": playlist_pos,
            "total": len(entries),
            "title": title,
            "video_id": vid,
            "url": url,
        })

        text, err, was_blocked = fetch_transcript_text(vid)

        # One retry with a longer backoff, but only for a genuine IP-block
        if was_blocked:
            emit({"type": "rate_limited", "pos": playlist_pos, "wait": 20})
            if _interruptible_sleep(20, stop):
                emit({"type": "stopped"})
                break
            text, err, was_blocked = fetch_transcript_text(vid)

        consecutive_blocks = consecutive_blocks + 1 if was_blocked else 0

        fname = f"{playlist_pos:02d}_{vid}_{sanitize_filename(title)}.txt"
        fpath = os.path.join(out_dir, fname)

        with open(fpath, "w", encoding="utf-8") as f:
            f.write(f"Title: {title}\nURL: {url}\n\n")
            f.write(text if text else f"[TRANSCRIPT UNAVAILABLE: {err}]")

        if text:
            written += 1
        emit({
            "type": "video_done",
            "pos": playlist_pos,
            "ok": bool(text),
            "error": err,
            "path": fpath,
        })

        # Stop early if YouTube is clearly blocking us outright -- further
        # requests will just fail the same way and waste time.
        if consecutive_blocks >= 3:
            emit({"type": "blocked_abort"})
            break

        # pause between requests (skip after the very last one)
        if n < len(targets) - 1:
            if _interruptible_sleep(delay, stop):
                emit({"type": "stopped"})
                break

    emit({"type": "finished", "out_dir": out_dir, "written": written})
    return written


def _interruptible_sleep(seconds, stop):
    """Sleep in short slices so a stop request is noticed promptly.
    Returns True if the sleep was cut short by stop()."""
    waited = 0.0
    while waited < seconds:
        if stop():
            return True
        chunk = min(0.25, seconds - waited)
        time.sleep(chunk)
        waited += chunk
    return False


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Fetch every video in a playlist
  python fetch_playlist_transcripts.py "https://www.youtube.com/playlist?list=PL..."

  # Fetch just the first 10 videos
  python fetch_playlist_transcripts.py "https://www.youtube.com/playlist?list=PL..." --limit 10

  # Resume from video 11 through 41 (skip ones you already fetched)
  python fetch_playlist_transcripts.py "https://www.youtube.com/playlist?list=PL..." --start 11 --end 41

  # Space out requests more to avoid YouTube's IP rate-limiting
  python fetch_playlist_transcripts.py "https://www.youtube.com/playlist?list=PL..." --delay 10

  # Works on a single video URL too, no playlist needed
  python fetch_playlist_transcripts.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
""",
    )
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
        "--out", default=default_out(),
        help="Output directory (default: the folder configured in yoink.config.json)",
    )
    parser.add_argument(
        "--delay", type=float, default=4.0,
        help="Seconds to wait between transcript fetches, to avoid YouTube IP rate-limiting (default: 4)",
    )
    args = parser.parse_args()

    def report(ev):
        t = ev["type"]
        if t == "reading":
            print(f"Reading playlist: {ev['url']}")
        elif t == "playlist":
            print(f"Playlist: {ev['title']}  |  {ev['total']} videos found")
        elif t == "video_start":
            print(
                f"[{ev['pos']}/{ev['total']}] {ev['title']} ({ev['video_id']}) ... ",
                end="", flush=True,
            )
        elif t == "rate_limited":
            print(
                f"rate-limited, waiting {ev['wait']}s and retrying once... ",
                end="", flush=True,
            )
        elif t == "video_done":
            print("done" if ev["ok"] else f"SKIPPED ({ev['error']})")
        elif t == "blocked_abort":
            print(
                "\nStopping: 3 consecutive IP-block errors. "
                "Wait before retrying, or switch networks/VPN, then resume with --start."
            )
        elif t == "finished":
            out = ev["out_dir"]
            shown = out if os.path.isabs(out) else f"./{out}"
            print(f"\nAll files written to {shown.rstrip('/')}/")
            print(
                "Next: paste the .txt contents (or the whole folder) "
                "back to Claude for synthesis."
            )

    run_fetch(
        args.playlist_url,
        out_dir=args.out,
        limit=args.limit,
        start=args.start,
        end=args.end,
        delay=args.delay,
        on_event=report,
    )


if __name__ == "__main__":
    main()
