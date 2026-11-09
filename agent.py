# -*- coding: utf-8 -*-
"""
agent.py  -  Henok's personal Telegram command cockpit (@HenokAssistantBot)

Runs 24/7 on Render (Python 3). Single file.

Required env vars:
  TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, OPENROUTER_API_KEY
Optional env vars:
  TARGET_CHANNEL (default -1003098019114), OWNER_ID (default 511440586), PORT (default 10000)

requirements.txt:
  pyTelegramBotAPI
  requests
"""

import base64
import logging
import os
import re
import sys
import threading
import urllib.parse
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
TARGET_CHANNEL = os.environ.get("TARGET_CHANNEL", "@HenokGraphics").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "511440586"))
PORT = int(os.environ.get("PORT", "10000"))

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_UPLOAD = "https://generativelanguage.googleapis.com/upload/v1beta/files"
# Resilient sequence (primary -> fallbacks). No deprecated aliases.
GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-pro-latest"]
STATUS_MODELS = ["gemini-flash-latest", "gemini-pro-latest", "gemini-2.5-flash", "gemini-2.5-pro"]
OPENROUTER_MODELS = [
    "anthropic/claude-3.5-sonnet",
    "openai/gpt-4o-mini",
    "deepseek/deepseek-chat",
    "meta-llama/llama-3.3-70b-instruct:free",
]

MAX_MEDIA_BYTES = 18 * 1024 * 1024      # Henok's limit for video/audio/photo
INLINE_LIMIT_BYTES = 12 * 1024 * 1024   # above this, use Gemini Files API (inline body cap is ~20MB incl. base64)
MAX_HISTORY_TURNS = 8

ACK_TEXT = "⏳ እሺ ሄኖክ! መረጃውን እያሰባሰብኩ ነው፤ ጥቂት ሰከንዶች ጠብቀኝ..."
BUSY_TEXT = ("ሄኖክ፣ አሁን መስመሮቹ ለአፍታ ተጨናንቀዋል 😅 መልእክትህ አልጠፋም — "
             "ከ1 ደቂቃ በኋላ ደግመህ ላክልኝ፣ ወዲያው እመልሳለሁ።")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("henok-agent")

# ----------------------------------------------------------------------------
# Health server (Render free tier needs an open port answering 200 on "/")
# ----------------------------------------------------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        payload = b"OK - Henok Assistant is alive"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok(body=False)

    def log_message(self, *args):
        pass


def start_health_server():
    def _serve():
        try:
            server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
            log.info("Health server listening on port %s", PORT)
            server.serve_forever()
        except Exception as e:  # never kill the bot because of the health port
            log.error("Health server error: %s", e)

    threading.Thread(target=_serve, daemon=True, name="health").start()


# ----------------------------------------------------------------------------
# Bot + in-memory state
# ----------------------------------------------------------------------------
start_health_server()

if not TELEGRAM_BOT_TOKEN:
    log.error("TELEGRAM_BOT_TOKEN is missing. Set it in Render environment variables.")
    time.sleep(5)
    sys.exit(1)

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None, threaded=True, num_threads=8)

state_lock = threading.Lock()
histories = {}        # user_id -> [(role, text)]
awaiting_topic = {}   # user_id -> mode ("news" | "post")
drafts = {}           # draft_id -> {"post": str, "ts": float}
news_cache = {}       # news_id -> {"label": str, "text": str, "ts": float}

CATEGORIES = {
    "ai": ("🤖 AI እና ቴክኖሎጂ", "AI & Technology"),
    "crypto": ("🪙 Crypto እና ገበያ", "Crypto & Markets"),
    "biz": ("💼 ኢኮኖሚና ቢዝነስ", "Economy & Business"),
    "creator": ("🎬 ፈጣሪዎችና ሶሻል ሚዲያ", "Creators, Video Creation, Editing & Social Media"),
}
CUSTOM_LABEL = "✍️ የራሴን ርዕስ እጽፋለሁ"

NEWS_WORDS = ["ዜና", "ትሬንድ", "ወቅታዊ", "አዲስ ነገር", "news", "trend", "latest", "trending"]
POST_WORDS = ["ፖስት", "ካፕሽን", "ለቻናል", "ለማህበረሰብ", "post", "caption"]
REALTIME_WORDS = ["ዛሬ", "አሁን", "ዋጋ", "ምንዛሬ", "ዜና", "ትሬንድ", "ወቅታዊ", "ቅርብ ጊዜ",
                  "today", "now", "price", "latest", "news", "trend", "current", "bitcoin", "btc",
                  "usd", "rate", "2026"]

# ----------------------------------------------------------------------------
# System prompt
# ----------------------------------------------------------------------------
def addis_now():
    return datetime.now(timezone.utc) + timedelta(hours=3)


def system_prompt():
    t = addis_now().strftime("%A, %d %B %Y, %H:%M")
    return (
        "You are the personal AI chief-of-staff of ሄኖክ (Henok Habtamu), a digital marketer, video producer "
        "and creator in Addis Ababa, Ethiopia. You live inside his private Telegram bot.\n"
        f"Current Addis Ababa time: {t}.\n\n"
        "BRAND CONTEXT: YouTube 'Henok Travel' (cinematic Ethiopian travel documentaries); community "
        "'Henok Graphics' (video editors, motion designers, creators); commerce: Whop viral clipping "
        "campaigns, Amazon KDP, Ye-buna (የቡና), Sekela Marketing. Content pillars: AI & Tech, Crypto & "
        "Markets, Economy & Business, Video Creation & Editing.\n\n"
        "STYLE RULES (strict):\n"
        "- Always reply in natural, energetic, modern Amharic as used by digital creatives in Addis Ababa. "
        "English tech/marketing terms (Hook, CTA, Reels, Canva, CapCut) may stay in English.\n"
        "- Address him warmly as 'ሄኖክ'.\n"
        "- No robotic fluff, no clinical essays, no repeated apologies. Be punchy, practical, specific.\n"
        "- This is Telegram plain text: do NOT use markdown symbols (no **, no ##, no backticks, no tables). "
        "Use emojis, short lines and simple '•' bullets.\n"
        "- Never claim you published anything. Publishing only happens when Henok taps the approval button.\n"
        "- If you are not sure about a fact, say so instead of inventing it."
    )


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def split_text(text, limit=3900):
    text = (text or "").strip() or "…"
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit * 0.5:
            cut = text.rfind(" ", 0, limit)
        if cut < limit * 0.5:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    chunks.append(text)
    return chunks


def deliver(chat_id, ack_id, text, markup=None):
    """Edit the acknowledgment message with the result (first chunk), send the rest as new messages.
    The keyboard, if any, is attached to the last chunk."""
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        mk = markup if i == len(chunks) - 1 else None
        if i == 0 and ack_id:
            try:
                bot.edit_message_text(chunk, chat_id, ack_id, reply_markup=mk,
                                      disable_web_page_preview=True)
                continue
            except Exception as e:
                log.warning("edit failed, sending new message: %s", e)
        bot.send_message(chat_id, chunk, reply_markup=mk, disable_web_page_preview=True)


def send_ack(chat_id):
    try:
        return bot.send_message(chat_id, ACK_TEXT).message_id
    except Exception as e:
        log.error("ack failed: %s", e)
        return None


def format_sources(sources, limit=6):
    if not sources:
        return ""
    lines = ["🔗 የተረጋገጡ ምንጮች:"]
    for i, (title, uri) in enumerate(sources[:limit], 1):
        lines.append(f"{i}. {title or 'ምንጭ'}\n{uri}")
    return "\n".join(lines)


def prune(store, max_items=60):
    with state_lock:
        if len(store) > max_items:
            for k in sorted(store, key=lambda k: store[k].get("ts", 0))[: len(store) - max_items]:
                store.pop(k, None)


def has_any(text, words):
    low = (text or "").lower()
    return any(w.lower() in low for w in words)


def is_owner(user_id):
    return user_id == OWNER_ID


def owner_only(func):
    def wrapper(message, *a, **kw):
        chat = getattr(message, "chat", None)
        if not chat or chat.type != "private":
            return  # Complete silence in groups/channels
        uid = getattr(message, "from_user", None) and message.from_user.id
        if not is_owner(uid):
            try:
                bot.reply_to(message, "ይህ የሄኖክ የግል ቦት ነው 🔒")
            except Exception:
                pass
            return
        return func(message, *a, **kw)
    wrapper.__name__ = func.__name__
    return wrapper


# ----------------------------------------------------------------------------
# Gemini (Google Generative Language REST API)
# ----------------------------------------------------------------------------
class GeminiError(Exception):
    pass


def _gemini_call(model, payload):
    url = f"{GEMINI_BASE}/models/{model}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    for attempt in range(2):
        r = requests.post(url, headers=headers, json=payload, timeout=180)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            time.sleep(2)
            continue
        raise GeminiError(f"{model} HTTP {r.status_code}: {r.text[:200]}")
    raise GeminiError(f"{model}: exhausted retries")


def _extract(data):
    cands = data.get("candidates") or []
    if not cands:
        raise GeminiError(f"no candidates: {str(data.get('promptFeedback'))[:150]}")
    cand = cands[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise GeminiError(f"empty text (finish={cand.get('finishReason')})")
    sources, seen = [], set()
    for ch in (cand.get("groundingMetadata") or {}).get("groundingChunks") or []:
        web = ch.get("web") or {}
        uri = web.get("uri")
        if uri and uri not in seen:
            seen.add(uri)
            sources.append((web.get("title") or "", uri))
    return text, sources


def gemini_generate(contents, grounding=False, system=None):
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY missing")
    payload = {
        "systemInstruction": {"parts": [{"text": system or system_prompt()}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 8192},
    }
    if grounding:
        payload["tools"] = [{"google_search": {}}]
    last_err = None
    for model in GEMINI_MODELS:
        try:
            data = _gemini_call(model, payload)
            text, sources = _extract(data)
            return text, sources, model
        except Exception as e:
            last_err = e
            log.warning("Gemini model failed: %s", e)
    raise GeminiError(f"all Gemini models failed: {last_err}")


def gemini_upload_file(data, mime, display_name):
    """Gemini Files API (resumable) for big media that cannot go inline."""
    start = requests.post(
        GEMINI_UPLOAD,
        headers={
            "x-goog-api-key": GEMINI_API_KEY,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(data)),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": display_name}},
        timeout=60,
    )
    if start.status_code != 200 or "X-Goog-Upload-URL" not in start.headers:
        raise GeminiError(f"upload start failed: {start.status_code} {start.text[:150]}")
    up = requests.post(
        start.headers["X-Goog-Upload-URL"],
        headers={
            "Content-Length": str(len(data)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        data=data,
        timeout=300,
    )
    if up.status_code != 200:
        raise GeminiError(f"upload failed: {up.status_code} {up.text[:150]}")
    info = up.json().get("file", {})
    name, uri = info.get("name"), info.get("uri")
    if not name or not uri:
        raise GeminiError("upload returned no file info")
    deadline = time.time() + 150
    state = info.get("state", "PROCESSING")
    while state == "PROCESSING" and time.time() < deadline:
        time.sleep(3)
        r = requests.get(f"{GEMINI_BASE}/{name}", headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=30)
        if r.status_code == 200:
            state = r.json().get("state", state)
    if state != "ACTIVE":
        raise GeminiError(f"uploaded file not ACTIVE (state={state})")
    return uri


# ----------------------------------------------------------------------------
# OpenRouter text fallback
# ----------------------------------------------------------------------------
def openrouter_generate(history, user_text, system=None):
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY missing")
    messages = [{"role": "system", "content": system or system_prompt()}]
    for role, txt in history:
        messages.append({"role": "assistant" if role == "model" else "user", "content": txt})
    messages.append({"role": "user", "content": user_text})
    last_err = None
    for model in OPENROUTER_MODELS:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://t.me/HenokAssistantBot",
                    "X-Title": "Henok Assistant",
                },
                json={"model": model, "messages": messages, "temperature": 0.8, "max_tokens": 4096},
                timeout=120,
            )
            if r.status_code != 200:
                raise RuntimeError(f"{model} HTTP {r.status_code}: {r.text[:150]}")
            txt = (r.json()["choices"][0]["message"]["content"] or "").strip()
            if not txt:
                raise RuntimeError(f"{model} empty")
            return txt, model
        except Exception as e:
            last_err = e
            log.warning("OpenRouter failed: %s", e)
    raise RuntimeError(f"all OpenRouter models failed: {last_err}")


# ----------------------------------------------------------------------------
# Unified text engine: Gemini -> OpenRouter -> friendly message (never "unavailable")
# ----------------------------------------------------------------------------
def ask_text(user_text, history=None, grounding=False):
    history = history or []

    # If grounding is needed, query Gemini with Google Search tool first
    if grounding:
        contents = [{"role": r, "parts": [{"text": t}]} for r, t in history]
        contents.append({"role": "user", "parts": [{"text": user_text}]})
        try:
            text, sources, model = gemini_generate(contents, grounding=True)
            log.info("grounded by Gemini %s", model)
            return text, sources
        except Exception as e:
            log.warning("Gemini grounding failed, switching to OpenRouter: %s", e)

    # For copywriting, creative thinking, and natural Amharic: prioritize Claude/GPT via OpenRouter
    if OPENROUTER_API_KEY:
        try:
            text, model = openrouter_generate(history, user_text)
            log.info("answered by OpenRouter %s", model)
            return text, []
        except Exception as e:
            log.warning("OpenRouter failed, falling back to Gemini: %s", e)

    # Fallback to Gemini
    contents = [{"role": r, "parts": [{"text": t}]} for r, t in history]
    contents.append({"role": "user", "parts": [{"text": user_text}]})
    try:
        text, sources, model = gemini_generate(contents, grounding=False)
        log.info("answered by Gemini %s", model)
        return text, sources
    except Exception as e:
        log.error("Gemini failed too: %s", e)

    return BUSY_TEXT, []


def remember(user_id, user_text, reply):
    with state_lock:
        h = histories.setdefault(user_id, [])
        h.append(("user", user_text[:1500]))
        h.append(("model", reply[:1500]))
        del h[: max(0, len(h) - MAX_HISTORY_TURNS * 2)]


# ----------------------------------------------------------------------------
# Interactive categories (inline buttons)
# ----------------------------------------------------------------------------

def make_pollinations_url(prompt_en):
    clean = re.sub(r"[^\w\s,-]", "", prompt_en).strip()
    if not clean:
        clean = "modern high-tech digital art 3d render clean minimalist"
    encoded = urllib.parse.quote(clean[:300])
    seed = int(time.time())
    return f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1080&nologo=true&seed={seed}"

def category_keyboard(mode):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(CATEGORIES["ai"][0], callback_data=f"cat:{mode}:ai"),
        types.InlineKeyboardButton(CATEGORIES["crypto"][0], callback_data=f"cat:{mode}:crypto"),
    )
    kb.add(
        types.InlineKeyboardButton(CATEGORIES["biz"][0], callback_data=f"cat:{mode}:biz"),
        types.InlineKeyboardButton(CATEGORIES["creator"][0], callback_data=f"cat:{mode}:creator"),
    )
    kb.add(types.InlineKeyboardButton(CUSTOM_LABEL, callback_data=f"cat:{mode}:custom"))
    return kb


def show_categories(chat_id, mode):
    head = ("ሄኖክ፣ ስለየትኛው ዘርፍ ወቅታዊ መረጃ ላምጣልህ? 👇" if mode == "news"
            else "ሄኖክ፣ ለየትኛው ዘርፍ ፖስት ልፍጠር? 👇")
    bot.send_message(chat_id, head, reply_markup=category_keyboard(mode))


# ----------------------------------------------------------------------------
# News + Post engines
# ----------------------------------------------------------------------------
def run_news(chat_id, label, topic, start_time=None):
    start_time = start_time or time.time()
    ack_id = send_ack(chat_id, "⏳ ሄኖክ፣ ወቅታዊ መረጃዎችን ከ Google Search እያሰባሰብኩ ነው (ከ10-15 ሰከንድ ይወስዳል)...")
    prompt = (
        f"ርዕስ/ዘርፍ: {topic}\n\n"
        "Use live Google Search to find the most important, VERIFIED developments from the last few days "
        "on this topic. Deliver 4-5 punchy insights. Each insight: an emoji + one-line headline, then 1-2 "
        "energetic sentences on why it matters to an Ethiopian creator/marketer like Henok. "
        "Mention the source name inline when you can. Finish with '🎯 ለዛሬ የይዘት ሀሳብ:' and ONE concrete "
        "content idea (video or post) he can make from this. Plain text only, no markdown."
    )
    text, sources = ask_text(prompt, grounding=True)
    body = text
    src = format_sources(sources)
    if src:
        body += "\n\n" + src
    markup = None
    if text != BUSY_TEXT:
        nid = uuid.uuid4().hex[:10]
        with state_lock:
            news_cache[nid] = {"label": label, "text": text, "ts": time.time()}
        prune(news_cache)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📝 ከዚህ ፖስት ፍጠርልኝ", callback_data=f"mk:{nid}"))
        elapsed = int(time.time() - start_time)
    body += f"\n\n⏱ የፈጀበት ጊዜ: {elapsed} ሰከንድ"
    deliver(chat_id, ack_id, body, markup)


POST_FORMAT = (
    "Return EXACTLY this structure and nothing before ===POST===:\n"
    "===POST===\n"
    "(The ready-to-publish Telegram post for the 'Henok Graphics' community of video editors, motion "
    "designers and creators: 1) 🔥 Catchy Hook line, 2) Structured body copy in natural, flawless modern Amharic, "
    "3) Clear Actionable CTA, 4) 5-8 relevant Hashtags. Plain text, no markdown symbols like ** or ##.)\n"
    "===IMAGE_PROMPT===\n"
    "(A standalone, highly detailed English prompt describing a cinematic, clean, modern commercial marketing visual or 3D digital illustration representing this exact topic for image generation. Do NOT include text on the image; focus on visual aesthetics, lighting, render style, 8k resolution.)\n"
    "===CANVA===\n"
    "🎨 Canva Design Blueprint:\n"
    "• Color palette: 3-4 HEX codes with role of each\n"
    "• Bold overlay hook text (max 6 words)\n"
    "• Visual composition: layout, focal point, fonts feel\n"
)


def run_post(chat_id, label, topic, context_text=None, start_time=None):
    start_time = start_time or time.time()
    ack_id = send_ack(chat_id, "⏳ ሄኖክ፣ ጥናቱን አድርጌ ፖስቱን እና AI ምስሉን እያዘጋጀሁ ነው (ከ20-30 ሰከንድ ይወስዳል)...")
    prompt = f"Create a full social post package about: {topic}\n"
    if context_text:
        prompt += f"\nUse these verified insights as the base:\n{context_text}\n"
    prompt += "\nUse Google Search to verify any facts or numbers you include.\n\n" + POST_FORMAT
    text, sources = ask_text(prompt, grounding=True)
    if text == BUSY_TEXT:
        deliver(chat_id, ack_id, text)
        return

    m_post = re.search(r"===POST===\s*(.*?)\s*(?:===IMAGE_PROMPT===|===CANVA===|\Z)", text, re.S)
    m_img = re.search(r"===IMAGE_PROMPT===\s*(.*?)\s*(?:===CANVA===|\Z)", text, re.S)
    m_canva = re.search(r"===CANVA===\s*(.*)\Z", text, re.S)

    post = m_post.group(1).strip() if m_post else text.strip()
    img_prompt = m_img.group(1).strip() if m_img else f"modern cinematic digital render representing {topic}"
    canva = m_canva.group(1).strip() if m_canva else ""

    image_url = make_pollinations_url(img_prompt)

    did = uuid.uuid4().hex[:10]
    with state_lock:
        drafts[did] = {
            "post": post,
            "image_url": image_url,
            "canva": canva,
            "ts": time.time(),
        }
    prune(drafts)

    elapsed = int(time.time() - start_time)

    preview = f"📝 የፖስት ረቂቅ — {label}\n━━━━━━━━━━━━\n{post}"
    if canva:
        preview += f"\n\n━━━━━━━━━━━━\n{canva}"
    src = format_sources(sources)
    if src:
        preview += f"\n\n{src}"

    preview += f"\n\n⏱ የፈጀበት ጊዜ: {elapsed} ሰከንድ"
    preview += (
        f"\n\n━━━━━━━━━━━━\n🔒 ወደ {TARGET_CHANNEL} የሚላከው ከላይ ያለው ፖስት እና ከታች ያለው AI ምስል ነው (Canva Blueprint አይላክም)። "
        "አንተ ካላጸደቅክ ምንም አይወጣም።"
    )

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ አጽድቅና ፖስት አድርግ", callback_data=f"ok:{did}"),
        types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"no:{did}"),
    )
    deliver(chat_id, ack_id, preview, kb)

    try:
        bot.send_photo(
            chat_id,
            photo=image_url,
            caption=f"🎨 ለዚህ ፖስት የተዘጋጀ AI ምስል ({label})\nበማጽደቂያው ቁልፍ ስታጸድቅ ይህ ምስል ከነፖስቱ ይለጠፋል።",
        )
    except Exception as e:
        log.warning("failed to send photo preview: %s", e)


# ----------------------------------------------------------------------------
# Multimodal (voice / audio / video / photo)
# ----------------------------------------------------------------------------
AUDIO_PROMPT = (
    "This is a voice/audio message from Henok. Do three things:\n"
    "1) 📝 ትራንስክሪፕት: transcribe it accurately (Amharic if spoken in Amharic; keep the original language "
    "and add an Amharic translation if it is another language).\n"
    "2) 📌 ማጠቃለያ: a short summary in 2-4 lines.\n"
    "3) ✅ Action Items: a clear checklist of tasks/decisions with any deadlines mentioned.\n"
    "Plain text, no markdown symbols."
)
VIDEO_PROMPT = (
    "Analyze this video for Henok (video producer). Provide:\n"
    "1) 🎬 Scene-by-scene breakdown with approximate timestamps (MM:SS).\n"
    "2) 📌 Short summary of the video.\n"
    "3) 🔥 THREE viral ~30-second clip candidates for Whop clipping / TikTok / Shorts: for each give exact "
    "start-end timestamps (MM:SS-MM:SS), why it can go viral, a hook caption for the first 2 seconds, and "
    "hashtags.\n"
    "4) ✂️ One quick editing tip per clip.\n"
    "Plain text, no markdown symbols."
)
PHOTO_PROMPT = (
    "Analyze this image for Henok's Canva design work. Give: 1) 🖼 what you see, 2) composition & layout "
    "(focal point, balance, empty space), 3) dominant colors as HEX codes and a matching 3-4 color palette, "
    "4) strengths, 5) weaknesses, 6) 4-6 concrete Canva improvement steps (fonts feel, overlay hook text, "
    "contrast, cropping). Plain text, no markdown symbols."
)


def multimodal_generate(parts):
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY missing")
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt()}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 8192},
    }
    last_err = None
    for model in GEMINI_MODELS:
        try:
            text, _ = _extract(_gemini_call(model, payload))
            return text
        except Exception as e:
            last_err = e
            log.warning("Multimodal model failed: %s", e)
    raise GeminiError(f"all models failed for media: {last_err}")


def detect_media(message):
    """Return (kind, file_id, mime, size) or None."""
    ct = message.content_type
    if ct == "voice":
        v = message.voice
        return "audio", v.file_id, v.mime_type or "audio/ogg", v.file_size or 0
    if ct == "audio":
        a = message.audio
        return "audio", a.file_id, a.mime_type or "audio/mpeg", a.file_size or 0
    if ct == "video":
        v = message.video
        return "video", v.file_id, v.mime_type or "video/mp4", v.file_size or 0
    if ct == "video_note":
        v = message.video_note
        return "video", v.file_id, "video/mp4", v.file_size or 0
    if ct == "photo":
        p = message.photo[-1]
        return "photo", p.file_id, "image/jpeg", p.file_size or 0
    if ct == "document":
        d = message.document
        mime = (d.mime_type or "").lower()
        if mime.startswith("image/"):
            return "photo", d.file_id, mime, d.file_size or 0
        if mime.startswith("video/"):
            return "video", d.file_id, mime, d.file_size or 0
        if mime.startswith("audio/"):
            return "audio", d.file_id, mime, d.file_size or 0
    return None


@bot.message_handler(content_types=["voice", "audio", "video", "video_note", "photo", "document"])
@owner_only
def handle_media(message):
    info = detect_media(message)
    chat_id = message.chat.id
    if not info:
        bot.reply_to(message, "ሄኖክ፣ ይህን የፋይል አይነት ገና አልተረዳሁም 🙈 ድምፅ፣ ቪዲዮ ወይም ፎቶ ላክልኝ።")
        return
    kind, file_id, mime, size = info
    if size and size > MAX_MEDIA_BYTES:
        bot.reply_to(message, f"ሄኖክ፣ ፋይሉ {size / 1048576:.1f}MB ነው — እስከ 18MB ብቻ ነው የምተነትነው። "
                              "አሳጥረህ ወይም compress አድርገህ ላክልኝ 🎞")
        return
    ack_id = send_ack(chat_id)
    try:
        fobj = bot.get_file(file_id)
        data = bot.download_file(fobj.file_path)
        if len(data) > MAX_MEDIA_BYTES:
            deliver(chat_id, ack_id, "ሄኖክ፣ ፋይሉ ከ18MB በላይ ነው — አሳጥረህ ላክልኝ 🎞")
            return
        prompt = {"audio": AUDIO_PROMPT, "video": VIDEO_PROMPT, "photo": PHOTO_PROMPT}[kind]
        caption = (message.caption or "").strip()
        if caption:
            prompt += f"\n\nHenok's extra instruction/context: {caption}"
        if len(data) > INLINE_LIMIT_BYTES:
            uri = gemini_upload_file(data, mime, f"{kind}-{uuid.uuid4().hex[:6]}")
            media_part = {"fileData": {"mimeType": mime, "fileUri": uri}}
        else:
            media_part = {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}}
        result = multimodal_generate([{"text": prompt}, media_part])
        deliver(chat_id, ack_id, result)
    except Exception as e:
        log.error("media handling failed: %s", e)
        deliver(chat_id, ack_id,
                "ሄኖክ፣ ፋይሉን አሁን ማጠናቀቅ አልቻልኩም 😅 ጥቂት ቆይተህ እንደገና ላክልኝ — "
                "ቪዲዮ ከሆነ አጠር ያለ ክፍል ብትልክ ይሻላል።")


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
@owner_only
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "ሰላም ሄኖክ! 👋🔥 እኔ የአንተ የግል ረዳት ነኝ — 24/7 ዝግጁ።\n\n"
        "ምን ላደርግልህ እችላለሁ:\n"
        "📰 /news — ወቅታዊ ዜና እና ትሬንድ (በቁልፍ ምረጥ)\n"
        "📝 /post — ሙሉ የፖስት ፓኬጅ + Canva Blueprint\n"
        "🎙 ድምፅ ላክልኝ — ትራንስክሪፕት + ማጠቃለያ + Action Items\n"
        "🎬 ቪዲዮ (እስከ 18MB) — ትንታኔ + 3 ቫይራል ክሊፖች\n"
        "🖼 ፎቶ — የ Canva ዲዛይን አስተያየት\n"
        "💬 ወይም ማንኛውንም ነገር ጻፍልኝ — ስክሪፕት፣ ሀሳብ፣ ጥናት\n"
        "🩺 /ai_status — የ AI ሞተር ሁኔታ\n\n"
        "🔒 ያለ አንተ ፈቃድ ምንም አልለጥፍም — ሁሉም በ ✅ / ❌ ቁልፍ ያልፋል።",
    )


@bot.message_handler(commands=["news", "trends"])
@owner_only
def cmd_news(message):
    show_categories(message.chat.id, "news")


@bot.message_handler(commands=["post"])
@owner_only
def cmd_post(message):
    show_categories(message.chat.id, "post")


@bot.message_handler(commands=["cancel"])
@owner_only
def cmd_cancel(message):
    with state_lock:
        awaiting_topic.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "እሺ ሄኖክ፣ ተሰርዟል ✅")


@bot.message_handler(commands=["ai_status"])
@owner_only
def cmd_ai_status(message):
    chat_id = message.chat.id
    ack_id = None
    try:
        ack_id = bot.send_message(chat_id, "🩺 የ AI ሞተሮችን እየፈተሽኩ ነው...").message_id
    except Exception:
        pass
    lines = ["🩺 AI Status\n━━━━━━━━━━━━"]
    if not GEMINI_API_KEY:
        lines.append("❌ GEMINI_API_KEY አልተሞላም")
    else:
        try:
            r = requests.get(f"{GEMINI_BASE}/models", headers={"x-goog-api-key": GEMINI_API_KEY},
                             params={"pageSize": 1}, timeout=20)
            lines.append("✅ Gemini API: ንቁ" if r.status_code == 200
                         else f"⚠️ Gemini API: HTTP {r.status_code}")
        except Exception as e:
            lines.append(f"⚠️ Gemini API: ግንኙነት አልተሳካም ({str(e)[:60]})")
        lines.append("\n🤖 Gemini ሞዴሎች:")
        for m in STATUS_MODELS:
            try:
                r = requests.get(f"{GEMINI_BASE}/models/{m}", headers={"x-goog-api-key": GEMINI_API_KEY},
                                 timeout=20)
                if r.status_code == 200:
                    role = " (በ ቅደም ተከተሉ ውስጥ)" if m in GEMINI_MODELS else " (ተጠባባቂ)"
                    lines.append(f"  ✅ {m}{role}")
                elif r.status_code == 404:
                    lines.append(f"  ❌ {m} — 404 (ለቁልፍህ አይገኝም)")
                else:
                    lines.append(f"  ⚠️ {m} — HTTP {r.status_code}")
            except Exception as e:
                lines.append(f"  ⚠️ {m} — {str(e)[:50]}")
    lines.append("\n🛟 OpenRouter Fallback: " +
                 ("✅ ቁልፍ አለ — " + ", ".join(OPENROUTER_MODELS) if OPENROUTER_API_KEY
                  else "❌ OPENROUTER_API_KEY አልተሞላም"))
    lines.append(f"\n📣 Target Channel: {TARGET_CHANNEL}")
    lines.append(f"👤 Owner ID: {OWNER_ID}")
    lines.append(f"🕒 የአዲስ አበባ ሰዓት: {addis_now().strftime('%Y-%m-%d %H:%M')}")
    deliver(chat_id, ack_id, "\n".join(lines))


# ----------------------------------------------------------------------------
# Callbacks (categories, approval gate)
# ----------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    chat = getattr(call.message, "chat", None)
    if not chat or chat.type != "private":
        return
    uid = call.from_user.id
    if not is_owner(uid):
        try:
            bot.answer_callback_query(call.id, "ይህ የሄኖክ የግል ቦት ነው 🔒", show_alert=True)
        except Exception:
            pass
        return
    data = call.data or ""
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    try:
        if data.startswith("cat:"):
            _, mode, key = data.split(":", 2)
            if key == "custom":
                with state_lock:
                    awaiting_topic[uid] = mode
                bot.edit_message_text(f"✅ የመረጥከው: {CUSTOM_LABEL}", chat_id, msg_id)
                bot.send_message(chat_id, "እሺ ሄኖክ ✍️ ርዕሱን ጻፍልኝ (ለመሰረዝ /cancel):")
                return
            label, topic = CATEGORIES[key]
            bot.edit_message_text(f"✅ የመረጥከው: {label}", chat_id, msg_id)  # locks selection in chat record
            if mode == "news":
                run_news(chat_id, label, topic)
            else:
                run_post(chat_id, label, topic)

        elif data.startswith("mk:"):
            nid = data.split(":", 1)[1]
            with state_lock:
                item = news_cache.get(nid)
            if not item:
                bot.send_message(chat_id, "ሄኖክ፣ ይህ መረጃ ጊዜው አልፏል — /news ብለህ እንደገና ጠይቀኝ 🙏")
                return
            try:
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
            except Exception:
                pass
            run_post(chat_id, item["label"], item["label"], context_text=item["text"])

        elif data.startswith("ok:"):
            did = data.split(":", 1)[1]
            with state_lock:
                draft = drafts.pop(did, None)
            if not draft:
                bot.edit_message_text("⚠️ ይህ ረቂቅ ጊዜው አልፏል ወይም ቀድሞ ተስተናግዷል።", chat_id, msg_id)
                return

            try:
                post_text = draft["post"]
                image_url = draft.get("image_url")

                posted_photo = False
                if image_url:
                    try:
                        bot.send_photo(TARGET_CHANNEL, photo=image_url, caption=post_text[:1024])
                        posted_photo = True
                        if len(post_text) > 1024:
                            for chunk in split_text(post_text[1024:], 4000):
                                bot.send_message(TARGET_CHANNEL, chunk, disable_web_page_preview=False)
                    except Exception as pe:
                        log.warning("send_photo failed, falling back to text: %s", pe)

                if not posted_photo:
                    for chunk in split_text(post_text, 4000):
                        bot.send_message(TARGET_CHANNEL, chunk, disable_web_page_preview=False)

                bot.edit_message_text(
                    f"✅ ጸድቋል እና ወደ {TARGET_CHANNEL} ተለጥፏል! 🚀\n\n{post_text[:3000]}",
                    chat_id,
                    msg_id,
                )
            except Exception as e:
                log.error("publish failed: %s", e)
                with state_lock:
                    drafts[did] = draft
                bot.send_message(
                    chat_id,
                    f"ሄኖክ፣ መለጠፍ አልተሳካም 😕 ቦቱ የ {TARGET_CHANNEL} አድሚን መሆኑን እና 'Post Messages' "
                    f"ፈቃድ እንዳለው አረጋግጥ።\nዝርዝር: {str(e)[:150]}\nበድጋሚ ✅ ን መንካት ትችላለህ።",
                )

        elif data.startswith("no:"):
            did = data.split(":", 1)[1]
            with state_lock:
                drafts.pop(did, None)
            bot.edit_message_text("❌ ተሰርዟል — ምንም አልተለጠፈም።", chat_id, msg_id)

    except Exception as e:
        log.error("callback error: %s", e)
        try:
            bot.send_message(chat_id, "ሄኖክ፣ ትንሽ ችግር ገጠመኝ 😅 እባክህ እንደገና ሞክር።")
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Free text
# ----------------------------------------------------------------------------
@bot.message_handler(content_types=["text"])
@owner_only
def handle_text(message):
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    uid, chat_id = message.from_user.id, message.chat.id

    # Custom topic typed after tapping "write my own topic"
    with state_lock:
        pending_mode = awaiting_topic.pop(uid, None)
    if pending_mode:
        label = f"✍️ {text[:40]}"
        if pending_mode == "news":
            run_news(chat_id, label, text)
        else:
            run_post(chat_id, label, text)
        return

    mode = "post" if has_any(text, POST_WORDS) else ("news" if has_any(text, NEWS_WORDS) else None)
    if mode:
        if len(text.split()) <= 5:
            show_categories(chat_id, mode)   # short/vague => buttons, no typing fatigue
            return
        if mode == "news":
            run_news(chat_id, f"✍️ {text[:40]}", text)
        else:
            run_post(chat_id, f"✍️ {text[:40]}", text)
        return

    # General conversation / research / scripts
    ack_id = send_ack(chat_id)
    with state_lock:
        history = list(histories.get(uid, []))
    grounding = has_any(text, REALTIME_WORDS)
    reply, sources = ask_text(text, history=history, grounding=grounding)
    body = reply
    src = format_sources(sources)
    if src:
        body += "\n\n" + src
    if reply != BUSY_TEXT:
        remember(uid, text, reply)
    deliver(chat_id, ack_id, body)


# ----------------------------------------------------------------------------
# Main: webhook cleanup + resilient polling
# ----------------------------------------------------------------------------
def main():
    log.info("Starting Henok Assistant agent...")
    try:
        bot.remove_webhook()
    except Exception as e:
        log.warning("remove_webhook failed: %s", e)
    try:
        bot.set_my_commands([
            types.BotCommand("news", "ወቅታዊ ዜና እና ትሬንድ"),
            types.BotCommand("post", "ፖስት ፓኬጅ + Canva Blueprint"),
            types.BotCommand("ai_status", "የ AI ሞተር ሁኔታ"),
            types.BotCommand("cancel", "ሰርዝ"),
            types.BotCommand("help", "እገዛ"),
        ])
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)

    first = True
    while True:
        try:
            bot.infinity_polling(skip_pending=first, timeout=30, long_polling_timeout=25)
            first = False
        except ApiTelegramException as e:
            first = False
            if getattr(e, "error_code", None) == 409 or "409" in str(e):
                log.warning("409 Conflict (another instance polling). Retrying in 5s...")
            else:
                log.error("Telegram API error: %s. Retrying in 5s...", e)
            time.sleep(5)
        except Exception as e:
            first = False
            if "409" in str(e):
                log.warning("409 Conflict. Retrying in 5s...")
            else:
                log.error("Polling crashed: %s. Retrying in 5s...", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
