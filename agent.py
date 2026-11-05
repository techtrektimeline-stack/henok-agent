# -*- coding: utf-8 -*-
import os
import re
import json
import time
import base64
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID", "-1003098019114").strip()
OWNER_ID = os.getenv("OWNER_ID", "").strip()

MAX_BYTES = 18 * 1024 * 1024
TELEGRAM_TEXT_LIMIT = 3900
HISTORY_LIMIT = 20

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)
HTTP = requests.Session()
STATE_LOCK = threading.RLock()

HISTORY = {}
SESSIONS = {}
PACKAGE_COUNTER = 0

# 24/7 Render Keep-Alive Server
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"HENOK AI is running 24/7")

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthCheckHandler)
        server.serve_forever()
    except Exception:
        pass

threading.Thread(target=start_health_server, daemon=True).start()

SYSTEM_PROMPT = """
You are HENOK AI, personal executive AI partner to ሄኖክ (Henok Habtamu) in Addis Ababa, Ethiopia.
- YouTube: TechTrek Timeline & Henok Travel
- Telegram Community: Henok Graphics
- Business: Sekela Marketing, Amazon KDP, Whop Viral Clipping, Ye-buna

Rules:
1. Always reply in clear, modern, natural Amharic as spoken by digital creators in Addis Ababa.
2. For audio/voice: transcribe, summarize, and list actionable items.
3. For videos: identify 3 viral 30-second clips with timestamps.
4. Address him warmly as 'ሄኖክ'.
"""

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

# ፈጣን እና አስተማማኝ OpenRouter ጥሪ (ምንም 503 የሌለበት)
def call_openrouter(messages):
    if not OPENROUTER_API_KEY:
        return None
    models = [
        "deepseek/deepseek-chat",
        "meta-llama/llama-3.3-70b-instruct:free",
        "google/gemini-2.0-flash-exp:free"
    ]
    for m in models:
        try:
            r = HTTP.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={"model": m, "messages": messages},
                timeout=25,
            )
            if r.status_code == 200:
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                if content:
                    return str(content).strip()
        except Exception:
            continue
    return None

# Gemini ጥሪ (ፈጣን 15 ሰከንድ Timeout ያለው)
def call_gemini(contents, timeout=20):
    if not GEMINI_API_KEY:
        return None
    models = ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-3.8-flash"]
    for m in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": contents,
        }
        try:
            r = HTTP.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=timeout)
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

def reply_with_ai(chat_id, user_text, extra_parts=None):
    hist = HISTORY.get(chat_id, [])[-6:]
    
    # ጽሑፍ ከሆነ OpenRouter በጣም ፈጣን ስለሆነ ቅድሚያ ይሰጠዋል
    if not extra_parts:
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + hist + [{"role": "user", "content": user_text}]
        ans_op = call_openrouter(msgs)
        if ans_op:
            return ans_op

    # ድምፅና ቪዲዮ ከሆነ ደግሞ በ Gemini ይሄዳል
    gemini_contents = [{"role": "user" if h["role"] == "user" else "model", "parts": [{"text": h["text"]}]} for h in hist]
    cur_parts = list(extra_parts or [])
    if user_text:
        cur_parts.append({"text": user_text})
    gemini_contents.append({"role": "user", "parts": cur_parts})
    
    ans = call_gemini(gemini_contents, timeout=35)
    if ans:
        return ans
        
    return None

# ═════════════════════════ Handlers ═════════════════════════

@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    safe_send(message.chat.id, "ሰላም ሄኖክ! 👋 እኔ HENOK AI ነኝ።\n\n• የድምፅ መልዕክት (Voice) ላክልኝ — አዳምጬ እተረጉማለሁ።\n• ቪዲዮ ወይም ፎቶ ላክልኝ — ተመልክቼ እተነትናለሁ።\n• መደበኛ ጥያቄዎችን በማንኛውም ጊዜ ጠይቀኝ።")

# 🎙️ የድምፅ መልዕክት (Voice Note)
@bot.message_handler(content_types=["voice", "audio"])
def handle_voice(message):
    chat_id = message.chat.id
    audio_obj = message.voice or message.audio
    
    wait_msg = bot.send_message(chat_id, "⏳ ሄኖክ፣ ድምፅህን እያዳመጥኩ ነው፤ ጥቂት ሰከንዶች ጠብቀኝ...")
    bot.send_chat_action(chat_id, "typing")
    
    try:
        f_info = bot.get_file(audio_obj.file_id)
        raw_bytes = bot.download_file(f_info.file_path)
    except Exception as e:
        bot.edit_message_text(f"ድምፁን ማውረድ አልተቻለም: {str(e)}", chat_id=chat_id, message_id=wait_msg.message_id)
        return
        
    prompt = "ይህን የድምፅ መልዕክት በጥሞና አዳምጥ።\n1. 📝 TRANSCRIPT: ቃል በቃል ጻፍ።\n2. 📌 SUMMARY: ዋናውን ሀሳብ አጠቃል።\n3. ⚡ ACTION ITEMS: ከድምፁ የሚወጡ ተግባራትን ዘርዝር።"
    extra_parts = [
        {"text": prompt},
        {"inlineData": {"mimeType": "audio/ogg", "data": base64.b64encode(raw_bytes).decode("ascii")}}
    ]
    ans = reply_with_ai(chat_id, "", extra_parts)
    try:
        bot.delete_message(chat_id, wait_msg.message_id)
    except Exception:
        pass
    if ans:
        add_history(chat_id, "user", "[Voice Note]")
        add_history(chat_id, "assistant", ans)
        safe_send(chat_id, f"🎙️ **የድምፅ መልዕክት ውጤት፦**\n\n{ans}")
    else:
        safe_send(chat_id, "ይቅርታ ሄኖክ፣ የድምፅ ሰርቨሩ ተጨናንቋል፤ እባክህ ትንሽ ቆይተህ ድምፁን በድጋሚ ላክ።")

# 🎬 ቪዲዮ (Video)
@bot.message_handler(content_types=["video", "video_note"])
def handle_video(message):
    chat_id = message.chat.id
    item = message.video or message.video_note
    if item.file_size and item.file_size > MAX_BYTES:
        safe_send(chat_id, "ቪዲዮው ከ 18MB በላይ ነው፤ እባክህ አሳጥረህ ላክልኝ።")
        return

    wait_msg = bot.send_message(chat_id, "⏳ ሄኖክ፣ ቪዲዮውን እየተመለከትኩ ነው...")
    bot.send_chat_action(chat_id, "upload_video")
    try:
        f_info = bot.get_file(item.file_id)
        data = bot.download_file(f_info.file_path)
    except Exception as e:
        bot.edit_message_text(f"ቪዲዮውን ማውረድ አልተቻለም: {str(e)}", chat_id=chat_id, message_id=wait_msg.message_id)
        return

    prompt = "ይህን ቪዲዮ ተመልከት፦\n1. ዋና ጭብጡ\n2. ለ Whop/TikTok የሚሆኑ 3 ምርጥ የ 30 ሰከንድ ቫይራል ክፍሎች (Start-End ጊዜና Hook)\n3. የማሻሻያ ምክር"
    extra_parts = [
        {"text": prompt},
        {"inlineData": {"mimeType": getattr(item, "mime_type", "video/mp4") or "video/mp4", "data": base64.b64encode(data).decode("ascii")}}
    ]
    ans = reply_with_ai(chat_id, "", extra_parts)
    try:
        bot.delete_message(chat_id, wait_msg.message_id)
    except Exception:
        pass
    if ans:
        safe_send(chat_id, f"🎬 **የቪዲዮ ትንተና ውጤት፦**\n\n{ans}")
    else:
        safe_send(chat_id, "ይቅርታ ሄኖክ፣ ቪዲዮውን መተንተን አልተቻለም።")

# ✍️ መደበኛ ጽሑፍ
@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    t = (message.text or "").strip()
    if not t:
        return
    if t.lower() in ["start", "/start", "menu", "/menu"]:
        cmd_start(message)
        return
    bot.send_chat_action(chat_id, "typing")
    ans = reply_with_ai(chat_id, t)
    if ans:
        add_history(chat_id, "user", t)
        add_history(chat_id, "assistant", ans)
        safe_send(chat_id, ans)
    else:
        safe_send(chat_id, "ይቅርታ ሄኖክ፣ አሁን ምላሽ ማመንጨት አልተቻለም፤ እባክህ በድጋሚ ሞክር።")

if __name__ == "__main__":
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            if "409" in str(e):
                time.sleep(5)
            else:
                time.sleep(3)
