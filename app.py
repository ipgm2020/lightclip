
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

import imageio_ffmpeg
from flask import Flask, request, jsonify, send_file
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
print(f"FFMPEG PATH: {FFMPEG_PATH}")

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")

# Optional cookies support. Keep cookies.txt beside app.py, or set COOKIES_FILE.
if (BASE_DIR / "cookies.txt").exists():
    os.environ.setdefault("COOKIES_FILE", str(BASE_DIR / "cookies.txt"))

RATES = {
    "h264": {360: 600, 480: 900, 720: 1800, 1080: 3000},
    "h265": {360: 400, 480: 600, 720: 1200, 1080: 2000},
}
COOKIES_BROWSER = os.environ.get("COOKIES_BROWSER")
COOKIES_FILE = os.environ.get("COOKIES_FILE")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# job_id -> {"cancel": bool, "proc": subprocess.Popen}
JOBS = {}
JOBS_LOCK = threading.Lock()


class Cancelled(Exception):
    pass


class FFmpegError(Exception):
    pass


def pick_rate(codec, height):
    table = RATES[codec]
    key = next((k for k in sorted(table) if k >= height), max(table))
    return table[key]


def valid(url):
    return bool(re.match(r"^https?://", url or "", re.IGNORECASE))


def is_facebook(url):
    host_match = re.search(r"https?://(?:www\.)?(?:m\.)?(?:facebook\.com|fb\.watch|fb\.com)(?:/|$)", url or "", re.IGNORECASE)
    return bool(host_match)


def ydl_opts(extra=None):
    app.logger.warning("YT-DLP VERSION: %s", yt_dlp.version.__version__)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "ffmpeg_location": FFMPEG_PATH,
    }

    app.logger.warning("DENO PATH: %s", shutil.which("deno"))
    app.logger.warning("NODE PATH: %s", shutil.which("node"))

    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    elif COOKIES_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)
    if os.environ.get("IMPERSONATE"):
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget.from_str(os.environ["IMPERSONATE"])
    if extra:
        opts.update(extra)
    return opts


def explain(error):
    raw = ANSI.sub("", str(error)).replace("ERROR: ", "").strip()
    low = raw.lower()
    if "cannot parse data" in low or "unable to extract" in low:
        message = "Facebook’s page couldn’t be read. Try updating yt-dlp and, if needed, use cookies.txt."
    elif "unsupported url" in low:
        message = "This link isn’t supported."
    elif "sign in" in low or "bot" in low or "login" in low:
        message = "This site requires login. If you have permission to access the video, configure cookies.txt."
    elif "private" in low or "not available" in low or "unavailable" in low:
        message = "This video is private or unavailable."
    elif re.search(r"http error (403|429)", low):
        message = "The site blocked the request. Wait a bit and retry."
    elif "getaddrinfo" in low or "timed out" in low or "connection" in low:
        message = "Network problem. Check your internet connection."
    else:
        message = "Can’t read this link."
    return message, raw[-500:]


def get_job(job_id):
    with JOBS_LOCK:
        return JOBS.get(job_id)


@app.get("/")
def home():
    # index.html is served from the static folder, matching this app's layout.
    return app.send_static_file("index.html")


@app.post("/api/info")
def info():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not valid(url):
        return jsonify(error="Paste a full link starting with http:// or https://"), 400

    try:
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            data = ydl.extract_info(url, download=False)
        if data.get("entries"):
            data = next((entry for entry in data["entries"] if entry), {}) or {}
    except Exception as error:
        message, detail = explain(error)
        app.logger.warning("INFO ERROR: %s", detail)
        return jsonify(error=message, detail=detail), 400

    heights = sorted({
        fmt["height"]
        for fmt in data.get("formats", [])
        if fmt.get("height") and fmt.get("vcodec") != "none"
    }, reverse=True)
    heights = [height for height in heights if height >= 144][:8] or [720]

    return jsonify(
        title=data.get("title") or "video",
        thumbnail=data.get("thumbnail"),
        duration=data.get("duration"),
        heights=heights,
    )


@app.get("/api/progress/<job_id>")
def progress(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify(active=False, stage="finished", percent=100)
    total = job.get("total_bytes")
    downloaded = job.get("downloaded_bytes", 0)
    return jsonify(
        active=True,
        stage=job.get("stage", "starting"),
        percent=job.get("percent"),
        downloaded_bytes=downloaded,
        total_bytes=total,
        speed=job.get("speed", 0),
        eta=job.get("eta"),
        filename=job.get("filename", "video.mp4"),
    )


@app.post("/api/cancel")
def cancel():
    body = request.get_json(silent=True) or {}
    job = get_job(body.get("job_id"))
    if job:
        job["cancel"] = True
        process = job.get("proc")
        if process and process.poll() is None:
            process.kill()
    return jsonify(ok=True)


@app.post("/api/download")
def download():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    compact = bool(body.get("compact", True))
    mobile = bool(body.get("mobile", True))
    codec = body.get("codec", "h264")
    job_id = body.get("job_id") or str(uuid.uuid4())

    try:
        height = max(144, min(int(body.get("height", 720)), 2160))
    except (TypeError, ValueError):
        height = 720
    if codec not in RATES:
        codec = "h264"
    if not valid(url):
        return jsonify(error="Invalid link"), 400

    job = {
        "cancel": False, "proc": None,
        "stage": "starting", "percent": 0.0,
        "downloaded_bytes": 0, "total_bytes": None,
        "speed": 0.0, "eta": None, "filename": "video.mp4",
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    temp_dir = tempfile.mkdtemp(prefix="lightclip_")

    def progress_hook(data):
        if job["cancel"]:
            raise Cancelled()
        status = data.get("status")
        if status == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            job.update({
                "stage": "downloading",
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "speed": data.get("speed") or 0,
                "eta": data.get("eta"),
                "percent": min(99.0, downloaded * 100.0 / total) if total else None,
            })
        elif status == "finished":
            job.update({"stage": "merging", "percent": 99.0, "speed": 0.0})

    try:
        fmt = (
            f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={height}]+ba/b[height<={height}]/b"
        )
        if mobile and is_facebook(url):
            # Prefer the user's selected resolution first; use Facebook's SD
            # format only as a fallback when the requested quality is unavailable.
            fmt = fmt + "/sd"

        options = ydl_opts({
            "format": fmt,
            "merge_output_format": "mp4",
            "outtmpl": os.path.join(temp_dir, "src.%(ext)s"),
            "progress_hooks": [progress_hook],
        })
        with yt_dlp.YoutubeDL(options) as ydl:
            data = ydl.extract_info(url, download=True)

        if job["cancel"]:
            raise Cancelled()
        if data.get("entries"):
            data = next((entry for entry in data["entries"] if entry), {}) or {}

        app.logger.info("FORMAT USED: %s", data.get("format_id"))
        title = data.get("title") or "video"
        safe_name = re.sub(r"[^\w\- ]", "", title, flags=re.UNICODE)[:80].strip() or "video"
        job["filename"] = f"{safe_name}.mp4"
        job.update({"stage": "processing", "percent": 99.0, "speed": 0.0})
        fps = data.get("fps") or 30
        duration = data.get("duration") or 0

        source_files = [
            os.path.join(temp_dir, name)
            for name in os.listdir(temp_dir)
            if name.startswith("src.") and os.path.isfile(os.path.join(temp_dir, name))
        ]
        if not source_files:
            raise RuntimeError("yt-dlp completed without creating an output file.")
        source = source_files[0]

        output = source
        source_kbps = os.path.getsize(source) * 8 / 1000 / duration if duration else float("inf")
        rate = pick_rate(codec, height)
        got_sd = data.get("format_id") == "sd"

        if compact and not got_sd and source_kbps > rate * 1.15:
            if not shutil.which("ffmpeg"):
                raise FFmpegError("ffmpeg is not installed or not in PATH.")
            output = os.path.join(temp_dir, "out.mp4")
            long_video = duration > 1200
            video_filter = f"scale=-2:'min(ih,{height})'" + (",fps=30" if fps > 30 else "")

            if codec == "h265":
                video_encoder = [
                    "-c:v", "libx265",
                    "-preset", "faster" if long_video else "medium",
                    "-crf", "27",
                    "-tag:v", "hvc1",
                    "-x265-params", f"vbv-maxrate={rate}:vbv-bufsize={rate * 2}:log-level=error",
                ]
            else:
                video_encoder = [
                    "-c:v", "libx264",
                    "-preset", "faster" if long_video else "slow",
                    "-crf", "24",
                    "-profile:v", "high",
                    "-bf", "3",
                    "-maxrate", f"{rate}k",
                    "-bufsize", f"{rate * 2}k",
                ]

            command = [
                "FFMPEG_PATH", "-y", "-i", source, "-vf", video_filter,
                *video_encoder, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", output,
            ]
            log_path = os.path.join(temp_dir, "ffmpeg.log")
            with open(log_path, "wb") as log_file:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log_file, text=True, bufsize=1)
                job["proc"] = process
                try:
                    while True:
                        if job["cancel"]:
                            process.kill()
                            process.wait()
                            raise Cancelled()
                        line = process.stdout.readline() if process.stdout else ""
                        if line:
                            key, _, value = line.strip().partition("=")
                            if key == "out_time_ms" and duration:
                                # FFmpeg's out_time_ms value is in microseconds despite its name.
                                seconds = int(value or 0) / 1_000_000
                                job.update({"stage": "processing", "percent": min(99.0, seconds * 100 / duration), "speed": 0.0})
                        elif process.poll() is not None:
                            break
                        else:
                            process.wait(timeout=0.2)
                finally:
                    if process.stdout:
                        process.stdout.close()

            if job["cancel"]:
                raise Cancelled()
            if process.returncode != 0:
                with open(log_path, "rb") as log_file:
                    detail = log_file.read()[-500:].decode(errors="replace")
                raise FFmpegError(detail or "ffmpeg failed.")

        job.update({"stage": "ready", "percent": 100.0, "speed": 0.0})
        response = send_file(output, as_attachment=True, download_name=f"{safe_name}.mp4", mimetype="video/mp4")
        response.call_on_close(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        return response

    except Exception as error:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if job["cancel"] or isinstance(error, Cancelled):
            return jsonify(error="Cancelled."), 499
        if isinstance(error, FFmpegError):
            app.logger.warning("FFMPEG ERROR: %s", error)
            return jsonify(error="Encoding failed. Turn off Compact encoding.", detail=str(error)[-500:]), 500
        message, detail = explain(error)
        app.logger.warning("DOWNLOAD ERROR: %s", detail)
        if message == "Can’t read this link.":
            message = "Download failed."
        return jsonify(error=message, detail=detail), 500
    finally:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)


if __name__ == "__main__":
    # Debug mode is useful locally, but turn it off for public deployment.
    app.run(host="127.0.0.1", port=5000, debug=True)
