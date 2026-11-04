# -*- coding: utf-8 -*-
import os
import re
import json
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, List, Dict, Any

import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

# እውነተኛውና ትክክለኛ የ Google Gemini ሞዴል
GEMINI_MODEL = "gemini-1.5-flash"
OPENROUTER_MODEL = "google/gemini-2.0-flash-exp:free"

TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID", "-1003098019114").strip()
OWNER_ID = os.getenv("OWNER_ID", "").strip()

MAX_AUDIO_BYTES = 14 * 1024 * 1024
TELEGRAM_TEXT_LIMIT = 3900
HISTORY_LIMIT = 20

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)
HTTP = requests.Session()
STATE_LOCK = threading.RLock()

HISTORY: Dict[int, List[Dict[str, str]]] = {}
SESSIONS: Dict[int, Dict[str, Any]] = {}
PACKAGE_COUNTER = 0

# Render እንዳይዘጋ የሚረዳ ሰርቨር
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

def is_owner(user_id):
    return bool(not OWNER_ID or str(user_id) == OWNER_ID)

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
    models = ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-1.5-pro"]
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
    prompt = "ይህን የድምፅ መልዕክት በጥሞና አዳምጥ። 1. TRANSCRIPT (ቃል በቃል) 2. SUMMARY (ማጠቃለያ) 3. ACTION ITEMS (ተግባራት) አቅርብ።"
    extra_parts = [
        {"text": prompt},
        {"inlineData": {"mimeType": "audio/ogg", "data": __import__("base64").b64encode(raw_bytes).decode("ascii")}}
    ]
    ans = reply_with_ai(chat_id, "", extra_parts)
    if ans:
        add_history(chat_id, "user", "[Voice Note]")
        add_history(chat_id, "assistant", ans)
        safe_send(chat_id, f"🎙️ **የድምፅ መልዕክት ውጤት፦**\n\n{ans}")
    else:
        safe_send(chat_id, "ይቅርታ ሄኖክ፣ ድምፁን መረዳት አልተቻለም። እባክህ በድጋሚ ላክ።")

@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    t = (message.text or "").strip()
    if not t:
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

if __name__ == "__main__":
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            if "409" in str(e):
                time.sleep(5)
            else:
                time.sleep(3)
