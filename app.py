"""
Yoink -- local web UI for grabbing YouTube transcripts, audio, and video.

Run:
    python app.py

Then open http://127.0.0.1:5000 in a browser.

This is a single-user tool: one fetch job runs at a time and its state lives
in memory, so the browser polls /api/status for progress.

Environment:
    HOST          interface to bind (default 127.0.0.1, localhost only)
    PORT          port to serve on (default 5000)
    YOINK_TOKEN   if set, every request must present this token -- required
                  before exposing the app beyond localhost, since anyone who
                  can reach it can start downloads onto this machine and read
                  files from the output folder
"""

import hmac
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

from config import default_out, list_subfolders, resolve_out, save_config
from download_videos import ffmpeg_available, run_download
from fetch_playlist_transcripts import run_fetch

BASE_DIR = Path(__file__).resolve().parent
UNAVAILABLE_MARKER = "[TRANSCRIPT UNAVAILABLE"
TOKEN = os.environ.get("YOINK_TOKEN") or None

app = Flask(__name__)


# ------------------------------------------------------------------- access

@app.before_request
def _require_token():
    """Gate every request when YOINK_TOKEN is set. Unset (the local default)
    leaves the app open, which is fine on 127.0.0.1 and not fine anywhere else."""
    if not TOKEN:
        return None
    supplied = (
        request.args.get("token")
        or request.headers.get("X-Yoink-Token")
        or request.cookies.get("yoink_token")
    )
    if supplied and hmac.compare_digest(supplied, TOKEN):
        return None
    return jsonify({"error": "Unauthorized. Append ?token=... to the URL."}), 401


@app.after_request
def _persist_token(resp):
    """Once a valid token arrives as ?token=..., remember it in a cookie so
    the polling requests and download links work without carrying it along."""
    if TOKEN and request.args.get("token") and resp.status_code < 400:
        resp.set_cookie(
            "yoink_token", TOKEN, httponly=True, samesite="Lax", max_age=86400
        )
    return resp

# ---------------------------------------------------------------- job state

_lock = threading.Lock()
_job = None  # dict, or None if nothing has been run yet
_stop_flag = threading.Event()


def _blank_job():
    return {
        "running": False,
        "phase": "idle",  # idle | reading | fetching | done | error | stopped
        "mode": "transcripts",  # transcripts | video | audio
        "warning": None,
        "pct": None,
        "speed": None,
        "eta": None,
        "playlist_title": None,
        "total": 0,
        "targets": 0,
        "completed": 0,
        "written": 0,
        "current": None,
        "items": [],
        "log": [],
        "error": None,
        "out_dir": default_out(),
        "started_at": None,
        "finished_at": None,
    }


def _log(job, message):
    job["log"].append({"t": time.time(), "message": message})
    del job["log"][:-400]  # keep the tail bounded


def _resolve_out(out=None):
    """Resolve a download folder, falling back to the configured default.
    Relative paths resolve against the app directory, not the shell's cwd."""
    return resolve_out(out)


def _handle_event(job, ev):
    t = ev["type"]
    with _lock:
        if t == "reading":
            job["phase"] = "reading"
            _log(job, f"Reading playlist: {ev['url']}")

        elif t == "playlist":
            job["phase"] = "fetching"
            job["playlist_title"] = ev["title"]
            job["total"] = ev["total"]
            job["targets"] = ev["targets"]
            job["items"] = [
                {
                    "pos": e["pos"],
                    "title": e["title"],
                    "video_id": e["video_id"],
                    "url": e["url"],
                    "status": "pending",
                    "error": None,
                }
                for e in ev["entries"]
            ]
            _log(job, f"{ev['title']} — {ev['total']} videos found, fetching {ev['targets']}")

        elif t == "warning":
            job["warning"] = ev["message"]
            _log(job, f"Warning: {ev['message']}")

        elif t == "video_start":
            job["current"] = ev["pos"]
            job["pct"] = job["speed"] = job["eta"] = None
            for it in job["items"]:
                if it["pos"] == ev["pos"]:
                    it["status"] = "fetching"
            _log(job, f"[{ev['pos']}/{ev['total']}] {ev['title']}")

        elif t == "progress":
            job["pct"] = ev["pct"]
            job["speed"] = ev["speed"]
            job["eta"] = ev["eta"]

        elif t == "rate_limited":
            for it in job["items"]:
                if it["pos"] == ev["pos"]:
                    it["status"] = "retrying"
            _log(job, f"Rate-limited — waiting {ev['wait']}s and retrying once")

        elif t == "video_done":
            job["completed"] += 1
            job["pct"] = job["speed"] = job["eta"] = None
            if ev["ok"]:
                job["written"] += 1
            for it in job["items"]:
                if it["pos"] == ev["pos"]:
                    it["status"] = "done" if ev["ok"] else "skipped"
                    it["error"] = ev["error"]
            if not ev["ok"]:
                _log(job, f"  skipped: {ev['error']}")

        elif t == "blocked_abort":
            job["phase"] = "error"
            job["error"] = (
                "Stopped after 3 consecutive IP-block errors. Wait 15-60 minutes, "
                "switch networks or VPN, then resume using the Start-at field."
            )
            _log(job, job["error"])

        elif t == "stopped":
            job["phase"] = "stopped"
            _log(job, "Stopped by user.")

        elif t == "finished":
            if job["phase"] not in ("error", "stopped"):
                job["phase"] = "done"
            job["current"] = None
            _log(job, f"Finished — {ev['written']} transcript(s) written to {ev['out_dir']}")


def _worker(job, params):
    try:
        common = dict(
            out_dir=params["out_dir"],
            limit=params["limit"],
            start=params["start"],
            end=params["end"],
            delay=params["delay"],
            on_event=lambda ev: _handle_event(job, ev),
            should_stop=_stop_flag.is_set,
        )
        if params["mode"] == "transcripts":
            run_fetch(params["url"], **common)
        else:
            run_download(
                params["url"],
                quality=params["quality"],
                audio_only=params["mode"] == "audio",
                **common,
            )
    except Exception as ex:  # surface yt-dlp / network failures in the UI
        with _lock:
            job["phase"] = "error"
            job["error"] = f"{ex.__class__.__name__}: {ex}"
            _log(job, job["error"])
    finally:
        with _lock:
            job["running"] = False
            job["current"] = None
            job["finished_at"] = time.time()


# ------------------------------------------------------------------ routes


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/start")
def api_start():
    global _job

    with _lock:
        if _job and _job["running"]:
            return jsonify({"error": "A fetch is already running."}), 409

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Enter a playlist or video URL."}), 400

    def as_int(key):
        raw = data.get(key)
        if raw in (None, ""):
            return None
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return None
        return val if val > 0 else None

    mode = data.get("mode") or "transcripts"
    if mode not in ("transcripts", "video", "audio"):
        return jsonify({"error": f"Unknown mode: {mode}"}), 400

    default_delay = 4.0 if mode == "transcripts" else 1.0
    try:
        delay = float(data.get("delay") if data.get("delay") not in (None, "") else default_delay)
    except (TypeError, ValueError):
        delay = default_delay
    delay = max(0.0, min(delay, 120.0))

    out_dir = _resolve_out(data.get("out"))

    params = {
        "url": url,
        "mode": mode,
        "out_dir": str(out_dir),
        "limit": as_int("limit"),
        "start": as_int("start"),
        "end": as_int("end"),
        "delay": delay,
        "quality": as_int("quality"),
    }

    _stop_flag.clear()
    job = _blank_job()
    job["running"] = True
    job["started_at"] = time.time()
    job["out_dir"] = str(out_dir)
    job["mode"] = mode

    with _lock:
        _job = job

    threading.Thread(target=_worker, args=(job, params), daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    _stop_flag.set()
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    with _lock:
        if _job is None:
            return jsonify(_blank_job())
        return jsonify(_job)


@app.get("/api/env")
def api_env():
    return jsonify({"ffmpeg": ffmpeg_available()})


@app.get("/api/config")
def api_config_get():
    out = default_out()
    resolved = resolve_out(out)
    return jsonify({
        "out_dir": out,
        "resolved": str(resolved),
        "exists": resolved.is_dir(),
        "folders": list_subfolders(),
        "env_override": bool(os.environ.get("YOINK_OUT")),
    })


@app.post("/api/config")
def api_config_set():
    data = request.get_json(silent=True) or {}
    out = (data.get("out_dir") or "").strip()
    if not out:
        return jsonify({"error": "Enter a folder."}), 400
    if os.environ.get("YOINK_OUT"):
        return jsonify({
            "error": "YOINK_OUT is set in the environment and overrides the saved "
                     "folder. Unset it to change the default here."
        }), 409

    resolved = resolve_out(out)
    if data.get("create"):
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            return jsonify({"error": f"Could not create folder: {ex}"}), 400

    save_config(out_dir=out)
    return jsonify({
        "out_dir": out,
        "resolved": str(resolved),
        "exists": resolved.is_dir(),
        "folders": list_subfolders(),
    })


@app.post("/api/reveal")
def api_reveal():
    """Open the download folder in the OS file browser. Local convenience --
    refused when the app is reachable from off-box, since opening windows on
    someone else's machine is not a remote user's business."""
    if TOKEN or os.environ.get("HOST", "127.0.0.1") not in ("127.0.0.1", "localhost"):
        return jsonify({"error": "Only available when running locally."}), 403

    target = _resolve_out((request.get_json(silent=True) or {}).get("out"))
    if not target.is_dir():
        return jsonify({"error": "Folder does not exist yet."}), 404

    opener = {"darwin": "open", "win32": "explorer"}.get(sys.platform, "xdg-open")
    try:
        subprocess.Popen([opener, str(target)])  # path only, no shell
    except OSError as ex:
        return jsonify({"error": str(ex)}), 500
    return jsonify({"ok": True})


@app.get("/api/files")
def api_files():
    out_dir = _resolve_out(request.args.get("out"))
    if not out_dir.is_dir():
        return jsonify({"dir": str(out_dir), "files": []})

    files = []
    for p in sorted(out_dir.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        ok = True
        if p.suffix == ".txt":
            try:
                # the marker is written immediately after the 2-line header
                ok = UNAVAILABLE_MARKER not in p.read_text(
                    encoding="utf-8", errors="replace"
                )[:600]
            except OSError:
                ok = False
        files.append(
            {
                "name": p.name,
                "size": p.stat().st_size,
                "modified": p.stat().st_mtime,
                "ok": ok,
            }
        )
    return jsonify({"dir": str(out_dir), "files": files})


def _safe_file(out_param, name):
    """Resolve `name` inside the output dir, refusing anything that escapes it."""
    out_dir = _resolve_out(out_param)
    target = (out_dir / name).resolve()
    if out_dir not in target.parents or not target.is_file():
        return None, out_dir
    return target, out_dir


@app.get("/api/file/<path:name>")
def api_file(name):
    target, _ = _safe_file(request.args.get("out"), name)
    if target is None:
        return jsonify({"error": "Not found."}), 404
    return jsonify(
        {
            "name": target.name,
            "text": target.read_text(encoding="utf-8", errors="replace"),
        }
    )


@app.get("/api/download/<path:name>")
def api_download(name):
    target, out_dir = _safe_file(request.args.get("out"), name)
    if target is None:
        return jsonify({"error": "Not found."}), 404
    return send_from_directory(out_dir, target.name, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "127.0.0.1")
    local_only = host in ("127.0.0.1", "localhost")

    url = f"http://127.0.0.1:{port}"
    if TOKEN:
        url += f"?token={TOKEN}"

    print(f"\n  Yoink running at {url}")
    if TOKEN:
        print("  Token auth is ON — requests need ?token=... or an X-Yoink-Token header.")
    elif not local_only:
        # Binding beyond localhost with no token means anyone who can route to
        # this port can queue downloads here and read the output folder.
        print(
            f"\n  !! Bound to {host} with NO token. Anyone who can reach this port can\n"
            f"  !! start downloads on this machine and read your output folder.\n"
            f"  !! Restart with a token:  YOINK_TOKEN={secrets.token_urlsafe(16)} HOST={host} python app.py"
        )
    print("  Press Ctrl+C to stop.\n")

    # only open a browser in the main process, not Flask's reloader child
    if local_only and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, debug=False, threaded=True)
