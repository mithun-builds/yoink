"""
Download video or audio files for a YouTube playlist, using yt-dlp.

Companion to fetch_playlist_transcripts.py: it reuses that module's playlist
parsing and slicing, and emits the same event protocol, so the web UI can
drive transcripts and media downloads through one code path.

Usage:
    python download_videos.py "<playlist_url>" --limit 5 --quality 720
    python download_videos.py "<playlist_url>" --audio-only
"""

import argparse
import os
import shutil

import yt_dlp

from config import default_out
from fetch_playlist_transcripts import (
    get_playlist_entries,
    sanitize_filename,
    select_targets,
    _interruptible_sleep,
)

# Video files are far larger than transcripts, so the default pacing is
# gentler than the transcript fetcher's -- but yt-dlp downloads are ordinary
# media requests and are not rate-limited the way the transcript API is.
DEFAULT_DELAY = 1.0


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def build_format(quality=None, audio_only=False):
    """Return (format_string, postprocessors, merge_container)."""
    if audio_only:
        pps = []
        if ffmpeg_available():
            pps = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        # Fall back to the SMALLEST muxed stream, not the best one. HLS course
        # sources publish only muxed A/V variants, so "bestaudio" matches
        # nothing and a "best" fallback would pull the 1080p stream just to
        # throw its video away -- hundreds of MB for a few MB of mp3.
        return "bestaudio/worst", pps, None

    if quality:
        fmt = (
            f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={quality}]+bestaudio/"
            f"best[height<={quality}]/best"
        )
    else:
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    # merging separate video+audio streams needs ffmpeg; without it, fall
    # back to a single pre-muxed file rather than failing at the merge step
    if not ffmpeg_available():
        fmt = f"best[height<={quality}]/best" if quality else "best"
        return fmt, [], None
    return fmt, [], "mp4"


def run_download(
    playlist_url,
    out_dir="downloads",
    limit=None,
    start=None,
    end=None,
    quality=None,
    audio_only=False,
    delay=DEFAULT_DELAY,
    on_event=None,
    should_stop=None,
):
    """Download media for each video in the playlist.

    Emits the same event types as fetch_playlist_transcripts.run_fetch, plus:
        {"type": "progress", "pos", "pct", "speed", "eta"}

    Returns the number of files successfully downloaded.
    """
    emit = on_event or (lambda ev: None)
    stop = should_stop or (lambda: False)

    os.makedirs(out_dir, exist_ok=True)

    emit({"type": "reading", "url": playlist_url})
    entries, playlist_title = get_playlist_entries(playlist_url)
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

    if not ffmpeg_available():
        emit({
            "type": "warning",
            "message": (
                "ffmpeg not found -- falling back to single-stream downloads. "
                "Quality may be capped and audio-only will stay in its source "
                "format instead of mp3. Install with: brew install ffmpeg"
            ),
        })

    fmt, postprocessors, merge_to = build_format(quality, audio_only)
    downloaded = 0

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

        # A yt-dlp progress hook that also gives us a cancellation point --
        # raising from the hook aborts the in-flight download, which is the
        # only way to interrupt a large file mid-transfer.
        def hook(d, pos=playlist_pos):
            if stop():
                raise yt_dlp.utils.DownloadCancelled("stopped by user")
            if d["status"] != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            pct = (d.get("downloaded_bytes", 0) / total * 100) if total else None
            emit({
                "type": "progress",
                "pos": pos,
                "pct": round(pct, 1) if pct is not None else None,
                "speed": d.get("_speed_str", "").strip() or None,
                "eta": d.get("_eta_str", "").strip() or None,
            })

        stem = f"{playlist_pos:02d}_{vid}_{sanitize_filename(title)}"
        ydl_opts = {
            "format": fmt,
            "outtmpl": os.path.join(out_dir, stem + ".%(ext)s"),
            "progress_hooks": [hook],
            "postprocessors": postprocessors,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "ignoreerrors": False,
            "retries": 3,
        }
        if merge_to:
            ydl_opts["merge_output_format"] = merge_to

        # For scraped course lessons the page URL is not resolvable by yt-dlp;
        # hand it the HLS manifest we already found instead.
        target = e.get("media") or url

        err = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([target])
            downloaded += 1
        except yt_dlp.utils.DownloadCancelled:
            emit({"type": "stopped"})
            break
        except Exception as ex:
            err = f"{ex.__class__.__name__}: {ex}"

        emit({
            "type": "video_done",
            "pos": playlist_pos,
            "ok": err is None,
            "error": err,
            "path": None,
        })

        if n < len(targets) - 1 and delay:
            if _interruptible_sleep(delay, stop):
                emit({"type": "stopped"})
                break

    emit({"type": "finished", "out_dir": out_dir, "written": downloaded})
    return downloaded


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("playlist_url", help="Full YouTube playlist or video URL")
    parser.add_argument("--limit", type=int, default=None, help="Only fetch the first N videos")
    parser.add_argument("--start", type=int, default=None, help="1-based position to start from")
    parser.add_argument("--end", type=int, default=None, help="1-based position to stop at (inclusive)")
    parser.add_argument(
        "--out", default=default_out(),
        help="Output directory (default: the folder configured in yoink.config.json)",
    )
    parser.add_argument(
        "--quality", type=int, default=None,
        help="Cap video height, e.g. 720 or 1080 (default: best available)",
    )
    parser.add_argument("--audio-only", action="store_true", help="Download audio as mp3")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Seconds between downloads")
    args = parser.parse_args()

    def report(ev):
        t = ev["type"]
        if t == "reading":
            print(f"Reading playlist: {ev['url']}")
        elif t == "playlist":
            print(f"Playlist: {ev['title']}  |  {ev['total']} videos found")
        elif t == "warning":
            print(f"WARNING: {ev['message']}")
        elif t == "video_start":
            print(f"[{ev['pos']}/{ev['total']}] {ev['title']} ... ", end="", flush=True)
        elif t == "video_done":
            print("done" if ev["ok"] else f"FAILED ({ev['error']})")
        elif t == "finished":
            out = ev["out_dir"]
            shown = out if os.path.isabs(out) else f"./{out}"
            print(f"\n{ev['written']} file(s) downloaded to {shown.rstrip('/')}/")

    run_download(
        args.playlist_url,
        out_dir=args.out,
        limit=args.limit,
        start=args.start,
        end=args.end,
        quality=args.quality,
        audio_only=args.audio_only,
        delay=args.delay,
        on_event=report,
    )


if __name__ == "__main__":
    main()
