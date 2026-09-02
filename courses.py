"""
Support for DeepLearning.AI's free short courses on learn.deeplearning.ai.

Those courses are public -- no login -- but they are a React app, so yt-dlp's
generic extractor cannot resolve a lesson page on its own. Each lesson page
does embed its own HLS manifest and subtitle tracks, though, so this module
scrapes them out and hands concrete media URLs to the normal download path.

Entries come back in the same shape get_playlist_entries() returns, plus two
extra keys, so the transcript and media downloaders need only a small branch:

    media      HLS manifest for the lesson video
    subtitle   English WebVTT track, used as the transcript source

Also holds the curated quick-pick list of DeepLearning.AI full courses that
are published free as YouTube playlists.
"""

import re
import urllib.error
import urllib.request

HOST = "learn.deeplearning.ai"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
TIMEOUT = 25

#  Full courses DeepLearning.AI publishes free on YouTube. Offered in the UI
#  as one-click picks so you don't have to go hunting for playlist URLs.
QUICK_PICKS = [
    ("Deep Learning 1 · Neural Networks", "PLkDaE6sCZn6Ec-XTbcX1uRg2_u4xOEky0"),
    ("Deep Learning 2 · Tuning & Regularization", "PLkDaE6sCZn6Hn0vK8co82zjQtt3T2Nkqc"),
    ("Deep Learning 3 · Structuring ML Projects", "PLkDaE6sCZn6E7jZ9sN_xHwSHOdjUxUW_b"),
    ("Deep Learning 4 · Convolutional Networks", "PLkDaE6sCZn6Gl29AoE31iwdVwSG-KnDzF"),
    ("Deep Learning 5 · Sequence Models", "PLkDaE6sCZn6F6wUI9tvS_Gw1vaFAx6rd6"),
    ("Machine Learning Specialization", "PLkDaE6sCZn6FNC6YRfRQc_FbeQrF8BwGI"),
    ("MLOps · ML in Production", "PLkDaE6sCZn6GMoA0wbpJLi3t34Gd8l0aK"),
    ("AI for Good Specialization", "PLkDaE6sCZn6HJ1XrZLpKeWQN5XMKhEz_V"),
]


def quick_picks():
    return [
        {"title": t, "url": f"https://www.youtube.com/playlist?list={pid}"}
        for t, pid in QUICK_PICKS
    ]


def is_course_url(url: str) -> bool:
    return HOST in (url or "")


def course_slug(url: str):
    """Course slug from any learn.deeplearning.ai URL, lesson pages included."""
    m = re.search(rf"{re.escape(HOST)}/(?:courses/)?([a-zA-Z0-9\-_]+)", url or "")
    if not m:
        return None
    slug = m.group(1)
    return None if slug in ("courses", "lesson") else slug


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8", "replace")


def _pretty(slug_tail: str) -> str:
    return slug_tail.replace("-", " ").replace("_", " ").strip().title()


def _lesson_index(media_url: str):
    """Lesson number, taken from the media filename (…_02_master.m3u8). The
    slug order on the page is not the teaching order, but the media path is."""
    for pat in (r"_(\d{2,3})_master\.m3u8", r"_(\d{2,3})[-_]master", r"_(\d{2,3})/"):
        m = re.search(pat, media_url)
        if m:
            return int(m.group(1))
    return None


def course_entries(url: str):
    """Discover every lesson of a free DeepLearning.AI short course.

    Returns (entries, course_title). Lessons with no video (quizzes, notebook-
    only steps) are skipped rather than written as empty files.
    """
    slug = course_slug(url)
    if not slug:
        raise ValueError(f"Not a recognisable {HOST} course URL: {url}")

    root = f"https://{HOST}/courses/{slug}"
    html = _get(root)

    course_title = _pretty(slug)
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if m:
        course_title = re.sub(r"\s*[-|]\s*DeepLearning\.AI.*$", "", m.group(1).strip()) \
                       or course_title

    lesson_slugs = sorted(set(re.findall(
        rf"/courses/{re.escape(slug)}/lesson/([a-zA-Z0-9\-_]+/[a-zA-Z0-9\-_]+)", html
    )))
    if not lesson_slugs:
        raise ValueError(f"No lessons found on {root} -- page layout may have changed.")

    found = []
    for ls in lesson_slugs:
        page_url = f"{root}/lesson/{ls}"
        try:
            page = _get(page_url)
        except (urllib.error.URLError, OSError):
            continue

        media = re.findall(r'https?://[^"\'\\ ]+?_master\.m3u8[^"\'\\ ]*', page)
        if not media:
            continue  # quiz / notebook lesson, nothing to download
        subs = re.findall(r'https?://[^"\'\\ ]+?/subtitle/en/[^"\'\\ ]+?\.vtt[^"\'\\ ]*', page)

        found.append({
            "id": ls.split("/")[-1],
            "title": _pretty(ls.split("/")[-1]),
            "url": page_url,
            "media": media[0],
            "subtitle": subs[0] if subs else None,
            "_idx": _lesson_index(media[0]),
        })

    found.sort(key=lambda e: (e["_idx"] is None, e["_idx"]))
    for e in found:
        e.pop("_idx", None)
    return found, course_title


def subtitle_text(vtt_url: str):
    """Fetch a WebVTT track and flatten it to plain prose.

    Returns (text, error). Mirrors fetch_transcript_text's contract minus the
    rate-limit flag, since this is a plain CDN file and is not rate-limited.
    """
    if not vtt_url:
        return None, "No English subtitle track for this lesson"
    try:
        raw = _get(vtt_url)
    except Exception as ex:
        return None, f"Subtitle fetch failed: {ex.__class__.__name__}"
    return vtt_to_text(raw), None


def vtt_to_text(raw: str) -> str:
    """Strip WebVTT cue numbers, timestamps and markup; collapse to one flow.
    Consecutive duplicate lines are dropped -- rolling captions repeat them."""
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if (not line
                or line.startswith(("WEBVTT", "X-TIMESTAMP-MAP", "NOTE", "STYLE"))
                or "-->" in line
                or line.isdigit()):
            continue
        line = re.sub(r"<[^>]+>", "", line)          # <c>, <v Speaker>, <i> …
        line = re.sub(r"\s+", " ", line).strip()
        if line and (not out or out[-1] != line):
            out.append(line)
    return " ".join(out)
