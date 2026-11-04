import os
import base64
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ───────────────────────── Config ─────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
TARGET_CHANNEL_ID = os.environ.get("TARGET_CHANNEL_ID", "-1003098019114")
OWNER_ID = os.environ.get("OWNER_ID")  # Henok's Telegram user id (send /id to the bot to get it)

MAX_MEDIA_BYTES = 19 * 1024 * 1024  # Telegram Bot API download limit is 20MB

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)
SESSIONS = {}  # chat_id -> dict (in-memory; resets on Render restart)


# ───────────────────── Keep-alive server ─────────────────────
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

# ───────────────────────── Prompts ─────────────────────────
SYSTEM_PROMPT = """
You are the Executive AI Producer and strategic partner of ሄኖክ (Henok Habtamu), a video producer,
digital marketer and creative strategist in Addis Ababa.
Rules:
- Always reply in natural, fluent, modern Amharic as spoken by digital creatives in Addis Ababa.
  Never produce stiff, word-for-word translated Amharic. Keep common tech terms
  (Premiere, CapCut, Canva, hook, CTA, Reels) in English when that is how creators actually say them.
- Address him warmly as 'ሄኖክ'.
- Be concise, practical and energetic. No long preambles.
"""

TOPICS = {
    "edit": "የቪዲዮ ኤዲቲንግ ፈጣን Tips (Premiere/CapCut)",
    "free": "የፍሪላንሲንግ እና የደንበኛ አያያዝ ሚስጥሮች",
    "motion": "After Effects እና የሞሽን ግራፊክስ ዘዴዎች",
    "color": "የቀለም ቅንብር (Color Grading) እና የዲዛይን ህጎች",
    "ai": "አዳዲስ AI መሳሪያዎች ለቪዲዮ ፈጣሪዎች",
    "pick": "AUTO",
}
TOPIC_LABELS = {
    "edit": "🎬 ቪዲዮ ኤዲቲንግ Tips",
    "free": "💼 ፍሪላንሲንግ እና ደንበኛ",
    "motion": "⚡ After Effects / Motion",
    "color": "🎨 Color Grading እና ዲዛይን",
    "ai": "🤖 አዳዲስ AI Tools",
    "pick": "🎲 አንተ ምረጥልኝ",
}
STYLES = {
    "dark": ("🖤 Dark + Neon", "Dark Theme ከ Neon ሰማያዊና ወይንጠጅ ቀለማት ጋር"),
    "clean": ("⚪ Minimalist", "Clean Minimalist ከነጭና ጥቁር የጸዳ አቀማመጥ ጋር"),
    "bold": ("🔥 Bold Contrast", "Bold High Contrast ከደማቅ ቢጫና ጥቁር ቀለማት ጋር"),
}
PLATFORMS = {
    "tg": ("📢 Telegram", "Telegram channel post: medium length, emojis, clear CTA"),
    "tt": ("🎵 TikTok", "TikTok: caption under 150 chars, a 15-30s spoken script, trending-style hashtags"),
    "ig": ("📸 Instagram", "Instagram Reels/Post: punchy caption, line breaks, 8-12 hashtags"),
    "yt": ("▶️ YT Shorts", "YouTube Shorts: a title under 70 chars, a 30-45s script, short description"),
}


# ───────────────────────── AI layer ─────────────────────────
def call_openrouter(prompt):
    if not OPENROUTER_API_KEY:
        return None
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://henok-agent.onrender.com",
                "X-Title": "Henok Executive Agent",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=60,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        print("OpenRouter error:", r.status_code, r.text[:200])
    except Exception as e:
        print("OpenRouter exception:", e)
    return None


def call_gemini(parts, timeout=90):
    """parts: list of {"text": ...} or {"inline_data": {"mime_type":..., "data": b64}}"""
    if not GEMINI_API_KEY:
        return None
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": parts}],
    }
    try:
        r = requests.post(url, json=body, timeout=timeout)
        if r.status_code == 200:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        print("Gemini error:", r.status_code, r.text[:200])
    except Exception as e:
        print("Gemini exception:", e)
    return None


def generate_text(prompt):
    ans = call_openrouter(prompt) or call_gemini([{"text": prompt}])
    return ans or "ይቅርታ ሄኖክ፣ አሁን ምላሽ ማዘጋጀት አልቻልኩም። እባክህ በድጋሚ ሞክር።"


def download_telegram_file(file_id, size=None):
    if size and size > MAX_MEDIA_BYTES:
        return None
    info = bot.get_file(file_id)
    return bot.download_file(info.file_path)


# ───────────────────────── Helpers ─────────────────────────
def is_owner(user_id):
    return (not OWNER_ID) or str(user_id) == str(OWNER_ID)


def safe_send(chat_id, text, markup=None):
    """Telegram limit is 4096 chars; split and attach buttons to the last chunk."""
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for i, chunk in enumerate(chunks):
        bot.send_message(chat_id, chunk, reply_markup=markup if i == len(chunks) - 1 else None)


def main_menu():
    m = InlineKeyboardMarkup(row_width=1)
    m.add(
        InlineKeyboardButton("📝 አዲስ ፖስት ፍጠር", callback_data="new"),
        InlineKeyboardButton("🎙 የድምፅ መልዕክት / 🎞 ቪዲዮ ላክልኝ", callback_data="hint_media"),
    )
    return m


def topic_menu():
    m = InlineKeyboardMarkup(row_width=1)
    m.add(*[InlineKeyboardButton(TOPIC_LABELS[k], callback_data=f"t:{k}") for k in TOPICS])
    return m


def style_menu():
    m = InlineKeyboardMarkup(row_width=1)
    m.add(*[InlineKeyboardButton(v[0], callback_data=f"s:{k}") for k, v in STYLES.items()])
    return m


def platform_menu():
    m = InlineKeyboardMarkup(row_width=2)
    m.add(*[InlineKeyboardButton(v[0], callback_data=f"p:{k}") for k, v in PLATFORMS.items()])
    return m


def approve_menu(platform):
    label = "✅ አጽድቅና ግሩፕ ላይ ለጥፍ" if platform == "tg" else "✅ አጽድቅ — ለኮፒ አዘጋጅልኝ"
    m = InlineKeyboardMarkup(row_width=2)
    m.add(
        InlineKeyboardButton(label, callback_data="pub"),
        InlineKeyboardButton("🔄 ሌላ ስሪት", callback_data="regen"),
        InlineKeyboardButton("❌ ሰርዝ", callback_data="cancel"),
    )
    return m


def build_post_prompt(topic, style, platform_key):
    topic_line = (
        "ርዕሱን አንተ ምረጥ — ለቪዲዮ ኤዲተሮችና ዲዛይነሮች አሁን የሚጠቅም አዲስ ሃሳብ"
        if topic == "AUTO" else topic
    )
    return f"""
ለ Henok Graphics ማህበረሰብ (የቪዲዮ ኤዲተሮች፣ ሞሽን ግራፊክስ ባለሙያዎች፣ ዲዛይነሮች) ይዘት አዘጋጅ።
ርዕስ: {topic_line}
ፕላትፎርም: {PLATFORMS[platform_key][1]}
የዲዛይን ስታይል: {style}

ምላሽህን በትክክል በዚህ ቅርጸት ስጥ (ምልክቶቹን አትቀይር):

===POST===
(የመጨረሻው ለመለጠፍ ዝግጁ የሆነ ጽሑፍ: ማራኪ Hook፣ አጫጭር ተግባራዊ ነጥቦች፣ CTA፣ Hashtags)

===CANVA===
🎨 Color Palette: (3-4 ቀለማት ከ HEX ኮድ ጋር)
🔤 Overlay Hook: (በምስሉ ላይ የሚጻፍ ከ6 ቃላት ያልበለጠ ኃይለኛ ጽሑፍ)
🖼 Composition: (የምስሉ አቀማመጥ፣ የፊደል መጠን ቅደም ተከተል፣ ምን ከየት ይቀመጣል)
"""


def split_post(full):
    post, canva = full, ""
    if "===POST===" in full:
        full = full.split("===POST===", 1)[1]
        post = full
    if "===CANVA===" in full:
        post, canva = full.split("===CANVA===", 1)
    return post.strip(), canva.strip()


# ───────────────────────── Commands ─────────────────────────
@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(message, f"Your Telegram user id: {message.from_user.id}")


@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    if not is_owner(message.from_user.id):
        return
    bot.send_message(
        message.chat.id,
        "ሰላም ሄኖክ! 👋 ዛሬ ምን እንስራ? ከታች ምረጥ — መጻፍ አያስፈልግም።",
        reply_markup=main_menu(),
    )


# ───────────────────── Voice / audio notes ─────────────────────
@bot.message_handler(content_types=["voice", "audio"])
def handle_voice(message):
    if not is_owner(message.from_user.id):
        return
    media = message.voice or message.audio
    bot.send_chat_action(message.chat.id, "typing")
    data = download_telegram_file(media.file_id, getattr(media, "file_size", None))
    if not data:
        bot.reply_to(message, "ሄኖክ፣ ይህ ፋይል ከ20MB በላይ ነው። አጠር አድርገህ ላክልኝ።")
        return
    mime = getattr(media, "mime_type", None) or "audio/ogg"
    prompt = (
        "ይህን የድምፅ መልዕክት አድምጥ እና እንዲህ አቅርብ:\n"
        "1. 📝 ሙሉ ጽሑፍ (transcript) — በተነገረው ቋንቋ\n"
        "2. ✅ የተግባር ዝርዝር (Action Items) — አጫጭር ነጥቦች\n"
        "3. 💡 አንድ የይዘት ሃሳብ ከዚህ ሊወጣ ይችላል"
    )
    result = call_gemini(
        [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}]
    )
    if not result:
        bot.reply_to(message, "ይቅርታ ሄኖክ፣ የድምፅ መልዕክቱን መተርጎም አልተቻለም። እባክህ በድጋሚ ላክ።")
        return
    SESSIONS[message.chat.id] = {"voice_summary": result[:1500]}
    m = InlineKeyboardMarkup(row_width=1)
    m.add(
        InlineKeyboardButton("📝 ከዚህ ሃሳብ ፖስት ፍጠር", callback_data="fromvoice"),
        InlineKeyboardButton("👍 ይበቃል", callback_data="cancel"),
    )
    safe_send(message.chat.id, result, m)


# ───────────────────── Photo / video analysis ─────────────────────
@bot.message_handler(content_types=["photo", "video", "video_note", "document"])
def handle_media(message):
    if not is_owner(message.from_user.id):
        return
    is_video = False
    if message.photo:
        item = message.photo[-1]
        mime = "image/jpeg"
    elif message.video or message.video_note:
        item = message.video or message.video_note
        mime = getattr(item, "mime_type", None) or "video/mp4"
        is_video = True
    else:
        item = message.document
        mime = item.mime_type or ""
        if not (mime.startswith("image/") or mime.startswith("video/")):
            bot.reply_to(message, "ሄኖክ፣ ፎቶ ወይም ቪዲዮ ብቻ ነው መተንተን የምችለው።")
            return
        is_video = mime.startswith("video/")

    bot.send_chat_action(message.chat.id, "typing")
    data = download_telegram_file(item.file_id, getattr(item, "file_size", None))
    if not data:
        bot.reply_to(
            message,
            "ሄኖክ፣ ፋይሉ ከ20MB በላይ ነው (የ Telegram Bot ገደብ)። አጭር ክፍል ቆርጠህ ላክልኝ።",
        )
        return

    if is_video:
        prompt = (
            "ይህን ቪዲዮ ተመልከት። ለ Whop clipping፣ TikTok እና Reels የሚሆኑ ቫይራል የመሆን እድል ያላቸውን "
            "ከ30 ሰከንድ ያልበለጡ 3 ክፍሎች ምረጥ። ለእያንዳንዱ:\n"
            "⏱ Start–End (mm:ss)\n🪝 ለምን ቫይራል ይሆናል (አንድ መስመር)\n"
            "📝 የሚመከር Caption/Hook\n🎯 የሚመጥን ፕላትፎርም\n"
            "በመጨረሻ ለ Opening Hook የሚሆን ምክር ጨምር።"
        )
    else:
        prompt = (
            "ይህን ምስል ተመልከት እና ለማህበራዊ ሚዲያ ፖስት እንዴት መጠቀም እንደሚቻል ተንትን: "
            "ጠንካራ ጎኖች፣ ማሻሻያዎች፣ የሚመጥን Hook ጽሑፍ፣ እና Canva ላይ እንዴት ማስዋብ እንደሚቻል።"
        )
    result = call_gemini(
        [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}],
        timeout=120,
    )
    safe_send(message.chat.id, result or "ይቅርታ ሄኖክ፣ ፋይሉን መተንተን አልተቻለም።")


# ───────────────────────── Text ─────────────────────────
@bot.message_handler(content_types=["text"])
def handle_text(message):
    if not is_owner(message.from_user.id):
        return
    t = message.text.lower()
    if any(w in t for w in ["ፖስት", "post", "ለጥፍ", "graphics", "ግሩፕ", "menu"]):
        bot.send_message(message.chat.id, "ሄኖክ፣ ምን እንስራ?", reply_markup=main_menu())
        return
    bot.send_chat_action(message.chat.id, "typing")
    safe_send(message.chat.id, generate_text(message.text))


# ───────────────────────── Callbacks ─────────────────────────
def edit(call, text, markup=None):
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id, message_id=call.message.message_id,
            text=text, reply_markup=markup,
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup)


def run_generation(call, chat_id):
    s = SESSIONS.get(chat_id, {})
    if not all(k in s for k in ("topic", "style", "platform")):
        edit(call, "ሄኖክ፣ ክፍለ ጊዜው ተቋርጧል። እባክህ /menu ን ተጫን።")
        return
    edit(call, "⏳ ሄኖክ፣ ፖስቱንና የ Canva መመሪያውን እያዘጋጀሁ ነው...")
    full = generate_text(build_post_prompt(s["topic"], s["style"], s["platform"]))
    post, canva = split_post(full)
    s["post"], s["canva"] = post, canva
    preview = f"📢 ፖስት:\n\n{post}\n\n━━━━━━━━━━━━━━━\n🎨 Canva Blueprint:\n\n{canva}"
    safe_send(chat_id, preview, approve_menu(s["platform"]))


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "ፈቃድ የለህም")
        return
    chat_id = call.message.chat.id
    data = call.data
    s = SESSIONS.setdefault(chat_id, {})

    if data == "new":
        edit(call, "ለዛሬው ፖስት የትኛው ርዕስ ይሻላል?", topic_menu())

    elif data == "hint_media":
        edit(call, "🎙 የድምፅ መልዕክት፣ 🖼 ፎቶ ወይም 🎞 ቪዲዮ (እስከ 20MB) በቀጥታ እዚህ ላክልኝ — እኔ እተነትንልሃለሁ።", main_menu())

    elif data == "fromvoice":
        s["topic"] = "ከዚህ የድምፅ መልዕክት የወጣ ሃሳብ:\n" + s.get("voice_summary", "")
        edit(call, "✅ ሃሳቡ ተመርጧል። የዲዛይን ስታይል ምረጥ፦", style_menu())

    elif data.startswith("t:"):
        key = data[2:]
        if key not in TOPICS:
            return
        s["topic"] = TOPICS[key]
        label = TOPIC_LABELS[key]
        edit(call, f"✅ ርዕስ: {label}\n\nአሁን የዲዛይን ስታይል ምረጥ፦", style_menu())

    elif data.startswith("s:"):
        key = data[2:]
        if key not in STYLES:
            return
        s["style"] = STYLES[key][1]
        edit(call, f"✅ ስታይል: {STYLES[key][0]}\n\nለየትኛው ፕላትፎርም ላዘጋጅ?", platform_menu())

    elif data.startswith("p:"):
        key = data[2:]
        if key not in PLATFORMS:
            return
        s["platform"] = key
        bot.answer_callback_query(call.id)
        run_generation(call, chat_id)
        return

    elif data == "regen":
        bot.answer_callback_query(call.id)
        run_generation(call, chat_id)
        return

    elif data == "pub":
        post = s.get("post")
        if not post:
            edit(call, "ሄኖክ፣ የሚለጠፍ ይዘት አላገኘሁም። /menu ን ተጫን።")
        elif s.get("platform") == "tg":
            try:
                bot.send_message(TARGET_CHANNEL_ID, post)
                edit(call, "🎉 በሄኖክ ጸድቋል! ወደ Henok Graphics ተለጥፏል ✅")
            except Exception as e:
                edit(call, f"⚠️ ወደ ግሩፕ መለጠፍ አልተቻለም: {e}")
        else:
            name = PLATFORMS[s["platform"]][0]
            edit(call, f"✅ ጸድቋል! {name} ላይ ለመለጠፍ ከታች ያለውን ኮፒ አድርግ (ራስ-ሰር ልጥፍ በቀጣይ ደረጃ ይመጣል):")
            bot.send_message(chat_id, post)

    elif data == "cancel":
        SESSIONS.pop(chat_id, None)
        edit(call, "❌ ተሰርዟል። /menu ን ተጫን።")

    bot.answer_callback_query(call.id)


if __name__ == "__main__":
    if not OWNER_ID:
        print("WARNING: OWNER_ID is not set — anyone can use this bot. Send /id to the bot and set OWNER_ID.")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
