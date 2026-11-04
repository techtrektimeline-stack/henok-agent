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
        self.wfile.write(b"Henok Autonomous AI Router is LIVE 24/7!")
    def log_message(self, format, *args):
        return

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_health_server, daemon=True).start()

# 2. ቁልፎች እና መለያዎች
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

# የ Henok Graphics ግሩፕ መለያ
TARGET_CHANNEL_ID = os.environ.get("TARGET_CHANNEL_ID", "-1003098019114")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# 3. የቦቱ የላቀ የአስተሳሰብ መመሪያ (Claude-Style Intelligence & System Persona)
SYSTEM_PROMPT = """
You are the personal AI executive director, creative strategist, and digital marketing partner to ሄኖክ (Henok Habtamu).
Always address him warmly and professionally as 'ሄኖክ'.

ስለ ሄኖክ ማወቅ ያለብህ ቁልፍ መረጃዎች:
- YouTube Channels: TechTrek Timeline (ቴክኖሎጂ፣ AI፣ ፈጠራ) እና Henok Travel (የኢትዮጵያ ጉዞና ቅርሶች)
- Telegram Community: Henok Graphics (የቪዲዮ ኤዲቲንግ፣ ግራፊክስ፣ ሞሽን፣ ፍሪላንሲንግ ማህበረሰብ)
- E-commerce & Digital Ventures: Amazon KDP፣ Ye-buna (የቡና)፣ Whop Viral Clipping Campaigns
- Digital Marketing Agency: Sekela Marketing (የሰከላ ማርኬቲንግ)

የአሰራርህ ወርቃማ ህጎች (Golden Operating Rules):
1. ብልህ ጥያቄዎችን መጠየቅ (Claude-Style Clarification):
   - ሄኖክ ያልተብራራ ወይም አጠቃላይ የሆነ ጥያቄ/ትዕዛዝ ሲሰጥህ (ለምሳሌ: "ግሩፑ ላይ የሆነ ነገር ፖስት አድርግ" ወይም "ስለ ኤዲቲንግ ጻፍ")፣ ዝም ብለህ አጠቃላይ የሆነ ባዶ ጽሑፍ አትደርድር!
   - ልክ እንደ ክላውድ (Claude) ጥልቅ የሆነ ጥያቄ በመጠየቅ ጀምር፦ የታለመው ታዳሚ ማን እንደሆነ፣ ዛሬ ትኩረት የተደረገበት ርዕስ (ለምሳሌ Premiere Pro፣ After Effects ወይስ CapCut?)፣ የሚፈለገው ድምጸ-ተውኔት ምን እንደሆነ 1 ወይም 2 ጥርት ያሉ ጥያቄዎችን ጠይቀው።

2. ከካንቫ እና ከዲዛይን ጋር የተጣመረ ይዘት (Post + Visual Idea):
   - ይዘት ወይም ፖስት ሲዘጋጅ ጽሑፍ ብቻ መሆን የለበትም። ሁልጊዜ አብሮ የሚሰራ የካንቫ (Canva) ወይም የግራፊክስ ዲዛይን ሃሳብ (የምስሉ ቀለም፣ በምስሉ ላይ የሚጻፈው አጭር Hook ጽሑፍ፣ እና የዲዛይን አቀማመጥ መመሪያ) አካትት።

3. የፈቃድ መጠየቅ ህግ (Human-in-the-Loop):
   - ፈቃድ (Approve Button) የሚጠየቀው ወደ ግሩፕ ወይም ቻናል በቀጥታ የሚለጠፍ የተጠናቀቀ ፖስት ሲዘጋጅ ብቻ ነው።
   - ለመደበኛ ውይይት፣ ለጥያቄዎች ወይም ሃሳብ ስትለዋወጡ ፈጽሞ የፈቃድ በተን (Approve Button) አታምጣ።

4. ቋንቋ:
   - ሁልጊዜ በተፈጥሯዊ፣ ግልጽ እና ማራኪ አማርኛ (ወይም በእንግሊዝኛ ሲጠይቅህ በእንግሊዝኛ) ምላሽ ስጥ።
"""

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
        r = requests.post(url, headers=headers, json=payload, timeout=40)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
    except Exception:
        pass
    return None

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

# 4. የማሰብና ሞዴሎችን የመምረጥ ብልህ ማዕከል (Claude 3.5 Sonnet First)
def autonomous_smart_router(user_prompt):
    text_lower = user_prompt.lower()
    
    # ኮዲንግና ቴክኒካል ስራዎች ከሆነ DeepSeek ይመረጣል
    coding_words = ["code", "python", "html", "css", "js", "api", "bug", "algorithm", "ኮድ", "ስሌት", "ሂሳብ", "ፕሮግራም"]
    if any(w in text_lower for w in coding_words):
        resp = call_openrouter(user_prompt, "deepseek/deepseek-chat")
        if not resp:
            resp = call_openrouter(user_prompt, "deepseek/deepseek-r1:free")
        if resp:
            return resp

    # ለስልት፣ ለጽሑፍ፣ ለማርኬቲንግ እና ለጥያቄ አፈታት በዋነኝነት Claude 3.5 Sonnet ይመረጣል
    resp = call_openrouter(user_prompt, "anthropic/claude-3.5-sonnet")
    if not resp:
        resp = call_openrouter(user_prompt, "deepseek/deepseek-chat")
    if resp:
        return resp
        
    return ask_gemini_text(user_prompt)

# 5. ሚዲያ (ድምፅ፣ ቪዲዮ፣ ፎቶ፣ ሰነድ) በ Gemini Flash
def ask_gemini_media(file_bytes, mime_type, caption_text=""):
    b64_data = base64.b64encode(file_bytes).decode('utf-8')
    parts = [
        {"text": f"{SYSTEM_PROMPT}\n\nሄኖክ የላከልህ የሚዲያ/የሰነድ ፋይል ነው። ተጨማሪ ማስታወሻ: {caption_text}\nፋይሉን በጥሞና ተመልክተህ ተንትነህ የሚፈለገውን ሙያዊ ስራ አቅርብ፦"},
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
        "👋 ሰላም ሄኖክ! እኔ ያንተ የግል AI ረዳትና የይዘት ስትራቴጂስት ነኝ።\n\n"
        "✨ **ምን ማድረግ እችላለሁ?**\n"
        "• በማንኛውም ርዕስ ላይ በድምፅ ወይም በጽሑፍ መወያየትና ማቀድ\n"
        "• ለ Henok Graphics፣ TechTrek እና ማህበራዊ ሚዲያዎችህ የሚሆኑ ምርጥ ጽሑፎችን ከዲዛይን ሃሳብ ጋር ማዘጋጀት\n"
        "• ወደ ግሩፕ ወይም ቻናል በቀጥታ እንዲለጠፍ ስታዝዘኝ ብቻ ፈቃድህን ጠይቄ መለጠፍ\n\n"
        "በምን እንጀምር?"
    )
    bot.reply_to(message, welcome_text)

# የድምፅ መልዕክት (Voice Note)
@bot.message_handler(content_types=['voice', 'audio'])
def handle_voice(message):
    bot.send_chat_action(message.chat.id, 'record_voice')
    try:
        file_info = bot.get_file(message.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)
        ai_resp = ask_gemini_media(downloaded, "audio/ogg", "የድምፅ መልዕክት")
        process_response(message, ai_resp)
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
        caption = message.caption or "ምስሉን ተንትነህ ሙያዊ ማብራሪያ ስጥ"
        ai_resp = ask_gemini_media(downloaded, "image/jpeg", caption)
        process_response(message, ai_resp)
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
        caption = message.caption or "ሰነዱን አንብበህ ተንትን"
        ai_resp = ask_gemini_media(downloaded, mime_type, caption)
        process_response(message, ai_resp)
    except Exception as e:
        bot.reply_to(message, f"ይቅርታ ሄኖክ፣ ሰነዱን ማንበብ አልቻልኩም: {str(e)}")

# ቪዲዮ
@bot.message_handler(content_types=['video'])
def handle_video(message):
    bot.send_chat_action(message.chat.id, 'upload_video')
    try:
        file_info = bot.get_file(message.video.file_id)
        downloaded = bot.download_file(file_info.file_path)
        caption = message.caption or "ይህን ቪዲዮ አይተህ ለክሊፒንግ የሚሆኑ ምርጥ 30 ሰከንድ ክፍሎችን ለይ"
        ai_resp = ask_gemini_media(downloaded, "video/mp4", caption)
        process_response(message, ai_resp)
    except Exception as e:
        bot.reply_to(message, f"ይቅርታ ሄኖክ፣ ቪዲዮውን መመልከት አልቻልኩም: {str(e)}")

# መደበኛ የጽሑፍ መልዕክት
@bot.message_handler(func=lambda message: True)
def handle_text(message):
    bot.send_chat_action(message.chat.id, 'typing')
    ai_resp = autonomous_smart_router(message.text)
    process_response(message, ai_resp)

# መልዕክቶችን የመለየትና ፖስት ሲሆን ብቻ ፈቃድ የመጠየቅ ብልህ አሰራር
def process_response(message, ai_resp):
    user_prompt = message.text or message.caption or ""
    prompt_lower = user_prompt.lower()
    
    # ሄኖክ ይዘት እንዲለጠፍ ወይም እንዲዘጋጅ ሲያዝዝ ብቻ የሚነቁ ቃላት
    posting_keywords = ["ፖስት አድርግ", "ለጥፍልኝ", "ግሩፑ ላይ ለጥፍ", "ቻናሉ ላይ ለጥፍ", "publish", "post to group", "post this"]
    is_posting_intent = any(w in prompt_lower for w in posting_keywords)
    
    # ቦቱ ጥያቄ እየጠየቀ ከሆነ (Clarification mode) ፈቃድ መጠየቅ አያስፈልግም
    is_asking_question = "?" in ai_resp or "ጥያቄ" in ai_resp or "ከመለጠፋችን በፊት" in ai_resp
    
    # ተራ ውይይት ወይም ቦቱ ገና ጥያቄ እየጠየቀ ከሆነ ያለምንም በተን በቀጥታ ይመልሳል
    if not is_posting_intent or is_asking_question:
        bot.reply_to(message, ai_resp)
        return
        
    # የተጠናቀቀ ፖስት ሲሆን ብቻ የፈቃድ መጠየቂያ በተን ያመጣል
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(
        InlineKeyboardButton("📢 አጽድቅና ግሩፕ ላይ ፖስት አድርግ", callback_data="publish_to_group"),
        InlineKeyboardButton("❌ ሰርዝ (አትለጥፈው)", callback_data="cancel_publish")
    )
    bot.reply_to(message, f"{ai_resp}\n\n━━━━━━━━━━━━━━━\nይህ ይዘት Henok Graphics ላይ ፖስት እንዲሆን ፈቅደሃል?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    current_text = call.message.text or ""
    base_text = current_text.split("━━━━━━━━━━━━━━━")[0].strip()
    
    if call.data == "publish_to_group":
        channel_target = TARGET_CHANNEL_ID or "-1003098019114"
        try:
            bot.send_message(channel_target, base_text)
            success_text = f"{base_text}\n\n━━━━━━━━━━━━━━━\n✅ **በሄኖክ ጸድቋል!**\n🎉 በቀጥታ ወደ **Henok Graphics** ግሩፕህ ተለጥፏል!"
            bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=success_text)
            bot.answer_callback_query(call.id, "በቀጥታ ግሩፕህ ላይ ተለጥፏል! 🎉")
        except Exception as e:
            error_text = f"{base_text}\n\n━━━━━━━━━━━━━━━\n⚠️ ወደ ግሩፕ ለመለጠፍ አልተቻለም: {str(e)}"
            bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=error_text)
            bot.answer_callback_query(call.id, "ስህተት ተፈጥሯል")
            
    elif call.data == "cancel_publish":
        cancel_text = f"{base_text}\n\n━━━━━━━━━━━━━━━\n❌ **በሄኖክ ተሰርዟል!**\n(ወደ ግሩፑ አልተለጠፈም)"
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=cancel_text)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "ተሰርዟል ❌")

print("🤖 Henok Autonomous Agent with Claude Strategy & Smart Intent Detection is running...")
bot.infinity_polling()
