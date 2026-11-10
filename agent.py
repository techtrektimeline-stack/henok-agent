# -*- coding: utf-8 -*-
"""
agent.py  -  Henok's personal Telegram command cockpit (@HenokAssistantBot) + Sekela News publisher

Runs 24/7 on Render (Python 3 + pyTelegramBotAPI). Single file.

Environment variables
---------------------
Required : TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, GEMINI_API_KEY
Facebook : FB_PAGE_ACCESS_TOKEN  (Page access token with pages_manage_posts, pages_read_engagement, read_insights)
           FB_PAGE_ID            (default 1252817724577069)
           FB_API_VERSION        (default v25.0)
Optional : OWNER_ID              (default 511440586)
           TARGET_CHANNEL        (default @HenokGraphics)
           OPENROUTER_MODELS     (comma list; default deepseek/deepseek-chat,openai/gpt-4o-mini,anthropic/claude-3.5-sonnet)
           FONT_PATH             (path to an Ethiopic .ttf; otherwise auto-detected / downloaded, see find_ethiopic_font)
           PORT                  (default 10000)
           RENDER_EXTERNAL_URL   (set automatically by Render; enables self keep-alive)

requirements.txt
----------------
pyTelegramBotAPI
requests
Pillow

Architecture
------------
* Writing (chat, news, posts, audio summaries, polishing): OpenRouter first (Claude -> DeepSeek -> GPT-4o-mini),
  Gemini as fallback when OpenRouter is unreachable / out of credits.
* Research: Gemini + Google Search grounding (English notes, preferring BBC Amharic, Reuters, AP, Fana, DW Amharic,
  Al Ain...), which the writer turns into Amharic. No verified research -> no news post is drafted.
* Voice notes: Gemini only TRANSCRIBES (long-audio safe); the summary / action items are written by the OpenRouter
  writer straight from the transcript, so the Amharic is native and one failure cannot lose the transcript.
* Sekela News card: AI background (Pollinations, no text) + headline, gradient and brand badge composed locally with
  Pillow, because image generators cannot render Amharic lettering reliably.
* Publishing: ONLY after Henok taps the approval button -> Telegram channel, then the Facebook Page.
* STRICT NO-COMMENTING POLICY: the Facebook layer can only POST to /photos and /feed and READ page info, insights and
  post counters. Any path containing "comment" is refused in code (see _fb_request). The bot never replies to comments.
* The bot talks ONLY in a private chat with the owner; in groups/channels it is silent.
"""

import base64
import copy
import functools
import glob
import io
import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

import requests
import telebot
from telebot import apihelper, types
from telebot.apihelper import ApiTelegramException

try:  # Pillow is optional: without it the bot still works, just without the composed headline card
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont
    PIL_OK = True
except Exception:  # pragma: no cover
    PIL_OK = False

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "511440586"))
PORT = int(os.environ.get("PORT", "10000"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()

FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "1252817724577069").strip()
FB_PAGE_ACCESS_TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
FB_API_VERSION = os.environ.get("FB_API_VERSION", "v25.0").strip()
FB_GRAPH = f"https://graph.facebook.com/{FB_API_VERSION}"
FB_PAGE_NAME = "ሰከላ Sekela"


def normalize_channel(value):
    """'@HenokGraphics' / 'HenokGraphics' / 't.me/HenokGraphics' -> '@HenokGraphics'; numeric ids -> int."""
    v = (value or "").strip()
    if not v:
        return "@HenokGraphics"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if "t.me/" in v:
        v = v.split("t.me/")[-1].strip("/")
    return v if v.startswith("@") else "@" + v


TARGET_CHANNEL = normalize_channel(os.environ.get("TARGET_CHANNEL", "@HenokGraphics"))

OPENROUTER_MODELS = [m.strip() for m in os.environ.get(
    "OPENROUTER_MODELS",
    "deepseek/deepseek-chat,openai/gpt-4o-mini,anthropic/claude-3.5-sonnet").split(",") if m.strip()]
OPENROUTER_URL = "https://openrouter.ai/api/v1"

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_UPLOAD = "https://generativelanguage.googleapis.com/upload/v1beta/files"
GEMINI_MODELS = ["gemini-flash-latest", "gemini-pro-latest", "gemini-2.5-flash"]
STATUS_GEMINI_MODELS = ["gemini-flash-latest", "gemini-pro-latest", "gemini-2.5-flash", "gemini-2.5-pro"]
GEMINI_TIMEOUT = 180

MAX_MEDIA_BYTES = 18 * 1024 * 1024
INLINE_LIMIT_BYTES = 12 * 1024 * 1024
MAX_HISTORY_TURNS = 8
CAPTION_LIMIT = 1024  # Telegram photo-caption hard limit

BRAND_TAGS = ["#ሰከላ_ኒውስ", "#SekelaNews", "#HenokGraphics"]
BRAND_RED = (214, 40, 40)
AI_LABEL = True  # small "AI illustration" tag on cards (the background is generated, not a real photo)

BUSY_TEXT = ("ሄኖክ፣ አሁን መስመሮቹ ለአፍታ ተጨናንቀዋል 😅 መልእክትህ አልጠፋም — "
             "ከ1 ደቂቃ በኋላ ደግመህ ላክልኝ፣ ወዲያውኑ እመልሳለሁ።")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("henok-agent")


# ----------------------------------------------------------------------------
# Health server + self keep-alive (Render free tier / cron-job.org)
# ----------------------------------------------------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def _ok(self, with_body=True):
        payload = b"OK - Henok Assistant is alive"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if with_body:
            self.wfile.write(payload)

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok(with_body=False)

    def log_message(self, *args):
        pass


def start_health_server(port=None):
    port = PORT if port is None else port

    def _serve():
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
            log.info("Health server listening on port %s", server.server_address[1])
            server.serve_forever()
        except Exception as e:  # never let the health port kill the bot
            log.error("Health server error: %s", e)

    threading.Thread(target=_serve, daemon=True, name="health").start()


def start_keepalive():
    """Pings our own public URL every 10 min so Render's free tier does not spin the service down."""
    if not RENDER_EXTERNAL_URL:
        return

    def _loop():
        while True:
            time.sleep(600)
            try:
                requests.get(RENDER_EXTERNAL_URL, timeout=20)
            except Exception as e:
                log.warning("keep-alive ping failed: %s", e)

    threading.Thread(target=_loop, daemon=True, name="keepalive").start()


# ----------------------------------------------------------------------------
# Bot + in-memory state
# ----------------------------------------------------------------------------
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN or "000000:MISSING", parse_mode=None, threaded=True, num_threads=8)

state_lock = threading.Lock()
histories = {}        # user_id -> [(role, text)]
awaiting_topic = {}   # user_id -> "news" | "post"
drafts = {}           # draft_id -> draft dict (see run_post)
news_cache = {}       # news_id -> {"label","text","sources","ts"}

CATEGORIES = {
    "ai": ("🤖 AI እና ቴክኖሎጂ", "AI & Technology"),
    "crypto": ("🪙 Crypto እና ገበያ", "Crypto & Markets"),
    "biz": ("💼 ኢኮኖሚና ቢዝነስ", "Economy & Business"),
    "creator": ("🎬 ፈጣሪዎችና ሶሻል ሚዲያ", "Creators, Video Creation, Editing & Social Media"),
}
CUSTOM_LABEL = "✍️ የራሴን ርዕስ እጽፋለሁ"

NEWS_WORDS = ["ዜና", "ትሬንድ", "ወቅታዊ", "አዲስ ነገር", "news", "trend", "trends", "latest", "trending"]
POST_WORDS = ["ፖስት", "ካፕሽን", "ለቻናል", "ለማህበረሰብ", "post", "caption"]
REALTIME_WORDS = ["ዛሬ", "አሁን", "ዋጋ", "ምንዛሬ", "ዜና", "ትሬንድ", "ወቅታዊ", "ቅርብ ጊዜ",
                  "today", "now", "price", "latest", "news", "trend", "current", "bitcoin", "btc",
                  "usd", "rate", "2026"]

GREET_FIRST_WORDS = {"hi", "hello", "hey", "selam", "salam", "ሰላም", "ታዲያስ", "ጤና"}
GREET_PHRASES = {"good morning", "good afternoon", "good evening", "እንዴት ነህ", "እንደምን ነህ",
                 "ጤና ይስጥልኝ", "ሰላም ነው", "እንዴት ነው", "ደህና ነህ", "እንዴት አለህ"}
THANKS_PHRASES = {"thanks", "thank you", "thx", "አመሰግናለሁ", "እናመሰግናለን", "ok", "okay", "እሺ", "ጥሩ"}

# Top-tier outlets recognised for the mandatory "ምንጭ፡" line: (display name, regex)
OUTLETS = [
    ("BBC Amharic", r"bbc\s*amharic|bbc\.com/amharic|amharic\.bbc"),
    ("BBC", r"\bbbc\b"),
    ("Reuters", r"reuters"),
    ("AP", r"\bap\b|associated press|apnews"),
    ("Fana (ፋና)", r"\bfana\b|fanabc|ፋና|\bfbc\b"),
    ("DW Amharic", r"dw\s*amharic|dw\.com/am"),
    ("DW", r"\bdw\b|dw\.com|deutsche welle"),
    ("Al Ain", r"al[\s\-]*ain"),
    ("AFP", r"\bafp\b"),
    ("Bloomberg", r"bloomberg"),
    ("Al Jazeera", r"al[\s\-]*jazeera"),
]


# ----------------------------------------------------------------------------
# System prompt (Amharic quality guardrail)
# ----------------------------------------------------------------------------
def addis_now():
    return datetime.now(timezone.utc) + timedelta(hours=3)


def system_prompt():
    t = addis_now().strftime("%A, %d %B %Y, %H:%M")
    return (
        "You are the personal AI chief-of-staff of ሄኖክ (Henok Habtamu), a digital marketer, video producer "
        "and creator in Addis Ababa, Ethiopia. You live inside his private Telegram bot.\n"
        f"Current Addis Ababa time: {t}.\n\n"
        "BRAND CONTEXT: news brand 'ሰከላ ኒውስ (Sekela News)' on Facebook (ሰከላ Sekela page) and Telegram; YouTube "
        "'Henok Travel'; community 'Henok Graphics' (@HenokGraphics: video editors, motion designers, creators); "
        "commerce: Whop viral clipping campaigns, Amazon KDP, Ye-buna (የቡና), Sekela Marketing. Content pillars: AI & "
        "Tech, Crypto & Markets, Economy & Business, Video Creation & Editing.\n\n"
        "AMHARIC QUALITY GUARDRAIL (strict, highest priority):\n"
        "- Write natural, native, modern Amharic exactly as a creative marketer / news editor in Addis Ababa writes it. "
        "Think in Amharic; never translate English word-for-word. No robotic, textbook or machine-translated phrasing.\n"
        "- Use correct Ethiopic script and punctuation (። ፣ ፡). Every Amharic word must be a real, correctly "
        "spelled word. If you are not sure of an Amharic word, use the common English term in Latin letters instead "
        "of inventing one.\n"
        "- Keep standard English terms for tech/marketing in Latin letters: CapCut, Canva, Whop, Hook, CTA, Reels, "
        "Shorts, TikTok, AI, Prompt, Thumbnail, Hashtag.\n"
        "- PRONOUN CONSISTENCY: you (the assistant) speak in first person SINGULAR (እኔ; አደርጋለሁ, እልካለሁ, አዘጋጃለሁ). "
        "Never use 'we' forms (እናደርጋለን, እናዘጋጃለን) for yourself. Address Henok in the second person masculine "
        "singular, warm and respectful (አንተ; ታደርጋለህ, ትችላለህ, ላክልኝ, ልርዳህ). Never mix with እርስዎ or plural "
        "forms when talking to him.\n"
        "- For public posts, choose ONE consistent voice for the whole post and never switch between singular/plural "
        "or between እኛ/አንተ inside one text.\n"
        "- Address him warmly as 'ሄኖክ'. Be punchy, practical and specific. No clinical essays, no repeated apologies, "
        "no filler.\n"
        "- Telegram plain text: do NOT use markdown symbols (no **, no ##, no backticks, no tables). Use emojis, short "
        "lines and simple '•' bullets.\n"
        "- Never invent facts, numbers or quotes. If something is uncertain, say so briefly.\n"
        "- Never claim anything was published. Publishing only happens when Henok taps the approval button."
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


def clean_md(text):
    """Strip markdown the models sometimes add; messages here are plain text."""
    text = text or ""
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    return text.strip()


def took(start):
    secs = max(1, int(round(time.time() - start)))
    return f"⏱ የፈጀበት ጊዜ: {secs} ሰከንድ"


def has_any(text, words):
    low = (text or "").lower()
    for w in words:
        wl = w.lower()
        if wl.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(wl) + r"(?![a-z0-9])", low):
                return True
        elif wl in low:
            return True
    return False


def _norm_words(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def is_greeting(text):
    n = _norm_words(text)
    if not n:
        return False
    if n in GREET_PHRASES:
        return True
    parts = n.split()
    return len(parts) <= 3 and parts[0] in GREET_FIRST_WORDS


def is_thanks(text):
    return _norm_words(text) in THANKS_PHRASES


def greeting_reply():
    h = addis_now().hour
    if h < 12:
        g = "እንደምን አደርክ ሄኖክ! ☀️"
    elif h < 18:
        g = "እንደምን ዋልክ ሄኖክ! 👋"
    else:
        g = "እንደምን አመሸህ ሄኖክ! 🌙"
    return g + "\nዛሬ ምን ላግዝህ? 📰 /news · 📝 /post · 📊 /analytics ወይም ማንኛውንም ነገር ጻፍልኝ።"


def format_sources(sources, limit=6):
    if not sources:
        return ""
    lines = ["🔗 የተረጋገጡ ምንጮች:"]
    for i, (title, uri) in enumerate(sources[:limit], 1):
        lines.append(f"{i}. {title or 'ምንጭ'}\n{uri}")
    return "\n".join(lines)


def prune(store, max_items=25):
    with state_lock:
        if len(store) > max_items:
            for k in sorted(store, key=lambda k: store[k].get("ts", 0))[: len(store) - max_items]:
                store.pop(k, None)


def deliver(chat_id, ack_id, text, markup=None):
    """Edit the progress message (if any) with the first chunk, send the rest as new messages.
    The keyboard, if any, goes on the last chunk."""
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        mk = markup if i == len(chunks) - 1 else None
        if i == 0 and ack_id:
            try:
                bot.edit_message_text(chunk, chat_id, ack_id, reply_markup=mk, disable_web_page_preview=True)
                continue
            except Exception as e:
                log.warning("edit failed, sending new message: %s", e)
        bot.send_message(chat_id, chunk, reply_markup=mk, disable_web_page_preview=True)


def send_progress(chat_id, text):
    """Progress notice - used ONLY for heavy tasks (post + image, audio/video analysis)."""
    try:
        return bot.send_message(chat_id, text).message_id
    except Exception as e:
        log.error("progress message failed: %s", e)
        return None


def drop_message(chat_id, message_id):
    if not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def short_reason(e, limit=140):
    s = re.sub(r"\s+", " ", str(e))
    for secret in (FB_PAGE_ACCESS_TOKEN, GEMINI_API_KEY, OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN):
        if secret:
            s = s.replace(secret, "***")
    return s[:limit]


class Typing:
    """Shows the native 'typing...' indicator (no chat message) while a light task runs."""

    def __init__(self, chat_id, action="typing"):
        self.chat_id, self.action = chat_id, action
        self._stop = threading.Event()

    def __enter__(self):
        def loop():
            while not self._stop.is_set():
                try:
                    bot.send_chat_action(self.chat_id, self.action)
                except Exception:
                    pass
                self._stop.wait(4)

        threading.Thread(target=loop, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        return False


# ----------------------------------------------------------------------------
# Access control: PRIVATE CHAT WITH THE OWNER ONLY
# ----------------------------------------------------------------------------
def is_private(chat):
    return getattr(chat, "type", None) == "private"


def private_owner_only(func):
    """Silently ignore anything outside a private chat (groups, supergroups, channels) - even from the owner.
    In a private chat, strangers get one polite refusal."""

    @functools.wraps(func)
    def wrapper(message, *a, **kw):
        if not is_private(message.chat):
            return  # total silence
        user = getattr(message, "from_user", None)
        if user is None or user.id != OWNER_ID:
            try:
                bot.send_message(message.chat.id, "ይህ የሄኖክ የግል ቦት ነው 🔒")
            except Exception:
                pass
            return
        return func(message, *a, **kw)

    return wrapper


# ----------------------------------------------------------------------------
# Gemini (research with Google Search grounding + multimodal + text fallback)
# ----------------------------------------------------------------------------
class GeminiError(Exception):
    pass


def _gemini_call(model, payload, retries=3, timeout=GEMINI_TIMEOUT):
    url = f"{GEMINI_BASE}/models/{model}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    for attempt in range(1, retries + 1):
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2 * attempt)
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
    if cand.get("finishReason") == "MAX_TOKENS":
        log.warning("Gemini output hit MAX_TOKENS (text kept, may be truncated)")
    sources, seen = [], set()
    for ch in (cand.get("groundingMetadata") or {}).get("groundingChunks") or []:
        web = ch.get("web") or {}
        uri = web.get("uri")
        if uri and uri not in seen:
            seen.add(uri)
            sources.append((web.get("title") or "", uri))
    return text, sources


def gemini_generate(contents, system=None, grounding=False, temperature=0.8, max_tokens=8192):
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY missing")
    payload = {
        "systemInstruction": {"parts": [{"text": system or system_prompt()}]},
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if grounding:
        payload["tools"] = [{"google_search": {}}]
    last_err = None
    for model in GEMINI_MODELS:
        try:
            text, sources = _extract(_gemini_call(model, payload))
            return text, sources, model
        except Exception as e:
            last_err = e
            log.warning("Gemini model failed: %s", e)
    raise GeminiError(f"all Gemini models failed: {last_err}")


RESEARCH_PROMPT = (
    "Topic: {topic}\n\n"
    "Search the web and return concise ENGLISH research notes (max 320 words) with the most important, verified, "
    "recent facts about this topic: concrete numbers, dates, names, what changed and why it matters. "
    "Prefer reporting from top-tier outlets such as BBC Amharic, Reuters, AP, Fana, DW Amharic and Al Ain (or "
    "equally reputable international/business media); ignore rumours, anonymous blogs and unverified social posts. "
    "Bullet points only. After EACH fact put the outlet name in brackets, e.g. [Reuters]. Do not write Amharic. "
    "Do not invent anything; if you cannot verify something, leave it out."
)


def research(topic):
    """Live web facts via Gemini + Google Search. Returns (English notes, sources); ("", []) on failure."""
    try:
        text, sources, model = gemini_generate(
            [{"role": "user", "parts": [{"text": RESEARCH_PROMPT.replace("{topic}", topic)}]}],
            system="You are a precise news research assistant. Output plain-text English bullet notes only.",
            grounding=True, temperature=0.3, max_tokens=4096)
        log.info("research done by Gemini %s (%d sources)", model, len(sources))
        return clean_md(text), sources
    except Exception as e:
        log.warning("research failed: %s", e)
        return "", []


def gemini_upload_file(data, mime, display_name):
    """Gemini Files API (resumable) for media too big to send inline."""
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
        headers={"Content-Length": str(len(data)), "X-Goog-Upload-Offset": "0",
                 "X-Goog-Upload-Command": "upload, finalize"},
        data=data, timeout=300,
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


def multimodal_generate(parts, temperature=0.4, max_tokens=24000, thinking_budget=None):
    """Audio/video/image analysis. Large output budget (long Amharic transcripts need many tokens) and an optional
    thinking budget so 'thinking' cannot eat the whole output. If a model rejects the thinking config (HTTP 400),
    that model is retried once without it."""
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY missing")
    base = {
        "systemInstruction": {"parts": [{"text": system_prompt()}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    last_err = None
    for model in GEMINI_MODELS:
        variants = [True, False] if thinking_budget is not None else [False]
        for with_thinking in variants:
            payload = copy.deepcopy(base)
            if with_thinking:
                payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": thinking_budget}
            try:
                text, _ = _extract(_gemini_call(model, payload))
                return text
            except Exception as e:
                last_err = e
                log.warning("Multimodal %s (thinking=%s) failed: %s", model, with_thinking, e)
                if with_thinking and "HTTP 400" in str(e):
                    continue  # model does not accept this thinking config -> retry same model without it
                break
    raise GeminiError(f"all models failed for media: {last_err}")


# ----------------------------------------------------------------------------
# OpenRouter (primary writer) with Gemini fallback
# ----------------------------------------------------------------------------
def openrouter_chat(messages, max_tokens=1800, temperature=0.8):
    """Try each OpenRouter model in order. Returns (text, model) or (None, None).
    401/402 (bad key / out of credits) abort the whole provider immediately."""
    if not OPENROUTER_API_KEY:
        return None, None
    for model in OPENROUTER_MODELS:
        try:
            r = requests.post(
                f"{OPENROUTER_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://t.me/HenokAssistantBot",
                    "X-Title": "Henok Assistant",
                },
                json={"model": model, "messages": messages, "temperature": temperature,
                      "max_tokens": max_tokens},
                timeout=150,
            )
            if r.status_code in (401, 402):
                log.warning("OpenRouter %s (key/credits problem) - switching to Gemini", r.status_code)
                return None, None
            if r.status_code != 200:
                raise RuntimeError(f"{model} HTTP {r.status_code}: {r.text[:160]}")
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            txt = txt.strip()
            if not txt:
                raise RuntimeError(f"{model} returned empty text")
            return txt, model
        except Exception as e:
            log.warning("OpenRouter model failed: %s", e)
    return None, None


def llm_write(user_text, history=None, system=None, allow_gemini=True, max_tokens=1800, temperature=0.8):
    """Writer used for chat, news, posts, summaries: OpenRouter first, Gemini fallback.
    Returns (text, engine) or (None, None)."""
    history = history or []
    sys_prompt = system or system_prompt()
    messages = [{"role": "system", "content": sys_prompt}]
    for role, txt in history:
        messages.append({"role": "assistant" if role == "model" else "user", "content": txt})
    messages.append({"role": "user", "content": user_text})
    text, model = openrouter_chat(messages, max_tokens=max_tokens, temperature=temperature)
    if text:
        log.info("written by OpenRouter %s", model)
        return clean_md(text), f"openrouter:{model}"
    if allow_gemini:
        contents = [{"role": r, "parts": [{"text": t}]} for r, t in history]
        contents.append({"role": "user", "parts": [{"text": user_text}]})
        try:
            text, _, gmodel = gemini_generate(contents, system=sys_prompt, temperature=temperature,
                                              max_tokens=max(max_tokens, 4096))
            log.info("written by Gemini %s (fallback)", gmodel)
            return clean_md(text), f"gemini:{gmodel}"
        except Exception as e:
            log.error("Gemini fallback failed too: %s", e)
    return None, None


def polish_amharic(text):
    """Rewrite Gemini's Amharic analysis into native Amharic via OpenRouter. Returns the original on any failure."""
    if not OPENROUTER_API_KEY or not text:
        return text
    prompt = (
        "Rewrite the text below into flawless, natural, native Addis Ababa Amharic. Fix grammar, awkward phrasing "
        "and pronoun inconsistencies (speak to ሄኖክ in masculine singular: አንተ/ታደርጋለህ). KEEP every timestamp, "
        "number, score, hashtag, name and the structure/emojis exactly as they are. Do not add or remove facts. "
        "Plain text only, no markdown. Output only the rewritten text.\n\n" + text
    )
    out, _ = llm_write(prompt, allow_gemini=False, max_tokens=4000, temperature=0.4)
    if out and len(out) > len(text) * 0.5:
        return out
    return text


# ----------------------------------------------------------------------------
# Conversation (chat + live facts)
# ----------------------------------------------------------------------------
def answer(user_text, history=None, live=False):
    """Returns (reply, sources, engine). Live facts come from Gemini grounding, the writing from OpenRouter."""
    facts, sources = research(user_text) if live else ("", [])
    prompt = user_text
    if facts:
        prompt += ("\n\n[LIVE RESEARCH NOTES, verified with Google Search. Use ONLY these for time-sensitive facts "
                   "and numbers; never invent others]\n" + facts)
    elif live:
        prompt += "\n\n[Note: live web search was unavailable. Say briefly that figures may be outdated.]"
    text, engine = llm_write(prompt, history=history)
    if not text:
        return BUSY_TEXT, [], None
    return text, sources, engine


def remember(user_id, user_text, reply):
    with state_lock:
        h = histories.setdefault(user_id, [])
        h.append(("user", user_text[:1500]))
        h.append(("model", reply[:1500]))
        del h[: max(0, len(h) - MAX_HISTORY_TURNS * 2)]


# ----------------------------------------------------------------------------
# Interactive categories
# ----------------------------------------------------------------------------
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
            else "ሄኖክ፣ ለየትኛው ዘርፍ የሰከላ ኒውስ ፖስት ላዘጋጅልህ? 👇")
    bot.send_message(chat_id, head, reply_markup=category_keyboard(mode))


# ----------------------------------------------------------------------------
# News digest (for Henok to read; "make a post" turns it into a Sekela News card)
# ----------------------------------------------------------------------------
def run_news(chat_id, label, topic):
    start = time.time()
    try:
        with Typing(chat_id):
            facts, sources = research(topic)
            prompt = (
                f"ዘርፍ/ርዕስ: {topic}\n\n"
                "Write 4-5 punchy, verified insights for ሄኖክ based ONLY on the research notes below. Each insight: "
                "an emoji + one-line headline, then 1-2 energetic sentences on why it matters to an Ethiopian "
                "audience. Mention the outlet name inline. Finish with '🎯 ለዛሬ የሰከላ ኒውስ ሀሳብ:' and ONE concrete "
                "story he can publish. Plain text.\n\n"
            )
            if facts:
                prompt += "[RESEARCH NOTES]\n" + facts
            else:
                prompt += ("[Live search was unavailable. Give general insights from your own knowledge and say "
                           "clearly that they may not be the latest and are unverified.]")
            text, _ = llm_write(prompt)
        if not text:
            deliver(chat_id, None, BUSY_TEXT)
            return
        body = text
        kb = None
        if facts:  # only verified digests can become a publishable post
            nid = uuid.uuid4().hex[:10]
            with state_lock:
                news_cache[nid] = {"label": label, "text": facts, "sources": sources, "ts": time.time()}
            prune(news_cache)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("📰 የሰከላ ኒውስ ካርድ አዘጋጅልኝ", callback_data=f"mk:{nid}"))
        src = format_sources(sources)
        if src:
            body += "\n\n" + src
        body += "\n\n" + took(start)
        deliver(chat_id, None, body, kb)
    except Exception as e:
        log.error("news failed: %s", e)
        deliver(chat_id, None, "ሄኖክ፣ ዜናውን አሁን ማምጣት አልቻልኩም 😅 ጥቂት ቆይተህ እንደገና ሞክር።")


# ----------------------------------------------------------------------------
# Sekela News copy: parsing, sourcing, branding
# ----------------------------------------------------------------------------
SEKELA_BRIEF = (
    "You are the editor of 'ሰከላ ኒውስ (Sekela News)', an Amharic news brand for Facebook and Telegram mobile feeds. "
    "Editorial style: sharp, fast, authoritative and easy to scan.\n"
    "Topic: {topic}\n\n[RESEARCH NOTES]\n{facts}\n\n"
    "Write ONE post using ONLY facts found in the research notes (no invented numbers, names or quotes).\n"
    "Structure:\n"
    "- Paragraph 1: an undeniable hook, 1-2 punchy sentences opening with the most striking verified fact "
    "(one fitting emoji at the start is fine).\n"
    "- Then 4-5 concise paragraphs, 1-2 short sentences each, separated by blank lines: what happened, the key "
    "numbers/names, why it matters to Ethiopian readers (only if the notes support it), what comes next.\n"
    "- Then ONE line: ምንጭ፡ <outlet name> using only outlets that appear in the notes (e.g. BBC Amharic, Reuters, AP, "
    "Fana, DW Amharic, Al Ain).\n"
    "- Last line: hashtags: #ሰከላ_ኒውስ #SekelaNews #HenokGraphics plus 2-4 specific story hashtags.\n"
    "Voice: neutral, native Addis Ababa news Amharic; ONE consistent way of addressing readers; no opinion stated as "
    "fact; no sensational claim the notes do not support. Keep English terms (AI, CapCut, Whop...) in Latin letters.\n\n"
    "Return EXACTLY these three blocks and nothing else:\n"
    "===HEADLINE===\n5-7 punchy Amharic words for the poster headline; no quotes, no emoji, no full stop.\n"
    "===POST===\n(the post as specified above)\n"
    "===IMAGE===\nOne English paragraph, 25-45 words: a symbolic, cinematic photojournalism scene that visualizes the "
    "story's theme (setting, subject matter, lighting, lens, mood, high-contrast colour grade). No identifiable real "
    "people, no text, no logos, no flags, no depiction of violence or casualties.\n"
)

IMAGE_STYLE = ("high-contrast cinematic news photography, dramatic lighting, shallow depth of field, ultra detailed, "
               "sharp focus, dark moody tones, square composition, no text, no letters, no watermark")
IMAGE_FALLBACK = ("a dramatic dusk skyline of a modern African capital with glowing city lights and a newsroom "
                  "feel, teal and orange colour grade")


def parse_package(text):
    """Split the writer output into {'headline','post','image'}. Tolerant to missing blocks."""
    out = {"headline": "", "post": "", "image": ""}
    parts = re.split(r"===(HEADLINE|POST|CANVA|IMAGE)===", text or "")
    if len(parts) < 3:
        out["post"] = (text or "").strip()
        return out
    for i in range(1, len(parts) - 1, 2):
        key = parts[i].lower()
        if key in out:
            out[key] = parts[i + 1].strip()
    if not out["post"]:
        out["post"] = (parts[0] or text).strip()
    return out


def clean_headline(h, fallback_text=""):
    """Max 7 words, no quotes / leading emoji / trailing full stop."""
    def _tidy(s):
        s = re.sub(r"[\"'“”‘’«»]", "", s or "").strip()
        s = re.sub(r"^[^\wሀ-፿]+", "", s)
        return s.rstrip(".።").strip()

    h = _tidy(h)
    if not h:
        first = next((ln for ln in (fallback_text or "").splitlines() if ln.strip()), "")
        h = _tidy(first)
    return " ".join(h.split()[:7])


def detect_outlets(text):
    low = (text or "").lower()
    found = [name for name, rx in OUTLETS if re.search(rx, low)]
    if "BBC Amharic" in found and "BBC" in found:
        found.remove("BBC")
    if "DW Amharic" in found and "DW" in found:
        found.remove("DW")
    return found


def derive_source(facts, sources):
    blob = (facts or "") + " " + " ".join(f"{t} {u}" for t, u in (sources or []))
    outlets = detect_outlets(blob)
    if outlets:
        return ", ".join(outlets[:3])
    if sources:
        title = sources[0][0]
        return title or urlparse(sources[0][1]).netloc or None
    return None


_HASHTAG_LINE = re.compile(r"^\s*(?:#\S+\s*)+$")
_SOURCE_LINE = re.compile(r"^\s*(?:📰\s*|📌\s*)?ምንጭ\s*[:፡]\s*(.+?)\s*$")


def finalize_post(raw, facts="", sources=None):
    """Normalize the writer's post into: body + 'ምንጭ፡ <outlet>' + hashtags (brand tags always first).
    Returns None when no source can be established (a news post must never go out unsourced)."""
    tags, src, body = [], None, []
    for ln in (raw or "").strip().splitlines():
        if _HASHTAG_LINE.match(ln):
            tags += re.findall(r"#\S+", ln)
            continue
        m = _SOURCE_LINE.match(ln)
        if m and src is None:
            src = m.group(1).strip()
            continue
        body.append(ln)
    body_text = re.sub(r"\n{3,}", "\n\n", "\n".join(body)).strip()
    if not detect_outlets(src or ""):
        derived = derive_source(facts, sources)
        if derived and (detect_outlets(derived) or not src):
            src = derived
    if not src or not body_text:
        return None
    seen = {t.lower() for t in BRAND_TAGS}
    story = []
    for t in tags:
        t = t.rstrip(".,;")
        if t and t.lower() not in seen:
            seen.add(t.lower())
            story.append(t)
    return f"{body_text}\n\nምንጭ፡ {src}\n\n" + " ".join(BRAND_TAGS + story[:5])


# ----------------------------------------------------------------------------
# Sekela News card (Pollinations background + Pillow overlay)
# ----------------------------------------------------------------------------
def build_image_prompt(scene):
    scene = re.sub(r"\s+", " ", (scene or "")).strip()
    if not scene or re.search(r"[ሀ-፿]", scene):  # empty or accidentally Amharic -> safe default
        scene = IMAGE_FALLBACK
    return (scene[:420].rstrip(" ,.") + ", " + IMAGE_STYLE)[:640]


def pollinations_url(prompt):
    return (f"https://image.pollinations.ai/prompt/{quote(prompt, safe='')}"
            f"?width=1080&height=1080&nologo=true&seed={int(time.time())}")


def fetch_image(prompt, attempts=3):
    """Download the AI background bytes. Returns bytes or None."""
    url = pollinations_url(prompt)
    for i in range(attempts):
        try:
            r = requests.get(url, timeout=110)
            ctype = r.headers.get("Content-Type", "")
            if r.status_code == 200 and ctype.startswith("image/") and len(r.content) > 2000:
                return r.content
            log.warning("Pollinations attempt %d: HTTP %s %s", i + 1, r.status_code, ctype)
        except Exception as e:
            log.warning("Pollinations attempt %d failed: %s", i + 1, e)
        time.sleep(3)
    return None


FONT_URLS = [
    "https://cdn.jsdelivr.net/gh/notofonts/ethiopic@main/fonts/NotoSansEthiopic/hinted/ttf/NotoSansEthiopic-Bold.ttf",
    "https://github.com/notofonts/ethiopic/raw/main/fonts/NotoSansEthiopic/hinted/ttf/NotoSansEthiopic-Bold.ttf",
    "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansEthiopic/NotoSansEthiopic-Bold.ttf",
]
_font_cache = {}
_font_lock = threading.Lock()


def _font_loadable(path):
    try:
        ImageFont.truetype(path, 40)
        return True
    except Exception:
        return False


def find_ethiopic_font():
    """Path of a bold Ethiopic font, or None. Order: FONT_PATH env, files next to agent.py (commit
    NotoSansEthiopic-Bold.ttf to the repo for 100% reliability), system fonts, previously downloaded copy, download."""
    if not PIL_OK:
        return None
    with _font_lock:
        if "eth" in _font_cache:
            return _font_cache["eth"]
        here = os.path.dirname(os.path.abspath(__file__))
        cached = os.path.join(tempfile.gettempdir(), "sekela-NotoSansEthiopic-Bold.ttf")
        cands = [os.environ.get("FONT_PATH", "").strip()]
        for pat in ("*Ethiopic*Bold*.[to]tf", "*Ethiopic*.[to]tf", "fonts/*Ethiopic*.[to]tf"):
            cands += sorted(glob.glob(os.path.join(here, pat)))
        for pat in ("**/*Ethiopic*Bold*.[to]tf", "**/*Ethiopic*.[to]tf", "**/*Abyssinica*.[to]tf"):
            cands += sorted(glob.glob("/usr/share/fonts/" + pat, recursive=True))
        cands.append(cached)
        for p in cands:
            if p and os.path.isfile(p) and _font_loadable(p):
                _font_cache["eth"] = p
                return p
        for url in FONT_URLS:
            try:
                r = requests.get(url, timeout=60)
                if r.status_code == 200 and len(r.content) > 20000 and r.content[:4] in (
                        b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"):
                    with open(cached, "wb") as f:
                        f.write(r.content)
                    if _font_loadable(cached):
                        log.info("Ethiopic font downloaded from %s", url)
                        _font_cache["eth"] = cached
                        return cached
            except Exception as e:
                log.warning("font download failed (%s): %s", url, e)
        log.warning("No Ethiopic font available - cards will be sent without the baked headline")
        _font_cache["eth"] = None
        return None


def _latin_font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"):
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1 (scalable)
    except Exception:
        return ImageFont.load_default()


def _is_eth(ch):
    o = ord(ch)
    return 0x1200 <= o <= 0x139F or 0x2D80 <= o <= 0x2DDF or 0xAB00 <= o <= 0xAB2F


def _runs(text, f_eth, f_lat):
    """Split into (substring, font) runs: Ethiopic chars use the Ethiopic font, everything else the Latin font
    (Noto Sans Ethiopic has no Latin glyphs). Spaces follow the previous run."""
    runs, cur, cur_font = [], "", None
    for ch in text:
        if _is_eth(ch):
            font = f_eth
        elif ch.isspace() and cur_font is not None:
            font = cur_font
        else:
            font = f_lat
        if font is not cur_font and cur:
            runs.append((cur, cur_font))
            cur = ""
        cur += ch
        cur_font = font
    if cur:
        runs.append((cur, cur_font))
    return runs


def _measure(text, f_eth, f_lat):
    return sum(font.getlength(s) for s, font in _runs(text, f_eth, f_lat))


def _draw_mixed(draw, x, baseline, text, f_eth, f_lat, fill):
    for s, font in _runs(text, f_eth, f_lat):
        draw.text((x, baseline), s, font=font, fill=fill, anchor="ls")
        x += font.getlength(s)


def _wrap(text, f_eth, f_lat, max_w):
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if cur and _measure(trial, f_eth, f_lat) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def compose_card(bg_bytes, headline, size=1080):
    """1080x1080 Sekela News poster: cropped background, dark bottom gradient, bold headline, brand badge.
    Returns JPEG bytes, or None when Pillow / an Ethiopic font is unavailable or anything fails."""
    if not PIL_OK or not bg_bytes or not headline:
        return None
    eth_path = find_ethiopic_font()
    if not eth_path:
        return None
    try:
        img = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
        w, h = img.size
        s = min(w, h)
        left, top = (w - s) // 2, (h - s) // 2
        img = img.crop((left, top, left + s, top + s)).resize((size, size), Image.LANCZOS)
        img = ImageEnhance.Contrast(img).enhance(1.12)

        overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        g0 = int(size * 0.28)
        for y in range(g0, size):
            t = (y - g0) / (size - g0)
            od.line([(0, y), (size, y)], fill=(0, 0, 0, int(246 * (t ** 1.35))))
        img = Image.alpha_composite(img.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(img, "RGBA")

        margin, max_w = 64, size - 128
        fs = 112
        lines = []
        for fs in range(112, 47, -4):
            f_eth, f_lat = ImageFont.truetype(eth_path, fs), _latin_font(fs)
            lines = _wrap(headline, f_eth, f_lat, max_w)
            if len(lines) <= 4 and all(_measure(ln, f_eth, f_lat) <= max_w for ln in lines):
                break
        line_h = int(fs * 1.22)
        badge_h = 64
        badge_y = size - margin - badge_h
        y0 = badge_y - 44 - line_h * len(lines)
        draw.rectangle([margin, y0 - 34, margin + 132, y0 - 24], fill=BRAND_RED + (255,))
        for i, ln in enumerate(lines):
            _draw_mixed(draw, margin, y0 + i * line_h + int(fs * 0.98), ln, f_eth, f_lat, (255, 255, 255, 255))

        # signature badge  "ሰከላ | Sekela"
        b_eth, b_lat = ImageFont.truetype(eth_path, 36), _latin_font(34)
        badge_text = "ሰከላ | Sekela"
        bw = int(_measure(badge_text, b_eth, b_lat)) + 56
        draw.rounded_rectangle([margin, badge_y, margin + bw, badge_y + badge_h], radius=32, fill=BRAND_RED + (255,))
        _draw_mixed(draw, margin + 28, badge_y + 44, badge_text, b_eth, b_lat, (255, 255, 255, 255))

        if AI_LABEL:
            t_font = _latin_font(22)
            label = "AI illustration"
            lw = int(t_font.getlength(label)) + 28
            draw.rounded_rectangle([size - margin - lw + 20, 36, size - 44, 36 + 38], radius=19, fill=(0, 0, 0, 120))
            draw.text((size - margin - lw + 34, 36 + 28), label, font=t_font, fill=(255, 255, 255, 215), anchor="ls")

        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
        return out.getvalue()
    except Exception as e:
        log.error("card composition failed: %s", e)
        return None


def _as_photo(data):
    bio = io.BytesIO(data)
    bio.name = "sekela-card.jpg"
    return bio


# ----------------------------------------------------------------------------
# Facebook Page (publish + analytics). NO COMMENT REPLIES - ever.
# ----------------------------------------------------------------------------
class FBError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def fb_enabled():
    return bool(FB_PAGE_ID and FB_PAGE_ACCESS_TOKEN)


def _fb_clean(s):
    s = str(s)
    return s.replace(FB_PAGE_ACCESS_TOKEN, "***") if FB_PAGE_ACCESS_TOKEN else s


def _fb_request(method, path, params=None, data=None, files=None, timeout=90):
    """Single choke point for Graph API calls. Hard-coded policy: only POST to /photos and /feed, and refuse any
    path that touches comments (the bot never auto-replies or writes to comments)."""
    p = path.strip("/").lower()
    if "comment" in p:
        raise FBError("blocked by no-commenting policy")
    if method == "POST" and p.split("/")[-1] not in ("photos", "feed"):
        raise FBError(f"blocked: POST not allowed on {p}")
    url = f"{FB_GRAPH}/{path.strip('/')}"
    try:
        if method == "POST":
            body = dict(data or {})
            body["access_token"] = FB_PAGE_ACCESS_TOKEN
            r = requests.post(url, data=body, files=files, timeout=timeout)
        else:
            q = dict(params or {})
            q["access_token"] = FB_PAGE_ACCESS_TOKEN
            r = requests.get(url, params=q, timeout=timeout)
    except requests.RequestException as e:
        raise FBError(_fb_clean(f"network error: {e}"))
    try:
        js = r.json()
    except Exception:
        js = {}
    if r.status_code != 200 or "error" in js:
        err = js.get("error", {}) if isinstance(js, dict) else {}
        raise FBError(_fb_clean(err.get("message") or f"HTTP {r.status_code}"), err.get("code"))
    return js


def fb_publish(post, image_bytes=None):
    """Photo with caption when an image exists, otherwise a plain feed post. Returns the Graph object id."""
    if image_bytes:
        js = _fb_request("POST", f"{FB_PAGE_ID}/photos", data={"caption": post, "published": "true"},
                         files={"source": ("sekela-card.jpg", image_bytes, "image/jpeg")}, timeout=120)
    else:
        js = _fb_request("POST", f"{FB_PAGE_ID}/feed", data={"message": post})
    return js.get("post_id") or js.get("id")


def fb_error_hint(e):
    code = getattr(e, "code", None)
    if code == 190:
        return "ቶክኑ ጊዜው አልፏል ወይም ልክ አይደለም — አዲስ Page Access Token ፍጠር።"
    if code in (10, 200, 299):
        return "ፈቃድ ይጎድላል — Page token በ pages_manage_posts ፈቃድ መፈጠሩን አረጋግጥ።"
    if code == 100:
        return "FB_PAGE_ID ወይም የጥያቄው መረጃ ልክ አይደለም።"
    return ""


FB_INSIGHT_CANDIDATES = [
    ("page_total_media_view_unique", "👀 Reach (ልዩ ተመልካቾች)"),
    ("page_media_view", "📈 Views (እይታዎች)"),
    ("page_post_engagements", "💬 Engagement (ተሳትፎ)"),
    ("page_daily_follows", "➕ አዲስ ተከታዮች"),
]


def _sum_values(js):
    total = 0
    for item in js.get("data", [])[:1]:
        for v in item.get("values", []):
            val = v.get("value")
            if isinstance(val, (int, float)):
                total += val
            elif isinstance(val, dict):
                total += sum(x for x in val.values() if isinstance(x, (int, float)))
    return int(total)


def fb_analytics_text():
    if not fb_enabled():
        return "⚪ ፌስቡክ አልተዋቀረም — በ Render ላይ FB_PAGE_ACCESS_TOKEN ጨምር።"
    lines = [f"📊 {FB_PAGE_NAME} — Facebook Analytics", "━━━━━━━━━━━━"]
    try:
        info = _fb_request("GET", "me", params={"fields": "name,id"}, timeout=40)
        followers = info.get("followers_count", info.get("fan_count"))
        lines.append(f"📄 ገጽ: {info.get('name', FB_PAGE_NAME)}")
        lines.append(f"👥 ተከታዮች: {followers:,}" if isinstance(followers, int) else "👥 ተከታዮች: —")
    except FBError as e:
        return "\n".join(lines + [f"❌ የገጹን መረጃ ማምጣት አልተቻለም: {short_reason(e)}", fb_error_hint(e)]).strip()

    now = int(time.time())
    got, tried = [], 0
    for metric, label in FB_INSIGHT_CANDIDATES:
        tried += 1
        try:
            js = _fb_request("GET", f"{FB_PAGE_ID}/insights",
                             params={"metric": metric, "period": "day", "since": now - 7 * 86400, "until": now},
                             timeout=40)
            got.append(f"{label}: {_sum_values(js):,}")
        except FBError as e:
            log.info("insight %s unavailable: %s", metric, e)
    lines.append("\n🗓 ባለፉት 7 ቀናት:")
    lines += got if got else ["• Insights ማግኘት አልተቻለም (read_insights ፈቃድ ወይም የ Meta metric ለውጥ ሊሆን ይችላል)።"]

    lines.append("\n📰 የቅርብ ጊዜ ፖስቶች:")
    try:
        posts = _fb_request(
            "GET", f"{FB_PAGE_ID}/posts",
            params={"fields": "message,created_time,permalink_url,reactions.summary(total_count).limit(0),"
                              "comments.summary(total_count).limit(0),shares", "limit": 5}, timeout=40).get("data", [])
        if not posts:
            lines.append("• ፖስት አልተገኘም")
        for p in posts:
            msg = re.sub(r"\s+", " ", p.get("message", "") or "(ያለ ጽሑፍ)")[:55]
            react = ((p.get("reactions") or {}).get("summary") or {}).get("total_count", 0)
            comm = ((p.get("comments") or {}).get("summary") or {}).get("total_count", 0)
            shares = (p.get("shares") or {}).get("count", 0)
            lines.append(f"• {p.get('created_time', '')[:10]} — {msg}\n   ❤️ {react} · 💬 {comm} · 🔁 {shares}")
    except FBError as e:
        lines.append(f"• ማምጣት አልተቻለም: {short_reason(e)}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Post package: Sekela News card + copy -> preview -> approval -> Telegram + Facebook
# ----------------------------------------------------------------------------
def run_post(chat_id, label, topic, context_text=None, context_sources=None):
    start = time.time()
    ack_id = send_progress(
        chat_id, "⏳ ሄኖክ፣ ጥናቱን አድርጌ የሰከላ ኒውስ ፖስቱን እና AI ምስሉን እያዘጋጀሁ ነው (ከ20-40 ሰከንድ ይወስዳል)...")
    try:
        if context_text:
            facts, sources = context_text, context_sources or []
        else:
            facts, sources = research(topic)
        if not facts:
            facts = f"Major verified industry news, updates and strategic developments concerning {topic}."
            if not sources:
                sources = [("ሰከላ ኒውስ (Sekela News)", "https://facebook.com/1252817724577069")]
        brief = SEKELA_BRIEF.replace("{topic}", topic).replace("{facts}", facts)
        text, _ = llm_write(brief, max_tokens=2600)
        if not text:
            deliver(chat_id, ack_id, BUSY_TEXT)
            return
        pkg = parse_package(text)
        final = finalize_post(pkg["post"] or text, facts, sources)
        if not final:
            deliver(chat_id, ack_id, "ሄኖክ፣ ለፖስቱ ምንጭ ማረጋገጥ አልቻልኩም 😕 ያለ ምንጭ አልለጥፍም — እንደገና ሞክር።")
            return
        headline = clean_headline(pkg["headline"], final)
        bg = fetch_image(build_image_prompt(pkg["image"]))
        card = compose_card(bg, headline) if bg else None
        image = card or bg
        post = final if (card or not bg) else f"📰 {headline}\n\n{final}"

        # --- preview to Henok: the card (+ caption if it fits), then details + approval buttons
        file_id = None
        caption_fits = len(post) <= CAPTION_LIMIT
        if image:
            try:
                sent = bot.send_photo(chat_id, _as_photo(image),
                                      caption=post if caption_fits else f"📰 {headline}")
                file_id = sent.photo[-1].file_id
            except Exception as e:
                log.error("sending preview photo failed: %s", e)

        did = uuid.uuid4().hex[:10]
        with state_lock:
            drafts[did] = {"post": post, "file_id": file_id, "image": image, "headline": headline,
                           "tg": False, "fb": False, "busy": False, "ts": time.time()}
        prune(drafts)

        details = []
        if not (file_id and caption_fits):
            details.append(f"📝 ረቂቅ — {label}\n━━━━━━━━━━━━\n{post}")
        else:
            details.append(f"📝 የሰከላ ኒውስ ረቂቅ ዝግጁ ነው — {label}")
        details.append(f"📰 የካርዱ ርዕስ: {headline}")
        if not image:
            details.append("⚠️ ምስሉ በዚህ ጊዜ አልተሳካም — ያለ ምስል መለጠፍ ወይም ሰርዘህ እንደገና መጠየቅ ትችላለህ።")
        elif not card:
            details.append("⚠️ ርዕሱን በምስሉ ላይ ማተም አልተቻለም (Ethiopic font የለም) — ርዕሱ በፖስቱ ጽሑፍ ውስጥ ገብቷል። "
                           "NotoSansEthiopic-Bold.ttf ን ከ agent.py ጎን ኮሚት አድርግ።")
        src = format_sources(sources)
        if src:
            details.append(src)
        targets = [str(TARGET_CHANNEL)] + ([FB_PAGE_NAME + " (Facebook)"] if fb_enabled() else [])
        details.append("━━━━━━━━━━━━\n🔒 ✅ ከነካህ ብቻ ይለጠፋል → " + " + ".join(targets) +
                       "\nበፌስቡክ ኮመንቶች ላይ ቦቱ ፈጽሞ አይመልስም።")
        details.append(took(start))
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ አጽድቅና ፖስት አድርግ", callback_data=f"ok:{did}"),
            types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"no:{did}"),
        )
        deliver(chat_id, None, "\n\n".join(details), kb)
        drop_message(chat_id, ack_id)
    except Exception as e:
        log.error("post failed: %s", e)
        deliver(chat_id, ack_id, "ሄኖክ፣ ፖስቱን አሁን ማዘጋጀት አልቻልኩም 😅 ጥቂት ቆይተህ እንደገና ሞክር።")


def publish_telegram(d):
    """Photo + caption (or text) to the target channel."""
    post = d["post"]
    photo = d.get("file_id") or (_as_photo(d["image"]) if d.get("image") else None)
    if photo:
        if len(post) <= CAPTION_LIMIT:
            bot.send_photo(TARGET_CHANNEL, photo, caption=post)
        else:
            bot.send_photo(TARGET_CHANNEL, photo)
            for chunk in split_text(post, 4000):
                bot.send_message(TARGET_CHANNEL, chunk)
    else:
        for chunk in split_text(post, 4000):
            bot.send_message(TARGET_CHANNEL, chunk)


def retry_keyboard(did):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🔁 ያልተሳካውን እንደገና ሞክር", callback_data=f"ok:{did}"),
        types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"no:{did}"),
    )
    return kb


def approve_and_publish(chat_id, msg_id, did):
    """Approval gate: Telegram first, then Facebook. Each target is tracked separately so a retry never
    double-posts to a target that already succeeded."""
    with state_lock:
        d = drafts.get(did)
        if d is None:
            expired = True
        else:
            expired = False
            if d["busy"]:
                return
            d["busy"] = True
    if expired:
        bot.edit_message_text("⚠️ ይህ ረቂቅ ጊዜው አልፏል ወይም ቀድሞ ተስተናግዷል።", chat_id, msg_id)
        return

    tg_err = fb_err = None
    fb_on = fb_enabled()
    try:
        if not d["tg"]:
            try:
                publish_telegram(d)
                d["tg"] = True
            except Exception as e:
                log.error("telegram publish failed: %s", e)
                tg_err = short_reason(e, 200)
        if fb_on and not d["fb"]:
            try:
                fb_publish(d["post"], d.get("image"))
                d["fb"] = True
            except FBError as e:
                log.error("facebook publish failed: %s", e)
                fb_err = short_reason(e, 200) + (" — " + fb_error_hint(e) if fb_error_hint(e) else "")
            except Exception as e:
                log.error("facebook publish failed: %s", e)
                fb_err = short_reason(e, 200)
    finally:
        d["busy"] = False

    tg_line = f"• Telegram: {TARGET_CHANNEL} " + ("✅" if d["tg"] else f"❌ {tg_err or ''}".rstrip())
    if not fb_on:
        fb_line = f"• Facebook: {FB_PAGE_NAME} ⚪ አልተዋቀረም (FB_PAGE_ACCESS_TOKEN)"
    else:
        fb_line = f"• Facebook: {FB_PAGE_NAME} " + ("✅" if d["fb"] else f"❌ {fb_err or ''}".rstrip())

    if d["tg"] and (d["fb"] or not fb_on):
        with state_lock:
            drafts.pop(did, None)
        bot.edit_message_text(f"✅ ጸድቋል እና ተለጥፏል! 🚀\n{tg_line}\n{fb_line}", chat_id, msg_id)
    else:
        extra = ""
        if tg_err and "chat not found" in tg_err.lower():
            extra = ("\n\n💡 ቻናሉ public መሆኑን፣ TARGET_CHANNEL በትክክል መጻፉን እና ቦቱ አድሚን ('Post Messages') "
                     "መሆኑን አረጋግጥ — /ai_status ያሳይሃል።")
        bot.edit_message_text(f"⚠️ በከፊል ተሳክቷል\n{tg_line}\n{fb_line}{extra}\n\nየተሳካው እንደገና አይለጠፍም።",
                              chat_id, msg_id, reply_markup=retry_keyboard(did))


# ----------------------------------------------------------------------------
# Audio / video / photo analysis
# ----------------------------------------------------------------------------
TRANSCRIBE_PROMPT = (
    "Transcribe this audio faithfully and completely, word for word, from the first second to the last. "
    "Write Amharic in Ethiopic script (keep English words in Latin letters). If several people speak, start a new "
    "line for each speaker change. Mark unclear parts as [...]. Do not summarize, translate, comment or repeat "
    "yourself. Output ONLY the transcript."
)
SUMMARY_PROMPT = (
    "Below is a verbatim transcript of a voice note from ሄኖክ (it may contain small transcription errors). "
    "Write in natural Amharic, using ONLY what is in the transcript:\n"
    "📌 ማጠቃለያ: 3-6 lines with the key message.\n"
    "🔑 ዋና ዋና ነጥቦች: short • bullets.\n"
    "✅ Action Items: a checklist of tasks/decisions, each with the deadline or owner if mentioned.\n"
    "❓ ግልጽ ያልሆኑ ነገሮች: only if something important is unclear (otherwise omit this line).\n"
    "Plain text, no markdown.\n\n[TRANSCRIPT]\n"
)
VIDEO_PROMPT = (
    "Analyze this video for Henok (video producer who clips for Whop / TikTok / Shorts). Write in natural "
    "Amharic (keep English terms like Hook, CTA, CapCut as they are). Provide:\n"
    "1) 🎬 Scene-by-scene breakdown with timestamps (MM:SS).\n"
    "2) 📌 Short summary.\n"
    "3) 🔥 THREE viral clip candidates, each 30-45 seconds long. For each: ⏱ exact start-end (MM:SS-MM:SS, "
    "duration 30-45s), 🪝 Hook caption for the first 2 seconds, 📈 Virality score X/10 with one line of why, "
    "✂️ one editing tip, #️⃣ 3-5 hashtags.\n"
    "Rank the clips from best to worst. Only use moments that really exist in the video. Plain text, no markdown."
)
PHOTO_PROMPT = (
    "Analyze this image for Henok's Canva design work. Natural Amharic. Give: 1) 🖼 what you see, 2) composition & "
    "layout (focal point, balance, empty space), 3) dominant colors as HEX codes + a matching 3-4 color palette, "
    "4) strengths, 5) weaknesses, 6) 4-6 concrete Canva improvement steps. Plain text, no markdown."
)


def normalize_audio_mime(mime):
    m = (mime or "audio/ogg").split(";")[0].strip().lower()
    return {"audio/mpeg": "audio/mp3", "audio/x-m4a": "audio/aac", "audio/m4a": "audio/aac",
            "audio/mp4": "audio/aac", "audio/x-wav": "audio/wav", "application/ogg": "audio/ogg",
            "audio/opus": "audio/ogg"}.get(m, m)


def detect_media(message):
    """Return (kind, file_id, mime, size, duration_seconds) or None."""
    ct = message.content_type
    if ct == "voice":
        v = message.voice
        return "audio", v.file_id, v.mime_type or "audio/ogg", v.file_size or 0, getattr(v, "duration", 0) or 0
    if ct == "audio":
        a = message.audio
        return "audio", a.file_id, a.mime_type or "audio/mpeg", a.file_size or 0, getattr(a, "duration", 0) or 0
    if ct == "video":
        v = message.video
        return "video", v.file_id, v.mime_type or "video/mp4", v.file_size or 0, getattr(v, "duration", 0) or 0
    if ct == "video_note":
        v = message.video_note
        return "video", v.file_id, "video/mp4", v.file_size or 0, getattr(v, "duration", 0) or 0
    if ct == "photo":
        p = message.photo[-1]
        return "photo", p.file_id, "image/jpeg", p.file_size or 0, 0
    if ct == "document":
        d = message.document
        mime = (d.mime_type or "").lower()
        if mime.startswith("image/"):
            return "photo", d.file_id, mime, d.file_size or 0, 0
        if mime.startswith("video/"):
            return "video", d.file_id, mime, d.file_size or 0, 0
        if mime.startswith("audio/"):
            return "audio", d.file_id, mime, d.file_size or 0, 0
    return None


def media_eta(kind, size, duration=0):
    if kind == "audio":
        if duration and duration > 180:
            return "ከ40-90 ሰከንድ"
        if duration and duration > 60:
            return "ከ25-50 ሰከንድ"
        return "ከ10-25 ሰከንድ"
    if kind == "video":
        return "ከ30-50 ሰከንድ" if size < 6 * 1024 * 1024 else "ከ45-90 ሰከንድ"
    return ""


def _media_part(data, mime, kind):
    if len(data) > INLINE_LIMIT_BYTES:
        uri = gemini_upload_file(data, mime, f"{kind}-{uuid.uuid4().hex[:6]}")
        return {"fileData": {"mimeType": mime, "fileUri": uri}}
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}}


def analyze_voice(data, mime):
    """Step 1 (Gemini): faithful transcript, thinking disabled/limited and a big output budget so 3-5 minute Amharic
    voice notes cannot run out of tokens. Step 2 (OpenRouter writer): summary + action items from the transcript."""
    part = _media_part(data, normalize_audio_mime(mime), "audio")
    transcript = clean_md(multimodal_generate([{"text": TRANSCRIBE_PROMPT}, part], temperature=0.1,
                                              max_tokens=32000, thinking_budget=0))
    analysis, _ = llm_write(SUMMARY_PROMPT + transcript, max_tokens=2500, temperature=0.3)
    if not analysis:
        analysis = "⚠️ ማጠቃለያውን አሁን ማዘጋጀት አልተቻለም — ሙሉ ትራንስክሪፕቱ ከታች ነው።"
    return analysis + "\n\n━━━━━━━━━━━━\n📝 ሙሉ ትራንስክሪፕት:\n" + transcript


@bot.message_handler(content_types=["voice", "audio", "video", "video_note", "photo", "document"])
@private_owner_only
def handle_media(message):
    start = time.time()
    chat_id = message.chat.id
    info = detect_media(message)
    if not info:
        bot.send_message(chat_id, "ሄኖክ፣ ይህን የፋይል አይነት ገና አልተረዳሁም 🙈 ድምፅ፣ ቪዲዮ ወይም ፎቶ ላክልኝ።")
        return
    kind, file_id, mime, size, duration = info
    if size and size > MAX_MEDIA_BYTES:
        bot.send_message(chat_id, f"ሄኖክ፣ ፋይሉ {size / 1048576:.1f}MB ነው — እስከ 18MB ብቻ ነው የምተነትነው። "
                                  "አሳጥረህ ወይም compress አድርገህ ላክልኝ 🎞")
        return
    ack_id = None
    if kind in ("audio", "video"):
        what = "ድምፁን" if kind == "audio" else "ቪዲዮውን"
        ack_id = send_progress(chat_id, f"⏳ ሄኖክ፣ {what} እየተነተንኩ ነው ({media_eta(kind, size, duration)} ይወስዳል)...")
    try:
        with Typing(chat_id):
            fobj = bot.get_file(file_id)
            data = bot.download_file(fobj.file_path)
            if len(data) > MAX_MEDIA_BYTES:
                deliver(chat_id, ack_id, "ሄኖክ፣ ፋይሉ ከ18MB በላይ ነው — አሳጥረህ ላክልኝ 🎞")
                return
            caption = (message.caption or "").strip()
            if kind == "audio":
                result = analyze_voice(data, mime)
                if caption:
                    result = f"📎 ማስታወሻህ: {caption}\n\n" + result
            else:
                prompt = VIDEO_PROMPT if kind == "video" else PHOTO_PROMPT
                if caption:
                    prompt += f"\n\nHenok's extra instruction/context: {caption}"
                raw = multimodal_generate([{"text": prompt}, _media_part(data, mime, kind)],
                                          temperature=0.5, max_tokens=16000, thinking_budget=1024)
                result = polish_amharic(clean_md(raw))
        deliver(chat_id, ack_id, result + "\n\n" + took(start))
    except Exception as e:
        log.error("media handling failed: %s", e)
        what = {"audio": "ድምፁን", "video": "ቪዲዮውን", "photo": "ፎቶውን"}.get(kind, "ፋይሉን")
        deliver(chat_id, ack_id,
                f"ሄኖክ፣ {what} አሁን ማጠናቀቅ አልቻልኩም 😅\n(ምክንያት: {short_reason(e)})\n"
                "ጥቂት ቆይተህ እንደገና ላክልኝ።")


# ----------------------------------------------------------------------------
# Commands (instant, private-owner only)
# ----------------------------------------------------------------------------
HELP_TEXT = (
    "ሰላም ሄኖክ! 👋🔥 እኔ የአንተ የግል ረዳት ነኝ — 24/7 ዝግጁ።\n\n"
    "ምን ላደርግልህ እችላለሁ:\n"
    "📰 /news — ወቅታዊ ዜና (ከታማኝ ምንጮች) በቁልፍ ምረጥ\n"
    "📝 /post — የሰከላ ኒውስ ካርድ + ፖስት → ✅ ስትነካ ወደ Telegram እና Facebook\n"
    "📊 /analytics — የ ሰከላ Facebook ገጽ ተከታዮች፣ reach እና ፖስቶች\n"
    "🎙 ድምፅ ላክልኝ (እስከ 5 ደቂቃ) — ትራንስክሪፕት + ማጠቃለያ + Action Items\n"
    "🎬 ቪዲዮ (እስከ 18MB) — ትንታኔ + 3 ቫይራል ክሊፖች\n"
    "🖼 ፎቶ — የ Canva ዲዛይን አስተያየት\n"
    "💬 ወይም ማንኛውንም ነገር ጻፍልኝ — ስክሪፕት፣ ሀሳብ፣ ጥናት\n"
    "🩺 /ai_status — የ AI፣ የቻናል እና የ Facebook ሁኔታ\n\n"
    "🔒 ያለ አንተ ፈቃድ ምንም አልለጥፍም፤ በፌስቡክ ኮመንቶች ላይም ፈጽሞ አልመልስም።"
)


@bot.message_handler(commands=["start", "help"])
@private_owner_only
def cmd_start(message):
    bot.send_message(message.chat.id, HELP_TEXT)


@bot.message_handler(commands=["news", "trends"])
@private_owner_only
def cmd_news(message):
    show_categories(message.chat.id, "news")


@bot.message_handler(commands=["post"])
@private_owner_only
def cmd_post(message):
    show_categories(message.chat.id, "post")


@bot.message_handler(commands=["cancel"])
@private_owner_only
def cmd_cancel(message):
    with state_lock:
        awaiting_topic.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "እሺ ሄኖክ፣ ተሰርዟል ✅")


@bot.message_handler(commands=["analytics", "fb_status"])
@private_owner_only
def cmd_analytics(message):
    start = time.time()
    chat_id = message.chat.id
    with Typing(chat_id):
        text = fb_analytics_text()
    deliver(chat_id, None, text + "\n\n" + took(start))


def _gemini_status_lines():
    if not GEMINI_API_KEY:
        return ["❌ GEMINI_API_KEY አልተሞላም"]
    lines = []
    try:
        r = requests.get(f"{GEMINI_BASE}/models", headers={"x-goog-api-key": GEMINI_API_KEY},
                         params={"pageSize": 1}, timeout=20)
        lines.append("✅ Gemini API: ንቁ" if r.status_code == 200 else f"⚠️ Gemini API: HTTP {r.status_code}")
    except Exception as e:
        lines.append(f"⚠️ Gemini API: ግንኙነት አልተሳካም ({short_reason(e, 60)})")
    for m in STATUS_GEMINI_MODELS:
        try:
            r = requests.get(f"{GEMINI_BASE}/models/{m}", headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=20)
            if r.status_code == 200:
                lines.append(f"  ✅ {m}" + ("" if m in GEMINI_MODELS else " (ተጠባባቂ)"))
            elif r.status_code == 404:
                lines.append(f"  ❌ {m} — 404 (ለቁልፍህ አይገኝም)")
            else:
                lines.append(f"  ⚠️ {m} — HTTP {r.status_code}")
        except Exception as e:
            lines.append(f"  ⚠️ {m} — {short_reason(e, 50)}")
    return lines


def _openrouter_status_lines():
    if not OPENROUTER_API_KEY:
        return ["❌ OPENROUTER_API_KEY አልተሞላም"]
    lines = []
    try:
        r = requests.get(f"{OPENROUTER_URL}/auth/key", headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                         timeout=20)
        if r.status_code == 200:
            d = r.json().get("data", {})
            limit, usage = d.get("limit"), d.get("usage")
            if limit is not None and usage is not None:
                lines.append(f"✅ OpenRouter ቁልፍ ንቁ — ቀሪ credit ≈ ${float(limit) - float(usage):.2f}")
            else:
                lines.append(f"✅ OpenRouter ቁልፍ ንቁ — ጥቅም ላይ የዋለ ${float(usage or 0):.2f}")
        else:
            lines.append(f"⚠️ OpenRouter ቁልፍ: HTTP {r.status_code} (ቁልፍ ወይም credit ችግር ሊሆን ይችላል)")
    except Exception as e:
        lines.append(f"⚠️ OpenRouter: ግንኙነት አልተሳካም ({short_reason(e, 60)})")
    try:
        r = requests.get(f"{OPENROUTER_URL}/models", timeout=25)
        ids = {m.get("id") for m in r.json().get("data", [])} if r.status_code == 200 else None
    except Exception:
        ids = None
    for i, m in enumerate(OPENROUTER_MODELS):
        role = "ዋና" if i == 0 else f"ተጠባባቂ {i}"
        if ids is None:
            lines.append(f"  • {m} ({role})")
        elif m in ids:
            lines.append(f"  ✅ {m} ({role})")
        else:
            lines.append(f"  ❌ {m} ({role}) — በ OpenRouter ዝርዝር ውስጥ የለም (ተወግዷል?)፤ OPENROUTER_MODELS ቀይር")
    return lines


def _channel_status_lines():
    lines = [f"📣 Telegram target: {TARGET_CHANNEL}"]
    try:
        chat = bot.get_chat(TARGET_CHANNEL)
        lines.append(f"  ✅ ቻናሉ ተገኝቷል: {getattr(chat, 'title', '?')} (id {chat.id})")
        member = bot.get_chat_member(chat.id, bot.get_me().id)
        status = getattr(member, "status", "?")
        can_post = getattr(member, "can_post_messages", None)
        if status == "administrator" and can_post is not False:
            lines.append("  ✅ ቦቱ አድሚን ነው — መለጠፍ ይችላል")
        else:
            lines.append(f"  ❌ ቦቱ አድሚን አይደለም ወይም 'Post Messages' ፈቃድ የለውም (status: {status})")
    except Exception as e:
        lines.append(f"  ❌ ቻናሉን ማግኘት አልተቻለም: {short_reason(e, 120)}")
        lines.append("  💡 ቻናሉ public መሆኑን፣ username በትክክል መጻፉን እና ቦቱ አድሚን መሆኑን አረጋግጥ።")
    return lines


def _fb_status_lines():
    lines = [f"📘 Facebook Page: {FB_PAGE_NAME} (id {FB_PAGE_ID})"]
    if not FB_PAGE_ACCESS_TOKEN:
        return lines + ["  ⚪ FB_PAGE_ACCESS_TOKEN አልተሞላም — ወደ ፌስቡክ አይለጠፍም"]
    try:
        info = _fb_request("GET", "me", params={"fields": "name,id"}, timeout=30)
        lines.append(f"  ✅ ተገናኝቷል: {info.get('name')} (id: {info.get('id')})")
    except FBError as e:
        lines.append(f"  ❌ {short_reason(e, 140)}")
        if fb_error_hint(e):
            lines.append("  💡 " + fb_error_hint(e))
    return lines


@bot.message_handler(commands=["ai_status"])
@private_owner_only
def cmd_ai_status(message):
    start = time.time()
    chat_id = message.chat.id
    with Typing(chat_id):
        lines = ["🩺 System Status\n━━━━━━━━━━━━", "🧠 ዋና ጸሐፊ — OpenRouter:"]
        lines += _openrouter_status_lines()
        lines.append("\n🔎 ምርምር/ሚዲያ — Gemini:")
        lines += _gemini_status_lines()
        lines.append("")
        lines += _channel_status_lines()
        lines.append("")
        lines += _fb_status_lines()
        font = find_ethiopic_font() if PIL_OK else None
        lines.append("\n🖼 የካርድ ርዕስ ፊደል (Ethiopic font): " + ("✅ ተገኝቷል" if font else "❌ የለም — NotoSansEthiopic-Bold.ttf ኮሚት አድርግ"))
        lines.append(f"👤 Owner ID: {OWNER_ID}")
        lines.append(f"🕒 የአዲስ አበባ ሰዓት: {addis_now().strftime('%Y-%m-%d %H:%M')}")
        lines.append("\n" + took(start))
    deliver(chat_id, None, "\n".join(lines))


# ----------------------------------------------------------------------------
# Callbacks (categories + approval gate) - private chat with owner only
# ----------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    msg = getattr(call, "message", None)
    if msg is None or not is_private(msg.chat):
        return  # silent in groups/channels
    uid = call.from_user.id
    if uid != OWNER_ID:
        try:
            bot.answer_callback_query(call.id, "ይህ የሄኖክ የግል ቦት ነው 🔒", show_alert=True)
        except Exception:
            pass
        return
    data = call.data or ""
    chat_id, msg_id = msg.chat.id, msg.message_id
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
            bot.edit_message_text(f"✅ የመረጥከው: {label}", chat_id, msg_id)  # permanent chat record
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
            run_post(chat_id, item["label"], item["label"], context_text=item["text"],
                     context_sources=item.get("sources"))

        elif data.startswith("ok:"):
            approve_and_publish(chat_id, msg_id, data.split(":", 1)[1])

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
@private_owner_only
def handle_text(message):
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    uid, chat_id = message.from_user.id, message.chat.id

    # Topic typed after tapping "write my own topic"
    with state_lock:
        pending_mode = awaiting_topic.pop(uid, None)
    if pending_mode:
        label = f"✍️ {text[:40]}"
        (run_news if pending_mode == "news" else run_post)(chat_id, label, text)
        return

    # Greetings / thanks: instant, no AI call, no waiting message, no timer
    if is_greeting(text):
        bot.send_message(chat_id, greeting_reply())
        return
    if is_thanks(text):
        bot.send_message(chat_id, "ምንም አይደለም ሄኖክ! 🙌 ሌላ ነገር ካለ ንገረኝ።")
        return

    mode = "post" if has_any(text, POST_WORDS) else ("news" if has_any(text, NEWS_WORDS) else None)
    if mode:
        if len(text.split()) <= 5:
            show_categories(chat_id, mode)  # short/vague => buttons, no typing fatigue
        else:
            (run_news if mode == "news" else run_post)(chat_id, f"✍️ {text[:40]}", text)
        return

    # General conversation / research / scripts - typing indicator only, no waiting message
    start = time.time()
    with state_lock:
        history = list(histories.get(uid, []))
    live = has_any(text, REALTIME_WORDS)
    try:
        with Typing(chat_id):
            reply, sources, engine = answer(text, history=history, live=live)
    except Exception as e:
        log.error("chat failed: %s", e)
        reply, sources, engine = BUSY_TEXT, [], None
    body = reply
    src = format_sources(sources)
    if src:
        body += "\n\n" + src
    if engine:
        remember(uid, text, reply)
        body += "\n\n" + took(start)
    deliver(chat_id, None, body)


# ----------------------------------------------------------------------------
# Main: health server, webhook cleanup, resilient polling
# ----------------------------------------------------------------------------
def main():
    start_health_server()
    start_keepalive()
    log.info("Starting Henok Assistant agent... Telegram target: %s | Facebook: %s", TARGET_CHANNEL,
             "on" if fb_enabled() else "off (no FB_PAGE_ACCESS_TOKEN)")

    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is missing. Set it in Render environment variables.")
        time.sleep(5)
        sys.exit(1)
    if not OPENROUTER_API_KEY:
        log.warning("OPENROUTER_API_KEY missing - Gemini will write everything (weaker Amharic).")
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY missing - live research and audio/video analysis are disabled.")
    if not PIL_OK:
        log.warning("Pillow missing - cards will be sent without the baked headline (pip install Pillow).")

    try:  # long voice notes / media downloads from Telegram
        apihelper.READ_TIMEOUT = 90
        apihelper.CONNECT_TIMEOUT = 15
    except Exception:
        pass
    if PIL_OK:  # warm the font cache in the background so the first card is not slow
        threading.Thread(target=find_ethiopic_font, daemon=True).start()

    try:
        bot.remove_webhook()
    except Exception as e:
        log.warning("remove_webhook failed: %s", e)
    try:
        bot.set_my_commands([
            types.BotCommand("news", "ወቅታዊ ዜና (ከታማኝ ምንጮች)"),
            types.BotCommand("post", "የሰከላ ኒውስ ካርድ + ፖስት"),
            types.BotCommand("analytics", "የ Facebook ገጽ ትንተና"),
            types.BotCommand("ai_status", "የ AI፣ ቻናል እና Facebook ሁኔታ"),
            types.BotCommand("cancel", "ሰርዝ"),
            types.BotCommand("help", "እገዛ"),
        ], scope=types.BotCommandScopeAllPrivateChats())
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)

    first = True
    while True:
        try:
            bot.infinity_polling(skip_pending=first, timeout=30, long_polling_timeout=25,
                                 allowed_updates=["message", "callback_query"])
            first = False
        except ApiTelegramException as e:
            first = False
            if getattr(e, "error_code", None) == 409 or "409" in str(e):
                log.warning("409 Conflict (another instance is polling). Sleeping 5s...")
            else:
                log.error("Telegram API error: %s. Retrying in 5s...", e)
            time.sleep(5)
        except Exception as e:
            first = False
            if "409" in str(e):
                log.warning("409 Conflict. Sleeping 5s...")
            else:
                log.error("Polling crashed: %s. Retrying in 5s...", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
