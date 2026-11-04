import os
import io
import re
import json
import base64
import itertools
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ═════════════════════════ Config ═════════════════════════
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.0-flash-exp:free")
TARGET_CHANNEL_ID = os.environ.get("TARGET_CHANNEL_ID", "-1003098019114")
OWNER_ID = os.environ.get("OWNER_ID")

MAX_BYTES = 19 * 1024 * 1024  # Telegram limit ~20MB
TEXT_EXT = (".txt", ".md", ".csv", ".json", ".srt", ".vtt", ".html", ".xml", ".py", ".js", ".log")
YT_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w\-]+\S*")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)
SESSIONS = {}
HISTORY = {}
PKG_IDS = itertools.count(1)

# ═════════════════════ Keep-alive server ═════════════════════
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Henok Interactive AI Router is LIVE 24/7!")
    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

threading.Thread(target=start_health_server, daemon=True).start()

# ═════════════════════════ Prompts ═════════════════════════
SYSTEM_PROMPT = """
You are the Executive AI Strategic Partner and Lead Producer to ሄኖክ (Henok Habtamu), based in Addis Ababa, Ethiopia.
Henok's world:
- YouTube: "TechTrek Timeline" (AI & Emerging Tech) and "Henok Travel" (Cinematic Ethiopian Travel)
- Communities: "Henok Graphics" (Video Editors, Motion Designers, Freelancers)
- Business: Sekela Marketing, Amazon KDP, Whop Viral Clipping, Ye-buna

Rules:
1. Language: Always reply in natural, modern, engaging Amharic as spoken by digital creatives in Addis Ababa. Tech terms (Premiere, CapCut, Canva, Hook, CTA, Reel) stay in English.
2. Direct Answers: When asked a question, answer directly with practical advice. DO NOT show multiple choice menus unless explicitly asked to create a post.
3. Tone: Respectful, warm, strategic, energetic. Address him as 'ሄኖክ'.
"""

TOPICS = {
    "edit": ("🎬 ቪዲዮ ኤዲቲንግ Tips", "የቪዲዮ ኤዲቲንግ ፈጣን Tips (Premiere/CapCut)"),
    "free": ("💼 ፍሪላንሲንግ እና ደንበኛ", "የፍሪላንሲንግ እና የደንበኛ አያያዝ ሚስጥሮች"),
    "motion": ("⚡ After Effects / Motion", "After Effects እና የሞሽን ግራፊክስ ዘዴዎች"),
    "color": ("🎨 Color Grading እና ዲዛይን", "የቀለም ቅንብር (Color Grading) እና የዲዛይን ህጎች"),
    "ai": ("🤖 አዳዲስ AI Tools", "አዳዲስ AI መሳሪያዎች ለቪዲዮ ፈጣሪዎች"),
    "pick": ("🎲 አንተ ምረጥልኝ", "AUTO"),
}
STYLES = {
    "dark": ("🖤 Dark + Neon", "Dark Theme ከ Neon ሰማያዊና ወይንጠጅ ቀለማት ጋር"),
    "clean": ("⚪ Minimalist Clean", "Clean Minimalist ከነጭና ጥቁር የጸዳ አቀማመጥ ጋር"),
    "bold": ("🔥 Bold Contrast", "Bold High Contrast ከደማቅ ቢጫና ጥቁር ቀለማት ጋር"),
}
PLATFORMS = {
    "tg": ("📢 Telegram", "Telegram channel post: medium length, emojis, clear CTA"),
    "tt": ("🎵 TikTok", "TikTok: caption under 150 chars, 15-30s spoken script, hashtags"),
    "ig": ("📸 Instagram", "Instagram Reels/Post: punchy caption, line breaks, hashtags"),
    "yt": ("▶️ YT Shorts", "YouTube Shorts: title under 70 chars, 30-45s script, description"),
}

# ═════════════════════════ AI layer (Multi-Model Resilience) ═════════════════════════
def call_openrouter(messages):
    if not OPENROUTER_API_KEY:
        return None
    # 100% ነፃ የሆኑ ሞዴሎች ቅድሚያ ይሰጣቸዋል
    models_to_try = [
        "google/gemini-2.0-flash-exp:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "deepseek/deepseek-chat",
        "anthropic/claude-3.5-sonnet"
    ]
    for m in models_to_try:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://henok-agent.onrender.com",
                    "X-Title": "Henok Executive Agent",
                },
                json={"model": m, "messages": messages},
                timeout=30,
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                if content:
                    return content
        except Exception:
            continue
    return None

def call_gemini(contents, timeout=40):
    if not GEMINI_API_KEY:
        return None
    models = ["models/gemini-flash-latest", "models/gemini-pro-latest"]
    body = {"contents": contents}
    for m in models:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/{m}:generateContent?key={GEMINI_API_KEY}"
            r = requests.post(url, headers={"Content-Type": "application/json"}, json=body, timeout=timeout)
            if r.status_code == 200:
                j = r.json()
                cands = j.get("candidates") or []
                if cands:
                    return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
        except Exception:
            continue
    return None

def reply_with_ai(chat_id, user_text, extra_parts=None):
    hist = HISTORY.get(chat_id, [])[-10:]
    if not extra_parts:
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        msgs += [{"role": h["role"], "content": h["text"]} for h in hist]
        msgs.append({"role": "user", "content": user_text})
        out = call_openrouter(msgs)
        if out:
            return out

    # Gemini Fallback / Media Handler
    contents = []
    for h in hist:
        contents.append({"role": "user" if h["role"] == "user" else "model", "parts": [{"text": h["text"]}]})
    
    current_parts = (extra_parts or []) + [{"text": f"{SYSTEM_PROMPT}\n\n{user_text}"}]
    contents.append({"role": "user", "parts": current_parts})
    out = call_gemini(contents)
    return out

def safe_send(chat_id, text, markup=None):
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for i, c in enumerate(chunks):
        bot.send_message(chat_id, c, reply_markup=markup if i == len(chunks) - 1 else None)

def add_history(chat_id, role, text):
    h = HISTORY.setdefault(chat_id, [])
    h.append({"role": role, "text": text})
    del h[:-30]

# ═════════════════════════ Menus & Workflows ═════════════════════════
def topic_menu():
    m = InlineKeyboardMarkup(row_width=1)
    m.add(*[InlineKeyboardButton(v[0], callback_data=f"t:{k}") for k, v in TOPICS.items()])
    return m

def style_menu():
    m = InlineKeyboardMarkup(row_width=1)
    m.add(*[InlineKeyboardButton(v[0], callback_data=f"s:{k}") for k, v in STYLES.items()])
    return m

def platform_menu():
    m = InlineKeyboardMarkup(row_width=2)
    m.add(*[InlineKeyboardButton(v[0], callback_data=f"p:{k}") for k, v in PLATFORMS.items()])
    m.add(InlineKeyboardButton("🌐 ለሁሉም", callback_data="p:all"))
    return m

def package_menu(pid, platform):
    label = "✅ አጽድቅና ቴሌግራም ላይ ለጥፍ" if platform == "tg" else "✅ አጽድቅ"
    m = InlineKeyboardMarkup(row_width=2)
    m.add(
        InlineKeyboardButton(label, callback_data=f"pub:{pid}"),
        InlineKeyboardButton("🔄 ሌላ ስሪት", callback_data=f"regen:{pid}"),
        InlineKeyboardButton("❌ ሰርዝ", callback_data=f"cancel:{pid}"),
    )
    return m

def lock_answer(call, label):
    try:
        bot.edit_message_text(
            text=f"{call.message.text}\n\n✅ **ምርጫህ:** {label}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
    except Exception:
        pass

def make_package(chat_id, topic, style, platform_key):
    bot.send_chat_action(chat_id, "typing")
    prompt = f"""
ለ Henok Graphics ማህበረሰብ ይዘት አዘጋጅ።
ርዕስ: {topic}
ፕላትፎርም: {PLATFORMS[platform_key][1]}
የዲዛይን ስታይል: {style}

በዚህ ቅደም ተከተል አቅርብ:
===POST===
(የሚለጠፈው ሳቢ ጽሑፍ ከነ Hook፣ Bullet points፣ CTA እና Hashtags)

===CANVA===
🎨 Color Palette: (3-4 ቀለማት ከ HEX ኮድ ጋር)
🔤 Overlay Hook: (በምስሉ ላይ የሚጻፍ አጭር ኃይለኛ ጽሑፍ)
🖼 Composition: (የምስሉ አቀማመጥ መመሪያ)
"""
    full = reply_with_ai(chat_id, prompt)
    if not full:
        bot.send_message(chat_id, "ይቅርታ ሄኖክ፣ ፖስቱን ማመንጨት አልተቻለም፤ እባክህ በድጋሚ ሞክር።")
        return

    post, canva = full, ""
    if "===POST===" in full:
        post = full.split("===POST===", 1)[1]
    if "===CANVA===" in full:
        parts = post.split("===CANVA===", 1)
        post, canva = parts[0], parts[1]

    pid = next(PKG_IDS)
    SESSIONS.setdefault(chat_id, {"pkgs": {}})["pkgs"][pid] = {
        "topic": topic, "style": style, "platform": platform_key, "post": post.strip(), "canva": canva.strip(),
    }
    body = f"📢 **ፖስት ({PLATFORMS[platform_key][0]}):**\n\n{post.strip()}\n\n━━━━━━━━━━━━━━━\n🎨 **Canva Blueprint:**\n\n{canva.strip()}"
    safe_send(chat_id, body, package_menu(pid, platform_key))

def advance(chat_id):
    s = SESSIONS.setdefault(chat_id, {"pending": {}})
    p = s["pending"]
    if not p.get("topic"):
        bot.send_message(chat_id, "❓ ስለ ምን ርዕስ ልፍጠር?\n(ከታች ምረጥ ወይም በድምፅ/ጽሑፍ ንገረኝ)፦", reply_markup=topic_menu())
        return
    if not p.get("style"):
        bot.send_message(chat_id, "❓ የ Canva ዲዛይን ስታይል ምረጥ፦", reply_markup=style_menu())
        return
    if not p.get("platforms"):
        bot.send_message(chat_id, "❓ ለየትኛው ፕላትፎርም ላዘጋጅ?፦", reply_markup=platform_menu())
        return

    for pk in p.get("platforms"):
        make_package(chat_id, p["topic"], p["style"], pk)
    s["pending"] = {}

# ═════════════════════════ Handlers ═════════════════════════
@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(message, f"የአንተ Telegram ID: {message.from_user.id}")

@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "ሰላም ሄኖክ! 👋\n"
        "• መደበኛ ጥያቄዎችን በጽሑፍ ወይም በድምፅ መጠየቅ ትችላለህ (በቀጥታ እመልሳለሁ)።\n"
        "• ደረጃ በደረጃ ፖስት ለመስራት ከፈለግህ **'ፖስት'** ብለህ ጻፍልኝ!"
    )

@bot.message_handler(content_types=["voice"])
def handle_voice(message):
    chat_id = message.chat.id
    v = message.voice
    if v.file_size and v.file_size > MAX_BYTES:
        bot.send_message(chat_id, "ይህ የድምፅ ፋይል ከ 20MB በላይ ስለሆነ Telegram አያወርደውም፤ እባክህ አሳጥረው።")
        return
    bot.send_chat_action(chat_id, "typing")
    try:
        data = bot.download_file(bot.get_file(v.file_id).file_path)
    except Exception as e:
        bot.send_message(chat_id, f"ድምፁን ማውረድ አልተቻለም: {type(e).__name__}")
        return

    parts = [
        {"text": "ይህን የድምፅ መልዕክት በጥሞና አዳምጠህ በደንብ የተብራራ ማጠቃለያ እና የተግባር ዝርዝር (Action Items) አቅርብ።"},
        {"inline_data": {"mime_type": v.mime_type or "audio/ogg", "data": base64.b64encode(data).decode()}}
    ]
    out = call_gemini([{"role": "user", "parts": parts}])
    if out:
        add_history(chat_id, "user", "[የድምፅ መልዕክት]")
        add_history(chat_id, "assistant", out)
        safe_send(chat_id, f"🎙 **የድምፅህ ማጠቃለያና Action Items:**\n\n{out}")
    else:
        bot.send_message(chat_id, "ድምፁን መረዳት አልተቻለም፤ እባክህ ትንሽ ቀረብ ብለህ በድጋሚ ላክ።")

@bot.message_handler(content_types=["video", "video_note"])
def handle_video(message):
    chat_id = message.chat.id
    item = message.video or message.video_note
    if item.file_size and item.file_size > MAX_BYTES:
        bot.send_message(chat_id, "ቪዲዮው ከ 20MB በላይ ነው (የቴሌግራም ገደብ)፤ የ YouTube ሊንኩን ላክልኝ ወይም አሳጥረው።")
        return
    bot.send_chat_action(chat_id, "upload_video")
    try:
        data = bot.download_file(bot.get_file(item.file_id).file_path)
        parts = [
            {"text": "ይህን ቪዲዮ (ምስሉንም ድምጹንም) ሙሉ በሙሉ ተመልከት።\n1. ዋና ጭብጡ\n2. ለ Whop/TikTok የሚሆኑ 3 ምርጥ የ 30 ሰከንድ ቫይራል ክሊፖች (Start-End ጊዜና Hook)\n3. የማሻሻያ ምክር"},
            {"inline_data": {"mime_type": getattr(item, "mime_type", None) or "video/mp4", "data": base64.b64encode(data).decode()}}
        ]
        out = call_gemini([{"role": "user", "parts": parts}])
        if out:
            safe_send(chat_id, out)
        else:
            bot.send_message(chat_id, "ቪዲዮውን መተንተን አልተቻለም።")
    except Exception as e:
        bot.send_message(chat_id, f"ስህተት ተፈጠረ: {str(e)}")

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    chat_id = message.chat.id
    item = message.photo[-1]
    bot.send_chat_action(chat_id, "typing")
    try:
        data = bot.download_file(bot.get_file(item.file_id).file_path)
        parts = [
            {"text": "ይህን ምስል በጥንቃቄ መርምር፦ በውስጡ ያሉ ጽሑፎች፣ ጥራቱ፣ እና ለ Canva ዲዛይን ማሻሻያ የሚሆኑ ነጥቦች።"},
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(data).decode()}}
        ]
        out = call_gemini([{"role": "user", "parts": parts}])
        if out:
            safe_send(chat_id, out)
        else:
            bot.send_message(chat_id, "ምስሉን መመልከት አልተቻለም።")
    except Exception as e:
        bot.send_message(chat_id, f"ስህተት: {str(e)}")

@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    text = message.text.strip()
    text_lower = text.lower()

    # ፖስት ማዘጋጀት ሲፈልግ ብቻ በበተን ይጀምራል
    if any(w in text_lower for w in ["ፖስት", "post", "ይዘት አዘጋጅ", "ፖስት አድርግ"]):
        SESSIONS[chat_id] = {"pending": {"topic": "", "style": "", "platforms": []}}
        advance(chat_id)
        return

    # መደበኛ ጥያቄ ከሆነ በቀጥታ ያለምንም በተን ይመልሳል
    bot.send_chat_action(chat_id, "typing")
    resp = reply_with_ai(chat_id, text)
    if resp:
        add_history(chat_id, "user", text)
        add_history(chat_id, "assistant", resp)
        safe_send(chat_id, resp)
    else:
        bot.send_message(chat_id, "ይቅርታ ሄኖክ፣ ምላሽ ለመስጠት አልተቻለም፤ እባክህ ትንሽ ቆይተህ እንደገና ሞክር።")

# ═════════════════════════ Callbacks ═════════════════════════
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    data = call.data
    s = SESSIONS.setdefault(chat_id, {"pending": {}, "pkgs": {}})
    bot.answer_callback_query(call.id)

    if data.startswith("t:"):
        key = data[2:]
        label, topic = TOPICS[key]
        s["pending"]["topic"] = topic
        lock_answer(call, label)
        advance(chat_id)

    elif data.startswith("s:"):
        key = data[2:]
        label, style = STYLES[key]
        s["pending"]["style"] = style
        lock_answer(call, label)
        advance(chat_id)

    elif data.startswith("p:"):
        key = data[2:]
        if key == "all":
            s["pending"]["platforms"] = ["tg", "tt", "ig", "yt"]
            lock_answer(call, "🌐 ለሁሉም")
        else:
            s["pending"]["platforms"] = [key]
            lock_answer(call, PLATFORMS[key][0])
        advance(chat_id)

    elif data.startswith("pub:"):
        pid = int(data.split(":")[1])
        pkg = s.get("pkgs", {}).get(pid)
        if pkg:
            try:
                bot.send_message(TARGET_CHANNEL_ID, pkg["post"])
                bot.send_message(chat_id, "🎉 **በሄኖክ ጸድቋል!**\nበቀጥታ ወደ **Henok Graphics** ግሩፕህ ተለጥፏል! ✅")
            except Exception as e:
                bot.send_message(chat_id, f"⚠️ ወደ ግሩፕ ለመለጠፍ አልተቻለም: {str(e)}")

    elif data.startswith("cancel:"):
        bot.send_message(chat_id, "❌ ተሰርዟል።")

    elif data.startswith("regen:"):
        pid = int(data.split(":")[1])
        pkg = s.get("pkgs", {}).get(pid)
        if pkg:
            make_package(chat_id, pkg["topic"], pkg["style"], pkg["platform"])

print("🤖 Henok Resilient Autonomous Agent is running 24/7...")
bot.infinity_polling(skip_pending=True)
