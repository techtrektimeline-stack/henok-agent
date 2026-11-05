# -*- coding: utf-8 -*-
"""HENOK AI — Render-hosted Telegram assistant (single long-polling worker)."""
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID", "-1003098019114").strip()
OWNER_ID = os.getenv("OWNER_ID", "511440586").strip()
MAX_BYTES = 18 * 1024 * 1024
TEXT_LIMIT = 3900
HTTP = requests.Session()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
if not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY is missing; bot will start but AI replies will fail")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None, threaded=True)

SYSTEM_PROMPT = """
You are HENOK AI, Henok's personal AI assistant and creative research partner.
LANGUAGE & TONE:
- Reply in fluent, natural Amharic when the user writes Amharic; otherwise use the user's language.
- Be warm, confident, energetic, engaging and human, without sounding robotic or exaggerating.
- Be concise for everyday questions and structured for complex tasks.
ACCURACY:
- Never invent facts, statistics, quotes, sources, dates or personal experiences.
- Distinguish verified facts, estimates and uncertainty.
- For current events, trends, prices, policies and recent developments, use web search when available and provide source links.
CREATOR CONTEXT:
- The only owner-confirmed account in the Account Registry is Henok Travel.
- Henok Travel focuses on cinematic travel storytelling, local history and culture, landscapes and useful visitor context.
- Do not assume TechTrek Timeline or any other channel, page, profile or group is owned by the user.
- Long-form scripts should have a strong hook, narrative arc, scene-by-scene narration, visual/B-roll directions, smooth transitions and a natural call to action.
- Keep narration separate from visual directions. Do not fabricate research.
PUBLISHING SAFETY:
- Never publish, post, email or distribute content externally without explicit owner approval through the approval buttons.
- Drafting content in this chat is allowed without approval.
"""

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"HENOK AI is running"
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

def gemini_generate(parts, timeout=45, use_web_search=False, system_instruction=None):
    if not GEMINI_API_KEY:
        log.error('Gemini request blocked: GEMINI_API_KEY is not configured')
        return None
    models = list(dict.fromkeys([GEMINI_MODEL, 'gemini-3.7-flash', 'gemini-3.5-flash-lite']))
    payload = {'systemInstruction': {'parts': [{'text': system_instruction or SYSTEM_PROMPT}]},
               'contents': [{'role': 'user', 'parts': parts}]}
    if use_web_search:
        payload['tools'] = [{'google_search': {}}]
    for model in models:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
        try:
            response = HTTP.post(url, headers={'Content-Type': 'application/json', 'x-goog-api-key': GEMINI_API_KEY},
                                 json=payload, timeout=(10, timeout))
            if response.ok:
                data = response.json()
                candidates = data.get('candidates') or []
                if candidates:
                    candidate = candidates[0]
                    parts_out = candidate.get('content', {}).get('parts', [])
                    answer = '\n'.join(p.get('text', '') for p in parts_out if p.get('text')).strip()
                    if answer:
                        log.info('Gemini succeeded: model=%s web_search=%s', model, use_web_search)
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
                            else:
                                answer += '\n\nNote: Search returned no source links for this response; verify time-sensitive claims.'
                        return answer
                log.error('Gemini returned no text: model=%s response=%s', model, str(data)[:500])
                continue
            try:
                detail = response.json().get('error', {}).get('message', response.text)
            except Exception:
                detail = response.text
            log.error('Gemini HTTP %s model=%s web_search=%s: %s', response.status_code, model, use_web_search, str(detail)[:500])
            if response.status_code == 400 and use_web_search:
                log.warning('Search-grounded request rejected; retrying without Search tool')
                payload.pop('tools', None)
                use_web_search = False
                continue
            if response.status_code in (400, 401, 403):
                break
        except requests.RequestException:
            log.exception('Gemini network error: model=%s', model)
        except Exception:
            log.exception('Unexpected Gemini error: model=%s', model)
    return None

def needs_live_search(text):
    import re
    patterns = [
        r'(?i)\b(today|latest|current|right now|this week|trending|breaking|recent|news|update)\b',
        r'(ዛሬ|አሁን|በዚህ ሳምንት|ወቅታዊ|አዲስ ዜና|የዛሬ ዜና|ትሬንድ|ትሬንዲንግ|አሁናዊ|በቅርቡ የወጣ)',
        r'(ፈልግ|በኢንተርኔት ፈልግ|ምንጭ አምጣ|ማስረጃ አምጣ)'
    ]
    return any(re.search(pattern, text or '') for pattern in patterns)

def build_script_instruction(channel, topic):
    channel = (channel or '').strip().lower()
    if channel not in ('travel', 'henok travel', 'henoktravel'):
        raise ValueError('Only Henok Travel is currently registered as an owner-confirmed account. Other channels are not authorized.')
    channel_name = 'Henok Travel'
    focus = 'cinematic travel documentary: destination context, history, culture, landscapes, sensory details, practical travel tips and responsible tourism'
    return (
        f'Create a research-led long-form YouTube video package for {channel_name}. Topic: {topic}. Focus: {focus}. '
        'Use Google Search grounding where available. Do not invent facts, statistics or sources. '
        'Include 3 title options, thumbnail concept, powerful 15-second hook, full narration with chapter headings and smooth transitions, '
        'scene-by-scene B-roll/visual directions separated from narration, on-screen graphic suggestions, natural CTA, source URLs and a fact-check note. '
        'Aim for an 8–12 minute script. Write in natural engaging Amharic unless English is requested. Never publish automatically.'
    )
def is_owner(message):
    return str(message.from_user.id) == OWNER_ID

def download_telegram_file(file_id):
    info = bot.get_file(file_id)
    if getattr(info, "file_size", 0) and info.file_size > MAX_BYTES:
        raise ValueError("ፋይሉ ከ18MB በላይ ነው። እባክህ አጭር ወይም ትንሽ ፋይል ላክ።")
    data = bot.download_file(info.file_path)
    if len(data) > MAX_BYTES:
        raise ValueError("ፋይሉ ከ18MB በላይ ነው።")
    return data

@bot.message_handler(commands=["start", "menu"])
def start_command(message):
    send_text(
        message.chat.id,
        "ሰላም ሄኖክ! 👋 HENOK AI ዝግጁ ነው።\n"
        "ጽሑፍ፣ Voice Note፣ ፎቶ ወይም ቪዲዮ ላክልኝ።\n/script tech | የቴክኖሎጂ ርዕስ\n/script travel | የጉዞ ርዕስ\n\n"
        "ወደ Henok Graphics የሚላክ ይዘት ለማጽደቅ /publish ተጠቀም።"
    )

@bot.message_handler(commands=["script"])
def script_command(message):
    if not is_owner(message):
        return
    raw = (message.text or '').partition(' ')[2].strip()
    if '|' not in raw:
        send_text(message.chat.id, 'አጠቃቀም፦ /script travel | የጉዞ ርዕስ (በአሁኑ ጊዜ Henok Travel ብቻ ተመዝግቧል።)')
        return
    channel, topic = [part.strip() for part in raw.split('|', 1)]
    if not topic:
        send_text(message.chat.id, 'እባክህ የቪዲዮውን ርዕስ ጨምር።')
        return
    bot.send_chat_action(message.chat.id, "typing")
    try:
        instruction = build_script_instruction(channel, topic)
    except ValueError as exc:
        send_text(message.chat.id, str(exc))
        return
    answer = gemini_generate([{"text": "Prepare the complete long-form video package."}], timeout=120, use_web_search=True, system_instruction=instruction)
    send_text(message.chat.id, answer or "ስክሪፕቱን ማዘጋጀት አልተቻለም። Render Logs አረጋግጥ።", reply_to=message.message_id)
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
    # Save pending content against the approval message in process memory.
    if sent:
        PENDING_POSTS[sent[-1].message_id] = content

PENDING_POSTS = {}

@bot.callback_query_handler(func=lambda call: call.data in ("publish:yes", "publish:no"))
def approval_callback(call):
    if str(call.from_user.id) != OWNER_ID:
        bot.answer_callback_query(call.id, "ይህን ማጽደቅ የሚችለው ባለቤቱ ብቻ ነው።", show_alert=True)
        return
    content = PENDING_POSTS.pop(call.message.message_id, None)
    if not content:
        bot.answer_callback_query(call.id, "ይህ ይዘት አልቋል ወይም አልተገኘም።", show_alert=True)
        return
    if call.data == "publish:no":
        bot.answer_callback_query(call.id, "ተሰርዟል።")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        send_text(call.message.chat.id, "❌ ይዘቱ ተወ፤ ወደ ግሩፑ አልተላከም።")
        return
    try:
        bot.send_message(TARGET_CHANNEL_ID, content)
        bot.answer_callback_query(call.id, "ተሳክቷል።")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        send_text(call.message.chat.id, "✅ ይዘቱ ወደ Henok Graphics ተልኳል።")
    except Exception:
        log.exception("Could not publish approved content")
        bot.answer_callback_query(call.id, "መላክ አልተቻለም።", show_alert=True)

@bot.message_handler(content_types=["text"])
def text_handler(message):
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    bot.send_chat_action(message.chat.id, "typing")
    answer = gemini_generate([{"text": text}], timeout=50, use_web_search=needs_live_search(text))
    if answer:
        send_text(message.chat.id, answer, reply_to=message.message_id)
    else:
        send_text(message.chat.id, "የAI አገልግሎቱ አሁን ምላሽ አልሰጠም። እባክህ ትንሽ ቆይተህ ድገም።", reply_to=message.message_id)

@bot.message_handler(content_types=["voice", "audio"])
def voice_handler(message):
    chat_id = message.chat.id
    bot.send_chat_action(chat_id, "typing")
    audio = message.voice or message.audio
    try:
        raw = download_telegram_file(audio.file_id)
        mime = getattr(audio, "mime_type", None) or ("audio/ogg" if message.voice else "audio/mpeg")
        prompt = "Listen to this audio. Transcribe it accurately, identify its language, then answer the speaker's actual request. If it is only a statement, summarize it."
        parts = [
            {"text": prompt},
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(raw).decode("ascii")}},
        ]
        answer = gemini_generate(parts, timeout=75)
        if answer:
            send_text(chat_id, answer, reply_to=message.message_id)
        else:
            send_text(chat_id, "ድምፁን ማቀናበር አልተሳካም። Render Logs ውስጥ የGemini ስህተቱን እየመዘገብኩ ነው።", reply_to=message.message_id)
    except Exception as exc:
        log.exception("Voice handler failed")
        send_text(chat_id, str(exc) if isinstance(exc, ValueError) else "የድምፅ ፋይሉን ማውረድ ወይም ማንበብ አልተቻለም።")

@bot.message_handler(content_types=["photo"])
def photo_handler(message):
    chat_id = message.chat.id
    bot.send_chat_action(chat_id, "typing")
    try:
        photo = message.photo[-1]
        raw = download_telegram_file(photo.file_id)
        prompt = (message.caption or "Describe this image accurately in Amharic.").strip()
        parts = [
            {"text": prompt},
            {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(raw).decode("ascii")}},
        ]
        answer = gemini_generate(parts, timeout=60)
        send_text(chat_id, answer or "ምስሉን መተንተን አልተቻለም።", reply_to=message.message_id)
    except Exception:
        log.exception("Photo handler failed")
        send_text(chat_id, "ፎቶውን ማቀናበር አልተቻለም።")

@bot.message_handler(content_types=["video", "document"])
def video_handler(message):
    chat_id = message.chat.id
    media = message.video or message.document
    mime = getattr(media, "mime_type", None) or ("video/mp4" if message.video else "")
    if not mime.startswith("video/"):
        return
    bot.send_chat_action(chat_id, "typing")
    try:
        raw = download_telegram_file(media.file_id)
        prompt = (getattr(message, "caption", None) or "Analyze this video. Summarize the events and identify useful moments with approximate timestamps.").strip()
        parts = [
            {"text": prompt},
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(raw).decode("ascii")}},
        ]
        answer = gemini_generate(parts, timeout=90)
        send_text(chat_id, answer or "ቪዲዮውን መተንተን አልተቻለም።", reply_to=message.message_id)
    except Exception:
        log.exception("Video handler failed")
        send_text(chat_id, "ቪዲዮውን ማቀናበር አልተቻለም። ከ18MB በታች የሆነ MP4 ሞክር።")

if __name__ == "__main__":
    start_health_server()
    # Polling and webhooks are mutually exclusive. Remove any old webhook
    # before starting exactly one long-polling process on this Render service.
    try:
        bot.remove_webhook()
        log.info("Removed any existing Telegram webhook before polling")
    except Exception:
        log.exception("Could not remove Telegram webhook")
        raise

    log.info("Starting HENOK AI long polling; model=%s", GEMINI_MODEL)
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=25,
        skip_pending=True,
        allowed_updates=["message", "callback_query"],
    )
