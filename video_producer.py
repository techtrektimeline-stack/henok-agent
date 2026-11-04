# -*- coding: utf-8 -*-
"""Generate a 30-second Amharic 9:16 motion-graphics MP4 for HENOK AI."""

import base64
import logging
import math
import os
import subprocess
import tempfile
import wave
from pathlib import Path

import imageio_ffmpeg
import requests
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("henok-ai.video")
W, H, FPS, SECONDS = 540, 960, 15, 30
MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts")
VOICE = os.getenv("GEMINI_TTS_VOICE", "Kore")
FONT_PATH = Path(tempfile.gettempdir()) / "NotoSansEthiopic-Regular.ttf"
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/notosansethiopic/NotoSansEthiopic%5Bwght%5D.ttf"
_FONT_DOWNLOAD_ATTEMPTED = False


def _get_font(size, bold=False):
    global _FONT_DOWNLOAD_ATTEMPTED
    if not FONT_PATH.exists() and not _FONT_DOWNLOAD_ATTEMPTED:
        _FONT_DOWNLOAD_ATTEMPTED = True
        try:
            response = requests.get(FONT_URL, timeout=(5, 20))
            response.raise_for_status()
            FONT_PATH.write_bytes(response.content)
        except Exception as exc:
            log.warning("Noto Sans Ethiopic font unavailable: %s", type(exc).__name__)
    if FONT_PATH.exists():
        try:
            return ImageFont.truetype(str(FONT_PATH), size=size)
        except Exception:
            pass
    fallback = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        return ImageFont.truetype(fallback, size=size)
    except Exception:
        return ImageFont.load_default()


def _tts_to_wav(text, api_key, path):
    """Generate WAV narration using the Gemini 3.8 Flash TTS Interactions API."""
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    body = {
        "model": MODEL,
        "input": [{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": text,
                "annotations": [{
                    "type": "speech_metadata",
                    "style": "Clear, confident, warm Ethiopian Amharic narration. Medium-fast pace, articulate pronunciation, engaging technology explainer."
                }]
            }]
        }],
        "response_format": {"type": "audio"},
        "generation_config": {
            "speech_config": [{"voice": VOICE}]
        }
    }
    response = requests.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=(15, 150),
    )
    if not response.ok:
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except Exception:
            detail = response.text
        raise RuntimeError(f"Gemini TTS HTTP {response.status_code}: {str(detail)[:500]}")
    payload = response.json()
    try:
        raw = base64.b64decode(payload["output_audio"]["data"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Gemini TTS response contained no output_audio.data") from exc
    if not raw.startswith(b"RIFF"):
        raise RuntimeError("Gemini TTS returned unexpected audio format; expected WAV")
    Path(path).write_bytes(raw)

def _center_text(draw, text, y, font, color, max_width=470, gap=12):
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = (current + " " + word).strip()
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    heights = [draw.textbbox((0, 0), line, font=font)[3] for line in lines]
    total = sum(heights) + gap * max(0, len(lines) - 1)
    top = y - total / 2
    for line, height in zip(lines, heights):
        box = draw.textbbox((0, 0), line, font=font)
        x = (W - (box[2] - box[0])) / 2
        draw.text((x, top), line, font=font, fill=color, stroke_width=1, stroke_fill=(4, 10, 22))
        top += height + gap


def _frame(t):
    im = Image.new("RGB", (W, H), (6, 13, 29))
    d = ImageDraw.Draw(im, "RGBA")
    for x in range(-70, W + 80, 70):
        d.line((x + int(t * 5) % 70, 0, x - 90, H), fill=(48, 88, 130, 35), width=1)
    for y in range(40, H, 70):
        d.line((0, y, W, y), fill=(48, 88, 130, 24), width=1)

    scene = min(4, int(t // 6))
    p = (t % 6) / 6
    white, cyan, muted = (245, 249, 255, 255), (50, 210, 255, 255), (165, 190, 220, 255)
    title, sub, small = _get_font(42, True), _get_font(25), _get_font(20, True)

    for i in range(12):
        a = t * 0.55 + i * math.pi / 6
        r = 145 + (i % 3) * 35
        x, y = W / 2 + math.cos(a) * r, 365 + math.sin(a) * r * .7
        d.ellipse((x-3, y-3, x+3, y+3), fill=(45, 195, 255, 150))

    if scene == 0:
        d.rounded_rectangle((65, 275, 475, 405), radius=28, fill=(12, 38, 70, 245), outline=cyan, width=3)
        f = _get_font(59, True)
        box = d.textbbox((0, 0), "HENOK AI", font=f)
        d.text(((W-(box[2]-box[0]))/2, 315), "HENOK AI", font=f, fill=white)
        _center_text(d, "ብልህ የዲጂታል ረዳትህ", 525, sub, cyan)
        _center_text(d, "YOUR IDEAS. MADE REAL.", 610, small, muted)
    elif scene == 1:
        xx = int(35 + 60 * (1-min(1, p*2)))
        d.rounded_rectangle((xx, 225, xx+450, 355), radius=22, fill=(17, 48, 80, 245), outline=cyan, width=2)
        d.ellipse((xx+22, 262, xx+80, 320), fill=(30, 145, 215, 255))
        d.text((xx+100, 255), "TELEGRAM", font=small, fill=white)
        d.text((xx+100, 296), "YOUR INSTRUCTION", font=sub, fill=muted)
        cy = 535
        for i in range(3):
            r = 65 + i*29 + int(5*math.sin(t*3+i))
            d.ellipse((W/2-r, cy-r, W/2+r, cy+r), outline=(45, 200, 255, 190-i*40), width=3)
        d.ellipse((W/2-52, cy-52, W/2+52, cy+52), fill=(15, 83, 137, 255), outline=cyan, width=3)
        _center_text(d, "AI", cy, _get_font(44, True), white)
        _center_text(d, "ትዕዛዝህን ይረዳል", 735, title, white)
    elif scene == 2:
        for i, label in enumerate(("SCRIPT", "VOICE", "VISUALS")):
            y = 245 + i*130
            x = 45 + int(55*(1-min(1, p*2)))
            d.rounded_rectangle((x, y, x+450, y+92), radius=20, fill=(17, 44, 75, 245), outline=(45, 195, 255, 180), width=2)
            d.ellipse((x+22, y+24, x+66, y+68), fill=(35+18*i, 150, 225, 255))
            d.text((x+90, y+25), label, font=_get_font(30, True), fill=white)
        _center_text(d, "ጽሑፍ • ድምፅ • ምስል", 760, sub, cyan)
    elif scene == 3:
        d.rounded_rectangle((30, 220, 510, 660), radius=24, fill=(12, 29, 53, 245), outline=(50, 110, 165, 210), width=2)
        for row in range(3):
            y = 290 + row*105
            d.rounded_rectangle((58, y, 482, y+58), radius=12, fill=(22, 50, 82, 255))
            for j in range(3):
                x = 72 + j*132 + int((t*20) % 22)
                d.rounded_rectangle((x, y+8, x+94, y+50), radius=8, fill=(25+row*15, 110+j*22, 165+j*16, 255))
        px = 65 + int(p*390)
        d.line((px, 270, px, 625), fill=(255, 205, 80, 255), width=4)
        _center_text(d, "MOTION GRAPHICS", 760, title, white)
        _center_text(d, "ከሀሳብ ወደ ቪዲዮ", 835, sub, cyan)
    else:
        d.rounded_rectangle((70, 230, 470, 625), radius=30, fill=(13, 49, 68, 245), outline=(65, 225, 185, 255), width=3)
        d.rounded_rectangle((145, 300, 395, 455), radius=18, fill=(24, 88, 115, 255), outline=(70, 225, 190, 255), width=2)
        d.polygon([(245, 335), (245, 420), (320, 377)], fill=white)
        d.rounded_rectangle((190, 490, 350, 540), radius=18, fill=(25, 160, 125, 255))
        _center_text(d, "MP4 READY", 510, _get_font(30, True), white)
        _center_text(d, "ቪዲዮው በTelegram", 710, title, white)
        _center_text(d, "HENOK AI • ሀሳብህን ወደ ፍጻሜ", 795, sub, cyan)

    d.rounded_rectangle((35, 915, 505, 922), radius=4, fill=(35, 55, 80, 255))
    d.rounded_rectangle((35, 915, 35+int(470*t/SECONDS), 922), radius=4, fill=cyan)
    return im


def _encode(audio_path, output_path):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-i", str(audio_path),
        "-vf", "scale=1080:1920:flags=lanczos,format=yuv420p",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
        "-c:a", "aac", "-b:a", "112k", "-af", "apad,atrim=duration=30",
        "-t", "30", "-movflags", "+faststart", str(output_path)
    ]
    with tempfile.TemporaryFile() as errfile:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errfile)
        try:
            for n in range(FPS * SECONDS):
                proc.stdin.write(_frame(n / FPS).tobytes())
            proc.stdin.close()
            code = proc.wait(timeout=240)
        except Exception:
            proc.kill()
            raise
        if code:
            errfile.seek(0)
            raise RuntimeError("FFmpeg failed: " + errfile.read().decode("utf-8", "replace")[-1200:])


def produce_henok_ai_video(api_key, output_path):
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing")
    narration = (
        "ሰላም! ይህ HENOK AI ነው። በTelegram ትዕዛዝህን ትሰጠዋለህ፤ "
        "Gemini ጥያቄህን ይረዳል። ስክሪፕትና የይዘት እቅድ ያዘጋጃል፤ "
        "የአማርኛ ድምፅ ያመነጫል፤ በMotion Graphics ወደ ቪዲዮ ይቀይራል። "
        "በመጨረሻ የተጠናቀቀውን ቪዲዮ በTelegram ይመልስልሃል። "
        "HENOK AI፤ ሀሳብህን ወደ ፍጻሜ!"
    )
    with tempfile.TemporaryDirectory(prefix="henok-ai-") as folder:
        audio = Path(folder) / "narration.wav"
        _tts_to_wav(narration, api_key, audio)
        _encode(audio, output_path)
    if not Path(output_path).is_file() or Path(output_path).stat().st_size < 50000:
        raise RuntimeError("MP4 output was not created successfully")
    return output_path
