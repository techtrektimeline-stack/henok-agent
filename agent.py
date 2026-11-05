# -*- coding: utf-8 -*-
import os
import re
import json
import time
import logging
import threading
import queue
import base64
import subprocess
import tempfile
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, List, Dict, Any

import requests
import telebot
import imageio_ffmpeg
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

# እውነተኛና አስተማማኝ የሞዴል ስሞች
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
OPENROUTER_MODEL = "google/gemini-2.0-flash-exp:free"

TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID", "-1003098019114").strip()
OWNER_ID = os.getenv("OWNER_ID", "").strip()

MAX_AUDIO_BYTES = 14 * 1024 * 1024
TELEGRAM_TEXT_LIMIT = 3900
HISTORY_LIMIT = 20

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)
HTTP = requests.Session()
STATE_LOCK = threading.RLock()
UPDATE_QUEUE = queue.Queue(maxsize=100)
GEMINI_TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts")
GEMINI_TTS_VOICE = os.getenv("GEMINI_TTS_VOICE", "Kore")

HISTORY: Dict[int, List[Dict[str, str]]] = {}
SESSIONS: Dict[int, Dict[str, Any]] = {}
PACKAGE_COUNTER = 0

# Telegram Webhook + health endpoint. Webhook avoids competing getUpdates pollers.
import hashlib

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
WEBHOOK_PATH = "/telegram"
WEBHOOK_SECRET = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode("utf-8")).hexdigest()[:48]
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logging.info("HTTP %s - %s", self.address_string(), fmt % args)

    def _reply(self, status, body):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        if self.path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(b"HENOK AI webhook service is healthy")))
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            return self._reply(200, "HENOK AI webhook service is healthy")
        return self._reply(404, "Not found")

    def do_POST(self):
        if self.path != WEBHOOK_PATH:
            return self._reply(404, "Not found")
        supplied = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if supplied != WEBHOOK_SECRET:
            logging.warning("Rejected webhook request: invalid secret")
            return self._reply(403, "Forbidden")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                return self._reply(400, "Invalid payload size")
            payload = self.rfile.read(length).decode("utf-8")
            update = telebot.types.Update.de_json(payload)
            try:
                UPDATE_QUEUE.put_nowait(update)
            except queue.Full:
                logging.error("Webhook update queue is full; Telegram will retry")
                return self._reply(503, "Busy")
            return self._reply(200, "OK")
        except Exception:
            logging.exception("Failed to process Telegram webhook update")
            return self._reply(500, "Update processing failed")

SYSTEM_PROMPT = """
You are HENOK AI, personal executive AI partner to ሄኖክ (Henok Habtamu) in Addis Ababa, Ethiopia.
- YouTube: TechTrek Timeline & Henok Travel
- Telegram Community: Henok Graphics
- Business: Sekela Marketing, Amazon KDP, Ye-buna

Rules:
1. Always reply in clear, modern, engaging Amharic. Tech terms stay in English.
2. Answer direct questions directly and concisely. No menus unless asked to create a post.
3. Address him warmly as 'ሄኖክ'.
"""

TOPICS = {
    "edit": ("🎬 ቪዲዮ ኤዲቲንግ Tips", "የቪዲዮ ኤዲቲንግ ፈጣን Tips (Premiere/CapCut)"),
    "free": ("💼 Freelancing", "የፍሪላንሲንግ እና የደንበኛ አያያዝ"),
    "motion": ("⚡ Motion Graphics", "After Effects እና Motion Graphics"),
    "color": ("🎨 Color Grading", "Color Grading እና የዲዛይን መርሆዎች"),
    "ai": ("🤖 AI Tools", "አዳዲስ AI Tools ለዲጂታል ፈጣሪዎች"),
    "pick": ("🎲 አንተ ምረጥልኝ", "ተስማሚ የይዘት ርዕስ ራስህ ምረጥ"),
}
STYLES = {
    "dark": ("🖤 Dark + Neon", "Dark Theme with neon blue and purple accents"),
    "clean": ("⚪ Minimalist Clean", "Clean minimalist white, black and neutral palette"),
    "bold": ("🔥 Bold Contrast", "Bold high-contrast yellow and black palette"),
}
PLATFORMS = {
    "tg": ("📢 Telegram", "Telegram post: useful copy, emojis, CTA"),
    "tt": ("🎵 TikTok", "TikTok: short caption, script, hashtags"),
    "ig": ("📸 Instagram", "Instagram: punchy caption, line breaks, hashtags"),
    "yt": ("▶️ YouTube Shorts", "YouTube Shorts: title, 30-45s script, description"),
}

def safe_send(chat_id, text, markup=None):
    text = str(text or "").strip() or "ምላሽ አልተገኘም።"
    chunks = [text[i:i + TELEGRAM_TEXT_LIMIT] for i in range(0, len(text), TELEGRAM_TEXT_LIMIT)]
    for idx, c in enumerate(chunks):
        bot.send_message(chat_id, c, reply_markup=markup if idx == len(chunks) - 1 else None)

def add_history(chat_id, role, text):
    with STATE_LOCK:
        items = HISTORY.setdefault(chat_id, [])
        items.append({"role": role, "text": text})
        del items[:-HISTORY_LIMIT]

def call_gemini(contents, timeout=45):
    if not GEMINI_API_KEY:
        return None
    models = [GEMINI_MODEL, "gemini-2.5-flash", "gemini-2.0-flash"]
    for m in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": contents,
        }
        try:
            r = HTTP.post(url, headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}, json=payload, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                cands = data.get("candidates") or []
                if cands:
                    parts = cands[0].get("content", {}).get("parts", [])
                    txt = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
                    if txt:
                        return txt
        except Exception:
            continue
    return None

def call_openrouter(messages):
    if not OPENROUTER_API_KEY:
        return None
    models = ["google/gemini-2.0-flash-exp:free", "meta-llama/llama-3.3-70b-instruct:free", "deepseek/deepseek-chat"]
    for m in models:
        try:
            r = HTTP.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={"model": m, "messages": messages},
                timeout=30,
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                if content:
                    return str(content).strip()
        except Exception:
            continue
    return None

def call_gemini_audio(raw_bytes, mime_type, prompt, timeout=90):
    """Analyze Telegram audio through the current Gemini Interactions API."""
    if not GEMINI_API_KEY:
        logging.error("Voice analysis unavailable: GEMINI_API_KEY is not configured")
        return None
    payload = {
        "model": "gemini-3.8-flash",
        "system_instruction": SYSTEM_PROMPT,
        "input": [
            {"type": "text", "text": prompt},
            {
                "type": "audio",
                "data": base64.b64encode(raw_bytes).decode("ascii"),
                "mime_type": mime_type,
            },
        ],
    }
    try:
        response = HTTP.post(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json=payload,
            timeout=(15, timeout),
        )
        if not response.ok:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except Exception:
                detail = response.text
            logging.error("Gemini audio API HTTP %s: %s", response.status_code, str(detail)[:1000])
            return None
        data = response.json()
        answer = data.get("output_text")
        if answer:
            return str(answer).strip()
        # REST responses may expose text in model_output steps.
        collected = []
        for step in data.get("steps", []):
            if step.get("type") == "model_output":
                for block in step.get("content", []):
                    if block.get("type") == "text" and block.get("text"):
                        collected.append(block["text"])
        answer = "\\n".join(collected).strip()
        if not answer:
            logging.error("Gemini audio API returned no text; status=%s keys=%s",
                          data.get("status"), list(data.keys()))
        return answer or None
    except requests.RequestException as exc:
        logging.exception("Gemini audio request failed: %s", type(exc).__name__)
        return None
    except Exception:
        logging.exception("Unexpected Gemini audio processing failure")
        return None


def reply_with_ai(chat_id, user_text, extra_parts=None):
    hist = HISTORY.get(chat_id, [])[-10:]
    gemini_contents = [{"role": "user" if h["role"] == "user" else "model", "parts": [{"text": h["text"]}]} for h in hist]
    cur_parts = list(extra_parts or [])
    if user_text:
        cur_parts.append({"text": user_text})
    gemini_contents.append({"role": "user", "parts": cur_parts})
    
    ans = call_gemini(gemini_contents)
    if ans:
        return ans
    if extra_parts:
        return None
        
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + hist + [{"role": "user", "content": user_text}]
    return call_openrouter(msgs)

def topic_menu():
    m = InlineKeyboardMarkup(row_width=1)
    m.add(*[InlineKeyboardButton(label, callback_data=f"t:{k}") for k, (label, _) in TOPICS.items()])
    return m

def style_menu():
    m = InlineKeyboardMarkup(row_width=1)
    m.add(*[InlineKeyboardButton(label, callback_data=f"s:{k}") for k, (label, _) in STYLES.items()])
    return m

def platform_menu():
    m = InlineKeyboardMarkup(row_width=2)
    m.add(*[InlineKeyboardButton(label, callback_data=f"p:{k}") for k, (label, _) in PLATFORMS.items()])
    m.add(InlineKeyboardButton("🌐 ለሁሉም", callback_data="p:all"))
    return m

def package_menu(pid, plat):
    label = "✅ አጽድቅና ግሩፕ ላይ ለጥፍ" if plat == "tg" else "✅ አጽድቅ"
    m = InlineKeyboardMarkup(row_width=2)
    m.add(
        InlineKeyboardButton(label, callback_data=f"pub:{pid}"),
        InlineKeyboardButton("🔄 ሌላ ስሪት", callback_data=f"reg:{pid}"),
        InlineKeyboardButton("❌ ሰርዝ", callback_data=f"can:{pid}"),
    )
    return m

def make_package(chat_id, topic, style, plat):
    global PACKAGE_COUNTER
    bot.send_chat_action(chat_id, "typing")
    prompt = f"""ለHenok Graphics ማህበረሰብ ይዘት አዘጋጅ።
ርዕስ: {topic} | ፕላትፎርም: {PLATFORMS[plat][1]} | ስታይል: {style}
===POST===
(የሚለጠፈው ጽሑፍ ከነ Hook፣ Body፣ CTA እና Hashtags)
===CANVA===
🎨 Color Palette: 3-4 HEX colors.
🔤 Overlay Hook: በምስሉ ላይ የሚታይ አጭር ጽሑፍ።
🖼 Composition: የምስሉ አቀማመጥ መመሪያ።
"""
    full = reply_with_ai(chat_id, prompt)
    if not full:
        safe_send(chat_id, "ይቅርታ ሄኖክ፣ ፖስቱን ማመንጨት አልተቻለም።")
        return
    post, canva = full, ""
    if "===POST===" in full:
        post = full.split("===POST===", 1)[1]
    if "===CANVA===" in post:
        post, canva = post.split("===CANVA===", 1)
    with STATE_LOCK:
        PACKAGE_COUNTER += 1
        pid = PACKAGE_COUNTER
        SESSIONS.setdefault(chat_id, {"pkgs": {}})["pkgs"][pid] = {
            "post": post.strip(), "platform": plat
        }
    safe_send(chat_id, f"📢 **ፖስት ({PLATFORMS[plat][0]}):**\n\n{post.strip()}\n\n━━━━━━━━━━━━━━━\n🎨 **Canva Blueprint:**\n\n{canva.strip()}", package_menu(pid, plat))

def advance(chat_id):
    s = SESSIONS.setdefault(chat_id, {"pending": {}})
    p = s["pending"]
    if not p.get("topic"):
        safe_send(chat_id, "❓ ስለ ምን ርዕስ ፖስት ላዘጋጅ?", topic_menu())
        return
    if not p.get("style"):
        safe_send(chat_id, "❓ የCanva Design ስታይል ምረጥ፦", style_menu())
        return
    if not p.get("platforms"):
        safe_send(chat_id, "❓ ለየትኛው ፕላትፎርም ይሁን?", platform_menu())
        return
    for pk in p.get("platforms"):
        make_package(chat_id, p["topic"], p["style"], pk)
    s["pending"] = {}

@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    safe_send(message.chat.id, "ሰላም ሄኖክ! 👋\n• መደበኛ ጥያቄዎችን ጠይቀኝ፤ በቀጥታ እመልሳለሁ።\n• Voice Note ላክ፤ አዳምጬ ጽሑፉንና Action Items አወጣለሁ።\n• ፖስት ለመስራት **'ፖስት'** በለኝ።")

@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(message, f"Telegram User ID: {message.from_user.id}")

def send_voice_reply(chat_id, text):
    """Create an OGG/Opus Telegram voice note from a short text reply."""
    if not GEMINI_API_KEY or not text:
        return False
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    body = {
        "model": GEMINI_TTS_MODEL,
        "input": [{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": text[:3500],
                "annotations": [{
                    "type": "speech_metadata",
                    "style": "Natural, warm, clear Amharic conversation. Friendly, concise, medium pace."
                }]
            }]
        }],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": GEMINI_TTS_VOICE}]}
    }
    try:
        response = HTTP.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json=body,
            timeout=(15, 90),
        )
        if not response.ok:
            logging.warning("Gemini TTS failed with HTTP %s", response.status_code)
            return False
        encoded = response.json().get("output_audio", {}).get("data")
        if not encoded:
            logging.warning("Gemini TTS response had no audio data")
            return False
        wav_bytes = base64.b64decode(encoded)
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory(prefix="henok-voice-") as folder:
            wav_path = os.path.join(folder, "reply.wav")
            ogg_path = os.path.join(folder, "reply.ogg")
            with open(wav_path, "wb") as audio_file:
                audio_file.write(wav_bytes)
            proc = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", wav_path,
                 "-c:a", "libopus", "-b:a", "32k", "-vbr", "on",
                 "-application", "voip", ogg_path],
                capture_output=True, timeout=60,
            )
            if proc.returncode != 0 or not os.path.isfile(ogg_path):
                logging.warning("FFmpeg voice conversion failed")
                return False
            with open(ogg_path, "rb") as audio_file:
                audio_data = BytesIO(audio_file.read())
            audio_data.name = "henok_reply.ogg"
            bot.send_voice(chat_id, audio_data, timeout=60)
        return True
    except Exception as exc:
        logging.warning("Voice reply failed: %s", type(exc).__name__)
        return False


@bot.message_handler(content_types=["voice", "audio"])
def handle_voice(message):
    chat_id = message.chat.id
    audio_obj = message.voice or message.audio
    bot.send_chat_action(chat_id, "typing")
    try:
        f_info = bot.get_file(audio_obj.file_id)
        raw_bytes = bot.download_file(f_info.file_path)
    except Exception as e:
        safe_send(chat_id, f"ድምፁን ማውረድ አልተቻለም: {str(e)}")
        return
    if len(raw_bytes) > MAX_AUDIO_BYTES:
        safe_send(chat_id, "የድምፅ ፋይሉ በጣም ትልቅ ነው። እባክህ ከ14MB በታች የሆነ አጭር Voice Note ላክ።")
        return
    prompt = "ይህን የድምፅ መልዕክት በጥሞና አዳምጥ። በተናገረበት ቋንቋ ተረዳ፤ ከዚያ በአማርኛ አጭር እና ግልጽ ምላሽ ስጥ። አስፈላጊ ከሆነ ትራንስክሪፕትና የሚቀጥሉ ተግባራትን ጨምር።"
    mime_type = getattr(audio_obj, "mime_type", None) or ("audio/ogg" if message.voice else "audio/mpeg")
    ans = call_gemini_audio(raw_bytes, mime_type, prompt)
    if ans:
        add_history(chat_id, "user", "[Voice Note]")
        add_history(chat_id, "assistant", ans)
        safe_send(chat_id, f"🎙️ የድምፅ መልዕክት ውጤት፦\n\n{ans}")
        if not send_voice_reply(chat_id, ans):
            logging.info("Text reply sent; voice reply unavailable for this update")
    else:
        safe_send(chat_id, "ይቅርታ ሄኖክ፣ ድምፁን መረዳት አልተቻለም። እባክህ በድጋሚ ላክ።")

@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    t = (message.text or "").strip()
    if not t:
        return
    # ተጠቃሚው 'start' ብሎ ሲጽፍ እንኳን የሰላምታ መልዕክት እንዲሰጠው
    if t.lower() in ["start", "/start", "menu", "/menu"]:
        cmd_start(message)
        return
    if any(w in t.lower() for w in ["ፖስት", "post", "ይዘት አዘጋጅ"]):
        SESSIONS[chat_id] = {"pending": {"topic": "", "style": "", "platforms": []}}
        advance(chat_id)
        return
    bot.send_chat_action(chat_id, "typing")
    ans = reply_with_ai(chat_id, t)
    if ans:
        add_history(chat_id, "user", t)
        add_history(chat_id, "assistant", ans)
        safe_send(chat_id, ans)
    else:
        safe_send(chat_id, "ይቅርታ ሄኖክ፣ አሁን ምላሽ ማመንጨት አልተቻለም። እባክህ በድጋሚ ሞክር።")

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    chat_id = call.message.chat.id
    d = call.data or ""
    s = SESSIONS.setdefault(chat_id, {"pending": {}, "pkgs": {}})
    if d.startswith("t:"):
        s["pending"]["topic"] = TOPICS[d[2:]][1]
        advance(chat_id)
    elif d.startswith("s:"):
        s["pending"]["style"] = STYLES[d[2:]][1]
        advance(chat_id)
    elif d.startswith("p:"):
        s["pending"]["platforms"] = ["tg", "tt", "ig", "yt"] if d[2:] == "all" else [d[2:]]
        advance(chat_id)
    elif d.startswith("pub:"):
        pid = int(d.split(":")[1])
        pkg = s.get("pkgs", {}).get(pid)
        if pkg:
            try:
                bot.send_message(TARGET_CHANNEL_ID, pkg["post"])
                safe_send(chat_id, "✅ ፖስቱ ወደ Henok Graphics ተለጥፏል!")
            except Exception as e:
                safe_send(chat_id, f"መለጠፍ አልተቻለም: {str(e)}")
    elif d.startswith("can:"):
        safe_send(chat_id, "❌ ተሰርዟል።")

def update_worker():
    while True:
        update = UPDATE_QUEUE.get()
        try:
            bot.process_new_updates([update])
        except Exception:
            logging.exception("Telegram update worker failed")
        finally:
            UPDATE_QUEUE.task_done()


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    if not RENDER_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL is missing; set it in Render environment")
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthCheckHandler)
    threading.Thread(target=update_worker, name="telegram-update-worker", daemon=True).start()
    webhook_url = RENDER_URL + WEBHOOK_PATH
    try:
        # Remove any old webhook/pending updates before registering the single endpoint.
        bot.remove_webhook()
        time.sleep(1)
        ok = bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
        if not ok:
            raise RuntimeError("Telegram rejected setWebhook")
        logging.info("HENOK AI webhook registered: %s", webhook_url)
        server.serve_forever()
    except Exception:
        logging.exception("HENOK AI webhook server failed to start")
        raise
    finally:
        # Keep the webhook configured during graceful shutdown/redeploy.
        # The next instance will register the same endpoint on startup.
        server.server_close()
