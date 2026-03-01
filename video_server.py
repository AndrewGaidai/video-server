"""
Video Server for Instagram Reels/TikTok - LINUX/RENDER VERSION
Captions with Pillow (NO emoji support)

Audio is source of truth:
- Video duration matches audio duration exactly
- Image switching is driven by switch_times (absolute timestamps in seconds)
- Last image always holds until audio ends
- Tiny fadeout to remove end pop/click

NEW:
- Client sends track_id (not music_url, not switch_times)
- Server fetches track from Supabase table: public.factory_music
- Optimized for low RAM: preprocess images to temp PNG and ImageClip(file)
- Download URL is short-lived (TTL); BuildShip downloads and uploads elsewhere
- Robust retries for Supabase + media downloads (Render sometimes resets sockets)
"""

from flask import Flask, request, jsonify, send_file
from moviepy.audio.fx import all as afx
from moviepy.editor import ImageClip, concatenate_videoclips, AudioFileClip, CompositeVideoClip
from PIL import Image, ImageDraw, ImageFont
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from io import BytesIO
import os
import uuid
import threading
import gc
import json
import tempfile
import time

app = Flask(__name__)
rendering_lock = threading.Lock()

# Required: for track lookup only
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
FACTORY_MUSIC_TABLE = "factory_music"

# Optional: if empty, we’ll use request.host_url
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()

# How long to keep mp4 around for Buildship to fetch
VIDEO_TTL_SECONDS = int(os.getenv("VIDEO_TTL_SECONDS", "600"))  # 10 min
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "10"))

# Render tuning (no quality loss)
FFMPEG_THREADS = int(os.getenv("FFMPEG_THREADS", "2"))  # safer on 4GB
VIDEO_BITRATE = os.getenv("VIDEO_BITRATE", "5000k")
VIDEO_PRESET = os.getenv("VIDEO_PRESET", "veryfast")

# In-memory registry: token -> {path, expires_at}
VIDEO_CACHE = {}
CACHE_LOCK = threading.Lock()

# Requests session with retries (fixes RemoteDisconnected / ECONNRESET style failures)
HTTP = requests.Session()
_retry = Retry(
    total=5,
    connect=5,
    read=5,
    backoff_factor=0.6,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET", "POST", "PUT"),
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retry, pool_connections=20, pool_maxsize=20)
HTTP.mount("https://", _adapter)
HTTP.mount("http://", _adapter)
HTTP.headers.update({"User-Agent": "video-server/1.0"})


def supabase_headers(json_content=False):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    hdrs = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    if json_content:
        hdrs["Content-Type"] = "application/json"
    return hdrs


def to_float_list(x):
    if x is None:
        return []
    if isinstance(x, list):
        out = []
        for v in x:
            try:
                out.append(float(v))
            except Exception:
                continue
        return out
    return []


def parse_switch_times(value):
    if value is None:
        return []
    if isinstance(value, list):
        return to_float_list(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return to_float_list(parsed)
        except Exception:
            return []
    return []


def compute_durations_from_switch_times(switch_times, audio_duration):
    times = []
    for t in to_float_list(switch_times):
        if 0.0 < t < float(audio_duration):
            times.append(float(t))
    times = sorted(set(times))
    boundaries = [0.0] + times + [float(audio_duration)]
    durations = []
    for i in range(len(boundaries) - 1):
        d = boundaries[i + 1] - boundaries[i]
        if d < 0:
            d = 0.0
        durations.append(d)
    if durations and durations[-1] < 0.01:
        durations[-1] = max(0.01, durations[-1])
    return durations


def safe_get(url, *, headers=None, params=None, timeout=(6, 30), stream=False):
    """
    Robust GET:
    - uses session retries
    - falls back to fresh connection if upstream closes keep-alive socket
    """
    try:
        return HTTP.get(url, headers=headers, params=params, timeout=timeout, stream=stream)
    except requests.exceptions.RequestException:
        return requests.get(url, headers=headers, params=params, timeout=timeout, stream=stream)


def fetch_track_by_track_id(track_id: str):
    hdrs = supabase_headers()
    if hdrs is None:
        raise Exception("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY env vars.")

    endpoint = f"{SUPABASE_URL}/rest/v1/{FACTORY_MUSIC_TABLE}"
    params = {
        "track_id": f"eq.{track_id}",
        "select": "track_id,url,switch_times,images_needed,fps",
        "limit": "1",
    }

    r = safe_get(endpoint, headers=hdrs, params=params, timeout=(6, 20))
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise Exception(f"Track not found: track_id='{track_id}'")

    row = rows[0]
    music_url = (row.get("url") or "").strip()
    switch_times = parse_switch_times(row.get("switch_times"))

    if not music_url:
        raise Exception(f"Track '{track_id}' has empty url.")
    if not switch_times:
        raise Exception(f"Track '{track_id}' has empty/invalid switch_times.")

    return {"music_url": music_url, "switch_times": switch_times}


def load_font(size: int):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for p in candidates:
        try:
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def resize_and_crop(img, target_width, target_height):
    img_width, img_height = img.size
    target_ratio = target_width / target_height
    img_ratio = img_width / img_height

    if img_ratio > target_ratio:
        new_height = target_height
        new_width = int(target_height * img_ratio)
    else:
        new_width = target_width
        new_height = int(target_width / img_ratio)

    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

    left = (new_width - target_width) // 2
    top = (new_height - target_height) // 2
    right = left + target_width
    bottom = top + target_height

    return img.crop((left, top, right, bottom))


def write_caption_png(caption: str, out_path: str):
    fontsize = 100 if len(caption) < 30 else 80
    stroke_width = 2

    text_img = Image.new("RGBA", (1080, 600), (0, 0, 0, 0))
    draw = ImageDraw.Draw(text_img)
    font = load_font(fontsize)

    max_width = 800
    lines = []
    words = caption.split()
    current = []

    for w in words:
        test = " ".join(current + [w])
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current.append(w)
        else:
            if current:
                lines.append(" ".join(current))
            current = [w]
    if current:
        lines.append(" ".join(current))

    line_height = fontsize + 10
    total_height = len(lines) * line_height
    y = (600 - total_height) // 2

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        x = (1080 - w) // 2

        for dx in range(-stroke_width, stroke_width + 1):
            for dy in range(-stroke_width, stroke_width + 1):
                if dx or dy:
                    draw.text((x + dx, y + dy), line, font=font, fill="black")

        draw.text((x, y), line, font=font, fill="white")
        y += line_height

    text_img.save(out_path)


def cleanup_loop():
    while True:
        now = time.time()
        to_delete = []
        with CACHE_LOCK:
            for token, meta in list(VIDEO_CACHE.items()):
                if meta["expires_at"] <= now:
                    to_delete.append((token, meta["path"]))
                    del VIDEO_CACHE[token]
        for _, path in to_delete:
            try:
                if os.path.exists(path):
                    os.remove(path)
                # try deleting parent tmp dir (might be empty now)
                parent = os.path.dirname(path)
                try:
                    os.rmdir(parent)
                except Exception:
                    pass
            except Exception:
                pass
        time.sleep(CLEANUP_INTERVAL_SECONDS)

threading.Thread(target=cleanup_loop, daemon=True).start()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/download/<token>.mp4", methods=["GET"])
def download_video(token):
    with CACHE_LOCK:
        meta = VIDEO_CACHE.get(token)
    if not meta:
        return jsonify({"error": "Not found or expired"}), 404

    path = meta["path"]
    if not os.path.exists(path):
        return jsonify({"error": "File missing"}), 404

    return send_file(path, mimetype="video/mp4", as_attachment=False)


@app.route("/create-video", methods=["POST"])
def create_video():
    if not rendering_lock.acquire(blocking=False):
        return jsonify({"error": "Server busy", "retry_after": 60}), 429

    tmp_dir = None
    audio = None
    video = None
    clips = []
    temp_img_paths = []
    caption_png = None

    try:
        data = request.json or {}
        image_urls = data.get("image_urls", [])
        track_id = (data.get("track_id") or "").strip()
        caption = data.get("caption", "")

        if not image_urls or not track_id:
            return jsonify({"error": "Missing image_urls or track_id"}), 400

        track = fetch_track_by_track_id(track_id)
        music_url = track["music_url"]
        switch_times = track["switch_times"]

        tmp_dir = tempfile.mkdtemp(prefix="vidsrv_")
        token = uuid.uuid4().hex
        music_path = os.path.join(tmp_dir, f"{token}.mp3")
        video_path = os.path.join(tmp_dir, f"{token}.mp4")

        # download music
        r = safe_get(music_url, timeout=(10, 60))
        r.raise_for_status()
        with open(music_path, "wb") as f:
            f.write(r.content)

        audio = AudioFileClip(music_path)
        audio_duration = float(audio.duration)

        durations = compute_durations_from_switch_times(switch_times, audio_duration)
        segments = len(durations)

        # ensure N images
        if len(image_urls) < segments:
            image_urls = image_urls + [image_urls[-1]] * (segments - len(image_urls))
        else:
            image_urls = image_urls[:segments]

        # OPTIMIZATION: preprocess to lossless PNG on disk to reduce RAM usage
        for idx, (img_url, duration) in enumerate(zip(image_urls, durations), start=1):
            resp = safe_get(img_url, timeout=(10, 60))
            resp.raise_for_status()

            img = Image.open(BytesIO(resp.content))
            if img.mode != "RGB":
                img = img.convert("RGB")
            img = resize_and_crop(img, 1080, 1920)

            img_path = os.path.join(tmp_dir, f"{token}_{idx}.png")
            img.save(img_path)  # lossless
            temp_img_paths.append(img_path)

            img.close()
            try:
                resp.close()
            except Exception:
                pass

            clips.append(ImageClip(img_path).set_duration(float(duration)))

        # chain is lighter than compose when all clips same size
        video = concatenate_videoclips(clips, method="chain").set_duration(audio_duration)

        if caption:
            try:
                caption_png = os.path.join(tmp_dir, f"{token}_caption.png")
                write_caption_png(caption, caption_png)
                txt_clip = ImageClip(caption_png, transparent=True).set_duration(audio_duration).set_position(("center", 1100))
                video = CompositeVideoClip([video, txt_clip]).set_duration(audio_duration)
            except Exception as e:
                print(f"CAPTION ERROR: {e}")

        audio2 = audio.subclip(0, audio_duration).fx(afx.audio_fadeout, 0.04)
        video = video.set_audio(audio2)

        video.write_videofile(
            video_path,
            fps=24,
            codec="libx264",
            audio_codec="aac",
            preset=VIDEO_PRESET,
            threads=FFMPEG_THREADS,
            bitrate=VIDEO_BITRATE,
            ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
            verbose=False,
            logger=None
        )

        # delete temp images/caption/mp3 immediately (keep only mp4 for Buildship)
        for p in temp_img_paths:
            try:
                os.remove(p)
            except Exception:
                pass
        if caption_png:
            try:
                os.remove(caption_png)
            except Exception:
                pass
        try:
            os.remove(music_path)
        except Exception:
            pass

        with CACHE_LOCK:
            VIDEO_CACHE[token] = {"path": video_path, "expires_at": time.time() + VIDEO_TTL_SECONDS}

        base = PUBLIC_BASE_URL.rstrip("/")
        if not base:
            base = request.host_url.rstrip("/")

        video_url = f"{base}/download/{token}.mp4"
        return jsonify({"success": True, "track_id": track_id, "segments": segments, "video_url": video_url})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        rendering_lock.release()

        try:
            if video is not None:
                video.close()
        except Exception:
            pass
        try:
            if audio is not None:
                audio.close()
        except Exception:
            pass
        for c in clips:
            try:
                c.close()
            except Exception:
                pass

        gc.collect()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"Video Server (Render) on :{port}")
    app.run(host="0.0.0.0", port=port, debug=False)