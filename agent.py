# -*- coding: utf-8 -*-
"""HENOK AI — Render-hosted Telegram assistant with dynamic model discovery and OpenRouter fallback."""
import os
import time
import base64
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("henok-agent")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip()
TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID", "-1003098019114").strip()
OWNER_ID = os.getenv("OWNER_ID", "511440586").strip()
MAX_BYTES = 18 * 1024 * 1024
TEXT_LIMIT = 3900
HTTP = requests.Session()

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None, threaded=True)

# Google በሚፈቅደው መሰረት ሞዴሎችን በራስ-ሰር ለማግኘት
AVAILABLE_MODELS = []

SYSTEM_PROMPT = """
You are HENOK AI, Henok's personal AI assistant and creative research partner.
LANGUAGE & TONE:
- Reply in fluent, natural Amharic when the user writes Amharic; otherwise use the user's language.
- Be warm, confident, energetic, engaging and human.
- Be concise for everyday questions and structured for complex tasks.
CREATOR CONTEXT:
- Henok Travel focuses on cinematic travel storytelling, local history, culture and landscapes.
- Telegram Community: Henok Graphics.
- Long-form scripts should have a strong hook, narrative arc, scene-by-scene narration and B-roll directions.
"""

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"HENOK AI is running 24/7"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        log.info("health: " + fmt, *args)

def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", port)

def send_text(chat_id, text, reply_markup=None, reply_to=None):
    text = str(text or "").strip() or "ምላሽ ማመንጨት አልተቻለም።"
    chunks = [text[i:i + TEXT_LIMIT] for i in range(0, len(text), TEXT_LIMIT)]
    sent_messages = []
    for i, chunk in enumerate(chunks):
        sent_messages.append(bot.send_message(
            chat_id, chunk,
            reply_markup=reply_markup if i == len(chunks) - 1 else None,
            reply_to_message_id=reply_to,
        ))
    return sent_messages

# ከአንተ የ Google AI Studio ቁልፍ ጋር የሚሰሩ ሞዴሎችን በራስ-ሰር ማግኘት
def get_available_gemini_models():
    global AVAILABLE_MODELS
    if AVAILABLE_MODELS:
        return AVAILABLE_MODELS
    if not GEMINI_API_KEY:
        return []
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        r = HTTP.get(url, timeout=15)
        if r.ok:
            data = r.json()
            models = []
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods:
                    name = m["name"].replace("models/", "")
                    models.append(name)
            if models:
                AVAILABLE_MODELS = models
                log.info("Discovered available Google Gemini models: %s", models[:5])
                return AVAILABLE_MODELS
    except Exception as e:
        log.warning("Could not list models: %s", e)
    return ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-3.8-flash"]

def call_openrouter(messages):
    if not OPENROUTER_API_KEY:
        return None
    models = ["deepseek/deepseek-chat", "meta-llama/llama-3.3-70b-instruct:free", "google/gemini-2.0-flash-exp:free"]
    for m in models:
        try:
            r = HTTP.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                json={"model": m, "messages": messages},
                timeout=25,
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                if content:
                    return str(content).strip()
        except Exception:
            continue
    return None

def gemini_generate(parts, timeout=45, use_web_search=False, system_instruction=None):
    if not GEMINI_API_KEY:
        log.error('Gemini request blocked: GEMINI_API_KEY is not configured')
        return None

    discovered = get_available_gemini_models()
    candidate_models = []
    if GEMINI_MODEL:
        candidate_models.append(GEMINI_MODEL)
    candidate_models.extend(discovered)
    candidate_models.extend(["gemini-1.5-flash", "gemini-2.0-flash", "gemini-3.8-flash"])
    models = list(dict.fromkeys(candidate_models))

    payload = {
        'systemInstruction': {'parts': [{'text': system_instruction or SYSTEM_PROMPT}]},
        'contents': [{'role': 'user', 'parts': parts}]
    }
    if use_web_search:
        payload['tools'] = [{'google_search': {}}]

    for model in models:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}'
        try:
            response = HTTP.post(
                url,
                headers={'Content-Type': 'application/json'},
                json=payload,
                timeout=(10, timeout)
            )
            if response.ok:
                data = response.json()
                candidates = data.get('candidates') or []
                if candidates:
                    candidate = candidates[0]
                    parts_out = candidate.get('content', {}).get('parts', [])
                    answer = '\n'.join(p.get('text', '') for p in parts_out if p.get('text')).strip()
                    if answer:
                        if use_web_search:
                            meta = candidate.get('groundingMetadata') or {}
                            chunks = meta.get('groundingChunks') or []
                            sources, seen = [], set()
                            for chunk in chunks:
                                web = chunk.get('web') or {}
                                uri = web.get('uri')
                                title = web.get('title') or uri
                                if uri and uri not in seen:
                                    seen.add(uri)
                                    sources.append(f'- {title}: {uri}')
                            if sources:
                                answer += '\n\nSources:\n' + '\n'.join(sources[:6])
                        return answer
            if response.status_code == 404:
                continue
        except Exception:
            continue
    return None

def reply_ai(chat_id, text, extra_parts=None, use_search=False):
    if extra_parts:
        return gemini_generate(extra_parts, timeout=60)

    # ጽሑፍ ከሆነ መጀመሪያ በ Google Gemini ይሞክራል
    ans = gemini_generate([{"text": text}], timeout=30, use_web_search=use_search)
    if ans:
        return ans

    # ጎግል ቢዘገይ ወይም 404 ቢጥል ወዲያውኑ ወደ OpenRouter ይሸጋገራል
    log.info("Gemini failed, falling back to OpenRouter")
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}]
    return call_openrouter(msgs)

def is_owner(message):
    return str(message.from_user.id) == OWNER_ID

def download_telegram_file(file_id):
    info = bot.get_file(file_id)
    if getattr(info, "file_size", 0) and info.file_size > MAX_BYTES:
        raise ValueError("ፋይሉ ከ18MB በላይ ነው።")
    data = bot.download_file(info.file_path)
    return data

@bot.message_handler(commands=["start", "menu"])
def start_command(message):
    send_text(
        message.chat.id,
        "ሰላም ሄኖክ! 👋 HENOK AI ዝግጁ ነው።\n"
        "• በድምፅ ወይም በጽሑፍ የፈለከውን ጠይቀኝ።\n"
        "• ሞዴሎችን ለመፈተሽ /ai_status በለኝ።\n"
        "• ወደ Henok Graphics ይዘት ለመለጠፍ /publish ተጠቀም።"
    )

@bot.message_handler(commands=["ai_status"])
def ai_status_command(message):
    if not is_owner(message):
        return
    bot.send_chat_action(message.chat.id, "typing")
    models = get_available_gemini_models()
    if models:
        send_text(
            message.chat.id,
            f"✅ AI STATUS: OK\n\nለአንተ ቁልፍ የተፈቀዱ Google Gemini ሞዴሎች፦\n" +
            "\n".join(f"• {m}" for m in models[:10]) +
            "\n\n👉 ቦቱ ከእነዚህ ውስጥ አንዱን በራስ-ሰር መርጦ ይሰራል!"
        )
    else:
        send_text(message.chat.id, "⚠️ Google AI Studio ሞዴሎችን ማግኘት አልተቻለም፤ GEMINI_API_KEYን ፈትሽ።")

@bot.message_handler(commands=["id"])
def id_command(message):
    send_text(message.chat.id, f"Telegram ID: {message.from_user.id}")

@bot.message_handler(commands=["publish"])
def publish_command(message):
    if not is_owner(message):
        return
    content = (message.text or "").partition(" ")[2].strip()
    if not content:
        send_text(message.chat.id, "አጠቃቀም፦ /publish ከዚያ የሚለጠፈውን ይዘት ጻፍ።")
        return
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton("✅ አጽድቅ", callback_data="publish:yes"),
        InlineKeyboardButton("❌ ተው", callback_data="publish:no"),
    )
    sent = send_text(message.chat.id, "ይህን ወደ Henok Graphics ልላክ?\n\n" + content, reply_markup=keyboard)
    if sent:
        PENDING_POSTS[sent[-1].message_id] = content

PENDING_POSTS = {}

@bot.callback_query_handler(func=lambda call: call.data in ("publish:yes", "publish:no"))
def approval_callback(call):
    if str(call.from_user.id) != OWNER_ID:
        bot.answer_callback_query(call.id, "ፈቃድ የለህም።", show_alert=True)
        return
    content = PENDING_POSTS.pop(call.message.message_id, None)
    if not content:
        bot.answer_callback_query(call.id, "ይህ ይዘት አልተገኘም።", show_alert=True)
        return
    if call.data == "publish:no":
        bot.answer_callback_query(call.id, "ተሰርዟል።")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        send_text(call.message.chat.id, "❌ ይዘቱ ተወ።")
        return
    try:
        bot.send_message(TARGET_CHANNEL_ID, content)
        bot.answer_callback_query(call.id, "ተሳክቷል።")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        send_text(call.message.chat.id, "✅ ይዘቱ ወደ Henok Graphics ተልኳል።")
    except Exception:
        bot.answer_callback_query(call.id, "መላክ አልተቻለም።", show_alert=True)

@bot.message_handler(content_types=["text"])
def text_handler(message):
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    bot.send_chat_action(message.chat.id, "typing")
    use_search = any(w in text.lower() for w in ["ዛሬ", "ወቅታዊ", "አዲስ ዜና", "ትሬንድ", "today", "news"])
    answer = reply_ai(message.chat.id, text, use_search=use_search)
    if answer:
        send_text(message.chat.id, answer, reply_to=message.message_id)
    else:
        send_text(message.chat.id, "የAI አገልግሎቱ አሁን ምላሽ አልሰጠም። እባክህ በድጋሚ ሞክር።", reply_to=message.message_id)

@bot.message_handler(content_types=["voice", "audio"])
def voice_handler(message):
    chat_id = message.chat.id
    bot.send_chat_action(chat_id, "typing")
    audio = message.voice or message.audio
    try:
        raw = download_telegram_file(audio.file_id)
        mime = getattr(audio, "mime_type", None) or ("audio/ogg" if message.voice else "audio/mpeg")
        prompt = "Listen to this audio. Transcribe it accurately in Amharic, then answer the speaker's actual request."
        parts = [
            {"text": prompt},
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(raw).decode("ascii")}},
        ]
        answer = reply_ai(chat_id, "", extra_parts=parts)
        if answer:
            send_text(chat_id, answer, reply_to=message.message_id)
        else:
            send_text(chat_id, "ድምፁን ማቀናበር አልተሳካም። እባክህ በድጋሚ ላክ።", reply_to=message.message_id)
    except Exception as exc:
        send_text(chat_id, "የድምፅ ፋይሉን ማውረድ አልተቻለም።")

if __name__ == "__main__":
    start_health_server()
    try:
        bot.remove_webhook()
    except Exception:
        pass

    log.info("Starting HENOK AI long polling")
    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=25,
                skip_pending=True,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as e:
            if "409" in str(e):
                time.sleep(5)
            else:
                time.sleep(3)
