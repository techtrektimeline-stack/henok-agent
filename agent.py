# -*- coding: utf-8 -*-
"""
HENOK AI — Instant Real-Time Telegram Assistant
Powered by Henok's Upgraded Gemini Engine
"""
import os
import io
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

# ያንተ የ Upgraded Gemini ሞዴል
GEMINI_MODEL = "gemini-1.5-flash"

TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID", "-1003098019114").strip()
OWNER_ID = os.getenv("OWNER_ID", "511440586").strip()

MAX_BYTES = 18 * 1024 * 1024
TELEGRAM_TEXT_LIMIT = 3900

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)
HTTP = requests.Session()

# 24/7 ሰርቨሩ ነቅቶ እንዲቆይ የሚረዳ
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"HENOK AI is LIVE 24/7")

def start_health_server():
    port = int(os.getenv("PORT", 10000))
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
2. Answer direct questions directly, instantly, and concisely.
3. For voice/audio: transcribe, summarize, and list actionable items.
4. For video: identify 3 viral 30-second clips with timestamps.
5. Address him warmly as 'ሄኖክ'.
"""

def safe_send(chat_id, text, markup=None):
    text = str(text or "").strip() or "ምላሽ አልተገኘም።"
    chunks = [text[i:i + TELEGRAM_TEXT_LIMIT] for i in range(0, len(text), TELEGRAM_TEXT_LIMIT)]
    for idx, c in enumerate(chunks):
        bot.send_message(chat_id, c, reply_markup=markup if idx == len(chunks) - 1 else None)

def call_gemini(contents, timeout=30):
    if not GEMINI_API_KEY:
        return None
    models = ["gemini-1.5-flash", "gemini-1.5-pro"]
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

# ═════════════════════════ Handlers ═════════════════════════

@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    safe_send(message.chat.id, "ሰላም ሄኖክ! 👋 እኔ ያንተ HENOK AI ረዳት ነኝ።\n\nበቀጥታ በድምፅ ወይም በጽሑፍ የፈለከውን ጠይቀኝ፤ በሰከንዶች ውስጥ እመልሳለሁ!")

@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(message, f"Telegram User ID: {message.from_user.id}")

# 🎙️ ድምፅ (Voice Note) በሰከንዶች ውስጥ
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

    prompt = "ይህን የድምፅ መልዕክት በጥሞና አዳምጥ።\n1. 📝 TRANSCRIPT: ቃል በቃል ጻፍ።\n2. 📌 SUMMARY: ዋናውን ሀሳብ አጠቃል።\n3. ⚡ ACTION ITEMS: ከድምፁ የሚወጡ ተግባራትን ዘርዝር።"
    extra_parts = [
        {"text": prompt},
        {"inlineData": {"mimeType": "audio/ogg", "data": base64.b64encode(raw_bytes).decode("ascii")}}
    ]
    ans = call_gemini([{"role": "user", "parts": extra_parts}], timeout=45)
    if ans:
        safe_send(chat_id, f"🎙️ **የድምፅ መልዕክት ውጤት፦**\n\n{ans}")
    else:
        safe_send(chat_id, "ይቅርታ ሄኖክ፣ ድምፁን ማቀናበር አልተቻለም፤ እባክህ በድጋሚ ሞክር።")

# ✍️ መደበኛ ጽሑፍ (Instant Text Reply)
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
    ans = call_gemini([{"role": "user", "parts": [{"text": t}]}], timeout=25)
    if ans:
        safe_send(chat_id, ans)
    else:
        safe_send(chat_id, "ይቅርታ ሄኖክ፣ አሁን ምላሽ ማመንጨት አልተቻለም። እባክህ በድጋሚ ሞክር።")

# ═════════════════════════ Start Bot ═════════════════════════
if __name__ == "__main__":
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            if "409" in str(e):
                time.sleep(5)
            else:
                time.sleep(3)
