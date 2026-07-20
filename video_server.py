"""
Video Server for Instagram Reels/TikTok - LINUX/RENDER VERSION
Captions with Pillow + official Google Noto color emoji artwork

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
import re

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

# Official Google Noto Emoji PNG assets. Assets are downloaded only when an
# emoji is used, then cached in memory for the life of the server process.
NOTO_EMOJI_BASE_URL = os.getenv(
    "NOTO_EMOJI_BASE_URL",
    "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128",
).strip().rstrip("/")
EMOJI_CACHE = {}
EMOJI_CACHE_LOCK = threading.Lock()

# Caption typography: use Montserrat Medium at a consistent size.
# Keep every caption at the same size and stroke regardless of its length.
CAPTION_FONT_PATH = os.getenv("CAPTION_FONT_PATH", "").strip()
MONTSERRAT_MEDIUM_URL = os.getenv(
    "MONTSERRAT_MEDIUM_URL",
    "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-Medium.ttf",
).strip()
MONTSERRAT_MEDIUM_CACHE_PATH = os.path.join(tempfile.gettempdir(), "Montserrat-Medium.ttf")
FONT_CACHE_LOCK = threading.Lock()
CAPTION_FONT_SIZE = 70
CAPTION_STROKE_WIDTH = 2


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
        CAPTION_FONT_PATH,
        "/usr/share/fonts/truetype/montserrat/Montserrat-Medium.ttf",
        "/usr/share/fonts/opentype/montserrat/Montserrat-Medium.otf",
        MONTSERRAT_MEDIUM_CACHE_PATH,
    ]
    for p in candidates:
        try:
            if p and os.path.exists(p):
                return ImageFont.truetype(p, size)
        except Exception:
            pass

    # Render images do not consistently include Montserrat Medium. Download the
    # official font once and cache it in /tmp so this remains a single-file app.
    if MONTSERRAT_MEDIUM_URL:
        with FONT_CACHE_LOCK:
            try:
                if not os.path.exists(MONTSERRAT_MEDIUM_CACHE_PATH):
                    response = HTTP.get(MONTSERRAT_MEDIUM_URL, timeout=(10, 30))
                    response.raise_for_status()
                    with open(MONTSERRAT_MEDIUM_CACHE_PATH, "wb") as font_file:
                        font_file.write(response.content)
                return ImageFont.truetype(MONTSERRAT_MEDIUM_CACHE_PATH, size)
            except Exception as exc:
                print(f"Could not load Montserrat Medium: {exc}")

    # Non-bold fallback, so a missing Montserrat file never silently becomes bold.
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        try:
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def is_emoji_base(ch: str):
    """Return True for code points that commonly start an emoji sequence."""
    cp = ord(ch)
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2190 <= cp <= 0x21FF
        or 0x2300 <= cp <= 0x23FF
        or 0x25A0 <= cp <= 0x25FF
        or 0x2B00 <= cp <= 0x2BFF
        or cp in {
            0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139,
            0x3030, 0x303D, 0x3297, 0x3299,
        }
    )


def split_emoji_runs(text: str):
    """
    Split mixed text into (kind, value) runs while keeping emoji sequences,
    skin tones, flags, variation selectors, and ZWJ combinations together.
    """
    runs = []
    plain = []
    i = 0

    def flush_plain():
        if plain:
            runs.append(("text", "".join(plain)))
            plain.clear()

    while i < len(text):
        ch = text[i]
        cp = ord(ch)

        # Keycap sequences: 0-9, # or * + optional VS16 + combining keycap.
        is_keycap = (
            ch in "0123456789#*"
            and (
                (i + 1 < len(text) and ord(text[i + 1]) == 0x20E3)
                or (
                    i + 2 < len(text)
                    and ord(text[i + 1]) == 0xFE0F
                    and ord(text[i + 2]) == 0x20E3
                )
            )
        )

        # Regional indicators form flag emoji in pairs.
        is_regional = 0x1F1E6 <= cp <= 0x1F1FF

        if not is_emoji_base(ch) and not is_keycap and not is_regional:
            plain.append(ch)
            i += 1
            continue

        flush_plain()
        cluster = [ch]
        i += 1

        if is_regional and i < len(text) and 0x1F1E6 <= ord(text[i]) <= 0x1F1FF:
            cluster.append(text[i])
            i += 1

        # Variation selectors, skin tones, keycaps, and subdivision-flag tags.
        while i < len(text):
            next_cp = ord(text[i])
            if (
                next_cp in (0xFE0E, 0xFE0F, 0x20E3)
                or 0x1F3FB <= next_cp <= 0x1F3FF
                or 0xE0020 <= next_cp <= 0xE007F
            ):
                cluster.append(text[i])
                i += 1
            else:
                break

        # Zero-width joiner sequences such as family/profession emoji.
        while i < len(text) and ord(text[i]) == 0x200D:
            cluster.append(text[i])
            i += 1
            if i >= len(text):
                break
            cluster.append(text[i])
            i += 1
            while i < len(text):
                next_cp = ord(text[i])
                if next_cp in (0xFE0E, 0xFE0F) or 0x1F3FB <= next_cp <= 0x1F3FF:
                    cluster.append(text[i])
                    i += 1
                else:
                    break

        runs.append(("emoji", "".join(cluster)))

    flush_plain()
    return runs


def emoji_asset_filename(cluster: str):
    # Noto asset filenames omit text/emoji variation selectors.
    codepoints = [ord(ch) for ch in cluster if ord(ch) not in (0xFE0E, 0xFE0F)]
    return "emoji_u" + "_".join(f"{cp:x}" for cp in codepoints) + ".png"


def emoji_asset_url(cluster: str):
    codepoints = [ord(ch) for ch in cluster if ord(ch) not in (0xFE0E, 0xFE0F)]

    # Noto keeps two-letter country flags in its official region-flags folder
    # rather than the general png/128 directory.
    if len(codepoints) == 2 and all(0x1F1E6 <= cp <= 0x1F1FF for cp in codepoints):
        region = "".join(chr(ord("A") + cp - 0x1F1E6) for cp in codepoints)
        return (
            "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/"
            f"third_party/region-flags/png/{region}.png"
        )

    return f"{NOTO_EMOJI_BASE_URL}/{emoji_asset_filename(cluster)}"


def load_emoji_asset(cluster: str, size: int):
    """Download an official Noto emoji PNG once and return a safe copy."""
    cache_key = (cluster, int(size))
    with EMOJI_CACHE_LOCK:
        cached = EMOJI_CACHE.get(cache_key)
        if cached is not None:
            return cached.copy()

    url = emoji_asset_url(cluster)
    response = None
    try:
        response = safe_get(url, timeout=(6, 20))
        if response.status_code != 200:
            return None

        emoji_img = Image.open(BytesIO(response.content)).convert("RGBA")
        emoji_img.thumbnail((size, size), Image.Resampling.LANCZOS)

        with EMOJI_CACHE_LOCK:
            EMOJI_CACHE[cache_key] = emoji_img.copy()
        return emoji_img
    except Exception as exc:
        print(f"EMOJI ASSET ERROR ({cluster!r}): {exc}")
        return None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def mixed_text_width(draw, text: str, font, emoji_size: int):
    width = 0.0
    for kind, value in split_emoji_runs(text):
        if kind == "emoji":
            width += emoji_size
        else:
            width += draw.textlength(value, font=font)
    return width


def wrap_caption(caption: str, draw, font, emoji_size: int, max_width: int):
    """Wrap at words while preserving explicit line breaks and blank lines."""
    lines = []
    source_lines = caption.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for source_line in source_lines:
        if not source_line.strip():
            lines.append("")
            continue

        words = re.findall(r"\S+", source_line)
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if not current or mixed_text_width(draw, candidate, font, emoji_size) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)

    return lines


def draw_mixed_line(text_img, draw, line: str, y: int, font, fontsize: int, line_height: int):
    """Center and draw a line containing stroked text and color emoji."""
    emoji_size = fontsize
    line_width = mixed_text_width(draw, line, font, emoji_size)
    x = (text_img.width - line_width) / 2

    for kind, value in split_emoji_runs(line):
        if kind == "emoji":
            emoji_img = load_emoji_asset(value, emoji_size)
            if emoji_img is not None:
                emoji_x = int(round(x + (emoji_size - emoji_img.width) / 2))
                emoji_y = int(round(y + (line_height - emoji_img.height) / 2))
                text_img.alpha_composite(emoji_img, (emoji_x, emoji_y))
                emoji_img.close()
            else:
                # Graceful fallback if an asset is unavailable.
                draw.text(
                    (x, y), value, font=font, fill="white",
                    stroke_width=CAPTION_STROKE_WIDTH, stroke_fill="black",
                )
            x += emoji_size
        else:
            draw.text(
                (x, y), value, font=font, fill="white",
                stroke_width=CAPTION_STROKE_WIDTH, stroke_fill="black",
            )
            x += draw.textlength(value, font=font)


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
    # Keep every caption at the size previously used for ~36-character text.
    fontsize = CAPTION_FONT_SIZE

    text_img = Image.new("RGBA", (1080, 600), (0, 0, 0, 0))
    draw = ImageDraw.Draw(text_img)
    font = load_font(fontsize)

    max_width = 800
    line_height = fontsize + 10
    lines = wrap_caption(caption, draw, font, fontsize, max_width)
    total_height = len(lines) * line_height
    y = (600 - total_height) // 2

    for line in lines:
        draw_mixed_line(text_img, draw, line, y, font, fontsize, line_height)
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
