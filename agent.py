import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import json
import base64
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# 1. Render ሰርቨር 24/7 Live ሆኖ እንዲቆይ የሚረዳ አነስተኛ ዌብ ሰርቨር (Port 10000)
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Henok Interactive AI Router is LIVE 24/7!")
    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_health_server, daemon=True).start()

# 2. ቁልፎች
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
TARGET_CHANNEL_ID = os.environ.get("TARGET_CHANNEL_ID", "-1003098019114")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# ጊዜያዊ ምርጫዎችን ለማስታወስ
USER_SESSIONS = {}

SYSTEM_PROMPT = """
You are the Executive AI Strategic Partner and Lead Producer to ሄኖክ (Henok Habtamu).
Henok runs:
- YouTube: TechTrek Timeline & Henok Travel
- Community: Henok Graphics (Video editing, Motion, Design)
- Business: Sekela Marketing, Amazon KDP, Ye-buna

Crucial Language Instructions:
- Always speak in natural, professional, persuasive Amharic as spoken by modern digital creators in Addis Ababa.
- NEVER use literal machine translations.
- Address him respectfully and warmly as 'ሄኖክ'.
"""

def call_openrouter(prompt):
    if not OPENROUTER_API_KEY:
        return None
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://henok-agent.onrender.com",
        "X-Title": "Henok Executive Agent"
    }
    payload = {
        "model": "anthropic/claude-3.5-sonnet",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
    except Exception:
        pass
    return None

def ask_gemini_fallback(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": f"{SYSTEM_PROMPT}\n\n{prompt}"}]}]}
    try:
        res = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=25)
        if res.status_code == 200:
            return res.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        pass
    return "ይቅርታ ሄኖክ፣ ምላሽ ለማዘጋጀት አልተቻለም፤ እባክህ በድጋሚ ሞክር።"

def generate_ai_text(prompt):
    ans = call_openrouter(prompt)
    if ans:
        return ans
    return ask_gemini_fallback(prompt)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "👋 ሰላም ሄኖክ! እኔ ያንተ የግል AI ስትራቴጂስት ነኝ።\n\n"
        "⚡ **ፈጣን አሰራር (Quick Actions):**\n"
        "• ለ Henok Graphics ፖስት ለማዘጋጀት በቀላሉ **'ፖስት'** ወይም **'post'** በለኝ፤ አማራጮችን በበተን አቀርብልሃለሁ!\n"
        "• መደበኛ ጥያቄዎችን በጽሑፍ ወይም በድምፅ መጠየቅ ትችላለህ።"
    )
    bot.reply_to(message, welcome_text)

# ፖስት የማዘጋጀት ፈጣን አማራጮች (Buttons)
def show_topic_options(message):
    markup = InlineKeyboardMarkup()
    markup.row_width = 1
    markup.add(
        InlineKeyboardButton("🎬 የቪዲዮ ኤዲቲንግ ፈጣን Tips (Premiere/CapCut)", callback_data="top_editing"),
        InlineKeyboardButton("💼 የፍሪላንሲንግ & የደንበኛ አያያዝ ሚስጥሮች", callback_data="top_freelance"),
        InlineKeyboardButton("⚡ After Effects & የሞሽን ግራፊክስ ዘዴዎች", callback_data="top_motion"),
        InlineKeyboardButton("🎨 የቀለም ቅንብር (Color Grading) እና የዲዛይን ህጎች", callback_data="top_color")
    )
    bot.reply_to(message, "ሄኖክ፣ ለዛሬው የ Henok Graphics ፖስት የትኛው ርዕስ ይሻላል? (ከታች ምረጥ)፦", reply_markup=markup)

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    text_lower = message.text.lower()
    if any(w in text_lower for w in ["ፖስት", "post", "ለጥፍ", "graphics", "ግሩፕ"]):
        show_topic_options(message)
    else:
        bot.send_chat_action(message.chat.id, 'typing')
        resp = generate_ai_text(message.text)
        bot.reply_to(message, resp)

# የበተን ምርጫዎች አሰራር (Callback Handler)
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data

    topics = {
        "top_editing": "የቪዲዮ ኤዲቲንግ ጠቃሚ አቋራጭ እና ፈጣን Tips",
        "top_freelance": "በግራፊክስ እና በቪዲዮ ኤዲቲንግ የውጭ ደንበኞችን የማግኘት ዘዴ",
        "top_motion": "በ After Effects የሚሰሩ ሳቢ የሞሽን ግራፊክስ ሚስጥሮች",
        "top_color": "ለጀማሪ እና ለመካከለኛ ባለሙያዎች የቀለም ቅንብር (Color Grading) ህጎች"
    }

    if data in topics:
        USER_SESSIONS[chat_id] = {"topic": topics[data]}
        markup = InlineKeyboardMarkup()
        markup.row_width = 1
        markup.add(
            InlineKeyboardButton("🖤 Dark Theme & Neon (ዘመናዊ የቴክኖሎጂ ዲዛይን)", callback_data="sty_dark"),
            InlineKeyboardButton("⚪ Minimalist Clean (ቀለል ያለ ንጹህ ዲዛይን)", callback_data="sty_clean"),
            InlineKeyboardButton("🔥 Bold & High Contrast (ዓይን የሚስብ ከፍተኛ ጉልበት ያለው)", callback_data="sty_bold")
        )
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=f"✅ የተመረጠው ርዕስ: **{topics[data]}**\n\nአሁን ደግሞ የካንቫ (Canva) የዲዛይን ከለር ስታይል ምረጥ፦",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)

    elif data in ["sty_dark", "sty_clean", "sty_bold"]:
        styles = {
            "sty_dark": "Dark Theme ከ Neon ሰማያዊና ወይንጠጅ ቀለማት ጋር",
            "sty_clean": "Clean Minimalist ከነጭና ጥቁር የጸዳ አቀማመጥ ጋር",
            "sty_bold": "Bold High Contrast ከደማቅ ቢጫና ጥቁር ቀለማት ጋር"
        }
        chosen_style = styles[data]
        topic = USER_SESSIONS.get(chat_id, {}).get("topic", "የቪዲዮ ኤዲቲንግ እና ግራፊክስ")
        
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="⏳ ሄኖክ፣ ምርጡን የፖስት ጽሑፍና የካንቫ ዲዛይን መመሪያ በ Claude እያዘጋጀሁ ነው...")

        prompt = f"""
ለቴሌግራም 'Henok Graphics' ግሩፕ የሚሆን ምርጥ ፖስት አዘጋጅ።
ርዕስ: {topic}
የዲዛይን ስታይል: {chosen_style}

የሚከተሉትን 2 ክፍሎች ብቻ በግልጽ አቅርብ:
1. 📢 የፖስቱ ጽሑፍ (ለቴሌግራም):
- በጣም የሚስብ Hook
- አጫጭር ተግባራዊ ነጥቦች (Bullet points)
- Call to Action (CTA) እና አግባብ የሆኑ Hashtags

2. 🎨 የካንቫ (Canva) ዲዛይን መመሪያ:
- የሚመከሩ ቀለማት (Color Palette)
- በምስሉ ላይ የሚጻፈው ዋና አጭር ጽሑፍ (Overlay Hook)
- የምስሉ አቀማመጥ (Visual Composition)
"""
        generated_post = generate_ai_text(prompt)
        USER_SESSIONS[chat_id]["final_text"] = generated_post

        # የማጽደቂያ በተን
        markup = InlineKeyboardMarkup()
        markup.row_width = 2
        markup.add(
            InlineKeyboardButton("📢 አጽድቅና ግሩፕ ላይ ፖስት አድርግ", callback_data="pub_confirm"),
            InlineKeyboardButton("❌ ሰርዝ", callback_data="pub_cancel")
        )
        bot.send_message(chat_id, f"{generated_post}\n\n━━━━━━━━━━━━━━━\nይህ ይዘት Henok Graphics ላይ ፖስት ይሁን?", reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif data == "pub_confirm":
        full_content = USER_SESSIONS.get(chat_id, {}).get("final_text", "")
        # የቴሌግራም ጽሑፉን ብቻ ቆርጦ ወደ ግሩፑ ለመለጠፍ
        post_only = full_content.split("2. 🎨")[0].replace("1. 📢 የፖስቱ ጽሑፍ (ለቴሌግራም):", "").strip()
        if not post_only:
            post_only = full_content
            
        try:
            bot.send_message(TARGET_CHANNEL_ID, post_only)
            bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="🎉 **በሄኖክ ጸድቋል!**\nበቀጥታ ወደ **Henok Graphics** ግሩፕህ ተለጥፏል! ✅")
            bot.answer_callback_query(call.id, "ተለጥፏል! 🎉")
        except Exception as e:
            bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=f"⚠️ ወደ ግሩፕ ለመለጠፍ አልተቻለም: {str(e)}")
            bot.answer_callback_query(call.id, "ስህተት")

    elif data == "pub_cancel":
        bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="❌ **ተሰርዟል!** (ወደ ግሩፑ አልተለጠፈም)")
        bot.answer_callback_query(call.id, "ተሰርዟል")

print("🤖 Henok Interactive Button-Driven Agent is running...")
bot.infinity_polling()
