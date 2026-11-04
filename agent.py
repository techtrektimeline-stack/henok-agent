import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import json
import base64
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# 1. Render ሰርቨር 24/7 Live ሆኖ እንዲቆይ የሚረዳ አነስተኛ ዌብ ሰርቨር
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Henok Autonomous AI Router is LIVE 24/7!")
    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_health_server, daemon=True).start()

# 2. ቁልፎች (Environment Variables)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
pending_actions = {}

SYSTEM_PROMPT = """
You are the personal AI executive assistant, video editor strategist, and digital marketing expert to Henok.
Always address the user as 'ሄኖክ' (Henok).
Henok manages multiple brands and channels:
- YouTube Channels: TechTrek Timeline (Technology & AI), Henok Travel (Ethiopian Travel & Heritage), and others.
- E-commerce & Monetization: Amazon KDP, Ye-buna (የቡና), Whop Viral Clipping Campaigns.
- Marketing & Ads: Sekela Marketing (የሰከላ ማርኬቲንግ), Meta/Facebook Ads.

Your Mission:
Deliver the highest quality, practical, and polished output. Be creative, persuasive, and structured.
Always respond in Amharic (or English when requested), calling him 'ሄኖክ'.
"""

# 3. የ OpenRouter ሞዴሎችን የመጥሪያ ፈንክሽን
def call_openrouter(user_prompt, model_name):
    if not OPENROUTER_API_KEY:
        return None
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://henok-agent.onrender.com",
        "X-Title": "Henok Autonomous Agent"
    }
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ]
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=35)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
    except Exception:
        pass
    return None

# 4. የ Gemini ጽሑፍ መጥሪያ ፈንክሽን (Fallback)
def ask_gemini_text(user_prompt):
    models = ["models/gemini-flash-latest", "models/gemini-flash-lite-latest", "models/gemini-pro-latest"]
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": f"{SYSTEM_PROMPT}\n\nየሄኖክ ጥያቄ/ትዕዛዝ: {user_prompt}"}]}]}
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent?key={GEMINI_API_KEY}"
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=35)
            if res.status_code == 200:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            continue
    return "ይቅርታ ሄኖክ፣ ምላሽ ለመስጠት አልተቻለም፤ እባክህ ትንሽ ቆይተህ እንደገና ሞክር።"

# 5. ራሱን ችሎ የተሻለውን AI የሚመርጠው ብልህ ሞተር (Autonomous Smart Router)
def autonomous_smart_router(user_prompt):
    text_lower = user_prompt.lower()
    
    # ሀ. ለኮዲንግ፣ ለቴክኒክ፣ ለስሌት ወይም አመክንዮ -> DeepSeek R1 / V3 ይመረጣል
    coding_words = ["code", "python", "html", "css", "js", "api", "bug", "algorithm", "ኮድ", "ስሌት", "ሂሳብ", "ፕሮግራም"]
    if any(w in text_lower for w in coding_words):
        resp = call_openrouter(user_prompt, "deepseek/deepseek-chat")
        if not resp:
            resp = call_openrouter(user_prompt, "deepseek/deepseek-r1:free")
        if resp:
            return f"🤖 **[በ DeepSeek የተዘጋጀ]**\n\n{resp}"
            
    # ለ. ለፈጠራ ጽሑፍ፣ ለቪዲዮ ስክሪፕት፣ ለማርኬቲንግ እና ለስቶሪቴሊንግ -> Claude 3.5 Sonnet ይመረጣል
    creative_words = ["script", "hook", "story", "tiktok", "reels", "youtube", "proposal", "caption", "ስክሪፕት", "ርዕስ", "ማስታወቂያ", "ፕሮፖዛል", "ይዘት", "ታሪክ", "ካፕሽን"]
    if any(w in text_lower for w in creative_words):
        resp = call_openrouter(user_prompt, "anthropic/claude-3.5-sonnet")
        if not resp:
            resp = call_openrouter(user_prompt, "deepseek/deepseek-chat")
        if resp:
            return f"✨ **[በ Claude 3.5 የተዘጋጀ]**\n\n{resp}"
            
    # ሐ. ለአጠቃላይ ጥያቄዎች እና ስትራቴጂ -> መጀመሪያ Claude/DeepSeek ከዚያ Gemini
    resp = call_openrouter(user_prompt, "anthropic/claude-3.5-sonnet")
    if not resp:
        resp = call_openrouter(user_prompt, "deepseek/deepseek-chat")
    if resp:
        return f"💡 **[በ AI ኤጀንት የተዘጋጀ]**\n\n{resp}"
        
    # መ. ሁልጊዜ ክፍት የሆነው Gemini Flash ያዘጋጃል
    return f"⚡ **[በ Gemini የተዘጋጀ]**\n\n{ask_gemini_text(user_prompt)}"

# 6. ለፎቶ፣ ድምፅ፣ ፒዲኤፍ እና ቪዲዮ (Gemini Multimodal)
def ask_gemini_media(file_bytes, mime_type, caption_text=""):
    b64_data = base64.b64encode(file_bytes).decode('utf-8')
    parts = [
        {"text": f"{SYSTEM_PROMPT}\n\nሄኖክ የላከልህ የሚዲያ/የሰነድ ፋይል ነው። ተጨማሪ ማስታወሻ: {caption_text}\nፋይሉን በጥሞና ተመልክተህ ተንትነህ የሚፈለገውን ስራ አቅርብ፦"},
        {
            "inlineData": {
                "mimeType": mime_type,
                "data": b64_data
            }
        }
    ]
    models = ["models/gemini-flash-latest", "models/gemini-flash-lite-latest"]
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": parts}]}
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent?key={GEMINI_API_KEY}"
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=40)
            if response.status_code == 200:
                return response.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            continue
    return "ይቅርታ ሄኖክ፣ ፋይሉን መተንተን አልተቻለም፤ እባክህ በድጋሚ ሞክር።"

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "👋 ሰላም ሄኖክ! እኔ ያንተ ራሱን የቻለ ራስ-ገዝ AI ረዳት (Autonomous AI Agent) ነኝ።\n\n"
        "🧠 **አሰራሬ፦**\n"
        "አንተ ምንም አይነት ሞዴል መምረጥ አይጠበቅብህም! የላክክልኝን ስራ እኔ ራሴ መርምሬ፦\n"
        "• ለፈጠራ ጽሑፍና ለቪዲዮ ስክሪፕቶች → **Claude 3.5 Sonnet**\n"
        "• ለኮዲንግ እና ለጥልቅ ስሌት → **DeepSeek V3 / R1**\n"
        "• ለቪዲዮ፣ ለድምፅ፣ ለፎቶ እና ለፒዲኤፍ → **Gemini Flash**\n"
        "በራሴ መርጬና አወዳድሬ ምርጡን ውጤት አቀርብልሃለሁ!\n\n"
        "ስራውን አዘጋጅቼ ሁልጊዜ ያንተን ፍቃድ (Approve/Reject) እጠይቃለሁ!"
    )
    bot.reply_to(message, welcome_text)

# የድምፅ መልዕክት
@bot.message_handler(content_types=['voice', 'audio'])
def handle_voice(message):
    bot.send_chat_action(message.chat.id, 'record_voice')
    try:
        file_info = bot.get_file(message.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)
        ai_resp = ask_gemini_media(downloaded, "audio/ogg", "የድምፅ መልዕክት")
        send_with_buttons(message, f"🎙️ **ሄኖክ፣ ድምፅህን አዳምጬ የሚከተለውን አዘጋጅቻለሁ፦**\n\n{ai_resp}")
    except Exception as e:
        bot.reply_to(message, f"ይቅርታ ሄኖክ፣ ድምፅህን ማንበብ አልቻልኩም: {str(e)}")

# ፎቶ እና ምስል
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    bot.send_chat_action(message.chat.id, 'upload_photo')
    try:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
        caption = message.caption or "ምስሉን ተንትነህ ለማህበራዊ ሚዲያ የሚሆን ይዘት አዘጋጅ"
        ai_resp = ask_gemini_media(downloaded, "image/jpeg", caption)
        send_with_buttons(message, f"🖼️ **ሄኖክ፣ ምስሉን ተመልክቼ የሚከተለውን አዘጋጅቻለሁ፦**\n\n{ai_resp}")
    except Exception as e:
        bot.reply_to(message, f"ይቅርታ ሄኖክ፣ ምስሉን መመልከት አልቻልኩም: {str(e)}")

# ፒዲኤፍ እና ሌሎች ሰነዶች
@bot.message_handler(content_types=['document'])
def handle_document(message):
    bot.send_chat_action(message.chat.id, 'upload_document')
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        mime_type = message.document.mime_type or "application/pdf"
        caption = message.caption or "ሰነዱን አንብበህ ዋና ዋና ነጥቦችንና የይዘት ሃሳቦችን አውጣ"
        ai_resp = ask_gemini_media(downloaded, mime_type, caption)
        send_with_buttons(message, f"📄 **ሄኖክ፣ ሰነዱን አንብቤ የሚከተለውን አዘጋጅቻለሁ፦**\n\n{ai_resp}")
    except Exception as e:
        bot.reply_to(message, f"ይቅርታ ሄኖክ፣ ሰነዱን ማንበብ አልቻልኩም: {str(e)}")

# ቪዲዮ (ለክሊፒንግ ትንታኔ)
@bot.message_handler(content_types=['video'])
def handle_video(message):
    bot.send_chat_action(message.chat.id, 'upload_video')
    try:
        file_info = bot.get_file(message.video.file_id)
        downloaded = bot.download_file(file_info.file_path)
        caption = message.caption or "ይህን ቪዲዮ አይተህ ለክሊፒንግ የሚሆኑ ምርጥ 30 ሰከንድ ክፍሎችን (Timestamps) እና ማራኪ Hooks ለይተህ አውጣልኝ"
        ai_resp = ask_gemini_media(downloaded, "video/mp4", caption)
        send_with_buttons(message, f"🎬 **ሄኖክ፣ ቪዲዮውን ተመልክቼ የሚከተሉትን የክሊፒንግ ሃሳቦች አውጥቻለሁ፦**\n\n{ai_resp}")
    except Exception as e:
        bot.reply_to(message, f"ይቅርታ ሄኖክ፣ ቪዲዮውን መመልከት አልቻልኩም: {str(e)}")

# መደበኛ የጽሑፍ መልዕክት (ራሱ በብልሃት መርጦ የሚያዘጋጅ)
@bot.message_handler(func=lambda message: True)
def handle_text(message):
    bot.send_chat_action(message.chat.id, 'typing')
    ai_resp = autonomous_smart_router(message.text)
    send_with_buttons(message, ai_resp)

def send_with_buttons(message, content):
    action_id = f"act_{message.message_id}"
    pending_actions[action_id] = {
        "content": content,
        "chat_id": message.chat.id
    }
    markup = InlineKeyboardMarkup()
    markup.row_width = 3
    markup.add(
        InlineKeyboardButton("✅ አጽድቅ (Approve)", callback_data=f"approve_{action_id}"),
        InlineKeyboardButton("✍️ አሻሽለው (Revise)", callback_data=f"revise_{action_id}"),
        InlineKeyboardButton("❌ ሰርዝ (Reject)", callback_data=f"reject_{action_id}")
    )
    bot.reply_to(message, f"{content}\n\n━━━━━━━━━━━━━━━\nይህ ስራ ይፈቀድልኝ?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    data = call.data
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    
    if data.startswith("approve_"):
        action_id = data.replace("approve_", "")
        action = pending_actions.get(action_id)
        if action:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"{action['content']}\n\n━━━━━━━━━━━━━━━\n✅ **በሄኖክ ጸድቋል!**\n(ስራው በተሳካ ሁኔታ ጸድቋል)",
                parse_mode="Markdown"
            )
            bot.answer_callback_query(call.id, "ስራው ጸድቋል! ✅")
            
    elif data.startswith("revise_"):
        bot.answer_callback_query(call.id, "አስተያየትህን ጻፍልኝ")
        bot.send_message(chat_id, "ሄኖክ፣ ምን እንዲስተካከል ትፈልጋለህ? በጽሑፍ ወይም በድምፅ ላክልኝና አሻሽለዋለሁ።")
        
    elif data.startswith("reject_"):
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="❌ **ስራው ተሰርዟል!**\nምንም እርምጃ አልተወሰደም።",
            parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id, "ተሰርዟል ❌")

print("🤖 Henok Autonomous AI Router is running 24/7 on Cloud...")
bot.infinity_polling()
