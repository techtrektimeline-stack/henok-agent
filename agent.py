import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import json
import base64

import os
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
pending_actions = {}

SYSTEM_PROMPT = """
You are the personal AI executive assistant, video editor strategist, and digital marketing expert to Henok.
Always address the user as 'ሄኖክ' (Henok).
Henok manages multiple brands and channels:
- YouTube Channels: TechTrek Timeline (Technology & AI), Henok Travel (Ethiopian Travel & Heritage), and others.
- E-commerce & Monetization: Amazon KDP, Ye-buna (የቡና), Whop Viral Clipping Campaigns.
- Marketing & Ads: Sekela Marketing (የሰከላ ማርኬቲንግ), Meta/Facebook Ads.

Your Capabilities:
1. Multimodal Analysis: When Henok sends you an image, a PDF document, an audio note, or a video file, carefully inspect and analyze the content.
2. For Video & Clipping: Pinpoint exact viral moments/timestamps (e.g., best 30s clips), provide catchy hooks, write platform-ready captions, and extract hashtags.
3. For PDF/Docs: Summarize, analyze campaign criteria (e.g. Whop requirements), or extract key action items.
4. Social Media Posting: Draft tailored posts for Telegram, TikTok, Instagram, and YouTube.

Always respond respectfully, practically, and creatively in Amharic (or English when requested), calling him 'ሄኖክ'.
"""

def call_gemini_api(parts):
    models = [
        "models/gemini-flash-latest",
        "models/gemini-flash-lite-latest",
        "models/gemini-pro-latest"
    ]
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": parts}]
    }
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent?key={GEMINI_API_KEY}"
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=40)
            if response.status_code == 200:
                data = response.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            continue
    return "ይቅርታ ሄኖክ፣ ምላሽ ለመስጠት አልተቻለም፤ እባክህ ትንሽ ቆይተህ እንደገና ሞክር።"

def ask_gemini_text(user_prompt):
    parts = [{"text": f"{SYSTEM_PROMPT}\n\nየሄኖክ ጥያቄ/ትዕዛዝ: {user_prompt}"}]
    return call_gemini_api(parts)

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
    return call_gemini_api(parts)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "👋 ሰላም ሄኖክ! እኔ ያንተ የግል AI ረዳት ነኝ።\n\n"
        "አሁን በሚከተሉት መንገዶች በሙሉ ማዘዝ ትችላለህ፦\n"
        "• **በጽሑፍ ወይም በድምፅ (Voice)** ጥያቄዎችህን መጠየቅ\n"
        "• **ፎቶ ወይም ምስል** በመላክ ማብራሪያና የፖስት ረቂቅ ማዘጋጀት\n"
        "• **ፒዲኤፍ (PDF)** በመላክ ትንታኔና ማጠቃለያ ማውጣት\n"
        "• **አጫጭር ቪዲዮ** በመላክ የክሊፒንግ (Whop/TikTok) ሃሳቦችን መለየት\n\n"
        "ስራውን አዘጋጅቼ ሁልጊዜ ያንተን ፍቃድ (Approve/Reject) እጠይቃለሁ!"
    )
    bot.reply_to(message, welcome_text)

@bot.message_handler(content_types=['voice', 'audio'])
def handle_voice(message):
    bot.send_chat_action(message.chat.id, 'record_voice')
    try:
        file_info = bot.get_file(message.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)
        ai_resp = ask_gemini_media(downloaded, "audio/ogg", "የድምፅ መልዕክት")
        send_with_buttons(message, f"🎙️ **ሄኖክ፣ ድምፅህን አዳምጬ የሚከተለውን አዘጋጅቻለሁ፦**\n\n{ai_resp}")
    except Exception as e:
        bot.reply_to(message, f"ይቅርታ ሄኖክ፣ ድምፅህን በማንበብ ላይ ስህተት ተፈጥሯል: {str(e)}")

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

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    bot.send_chat_action(message.chat.id, 'typing')
    ai_resp = ask_gemini_text(message.text)
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

print("🤖 Henok Super-Agent is running...")
bot.infinity_polling()
