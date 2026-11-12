# -*- coding: utf-8 -*-
"""
agent.py  -  Henok's personal Telegram command cockpit (@HenokAssistantBot) + Sekela News publisher

Runs 24/7 on Render (Python 3 + pyTelegramBotAPI). Single file.

Environment variables
---------------------
Required : TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, GEMINI_API_KEY
Facebook : FB_PAGE_ACCESS_TOKEN  (NEVER-EXPIRING Page access token: pages_manage_posts, pages_read_engagement, read_insights)
           FB_PAGE_ID            (default 1252817724577069)
           FB_API_VERSION        (default v25.0)
YouTube  : YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
           YT_PRIVACY            (public | unlisted | private; default public - upload happens ONLY after approval tap)
Optional : OWNER_ID              (default 511440586)
           TARGET_CHANNEL        (default @HenokGraphics)
           OPENROUTER_MODELS     (comma list; default deepseek/deepseek-chat,openai/gpt-4o-mini)
           OPENROUTER_AUTO_EXTRA (default 1: auto-add a verified Claude Haiku / Grok mini slug taken from OpenRouter's
                                  live /api/v1/models list as extra fallback; 0 disables)
           FONT_PATH, PORT (default 10000), RENDER_EXTERNAL_URL
           HUNTER_ENABLED=1, HUNT_INTERVAL_MIN=45, HUNT_MIN_IMPORTANCE=5, HUNT_MAX_PER_DAY=20, HUNT_QUIET=23-6,
           NEWS_RSS_FEEDS="Name|url,Name|url"
           ALERTS_ENABLED=1, ALERT_INTERVAL_MIN=240, ALERT_SUBS_DELTA=3, ALERT_VIEWS_DELTA=1000, ALERT_TG_DELTA=10,
           ALERT_FB_DELTA=5

requirements.txt
----------------
pyTelegramBotAPI
requests
Pillow
imageio-ffmpeg
yt-dlp

Architecture
------------
* Writing (chat, news, posts, audio summaries, polishing): OpenRouter first (DeepSeek -> GPT-4o-mini -> verified
  Claude/Grok slug if available), Gemini as fallback when OpenRouter is unreachable / out of credits.
* Research: Gemini + Google Search grounding (BBC Amharic, Fana, EBC, ENA, VOA/DW Amharic, Addis Standard, Reuters, AP,
  verified X accounts); the writer turns it into Amharic in the strict 5-part Sekela News template.
* Breaking-news hunter: scans 12h (automatic 24h fallback, then RSS fallback) with importance >= 5 and pushes a draft
  to the owner's DM with approve / cancel buttons. Nothing is posted without the approval tap.
* YouTube: channel analytics (Data API), long video -> 9:16 Shorts clipper (Gemini picks segments, ffmpeg cuts),
  upload ONLY after the owner taps approve.
* /analytics: branded PNG dashboard (Telegram + YouTube + Facebook) + proactive alerts on notable changes.
* STRICT NO-COMMENTING POLICY: the Facebook layer can only POST to /photos and /feed and READ page info, insights and
  post counters. Any path containing "comment" is refused in code (see _fb_request).
* The bot talks ONLY in a private chat with the owner; in groups/channels it is silent.
"""

import base64
import copy
import functools
import glob
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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

YT_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
YT_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
YT_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip()
YT_PRIVACY = os.environ.get("YT_PRIVACY", "public").strip().lower()
if YT_PRIVACY not in ("public", "unlisted", "private"):
    YT_PRIVACY = "public"


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

# Base models are only the two that /ai_status confirmed. The Claude / Grok slugs are NOT hardcoded any more:
# get_openrouter_models() looks them up in OpenRouter's live model list so a wrong slug can never show a red X.
OPENROUTER_BASE_MODELS = [m.strip() for m in os.environ.get(
    "OPENROUTER_MODELS", "deepseek/deepseek-chat,openai/gpt-4o-mini").split(",") if m.strip()]
OPENROUTER_EXTRA_RULES = (("anthropic/claude", "haiku"), ("x-ai/grok", "mini"))
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
awaiting_topic = {}   # user_id -> "news" | "post" | "shorts"
drafts = {}           # draft_id -> draft dict (see present_draft)
news_cache = {}       # news_id -> {"label","text","sources","ts"}
video_cache = {}      # video_id -> {"data": bytes, "ts": float}  (for the Shorts clipper button)
shorts_drafts = {}    # short_id -> {"path","title","description","tags","busy","ts"}
bot_stats = {"drafts": 0, "tg_posts": 0, "fb_posts": 0, "yt_uploads": 0, "breaking": 0}

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

# Top-tier outlets recognised for the mandatory "📌 ምንጭ፡" line: (Amharic display name, regex)
OUTLETS = [
    ("ቢቢሲ አማርኛ", r"bbc\s*amharic|bbc\.com/amharic|amharic\.bbc|ቢቢሲ\s*አማርኛ"),
    ("ቢቢሲ", r"\bbbc\b|ቢቢሲ"),
    ("ሮይተርስ", r"reuters|ሮይተርስ"),
    ("AP", r"\bap\b|associated press|apnews|አሶሼትድ ፕሬስ"),
    ("ፋና", r"\bfana\b|fanabc|ፋና"),
    ("ኢቢሲ", r"\bebc\b|ethiopian broadcasting|ኢቢሲ"),
    ("ኢዜአ", r"\bena\b|ethiopian news agency|ena\.et|ኢዜአ"),
    ("ዶቼ ቬለ አማርኛ", r"dw\s*amharic|dw\.com/am|ዶቼ ቬለ አማርኛ"),
    ("ዶቼ ቬለ", r"\bdw\b|dw\.com|deutsche welle|ዶቼ ቬለ"),
    ("የአሜሪካ ድምጽ አማርኛ", r"voa\s*amharic|voanews|amharic\.voanews|የአሜሪካ ድምጽ"),
    ("አዲስ ስታንዳርድ", r"addis\s*standard|addisstandard|አዲስ ስታንዳርድ"),
    ("አል-አይን", r"al[\s\-]*ain|አል-?አይን"),
    ("AFP", r"\bafp\b"),
    ("ብሎምበርግ", r"bloomberg|ብሎምበርግ"),
    ("አልጀዚራ", r"al[\s\-]*jazeera|አልጀዚራ"),
    ("ይፋዊ የመንግሥት መግለጫ", r"\[gov|prime minister'?s office|\bpmo\b|ethiopian government|የመንግሥት መግለጫ"),
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
    return g + "\nዛሬ ምን ላግዝህ? 📰 /news · 📝 /post · 📊 /analytics · ✂️ /shorts ወይም ማንኛውንም ነገር ጻፍልኝ።"


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
    for secret in (FB_PAGE_ACCESS_TOKEN, GEMINI_API_KEY, OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN,
                   YT_CLIENT_SECRET, YT_REFRESH_TOKEN):
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
    "Search the web and return concise ENGLISH research notes (max 380 words) for a newsroom: the verified facts "
    "(who, what, when, where), concrete numbers, background and - importantly - any OFFICIAL statements or direct "
    "quotes with the name/title of the speaker, reactions of authorities or parties, and announced next steps. "
    "Use only verified top-tier reporting: Reuters, AP, BBC Amharic, EBC, Fana, ENA, Al Ain, DW Amharic, VOA Amharic, "
    "Addis Standard, official government statements/feeds, and verified news-publisher accounts on X. Ignore rumours, "
    "anonymous blogs and unverified posts. Bullet points only; after EACH fact put the outlet in brackets, e.g. "
    "[Reuters], [Gov: Ministry of Finance] or [X: @FanaBC]. Do not write Amharic. Do not invent anything; if "
    "something cannot be verified, leave it out."
)
RESEARCH_LITE_PROMPT = (
    "What are the latest verified facts about: {topic}? Answer in English bullet points (max 250 words), put the "
    "outlet name in brackets after each fact, and include official statements and announced next steps if any. "
    "No Amharic. Do not invent anything."
)


def research(topic, lite=False):
    """Live web facts via Gemini + Google Search. Returns (English notes, sources); ("", []) on failure."""
    prompt = (RESEARCH_LITE_PROMPT if lite else RESEARCH_PROMPT).replace("{topic}", topic)
    try:
        text, sources, model = gemini_generate(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system="You are a precise news research assistant. Output plain-text English bullet notes only.",
            grounding=True, temperature=0.3, max_tokens=4096)
        log.info("research done by Gemini %s (%d sources, lite=%s)", model, len(sources), lite)
        return clean_md(text), sources
    except Exception as e:
        log.warning("research failed (lite=%s): %s", lite, e)
        return "", []


def research_for(topic):
    """Research with one automatic second attempt (simpler prompt) if the first search comes back empty."""
    facts, sources = research(topic)
    if not facts:
        time.sleep(3)
        facts, sources = research(topic, lite=True)
    return facts, sources


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
# OpenRouter (primary writer) with Gemini fallback + live model-slug verification
# ----------------------------------------------------------------------------
_or_lock = threading.Lock()
_or_cache = {"ts": 0.0, "models": None, "ids": None}


def fetch_openrouter_ids():
    """Set of every model id OpenRouter currently lists (public endpoint), or None when unreachable."""
    try:
        r = requests.get(f"{OPENROUTER_URL}/models", timeout=25)
        if r.status_code == 200:
            return {m.get("id") for m in r.json().get("data", []) if m.get("id")}
    except Exception as e:
        log.info("OpenRouter model list unavailable: %s", e)
    return None


def get_openrouter_models(force=False):
    """Verified model list for the writer: the configured base models that really exist on OpenRouter, plus (optional)
    one Claude Haiku and one Grok mini slug picked from the live list, so a bad slug can never appear.
    Cached for 6 hours; if the list cannot be fetched the configured base models are used and retried in ~10 min."""
    now = time.time()
    with _or_lock:
        if not force and _or_cache["models"] and now - _or_cache["ts"] < 21600:
            return list(_or_cache["models"])
    ids = fetch_openrouter_ids()
    if ids is None:
        with _or_lock:
            _or_cache.update(ts=now - 21000, models=list(OPENROUTER_BASE_MODELS), ids=None)
        return list(OPENROUTER_BASE_MODELS)
    models = [m for m in OPENROUTER_BASE_MODELS if m in ids] or list(OPENROUTER_BASE_MODELS)
    if os.environ.get("OPENROUTER_AUTO_EXTRA", "1") != "0":
        for prefix, keyword in OPENROUTER_EXTRA_RULES:
            cands = sorted(i for i in ids if i.startswith(prefix) and keyword in i and ":" not in i
                           and "preview" not in i and "beta" not in i)
            if cands and cands[-1] not in models:
                models.append(cands[-1])
    with _or_lock:
        _or_cache.update(ts=now, models=list(models), ids=ids)
    log.info("OpenRouter models verified: %s", models)
    return list(models)


def openrouter_chat(messages, max_tokens=1800, temperature=0.8):
    """Try each verified OpenRouter model in order. Returns (text, model) or (None, None).
    401/402 (bad key / out of credits) abort the whole provider immediately."""
    if not OPENROUTER_API_KEY:
        return None, None
    for model in get_openrouter_models():
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
# Newsroom engine: Amharic news template, sourcing, story discovery
# ----------------------------------------------------------------------------
CHANNEL_HANDLE = TARGET_CHANNEL if isinstance(TARGET_CHANNEL, str) else "@HenokGraphics"
FOOTER = ("🔗 ፈጣን እና ታማኝ መረጃዎችን ለማግኘት የሰከላ ኒውስ ማህበረሰብን ይቀላቀሉ:\n"
          f"Telegram: {CHANNEL_HANDLE} | Facebook: {FB_PAGE_NAME}")
NO_OFFICIAL_LINE = "ጉዳዩን በተመለከተ ተጨማሪ መረጃዎች ሲገኙ ሰከላ ኒውስ ያሳውቃል።"
UNVERIFIED_SOURCE = "ሰከላ ኒውስ ዴስክ (በማረጋገጥ ላይ)"

NEWSROOM_SYSTEM = (
    "You are the senior Amharic news editor of 'ሰከላ ኒውስ (Sekela News)', writing to the standard of professional "
    "Ethiopian newsrooms such as Tikvah-ETH, Addis Insight and BBC Amharic.\n"
    "RULES: pure native Addis Ababa journalistic Amharic in correct Ethiopic script; strict inverted pyramid; "
    "neutral third person; NEVER address anyone (no አንተ/እናንተ/እኔ/እኛ, no questions, no greetings, never the name ሄኖክ); "
    "zero conversational filler, no slang, no word-for-word translated idioms or English sentence structure; short, "
    "factual sentences in news register (አስታወቀ, ገለጸ, ዘግቧል, እንደ ... ገለጻ); attribute every claim and quote to its "
    "source (e.g. 'ሮይተርስ እንደዘገበው', 'የገንዘብ ሚኒስቴር አስታውቋል'); use ONLY facts, numbers, names and quotes present "
    "in the research notes; never invent, embellish or speculate; spell names of people and places consistently; keep "
    "widely used English terms (AI, Bitcoin, Whop) in Latin letters; plain text, no markdown, no emojis inside "
    "paragraphs."
)

NEWS_BRIEF = (
    "Write ONE Amharic news item in the strict 5-part inverted-pyramid format from the research notes.\n"
    "Topic: {topic}\n\n[RESEARCH NOTES]\n{facts}\n\n{mode_note}"
    "Return EXACTLY these blocks and nothing else:\n"
    "===HEADLINE===\n5-8 words: a bold, factual, scroll-stopping news headline; no quotes, no emoji, no full stop.\n"
    "===LEAD===\nThe hook: 1-2 short sentences giving the core of the story directly (who, what, when, where).\n"
    "===CONTEXT===\n3-5 sentences: the detailed body - key figures, data, background and circumstances.\n"
    "===OFFICIAL===\n2-3 sentences: official statements or direct quotes (attributed to the named official or body), "
    "reactions of authorities or parties, and the next steps. If the notes contain no official statement or next "
    "steps, write exactly: " + NO_OFFICIAL_LINE + "\n"
    "===SOURCE===\nThe outlet names taken from the notes, separated by ' / ' (Amharic names where established: "
    "ቢቢሲ አማርኛ, ሮይተርስ, ፋና, ኢቢሲ, ኢዜአ, አል-አይን, ዶቼ ቬለ አማርኛ; AP).\n"
    "===TAG===\nONE specific story hashtag, e.g. #የነዳጅ_ዋጋ or #Bitcoin (no spaces).\n"
    "===IMAGE===\nOne English paragraph, 25-45 words: a symbolic, cinematic photojournalism scene that visualizes the "
    "story's theme (setting, subject matter, lighting, lens, mood, high-contrast colour grade). No identifiable real "
    "people, no text, no logos, no flags, no depiction of violence or casualties.\n"
)
UNVERIFIED_NOTE = (
    "[NO LIVE RESEARCH WAS AVAILABLE] Write a careful editorial synthesis using ONLY well-established background "
    "facts you are certain of. Do NOT state specific recent numbers, dates, quotes or events. In ===SOURCE=== write "
    "exactly: " + UNVERIFIED_SOURCE + "\n\n"
)

IMAGE_STYLE = ("high-contrast cinematic news photography, dramatic lighting, shallow depth of field, ultra detailed, "
               "sharp focus, dark moody tones, square composition, no text, no letters, no watermark")
IMAGE_FALLBACK = ("a dramatic dusk skyline of a modern African capital with glowing city lights and a newsroom "
                  "feel, teal and orange colour grade")

PKG_KEYS = ("headline", "lead", "context", "official", "source", "tag", "image")


def parse_package(text):
    """Split the writer output into its labelled blocks. Missing blocks stay empty."""
    out = {k: "" for k in PKG_KEYS}
    parts = re.split(r"===(HEADLINE|LEAD|CONTEXT|OFFICIAL|SOURCE|TAG|IMAGE)===", text or "")
    for i in range(1, len(parts) - 1, 2):
        out[parts[i].lower()] = parts[i + 1].strip()
    return out


def _tidy_headline(s):
    s = re.sub(r"[\"'“”‘’«»]", "", s or "").strip()
    s = re.sub(r"^[^\wሀ-፿]+", "", s)  # leading emoji / bullets
    return s.rstrip(".።").strip()


def clean_headline(h, fallback_text="", max_words=8):
    """5-8 word headline: no quotes / leading emoji / trailing full stop; falls back to the lead's first words."""
    h = _tidy_headline(h) or _tidy_headline(fallback_text)
    return " ".join(h.split()[:max_words])


def detect_outlets(text):
    """Amharic display names of the recognised top-tier outlets mentioned in text."""
    low = (text or "").lower()
    found = [name for name, rx in OUTLETS if re.search(rx, low)]
    for generic, specific in (("ቢቢሲ", "ቢቢሲ አማርኛ"), ("ዶቼ ቬለ", "ዶቼ ቬለ አማርኛ")):
        if generic in found and specific in found:
            found.remove(generic)
    return found


def _blob(facts, sources):
    return (facts or "") + " " + " ".join(f"{t} {u}" for t, u in (sources or []))


def resolve_source(raw_src, facts, sources, unverified=False):
    """Name the outlet(s) for the mandatory source line. Returns None when no source can be established."""
    if unverified:
        return UNVERIFIED_SOURCE
    found = detect_outlets(raw_src or "") or detect_outlets(_blob(facts, sources))
    if found:
        return " / ".join(found[:3])
    if (raw_src or "").strip():
        return raw_src.strip()[:80]
    if sources:
        return sources[0][0] or urlparse(sources[0][1]).netloc or None
    return None


def story_tag(raw, headline):
    t = (raw or "").strip().split()[0] if (raw or "").strip() else ""
    t = re.sub(r"[^\wሀ-፿#]", "", t).rstrip("_")
    if t and not t.startswith("#"):
        t = "#" + t
    if len(t) < 3 or t.lower() in {b.lower() for b in BRAND_TAGS}:
        words = [re.sub(r"[^\wሀ-፿]", "", w) for w in headline.split()[:2]]
        t = "#" + "_".join(w for w in words if w) if any(words) else "#ዜና"
    return t


def _para(s):
    s = clean_md(s or "")
    s = re.sub(r"^[\s•\-\*]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def assemble_news_post(pkg, facts="", sources=None, unverified=False):
    """Build the post in the exact 5-part Sekela News template. Returns (post, headline) or None if the writer's
    output is unusable (missing lead / context / source)."""
    lead, context = _para(pkg.get("lead")), _para(pkg.get("context"))
    official = _para(pkg.get("official")) or NO_OFFICIAL_LINE
    if not lead or not context:
        return None
    headline = clean_headline(pkg.get("headline"), lead)
    if not headline:
        return None
    src = resolve_source(pkg.get("source"), facts, sources, unverified)
    if not src:
        return None
    tags = " ".join(BRAND_TAGS + [story_tag(pkg.get("tag"), headline)])
    post = (f"🔴 {headline}\n\n{lead}\n\n{context}\n\n{official}\n\n"
            f"📌 ምንጭ፡ {src}\n\n{FOOTER}\n\n{tags}")
    return post, headline


# --- story discovery (used by /news and by the breaking-news hunter) -------------------------------------------
STORY_PROMPT = (
    "Search the web NOW and find the most important and timely news from the last {hours} hours about: {focus}\n"
    "Use verified reporting from: BBC Amharic, Fana BC, EBC, ENA, VOA Amharic, DW Amharic, Al Ain Amharic, "
    "Addis Standard, Reuters, AP, official Ethiopian government statements, and verified news accounts on X (Twitter) "
    "such as @PMEthiopia, @FanaBC, @ENA_Amharic, @BBCAmharic, @addisstandard, @VOAAmharic, @dwamharic. "
    "Cover diplomacy and the Prime Minister's visits/meetings, ministry updates, economy and prices, Addis Ababa "
    "developments, Horn of Africa, and major global/tech news.\n"
    "{feeds}"
    "Return {n} top stories, ONE per line, in EXACTLY this format (English):\n"
    "STORY|<importance 1-10>|<headline, max 14 words>|<outlet names, comma separated>|<one sentence with the key fact>\n"
    "Importance: 8-10 = major national/diplomatic/breaking news, 5-7 = important daily news. "
    "Always return the top available verified news stories - there is almost always important daily news."
)
_STORY_RE = re.compile(r"^\s*STORY\s*\|\s*(\d+)\s*\|(.*?)\|(.*?)\|(.*)$")


def parse_story_lines(text):
    stories = []
    for ln in (text or "").splitlines():
        m = _STORY_RE.match(ln.strip())
        if m:
            stories.append({"importance": int(m.group(1)), "title": m.group(2).strip(),
                            "outlets": m.group(3).strip(), "summary": m.group(4).strip()})
    return sorted(stories, key=lambda s: -s["importance"])


def scan_stories(focus, hours=24, n=4, feeds=""):
    """Gemini + Google Search: top verified stories. Returns (stories, sources); ([], []) on failure."""
    feed_block = ("RECENT HEADLINES FROM NEWS FEEDS (verify them with search before using):\n" + feeds + "\n\n"
                  if feeds else "")
    prompt = (STORY_PROMPT.replace("{hours}", str(hours)).replace("{focus}", focus)
              .replace("{feeds}", feed_block).replace("{n}", str(n)))
    try:
        text, sources, _ = gemini_generate(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system="You are a precise news monitoring assistant. Follow the output format exactly.",
            grounding=True, temperature=0.2, max_tokens=4096)
        return parse_story_lines(text), sources
    except Exception as e:
        log.warning("story scan failed: %s", e)
        return [], []


def amharic_story_lines(stories):
    """Translate story headlines + summaries into Amharic news style. Returns {index: (headline, summary)}."""
    if not stories:
        return {}
    listing = "\n".join(f"{i}|{s['title']}|{s['summary']}" for i, s in enumerate(stories, 1))
    prompt = ("Translate these news items into Amharic news style: headline max 8 words, summary ONE sentence. "
              "Output exactly one line per item in the format N|<headline>|<summary> and nothing else.\n" + listing)
    text, _ = llm_write(prompt, system=NEWSROOM_SYSTEM, max_tokens=1500, temperature=0.3)
    out = {}
    for ln in (text or "").splitlines():
        m = re.match(r"^\s*(\d+)\s*\|(.+?)\|(.+)$", ln)
        if m:
            out[int(m.group(1))] = (clean_headline(m.group(2)), _para(m.group(3)))
    return out


NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def run_news(chat_id, label, topic):
    """/news: a clean numbered list of verified top stories; each has a button that builds the full card + post."""
    start = time.time()
    try:
        with Typing(chat_id):
            stories, sources = scan_stories(topic, hours=24, n=4)
            am = amharic_story_lines(stories) if stories else {}
        nid = uuid.uuid4().hex[:10]
        with state_lock:
            news_cache[nid] = {"label": label, "topic": topic, "stories": stories, "sources": sources,
                               "ts": time.time()}
        prune(news_cache)
        kb = types.InlineKeyboardMarkup()
        if not stories:
            kb.add(types.InlineKeyboardButton("📝 ስለ ርዕሱ በቀጥታ ፖስት አዘጋጅ", callback_data=f"mk:{nid}"))
            deliver(chat_id, None, f"ሄኖክ፣ ስለ «{label}» ከታማኝ ምንጮች አዲስ ትልቅ ዜና አሁን አልተገኘም። "
                                   "ርዕሱን በቀጥታ ወደ ፖስት መቀየር ትችላለህ 👇\n\n" + took(start), kb)
            return
        lines = [f"📰 {label} — ዋና ዋና ዜናዎች (ባለፉት 24 ሰዓታት)", "━━━━━━━━━━━━"]
        for i, s in enumerate(stories[:4], 1):
            headline, summary = am.get(i, (clean_headline(s["title"]), s["summary"]))
            src = " / ".join(detect_outlets(s["outlets"])[:3]) or s["outlets"] or "—"
            lines.append(f"{NUM_EMOJI[i - 1]} 🔴 {headline}\n{summary}\n📌 ምንጭ፡ {src}")
            kb.add(types.InlineKeyboardButton(f"{NUM_EMOJI[i - 1]} ካርድ + ፖስት አዘጋጅ", callback_data=f"st:{nid}:{i - 1}"))
        kb.add(types.InlineKeyboardButton("📝 ሌላ ርዕስ በቀጥታ ፖስት አድርግ", callback_data=f"mk:{nid}"))
        body = "\n\n".join(lines) + "\n\n" + took(start)
        deliver(chat_id, None, body, kb)
    except Exception as e:
        log.error("news failed: %s", e)
        deliver(chat_id, None, "ሄኖክ፣ ዜናውን አሁን ማምጣት አልቻልኩም 😅 ጥቂት ቆይተህ እንደገና ሞክር።")


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
        return ("ቶክኑ ጊዜው አልፏል ወይም ልክ አይደለም — ቋሚ (never-expiring) Page Access Token ፍጠር፣ "
                "ከዚያ በ Render ላይ FB_PAGE_ACCESS_TOKEN ቀይር።")
    if code in (10, 200, 299):
        return "ፈቃድ ይጎድላል — Page token በ pages_manage_posts ፈቃድ መፈጠሩን አረጋግጥ።"
    if code == 100:
        return "FB_PAGE_ID ወይም የጥያቄው መረጃ ልክ አይደለም።"
    return ""


FB_INSIGHT_CANDIDATES = [  # (metric, label, how to aggregate the last 7 daily values, key for the dashboard)
    ("page_total_media_view_unique", "👀 Reach (ልዩ ተመልካቾች)", "sum", "reach"),
    ("page_media_view", "📈 Views (እይታዎች)", "sum", "views"),
    ("page_post_engagements", "💬 Engagement (ተሳትፎ)", "sum", "engagement"),
    ("page_daily_follows", "➕ አዲስ ተከታዮች", "sum", "new_follows"),
    ("page_follows", "👥 ጠቅላላ ተከታዮች", "last", "followers"),
]


def _insight_value(js, mode):
    vals = []
    for item in js.get("data", [])[:1]:
        for v in item.get("values", []):
            val = v.get("value")
            if isinstance(val, (int, float)):
                vals.append(val)
            elif isinstance(val, dict):
                vals.append(sum(x for x in val.values() if isinstance(x, (int, float))))
    if not vals:
        return None
    return int(vals[-1] if mode == "last" else sum(vals))


def fb_analytics_text(metrics=None):
    """Page name/id via `me?fields=name,id`, 7-day insights (each metric optional) and the latest post counters.
    If a dict is passed as `metrics` it is filled with the numbers (reach, views, engagement, new_follows, followers)
    plus `_ok` / `_err` so the dashboard image can use them."""
    metrics = metrics if metrics is not None else {}
    if not fb_enabled():
        metrics.update(_ok=False, _err="no token")
        return "⚪ ፌስቡክ አልተዋቀረም — በ Render ላይ FB_PAGE_ACCESS_TOKEN ጨምር።"
    lines = [f"📊 {FB_PAGE_NAME} — Facebook Analytics", "━━━━━━━━━━━━"]
    try:
        me = _fb_request("GET", "me", params={"fields": "name,id"}, timeout=40)
        metrics["_ok"] = True
        lines.append(f"📄 ገጽ: {me.get('name', FB_PAGE_NAME)} (id {me.get('id', FB_PAGE_ID)})")
        if me.get("id") and str(me["id"]) != str(FB_PAGE_ID):
            lines.append("⚠️ ቶክኑ የሌላ ገጽ ነው — FB_PAGE_ID እና ቶክኑ አይዛመዱም።")
    except FBError as e:
        metrics.update(_ok=False, _err=short_reason(e, 90))
        return "\n".join(lines + [f"❌ የገጹን መረጃ ማምጣት አልተቻለም: {short_reason(e)}", fb_error_hint(e)]).strip()

    now = int(time.time())
    got = []
    for metric, label, mode, key in FB_INSIGHT_CANDIDATES:
        try:
            js = _fb_request("GET", f"{FB_PAGE_ID}/insights",
                             params={"metric": metric, "period": "day", "since": now - 7 * 86400, "until": now},
                             timeout=40)
            val = _insight_value(js, mode)
            if val is not None:
                got.append(f"{label}: {val:,}")
                metrics[key] = val
        except FBError as e:
            log.info("insight %s unavailable: %s", metric, e)
    lines.append("\n🗓 ባለፉት 7 ቀናት:")
    lines += got if got else ["• Insights ማግኘት አልተቻለም (read_insights ፈቃድ ወይም የ Meta metric ለውጥ ሊሆን ይችላል)።"]

    lines.append("\n📰 የቅርብ ጊዜ ፖስቶች:")
    rich = ("message,created_time,permalink_url,reactions.summary(total_count).limit(0),"
            "comments.summary(total_count).limit(0),shares")
    posts = None
    for fields in (rich, "message,created_time,permalink_url"):
        try:
            posts = _fb_request("GET", f"{FB_PAGE_ID}/posts", params={"fields": fields, "limit": 5},
                                timeout=40).get("data", [])
            break
        except FBError as e:
            log.info("posts query (%s) failed: %s", fields[:20], e)
    if posts is None:
        lines.append("• ማምጣት አልተቻለም (pages_read_engagement ፈቃድ ያስፈልጋል)")
    elif not posts:
        lines.append("• ፖስት አልተገኘም")
    for p in posts or []:
        msg = re.sub(r"\s+", " ", p.get("message", "") or "(ያለ ጽሑፍ)")[:55]
        row = f"• {p.get('created_time', '')[:10]} — {msg}"
        if "reactions" in p or "comments" in p:
            react = ((p.get("reactions") or {}).get("summary") or {}).get("total_count", 0)
            comm = ((p.get("comments") or {}).get("summary") or {}).get("total_count", 0)
            shares = (p.get("shares") or {}).get("count", 0)
            row += f"\n   ❤️ {react} · 💬 {comm} · 🔁 {shares}"
        lines.append(row)
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Post package: Sekela News card + copy -> preview -> approval -> Telegram + Facebook
# ----------------------------------------------------------------------------
def build_draft(topic, facts=None, sources=None, allow_fallback=True):
    """Research -> Amharic news copy (template) -> AI background -> Sekela card. No Telegram I/O.
    Returns a draft dict or None. With allow_fallback=False an unverifiable story is dropped;
    with allow_fallback=True an editorial synthesis is drafted but flagged `unverified` when no facts exist."""
    sources = list(sources or [])
    if not facts:
        facts, sources = research_for(topic)
    unverified = not facts
    if unverified and not allow_fallback:
        return None
    built, pkg = None, {}
    for attempt in range(2):
        brief = (NEWS_BRIEF.replace("{topic}", topic)
                 .replace("{facts}", facts or "(none)")
                 .replace("{mode_note}", UNVERIFIED_NOTE if unverified else ""))
        text, _ = llm_write(brief, system=NEWSROOM_SYSTEM, max_tokens=2600, temperature=0.6 if attempt == 0 else 0.4)
        if not text:
            continue
        pkg = parse_package(text)
        built = assemble_news_post(pkg, facts or "", sources, unverified)
        if built:
            break
    if not built:
        return None
    post, headline = built
    bg = fetch_image(build_image_prompt(pkg.get("image")))
    card = compose_card(bg, headline) if bg else None
    bot_stats["drafts"] += 1
    return {"post": post, "headline": headline, "image": card or bg, "card": bool(card),
            "sources": sources, "unverified": unverified}


def present_draft(chat_id, label, d, header, start=None):
    """Send the card preview + details with the approval buttons, and register the draft for publishing."""
    post, headline, image = d["post"], d["headline"], d.get("image")
    caption_fits = len(post) <= CAPTION_LIMIT
    file_id = None
    if image:
        try:
            sent = bot.send_photo(chat_id, _as_photo(image), caption=post if caption_fits else f"🔴 {headline}")
            file_id = sent.photo[-1].file_id
        except Exception as e:
            log.error("sending preview photo failed: %s", e)

    did = uuid.uuid4().hex[:10]
    with state_lock:
        drafts[did] = {"post": post, "file_id": file_id, "image": image, "headline": headline,
                       "unverified": d.get("unverified", False), "confirmed": False,
                       "tg": False, "fb": False, "busy": False, "ts": time.time()}
    prune(drafts)

    details = [header]
    if not (file_id and caption_fits):
        details.append("━━━━━━━━━━━━\n" + post)
    if d.get("unverified"):
        details.append("⚠️ ቀጥታ የዜና ፍለጋ አልተሳካም — ይህ ረቂቅ በታወቁ እውነታዎች ላይ ብቻ የተመሠረተ ነው፤ ምንጩ አልተረጋገጠም። "
                       "ከመለጠፍህ በፊት አረጋግጥ (ሲያጸድቅ ሁለተኛ ማረጋገጫ ይጠየቃል)።")
    if not image:
        details.append("⚠️ ምስሉ በዚህ ጊዜ አልተሳካም — ያለ ምስል መለጠፍ ወይም ሰርዘህ እንደገና መጠየቅ ትችላለህ።")
    elif not d.get("card"):
        details.append("⚠️ ርዕሱን በምስሉ ላይ ማተም አልተቻለም (Ethiopic font የለም) — ርዕሱ በፖስቱ ጽሑፍ ውስጥ አለ። "
                       "NotoSansEthiopic-Bold.ttf ን ከ agent.py ጎን ኮሚት አድርግ።")
    src = format_sources(d.get("sources"), limit=4)
    if src:
        details.append(src)
    targets = [str(TARGET_CHANNEL)] + ([FB_PAGE_NAME + " (Facebook)"] if fb_enabled() else [])
    details.append("━━━━━━━━━━━━\n🔒 ✅ ከነካህ ብቻ ይለጠፋል → " + " + ".join(targets) +
                   "\nበፌስቡክ ኮመንቶች ላይ ቦቱ ፈጽሞ አይመልስም።")
    if start:
        details.append(took(start))
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ አጽድቅና ፖስት አድርግ", callback_data=f"ok:{did}"),
        types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"no:{did}"),
    )
    deliver(chat_id, None, "\n\n".join(details), kb)
    return did


def run_post(chat_id, label, topic, context_text=None, context_sources=None):
    start = time.time()
    ack_id = send_progress(
        chat_id, "⏳ ሄኖክ፣ ጥናቱን አድርጌ የሰከላ ኒውስ ፖስቱን እና AI ምስሉን እያዘጋጀሁ ነው (ከ20-40 ሰከንድ ይወስዳል)...")
    try:
        d = build_draft(topic, facts=context_text, sources=context_sources, allow_fallback=True)
        if not d:
            deliver(chat_id, ack_id, "ሄኖክ፣ ዜናውን አሁን ማዘጋጀት አልቻልኩም 😕 ጥቂት ቆይተህ ደግመህ ሞክር።")
            return
        present_draft(chat_id, label, d, f"📝 የሰከላ ኒውስ ረቂቅ ዝግጁ ነው — {label}", start)
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
                bot_stats["tg_posts"] += 1
            except Exception as e:
                log.error("telegram publish failed: %s", e)
                tg_err = short_reason(e, 200)
        if fb_on and not d["fb"]:
            try:
                fb_publish(d["post"], d.get("image"))
                d["fb"] = True
                bot_stats["fb_posts"] += 1
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
# Proactive breaking-news hunter (background thread -> push to Henok's private DM)
# ----------------------------------------------------------------------------
HUNT_HEADER = "🔔 ሄኖክ፣ አዲስ ሰበር ዜና አዘጋጅቼልሃለሁ! ይሄንን ለሰከላ ኒውስ ፖስት እናድርገው?"
HUNT_FOCUS = ("Ethiopia (top priority: government and politics, Prime Minister Abiy Ahmed's activities and diplomatic "
              "visits, economy and prices, diplomacy, security developments as reported by official or verified "
              "outlets, infrastructure, major events in Addis Ababa) and major international developments that matter "
              "to Ethiopians (Horn of Africa, African Union, global economy, AI/tech, crypto markets)")

hunt_lock = threading.Lock()
hunt_seen = []                               # [(token_set, timestamp)] stories already offered (last 24h)
hunt_daily = {"date": "", "count": 0}

DEFAULT_FEEDS = (
    "BBC Amharic|https://feeds.bbci.co.uk/amharic/rss.xml,"
    "Fana BC|https://www.fanabc.com/feed/,"
    "VOA Amharic|https://amharic.voanews.com/api/z$pqveymop,"
    "DW Amharic|https://rss.dw.com/xml/rss-am-all,"
    "Addis Standard|https://addisstandard.com/feed/,"
    "ENA|https://www.ena.et/feed"
)


def _title_tokens(title):
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(w) > 3}


def _is_seen(title):
    now = time.time()
    hunt_seen[:] = [(t, ts) for t, ts in hunt_seen if now - ts < 86400]
    tokens = _title_tokens(title)
    if len(tokens) < 2:  # non-Latin (Amharic) title: compare the raw string
        raw = (title or "").strip()
        return any(raw and raw == r for r in _raw_seen)
    for t, _ in hunt_seen:
        if len(t) >= 2 and len(tokens & t) / min(len(tokens), len(t)) >= 0.6:
            return True
    return False


_raw_seen = []


def _mark_seen(title):
    hunt_seen.append((_title_tokens(title), time.time()))
    raw = (title or "").strip()
    if raw:
        _raw_seen.append(raw)
        del _raw_seen[:-200]


def in_quiet_hours(hour=None):
    """HUNT_QUIET like '23-6' (Addis Ababa time): no scans / pushes during those hours. '' disables."""
    spec = os.environ.get("HUNT_QUIET", "23-6").strip()
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", spec)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    h = addis_now().hour if hour is None else hour
    return (h >= a or h < b) if a > b else (a <= h < b)


def fetch_feed_items(max_age_hours=24, per_feed=8):
    """Recent items from configured RSS/Atom feeds as [{"source","title"}] (best effort; a dead feed is skipped)."""
    spec = os.environ.get("NEWS_RSS_FEEDS", DEFAULT_FEEDS)
    atom = "{http://www.w3.org/2005/Atom}"
    out = []
    for item in [s for s in spec.split(",") if "|" in s]:
        name, url = [x.strip() for x in item.split("|", 1)]
        try:
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0 SekelaNewsBot"})
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            entries = list(root.iter("item")) or list(root.iter(atom + "entry"))
            n = 0
            for it in entries:
                title = (it.findtext("title") or it.findtext(atom + "title") or "").strip()
                pub = it.findtext("pubDate") or it.findtext(atom + "updated") or it.findtext(atom + "published")
                if pub:
                    try:
                        dt = parsedate_to_datetime(pub)
                    except Exception:
                        try:
                            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                        except Exception:
                            dt = None
                    if dt is not None and dt.tzinfo and (datetime.now(timezone.utc) - dt).total_seconds() > max_age_hours * 3600:
                        continue
                if title:
                    out.append({"source": name, "title": title})
                    n += 1
                if n >= per_feed:
                    break
        except Exception as e:
            log.info("feed %s unavailable: %s", name, e)
    return out


def feed_items_text(items):
    return "\n".join(f"- [{i['source']}] {i['title']}" for i in items)


def stories_from_feeds(items, n=6):
    """Last-resort story list built straight from RSS headlines of recognised outlets (importance 6)."""
    return [{"importance": 6, "title": i["title"], "outlets": i["source"],
             "summary": i["title"]} for i in items[:n]]


def hunt_once(manual=False, min_importance=None):
    """One scan. Searches the last 12h (automatic 24h fallback, then RSS fallback) for a timely, not-yet-offered story
    with importance >= threshold (default 5), drafts the card + copy and pushes it to Henok with the approve/cancel
    buttons. A story that came from a recognised outlet is never dropped as 'unverified'.
    Returns: sent | none | failed | busy | cap | nokey."""
    if not hunt_lock.acquire(blocking=False):
        return "busy"
    try:
        if not GEMINI_API_KEY:
            return "nokey"
        today = addis_now().strftime("%Y-%m-%d")
        if hunt_daily["date"] != today:
            hunt_daily.update(date=today, count=0)
        if not manual and hunt_daily["count"] >= int(os.environ.get("HUNT_MAX_PER_DAY", "20")):
            return "cap"
        threshold = min_importance if min_importance is not None else int(os.environ.get("HUNT_MIN_IMPORTANCE", "5"))
        feed_items = fetch_feed_items(max_age_hours=24, per_feed=8)
        feeds_txt = feed_items_text(feed_items)

        stories, scan_sources = scan_stories(HUNT_FOCUS, hours=12, n=6, feeds=feeds_txt)
        fresh = [s for s in stories if s["importance"] >= threshold and not _is_seen(s["title"])]
        if not fresh:  # 24h fallback
            stories24, src24 = scan_stories(HUNT_FOCUS, hours=24, n=6, feeds=feeds_txt)
            if src24:
                scan_sources = src24
            stories = stories or stories24
            fresh = [s for s in stories24 if s["importance"] >= threshold and not _is_seen(s["title"])]
            if not fresh:
                fresh = [s for s in stories24 if not _is_seen(s["title"])]
        if not fresh:  # RSS fallback: recognised outlets' own headlines
            fresh = [s for s in stories_from_feeds(feed_items) if not _is_seen(s["title"])]
        if not fresh and manual and stories:
            fresh = stories  # manual request: show the top story even if it was offered before
        if not fresh:
            return "none"

        story = fresh[0]
        _mark_seen(story["title"])
        start = time.time()
        topic = f"{story['title']}. {story['summary']}"
        facts, sources = research_for(topic)
        if not facts:  # a story from a recognised outlet is still usable: build on the scan's own facts
            facts = f"{story['title']}. {story['summary']} (Reported by: {story['outlets']})"
            sources = scan_sources or [(story['outlets'] or "Sekela News Desk", "https://t.me/HenokGraphics")]
        d = build_draft(topic, facts=facts, sources=sources, allow_fallback=True)
        if not d:
            return "failed"
        present_draft(OWNER_ID, "ሰበር ዜና", d, HUNT_HEADER, start)
        hunt_daily["count"] += 1
        bot_stats["breaking"] += 1
        return "sent"
    except Exception as e:
        log.error("hunt failed: %s", e)
        return "failed"
    finally:
        hunt_lock.release()


def hunter_loop():
    time.sleep(180)
    while True:
        try:
            if not in_quiet_hours():
                log.info("breaking-news hunt: %s", hunt_once())
        except Exception as e:
            log.error("hunter loop error: %s", e)
        time.sleep(max(5, int(os.environ.get("HUNT_INTERVAL_MIN", "45"))) * 60)


def start_hunter():
    if os.environ.get("HUNTER_ENABLED", "1") == "0" or not GEMINI_API_KEY:
        return
    threading.Thread(target=hunter_loop, daemon=True, name="hunter").start()
    log.info("Breaking-news hunter started (every %s min, quiet hours %s Addis time)",
             os.environ.get("HUNT_INTERVAL_MIN", "45"), os.environ.get("HUNT_QUIET", "23-6") or "off")


# ----------------------------------------------------------------------------
# YouTube module: analytics (Data API) + approval-gated upload
# ----------------------------------------------------------------------------
class YTError(Exception):
    pass


_yt_token = {"token": "", "exp": 0.0}
_yt_lock = threading.Lock()


def yt_enabled():
    return bool(YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN)


def yt_access_token():
    """Short-lived access token from the stored refresh token (cached until ~1 min before expiry)."""
    if not yt_enabled():
        raise YTError("YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN አልተሞሉም")
    with _yt_lock:
        if _yt_token["token"] and time.time() < _yt_token["exp"] - 60:
            return _yt_token["token"]
        try:
            r = requests.post("https://oauth2.googleapis.com/token", data={
                "client_id": YT_CLIENT_ID, "client_secret": YT_CLIENT_SECRET,
                "refresh_token": YT_REFRESH_TOKEN, "grant_type": "refresh_token"}, timeout=30)
        except requests.RequestException as e:
            raise YTError(f"network error: {short_reason(e, 100)}")
        if r.status_code != 200:
            raise YTError(f"token HTTP {r.status_code}: {short_reason(r.text, 150)}")
        js = r.json()
        _yt_token.update(token=js.get("access_token", ""), exp=time.time() + int(js.get("expires_in", 3000)))
        if not _yt_token["token"]:
            raise YTError("no access_token returned")
        return _yt_token["token"]


def _yt_get(path, params):
    token = yt_access_token()
    try:
        r = requests.get(f"https://www.googleapis.com/youtube/v3/{path}", params=params,
                         headers={"Authorization": f"Bearer {token}"}, timeout=40)
    except requests.RequestException as e:
        raise YTError(f"network error: {short_reason(e, 100)}")
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except Exception:
            msg = r.text
        raise YTError(f"{path} HTTP {r.status_code}: {short_reason(msg, 140)}")
    return r.json()


def yt_channel_stats():
    """Channel totals + the latest uploads with their view/like/comment counters."""
    js = _yt_get("channels", {"part": "snippet,statistics,contentDetails", "mine": "true"})
    items = js.get("items") or []
    if not items:
        raise YTError("ለዚህ ቶክን ቻናል አልተገኘም (ትክክለኛውን ቻናል/Brand account መርጠህ ቶክኑን ፍጠር)")
    ch = items[0]
    st = ch.get("statistics", {})
    out = {"title": (ch.get("snippet") or {}).get("title", "?"),
           "subs": int(st.get("subscriberCount", 0)), "views": int(st.get("viewCount", 0)),
           "videos": int(st.get("videoCount", 0)), "hidden_subs": bool(st.get("hiddenSubscriberCount")),
           "recent": []}
    uploads = ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    if uploads:
        try:
            pl = _yt_get("playlistItems", {"part": "contentDetails", "playlistId": uploads, "maxResults": 8})
            ids = [i["contentDetails"]["videoId"] for i in pl.get("items", []) if i.get("contentDetails")]
            if ids:
                vs = _yt_get("videos", {"part": "snippet,statistics", "id": ",".join(ids)})
                byid = {v["id"]: v for v in vs.get("items", [])}
                for vid in ids:
                    v = byid.get(vid)
                    if not v:
                        continue
                    s = v.get("statistics", {})
                    out["recent"].append({"id": vid, "title": (v.get("snippet") or {}).get("title", ""),
                                          "published": ((v.get("snippet") or {}).get("publishedAt") or "")[:10],
                                          "views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                          "comments": int(s.get("commentCount", 0))})
        except YTError as e:
            log.info("recent uploads unavailable: %s", e)
    return out


def yt_analytics_text(yt):
    if not yt:
        return "⚪ YouTube አልተዋቀረም — YOUTUBE_CLIENT_ID / SECRET / REFRESH_TOKEN ጨምር።"
    if yt.get("error"):
        return f"▶️ YouTube\n❌ ማምጣት አልተቻለም: {yt['error']}"
    subs = "ተደብቋል" if yt["hidden_subs"] else f"{yt['subs']:,}"
    lines = [f"▶️ YouTube — {yt['title']}", "━━━━━━━━━━━━",
             f"👥 Subscribers: {subs}", f"👁 ጠቅላላ Views: {yt['views']:,}", f"🎞 ቪዲዮዎች: {yt['videos']:,}"]
    if yt["recent"]:
        lines.append("\n🆕 የቅርብ ጊዜ ቪዲዮዎች:")
        for v in yt["recent"][:5]:
            lines.append(f"• {v['published']} — {re.sub(chr(10), ' ', v['title'])[:48]}\n"
                         f"   👁 {v['views']:,} · 👍 {v['likes']:,} · 💬 {v['comments']:,}")
    return "\n".join(lines)


def yt_upload_short(path, title, description, tags):
    """Resumable upload of a finished clip. Called ONLY from the approval callback."""
    token = yt_access_token()
    size = os.path.getsize(path)
    meta = {"snippet": {"title": title[:100], "description": description[:4900], "tags": tags[:15],
                        "categoryId": "22"},
            "status": {"privacyStatus": YT_PRIVACY, "selfDeclaredMadeForKids": False}}
    init = requests.post("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "application/json; charset=UTF-8",
                                  "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
                         json=meta, timeout=60)
    if init.status_code != 200 or not init.headers.get("Location"):
        raise YTError(f"upload init HTTP {init.status_code}: {short_reason(init.text, 160)}")
    with open(path, "rb") as f:
        up = requests.put(init.headers["Location"], data=f,
                          headers={"Content-Type": "video/mp4", "Content-Length": str(size)}, timeout=900)
    if up.status_code not in (200, 201):
        raise YTError(f"upload HTTP {up.status_code}: {short_reason(up.text, 160)}")
    return up.json().get("id")


# ----------------------------------------------------------------------------
# Long video -> 9:16 Shorts clipper (Gemini picks the segments, ffmpeg cuts them)
# ----------------------------------------------------------------------------
CLIP_PROMPT = (
    "You are a viral Shorts editor. Watch this video and choose the {n} best self-contained segments for YouTube "
    "Shorts / TikTok / Reels: each 25-55 seconds long, starting on a strong hook and ending on a complete thought. "
    "Return ONLY a JSON array (no markdown, no comments) of objects with exactly these keys: "
    "\"start\" (MM:SS), \"end\" (MM:SS), \"title\" (max 70 characters, curiosity-driven, no hashtags), "
    "\"description\" (2-3 short lines), \"hashtags\" (array of 4-6 hashtags starting with #), "
    "\"score\" (virality 1-10). Write title and description in natural Amharic when the video is in Amharic, "
    "otherwise in the video's own language. Only use moments that really exist in the video; timestamps must be "
    "accurate. Rank best first."
)


def _ts_seconds(v):
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    try:
        secs = 0.0
        for part in s.split(":"):
            secs = secs * 60 + float(part)
        return secs
    except Exception:
        return None


def parse_clips(text, max_clips=3):
    """Parse Gemini's JSON clip list into validated dicts: start, end (seconds), title, description, tags."""
    raw = (text or "").strip()
    a, b = raw.find("["), raw.rfind("]")
    if a < 0 or b <= a:
        return []
    try:
        arr = json.loads(raw[a:b + 1])
    except Exception:
        return []
    clips = []
    for c in arr if isinstance(arr, list) else []:
        if not isinstance(c, dict):
            continue
        s, e = _ts_seconds(c.get("start")), _ts_seconds(c.get("end"))
        if s is None or e is None or e <= s:
            continue
        if e - s < 8:
            continue
        if e - s > 59:
            e = s + 59
        tags = [t if str(t).startswith("#") else "#" + str(t) for t in (c.get("hashtags") or []) if str(t).strip()]
        clips.append({"start": s, "end": e, "title": _para(str(c.get("title", "")))[:100] or "Short",
                      "description": _para(str(c.get("description", ""))), "tags": tags[:8],
                      "score": c.get("score", "")})
    return clips[:max_clips]


def get_ffmpeg():
    """System ffmpeg if present, otherwise the static binary shipped by the `imageio-ffmpeg` pip package
    (so no Dockerfile is needed on Render)."""
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def cut_vertical_clip(ffmpeg, src, start, end, out):
    """Centre-crop to 9:16, scale to 1080x1920, H.264 + AAC. Raises RuntimeError on failure."""
    cmd = [ffmpeg, "-y", "-ss", f"{start:.2f}", "-i", src, "-t", f"{end - start:.2f}",
           "-vf", "crop='min(iw,ih*9/16)':ih,scale=1080:1920:flags=lanczos,setsar=1",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, timeout=420)
    if r.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) < 5000:
        raise RuntimeError("ffmpeg failed: " + r.stderr.decode("utf-8", "ignore")[-200:])


def download_youtube(url, outdir):
    """Download a public YouTube video (<=720p, single file) with yt-dlp. Returns (path, error)."""
    try:
        import yt_dlp
        from yt_dlp.utils import match_filter_func
    except Exception:
        return None, "yt-dlp አልተጫነም (requirements.txt ላይ yt-dlp ጨምር)"
    opts = {"outtmpl": os.path.join(outdir, "src.%(ext)s"),
            "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
            "quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30,
            "max_filesize": 400 * 1024 * 1024, "match_filter": match_filter_func("duration < 10800")}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
            path = y.prepare_filename(info)
        if path and os.path.isfile(path):
            return path, None
        return None, "ፋይሉ አልወረደም"
    except Exception as e:
        return None, short_reason(e, 160)


def prune_shorts(max_age=3 * 3600):
    now = time.time()
    with state_lock:
        old = [k for k, v in shorts_drafts.items() if now - v.get("ts", 0) > max_age]
        items = [shorts_drafts.pop(k) for k in old]
    for v in items:
        shutil.rmtree(os.path.dirname(v.get("path", "")), ignore_errors=True)


def is_youtube_url(url):
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return host.endswith("youtube.com") or host == "youtu.be"


def run_shorts(chat_id, data=None, url=None):
    """Long video (bytes from Telegram, or a YouTube URL) -> up to 3 vertical Shorts previews in the DM, each with
    approve / cancel buttons. NOTHING is uploaded to YouTube until the approve button is tapped."""
    start = time.time()
    ack_id = send_progress(chat_id, "✂️ ሄኖክ፣ ምርጥ ክፍሎችን እየመረጥኩ እና ወደ 9:16 Shorts እየቆረጥኩ ነው (ከ1-3 ደቂቃ ይወስዳል)...")
    workdir = tempfile.mkdtemp(prefix="shorts-")
    keep = False
    try:
        ffmpeg = get_ffmpeg()
        if not ffmpeg:
            deliver(chat_id, ack_id, "ሄኖክ፣ ffmpeg አልተገኘም 😕 requirements.txt ላይ imageio-ffmpeg ጨምር (Dockerfile አያስፈልግም)፤ "
                                     "Render እንደገና deploy ሲያደርግ ይሰራል።")
            return
        if url:
            part = {"fileData": {"fileUri": url}}
        else:
            part = _media_part(data, "video/mp4", "video")
        raw = multimodal_generate([{"text": CLIP_PROMPT.replace("{n}", "3")}, part],
                                  temperature=0.3, max_tokens=8000, thinking_budget=1024)
        clips = parse_clips(raw)
        if not clips:
            deliver(chat_id, ack_id, "ሄኖክ፣ ለ Shorts የሚሆኑ ክፍሎችን መምረጥ አልቻልኩም 😕 ሌላ ቪዲዮ ሞክር ወይም ደግመህ ላክ።")
            return
        if url:
            src, err = download_youtube(url, workdir)
            if not src:
                plan = "\n".join(f"{i}. {int(c['start']) // 60:02d}:{int(c['start']) % 60:02d}-"
                                 f"{int(c['end']) // 60:02d}:{int(c['end']) % 60:02d} — {c['title']}"
                                 for i, c in enumerate(clips, 1))
                deliver(chat_id, ack_id, f"ሄኖክ፣ ቪዲዮውን ማውረድ አልተቻለም ({err})። YouTube አንዳንዴ የ server ማውረድን ይከለክላል።\n"
                                         "የተመረጡት ክፍሎች ይህ ናቸው — ቪዲዮውን እዚህ ቴሌግራም ላይ (እስከ 18MB) ከላክክልኝ ቆርጬ አዘጋጃለሁ:\n" + plan)
                return
        else:
            src = os.path.join(workdir, "src.mp4")
            with open(src, "wb") as f:
                f.write(data)
        prune_shorts()
        made = 0
        for i, c in enumerate(clips, 1):
            out = os.path.join(workdir, f"short{i}.mp4")
            try:
                cut_vertical_clip(ffmpeg, src, c["start"], c["end"], out)
            except Exception as e:
                log.error("clip %d failed: %s", i, e)
                continue
            tags = c["tags"] or []
            hashtags = " ".join(tags + ["#Shorts"]) if "#shorts" not in " ".join(tags).lower() else " ".join(tags)
            desc = (c["description"] + "\n\n" + hashtags).strip()
            sid = uuid.uuid4().hex[:10]
            with state_lock:
                shorts_drafts[sid] = {"path": out, "title": c["title"], "description": desc,
                                      "tags": [t.lstrip("#") for t in tags] + ["Shorts"], "busy": False,
                                      "ts": time.time()}
            keep = True
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ አጽድቅና YouTube ላይ ጫን", callback_data=f"yk:{sid}"),
                   types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"yn:{sid}"))
            caption = (f"✂️ Short {i}/{len(clips)} — ⭐ {c.get('score', '?')}/10\n🎬 {c['title']}\n\n{desc}\n\n"
                       f"🔒 ✅ ካልነካህ YouTube ላይ አይጫንም ({YT_PRIVACY})")[:1020]
            try:
                with open(out, "rb") as vf:
                    bot.send_video(chat_id, vf, caption=caption, reply_markup=kb, supports_streaming=True,
                                   width=1080, height=1920, timeout=180)
                made += 1
            except Exception as e:
                log.error("sending short preview failed: %s", e)
        drop_message(chat_id, ack_id)
        if made == 0:
            bot.send_message(chat_id, "ሄኖክ፣ ክሊፖቹን መቁረጥ አልተሳካም 😕 ሌላ ቪዲዮ ሞክር።")
        else:
            bot.send_message(chat_id, f"✅ {made} Shorts ተዘጋጅተዋል — የምትወደውን አጽድቅ፤ አለበለዚያ ሰርዝ።\n" + took(start))
    except Exception as e:
        log.error("shorts failed: %s", e)
        deliver(chat_id, ack_id, f"ሄኖክ፣ Shorts ማዘጋጀት አልተሳካም 😅\n(ምክንያት: {short_reason(e)})")
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def approve_short_upload(chat_id, msg_id, sid):
    """Approval gate for YouTube: runs ONLY from the owner's tap on the approve button."""
    with state_lock:
        d = shorts_drafts.get(sid)
        if d is None:
            expired = True
        else:
            expired = False
            if d["busy"]:
                return
            d["busy"] = True
    if expired:
        try:
            bot.edit_message_caption("⚠️ ይህ ክሊፕ ጊዜው አልፏል ወይም ቀድሞ ተስተናግዷል።", chat_id, msg_id)
        except Exception:
            bot.send_message(chat_id, "⚠️ ይህ ክሊፕ ጊዜው አልፏል ወይም ቀድሞ ተስተናግዷል።")
        return
    try:
        bot.edit_message_caption("⏳ YouTube ላይ እየጫንኩ ነው...", chat_id, msg_id)
    except Exception:
        pass
    try:
        vid = yt_upload_short(d["path"], d["title"], d["description"], d["tags"])
        bot_stats["yt_uploads"] += 1
        with state_lock:
            shorts_drafts.pop(sid, None)
        shutil.rmtree(os.path.dirname(d["path"]), ignore_errors=True)
        text = f"✅ YouTube ላይ ተጭኗል ({YT_PRIVACY}) 🚀\nhttps://youtube.com/shorts/{vid}"
        try:
            bot.edit_message_caption(text, chat_id, msg_id)
        except Exception:
            bot.send_message(chat_id, text)
    except Exception as e:
        log.error("youtube upload failed: %s", e)
        d["busy"] = False
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔁 እንደገና ሞክር", callback_data=f"yk:{sid}"),
               types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"yn:{sid}"))
        hint = ""
        if "403" in str(e) or "scope" in str(e).lower():
            hint = "\n💡 YOUTUBE_REFRESH_TOKEN የ youtube.upload ፈቃድ ላይኖረው ይችላል።"
        try:
            bot.edit_message_caption(f"❌ መጫን አልተሳካም: {short_reason(e, 200)}{hint}", chat_id, msg_id, reply_markup=kb)
        except Exception:
            bot.send_message(chat_id, f"❌ መጫን አልተሳካም: {short_reason(e, 200)}{hint}", reply_markup=kb)


# ----------------------------------------------------------------------------
# Analytics: collection, branded PNG dashboard, proactive alerts
# ----------------------------------------------------------------------------
def tg_member_count():
    fn = getattr(bot, "get_chat_member_count", None) or getattr(bot, "get_chat_members_count", None)
    try:
        return int(fn(TARGET_CHANNEL)) if fn else None
    except Exception as e:
        log.info("telegram member count unavailable: %s", e)
        return None


def collect_analytics():
    """One structured snapshot of Telegram + YouTube + Facebook (each part fails independently)."""
    data = {"tg": tg_member_count(), "yt": None, "fb": {}, "fb_text": ""}
    if yt_enabled():
        try:
            data["yt"] = yt_channel_stats()
        except Exception as e:
            data["yt"] = {"error": short_reason(e, 150)}
    data["fb_text"] = fb_analytics_text(data["fb"])
    return data


def _fmt(n):
    return f"{n:,}" if isinstance(n, (int, float)) else "—"


def render_analytics_card(data):
    """Branded 1080x1340 dark-navy & gold Sekela Media dashboard (Telegram, YouTube, Facebook, bot activity) as JPEG
    bytes. Ethiopic text uses the Ethiopic font, Latin/numbers use a Latin font. Returns None if Pillow is missing."""
    if not PIL_OK:
        return None
    try:
        W, H = 1080, 1340
        NAVY, PANEL, LINE = (14, 18, 28), (24, 31, 48), (45, 55, 80)
        GOLD, TXT, MUTED, RED = (235, 180, 45), (230, 235, 245), (160, 170, 190), (255, 110, 110)
        BLUE, GREEN, PURPLE, ORANGE = (40, 165, 255), (80, 210, 130), (180, 120, 255), (255, 140, 90)
        img = Image.new("RGB", (W, H), NAVY)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 10], fill=GOLD)
        eth = find_ethiopic_font()

        def put(x, y, text, size, fill, max_w=None):
            f_lat = _latin_font(size)
            f_eth = ImageFont.truetype(eth, size) if eth else f_lat
            text = str(text)
            if max_w:
                orig = text
                while len(text) > 1 and _measure(text, f_eth, f_lat) > max_w:
                    text = text[:-1]
                if text != orig:
                    text = text.rstrip() + "…"
            _draw_mixed(d, x, y + size, text, f_eth, f_lat, fill)

        def panel(y, h, title, color):
            d.rounded_rectangle([40, y, W - 40, y + h], radius=18, fill=PANEL, outline=LINE, width=2)
            d.ellipse([64, y + 24, 80, y + 40], fill=color)
            put(96, y + 16, title, 28, TXT, max_w=W - 200)

        def kpi(x, y, label, value, color, size=46):
            put(x, y, label, 20, MUTED)
            put(x, y + 28, value, size, color)

        def bar_rows(x, y, rows, color, row_h, label_w=330, bar_w=470):
            top = max([v for _, v in rows] + [1])
            for i, (label, val) in enumerate(rows):
                yy = y + i * row_h
                put(x, yy, label, 20, TXT, max_w=label_w - 12)
                w = max(8, int(bar_w * val / top))
                d.rounded_rectangle([x + label_w, yy + 2, x + label_w + w, yy + 26], radius=7, fill=color)
                put(x + label_w + w + 10, yy, _fmt(val), 20, MUTED)

        put(48, 30, "SEKELA MEDIA — ANALYTICS", 40, GOLD)
        put(48, 86, addis_now().strftime("%Y-%m-%d %H:%M") + " (Addis Ababa)", 22, MUTED)

        # Telegram
        panel(140, 150, f"Telegram — {TARGET_CHANNEL}", BLUE)
        kpi(76, 196, "Members", _fmt(data.get("tg")), BLUE if data.get("tg") is not None else RED)

        # YouTube
        yt = data.get("yt")
        yt_title = (yt or {}).get("title") if yt and not yt.get("error") else "not connected"
        panel(310, 470, f"YouTube — {yt_title}", RED if not yt or yt.get("error") else (255, 70, 70))
        if yt and not yt.get("error"):
            kpi(76, 366, "Subscribers", "hidden" if yt["hidden_subs"] else _fmt(yt["subs"]), GOLD)
            kpi(396, 366, "Total views", _fmt(yt["views"]), GREEN)
            kpi(736, 366, "Videos", _fmt(yt["videos"]), BLUE)
            put(76, 460, "Recent videos — views", 22, TXT)
            rows = [(v["title"].replace("\n", " "), v["views"]) for v in yt["recent"][:6]]
            if rows:
                bar_rows(76, 500, rows, (255, 70, 70), 44)
            else:
                put(76, 500, "No recent uploads found", 20, MUTED)
        else:
            put(76, 380, (yt or {}).get("error") or "Add YOUTUBE_* variables on Render", 22, RED, max_w=W - 160)

        # Facebook
        fb = data.get("fb") or {}
        panel(800, 340, f"Facebook — {FB_PAGE_NAME}", PURPLE)
        if fb.get("_ok"):
            kpi(76, 856, "Followers", _fmt(fb.get("followers")), GOLD)
            kpi(396, 856, "Reach (7d)", _fmt(fb.get("reach")), GREEN)
            kpi(736, 856, "Engagement (7d)", _fmt(fb.get("engagement")), BLUE)
            rows = [(lbl, fb[k]) for lbl, k in (("Views (7d)", "views"), ("Reach (7d)", "reach"),
                                                ("Engagement (7d)", "engagement"), ("New followers (7d)", "new_follows"))
                    if isinstance(fb.get(k), (int, float))]
            if rows:
                bar_rows(76, 972, rows, PURPLE, 44)
        else:
            put(76, 870, "Token needed: " + (fb.get("_err") or "FB_PAGE_ACCESS_TOKEN"), 22, RED, max_w=W - 160)
            put(76, 912, "Create a never-expiring Page Access Token", 20, MUTED)

        # Bot activity
        panel(1160, 150, "Sekela bot activity (since last restart)", GOLD)
        for i, (lbl, key, col) in enumerate((("Drafts", "drafts", GOLD), ("TG posts", "tg_posts", BLUE),
                                              ("FB posts", "fb_posts", PURPLE), ("YT uploads", "yt_uploads", RED),
                                              ("Breaking", "breaking", GREEN))):
            kpi(76 + i * 195, 1214, lbl, str(bot_stats.get(key, 0)), col, size=38)

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=92)
        return out.getvalue()
    except Exception as e:
        log.warning("analytics card render failed: %s", e)
        return None


def analytics_summary_text(data):
    parts = ["📣 Telegram — " + str(TARGET_CHANNEL) + "\n" +
             (f"👥 Members: {data['tg']:,}" if data.get("tg") is not None else "⚠️ ቁጥሩን ማምጣት አልተቻለም (ቦቱ አድሚን መሆን አለበት)"),
             yt_analytics_text(data.get("yt")) if (data.get("yt") or yt_enabled()) else yt_analytics_text(None),
             data.get("fb_text", ""),
             ("🤖 የቦት እንቅስቃሴ: ድራፍቶች {drafts} · TG ፖስቶች {tg_posts} · FB ፖስቶች {fb_posts} · "
              "YT uploads {yt_uploads} · ሰበር ዜና {breaking}").format(**bot_stats)]
    return "\n\n".join(p for p in parts if p)


MILESTONES = [1000, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000]
analytics_prev = {}


def make_snapshot(data):
    snap = {"tg": data.get("tg"), "yt": None, "fb": None}
    yt = data.get("yt")
    if yt and not yt.get("error"):
        snap["yt"] = {"subs": yt["subs"], "views": yt["views"],
                      "videos": {v["id"]: {"title": v["title"], "views": v["views"]} for v in yt["recent"]}}
    fb = data.get("fb") or {}
    if fb.get("_ok"):
        snap["fb"] = {"followers": fb.get("followers"), "reach": fb.get("reach")}
    return snap


def analytics_changes(prev, cur):
    """Human-readable notes for notable changes between two snapshots (empty list = nothing worth a ping)."""
    notes = []
    tg_d = int(os.environ.get("ALERT_TG_DELTA", "10"))
    subs_d = int(os.environ.get("ALERT_SUBS_DELTA", "3"))
    views_d = int(os.environ.get("ALERT_VIEWS_DELTA", "1000"))
    fb_d = int(os.environ.get("ALERT_FB_DELTA", "5"))
    if prev.get("tg") is not None and cur.get("tg") is not None:
        delta = cur["tg"] - prev["tg"]
        if abs(delta) >= tg_d:
            notes.append(f"📣 Telegram ተመዝጋቢዎች {'+' if delta > 0 else ''}{delta} → {cur['tg']:,}")
    py, cy = prev.get("yt"), cur.get("yt")
    if py and cy:
        ds = cy["subs"] - py["subs"]
        if ds >= subs_d or ds <= -5:
            notes.append(f"▶️ YouTube subscribers {'+' if ds > 0 else ''}{ds} → {cy['subs']:,}")
        dv = cy["views"] - py["views"]
        if dv >= views_d:
            notes.append(f"👁 YouTube views +{dv:,} ከባለፈው ፍተሻ ወዲህ (ጠቅላላ {cy['views']:,})")
        for vid, v in cy["videos"].items():
            old = py["videos"].get(vid)
            if old is None:
                notes.append(f"🎬 አዲስ ቪዲዮ ተለቋል: {v['title'][:50]}")
                continue
            for m in MILESTONES:
                if old["views"] < m <= v["views"]:
                    notes.append(f"🔥 «{v['title'][:45]}» {m:,} views አልፏል! ({v['views']:,})")
    pf, cf = prev.get("fb"), cur.get("fb")
    if pf and cf:
        if isinstance(pf.get("followers"), int) and isinstance(cf.get("followers"), int):
            df = cf["followers"] - pf["followers"]
            if abs(df) >= fb_d:
                notes.append(f"📘 Facebook ተከታዮች {'+' if df > 0 else ''}{df} → {cf['followers']:,}")
        if isinstance(pf.get("reach"), int) and isinstance(cf.get("reach"), int):
            if cf["reach"] >= 200 and cf["reach"] >= pf["reach"] * 1.5:
                notes.append(f"📈 Facebook reach ጨምሯል: {pf['reach']:,} → {cf['reach']:,} (7 ቀን)")
    return notes


def analytics_alert_once():
    """Collect a snapshot, compare with the previous one and push a summary card to Henok's DM if notable.
    The first run after a restart only stores the baseline. Returns the number of notes sent."""
    data = collect_analytics()
    cur = make_snapshot(data)
    prev = dict(analytics_prev)
    analytics_prev.clear()
    analytics_prev.update(cur)
    if not prev:
        return 0
    notes = analytics_changes(prev, cur)
    if not notes:
        return 0
    card = render_analytics_card(data)
    caption = ("📊 ሄኖክ፣ ጠቃሚ የ analytics ለውጥ አለ!\n\n" + "\n".join(notes))[:1020]
    if card:
        try:
            bot.send_photo(OWNER_ID, io.BytesIO(card), caption=caption)
            return len(notes)
        except Exception as e:
            log.warning("alert photo failed: %s", e)
    bot.send_message(OWNER_ID, caption)
    return len(notes)


def alerts_loop():
    time.sleep(300)
    while True:
        try:
            if not in_quiet_hours():
                n = analytics_alert_once()
                log.info("analytics alert check: %d notes", n)
        except Exception as e:
            log.error("alerts loop error: %s", e)
        time.sleep(max(10, int(os.environ.get("ALERT_INTERVAL_MIN", "240"))) * 60)


def start_alerts():
    if os.environ.get("ALERTS_ENABLED", "1") == "0":
        return
    threading.Thread(target=alerts_loop, daemon=True, name="alerts").start()
    log.info("Analytics alerts started (every %s min)", os.environ.get("ALERT_INTERVAL_MIN", "240"))


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
                                  "አሳጥረህ ወይም compress አድርገህ ላክልኝ 🎞 (ረጅም ቪዲዮ ከሆነ የ YouTube ሊንክ ከ /shorts ጋር ላክ)")
        return
    ack_id = None
    if kind in ("audio", "video"):
        what = "ድምፁን" if kind == "audio" else "ቪዲዮውን"
        ack_id = send_progress(chat_id, f"⏳ ሄኖክ፣ {what} እየተነተንኩ ነው ({media_eta(kind, size, duration)} ይወስዳል)...")
    try:
        markup = None
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
                if kind == "video":
                    vid = uuid.uuid4().hex[:10]
                    with state_lock:
                        video_cache[vid] = {"data": data, "ts": time.time()}
                    prune(video_cache, 2)  # keep memory small on the free tier
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("✂️ ክሊፖቹን ቆርጠህ 9:16 Shorts አዘጋጅ", callback_data=f"vc:{vid}"))
        deliver(chat_id, ack_id, result + "\n\n" + took(start), markup)
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
    "📰 /news — ዋና ዋና ዜናዎች (ከታማኝ ምንጮች) → ቁልፍ ስትነካ ካርድ + ፖስት\n"
    "🔔 /breaking — ሰበር ዜና አሁን ፈልግ (ቦቱ በራሱም ይፈትሻል እና ያሳውቅሃል)\n"
    "📝 /post — የሰከላ ኒውስ ካርድ + ፖስት → ✅ ስትነካ ወደ Telegram እና Facebook\n"
    "📊 /analytics — Telegram + YouTube + Facebook ዳሽቦርድ (ምስል) — ጠቃሚ ለውጥ ሲኖር በራሴ አሳውቅሃለሁ\n"
    "✂️ /shorts — ረጅም ቪዲዮ (YouTube ሊንክ ወይም ፋይል) → 9:16 Shorts → ✅ ስትነካ YouTube ላይ\n"
    "🎙 ድምፅ ላክልኝ (እስከ 5 ደቂቃ) — ትራንስክሪፕት + ማጠቃለያ + Action Items\n"
    "🎬 ቪዲዮ (እስከ 18MB) — ትንታኔ + 3 ቫይራል ክሊፖች + ✂️ መቁረጫ ቁልፍ\n"
    "🖼 ፎቶ — የ Canva ዲዛይን አስተያየት\n"
    "💬 ወይም ማንኛውንም ነገር ጻፍልኝ — ስክሪፕት፣ ሀሳብ፣ ጥናት\n"
    "🩺 /ai_status — የ AI፣ የቻናል፣ YouTube እና Facebook ሁኔታ\n\n"
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


@bot.message_handler(commands=["analytics", "fb_status", "youtube"])
@private_owner_only
def cmd_analytics(message):
    start = time.time()
    chat_id = message.chat.id
    ack_id = send_progress(chat_id, "📊 ሄኖክ፣ የ Telegram፣ YouTube እና Facebook መረጃ እየሰበሰብኩ ነው...")
    try:
        data = collect_analytics()
        analytics_prev.clear()
        analytics_prev.update(make_snapshot(data))  # a manual check also refreshes the alert baseline
        card = render_analytics_card(data)
        summary = analytics_summary_text(data) + "\n\n" + took(start)
    except Exception as e:
        log.error("analytics failed: %s", e)
        deliver(chat_id, ack_id, f"ሄኖክ፣ analytics ማምጣት አልተሳካም 😅 ({short_reason(e)})")
        return
    drop_message(chat_id, ack_id)
    if card:
        try:
            bot.send_photo(chat_id, io.BytesIO(card), caption="📊 ሰከላ ሚዲያ — Analytics Dashboard")
        except Exception as e:
            log.warning("analytics photo send failed: %s", e)
    deliver(chat_id, None, summary)


@bot.message_handler(commands=["shorts", "clips"])
@private_owner_only
def cmd_shorts(message):
    chat_id, uid = message.chat.id, message.from_user.id
    m = re.search(r"https?://\S+", message.text or "")
    if m:
        url = m.group(0)
        if not is_youtube_url(url):
            bot.send_message(chat_id, "ሄኖክ፣ ለአሁን የ YouTube ሊንክ ብቻ ነው የምቀበለው 🙏 ወይም ቪዲዮውን እዚህ ላክልኝ።")
            return
        run_shorts(chat_id, url=url)
        return
    with state_lock:
        awaiting_topic[uid] = "shorts"
    bot.send_message(chat_id, "እሺ ሄኖክ ✂️ የረጅሙን ቪዲዮ የ YouTube ሊንክ ላክልኝ፤ ወይም ቪዲዮውን ፋይል (እስከ 18MB) ላክልኝ → "
                              "ከትንታኔው በታች ✂️ ቁልፍ ይመጣል። (ለመሰረዝ /cancel)")


@bot.message_handler(commands=["breaking", "hunt"])
@private_owner_only
def cmd_breaking(message):
    start = time.time()
    chat_id = message.chat.id
    ack_id = send_progress(chat_id, "⏳ ሄኖክ፣ ሰበር ዜናዎችን እየፈተሽኩ ነው (ባለፉት 12 ሰዓታት፣ አስፈላጊ ከሆነ 24 ሰዓት) — ከ40-90 ሰከንድ ይወስዳል...")
    status = hunt_once(manual=True, min_importance=5)
    if status == "sent":
        drop_message(chat_id, ack_id)
        return
    msgs = {
        "none": "ሄኖክ፣ አሁን አዲስ ያልቀረበ ዜና አልተገኘም — ቀደም ብለው የተላኩልህን ዜናዎች ደግሜ አልልክም። ሲገኝ በራሴ አሳውቅሃለሁ 🔔",
        "failed": "ሄኖክ፣ ዜናውን አሁን ማዘጋጀት አልቻልኩም 😅 ጥቂት ቆይተህ እንደገና ሞክር።",
        "busy": "ሄኖክ፣ ፍተሻው አሁን እየተካሄደ ነው — ጥቂት ቆይተህ ሞክር።",
        "nokey": "GEMINI_API_KEY አልተሞላም — ሰበር ዜና መፈለግ አልችልም።",
        "cap": "የዛሬው የሰበር ዜና ገደብ ደርሷል።",
    }
    deliver(chat_id, ack_id, msgs.get(status, msgs["failed"]) + "\n\n" + took(start))


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
    active = get_openrouter_models(force=True)
    with _or_lock:
        ids = _or_cache.get("ids")
    for i, m in enumerate(OPENROUTER_BASE_MODELS):
        role = "ዋና" if i == 0 else f"ተጠባባቂ {i}"
        if ids is None:
            lines.append(f"  • {m} ({role})")
        elif m in ids:
            lines.append(f"  ✅ {m} ({role})")
        else:
            lines.append(f"  ❌ {m} ({role}) — በ OpenRouter ዝርዝር ውስጥ የለም፤ OPENROUTER_MODELS ቀይር")
    for m in active:
        if m not in OPENROUTER_BASE_MODELS:
            lines.append(f"  ✅ {m} (በራሱ የተረጋገጠ ተጨማሪ ተጠባባቂ)")
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
        me = _fb_request("GET", "me", params={"fields": "name,id"}, timeout=30)
        lines.append(f"  ✅ ተገናኝቷል: {me.get('name')} (id {me.get('id')})")
        if me.get("id") and str(me["id"]) != str(FB_PAGE_ID):
            lines.append("  ⚠️ ቶክኑ የሌላ ገጽ ነው — FB_PAGE_ID እና ቶክኑ አይዛመዱም")
    except FBError as e:
        lines.append(f"  ❌ {short_reason(e, 140)}")
        if fb_error_hint(e):
            lines.append("  💡 " + fb_error_hint(e))
    return lines


def _yt_status_lines():
    lines = ["▶️ YouTube:"]
    if not yt_enabled():
        lines.append("  ⚪ YOUTUBE_CLIENT_ID / SECRET / REFRESH_TOKEN አልተሞሉም")
    else:
        try:
            _yt_get("channels", {"part": "id", "mine": "true"})
            s = yt_channel_stats()
            lines.append(f"  ✅ ተገናኝቷል: {s['title']} ({s['subs']:,} subs · {s['videos']:,} videos)")
        except Exception as e:
            lines.append(f"  ❌ {short_reason(e, 160)}")
    lines.append("  ✅ ffmpeg ዝግጁ (Shorts መቁረጥ ይሰራል)" if get_ffmpeg()
                 else "  ❌ ffmpeg የለም — requirements.txt ላይ imageio-ffmpeg ጨምር")
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
        lines.append("")
        lines += _yt_status_lines()
        font = find_ethiopic_font() if PIL_OK else None
        lines.append("\n🖼 የካርድ ርዕስ ፊደል (Ethiopic font): " + ("✅ ተገኝቷል" if font else "❌ የለም — NotoSansEthiopic-Bold.ttf ኮሚት አድርግ"))
        lines.append(f"👤 Owner ID: {OWNER_ID}")
        lines.append(f"🕒 የአዲስ አበባ ሰዓት: {addis_now().strftime('%Y-%m-%d %H:%M')}")
        lines.append("\n" + took(start))
    deliver(chat_id, None, "\n".join(lines))


# ----------------------------------------------------------------------------
# Callbacks (categories + approval gates) - private chat with owner only
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
            run_post(chat_id, item["label"], item["topic"])

        elif data.startswith("st:"):
            _, nid, idx = data.split(":", 2)
            with state_lock:
                item = news_cache.get(nid)
            if not item or not item["stories"] or int(idx) >= len(item["stories"]):
                bot.send_message(chat_id, "ሄኖክ፣ ይህ መረጃ ጊዜው አልፏል — /news ብለህ እንደገና ጠይቀኝ 🙏")
                return
            s = item["stories"][int(idx)]
            run_post(chat_id, item["label"], f"{s['title']}. {s['summary']}")

        elif data.startswith("ok:"):
            did = data.split(":", 1)[1]
            with state_lock:
                d = drafts.get(did)
            if d is not None and d.get("unverified") and not d.get("confirmed"):
                bot.send_message(chat_id, "⚠️ ይህ ረቂቅ ያልተረጋገጠ ነው (ቀጥታ ምንጭ አልተገኘም)። ወደ ህዝብ ከመውጣቱ በፊት "
                                          "በእርግጥ ማጽደቅ ትፈልጋለህ?")
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("⚠️ አዎ፣ ያልተረጋገጠ መሆኑን አውቄ ፖስት አድርግ", callback_data=f"okc:{did}"))
                kb.add(types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"no:{did}"))
                try:
                    bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=kb)
                except Exception:
                    pass
                return
            approve_and_publish(chat_id, msg_id, did)

        elif data.startswith("okc:"):
            did = data.split(":", 1)[1]
            with state_lock:
                if did in drafts:
                    drafts[did]["confirmed"] = True
            approve_and_publish(chat_id, msg_id, did)

        elif data.startswith("no:"):
            did = data.split(":", 1)[1]
            with state_lock:
                drafts.pop(did, None)
            bot.edit_message_text("❌ ተሰርዟል — ምንም አልተለጠፈም።", chat_id, msg_id)

        elif data.startswith("vc:"):  # cut Shorts from a video that was just analysed
            vid = data.split(":", 1)[1]
            with state_lock:
                item = video_cache.pop(vid, None)
            if not item:
                bot.send_message(chat_id, "ሄኖክ፣ ቪዲዮው ከማህደረ ትውስታ ተሰርዟል — እንደገና ላክልኝ 🙏")
                return
            try:
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
            except Exception:
                pass
            run_shorts(chat_id, data=item["data"])

        elif data.startswith("yk:"):  # APPROVAL: upload a Short to YouTube
            approve_short_upload(chat_id, msg_id, data.split(":", 1)[1])

        elif data.startswith("yn:"):
            sid = data.split(":", 1)[1]
            with state_lock:
                d = shorts_drafts.pop(sid, None)
            if d:
                shutil.rmtree(os.path.dirname(d["path"]), ignore_errors=True)
            try:
                bot.edit_message_caption("❌ ተሰርዟል — YouTube ላይ ምንም አልተጫነም።", chat_id, msg_id)
            except Exception:
                bot.send_message(chat_id, "❌ ተሰርዟል — YouTube ላይ ምንም አልተጫነም።")

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

    # Topic typed after tapping "write my own topic" (or a link after /shorts)
    with state_lock:
        pending_mode = awaiting_topic.pop(uid, None)
    if pending_mode == "shorts":
        m = re.search(r"https?://\S+", text)
        if not m or not is_youtube_url(m.group(0)):
            bot.send_message(chat_id, "ሄኖክ፣ ትክክለኛ የ YouTube ሊንክ አላገኘሁም 🙏 /shorts ብለህ እንደገና ሞክር።")
            return
        run_shorts(chat_id, url=m.group(0))
        return
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
    log.info("Starting Henok Assistant agent... Telegram target: %s | Facebook: %s | YouTube: %s", TARGET_CHANNEL,
             "on" if fb_enabled() else "off (no FB_PAGE_ACCESS_TOKEN)", "on" if yt_enabled() else "off")

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
    if OPENROUTER_API_KEY:  # verify model slugs once at startup (also warms the cache)
        threading.Thread(target=get_openrouter_models, daemon=True).start()
    start_hunter()   # proactive breaking-news hunter
    start_alerts()   # proactive analytics alerts

    try:
        bot.remove_webhook()
    except Exception as e:
        log.warning("remove_webhook failed: %s", e)
    try:
        bot.set_my_commands([
            types.BotCommand("news", "ዋና ዋና ዜናዎች (ከታማኝ ምንጮች)"),
            types.BotCommand("breaking", "ሰበር ዜና አሁን ፈልግ"),
            types.BotCommand("post", "የሰከላ ኒውስ ካርድ + ፖስት"),
            types.BotCommand("analytics", "Telegram + YouTube + Facebook ዳሽቦርድ"),
            types.BotCommand("shorts", "ረጅም ቪዲዮ → 9:16 Shorts"),
            types.BotCommand("ai_status", "የ AI፣ ቻናል፣ YouTube እና Facebook ሁኔታ"),
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
