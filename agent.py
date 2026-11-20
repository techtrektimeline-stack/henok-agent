# -*- coding: utf-8 -*-
"""
agent.py  -  Henok's 24/7 Telegram command cockpit (@HenokAssistantBot) + Sekela Media/Sport/Coffee/Bego publisher

Runs 24/7 on Render (Python 3 + pyTelegramBotAPI). Single file.

Environment variables
---------------------
Required : TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, GEMINI_API_KEY
Facebook : FB_PAGE_ACCESS_TOKEN  (NEVER-EXPIRING Page access token: pages_manage_posts, pages_read_engagement, read_insights)
           FB_PAGE_ID            (default 1252817724577069)
           FB_API_VERSION        (default v25.0)
           FB_SPORT_PAGE_ID      (Sekela Sport page, default 499790239874374)
           FB_SPORT_ACCESS_TOKEN (Sekela Sport Page access token)
YouTube  : YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
           YOUTUBE_REFRESH_TOKEN_1.._9 (+ YOUTUBE_NAME_1.._9) or YOUTUBE_TOKENS_JSON
Canva    : CANVA_CLIENT_ID, CANVA_CLIENT_SECRET, CANVA_SCOPES
Images   : Real photo -> Gemini Imagen (GEMINI_API_KEY) -> Pollinations (20s fallback) -> Branded 1080x1350 backdrop
           /image studio also uses HF_TOKEN and TOGETHER_API_KEY when set
Learning : LEARN_FILE (default agent_learning.json next to agent.py; pinned Telegram backup restores after restarts)
Sport    : SPORT_ENGINE=1, SPORT_LEAGUES, SPORT_POLL_SEC=75, SPORT_HIGHLIGHTS=1, SPORT_CLIP_MAX_SEC=60
News AI  : NEWS_WRITER_MODELS, NEWS_ALLOW_GEMINI=1, NEWS_PROOFREAD=1, YT_PRIVACY=public
Optional : OWNER_ID (511440586), TARGET_CHANNEL (@sekelamediaET), SPORT_CHANNEL (@SekelaSportET), PORT (10000),
           RENDER_EXTERNAL_URL, HUNTER_ENABLED=1, HUNT_INTERVAL_MIN=30, HUNT_SPORT_INTERVAL_MIN=40, HUNT_QUIET=23-6,
           ALERTS_ENABLED=1, ALERT_INTERVAL_MIN=240
"""

import base64
import copy
import functools
import glob
import hashlib
import io
import html as html_lib
import json
import math
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urljoin, urlparse

import requests
import telebot
from telebot import apihelper, types
from telebot.apihelper import ApiTelegramException

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat
    PIL_OK = True
except Exception:
    PIL_OK = False

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GITHUB_AI_TOKEN = os.environ.get("GITHUB_AI_TOKEN", "").strip()
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
    v = (value or "").strip()
    if not v:
        return "@sekelamediaET"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if "t.me/" in v:
        v = v.split("t.me/")[-1].strip("/")
    return v if v.startswith("@") else "@" + v


TARGET_CHANNEL = normalize_channel(os.environ.get("TARGET_CHANNEL", "@sekelamediaET"))
SPORT_CHANNEL = normalize_channel(os.environ.get("SPORT_CHANNEL", "@SekelaSportET"))
FB_SPORT_PAGE_ID = os.environ.get("FB_SPORT_PAGE_ID", "499790239874374").strip()
FB_SPORT_ACCESS_TOKEN = os.environ.get("FB_SPORT_ACCESS_TOKEN", "").strip()

CANVA_CLIENT_ID = os.environ.get("CANVA_CLIENT_ID", "").strip()
CANVA_CLIENT_SECRET = os.environ.get("CANVA_CLIENT_SECRET", "").strip()
CANVA_SCOPES = os.environ.get("CANVA_SCOPES", "profile:read design:meta:read").strip()
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()
TOGETHER_API_KEY = os.environ.get("TOGETHER_API_KEY", "").strip()


def _tg_link(channel, fallback):
    if isinstance(channel, str) and channel.startswith("@"):
        return "https://t.me/" + channel[1:]
    return fallback


BRANDS = {
    "media": {
        "key": "media", "emoji": "📰", "name_am": "ሰከላ ሚዲያ", "name_en": "Sekela Media",
        "badge": "ሰከላ ሚዲያ | Sekela Media",
        "tg": TARGET_CHANNEL, "tg_link": _tg_link(TARGET_CHANNEL, "https://t.me/sekelamediaET"),
        "tg_handle": str(TARGET_CHANNEL) if str(TARGET_CHANNEL).startswith("@") else "@sekelamediaET",
        "fb_id": FB_PAGE_ID, "fb_token": FB_PAGE_ACCESS_TOKEN, "fb_name": FB_PAGE_NAME,
        "fb_link": f"https://www.facebook.com/{FB_PAGE_ID}",
        "tags": ["#ሰከላ_ሚዲያ", "#SekelaMedia", "#ሰከላ_ኒውስ", "#SekelaNews"],
        "brief_note": "This is a general news story (Ethiopia, diaspora, economy, tech/AI, culture, global events) for ሰከላ ሚዲያ.",
    },
    "sport": {
        "key": "sport", "emoji": "⚽", "name_am": "ሰከላ ስፖርት", "name_en": "Sekela Sport",
        "badge": "ሰከላ ስፖርት | Sekela Sport",
        "tg": SPORT_CHANNEL, "tg_link": _tg_link(SPORT_CHANNEL, "https://t.me/SekelaSportET"),
        "tg_handle": str(SPORT_CHANNEL) if str(SPORT_CHANNEL).startswith("@") else "@SekelaSportET",
        "fb_id": FB_SPORT_PAGE_ID, "fb_token": FB_SPORT_ACCESS_TOKEN, "fb_name": "ሰከላ ስፖርት",
        "fb_link": f"https://www.facebook.com/{FB_SPORT_PAGE_ID}",
        "tags": ["#ሰከላ_ስፖርት", "#SekelaSport", "#ሰከላ_ሚዲያ", "#SekelaMedia"],
        "brief_note": "This is a SPORT story for ሰከላ ስፖርት: use vibrant football/sport vocabulary. Give scorer, minute & scoreline for goals, or player, clubs & fee for transfers ONLY if in the notes.",
    },
}
COFFEE_BADGE = "ሰከላ ኮፊ ታይም | Sekela Coffee Time"
COFFEE_TAGS = ["#ሰከላ_ኮፊ_ታይም", "#SekelaCoffeeTime", "#ሰከላ_ሚዲያ", "#SekelaMedia"]
BEGO_BADGE = "ሰከላ በጎ | Sekela Bego"
BEGO_TAGS = ["#ሰከላ_በጎ", "#SekelaBego", "#አፋልጉኝ", "#እርዳታ", "#ሰከላ_ሚዲያ"]
ALL_BRAND_TAGS = ({t.lower() for b in BRANDS.values() for t in b["tags"]}
                  | {t.lower() for t in COFFEE_TAGS}
                  | {t.lower() for t in BEGO_TAGS})


def other_brand(brand):
    return "sport" if brand == "media" else "media"


def brand_label(brand):
    b = BRANDS[brand]
    return f"{b['emoji']} {b['name_am']}"


def fb_enabled(brand="media"):
    b = BRANDS[brand]
    return bool(b["fb_id"] and b["fb_token"])


def brand_footer(brand):
    b = BRANDS[brand]
    tip = os.environ.get("TIP_LINK", "").strip()
    tip_line = f"\n📩 ጥቆማ / መረጃ ለመላክ: {tip}" if tip else ""
    return ("🔗 ፈጣን እና ታማኝ መረጃ ለማግኘት ይቀላቀሉን:\n"
            f"Telegram: {b['tg_link']}\nFacebook: {b['fb_link']}" + tip_line)


OPENROUTER_BASE_MODELS = [m.strip() for m in os.environ.get(
    "OPENROUTER_MODELS", "openai/gpt-4o-mini,anthropic/claude-haiku-4.5,deepseek/deepseek-chat").split(",")
    if m.strip()]
NEWS_WRITER_MODELS = [m.strip() for m in os.environ.get(
    "NEWS_WRITER_MODELS", "openai/gpt-4o-mini,anthropic/claude-haiku-4.5,deepseek/deepseek-chat").split(",")
    if m.strip()]
NEWS_ALLOW_GEMINI = os.environ.get("NEWS_ALLOW_GEMINI", "1").strip() != "0"
OPENROUTER_EXTRA_RULES = (("anthropic/claude", "haiku"), ("x-ai/grok", "mini"))
OPENROUTER_URL = "https://openrouter.ai/api/v1"

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_UPLOAD = "https://generativelanguage.googleapis.com/upload/v1beta/files"
GEMINI_MODELS = ["gemini-2.0-flash", "gemini-flash-latest", "gemini-2.5-pro", "gemini-pro-latest"]
STATUS_GEMINI_MODELS = ["gemini-2.0-flash", "gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-pro",
                        "gemini-pro-latest"]
GEMINI_TIMEOUT = 180

MAX_MEDIA_BYTES = 18 * 1024 * 1024
INLINE_LIMIT_BYTES = 12 * 1024 * 1024
MAX_HISTORY_TURNS = 8
CAPTION_LIMIT = 1024

AI_LABEL = True

BUSY_TEXT = ("ሄኖክ፣ አሁን መስመሮቹ ለአፍታ ተጨናንቀዋል 😅 መልእክትህ አልጠፋም — "
             "ከ1 ደቂቃ በኋላ ደግመህ ላክልኝ፣ ወዲያውኑ እመልሳለሁ።")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("henok-agent")


# ----------------------------------------------------------------------------
# Health server + self keep-alive
# ----------------------------------------------------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="text/plain; charset=utf-8", with_body=True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if with_body:
            self.wfile.write(payload)

    def _ok(self, with_body=True):
        self._send(200, b"OK - Henok Assistant is alive", with_body=with_body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") == "/canva/callback":
            try:
                ok, msg = canva_handle_callback(parse_qs(parsed.query))
            except Exception as e:
                log.error("canva callback crashed: %s", e)
                ok, msg = False, "ውስጣዊ ስህተት ተፈጥሯል — በ Telegram ላይ /canva ብለህ እንደገና ሞክር።"
            self._send(200 if ok else 400, canva_result_page(ok, msg).encode("utf-8"), "text/html; charset=utf-8")
            return
        self._ok()

    def do_HEAD(self):
        self._ok(with_body=False)

    def log_message(self, *args):
        pass


def start_health_server(port=None):
    port = int(os.environ.get("PORT", port if port is not None else PORT))
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
        log.info("Health server listening on port %s", server.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
    except Exception as e:
        log.error("Health server error: %s", e)


def start_keepalive():
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
histories = {}
awaiting_topic = {}   # user_id -> "news" | "post" | "post_media" | "post_sport" | "bego" | "image" | "shorts" | "highlight" | "edit:<pid>"
drafts = {}
news_cache = {}
video_cache = {}
shorts_drafts = {}
published = {}
bot_stats = {"drafts": 0, "tg_posts": 0, "fb_posts": 0, "yt_uploads": 0, "breaking": 0}

CATEGORIES = {
    "ai": ("🤖 AI እና ቴክኖሎጂ", "AI, Technology, Meta/Facebook/TikTok platform updates & digital tools"),
    "crypto": ("🪙 Crypto እና ገበያ", "Crypto, Bitcoin & Markets"),
    "biz": ("💼 ኢኮኖሚና ቢዝነስ", "Ethiopia Economy, Business, Birr, Jobs & Opportunities"),
    "creator": ("🎬 ፈጣሪዎችና ሶሻል ሚዲያ", "Ethiopian Creators, TikTok, YouTube, Video Creation & Social Media trends"),
    "sport": ("⚽ ስፖርት (እግር ኳስ)", "Football and sports: Ethiopian Premier League, English Premier League, Champions League, European leagues, transfers, Olympics, live goals"),
    "coffee": ("☕ ሰከላ ኮፊ ታይም", "Surprising, funny, educational, or weird-but-true viral stories from ANY field (tech, sport, Ethiopia, world)"),
    "bego": ("🤍 ሰከላ በጎ", "Urgent verified Ethiopian missing person appeals or medical/charity community support"),
}
CUSTOM_LABEL = "✍️ የራሴን ርዕስ እጽፋለሁ"

SPORT_WORDS = ["ስፖርት", "እግር ኳስ", "ኳስ", "ጎል", "ዝውውር", "ፕሪሚየር ሊግ", "ሻምፒየንስ ሊግ", "ኦሎምፒክ", "ቅዱስ ጊዮርጊስ",
               "football", "soccer", "premier league", "champions league", "la liga", "serie a", "bundesliga",
               "goal", "goals", "transfer", "arsenal", "chelsea", "liverpool", "man utd", "manchester united",
               "manchester city", "tottenham", "barcelona", "real madrid", "olympic", "olympics", "match", "fixture"]
COFFEE_WORDS = ["ኮፊ ታይም", "coffee time", "coffeetime", "ሰከላ ኮፊ"]
BEGO_WORDS = ["ሰከላ በጎ", "አፋልጉኝ", "እርዳታ", "bego", "missing person", "charity appeal"]
NEWS_WORDS = ["ዜና", "ትሬንድ", "ወቅታዊ", "አዲስ ነገር", "news", "trend", "trends", "latest", "trending"]
POST_WORDS = ["ፖስት", "ካፕሽን", "ለቻናል", "ለማህበረሰብ", "post", "caption"]
REALTIME_WORDS = ["ዛሬ", "አሁን", "ዋጋ", "ምንዛሬ", "ዜና", "ትሬንድ", "ወቅታዊ", "ቅርብ ጊዜ",
                  "today", "now", "price", "latest", "news", "trend", "current", "bitcoin", "btc",
                  "usd", "rate", "2026"]

GREET_FIRST_WORDS = {"hi", "hello", "hey", "selam", "salam", "ሰላም", "ታዲያስ", "ጤና"}
GREET_PHRASES = {"good morning", "good afternoon", "good evening", "እንዴት ነህ", "እንደምን ነህ",
                 "ጤና ይስጥልኝ", "ሰላም ነው", "እንዴት ነው", "ደህና ነህ", "እንዴት አለህ"}
THANKS_PHRASES = {"thanks", "thank you", "thx", "አመሰግናለሁ", "እናመሰግናለን", "ok", "okay", "እሺ", "ጥሩ"}

OUTLETS = [
    ("ቢቢሲ አማርኛ", r"bbc\s*amharic|bbc\.com/amharic|amharic\.bbc|ቢቢሲ\s*አማርኛ"),
    ("ቢቢሲ ስፖርት", r"bbc\s*sport"),
    ("ስካይ ስፖርትስ", r"sky\s*sports"),
    ("ዋልታ", r"walta|waltainfo|ዋልታ"),
    ("ቦርከና", r"borkena"),
    ("ፋብሪዚዮ ሮማኖ", r"fabrizio|romano"),
    ("ዘ ጋርዲያን", r"guardian"),
    ("ቲክቫህ ኢትዮጵያ", r"tikvah|ቲክቫህ"),
    ("ጉርሻ", r"gursha|ጉርሻ"),
    ("ብሐጃ", r"behaja|ብሐጃ"),
    ("3-4-3 ስፖርት", r"3-?4-?3|433"),
    ("ካፒታል ኢትዮጵያ", r"capital\s*ethiopia|capitalethiopia"),
    ("አዲስ ፎርቹን", r"addis\s*fortune|addisfortune"),
    ("ሸገር ኤፍኤም", r"sheger|ሸገር"),
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
    ("Google Trends", r"google\s*trends"),
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
        "BRAND CONTEXT: media brand 'ሰከላ ሚዲያ (Sekela Media)' on Facebook (ሰከላ Sekela) and Telegram (@sekelamediaET), "
        "including '☕ ሰከላ ኮፊ ታይም (Sekela Coffee Time)' and humanitarian segment '🤍 ሰከላ በጎ (Sekela Bego)'; "
        "sports brand 'ሰከላ ስፖርት (Sekela Sport)' on Facebook and Telegram (@SekelaSportET); YouTube 'Henok Travel'.\n\n"
        "AMHARIC QUALITY GUARDRAIL (strict):\n"
        "- Write natural, native, modern Addis Ababa Amharic. Think in Amharic; never translate word-for-word.\n"
        "- Use correct Ethiopic script and punctuation (። ፣ ፡). Keep standard tech/marketing terms in Latin letters "
        "(CapCut, Canva, Whop, Hook, CTA, Reels, Shorts, TikTok, AI, Prompt).\n"
        "- PRONOUN CONSISTENCY: you speak in first person SINGULAR (እኔ; አደርጋለሁ, እልካለሁ) and address Henok in "
        "masculine singular (አንተ; ታደርጋለህ, ትችላለህ, ሄኖክ).\n"
        "- Telegram plain text: NO markdown symbols (**, ##, backticks, tables). Use emojis and '•' bullets.\n"
        "- Never invent facts, numbers, phone numbers, bank accounts, or quotes. Never claim anything was published."
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
    g = "እንደምን አደርክ ሄኖክ! ☀️" if h < 12 else ("እንደምን ዋልክ ሄኖክ! 👋" if h < 18 else "እንደምን አመሸህ ሄኖክ! 🌙")
    return g + "\nከታች ባሉት ቁልፎች የሚፈልጉትን ምረጥ 👇 (ወይም /sekela)"


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
    chunks = split_text(text)
    last_id = ack_id
    for i, chunk in enumerate(chunks):
        mk = markup if i == len(chunks) - 1 else None
        if i == 0 and ack_id:
            try:
                bot.edit_message_text(chunk, chat_id, ack_id, reply_markup=mk, disable_web_page_preview=True)
                last_id = ack_id
                continue
            except Exception as e:
                log.warning("edit failed, sending new message: %s", e)
        sent = bot.send_message(chat_id, chunk, reply_markup=mk, disable_web_page_preview=True)
        last_id = getattr(sent, "message_id", last_id)
    return last_id


def send_progress(chat_id, text):
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
    extra = [c.get("refresh", "") for c in globals().get("YT_CHANNELS", [])]
    for secret in [FB_PAGE_ACCESS_TOKEN, GEMINI_API_KEY, OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, YT_CLIENT_SECRET,
                   YT_REFRESH_TOKEN, FB_SPORT_ACCESS_TOKEN, CANVA_CLIENT_SECRET, HF_TOKEN, TOGETHER_API_KEY] + extra:
        if secret:
            s = s.replace(secret, "***")
    return s[:limit]


class Typing:
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


_tip_waiters = {}
_tip_box = {}


def _handle_public_tip(message):
    user = getattr(message, "from_user", None)
    if not user:
        return False
    uid, chat_id = user.id, message.chat.id
    txt = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    low = txt.lower()
    now = time.time()
    # Prune expired waiters (>30m)
    for k in [k for k, ts in list(_tip_waiters.items()) if now - ts > 1800]:
        _tip_waiters.pop(k, None)
    if low.startswith("/start tip") or low == "/tip":
        _tip_waiters[uid] = now
        bot.send_message(
            chat_id,
            "🙏 እንኳን ወደ **ሰከላ ሚዲያ የጥቆማ መስመር** በደህና መጡ!\n\n"
            "ዜና፣ የአደጋ/ክስተት ፎቶ ወይም ቪዲዮ፣ ወይም የ«አፋልጉኝ / እርዳታ» መልእክትዎን "
            "አሁን እዚህ ይላኩልን — በቀጥታ ለአርታዒ ቡድናችን ይደርሳል።"
        )
        return True
    if uid not in _tip_waiters:
        return False
    _tip_waiters.pop(uid, None)
    sender = (getattr(user, "first_name", "") or "") + (f" (@{user.username})" if getattr(user, "username", None) else f" (ID:{uid})")
    tid = uuid.uuid4().hex[:10]
    photo_id = message.photo[-1].file_id if getattr(message, "photo", None) else None
    video_id = message.video.file_id if getattr(message, "video", None) else None
    _tip_box[tid] = {"text": txt, "photo_id": photo_id, "video_id": video_id, "sender": sender.strip(), "ts": now}
    if len(_tip_box) > 30:
        for k in sorted(_tip_box, key=lambda x: _tip_box[x]["ts"])[:-20]:
            _tip_box.pop(k, None)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("📝 ወደ ዜና ረቂቅ ቀይር", callback_data=f"tpn:{tid}"),
        types.InlineKeyboardButton("🤍 ወደ ሰከላ በጎ ቀይር", callback_data=f"tpb:{tid}"),
    )
    if video_id:
        kb.add(types.InlineKeyboardButton("🎬 ቪዲዮውን ብራንድ አድርገህ አዘጋጅ", callback_data=f"tpv:{tid}"))
    kb.add(types.InlineKeyboardButton("❌ አልፈው", callback_data=f"tpx:{tid}"))
    summary = f"📩 **አዲስ የህዝብ ጥቆማ ደርሷል!**\n👤 ላኪ: {sender.strip()}\n\n{txt or '(ጽሑፍ የለውም)'}"
    try:
        if photo_id:
            bot.send_photo(OWNER_ID, photo_id, caption=summary[:1020], reply_markup=kb)
        elif video_id:
            bot.send_video(OWNER_ID, video_id, caption=summary[:1020], reply_markup=kb)
        else:
            bot.send_message(OWNER_ID, summary[:4000], reply_markup=kb)
    except Exception as e:
        log.warning("tip relay failed: %s", e)
    bot.send_message(chat_id, "✅ እናመሰግናለን! መረጃዎ ለሰከላ ሚዲያ አርታዒ ቡድን ደርሷል።")
    return True


def private_owner_only(func):
    @functools.wraps(func)
    def wrapper(message, *a, **kw):
        if not is_private(message.chat):
            return
        user = getattr(message, "from_user", None)
        if user is None or user.id != OWNER_ID:
            if _handle_public_tip(message):
                return
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


_last_ai_error = {"or": "", "gm": "", "hunt": ""}


def _ai_error_hint():
    parts = []
    if _last_ai_error.get("hunt"):
        parts.append("Desk: " + _last_ai_error["hunt"])
    if _last_ai_error.get("or"):
        parts.append("OpenRouter: " + _last_ai_error["or"])
    if _last_ai_error.get("gm"):
        parts.append("Gemini: " + _last_ai_error["gm"])
    return ("\n🔍 ምክንያት: " + " | ".join(parts)) if parts else ""


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


_gm_lock = threading.Lock()
_gm_cache = {"ts": 0.0, "models": None}
_gm_bad = set()
GEMINI_PREFERRED = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash", "gemini-2.5-pro", "gemini-pro-latest"]
_GEMINI_SKIP = re.compile(r"omni|image|imagen|tts|live|audio|embed|thinking|exp|preview|lite|8b|robotics|computer|"
                          r"learnlm|gemma|aqa|veo", re.I)


def gemini_active_models(force=False):
    now = time.time()
    with _gm_lock:
        if not force and _gm_cache["models"] and now - _gm_cache["ts"] < 21600:
            return [m for m in _gm_cache["models"] if m not in _gm_bad] or list(GEMINI_PREFERRED)
    names = None
    try:
        names, token = set(), None
        for _ in range(6):
            params = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            r = requests.get(f"{GEMINI_BASE}/models", headers={"x-goog-api-key": GEMINI_API_KEY},
                             params=params, timeout=25)
            if r.status_code != 200:
                names = None
                break
            js = r.json()
            for m in js.get("models", []):
                if "generateContent" in (m.get("supportedGenerationMethods") or []):
                    names.add((m.get("name") or "").split("/", 1)[-1])
            token = js.get("nextPageToken")
            if not token:
                break
    except Exception as e:
        log.info("Gemini model list unavailable: %s", e)
        names = None
    if names:
        names = {n for n in names if "omni" not in n.lower()}
        models = [m for m in GEMINI_PREFERRED if m in names]
        extra = sorted(m for m in names if "flash" in m and m not in models and not _GEMINI_SKIP.search(m))
        models += extra[-1:]
        if not models:
            models = list(GEMINI_PREFERRED)
    else:
        models = list(GEMINI_PREFERRED)
    with _gm_lock:
        _gm_cache.update(ts=now if names else now - 21000, models=list(models))
    return [m for m in models if m not in _gm_bad] or models


get_gemini_models = gemini_active_models


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
    for model in gemini_active_models():
        try:
            text, sources = _extract(_gemini_call(model, payload))
            _last_ai_error["gm"] = ""
            return text, sources, model
        except Exception as e:
            last_err = e
            log.warning("Gemini model failed: %s", e)
            if "HTTP 404" in str(e):
                _gm_bad.add(model)
    if grounding and last_err and "HTTP 400" in str(last_err):
        payload.pop("tools", None)
        for model in gemini_active_models():
            try:
                text, sources = _extract(_gemini_call(model, payload))
                _last_ai_error["gm"] = ""
                return text, sources, model
            except Exception as e:
                last_err = e
    _last_ai_error["gm"] = short_reason(last_err, 90)
    raise GeminiError(f"all Gemini models failed: {last_err}")


RESEARCH_PROMPT = (
    "Current Addis Ababa date/time: {now_eat} (UTC: {now_utc}).\n"
    "Topic: {topic}\n\n"
    "Search the web NOW across: (1) Ethiopian state/mainstream outlets (Fana, EBC, ENA, Walta) and independent media "
    "(Addis Standard, Borkena, Capital Ethiopia, Addis Fortune, Sheger FM, DW/VOA/BBC Amharic); (2) Social media & "
    "diaspora/activist discussions (TikTok, YouTube, X/Twitter, public Facebook pages, Tikvah, Gursha, Behaja); "
    "(3) Google Trends & international reporting mentioning Ethiopia or major global events (economy, AI/tech, "
    "geopolitics, culture).\n"
    "Return concise ENGLISH research notes (max 380 words): verified facts (who, what, when, where), concrete "
    "numbers, background, OFFICIAL statements/quotes with speaker name/title, public reactions, and next steps.\n"
    "FRESHNESS RULE: Prioritize facts published within the last 24 hours (prefer 2-12 hours). Verify dates against "
    "current time and ignore 2-3 day old news.\n"
    "Bullet points only; after EACH fact put the outlet/platform in brackets, e.g. [Reuters], [Tikvah], [X], "
    "[TikTok], [YouTube], [Addis Standard]. Do not write Amharic. Do not invent anything."
)
RESEARCH_LITE_PROMPT = (
    "Current time: {now_utc} UTC. What are the latest verified facts (within the last 24 hours) about: {topic}? "
    "Check Ethiopian media, international outlets, X, YouTube, TikTok and public Facebook pages. Answer in English "
    "bullet points (max 250 words), put the outlet/platform in brackets after each fact. No Amharic. Do not invent."
)


def research(topic, lite=False):
    now_eat = addis_now().strftime("%Y-%m-%d %H:%M")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    tpl = RESEARCH_LITE_PROMPT if lite else RESEARCH_PROMPT
    prompt = tpl.replace("{topic}", topic).replace("{now_eat}", now_eat).replace("{now_utc}", now_utc)
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
    facts, sources = research(topic)
    if not facts:
        time.sleep(2)
        facts, sources = research(topic, lite=True)
    return facts, sources


def gemini_upload_file(data, mime, display_name):
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
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY missing")
    base = {
        "systemInstruction": {"parts": [{"text": system_prompt()}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    variants = []
    if thinking_budget is not None:
        variants.append((thinking_budget, max_tokens))
    variants.append((None, max_tokens))
    if max_tokens > 8192:
        variants.append((None, 8192))
    last_err, tried, errs = None, [], []
    for model in gemini_active_models():
        tried.append(model)
        for tb, mt in variants:
            payload = copy.deepcopy(base)
            payload["generationConfig"]["maxOutputTokens"] = mt
            if tb is not None:
                payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": tb}
            try:
                text, _ = _extract(_gemini_call(model, payload))
                return text
            except Exception as e:
                last_err = e
                code = re.search(r"HTTP (\d{3})", str(e))
                code = code.group(1) if code else "no-HTTP"
                errs.append(f"{model}={code}")
                log.warning("Multimodal %s failed: %s", model, e)
                if "HTTP 404" in str(e):
                    _gm_bad.add(model)
                    break
    raise GeminiError(f"all models failed for media ({', '.join(tried)}) [{'; '.join(dict.fromkeys(errs))}]: {last_err}")


# ----------------------------------------------------------------------------
# OpenRouter (primary writer) with Gemini fallback
# ----------------------------------------------------------------------------
_or_lock = threading.Lock()
_or_cache = {"ts": 0.0, "models": None, "ids": None}


def fetch_openrouter_ids():
    try:
        r = requests.get(f"{OPENROUTER_URL}/models", timeout=25)
        if r.status_code == 200:
            return {m.get("id") for m in r.json().get("data", []) if m.get("id")}
    except Exception as e:
        log.info("OpenRouter model list unavailable: %s", e)
    return None


def get_openrouter_models(force=False):
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


OPENROUTER_FREE_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "google/gemini-2.0-flash-exp:free",
    "openrouter/free",
]


def ordered_openrouter_models(prefer=None):
    base = get_openrouter_models()
    with _or_lock:
        ids = _or_cache.get("ids")
    free = [m for m in OPENROUTER_FREE_MODELS if (ids is None or m in ids) and m not in base]
    if not prefer:
        return base + free
    first = [m for m in prefer if ids is None or m in ids]
    return first + [m for m in base if m not in first] + [m for m in free if m not in first]


_or_no_credits = False


def openrouter_chat(messages, max_tokens=1800, temperature=0.8, models=None):
    global _or_no_credits
    if not OPENROUTER_API_KEY:
        return None, None
    cand_models = ordered_openrouter_models(models)
    if _or_no_credits or os.environ.get("OPENROUTER_FREE_ONLY", "0") == "1":
        cand_models = [m for m in cand_models if ":free" in m or m == "openrouter/free"]
    for model in cand_models:
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
                timeout=90,
            )
            if r.status_code == 401:
                _last_ai_error["or"] = "HTTP 401 (bad key)"
                log.warning("OpenRouter 401 (bad key) - switching to Gemini")
                return None, None
            if r.status_code == 402:
                _or_no_credits = True
                _last_ai_error["or"] = "HTTP 402 (no credits)"
                log.warning("OpenRouter 402 on %s - switching to free models only", model)
                if ":free" not in model and model != "openrouter/free":
                    continue
                return None, None
            if r.status_code != 200:
                raise RuntimeError(f"{model} HTTP {r.status_code}: {r.text[:160]}")
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            txt = txt.strip()
            if not txt:
                raise RuntimeError(f"{model} returned empty text")
            _last_ai_error["or"] = ""
            return txt, model
        except Exception as e:
            _last_ai_error["or"] = short_reason(e, 90)
            log.warning("OpenRouter model failed: %s", e)
    return None, None


def llm_write(user_text, history=None, system=None, allow_gemini=True, max_tokens=1800, temperature=0.8,
              models=None):
    history = history or []
    sys_prompt = system or system_prompt()
    messages = [{"role": "system", "content": sys_prompt}]
    for role, txt in history:
        messages.append({"role": "assistant" if role == "model" else "user", "content": txt})
    messages.append({"role": "user", "content": user_text})
    if GITHUB_AI_TOKEN:
        for gh_model in ("openai/gpt-4o-mini", "openai/gpt-4o"):
            try:
                r = requests.post(
                    "https://models.github.ai/inference/chat/completions",
                    headers={"Authorization": f"Bearer {GITHUB_AI_TOKEN}", "Content-Type": "application/json"},
                    json={"model": gh_model, "messages": messages,
                          "temperature": min(temperature, 0.6), "max_tokens": max_tokens},
                    timeout=60,
                )
                if r.status_code == 200:
                    txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    if txt.strip():
                        log.info("written by GitHub Models %s", gh_model)
                        return clean_md(txt.strip()), f"github:{gh_model}"
                elif r.status_code in (401, 403):
                    log.warning("GitHub Models %s auth error", r.status_code)
                    break
            except Exception as e:
                log.warning("GitHub Models (%s) failed: %s", gh_model, short_reason(e, 70))
    text, model = openrouter_chat(messages, max_tokens=max_tokens, temperature=temperature, models=models)
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
    if GROQ_API_KEY:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile", "messages": messages,
                      "temperature": min(temperature, 0.55), "max_tokens": max_tokens,
                      "frequency_penalty": 0.45, "presence_penalty": 0.25},
                timeout=60,
            )
            if r.status_code == 200:
                txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                if txt.strip():
                    log.info("written by Groq llama-3.3-70b-versatile (fallback)")
                    return clean_md(txt.strip()), "groq:llama-3.3-70b"
        except Exception as e:
            log.warning("Groq fallback failed: %s", e)
    # 4th emergency zero-key fallback (Pollinations OpenAI-compatible free endpoint)
    try:
        r = requests.post(
            "https://text.pollinations.ai/openai",
            headers={"Content-Type": "application/json"},
            json={"model": "openai", "messages": messages, "temperature": temperature},
            timeout=60,
        )
        if r.status_code == 200:
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if txt.strip():
                log.info("written by Pollinations Text (zero-key fallback)")
                return clean_md(txt.strip()), "pollinations:openai"
    except Exception as e:
        log.warning("Pollinations text fallback failed: %s", e)
    return None, None


def polish_amharic(text):
    if not OPENROUTER_API_KEY or not text:
        return text
    prompt = (
        "Rewrite the text below into flawless, natural, native Addis Ababa Amharic. Fix grammar, awkward phrasing "
        "and pronoun inconsistencies (speak to ሄኖክ in masculine singular: አንተ/ታደርጋለህ). KEEP every timestamp, "
        "number, score, hashtag, name and the structure/emojis exactly as they are. Do not add or remove facts. "
        "Plain text only, no markdown. Output only the rewritten text.\n\n" + text
    )
    out, _ = llm_write(prompt, allow_gemini=True, max_tokens=4000, temperature=0.4)
    if out and len(out) > len(text) * 0.5:
        return out
    return text


# ----------------------------------------------------------------------------
# Conversation (chat + live facts)
# ----------------------------------------------------------------------------
def answer(user_text, history=None, live=False):
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
# Unified /sekela Hub + Menus
# ----------------------------------------------------------------------------
def sekela_hub_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📰 ሰከላ ሚዲያ (Media)", callback_data="hub:media"),
        types.InlineKeyboardButton("⚽ ሰከላ ስፖርት (Sport)", callback_data="hub:sport"),
    )
    kb.add(
        types.InlineKeyboardButton("📊 Analytics", callback_data="hub:an"),
        types.InlineKeyboardButton("🎨 Image / Canva", callback_data="hub:img"),
    )
    kb.add(
        types.InlineKeyboardButton("🧠 Rules", callback_data="hub:rules"),
        types.InlineKeyboardButton("🩺 Status", callback_data="hub:status"),
    )
    return kb


def sekela_media_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🔔 ሰበር ዜና (Breaking)", callback_data="hub:media:break"),
        types.InlineKeyboardButton("📰 ዋና ዜናዎች", callback_data="hub:media:news"),
    )
    kb.add(
        types.InlineKeyboardButton("☕ ሰከላ ኮፊ ታይም", callback_data="hub:coffee"),
        types.InlineKeyboardButton("🤍 ሰከላ በጎ (እርዳታ/አፋልጉኝ)", callback_data="hub:bego"),
    )
    kb.add(
        types.InlineKeyboardButton("📝 ፈጣን ካርድ + ፖስት", callback_data="hub:media:fast"),
        types.InlineKeyboardButton("✍️ የራሴን ርዕስ ልጻፍ", callback_data="hub:media:custom"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ ወደ ዋናው መምረጫ", callback_data="hub:main"))
    return kb


def sekela_sport_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("⚽ የስፖርት ዜና & ጎሎች", callback_data="hub:sport:break"),
        types.InlineKeyboardButton("📅 የዛሬ/ነገ ጨዋታዎች", callback_data="hub:sport:sched"),
    )
    kb.add(
        types.InlineKeyboardButton("📝 የስፖርት ካርድ + ፖስት", callback_data="hub:sport:fast"),
        types.InlineKeyboardButton("✍️ የራሴን ርዕስ ልጻፍ", callback_data="hub:sport:custom"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ ወደ ዋናው መምረጫ", callback_data="hub:main"))
    return kb


def sekela_bego_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔍 ከማህበራዊ ሚዲያ ፈልግ (አፋልጉኝ / እርዳታ)", callback_data="hub:bego:search"))
    kb.add(types.InlineKeyboardButton("⬅️ ወደ ዋናው መምረጫ", callback_data="hub:main"))
    return kb


def sekela_image_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎨 AI ምስል / ሎጎ ስራ", callback_data="hub:img:ai"),
        types.InlineKeyboardButton("🖌 Canva ግንኙነት", callback_data="hub:img:canva"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ ወደ ዋናው መምረጫ", callback_data="hub:main"))
    return kb


def show_sekela_hub(chat_id, edit_id=None):
    intro = ("ሰላም ሄኖክ! 👋🔥 የሰከላ ዋና የቁጥጥር ማዕከል ዝግጁ ነው።\n"
             "ያለ ረጅም ትዕዛዝ ቁልፎቹን ብቻ ነክተህ በ ✅ አጽድቅ ወይም በ ❌ ሰርዝ።\n"
             "ምን እንሥራ? 👇")
    if edit_id:
        try:
            bot.edit_message_text(intro, chat_id, edit_id, reply_markup=sekela_hub_keyboard())
            return
        except Exception:
            pass
    bot.send_message(chat_id, intro, reply_markup=sekela_hub_keyboard())


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
    kb.add(
        types.InlineKeyboardButton(CATEGORIES["sport"][0], callback_data=f"cat:{mode}:sport"),
        types.InlineKeyboardButton(CATEGORIES["coffee"][0], callback_data=f"cat:{mode}:coffee"),
    )
    kb.add(types.InlineKeyboardButton(CATEGORIES["bego"][0], callback_data=f"cat:{mode}:bego"))
    kb.add(types.InlineKeyboardButton(CUSTOM_LABEL, callback_data=f"cat:{mode}:custom"))
    return kb


def show_categories(chat_id, mode):
    head = ("ሄኖክ፣ ስለየትኛው ዘርፍ ወቅታዊ መረጃ ላምጣልህ? 👇" if mode == "news"
            else "ሄኖክ፣ ለየትኛው ዘርፍ 1080x1350 ካርድ + ፖስት ላዘጋጅልህ? 👇")
    bot.send_message(chat_id, head, reply_markup=category_keyboard(mode))


# ----------------------------------------------------------------------------
# Newsroom engine: Sekela Media, Sekela Sport, Sekela Coffee Time, Sekela Bego
# ----------------------------------------------------------------------------
UNVERIFIED_SOURCE = "ሰከላ ሚዲያ ዴስክ (በማረጋገጥ ላይ)"

NEWSROOM_SYSTEM = (
    "You are the senior Amharic editor of 'ሰከላ ሚዲያ (Sekela Media)', 'ሰከላ ስፖርት (Sekela Sport)', 'ሰከላ ኮፊ ታይም', "
    "and 'ሰከላ በጎ'. You THINK in Amharic and write with Sekela's own distinctive, authoritative, vivid Addis Ababa "
    "journalistic voice - combining the warmth and clarity of Zehabesha, Gursha, Getu Temesgen and Tikvah.\n"
    "NATIVE-VOICE RULES (highest priority):\n"
    "- ZERO literal English-to-Amharic calques or broken idioms (NEVER translate 'ruled out' as 'ፍርድ' or 'by' as 'ባይ'; write crystal-clear natural Amharic like 'ከውድድሩ ውጪ ሊሆን ነው', 'በይፋ አረጋገጠ'). Put time/place naturally («ማክሰኞ ዕለት በአዲስ አበባ ...») and end sentences "
    "with strong verbs («አስታውቋል», «ይፋ አድርጓል», «ተፈራርመዋል», «አሻቅቧል», «ወርዷል», «ተጠናቋል»).\n"
    "- Connect sentences smoothly («በተጨማሪም», «ይህም», «ከዚህ ቀደም», «በሌላ በኩል», «በመሆኑም», «ይሁን እንጂ»). Mostly 12-22 words "
    "per sentence, never more than 26.\n"
    "- PERFECT GRAMMAR & HEADLINE ACCURACY: plural/collective subjects take PLURAL verbs («አምስት አሰልጣኞች ለቀቁ» or «ክለቡ አምስት አሰልጣኞችን አሰናበተ» — NEVER «አምስት አሰልጣኞችን ለቀቀ»), «ግጭቶች እየተባባሱ ናቸው», «ባለሥልጣናቱ አስታውቀዋል». Every paragraph MUST end with a complete sentence (።). "
    "singular subjects take singular verbs («ሚኒስቴሩ አስታውቋል»). Consistent tense inside each paragraph.\n"
    "- RICH AND SPECIFIC: give who, what, where, when, numbers, names, background, why it matters to Ethiopians, and "
    "what happens next — strictly from the notes. Never invent facts, phone numbers, bank accounts, or quotes.\n"
    "- ORIGINAL REWRITE RULE: NEVER copy or lightly paraphrase BBC Amharic, Fana, or any source wording. Extract ONLY the verified facts and rewrite 100% originally in Sekela's own voice with richer background context (4-6 clear sentences) and a short 2-line punchy card headline.\n"
    "- SAFETY RULE: NEVER write anything that incites ethnic or religious conflict, hate speech, or division.\n"
    "- Neutral third person; NEVER address anyone directly (never ሄኖክ/አንተ/እኛ).\n"
    "- ALWAYS use standard Western digits (0-9, e.g. 2026, 5, 10%) — NEVER Geez numerals (፩,፪). Plain text, no markdown, no emojis inside paragraphs; keep widely used Latin terms (AI, Bitcoin, TikTok, clubs/players)."
)

NEWS_BRIEF = (
    "Write ONE Amharic news item from the research notes in Sekela's signature journalistic voice (CRITICAL: even if Topic and Research Notes are 100% in English, write HEADLINE, LEAD, and CONTEXT 100% in natural Amharic Ethiopic script — NEVER output English sentences): bold headline -> "
    "hook -> full explanation -> sources.\n"
    "Topic: {topic}\n{brand_note}\n\n[RESEARCH NOTES]\n{facts}\n\n{mode_note}"
    "SEO: naturally include the main high-search keyword in the headline and hook.\n"
    "Return EXACTLY these blocks and nothing else:\n"
    "===HEADLINE===\n4-6 words: crystal-clear, grammatically natural, curiosity-sparking Amharic hook that captures attention and makes readers want to read the full story (for AI/tech/software topics, prefer high-save curiosity hooks like 'አዲሱ X ምንድነው?', 'X ከ Y በምን ይለያል?', or 'X ምን አዲስ ነገር ይዞ መጣ?'); no quotes, no emoji, no full stop, ZERO awkward literal translations.\n"
    "===LEAD===\nThe hook: 1-2 vivid sentences (25-45 words) answering who, what, where and when.\n"
    "===CONTEXT===\nThe full story: 4-6 connected sentences (80-130 words) with key figures, names, background, reactions "
    "and what comes next.\n"
    "===OFFICIAL===\nONE sentence with an official statement or quote, attributed to the named speaker/body. If none, write: NONE\n"
    "===SOURCE===\nOutlet/platform names from the notes separated by ' / ' (ቢቢሲ አማርኛ, ሮይተርስ, ፋና, ኢቢሲ, ኢዜአ, ዋልታ, "
    "ቲክቫህ ኢትዮጵያ, ጉርሻ, 3-4-3 ስፖርት, አል-አይን, ዶቼ ቬለ አማርኛ, AP, X, TikTok, YouTube).\n"
    "===TAGS===\n4-6 topic hashtags separated by spaces (Amharic + English; no brand tags).\n"
    "===IMAGE===\nOne English paragraph (25-45 words): symbolic, cinematic vertical 3:4 photojournalism scene, main "
    "subject in upper 60% of frame, high-contrast colour grade, no identifiable faces if sensitive, no text, no logos, no violence.\n"
)

COFFEE_BRIEF = (
    "Write ONE viral '☕ ሰከላ ኮፊ ታይም (Sekela Coffee Time)' story from the research notes — surprising, funny, "
    "educational, or mind-blowing weird-but-true story from ANY field (tech, sport, Ethiopia, science, or world) "
    "in the style of 60K+ liked viral cards.\n"
    "Topic: {topic}\n\n[RESEARCH NOTES]\n{facts}\n\n{mode_note}"
    "TONE: Conversational, warm, witty, unforgettable Addis Ababa Amharic.\n"
    "Return EXACTLY these blocks and nothing else:\n"
    "===HEADLINE===\n3-5 punchy words: huge curiosity-sparking headline for a photo card; no quotes, no emoji, no full stop.\n"
    "===LEAD===\nONE unforgettable curiosity-sparking 1-line hook sentence (12-25 words) that stops scrolling.\n"
    "===CONTEXT===\n4-5 engaging, vivid sentences (70-110 words) sharing the surprising details and why people love it.\n"
    "===OFFICIAL===\nNONE\n"
    "===SOURCE===\nOutlet/platform names from the notes separated by ' / '.\n"
    "===TAGS===\n4-6 viral Amharic & English hashtags (no brand tags).\n"
    "===IMAGE===\nOne English paragraph (25-45 words): clean, striking vertical 3:4 photo depicting the subject in the "
    "upper 60% of the frame, warm cinematic lighting, no text, no logos.\n"
)

BEGO_BRIEF = (
    "Write ONE compassionate, dignified, crystal-clear humanitarian/community post for '🤍 ሰከላ በጎ (Sekela Bego)' "
    "(missing person 'አፋልጉኝ', medical/charity appeal 'እርዳታ', or community help) using ONLY the facts provided.\n"
    "STRICT SAFETY RULE: Copy any Name, Location, Phone Number, or Bank Account EXACTLY as given in the notes. "
    "NEVER invent or guess a phone number, bank account, or address. If any is missing, omit it.\n"
    "Input / Notes:\n{facts}\n\n"
    "Return EXACTLY these blocks and nothing else:\n"
    "===HEADLINE===\n3-5 clear, respectful Amharic words for the card (e.g. አፋልጉኝ — ስም / አስቸኳይ የእርዳታ ጥሪ); no quotes, no emoji, no full stop.\n"
    "===LEAD===\n1-2 warm, urgent sentences clearly stating who needs help or is missing, and where.\n"
    "===CONTEXT===\n4-6 well-organized sentences or short clean lines highlighting: • ስም (Name), • ቦታ (Location), "
    "• ዝርዝር ሁኔታ (Situation), • የመገናኛ ስልክ / የባንክ ሂሳብ (Phone / Bank Account ONLY if provided), and a compassionate Call-to-Action to share & support.\n"
    "===OFFICIAL===\nNONE\n"
    "===SOURCE===\n{source_hint}\n"
    "===TAGS===\n3-5 relevant hashtags (no brand tags).\n"
    "===IMAGE===\nOne English paragraph (25-40 words): warm, dignified, hopeful vertical 3:4 humanitarian photography "
    "(community support, warm sunlight, gentle teal and gold tones, upper 60% clear), NO distressing/graphic scenes, NO text, NO logos.\n"
)

UNVERIFIED_NOTE = (
    "[NO LIVE RESEARCH WAS AVAILABLE] Write a careful editorial synthesis using ONLY well-established background "
    "facts you are certain of. Do NOT state specific recent numbers, dates, quotes or events. In ===SOURCE=== write "
    "exactly: " + UNVERIFIED_SOURCE + "\n\n"
)

IMAGE_STYLE = ("high-contrast cinematic news photography, dramatic lighting, shallow depth of field, ultra detailed, "
               "sharp focus, vertical 3:4 portrait composition, main subject centered in upper half, "
               "no text, no letters, no watermark")
IMAGE_FALLBACKS = {
    "media": "dramatic dusk skyline of a modern African capital with glowing city lights and newsroom mood, teal and gold grade",
    "sport": "floodlit football stadium pitch at night, dramatic stadium light beams, dark emerald and gold atmosphere",
    "coffee": "Ethiopian coffee ceremony smoke rising in golden morning sunlight on a sleek modern wooden table, cinematic photography",
    "bego": "warm hopeful community hands holding a glowing lantern in soft morning light, gentle teal and warm gold tones",
}

PKG_KEYS = ("headline", "lead", "context", "official", "source", "tags", "image")


def parse_package(text):
    out = {k: "" for k in PKG_KEYS}
    parts = re.split(r"===(HEADLINE|LEAD|CONTEXT|OFFICIAL|SOURCE|TAGS|IMAGE)===", text or "")
    for i in range(1, len(parts) - 1, 2):
        out[parts[i].lower()] = parts[i + 1].strip()
    return out


def _tidy_headline(s):
    s = re.sub(r"[\"'“”‘’«»]", "", s or "").strip()
    s = re.sub(r"^[^\wሀ-፼]+", "", s)
    return s.rstrip(".።").strip()


def clean_headline(h, fallback_text="", max_words=7):
    h = _tidy_headline(h) or _tidy_headline(fallback_text)
    return " ".join(h.split()[:max_words])


def detect_outlets(text):
    low = (text or "").lower()
    found = [name for name, rx in OUTLETS if re.search(rx, low)]
    for generic, specific in (("ቢቢሲ", "ቢቢሲ አማርኛ"), ("ቢቢሲ", "ቢቢሲ ስፖርት"), ("ዶቼ ቬለ", "ዶቼ ቬለ አማርኛ")):
        if generic in found and specific in found:
            found.remove(generic)
    return found


def _blob(facts, sources):
    return (facts or "") + " " + " ".join(f"{t} {u}" for t, u in (sources or []))


def resolve_source(raw_src, facts, sources, unverified=False):
    if unverified:
        return UNVERIFIED_SOURCE
    found = detect_outlets(raw_src or "") or detect_outlets(_blob(facts, sources))
    if found:
        return " / ".join(found[:3])
    if (raw_src or "").strip():
        return raw_src.strip()[:80]
    if sources:
        return sources[0][0] or urlparse(sources[0][1]).netloc or "ሰከላ ሚዲያ ዴስክ"
    return "የተረጋገጡ የዜና ምንጮች"


def build_tags(brand, raw, headline, segment=None):
    if segment == "coffee":
        brand_tags = list(COFFEE_TAGS)
    elif segment == "bego":
        brand_tags = list(BEGO_TAGS)
    else:
        brand_tags = list(BRANDS[brand]["tags"])
    seen = {t.lower() for t in brand_tags} | ALL_BRAND_TAGS
    dyn = []
    for tok in re.split(r"[\s,]+", raw or ""):
        t = re.sub(r"[^\w#]", "", tok).lstrip("#").strip("_")
        if len(t) < 2:
            continue
        t = "#" + t
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        dyn.append(t)
    if len(dyn) < 4:
        words = [re.sub(r"[^\w]", "", w) for w in (headline or "").split()[:2]]
        extras = (["#" + "_".join(w for w in words if w)] if any(words) else [])
        if segment == "coffee":
            extras += ["#CoffeeTime", "#ViralEthiopia"]
        elif segment == "bego":
            extras += ["#ኢትዮጵያ", "#Ethiopia", "#ድጋፍ"]
        elif brand == "media":
            extras += ["#ኢትዮጵያ", "#Ethiopia"]
        else:
            extras += ["#እግር_ኳስ", "#Football"]
        for t in extras:
            if len(dyn) >= 4:
                break
            if t.lower() not in seen and len(t) > 2:
                seen.add(t.lower())
                dyn.append(t)
    return brand_tags + dyn[:6]


def _para(s):
    s = clean_md(s or "")
    s = re.sub(r"^[\s•\-\*]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def too_thin(pkg):
    ctx = _para(pkg.get("context"))
    sents = [x for x in re.split(r"[።!?\n]+", ctx) if x.strip()]
    return len(sents) < 4 or len(ctx.split()) < 55


PROOF_PROMPT = (
    "You are a strict Amharic proofreader of a news desk. Below are LEAD, CONTEXT and OFFICIAL blocks. "
    "Fix ONLY grammar, subject-verb agreement (plural subjects need plural verbs: «ግጭቶች እየተባባሱ ናቸው»), tense, "
    "spelling, doubled words, and English-sounding calques. Keep all facts, numbers, names and attribution unchanged. "
    "Return EXACTLY:\n===LEAD===\n...\n===CONTEXT===\n...\n===OFFICIAL===\n...\n\n"
)


def proofread_pkg(pkg):
    if os.environ.get("NEWS_PROOFREAD", "1") == "0" or not OPENROUTER_API_KEY:
        return pkg
    body = (f"===LEAD===\n{pkg.get('lead', '')}\n===CONTEXT===\n{pkg.get('context', '')}\n"
            f"===OFFICIAL===\n{pkg.get('official') or 'NONE'}\n")
    out, _ = llm_write(PROOF_PROMPT + body, system=NEWSROOM_SYSTEM, allow_gemini=False, max_tokens=2000,
                       temperature=0.2, models=NEWS_WRITER_MODELS)
    if not out:
        return pkg
    parts = re.split(r"===(LEAD|CONTEXT|OFFICIAL)===", out)
    got = {parts[i].lower(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    new = dict(pkg)
    for k in ("lead", "context"):
        v = got.get(k, "")
        if not v or len(v) < 0.7 * len(pkg.get(k, "")):
            return pkg
        new[k] = v
    off = got.get("official", "")
    if off and (off.strip(" .።").upper() == "NONE" or len(off) >= 0.7 * len(pkg.get("official") or "")):
        new["official"] = off
    nums = lambda p: set(re.findall(r"\d[\d,.]*\d|\d", " ".join(str(p.get(k, "")) for k in ("lead", "context", "official"))))
    if nums(pkg) - nums(new):
        return pkg
    return new


def long_sentences(pkg, limit=27):
    txt = " ".join([pkg.get("lead", ""), pkg.get("context", ""), pkg.get("official", "")])
    return [x for x in re.split(r"[።!?\n]+", txt) if len(x.split()) > limit]


_GEEZ_DIGITS = str.maketrans("፩፪፫፬፭፮፯፰፱", "123456789")


def _clean_amharic_artifacts(text):
    t = re.sub(r"={0,3}(?:HEADLINE|LEAD|CONTEXT|OFFICIAL|SOURCE|TAGS|IMAGE)={0,3}", " ", text or "", flags=re.I)
    t = t.translate(_GEEZ_DIGITS)
    return re.sub(r"[ \t]+", " ", t).strip()


def _has_repetition_loop(text):
    words = (text or "").split()
    if len(words) < 12:
        return False
    seen_ng = {}
    for i in range(len(words) - 2):
        ng = " ".join(words[i:i + 3])
        seen_ng[ng] = seen_ng.get(ng, 0) + 1
        if seen_ng[ng] >= 4:
            return True
    return False


def _trim_complete_sentences(text):
    t = _clean_amharic_artifacts(text)
    if not t or t[-1] in "።.!?":
        return t
    m = re.search(r"^(.*[።.!?])", t, re.S)
    return m.group(1).strip() if m else t


def assemble_news_post(pkg, facts="", sources=None, unverified=False, brand="media", segment=None):
    lead = _trim_complete_sentences(_para(pkg.get("lead")))
    context = _trim_complete_sentences(_para(pkg.get("context")))
    official = _trim_complete_sentences(_para(pkg.get("official")))
    if official.strip(" .።").upper() in ("", "NONE", "N/A"):
        official = ""
    if not lead or not context:
        return None
    if not re.search(r"[ሀ-፼]{3,}", lead):
        return None
    if _has_repetition_loop(lead) or _has_repetition_loop(context):
        return None
    max_words = 5 if segment in ("coffee", "bego") else 7
    headline = clean_headline(pkg.get("headline"), lead, max_words=max_words)
    if not headline:
        return None
    if segment == "bego":
        src = (pkg.get("source") or "").strip() or "ሰከላ በጎ (ከተላከው መረጃ)"
        head_line = f"🤍 {headline}"
    elif segment == "coffee":
        src = resolve_source(pkg.get("source"), facts, sources, unverified)
        head_line = f"☕ {headline}"
    else:
        src = resolve_source(pkg.get("source"), facts, sources, unverified)
        head_line = f"{headline}\n#Ethiopia #ኢትዮጵያ" if brand == "media" else headline
    tags = " ".join(build_tags(brand, pkg.get("tags"), headline, segment=segment))
    parts = [head_line, lead, context]
    if official:
        parts.append(official)
    parts += [f"📌 ምንጭ፡ {src}", brand_footer(brand), tags]
    return "\n\n".join(parts), headline


STORY_PROMPT = (
    "Current Addis Ababa date/time: {now_eat} (UTC: {now_utc}).\n"
    "Search the web NOW and find {n} engaging, timely stories from the last {hours} hours about: {focus}\n"
    "MULTI-DESK COVERAGE (include ALL relevant pillars, not just breaking news):\n"
    "1) Ethiopian Ministries (~22 ministries: Health, Education, Finance, Foreign Affairs, Transport, MInT, Trade, Agriculture, "
    "Justice, Peace, Labor, Tourism, Water & Energy, Urban) + Public Utilities (Ethio Telecom, Ethiopian Electric Utility "
    "power outage/maintenance notices, AAWSA water alerts, NBE, Ethiopian Airlines, Immigration/Passport, Addis Mayor).\n"
    "2) Social Media & Creators: TikTok Creative Awards / vote leads, viral TikTokers, YouTubers, Instagram, X/Twitter "
    "Ethiopian commentators & private media (Tikvah, Getu Temesgen, Zehabesha, Rosha TV, Gursha, Sheger FM, Addis Standard).\n"
    "3) Peaceful Community & Faith Updates: Ethiopian Orthodox Tewahedo Church & Islamic Affairs announcements, holidays, charity.\n"
    "4) Global, Ethiopian TVs & Sport: EBC/Fana/Walta/Arts TV/EBS/Asham/fast Facebook news pages, viral TikTok debates "
    "(turn trending discussions into original news), US/UK/EU/Asia/China-Ethiopia relations, ANY new AI, software, app, gadget, or tech announcement/launch worldwide (not just articles — turn new product/model/software releases and trending tech discussions into news), BeSoccer/ESPN Football "
    "(pre-match previews, live goals, post-match reactions for EPL/UCL/Walias), Ethiopian Athletics/Running & Boxing.\n"
    "SAFETY FILTER (STRICT): NEVER include anything that incites ethnic or religious conflict, hate speech, or violence.\n"
    "{feeds}"
    "Return {n} stories, ONE per line, in EXACTLY this format (English):\n"
    "STORY|<importance 1-10>|<headline, max 14 words>|<outlet/platform names, comma separated>|<one sentence with the key fact>\n"
    "Importance: 8-10 = major breaking/viral/utility/match story, 4-7 = strong daily, ministry, creator, or global story."
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
    feed_block = ("RECENT HEADLINES FROM NEWS FEEDS & GOOGLE TRENDS (all <24h; verify before using):\n" + feeds + "\n\n"
                  if feeds else "")
    now_eat = addis_now().strftime("%Y-%m-%d %H:%M")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    prompt = (STORY_PROMPT.replace("{hours}", str(min(24, hours))).replace("{focus}", focus)
              .replace("{feeds}", feed_block).replace("{n}", str(n))
              .replace("{now_eat}", now_eat).replace("{now_utc}", now_utc))
    try:
        text, sources, _ = gemini_generate(
            [{"role": "user", "parts": [{"text": prompt}]}],
            system="You are a precise news monitoring assistant. Follow the output format and 24-hour freshness rule strictly.",
            grounding=True, temperature=0.2, max_tokens=4096)
        return parse_story_lines(text), sources
    except Exception as e:
        log.warning("story scan failed: %s", e)
        return [], []


def amharic_story_lines(stories):
    if not stories:
        return {}
    listing = "\n".join(f"{i}|{s['title']}|{s['summary']}" for i, s in enumerate(stories, 1))
    sys_tr = ("You are a professional Ethiopian newsroom translator. Translate EVERY item into natural, fluent Amharic "
              "(Ethiopic script). Keep exact format `N|<headline>|<summary>` — one line per item, nothing else.")
    prompt = ("Translate ALL items below into Amharic news style (headline max 8 words, summary 1 complete sentence). "
              "Do NOT skip any item and do NOT leave English sentences untranslated.\n" + listing)
    text, _ = llm_write(prompt, system=sys_tr, max_tokens=2800, temperature=0.2, allow_gemini=True, models=NEWS_WRITER_MODELS)
    out = {}
    for ln in (text or "").splitlines():
        m = re.match(r"^\s*(\d+)\s*\|(.+?)\|(.+)$", ln)
        if m:
            out[int(m.group(1))] = (clean_headline(m.group(2)), _para(m.group(3)))
    return out


NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def detect_brand(text):
    return "sport" if has_any(text, SPORT_WORDS) else "media"


def run_news(chat_id, label, topic, brand=None, segment=None):
    start = time.time()
    brand = brand or detect_brand(topic)
    ack_id = send_progress(chat_id, f"⏳ {label} — ዋና ዋና ዜናዎችን እየፈተሽኩ ነው (ከ15-35 ሰከንድ)...")
    try:
        with Typing(chat_id):
            tg_items = fetch_tg_channel_items(brand, max_age_hours=24, per_channel=3)
            feed_items = fetch_feed_items(brand, max_age_hours=24, per_feed=5)
            trends = fetch_google_trends(max_age_hours=24) if brand == "media" else []
            if brand == "media":
                tg_items = [it for it in tg_items if not has_any(it["title"], SPORT_WORDS)]
                trends = [t for t in trends if not has_any(t["title"], SPORT_WORDS)]
                feed_items = [it for it in feed_items if not has_any(it["title"], SPORT_WORDS)]
            primary_pool = tg_items + feed_items
            feeds_txt = feed_items_text(primary_pool + trends[:6])
            stories, sources = scan_stories(topic, hours=24, n=4, feeds=feeds_txt)
            if not stories and (primary_pool or trends):
                kws = [w for w in re.findall(r"[A-Za-z0-9ሀ-፼]{3,}", topic.lower()) if w not in STOP_KEYS]
                pool = primary_pool or trends
                matched = [it for it in pool if any(k in it["title"].lower() for k in kws)] if kws else []
                stories = stories_from_feeds(matched or pool, n=4)
                if not sources:
                    sources = [(s["outlets"], s["link"]) for s in stories if s.get("link")]
            am = amharic_story_lines(stories) if stories else {}
        nid = uuid.uuid4().hex[:10]
        with state_lock:
            news_cache[nid] = {"label": label, "topic": topic, "stories": stories, "sources": sources,
                               "brand": brand, "segment": segment, "ts": time.time()}
        prune(news_cache)
        kb = types.InlineKeyboardMarkup()
        if not stories:
            kb.add(types.InlineKeyboardButton("📝 ስለ ርዕሱ በቀጥታ ፖስት አዘጋጅ", callback_data=f"mk:{nid}"))
            deliver(chat_id, ack_id, f"ሄኖክ፣ ስለ «{label}» ከታማኝ ምንጮች አዲስ ዜና አሁን አልተገኘም። "
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
        deliver(chat_id, ack_id, body, kb)
    except Exception as e:
        log.error("news failed: %s", e)
        deliver(chat_id, ack_id, "ሄኖክ፣ ዜናውን አሁን ማምጣት አልቻልኩም 😅 ጥቂት ቆይተህ እንደገና ሞክር።")


# ----------------------------------------------------------------------------
# 1080x1350 (4:5 Portrait) Cards + Fast Image Priority Pipeline
# ----------------------------------------------------------------------------
def build_image_prompt(scene, brand="media", segment=None):
    scene = re.sub(r"\s+", " ", (scene or "")).strip()
    if not scene or re.search(r"[ሀ-፼]", scene):
        key = segment if segment in ("coffee", "bego") else brand
        scene = IMAGE_FALLBACKS.get(key, IMAGE_FALLBACKS["media"])
    return (scene[:420].rstrip(" ,.") + ", " + IMAGE_STYLE)[:640]


def pollinations_url(prompt, model="flux"):
    return (f"https://image.pollinations.ai/prompt/{quote(prompt[:520], safe='')}"
            f"?model={model}&width=1080&height=1350&nologo=true&seed={int(time.time())}")


def _fallback_backdrop(brand="media", segment=None, width=1080, height=1350):
    if not PIL_OK:
        return None
    key = segment if segment in ("coffee", "bego") else brand
    accents = {"media": (200, 16, 46), "sport": (0, 135, 81), "coffee": (200, 125, 50), "bego": (214, 175, 62)}
    ac = accents.get(key, accents["media"])
    im = Image.new("RGB", (width, height), (18, 20, 26))
    dr = ImageDraw.Draw(im, "RGBA")
    # Subtle modern architectural grid lines + top glow (no empty circle)
    for x in range(0, width, 90):
        dr.line([(x, 0), (x, height)], fill=(255, 255, 255, 10), width=1)
    for y in range(0, height, 90):
        dr.line([(0, y), (width, y)], fill=(255, 255, 255, 10), width=1)
    for r in range(520, 60, -40):
        alpha = max(4, int(22 * (1.0 - r / 560.0)))
        dr.ellipse([width // 2 - r, int(height * 0.34) - r, width // 2 + r, int(height * 0.34) + r],
                   fill=ac + (alpha,))
    bio = io.BytesIO()
    im.save(bio, "JPEG", quality=90)
    return bio.getvalue()


_PHOTO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}
_META_IMG = [
    re.compile(r'<meta[^>]+(?:property|name)\s*=\s*["\'](?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["\']'
               r'[^>]*?content\s*=\s*["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*?(?:property|name)\s*=\s*["\']'
               r'(?:og:image|twitter:image)["\']', re.I),
    re.compile(r'"image"\s*:\s*\{?[^\}]*?"url"\s*:\s*"(https?://[^"]+)"', re.I),
    re.compile(r'<img[^>]+(?:src|data-src)\s*=\s*["\'](https?://[^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']', re.I),
]
_BAD_IMG = re.compile(
    r"logo|favicon|placeholder|sprite|avatar|default[-_]?(?:image|thumb|share)|1x1|watermark|"
    r"gettyimages|shutterstock|alamy|istockphoto|depositphotos|123rf|dreamstime|"
    r"small[-_]?img|crest|badge|150x150|100x100", re.I
)


def download_photo(url, referer=None):
    """Photo Quality Gate: accept real news/TG photos >=480x320, reject watermarks/icons/tiny thumbs."""
    try:
        if not url or _BAD_IMG.search(url):
            return None
        headers = dict(_PHOTO_HEADERS)
        if referer:
            headers["Referer"] = referer
        r = requests.get(url, headers=headers, timeout=14, allow_redirects=True)
        if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image/") or len(r.content) < 16000:
            return None
        if PIL_OK:
            im = Image.open(io.BytesIO(r.content))
            w, h = im.size
            if w < 480 or h < 320:
                return None
            if w / max(1, h) > 2.8 or h / max(1, w) > 2.8:
                return None
            edge_std = ImageStat.Stat(im.convert("L").resize((180, 180)).filter(ImageFilter.FIND_EDGES)).stddev[0]
            if edge_std < 4.5:
                return None
        return r.content
    except Exception as e:
        log.info("photo download failed: %s", short_reason(e, 80))
        return None


def _wiki_real_photo(query):
    try:
        q = re.sub(r"[^A-Za-z0-9\s-]", " ", query or "").strip()
        words = [w for w in q.split() if len(w) >= 3][:4]
        if not words:
            return None
        r = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "generator": "search", "gsrsearch": " ".join(words),
                    "gsrlimit": 3, "prop": "pageimages", "pithumbsize": 1200, "format": "json"},
            headers=_PHOTO_HEADERS, timeout=8,
        )
        if r.status_code != 200:
            return None
        pages = (r.json().get("query") or {}).get("pages") or {}
        for pg in pages.values():
            src_u = (pg.get("thumbnail") or {}).get("source")
            if src_u:
                b = download_photo(src_u)
                if b:
                    return b
    except Exception:
        pass
    return None


def _fit_panel(im, pw, ph, bg=(20, 21, 24)):
    """Fits an image into a panel without chopping off text/documents or faces."""
    w, h = im.size
    panel_ratio = pw / max(1, ph)
    img_ratio = w / max(1, h)
    if max(panel_ratio / img_ratio, img_ratio / panel_ratio) <= 1.55:
        return _cover_crop_resize(im, pw, ph)
    scale = min(pw / max(1, w), ph / max(1, h))
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    resized = im.resize((nw, nh), Image.LANCZOS)
    frame = Image.new("RGB", (pw, ph), bg)
    frame.paste(resized, ((pw - nw) // 2, (ph - nh) // 2))
    return frame


def _compose_photo_collage(photo_bytes_list, width=1080, height=1350):
    """Blends 2-4 real photos cleanly in the upper ~68% zone so faces & documents stay visible above the headline."""
    if not PIL_OK or len(photo_bytes_list) < 2:
        return photo_bytes_list[0] if photo_bytes_list else None
    try:
        ims = [Image.open(io.BytesIO(b)).convert("RGB") for b in photo_bytes_list[:4]]
        W, H = width, height
        top_h = int(H * 0.68)
        gap = 8
        canvas = Image.new("RGB", (W, H), (18, 20, 24))
        n = len(ims)
        if n == 2:
            w1 = (W - gap) // 2
            canvas.paste(_fit_panel(ims[0], w1, top_h), (0, 0))
            canvas.paste(_fit_panel(ims[1], W - w1 - gap, top_h), (w1 + gap, 0))
        elif n == 3:
            h1 = int(top_h * 0.54)
            h2 = top_h - h1 - gap
            w2 = (W - gap) // 2
            canvas.paste(_fit_panel(ims[0], W, h1), (0, 0))
            canvas.paste(_fit_panel(ims[1], w2, h2), (0, h1 + gap))
            canvas.paste(_fit_panel(ims[2], W - w2 - gap, h2), (w2 + gap, h1 + gap))
        else:
            w2, h2 = (W - gap) // 2, (top_h - gap) // 2
            canvas.paste(_fit_panel(ims[0], w2, h2), (0, 0))
            canvas.paste(_fit_panel(ims[1], W - w2 - gap, h2), (w2 + gap, 0))
            canvas.paste(_fit_panel(ims[2], w2, top_h - h2 - gap), (0, h2 + gap))
            canvas.paste(_fit_panel(ims[3], W - w2 - gap, top_h - h2 - gap), (w2 + gap, h2 + gap))
        canvas = ImageEnhance.Contrast(canvas).enhance(1.05)
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=92)
        return out.getvalue()
    except Exception as e:
        log.info("collage build failed: %s", short_reason(e, 70))
        return photo_bytes_list[0]


def find_real_photo(urls, direct_image=None, max_pages=5, topic_hint=None, extra_images=None):
    cand_imgs = []
    if direct_image:
        cand_imgs.append(direct_image)
    for u in (extra_images or []):
        if u and u not in cand_imgs:
            cand_imgs.append(u)
    if cand_imgs:
        downloaded = []
        for u in cand_imgs[:4]:
            b = download_photo(u)
            if b:
                downloaded.append(b)
        if len(downloaded) >= 2:
            return _compose_photo_collage(downloaded)
        if downloaded:
            return downloaded[0]
    seen, pages = set(), 0
    for u in urls or []:
        if not u or u in seen or pages >= max_pages:
            continue
        seen.add(u)
        pages += 1
        try:
            r = requests.get(u, headers=_PHOTO_HEADERS, timeout=10, allow_redirects=True)
            if r.status_code != 200 or "html" not in r.headers.get("Content-Type", ""):
                continue
            html = r.text[:250000]
            for rx in _META_IMG:
                for m in rx.finditer(html):
                    img = urljoin(r.url, html_lib.unescape(m.group(1)).strip())
                    if _BAD_IMG.search(img):
                        continue
                    b = download_photo(img, referer=r.url)
                    if b:
                        return b
                    break
        except Exception as e:
            log.info("article page fetch failed: %s", short_reason(e, 80))
    if topic_hint:
        b = _wiki_real_photo(topic_hint)
        if b:
            return b
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
    return ImageFont.load_default()


def _is_eth(ch):
    o = ord(ch)
    return 0x1200 <= o <= 0x137F or 0x1380 <= o <= 0x139F or 0x2D80 <= o <= 0x2DDF or 0xAB00 <= o <= 0xAB2F


def _split_runs(text):
    runs, buf, cur = [], "", None
    for ch in text:
        eth = _is_eth(ch)
        if cur is None or eth == cur:
            buf += ch
            cur = eth
        else:
            runs.append((buf, cur))
            buf, cur = ch, eth
    if buf:
        runs.append((buf, cur))
    return runs


def _measure(text, f_eth, f_lat):
    return sum((f_eth if eth else f_lat).getlength(s) for s, eth in _split_runs(text))


def _draw_mixed(draw, x, baseline, text, f_eth, f_lat, fill):
    cx = float(x)
    for s, eth in _split_runs(text):
        f = f_eth if eth else f_lat
        draw.text((cx, baseline), s, font=f, fill=fill, anchor="ls")
        cx += f.getlength(s)


def _wrap(text, f_eth, f_lat, max_w):
    lines, cur = [], ""
    for w in text.split():
        cand = (cur + " " + w).strip()
        if not cur or _measure(cand, f_eth, f_lat) <= max_w:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _cover_crop_resize(img, width, height):
    w, h = img.size
    target = width / height
    src_r = w / max(1, h)
    if src_r > target:
        new_w = int(h * target)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    elif src_r < target:
        new_h = int(w / target)
        top = max(0, (h - new_h) // 4)
        img = img.crop((0, top, w, top + new_h))
    return img.resize((width, height), Image.LANCZOS)


def _apply_seamless_gradient(img, start_ratio=0.56, tint=(12, 8, 12), max_alpha=245):
    W, H = img.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    g0 = int(H * start_ratio)
    span = max(1, H - g0)
    for y in range(g0, H):
        t = (y - g0) / span
        s = t * t * (3.0 - 2.0 * t)
        od.line([(0, y), (W, y)], fill=tint + (int(max_alpha * s),))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _draw_social_footer(draw, right_x, center_y, handle):
    f_h = _latin_font(23)
    hw = int(f_h.getlength(handle))
    x_text = right_x - hw
    r = 14
    fx = x_text - 14 - r
    draw.ellipse([fx - r, center_y - r, fx + r, center_y + r], fill=(24, 119, 242, 245))
    f_fb = _latin_font(21)
    draw.text((fx - 4, center_y + 8), "f", font=f_fb, fill=(255, 255, 255, 255), anchor="ls")
    tx = fx - 2 * r - 8
    draw.ellipse([tx - r, center_y - r, tx + r, center_y + r], fill=(38, 165, 228, 245))
    draw.polygon([(tx - 7, center_y), (tx + 7, center_y - 6), (tx + 3, center_y + 7), (tx - 1, center_y + 2)],
                 fill=(255, 255, 255, 255))
    draw.text((x_text, center_y + 8), handle, font=f_h, fill=(238, 240, 246, 235), anchor="ls")


CARD_THEMES = {
    "media": {"main": (162, 20, 44), "deep": (16, 8, 14), "gold": (255, 212, 72), "accent": (255, 212, 72)},
    "sport": {"main": (4, 128, 90), "deep": (6, 20, 16), "gold": (255, 212, 72), "accent": (64, 232, 162)},
    "coffee": {"main": (118, 66, 24), "deep": (18, 11, 6), "gold": (255, 198, 68), "accent": (255, 198, 68)},
    "bego": {"main": (14, 77, 100), "deep": (8, 26, 36), "gold": (244, 208, 63), "accent": (244, 208, 63)},
}


def _draw_bold_shadow(draw, x, baseline, text, f_eth, f_lat, fill, shadow=True):
    if shadow:
        for dx, dy, a in ((0, 9, 75), (3, 9, 75), (6, 9, 65), (0, 6, 100), (3, 6, 100), (5, 6, 95), (2, 4, 130),
                          (4, 4, 130), (-2, 3, 140), (0, 3, 165), (3, 3, 165), (6, 3, 130)):
            _draw_mixed(draw, x + dx, baseline + dy, text, f_eth, f_lat, (0, 0, 0, a))
        for dx in (-3, -2, -1, 0, 1, 2, 3):
            for dy in (-3, -2, -1, 0, 1, 2, 3):
                if dx * dx + dy * dy in (4, 5, 8, 9, 10, 13, 18):
                    _draw_mixed(draw, x + dx, baseline + dy, text, f_eth, f_lat, (6, 6, 10, 240))
    for dx, dy in ((0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (0, 2)):
        _draw_mixed(draw, x + dx, baseline + dy, text, f_eth, f_lat, fill)


def _draw_sekela_s_emblem(draw, x, y, h, accent):
    """Draws a crisp rounded Sekela S badge with bold geometric S and clean border."""
    s = h
    r = max(12, int(s * 0.24))
    draw.rounded_rectangle([x, y, x + s, y + s], radius=r, fill=accent + (255,), outline=(255, 255, 255, 210), width=2)
    f_s = _latin_font(int(s * 0.68))
    bbox = f_s.getbbox("S")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = x + (s - tw) // 2 - bbox[0]
    ty = y + (s - th) // 2 - bbox[1]
    draw.text((tx, ty), "S", font=f_s, fill=(255, 255, 255, 255))
    return s


def compose_card(bg_bytes, headline, badge=None, ai=True, width=1080, height=1350, brand="media", segment=None):
    """1080x1350 (4:5 portrait) v6 card: Matte Charcoal #141518, upper ~75% photo, compact cosine fade, 2-line bold hook."""
    if not PIL_OK or not bg_bytes or not headline:
        return None
    eth_path = find_ethiopic_font()
    if not eth_path:
        return None
    CHARCOAL = (20, 21, 24)
    accents = {"media": (200, 16, 46), "sport": (0, 135, 81), "coffee": (200, 125, 50), "bego": (214, 175, 62)}
    sub_labels = {"media": "MEDIA", "sport": "SPORT", "coffee": "COFFEE", "bego": "BEGO"}
    default_badges = {"media": "ሰከላ ዜና", "sport": "ሰከላ ስፖርት", "coffee": "ሰከላ ኮፊ ታይም", "bego": "ሰከላ በጎ"}
    key = segment if segment in ("coffee", "bego") else brand
    accent = accents.get(key, accents["media"])
    sub_en = sub_labels.get(key, "MEDIA")
    badge_txt = badge or default_badges.get(key, "ሰከላ ዜና")
    brand_am = BRANDS.get(brand, BRANDS["media"]).get("name_am", "ሰከላ ሚዲያ")
    handle = BRANDS.get(brand, BRANDS["media"]).get("tg_handle", "@sekelamediaET")

    try:
        W, H = width, height
        canvas = Image.new("RGBA", (W, H), CHARCOAL + (255,))
        margin, max_w = 54, W - 124

        # Fit headline into max 2 punchy, large lines that rise cleanly from right above the footer
        fs, lines = 92, []
        for fs in range(98, 52, -4):
            f_eth, f_lat = ImageFont.truetype(eth_path, fs), _latin_font(fs)
            lines = _wrap(headline, f_eth, f_lat, max_w - 28)
            if len(lines) <= 2 and all(_measure(ln, f_eth, f_lat) <= max_w - 28 for ln in lines):
                break
        if len(lines) > 2:
            lines = lines[:2]
        line_h = int(fs * 1.24)
        total_text_h = len(lines) * line_h
        hy = H - 132 - total_text_h
        pill_y = hy - 64

        # Full-bleed studio-graded photo (vibrant color, crisp sharpness, balanced contrast)
        src_im = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
        photo = _cover_crop_resize(src_im, W, H)
        photo = ImageEnhance.Color(photo).enhance(1.12)
        photo = ImageEnhance.Contrast(photo).enhance(1.08)
        photo = ImageEnhance.Sharpness(photo).enhance(1.10).convert("RGBA")
        canvas.paste(photo, (0, 0))

        scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sp = scrim.load()
        fade_start = max(int(H * 0.40), pill_y - 140)
        fade_end = min(H, hy + total_text_h + 28)
        max_scrim = 250  # Rich deep charcoal scrim (~98%) so text pops on bright photos & documents
        for y in range(fade_start, H):
            if y < fade_end:
                t = (y - fade_start) / max(1, (fade_end - fade_start))
                a = int(max_scrim * (0.5 * (1.0 - math.cos(math.pi * t))))
            else:
                a = max_scrim
            for x in range(W):
                sp[x, y] = (CHARCOAL[0], CHARCOAL[1], CHARCOAL[2], a)
        canvas = Image.alpha_composite(canvas, scrim)

        # Gentle top feathered shadow for floating SEKELA logo
        top_sh = Image.new("RGBA", (W, 180), (0, 0, 0, 0))
        tp = top_sh.load()
        for y in range(180):
            a = int(160 * ((1.0 - y / 180.0) ** 1.5))
            for x in range(W):
                tp[x, y] = (CHARCOAL[0], CHARCOAL[1], CHARCOAL[2], a)
        canvas.paste(top_sh, (0, 0), top_sh)
        draw = ImageDraw.Draw(canvas, "RGBA")

        # Top 8px brand stripe
        draw.rectangle([0, 0, W, 8], fill=accent + (255,))

        # Clean brand-only top-left header ("ሰከላ ሚዲያ" / "ሰከላ ስፖርት") with crisp dark pill backing
        f_top_eth = ImageFont.truetype(eth_path, 26)
        f_top_lat = _latin_font(24)
        top_tw = int(_measure(brand_am, f_top_eth, f_top_lat))
        draw.rounded_rectangle([50, 34, 50 + top_tw + 32, 82], radius=12, fill=(16, 18, 22, 185))
        draw.rounded_rectangle([50, 34, 58, 82], radius=4, fill=accent + (255,))
        _draw_mixed(draw, 70, 68, brand_am, f_top_eth, f_top_lat, (255, 255, 255, 255))

        # Category badge
        b_eth, b_lat = ImageFont.truetype(eth_path, 28), _latin_font(26)
        bw = int(_measure(badge_txt, b_eth, b_lat)) + 40
        pill_h = 46
        draw.rounded_rectangle([margin, pill_y, margin + bw, pill_y + pill_h], radius=10, fill=accent + (255,))
        _draw_mixed(draw, margin + 20, pill_y + 33, badge_txt, b_eth, b_lat, (255, 255, 255, 255))

        # Vertical brand bar + Extra-bold Hook
        bar_h = max(40, total_text_h - 14)
        draw.rounded_rectangle([margin, hy + 10, margin + 12, hy + 10 + bar_h], radius=6, fill=accent + (255,))
        f_eth, f_lat = ImageFont.truetype(eth_path, fs), _latin_font(fs)
        for i, ln in enumerate(lines):
            base = hy + i * line_h + int(fs * 0.92)
            _draw_bold_shadow(draw, margin + 36, base, ln, f_eth, f_lat, (255, 255, 255, 255))

        # Clean bottom social footer (no divider line)
        f_foot_lat = _latin_font(22)
        f_foot_eth = ImageFont.truetype(eth_path, 24)
        draw.ellipse([margin, H - 54, margin + 28, H - 26], fill=(42, 171, 238, 255))
        draw.polygon([(margin + 6, H - 40), (margin + 21, H - 47), (margin + 17, H - 33), (margin + 12, H - 38)],
                     fill=(255, 255, 255, 255))
        draw.ellipse([margin + 36, H - 54, margin + 64, H - 26], fill=(24, 119, 242, 255))
        draw.text((margin + 46, H - 51), "f", font=_latin_font(21), fill=(255, 255, 255, 255))
        draw.text((margin + 76, H - 51), handle, font=f_foot_lat, fill=(195, 202, 215, 255))

        rw = int(_measure(brand_am, f_foot_eth, f_foot_lat))
        _draw_mixed(draw, W - margin - rw, H - 31, brand_am, f_foot_eth, f_foot_lat, (195, 202, 215, 255))

        if AI_LABEL and ai:
            a_font = _latin_font(18)
            label = "AI"
            lw = int(a_font.getlength(label)) + 22
            draw.rounded_rectangle([W - margin - lw, 36, W - margin, 36 + 28], radius=14, fill=(0, 0, 0, 110))
            draw.text((W - margin - lw + 11, 41), label, font=a_font, fill=(255, 255, 255, 190))

        out = io.BytesIO()
        canvas.convert("RGB").save(out, "JPEG", quality=92, optimize=True)
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


def _fb_clean(s):
    s = str(s)
    for b in BRANDS.values():
        if b["fb_token"]:
            s = s.replace(b["fb_token"], "***")
    return s


def _fb_request(method, path, params=None, data=None, files=None, timeout=90, brand="media"):
    p = path.strip("/").lower()
    if "comment" in p:
        raise FBError("blocked by no-commenting policy")
    if method == "POST" and p.split("/")[-1] not in ("photos", "feed", "videos"):
        raise FBError(f"blocked: POST not allowed on {p}")
    token = BRANDS[brand]["fb_token"]
    url = f"{FB_GRAPH}/{path.strip('/')}"
    try:
        if method == "POST":
            body = dict(data or {})
            body["access_token"] = token
            r = requests.post(url, data=body, files=files, timeout=timeout)
        else:
            q = dict(params or {})
            q["access_token"] = token
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


def fb_publish(post, image_bytes=None, brand="media"):
    pid = BRANDS[brand]["fb_id"]
    if image_bytes:
        js = _fb_request("POST", f"{pid}/photos", data={"caption": post, "published": "true"},
                         files={"source": ("sekela-card.jpg", image_bytes, "image/jpeg")}, timeout=120, brand=brand)
    else:
        js = _fb_request("POST", f"{pid}/feed", data={"message": post}, brand=brand)
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


FB_INSIGHT_CANDIDATES = [
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


def fb_analytics_text(brand="media", metrics=None):
    metrics = metrics if metrics is not None else {}
    pid, page_name = BRANDS[brand]["fb_id"], BRANDS[brand]["fb_name"]
    if not fb_enabled(brand):
        metrics.update(_ok=False, _err="no token")
        return "⚪ ፌስቡክ አልተዋቀረም — በ Render ላይ FB_PAGE_ACCESS_TOKEN ጨምር።"
    lines = [f"📊 {page_name} — Facebook Analytics", "━━━━━━━━━━━━"]
    try:
        me = _fb_request("GET", "me", params={"fields": "name,id"}, timeout=40, brand=brand)
        metrics["_ok"] = True
        lines.append(f"📄 ገጽ: {me.get('name', page_name)} (id {me.get('id', pid)})")
        if me.get("id") and str(me["id"]) != str(pid):
            lines.append("⚠️ ቶክኑ የሌላ ገጽ ነው — FB_PAGE_ID እና ቶክኑ አይዛመዱም።")
    except FBError as e:
        metrics.update(_ok=False, _err=short_reason(e, 90))
        return "\n".join(lines + [f"❌ የገጹን መረጃ ማምጣት አልተቻለም: {short_reason(e)}", fb_error_hint(e)]).strip()

    now = int(time.time())
    got = []
    for metric, label, mode, key in FB_INSIGHT_CANDIDATES:
        try:
            js = _fb_request("GET", f"{pid}/insights",
                             params={"metric": metric, "period": "day", "since": now - 7 * 86400, "until": now},
                             timeout=40, brand=brand)
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
            posts = _fb_request("GET", f"{pid}/posts", params={"fields": fields, "limit": 5},
                                timeout=40, brand=brand).get("data", [])
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
# Post package: guaranteed 1080x1350 card + copy -> preview -> approval -> TG + FB
# ----------------------------------------------------------------------------
def _resolve_card_bg(pkg_image, brand, segment, photo_urls=None, story_image=None, sources=None, custom_photo=None, extra_images=None):
    """Priority: 1) custom_photo / real source photo -> 2) Gemini Imagen -> 3) Pollinations 20s -> 4) Branded backdrop."""
    if custom_photo:
        return custom_photo, True, False
    real = find_real_photo(list(photo_urls or []) + [u for _, u in (sources or [])[:5]], story_image, topic_hint=pkg_image, extra_images=extra_images)
    if real:
        return real, True, False
    ai_bg, is_ai = fetch_image(build_image_prompt(pkg_image, brand, segment=segment), brand=brand, segment=segment)
    if ai_bg:
        return ai_bg, False, is_ai
    return _fallback_backdrop(brand=brand, segment=segment), False, False


def build_draft(topic, facts=None, sources=None, allow_fallback=True, brand=None, photo_urls=None, story_image=None,
                segment=None, custom_photo=None, extra_images=None):
    if segment in ("coffee", "bego"):
        brand = "media"
    else:
        brand = brand or detect_brand(topic)
    sources = list(sources or [])
    if not facts and segment != "bego":
        facts, sources = research_for(topic)
    unverified = not facts
    if unverified and not allow_fallback:
        return None
    rich = len((facts or "").split()) >= 60
    best, best_score, extra, last_text = None, None, "", ""
    if segment == "bego":
        brief_tpl = BEGO_BRIEF
    elif segment == "coffee":
        brief_tpl = COFFEE_BRIEF
    else:
        brief_tpl = NEWS_BRIEF
    src_hint = resolve_source("", facts or "", sources, False) if (segment == "bego" and sources) else "ሰከላ በጎ (ከተላከው መረጃ)"
    for attempt in range(2):
        brief = (brief_tpl.replace("{topic}", topic)
                 .replace("{brand_note}", BRANDS[brand]["brief_note"])
                 .replace("{facts}", facts or topic or "(none)")
                 .replace("{source_hint}", src_hint)
                 .replace("{mode_note}", UNVERIFIED_NOTE if (unverified and segment != "bego") else "")) + learning_block(brand) + extra
        text, engine = llm_write(brief, system=NEWSROOM_SYSTEM, max_tokens=3000,
                                 temperature=0.55 if segment == "coffee" else (0.35 if segment == "bego" else (0.5 if attempt == 0 else 0.3)),
                                 allow_gemini=True, models=NEWS_WRITER_MODELS)
        if not text:
            continue
        last_text = text
        pkg = parse_package(text)
        built = assemble_news_post(pkg, facts or "", sources, unverified and segment != "bego", brand, segment=segment)
        if not built and allow_fallback:
            paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
            if paras:
                pkg = {
                    "headline": clean_headline(paras[0], max_words=7) or clean_headline(topic, max_words=7) or "ሰበር ዜና",
                    "lead": paras[0],
                    "context": " ".join(paras[1:3]) if len(paras) > 1 else paras[0],
                    "official": "NONE",
                    "source": "",
                    "image": topic,
                    "tags": "",
                }
                built = assemble_news_post(pkg, facts or "", sources, unverified and segment != "bego", brand, segment=segment)
        if not built:
            continue
        longs = long_sentences(pkg)
        thin = (rich and segment not in ("coffee", "bego") and too_thin(pkg))
        score = len(longs) * 2 + (3 if thin else 0)
        if best is None or score < best_score:
            best, best_score = (pkg, built), score
        if score == 0:
            break
        extra = "\n\nREWRITE:"
        if longs:
            extra += " some sentences were longer than 26 words - split them (mostly 12-22 words each)."
        if thin:
            extra += " the CONTEXT block was too thin - write 4-6 well-connected sentences (80-130 words) with concrete facts."
        extra += " Keep all facts unchanged and keep the grammar perfect."
    if not best:
        log.error("news writer produced nothing (both OpenRouter and Gemini failed)")
        return None
    pkg, (post, headline) = best
    fixed = proofread_pkg(pkg) if (segment != "bego" and best_score and best_score > 0) else pkg
    if fixed is not pkg:
        rebuilt = assemble_news_post(fixed, facts or "", sources, unverified, brand, segment=segment)
        if rebuilt:
            pkg, (post, headline) = fixed, rebuilt
    bg, is_real, is_ai = _resolve_card_bg(pkg.get("image"), brand, segment, photo_urls, story_image, sources, custom_photo, extra_images=extra_images)
    badge = COFFEE_BADGE if segment == "coffee" else (BEGO_BADGE if segment == "bego" else BRANDS[brand]["badge"])
    card = compose_card(bg, headline, badge, ai=is_ai, brand=brand, segment=segment) if bg else None
    bot_stats["drafts"] += 1
    mark_seen(headline + " " + _para(pkg.get("lead")))
    return {"post": post, "headline": headline, "image": card or bg, "card": bool(card), "sources": sources,
            "unverified": unverified and segment != "bego", "brand": brand, "segment": segment, "pkg": pkg,
            "facts": facts or "", "bg": bg, "real": is_real, "is_ai": is_ai,
            "topic": segment if segment in ("coffee", "bego") else None}


def draft_keyboard(did, brand, swap=True):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ አጽድቅና ፖስት አድርግ", callback_data=f"ok:{did}"),
        types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"no:{did}"),
    )
    if swap:
        kb.add(
            types.InlineKeyboardButton(f"🎯 {brand_label(brand)}", callback_data="noop"),
            types.InlineKeyboardButton(f"🔄 ወደ {brand_label(other_brand(brand))} ቀይር", callback_data=f"sw:{did}"),
        )
    return kb


def present_draft(chat_id, label, d, header, start=None):
    brand = d["brand"]
    segment = d.get("segment")
    post, headline, image = d["post"], d["headline"], d.get("image")
    caption_fits = len(post) <= CAPTION_LIMIT
    file_id, photo_msg = None, None
    if image:
        try:
            sent = bot.send_photo(chat_id, _as_photo(image), caption=post if caption_fits else f"🔴 {headline}")
            file_id, photo_msg = sent.photo[-1].file_id, sent.message_id
        except Exception as e:
            log.error("sending preview photo failed: %s", e)

    did = uuid.uuid4().hex[:10]
    with state_lock:
        drafts[did] = {"post": post, "file_id": file_id, "image": image, "headline": headline, "card": d.get("card"),
                       "unverified": d.get("unverified", False), "confirmed": False, "brand": brand,
                       "segment": segment, "pkg": d.get("pkg") or {}, "facts": d.get("facts", ""),
                       "sources": d.get("sources") or [], "bg": d.get("bg"), "real": d.get("real", False),
                       "is_ai": d.get("is_ai", False), "photo_msg": photo_msg, "details_msg": None, "pid": None,
                       "tg": False, "fb": False, "busy": False, "ts": time.time(), "topic": draft_topic(d),
                       "fixed": d.get("fixed", False) or segment in ("coffee", "bego"), "learned": False,
                       "fb_post_id": None, "outlet": ((d.get("sources") or [("", "")])[0][0] if d.get("sources") else "")}
    prune(drafts, 12)

    if segment == "coffee":
        dest = f"{brand_label(brand)} (☕ ኮፊ ታይም)"
    elif segment == "bego":
        dest = f"{brand_label(brand)} (🤍 ሰከላ በጎ)"
    else:
        dest = brand_label(brand)
    details = [header + f"\n🎯 መድረሻ: {dest}"]
    if not (file_id and caption_fits):
        details.append("━━━━━━━━━━━━\n" + post)
    if d.get("unverified"):
        details.append("⚠️ ቀጥታ የዜና ፍለጋ አልተሳካም — ይህ ረቂቅ በታወቁ እውነታዎች ላይ የተመሠረተ ነው። ከመለጠፍህ በፊት አረጋግጥ።")
    if d.get("real"):
        details.append("📷 ምስሉ እውነተኛ ፎቶ ነው።")
    if image and not d.get("card"):
        details.append("⚠️ ርዕሱን በምስሉ ላይ ማተም አልተቻለም (Ethiopic font የለም) — ርዕሱ በፖስቱ ጽሑፍ ውስጥ አለ።")
    src = format_sources(d.get("sources"), limit=4)
    if src:
        details.append(src)
    targets = [str(BRANDS[brand]["tg"])] + ([BRANDS[brand]["fb_name"] + " (Facebook)"] if fb_enabled(brand) else [])
    details.append("━━━━━━━━━━━━\n🔒 ✅ ከነካህ ብቻ ይለጠፋል → " + " + ".join(targets) +
                   "\nበፌስቡክ ኮመንቶች ላይ ቦቱ ፈጽሞ አይመልስም።")
    if start:
        details.append(took(start))
    mid = deliver(chat_id, None, "\n\n".join(details),
                  draft_keyboard(did, brand, swap=not d.get("fixed") and segment not in ("coffee", "bego")))
    with state_lock:
        if did in drafts:
            drafts[did]["details_msg"] = mid
    return did


def rebrand_draft(chat_id, msg_id, did):
    with state_lock:
        d = drafts.get(did)
        if d is None or d["busy"]:
            d = None
        else:
            drafts.pop(did, None)
    if d is None:
        bot.send_message(chat_id, "⚠️ ይህ ረቂቅ ጊዜው አልፏል ወይም አሁን እየተሰራ ነው።")
        return
    new = other_brand(d["brand"])
    built = assemble_news_post(d["pkg"], d["facts"], d["sources"], d["unverified"], new)
    if not built:
        bot.send_message(chat_id, "ሄኖክ፣ ረቂቁን ወደ ሌላ ብራንድ መቀየር አልተቻለም 😕 እንደገና ጠይቀኝ።")
        return
    post, headline = built
    card = compose_card(d["bg"], headline, BRANDS[new]["badge"], ai=d.get("is_ai", False), brand=new) if d.get("bg") else None
    nd = {"post": post, "headline": headline, "image": card or d.get("bg"), "card": bool(card),
          "sources": d["sources"], "unverified": d["unverified"], "brand": new, "pkg": d["pkg"],
          "facts": d["facts"], "bg": d.get("bg"), "real": d.get("real", False), "is_ai": d.get("is_ai", False)}
    for mid in {d.get("photo_msg"), d.get("details_msg"), msg_id}:
        drop_message(chat_id, mid)
    present_draft(chat_id, "", nd, f"🔄 ወደ {brand_label(new)} ተቀይሯል")


def run_post(chat_id, label, topic, context_text=None, context_sources=None, brand=None, photo_urls=None,
             story_image=None, segment=None, custom_photo=None):
    start = time.time()
    ack_id = send_progress(
        chat_id, "⏳ ሄኖክ፣ ጥናቱን አድርጌ 1080x1350 ካርዱን እና ፖስቱን እያዘጋጀሁ ነው (ከ15-35 ሰከንድ)...")
    try:
        d = build_draft(topic, facts=context_text, sources=context_sources, allow_fallback=True, brand=brand,
                        photo_urls=photo_urls, story_image=story_image, segment=segment, custom_photo=custom_photo)
        if not d:
            deliver(chat_id, ack_id, "ሄኖክ፣ ዜናውን አሁን ማዘጋጀት አልቻልኩም 😕 ጥቂት ቆይተህ ደግመህ ሞክር።" + _ai_error_hint())
            return
        if segment == "coffee":
            head = "☕ የሰከላ ኮፊ ታይም ረቂቅ ዝግጁ ነው!"
        elif segment == "bego":
            head = "🤍 የሰከላ በጎ ረቂቅ ዝግጁ ነው!"
        else:
            head = f"📝 ረቂቅ ዝግጁ ነው — {label}"
        present_draft(chat_id, label, d, head, start)
        drop_message(chat_id, ack_id)
    except Exception as e:
        log.error("post failed: %s", e)
        deliver(chat_id, ack_id, "ሄኖክ፣ ፖስቱን አሁን ማዘጋጀት አልቻልኩም 😅 ጥቂት ቆይተህ እንደገና ሞክር።")


def run_fast_post(chat_id, brand="media"):
    """One-tap fast card + post: picks the hottest fresh unseen story and builds a 1080x1350 card immediately."""
    start = time.time()
    ack = send_progress(chat_id, f"⏳ ሄኖክ፣ ለ{brand_label(brand)} የቅርብ ጊዜውን ዋና ታሪክ መርጬ 1080x1350 ካርድ እያዘጋጀሁ ነው...")
    status = hunt_once(manual=True, min_importance=4, brand=brand, force_strict=False)
    if status == "sent":
        drop_message(chat_id, ack)
        return
    deliver(chat_id, ack, "ሄኖክ፣ አሁን ፈጣን ካርድ ማዘጋጀት አልተቻለም 😕 እባክህ የራስህን ርዕስ ጻፍ ወይም ቆይተህ ሞክር።" + _ai_error_hint() + "\n\n" + took(start))


def run_coffee_auto(chat_id, custom_topic=None):
    if custom_topic:
        run_post(chat_id, "☕ ሰከላ ኮፊ ታይም", custom_topic, brand="media", segment="coffee")
        return
    start = time.time()
    ack_id = send_progress(chat_id, "☕ ሄኖክ፣ አስገራሚ ቫይራል ታሪክ ፈልጌ የሰከላ ኮፊ ታይም 1080x1350 ካርድ እያዘጋጀሁ ነው...")
    try:
        focus = ("Surprising, funny, educational, or mind-blowing weird-but-true viral human-interest facts and stories "
                 "from ANY field (tech, science, sport, Ethiopia, or world) like 60K+ liked viral cards")
        trends = fetch_google_trends(max_age_hours=24)
        stories, scan_sources = scan_stories(focus, hours=24, n=6, feeds=feed_items_text(trends))
        fresh = [s for s in stories if not is_seen(f"{s['title']} {s['summary']}")] or stories
        if fresh:
            s = fresh[0]
            topic = f"{s['title']}. {s['summary']}"
            photo_urls = [s["link"]] if s.get("link") else []
            facts, sources = research_for(topic)
            if not facts:
                facts = f"{s['title']}. {s['summary']} (Source: {s['outlets']})"
                sources = scan_sources
            d = build_draft(topic, facts=facts, sources=sources, allow_fallback=True, brand="media",
                            photo_urls=photo_urls, story_image=s.get("image"), segment="coffee")
        else:
            d = build_draft(focus, allow_fallback=True, brand="media", segment="coffee")
        if not d:
            deliver(chat_id, ack_id, "ሄኖክ፣ የኮፊ ታይም ረቂቁን አሁን ማዘጋጀት አልቻልኩም 😕 ጥቂት ቆይተህ ደግመህ ሞክር።")
            return
        present_draft(chat_id, "☕ ሰከላ ኮፊ ታይም", d, "☕ ሄኖክ፣ አዲስ የሰከላ ኮፊ ታይም ረቂቅ ዝግጁ ነው!", start)
        drop_message(chat_id, ack_id)
    except Exception as e:
        log.error("coffee auto failed: %s", e)
        deliver(chat_id, ack_id, "ሄኖክ፣ የኮፊ ታይም ረቂቁን ማዘጋጀት አልቻልኩም 😅")


def run_bego_draft(chat_id, raw_text, photo_bytes=None, sources=None):
    """Build a dignified 🤍 ሰከላ በጎ (Sekela Bego) post + 1080x1350 teal-gold card from raw details."""
    run_post(chat_id, "🤍 ሰከላ በጎ", raw_text[:120], context_text=raw_text, context_sources=sources or [],
             brand="media", segment="bego", custom_photo=photo_bytes)


def run_bego_search(chat_id):
    """Search verified urgent Ethiopian missing-person (አፋልጉኝ) or medical/charity appeals."""
    start = time.time()
    ack = send_progress(chat_id, "🔍 ሄኖክ፣ ከታማኝ ማህበራዊ ገጾች አስቸኳይ የአፋልጉኝ / የእርዳታ ጥሪዎችን እየፈለግኩ ነው...")
    try:
        focus = ("Verified urgent Ethiopian humanitarian appeals, missing person notices (አፋልጉኝ), or verified "
                 "medical/charity support campaigns from Tikvah Ethiopia, EBC, Fana, Addis Standard, or recognized NGOs "
                 "in the last 48 hours with verified contact details")
        stories, scan_sources = scan_stories(focus, hours=24, n=5)
        fresh = [s for s in stories if not is_seen(f"{s['title']} {s['summary']}")] or stories
        if not fresh:
            deliver(chat_id, ack, "ሄኖክ፣ አሁን የተረጋገጠ አዲስ የእርዳታ/አፋልጉኝ ጥሪ አልተገኘም። መረጃውን በጽሑፍ፣ በፎቶ ወይም በድምፅ ላክልኝና ካርዱን ላዘጋጅልህ 🤍")
            return
        s = fresh[0]
        topic = f"{s['title']}. {s['summary']}"
        facts, sources = research_for(topic)
        if not facts:
            facts = f"{s['title']}. {s['summary']} (Source: {s['outlets']})"
            sources = scan_sources
        d = build_draft(topic, facts=facts, sources=sources, allow_fallback=True, brand="media",
                        photo_urls=[s["link"]] if s.get("link") else None, story_image=s.get("image"), segment="bego")
        if not d:
            deliver(chat_id, ack, "ሄኖክ፣ የሰከላ በጎ ረቂቁን አሁን ማዘጋጀት አልተቻለም 😕 መረጃውን ራስህ ላክልኝ።")
            return
        present_draft(chat_id, "🤍 ሰከላ በጎ", d, "🤍 ሄኖክ፣ አዲስ የሰከላ በጎ ጥሪ ተዘጋጅቷል (ከማጽደቅህ በፊት ስልክ/መረጃ አረጋግጥ)", start)
        drop_message(chat_id, ack)
    except Exception as e:
        log.error("bego search failed: %s", e)
        deliver(chat_id, ack, "ሄኖክ፣ ፍለጋው አሁን አልተሳካም 😅")


def publish_telegram(d):
    chan = BRANDS[d["brand"]]["tg"]
    post = d["post"]
    photo = d.get("file_id") or (_as_photo(d["image"]) if d.get("image") else None)
    if photo:
        if len(post) <= CAPTION_LIMIT:
            m = bot.send_photo(chan, photo, caption=post)
            return m.message_id, "photo", chan
        first = bot.send_photo(chan, photo)
        text_id = None
        for chunk in split_text(post, 4000):
            m = bot.send_message(chan, chunk)
            text_id = text_id or m.message_id
        return text_id or first.message_id, "text", chan
    msg_id = None
    for chunk in split_text(post, 4000):
        m = bot.send_message(chan, chunk)
        msg_id = msg_id or m.message_id
    return msg_id, "text", chan


def published_keyboard(pid):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📌 Pin Post", callback_data=f"pin:{pid}"),
           types.InlineKeyboardButton("✏️ Edit Post", callback_data=f"ed:{pid}"))
    return kb


def retry_keyboard(did, pid=None):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🔁 ያልተሳካውን እንደገና ሞክር", callback_data=f"ok:{did}"),
        types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"no:{did}"),
    )
    if pid:
        kb.add(types.InlineKeyboardButton("📌 Pin Post", callback_data=f"pin:{pid}"),
               types.InlineKeyboardButton("✏️ Edit Post", callback_data=f"ed:{pid}"))
    return kb


def approve_and_publish(chat_id, msg_id, did):
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

    brand = d["brand"]
    b = BRANDS[brand]
    tg_err = fb_err = None
    fb_on = fb_enabled(brand)
    try:
        if not d["tg"]:
            try:
                mid, kind, chan = publish_telegram(d)
                d["tg"] = True
                bot_stats["tg_posts"] += 1
                pid = uuid.uuid4().hex[:10]
                with state_lock:
                    published[pid] = {"chan": chan, "msg_id": mid, "kind": kind, "post": d["post"], "brand": brand,
                                      "ts": time.time(), "topic": d.get("topic"), "headline": d.get("headline"),
                                      "outlet": d.get("outlet", "")}
                d["pid"] = pid
                prune(published, 30)
            except Exception as e:
                log.error("telegram publish failed: %s", e)
                tg_err = short_reason(e, 200)
        if fb_on and not d["fb"]:
            try:
                d["fb_post_id"] = fb_publish(d["post"], d.get("image"), brand)
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

    tg_line = f"• Telegram: {b['tg']} " + ("✅" if d["tg"] else f"❌ {tg_err or ''}".rstrip())
    if not fb_on:
        fb_line = f"• Facebook: {b['fb_name']} ⚪ አልተዋቀረም (token)"
    else:
        fb_line = f"• Facebook: {b['fb_name']} " + ("✅" if d["fb"] else f"❌ {fb_err or ''}".rstrip())

    if (d["tg"] or d["fb"]) and not d.get("learned"):
        d["learned"] = True
        learn_event("approve", d)
        learn_track(d, fb_post_id=d.get("fb_post_id"))
    if d["tg"] and (d["fb"] or not fb_on):
        with state_lock:
            drafts.pop(did, None)
        bot.edit_message_text(f"✅ ጸድቋል እና ተለጥፏል! 🚀 ({brand_label(brand)})\n{tg_line}\n{fb_line}", chat_id, msg_id,
                              reply_markup=published_keyboard(d["pid"]) if d.get("pid") else None)
    else:
        extra = ""
        if tg_err and "chat not found" in tg_err.lower():
            extra = "\n\n💡 ቻናሉ public መሆኑን እና ቦቱ አድሚን መሆኑን አረጋግጥ — /ai_status ያሳይሃል።"
        bot.edit_message_text(f"⚠️ በከፊል ተሳክቷል ({brand_label(brand)})\n{tg_line}\n{fb_line}{extra}\n\n"
                              "የተሳካው እንደገና አይለጠፍም።", chat_id, msg_id, reply_markup=retry_keyboard(did, d.get("pid")))


def pin_published(chat_id, pid):
    with state_lock:
        p = published.get(pid)
    if not p:
        bot.send_message(chat_id, "ሄኖክ፣ ይህ ፖስት ከማህደረ ትውስታ ተሰርዟል — በቻናሉ ላይ በእጅ Pin አድርግ 🙏")
        return
    try:
        bot.pin_chat_message(p["chan"], p["msg_id"], disable_notification=True)
        bot.send_message(chat_id, f"📌 ፖስቱ በ {p['chan']} ላይ ተሰክቷል (Pin ተደርጓል)።")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Pin ማድረግ አልተሳካም: {short_reason(e, 140)}")


def edit_published(chat_id, pid, new_text):
    with state_lock:
        p = published.get(pid)
    if not p:
        bot.send_message(chat_id, "ሄኖክ፣ ይህ ፖስት ከማህደረ ትውስታ ተሰርዟል — በቻናሉ ላይ በእጅ አርም 🙏")
        return
    if p["kind"] == "photo" and len(new_text) > CAPTION_LIMIT:
        with state_lock:
            awaiting_topic[OWNER_ID] = f"edit:{pid}"
        bot.send_message(chat_id, f"ሄኖክ፣ ጽሑፉ {len(new_text)} ፊደል ነው — ለፎቶ ፖስት ከ{CAPTION_LIMIT} መብለጥ አይችልም። አሳጥረህ ላክልኝ።")
        return
    try:
        if p["kind"] == "photo":
            bot.edit_message_caption(new_text, p["chan"], p["msg_id"])
        else:
            bot.edit_message_text(new_text, p["chan"], p["msg_id"], disable_web_page_preview=True)
        old_text = p.get("post", "")
        p["post"] = new_text
        if new_text.strip() != old_text.strip():
            learn_event("edit", p)
            learn_style_example(new_text)
        bot.send_message(chat_id, f"✅ ፖስቱ በ {p['chan']} ላይ ተስተካክሏል (የ Facebook ፖስት አልተነካም)።",
                         reply_markup=published_keyboard(pid))
    except Exception as e:
        bot.send_message(chat_id, f"❌ ማስተካከል አልተሳካም: {short_reason(e, 160)}")


# ----------------------------------------------------------------------------
# Self-learning memory (agent_learning.json)
# ----------------------------------------------------------------------------
LEARN_FILE = os.environ.get("LEARN_FILE", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "agent_learning.json")
_learn_lock = threading.RLock()
_L = None
_learn_flags = {"dirty": False, "important": False, "perf_ts": 0.0, "loaded_from_disk": False}
LEARN_TOPICS = ["economy", "tech", "addis", "diplomacy", "social", "culture", "coffee", "bego", "conflict", "politics",
                "ethiopian_football", "athletics", "boxing", "womens", "premier", "champions", "transfers", "other"]
TOPIC_AM = {"economy": "ኢኮኖሚ", "tech": "ቴክኖሎጂ", "addis": "አዲስ አበባ/መሠረተ ልማት", "diplomacy": "ዲፕሎማሲ", "social": "ማህበራዊ ሚዲያ",
            "culture": "ባህል", "coffee": "ኮፊ ታይም", "bego": "ሰከላ በጎ", "conflict": "ግጭት", "politics": "ፖለቲካ",
            "ethiopian_football": "የኢትዮጵያ እግር ኳስ", "athletics": "አትሌቲክስ", "boxing": "ቦክስ",
            "womens": "የሴቶች ስፖርት", "premier": "ፕሪሚየር ሊግ", "champions": "ቻምፒየንስ ሊግ", "transfers": "ዝውውር", "other": "ሌላ"}


def _learn_default():
    return {"version": 1, "updated": 0, "rules": [], "events": [], "pending": [], "perf": {}, "boosts": {},
            "favorites": [], "style": [], "winners": [], "backup": {"msg_id": None, "ts": 0}}


def _learn_paths():
    return [LEARN_FILE, os.path.join(tempfile.gettempdir(), "agent_learning.json")]


def learn_load():
    global _L
    with _learn_lock:
        if _L is not None:
            return _L
        _L = _learn_default()
        for path in _learn_paths():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    js = json.load(f)
                if isinstance(js, dict):
                    _L.update(js)
                    _learn_flags["loaded_from_disk"] = True
                    break
            except Exception:
                continue
        return _L


def learn_save():
    with _learn_lock:
        L = learn_load()
        L["updated"] = time.time()
        payload = json.dumps(L, ensure_ascii=False)
        for path in _learn_paths():
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp, path)
                _learn_flags["dirty"] = False
                return True
            except Exception as e:
                log.info("learning file not saved at %s: %s", path, e)
        return False


def learn_touch(important=False):
    _learn_flags["dirty"] = True
    if important:
        _learn_flags["important"] = True


def draft_topic(d):
    if d.get("topic"):
        return d["topic"]
    if d.get("segment") in ("coffee", "bego"):
        return d["segment"]
    text = f"{d.get('headline', '')} {_para((d.get('pkg') or {}).get('lead'))}"
    return classify_sport_topic(text) if d.get("brand") == "sport" else classify_topic(text)


def learn_event(action, d):
    try:
        with _learn_lock:
            L = learn_load()
            L["events"].append({"ts": time.time(), "brand": d.get("brand", "media"), "action": action,
                                "topic": draft_topic(d), "outlet": d.get("outlet", ""),
                                "headline": (d.get("headline") or "")[:90]})
            del L["events"][:-400]
        learn_touch()
    except Exception as e:
        log.info("learn_event failed: %s", e)


def learn_style_example(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) < 40:
        return
    with _learn_lock:
        L = learn_load()
        L["style"].append(text[:260])
        del L["style"][:-4]
    learn_touch()


def learn_weights(brand):
    now = time.time()
    with _learn_lock:
        L = learn_load()
        events, boosts = list(L["events"]), dict(L["boosts"])
        perf = json.loads(json.dumps(L["perf"].get(brand, {})))
    w = {}
    for e in events:
        if e.get("brand") != brand:
            continue
        delta = {"approve": 0.25, "cancel": -0.25, "edit": 0.05, "switch": -0.1}.get(e.get("action"), 0)
        decay = 0.5 ** (((now - e.get("ts", now)) / 86400) / 21)
        w[e["topic"]] = w.get(e["topic"], 1.0) + delta * decay
    avgs = {t: p["score"] / p["n"] for t, p in perf.items() if p.get("n", 0) >= 2}
    if len(avgs) >= 2:
        mean = sum(avgs.values()) / len(avgs)
        if mean > 0:
            for t, a in avgs.items():
                w[t] = w.get(t, 1.0) + 0.4 * (min(a / mean, 2.5) - 1)
    for t, b in boosts.items():
        w[t] = w.get(t, 1.0) + b
    return {t: max(0.3, min(2.5, v)) for t, v in w.items()}


def learn_favorites():
    with _learn_lock:
        return list(learn_load().get("favorites", []))


def learning_block(brand):
    try:
        with _learn_lock:
            L = learn_load()
            rules = [r["rule"] for r in L["rules"]][-12:]
            winners = list(L["winners"])[:4]
            style = list(L["style"])[-3:]
        w = learn_weights(brand)
        lines = []
        if rules:
            lines.append("HENOK'S STANDING EDITORIAL RULES (obey strictly):\n" + "\n".join("- " + r for r in rules))
        good = [TOPIC_AM.get(t, t) for t, v in sorted(w.items(), key=lambda x: -x[1]) if v >= 1.2][:3]
        bad = [TOPIC_AM.get(t, t) for t, v in sorted(w.items(), key=lambda x: x[1]) if v <= 0.8][:3]
        if good:
            lines.append("Topics Henok approves / perform best: " + ", ".join(good) + ".")
        if bad:
            lines.append("Topics he usually cancels (be selective): " + ", ".join(bad) + ".")
        win = [x["headline"] for x in winners if x.get("brand") == brand]
        if win:
            lines.append("Best-performing recent headlines:\n" + "\n".join("- " + h for h in win))
        if style:
            lines.append("Henok's preferred wording from edits:\n" + "\n".join("- " + x for x in style))
        return ("\n\n" + "\n".join(lines))[:1800] if lines else ""
    except Exception:
        return ""


def learning_scan_note(brand):
    with _learn_lock:
        rules = [r["rule"] for r in learn_load()["rules"]][-6:]
    w = learn_weights(brand)
    good = [t for t, v in sorted(w.items(), key=lambda x: -x[1]) if v >= 1.2][:3]
    parts = []
    if rules:
        parts.append("Owner's standing rules: " + "; ".join(rules) + ".")
    if good:
        parts.append("Prefer these topics: " + ", ".join(good) + ".")
    return " ".join(parts)


def choose_desk(brand, wheel_topic, topics):
    w = learn_weights(brand)
    weights = [max(0.15, w.get(t, 1.0)) * (2.2 if t == wheel_topic else 1.0) for t in topics]
    return random.choices(list(topics), weights=weights, k=1)[0]


def learn_summary_facts():
    cutoff = time.time() - 30 * 86400
    with _learn_lock:
        L = learn_load()
        ev = [e for e in L["events"] if e.get("ts", 0) >= cutoff]
        winners = list(L["winners"])[:3]
    if not ev and not winners:
        return ""
    ap, ca = sum(1 for e in ev if e["action"] == "approve"), sum(1 for e in ev if e["action"] == "cancel")
    cnt = {}
    for e in ev:
        if e["action"] == "approve":
            cnt[e["topic"]] = cnt.get(e["topic"], 0) + 1
    top = ", ".join(f"{TOPIC_AM.get(t, t)} ({n})" for t, n in sorted(cnt.items(), key=lambda x: -x[1])[:3])
    out = f"Henok's drafts in the last 30 days: {ap} approved, {ca} cancelled" + (f"; most approved: {top}" if top else "")
    if winners:
        out += "; best-performing: " + "; ".join(f"«{x['headline']}» score {x['score']:.0f}" for x in winners)
    return out


def learn_track(d, fb_post_id=None, yt_id=None):
    with _learn_lock:
        L = learn_load()
        L["pending"].append({"brand": d.get("brand", "media"), "topic": draft_topic(d), "headline": (d.get("headline") or "")[:90],
                             "fb": fb_post_id, "yt": yt_id, "ts": time.time(), "done": False})
        del L["pending"][:-60]
    learn_touch()


def _fb_post_score(brand, post_id):
    js = _fb_request("GET", post_id, params={
        "fields": "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0),shares"},
        timeout=40, brand=brand)
    react = ((js.get("reactions") or {}).get("summary") or {}).get("total_count", 0) or 0
    comm = ((js.get("comments") or {}).get("summary") or {}).get("total_count", 0) or 0
    shares = (js.get("shares") or {}).get("count", 0) or 0
    views = 0
    try:
        ins = _fb_request("GET", f"{post_id}/insights", params={"metric": "post_media_view"}, timeout=40, brand=brand)
        views = (ins.get("data") or [{}])[0].get("values", [{}])[0].get("value", 0) or 0
    except Exception:
        pass
    return react + 3 * comm + 4 * shares + (views / 200.0 if isinstance(views, (int, float)) else 0)


def learn_collect_performance():
    now = time.time()
    with _learn_lock:
        todo = [p for p in learn_load()["pending"] if not p["done"] and 6 * 3600 <= now - p["ts"] <= 14 * 86400]
    for p in todo:
        score = None
        try:
            if p.get("fb") and fb_enabled(p["brand"]):
                score = _fb_post_score(p["brand"], p["fb"])
            elif p.get("yt") and yt_enabled():
                v = _yt_get("videos", {"part": "statistics", "id": p["yt"]}, 0).get("items", [{}])[0].get("statistics", {})
                score = int(v.get("viewCount", 0)) / 50 + 2 * int(v.get("likeCount", 0)) + 4 * int(v.get("commentCount", 0))
        except Exception as e:
            log.info("performance check failed: %s", short_reason(e, 100))
            continue
        if score is None:
            continue
        with _learn_lock:
            L = learn_load()
            t = L["perf"].setdefault(p["brand"], {}).setdefault(p["topic"], {"n": 0, "score": 0.0})
            t["n"] += 1
            t["score"] += float(score)
            p["done"], p["score"] = True, float(score)
            L["winners"].append({"brand": p["brand"], "topic": p["topic"], "headline": p["headline"], "score": float(score)})
            L["winners"] = sorted(L["winners"], key=lambda x: -x["score"])[:10]
        learn_touch()
    _learn_flags["perf_ts"] = now


RULE_TRIGGERS = ["ከዛሬ ጀምሮ", "ከአሁን ጀምሮ", "ከአሁን በኋላ", "ለወደፊት", "ከዚህ በኋላ", "ህግ:", "ሕግ:", "ህግ፦", "rule:", "from now on"]
_MORE = ["አብዛ", "ጨምር", "ተጨማሪ", "ቅድሚያ", "አበዛ", "more", "increase", "prioritize", "prioritise", "focus on", "follow"]
_LESS = ["ቀንስ", "አቁም", "አታድርግ", "ያነሰ", "አትጻፍ", "አትለጥፍ", "less", "stop", "avoid", "no more", "don't", "never"]
RULE_PARSE_PROMPT = (
    "Convert Henok's editorial instruction for his news bot (Amharic or English) into ONE JSON object and nothing else: "
    '{"rule": "<one clear English directive, max 25 words>", "topic": "<one of ' + ", ".join(LEARN_TOPICS) +
    ' or null>", "direction": "<more|less|null>", "team": "<football club in English or null>"}.\nInstruction: '
)


def learn_add_rule(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    rule, topic, direction, team = text, None, None, None
    raw, _ = llm_write(RULE_PARSE_PROMPT + text, max_tokens=300, temperature=0.1, models=NEWS_WRITER_MODELS)
    try:
        m = re.search(r"\{.*\}", raw or "", re.S)
        js = json.loads(m.group(0)) if m else {}
        rule = str(js.get("rule") or text)[:220]
        topic = js.get("topic") if js.get("topic") in LEARN_TOPICS else None
        direction = js.get("direction") if js.get("direction") in ("more", "less") else None
        team = (js.get("team") or None)
    except Exception:
        pass
    low = text.lower()
    if not direction:
        direction = "more" if any(w in low for w in _MORE) else ("less" if any(w in low for w in _LESS) else None)
    if not topic:
        t = classify_topic(text)
        topic = t if t != "other" else (classify_sport_topic(text) if classify_sport_topic(text) != "other" else None)
    if not team:
        known = [t for t in DEFAULT_FAVORITES + [k.title() for k in TEAM_AM] if t.lower() in low]
        team = known[0] if known else None
    rid = uuid.uuid4().hex[:6]
    with _learn_lock:
        L = learn_load()
        L["rules"].append({"id": rid, "text": text[:300], "rule": rule, "topic": topic, "direction": direction,
                           "team": team, "ts": time.time()})
        del L["rules"][:-25]
        if topic and direction:
            L["boosts"][topic] = max(-0.7, min(1.5, L["boosts"].get(topic, 0.0) + (0.5 if direction == "more" else -0.5)))
        if team and direction != "less" and team not in L["favorites"]:
            L["favorites"].append(str(team)[:40])
            del L["favorites"][:-15]
    learn_touch(important=True)
    return {"id": rid, "rule": rule, "topic": topic, "direction": direction, "team": team}


def learn_delete_rule(rid):
    with _learn_lock:
        L = learn_load()
        r = next((x for x in L["rules"] if x["id"] == rid), None)
        if not r:
            return False
        L["rules"].remove(r)
        if r.get("topic") and r.get("direction"):
            L["boosts"][r["topic"]] = L["boosts"].get(r["topic"], 0.0) - (0.5 if r["direction"] == "more" else -0.5)
            if abs(L["boosts"][r["topic"]]) < 0.05:
                L["boosts"].pop(r["topic"], None)
    learn_touch(important=True)
    return True


def learn_rules_message():
    with _learn_lock:
        L = learn_load()
        rules, favs = list(L["rules"]), list(L["favorites"])
        n_ev = len(L["events"])
    lines = ["🧠 የሄኖክ ቋሚ የአርትኦት ህጎች:"]
    lines += [f"{i}. {r['text'][:120]}" for i, r in enumerate(rules, 1)] or ["(ገና ምንም ህግ የለም)"]
    if favs:
        lines.append("\n⚽ የምከታተላቸው ክለቦች: " + ", ".join(favs))
    for brand in ("media", "sport"):
        w = learn_weights(brand)
        if w:
            up = [TOPIC_AM.get(t, t) for t, v in sorted(w.items(), key=lambda x: -x[1]) if v >= 1.15][:3]
            dn = [TOPIC_AM.get(t, t) for t, v in sorted(w.items(), key=lambda x: x[1]) if v <= 0.85][:3]
            if up or dn:
                lines.append(f"{BRANDS[brand]['emoji']} " + (f"የሚወደዱ: {', '.join(up)}  " if up else "") + (f"የሚቀነሱ: {', '.join(dn)}" if dn else ""))
    lines.append(f"\n📚 የተመዘገቡ ውሳኔዎች (✅/❌/✏️): {n_ev}")
    lines.append("\n➕ አዲስ ህግ ለመጨመር በቀጥታ ጻፍ: «ከዛሬ ጀምሮ የኢኮኖሚ ዜና አብዛ»")
    kb = types.InlineKeyboardMarkup(row_width=4)
    btns = [types.InlineKeyboardButton(f"🗑 {i}", callback_data=f"rl:{r['id']}") for i, r in enumerate(rules, 1)]
    for i in range(0, len(btns), 4):
        kb.add(*btns[i:i + 4])
    return "\n".join(lines), (kb if btns else None)


def learn_backup():
    try:
        with _learn_lock:
            L = learn_load()
            L["updated"] = time.time()
            data = json.dumps(L, ensure_ascii=False).encode("utf-8")
            old = (L.get("backup") or {}).get("msg_id")
        f = io.BytesIO(data)
        f.name = "agent_learning.json"
        m = bot.send_document(OWNER_ID, f, caption="🧠 የራስ-ትምህርት backup — አታጥፋው", disable_notification=True)
        try:
            bot.pin_chat_message(OWNER_ID, m.message_id, disable_notification=True)
        except Exception:
            pass
        if old:
            try:
                bot.delete_message(OWNER_ID, old)
            except Exception:
                pass
        with _learn_lock:
            learn_load()["backup"] = {"msg_id": m.message_id, "ts": time.time()}
        _learn_flags["important"] = False
        learn_save()
    except Exception as e:
        log.info("learning backup failed: %s", short_reason(e, 120))


def learn_restore():
    try:
        learn_load()
        local_ts = learn_load().get("updated", 0) if _learn_flags["loaded_from_disk"] else 0
        pm = getattr(bot.get_chat(OWNER_ID), "pinned_message", None)
        doc = getattr(pm, "document", None)
        if not doc or doc.file_name != "agent_learning.json":
            return
        raw = bot.download_file(bot.get_file(doc.file_id).file_path)
        js = json.loads(raw.decode("utf-8"))
        if isinstance(js, dict) and js.get("updated", 0) > local_ts:
            with _learn_lock:
                global _L
                base = _learn_default()
                base.update(js)
                base["backup"] = {"msg_id": pm.message_id, "ts": js.get("updated", 0)}
                _L = base
            learn_save()
            log.info("learning memory restored (%d rules)", len(js.get("rules", [])))
    except Exception as e:
        log.info("learning restore skipped: %s", short_reason(e, 100))


def learn_worker():
    learn_restore()
    last_backup = time.time()
    while True:
        time.sleep(600)
        try:
            if _learn_flags["dirty"]:
                learn_save()
            if time.time() - _learn_flags["perf_ts"] > 3 * 3600:
                learn_collect_performance()
            if _learn_flags["important"] or (_learn_flags["dirty"] and time.time() - last_backup > 12 * 3600):
                learn_backup()
                last_backup = time.time()
        except Exception as e:
            log.warning("learn worker error: %s", e)


def start_learning():
    learn_load()
    threading.Thread(target=learn_worker, daemon=True, name="learning").start()


# ----------------------------------------------------------------------------
# Proactive breaking-news hunter + Google Trends + Morning Briefing Guarantee
# ----------------------------------------------------------------------------
HUNT_HEADERS = {
    "media": "🔔 ሄኖክ፣ አዲስ ሰበር ዜና አዘጋጅቼልሃለሁ! ለሰከላ ሚዲያ ፖስት እናድርገው?",
    "sport": "⚽ ሄኖክ፣ አዲስ የስፖርት ዜና አዘጋጅቼልሃለሁ! ለሰከላ ስፖርት ፖስት እናድርገው?",
    "coffee": "☕ ሄኖክ፣ አዲስ የሰከላ ኮፊ ታይም ቫይራል ታሪክ አዘጋጅቼልሃለሁ! ፖስት እናድርገው?",
}
HUNT_FOCUS = {
    "media": ("Ethiopia & Global: Ethiopian PM/government official pages & ministries, state/independent outlets (Fana, EBC, ENA, "
              "Addis Standard, Borkena, Capital, Addis Fortune, Sheger FM), Ethiopian activists & social media (TikTok, YouTube, X, "
              "public Facebook pages, Tikvah, Gursha, Behaja), economy/birr/jobs, AI/tech updates, Addis Ababa local life, and major global events"),
    "sport": ("Men's top-flight football (Arsenal, Man United, Man City, Liverpool, Chelsea, Real Madrid, Barcelona, Premier League, "
              "Champions League, La Liga), LiveScore live results/standings/fixtures/announcements, Ethiopian sports media & Arsenal/ManUtd "
              "Ethiopian fan communities, plus Ethiopian athletics. EXCLUDE obscure women's club transfers."),
}

PRIORITY_TOPICS = ("economy", "tech", "addis", "diplomacy", "social", "culture", "coffee")
TOPIC_DESK = {
    "economy": "Economy, NBE, Ministries & Business: National Bank (NBE), Finance/Trade/Agriculture/Revenue/Labor ministries, birr, banks, jobs",
    "tech": "ALL AI, Software & Tech Launches: ANY new AI model/tool/app/software/hardware announcement worldwide (OpenAI, Gemini, Claude, DeepSeek, Grok, Qwen, Mistral, Runway, Cursor, Meta, Apple, Google, Microsoft) + Ethio Telecom, Telebirr, MInT, startups",
    "addis": "Addis Ababa, Utilities & Public Alerts: EEU power outage schedules, AAWSA water notices, roads, Immigration/Passport, Ethiopian Airlines, Mayor",
    "diplomacy": "Diplomacy & Global News: US, UK, Europe, Asia, China-US-Ethiopia ties, AU, Foreign Affairs, major world events",
    "social": "TikTok, YouTube, X & Creator Trends: TikTok Creative Awards/leaderboards, YouTubers, Instagram, X/Twitter activists, private media",
    "culture": "Culture, Faith & Community: Ethiopian Orthodox & Islamic community announcements, holidays, music, film, tourism (strictly peaceful)",
    "coffee": "Sekela Coffee Time: surprising, funny, educational, or weird-but-true viral human-interest stories from ANY field",
}
TOPIC_WHEEL = ["economy", "tech", "coffee", "addis", "diplomacy", "social", "economy", "culture", "coffee", "tech", "diplomacy", "addis"]
_wheel = {"media": 0}
_TOPIC_WORDS = {
    "economy": ["economy", "economic", "business", "bank", "inflation", "birr", "exchange rate", "trade", "export",
                "import", "invest", "budget", "tax", "gdp", "imf", "world bank", "price", "coffee", "market", "job", "vacancy",
                "ኢኮኖሚ", "ንግድ", "ባንክ", "ብር", "ምንዛሬ", "ኢንቨስትመንት", "ወጪ ንግድ", "ዋጋ", "ቡና", "በጀት", "ግብር", "ገበያ", "ስራ"],
    "tech": ["ai ", " ai", "artificial intelligence", "tech", "startup", "digital", "internet", "telecom", "safaricom",
             "fintech", "5g", "software", "data center", "meta", "facebook", "instagram", "whatsapp",
             "ቴክኖሎጂ", "ዲጂታል", "ሰው ሰራሽ", "ቴሌ", "ኢንተርኔት", "ስታርታፕ"],
    "addis": ["addis ababa", "corridor", "railway", "light rail", "airport", "bishoftu", "dam", "construction",
              "infrastructure", "housing", "road", "አዲስ አበባ", "ኮሪደር", "መንገድ", "ግድብ", "ግንባታ", "ባቡር", "አየር መንገድ",
              "አየር ማረፊያ", "መኖሪያ ቤት"],
    "diplomacy": ["diplomat", "ambassador", "summit", "african union", "foreign minister", "bilateral", "agreement",
                  "treaty", "talks", "visit", "ዲፕሎማሲ", "አምባሳደር", "ስምምነት", "ጉባኤ", "አፍሪካ ህብረት", "አፍሪካ ሕብረት", "የውጭ ጉዳይ"],
    "social": ["tiktok", "creator", "influencer", "youtube", "social media", "viral", "trend", "award", "ቲክቶክ",
               "ፈጣሪ", "ማህበራዊ ሚዲያ", "ማኅበራዊ ሚዲያ", "ሽልማት"],
    "culture": ["culture", "festival", "music", "film", "heritage", "tourism", "art ", "timkat", "meskel", "ባህል",
                "ፌስቲቫል", "ሙዚቃ", "ፊልም", "ቱሪዝም", "ቅርስ", "ጥምቀት", "መስቀል", "ኪነ ጥበብ"],
    "coffee": ["weird", "surprising", "bizarre", "heartwarming", "record", "unusual", "coffee time", "fun fact", "amazing",
               "አስገራሚ", "አዝናኝ", "ሪከርድ"],
    "conflict": ["war", "conflict", "clash", "fighting", "attack", "killed", "militia", "fano", "tplf", "shelling",
                 "drone strike", "armed", "insurgen", "casualt", "ጦርነት", "ግጭት", "ውጊያ", "ታጣቂ", "ፋኖ", "ጥቃት",
                 "ተገደሉ", "ተኩስ", "ጦር"],
    "politics": ["election", "parliament", "opposition", "party", "politic", "ምርጫ", "ፓርቲ", "ፓርላማ", "ፖለቲካ"],
}


def classify_topic(text):
    low = " " + (text or "").lower() + " "
    scores = {k: sum(1 for w in ws if w in low) for k, ws in _TOPIC_WORDS.items()}
    best = max(scores.values())
    if best == 0:
        return "other"
    order = list(PRIORITY_TOPICS) + ["politics", "conflict"]
    return next(k for k in order if scores.get(k) == best)


SPORT_DESKS = {
    "premier": "English Premier League: results, goals, injuries, managers and transfer-window news with Ethiopian fan interest",
    "champions": "UEFA Champions League and big European leagues (La Liga, Serie A, Bundesliga, Ligue 1): results, goals, stars",
    "transfers": "Confirmed football transfers and contract news (Fabrizio Romano style 'here we go' deals)",
    "ethiopian_football": "Ethiopian football: Ethiopian Premier League, Saint George (ቅዱስ ጊዮርጊስ), Ethiopia Bunna, Walia Ibex, CAF",
    "athletics": "Ethiopian athletics and running (ሩጫ): marathons, Diamond League, World Athletics, Olympic records",
    "boxing": "Boxing (ቦክስ): major fights and results, heavyweight scene, East-African boxers",
    "womens": "Women's sports (የሴቶች ስፖርት): Women's Super League, Champions League, Ethiopian women athletes",
}
SPORT_WHEEL = ["premier", "ethiopian_football", "athletics", "champions", "womens", "boxing", "transfers", "ethiopian_football",
               "premier", "athletics"]
_wheel["sport"] = 0
offer_log = []
none_streak = {"media": 0, "sport": 0}
_SPORT_WORDS = {
    "ethiopian_football": ["ethiopian premier league", "ethiopia national", "walia", "saint george", "st george", "ethiopia bunna",
                           "ethiopian football", "ethiopian players", "ቅዱስ ጊዮርጊስ", "ኢትዮጵያ ቡና", "ብሔራዊ ቡድን", "ዋልያ", "የኢትዮጵያ ፕሪሚየር"],
    "athletics": ["athletics", "marathon", "diamond league", "world athletics", "olympic", "10,000m", "5,000m", "half marathon",
                  "sprint", "ሩጫ", "አትሌት", "ማራቶን", "ዳይመንድ ሊግ", "አትሌቲክስ"],
    "boxing": ["boxing", "boxer", "heavyweight", "knockout", "title fight", "ቦክስ"],
    "womens": ["women's", "womens", "wsl", "lionesses", "female", "women football", "ሴቶች", "የሴቶች"],
    "transfers": ["transfer", "signing", "signs", "here we go", "contract", "loan deal", "bid ", "ዝውውር", "ፈረመ", "ኮንትራት"],
    "champions": ["champions league", "uefa", "europa league", "la liga", "serie a", "bundesliga", "ligue 1", "real madrid",
                  "barcelona", "bayern", "ቻምፒየንስ ሊግ", "ላ ሊጋ", "ሴሪኤ", "ቡንደስሊጋ"],
    "premier": ["premier league", "arsenal", "chelsea", "liverpool", "manchester", "tottenham", "newcastle", "aston villa", "epl",
                "ፕሪሚየር ሊግ", "አርሰናል", "ቼልሲ", "ሊቨርፑል", "ማንቸስተር"],
}


def classify_sport_topic(text):
    low = " " + (text or "").lower() + " "
    scores = {k: sum(1 for w in ws if w in low) for k, ws in _SPORT_WORDS.items()}
    best = max(scores.values())
    if best == 0:
        return "other"
    order = ["ethiopian_football", "athletics", "boxing", "womens", "transfers", "champions", "premier"]
    return next(k for k in order if scores.get(k) == best)


hunt_locks = {"media": threading.Lock(), "sport": threading.Lock()}
hunt_daily = {"media": {"date": "", "count": 0}, "sport": {"date": "", "count": 0}}
recent_outlets = {"media": [], "sport": []}
recent_picks = {"media": [], "sport": []}
BALANCE_WINDOW = 4

DEFAULT_FEEDS = {
    "media": (
        "BBC Amharic|https://feeds.bbci.co.uk/amharic/rss.xml,"
        "Fana BC|https://www.fanabc.com/feed/,"
        "Walta|https://waltainfo.com/feed/,"
        "Addis Standard|https://addisstandard.com/feed/,"
        "ENA|https://www.ena.et/feed,"
        "EBC|https://www.ebc.et/feed/,"
        "Capital Ethiopia|https://www.capitalethiopia.com/feed/,"
        "Addis Fortune|https://addisfortune.news/feed/,"
        "The Reporter Ethiopia|https://www.thereporterethiopia.com/feed/,"
        "Ethio Negari|https://ethionegari.com/feed/,Zehabesha|https://zehabesha.com/feed/,OpenAI News|https://openai.com/news/rss.xml,TechCrunch AI|https://techcrunch.com/category/artificial-intelligence/feed/,The Verge AI|https://www.theverge.com/rss/ai-artificial-intelligence/index.xml,Hacker News|https://hnrss.org/frontpage,HuggingFace Blog|https://huggingface.co/blog/feed.xml,Product Hunt|https://www.producthunt.com/feed,Ars Technica|https://feeds.arstechnica.com/arstechnica/technology-lab"
    ),
    "sport": (
        "BBC Sport|https://feeds.bbci.co.uk/sport/football/rss.xml,"
        "Sky Sports|https://www.skysports.com/rss/12040,"
        "The Guardian|https://www.theguardian.com/football/rss,"
        "ESPN FC|https://www.espn.com/espn/rss/soccer/news,"
        "Goal Football|https://www.goal.com/feeds/en/news,"
        "BBC Athletics|https://feeds.bbci.co.uk/sport/athletics/rss.xml,"
        "BBC Boxing|https://feeds.bbci.co.uk/sport/boxing/rss.xml"
    ),
}

SEEN_FILE = os.environ.get("SEEN_FILE", os.path.join(tempfile.gettempdir(), "sekela_seen_stories.json"))
SEEN_TTL = 14 * 3600
_seen_lock = threading.Lock()
_seen_items = None
STOP_KEYS = {
    "ethiopia", "ethiopian", "ethiopians", "addis", "ababa", "news", "says", "said", "with", "from", "that", "this",
    "will", "have", "been", "after", "over", "into", "their", "about", "report", "reports", "reported", "year", "years",
    "week", "more", "than", "also", "amid", "government", "minister", "prime", "million", "billion", "percent",
    "announced", "announces", "official", "officials", "state", "country", "african", "africa", "latest", "today",
    "first", "second", "other", "between", "during", "against", "while", "which", "where", "there", "these", "those",
    "sekela", "media", "sport", "football", "match", "game", "team", "club",
}
STOP_AM = {"ኢትዮጵያ", "አዲስ", "አበባ", "ዜና", "መንግሥት", "ሚኒስትር", "ጠቅላይ", "ሰከላ", "ሚዲያ", "ስፖርት", "ምንጭ",
           "አስታወቀ", "ገለጸ", "ዘግቧል", "ተናግረዋል", "ተናግሯል", "አስታውቋል", "እንደዘገበው", "ኢትዮጵያዊ", "ሀገር", "ሃገር"}


def _stem_am(w):
    if len(w) > 3 and w[0] in "የበለከ":
        w = w[1:]
    if len(w) > 3 and w[-1] in "ንናምው":
        w = w[:-1]
    return w


def story_keys(text):
    t = text or ""
    keys = set()
    for w in re.findall(r"[A-Za-z]{4,}", t):
        lw = w.lower()
        if lw not in STOP_KEYS:
            keys.add(lw)
    for n in re.findall(r"\d[\d,.]*\d|\d{3,}", t):
        if not re.fullmatch(r"(19|20)\d\d", n):
            keys.add(n)
    for w in re.findall(r"[ሀ-፼]{3,}", t):
        w = _stem_am(w)
        if len(w) >= 3 and w not in STOP_AM:
            keys.add(w)
    return keys


def _seen_load():
    global _seen_items
    if _seen_items is not None:
        return
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            _seen_items = [i for i in json.load(f) if isinstance(i, dict) and "keys" in i and "ts" in i]
    except Exception:
        _seen_items = []


def _seen_save():
    try:
        tmp = SEEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_seen_items, f, ensure_ascii=False)
        os.replace(tmp, SEEN_FILE)
    except Exception as e:
        log.info("seen-topics file not saved: %s", e)


def _seen_prune():
    now = time.time()
    _seen_items[:] = [i for i in _seen_items if now - i["ts"] < SEEN_TTL]


def is_seen(text):
    keys = story_keys(text)
    raw = re.sub(r"\s+", " ", (text or "").lower()).strip()[:120]
    with _seen_lock:
        _seen_load()
        _seen_prune()
        if len(keys) < 2:
            return bool(raw) and any(it.get("raw") == raw for it in _seen_items)
        for it in _seen_items:
            ik = set(it["keys"])
            shared = keys & ik
            if len(shared) >= 4 or (len(shared) >= 3 and len(shared) / max(1, min(len(keys), len(ik))) >= 0.6):
                return True
    return False


def mark_seen(text):
    keys = story_keys(text)
    raw = re.sub(r"\s+", " ", (text or "").lower()).strip()[:120]
    if not raw:
        return
    with _seen_lock:
        _seen_load()
        _seen_prune()
        _seen_items.append({"ts": time.time(), "keys": sorted(keys)[:40], "raw": raw})
        del _seen_items[:-400]
        _seen_save()


def in_quiet_hours(hour=None):
    spec = os.environ.get("HUNT_QUIET", "23-6").strip()
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", spec)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    h = addis_now().hour if hour is None else hour
    return (h >= a or h < b) if a > b else (a <= h < b)


def _parse_pub_dt(pub):
    if not pub:
        return None
    dt = None
    try:
        dt = parsedate_to_datetime(pub.strip())
    except Exception:
        try:
            dt = datetime.fromisoformat(pub.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fetch_one_feed(name, url, max_age_hours, limit):
    atom = "{http://www.w3.org/2005/Atom}"
    mrss = "{http://search.yahoo.com/mrss/}"
    dc = "{http://purl.org/dc/elements/1.1/}"
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0 SekelaNewsBot"})
        if r.status_code != 200:
            return name, []
        root = ET.fromstring(r.content)
        entries = list(root.iter("item")) or list(root.iter(atom + "entry"))
        rows = []
        now_utc = datetime.now(timezone.utc)
        for it in entries:
            title = (it.findtext("title") or it.findtext(atom + "title") or "").strip()
            pub = (it.findtext("pubDate") or it.findtext(dc + "date") or
                   it.findtext(atom + "published") or it.findtext(atom + "updated"))
            dt = _parse_pub_dt(pub)
            if pub and dt is None:
                continue
            if dt is not None and (now_utc - dt).total_seconds() > max_age_hours * 3600:
                continue
            if not title:
                continue
            link = (it.findtext("link") or "").strip()
            if not link:
                el = it.find(atom + "link")
                link = (el.get("href") if el is not None else "") or ""
            img = None
            for tag in (mrss + "content", mrss + "thumbnail"):
                el = it.find(tag)
                if el is not None and el.get("url"):
                    img = el.get("url")
                    break
            if not img:
                enc = it.find("enclosure")
                if enc is not None and (enc.get("type") or "").startswith("image"):
                    img = enc.get("url")
            age_h = round((now_utc - dt).total_seconds() / 3600, 1) if dt else 12.0
            rows.append({"source": name, "title": title, "link": link, "image": img, "age_h": age_h})
            if len(rows) >= limit:
                break
        rows.sort(key=lambda x: x["age_h"])
        return name, rows
    except Exception as e:
        log.info("feed %s unavailable: %s", name, e)
        return name, []


def fetch_google_trends(max_age_hours=24):
    ht = "{https://trends.google.com/trending/rss}"
    out = []
    now_utc = datetime.now(timezone.utc)
    for geo in ("ET",):
        try:
            r = requests.get(f"https://trends.google.com/trending/rss?geo={geo}", timeout=10,
                             headers={"User-Agent": "Mozilla/5.0 SekelaNewsBot"})
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                if not title:
                    continue
                dt = _parse_pub_dt(it.findtext("pubDate"))
                if dt is not None and (now_utc - dt).total_seconds() > max_age_hours * 3600:
                    continue
                ni = it.find(ht + "news_item")
                n_title = (ni.findtext(ht + "news_item_title") or "").strip() if ni is not None else ""
                n_url = (ni.findtext(ht + "news_item_url") or "").strip() if ni is not None else ""
                n_src = (ni.findtext(ht + "news_item_source") or "").strip() if ni is not None else ""
                headline = f"{title}: {n_title}" if n_title else title
                src = f"Google Trends ({geo})" + (f" / {n_src}" if n_src else "")
                out.append({"source": src, "title": headline[:140], "link": n_url, "image": None,
                            "age_h": round((now_utc - dt).total_seconds() / 3600, 1) if dt else 6.0})
                if len(out) >= 10:
                    break
        except Exception as e:
            log.info("Google Trends (%s) unavailable: %s", geo, short_reason(e, 80))
    return out



ETH_TG_CHANNELS = os.environ.get(
    "ETH_TG_CHANNELS",
    "tikvahethiopia,ethio_mereja,GetuTemesgen,zehabesha,RoshaTV,fanabora,NBE_Ethiopia,MoE_Ethiopia,"
    "PMOEthiopia,AdanechAbiebie,HOPREthiopia,ethiotelecom,EthiopianElectricUtility,AAWSA_Official,"
    "flyethiopian,MohEthiopia,mfaethiopia,MInTEthiopia,MoF_Ethiopia,ICS_Ethiopia,EOTC_TV,"
    "EthiopianIslamicAffairs,ShegerFM102_1,AhaduFM943,BisratFM1011,BBCAmharic,addisstandard,"
    "alainamharic,ebcnews,ENA_Ethiopia"
)
SPORT_TG_CHANNELS = os.environ.get(
    "SPORT_TG_CHANNELS",
    "tikvahethsport,ethiosport,soccerethiopia,433,FabrizioRomano,Arsenal_Ethiopia,ManUtd_Ethiopia,Ethiopian_Football"
)


def _fetch_one_tg_channel(handle, max_age_hours=24, per_channel=3):
    handle = handle.strip().lstrip("@")
    if not handle:
        return []
    out = []
    now_utc = datetime.now(timezone.utc)
    try:
        r = requests.get(f"https://t.me/s/{handle}", timeout=10,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; SekelaNewsBot/1.0)"})
        if r.status_code != 200 or "tgme_widget_message_wrap" not in r.text:
            return []
        blocks = r.text.split("<div class=\"tgme_widget_message_wrap")[1:]
        for blk in reversed(blocks[-12:]):
            mp = re.search(r'data-post="([^"]+)"', blk)
            mt = re.search(r'<time[^>]+datetime="([^"]+)"', blk)
            mx = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', blk, re.S)
            if not mx:
                continue
            age_h = 6.0
            if mt:
                try:
                    dt = datetime.fromisoformat(mt.group(1).replace("Z", "+00:00"))
                    age_h = (now_utc - dt).total_seconds() / 3600.0
                    if age_h > max_age_hours:
                        continue
                except Exception:
                    pass
            txt = re.sub(r"<br\s*/?>", " ", mx.group(1))
            txt = re.sub(r"<[^>]+>", " ", txt)
            txt = re.sub(r"\s+", " ", html_lib.unescape(txt)).strip()
            if len(txt) < 25:
                continue
            imgs = re.findall(r"tgme_widget_message_photo_wrap[^>]+background-image:url\(['\"]([^'\"]+)['\"]\)", blk)
            link = f"https://t.me/{mp.group(1)}" if mp else f"https://t.me/s/{handle}"
            out.append({
                "source": f"TG @{handle}",
                "title": txt[:180],
                "link": link,
                "image": imgs[0] if imgs else None,
                "images": imgs[:4],
                "age_h": round(age_h, 1),
            })
            if len(out) >= per_channel:
                break
    except Exception as e:
        log.info("TG scrape @%s skipped: %s", handle, short_reason(e, 70))
    return out


GOOGLE_NEWS_QUERIES = {
    "media": [
        ("ኢትዮጵያ", "am", "ET", "ET:am"),
        ("አዲስ አበባ", "am", "ET", "ET:am"),
        ("ብር ምንዛሪ ብሔራዊ ባንክ", "am", "ET", "ET:am"),
        ("Ethiopia economy OR telecom OR bank OR ministry", "en-ET", "ET", "ET:en"),
        ("Ethiopia AI OR tech OR startup", "en-ET", "ET", "ET:en"),
        ("Trump OR China OR Russia summit OR world breaking news", "en-ET", "ET", "ET:en"),
    ],
    "sport": [
        ("ዋሊያዎቹ OR ኢትዮጵያ እግር ኳስ", "am", "ET", "ET:am"),
        ("የኢትዮጵያ ፕሪሚየር ሊግ OR ቅዱስ ጊዮርጊስ", "am", "ET", "ET:am"),
        ("Ethiopian athletes OR Ethiopia athletics", "en-ET", "ET", "ET:en"),
        ("Premier League OR Champions League transfer", "en-ET", "ET", "ET:en"),
    ],
}
_source_fails = {}


def fetch_google_news_rss(brand="media", max_age_hours=24, per_query=3):
    """Fetches fresh stories from Google News RSS search (Amharic + English Ethiopia/Sport queries)."""
    out = []
    now_utc = datetime.now(timezone.utc)
    queries = GOOGLE_NEWS_QUERIES.get(brand, GOOGLE_NEWS_QUERIES["media"])
    for q, hl, gl, ceid in queries:
        try:
            url = f"https://news.google.com/rss/search?q={quote_plus(q + ' when:1d')}&hl={hl}&gl={gl}&ceid={ceid}"
            r = requests.get(url, timeout=(5, 10), headers=_PHOTO_HEADERS)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            cnt = 0
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                if not title:
                    continue
                dt = _parse_pub_dt(it.findtext("pubDate"))
                if dt is not None and (now_utc - dt).total_seconds() > max_age_hours * 3600:
                    continue
                src_el = it.find("source")
                src_name = (src_el.text or "").strip() if src_el is not None else "Google News"
                out.append({
                    "source": src_name or "Google News",
                    "title": title[:180],
                    "link": link,
                    "image": None,
                    "images": [],
                    "age_h": round((now_utc - dt).total_seconds() / 3600, 1) if dt else 4.0,
                })
                cnt += 1
                if cnt >= per_query:
                    break
        except Exception as e:
            log.info("Google News RSS (%s) skipped: %s", q[:20], short_reason(e, 60))
    return out


def fetch_tg_channel_items(brand="media", max_age_hours=24, per_channel=3):
    raw = ETH_TG_CHANNELS if brand == "media" else SPORT_TG_CHANNELS
    handles = [h.strip() for h in raw.split(",") if h.strip()]
    if not handles:
        return []
    out = []
    with ThreadPoolExecutor(max_workers=min(6, len(handles))) as ex:
        for rows in ex.map(lambda h: _fetch_one_tg_channel(h, max_age_hours, per_channel), handles):
            out.extend(rows)
    return out


def fetch_feed_items(brand="media", max_age_hours=24, per_feed=5):
    env_name = "NEWS_RSS_FEEDS" if brand == "media" else "SPORT_RSS_FEEDS"
    spec = os.environ.get(env_name, DEFAULT_FEEDS[brand])
    feeds = []
    for item in [x for x in spec.split(",") if "|" in x]:
        name, url = [x.strip() for x in item.split("|", 1)]
        feeds.append((name, url, max_age_hours, 2 if (brand == "media" and "bbc" in name.lower()) else per_feed))
    per = {}
    if feeds:
        with ThreadPoolExecutor(max_workers=min(8, len(feeds))) as ex:
            for name, rows in ex.map(lambda a: _fetch_one_feed(*a), feeds):
                if rows:
                    per[name] = rows
    names = list(per)
    random.shuffle(names)
    out = []
    while any(per[n] for n in names):
        for n in names:
            if per[n]:
                out.append(per[n].pop(0))
    return out


def feed_items_text(items, limit=32):
    return "\n".join(f"- [{i['source']}] {i['title']}" for i in items[:limit])


def stories_from_feeds(items, n=8):
    return [{"importance": 6, "title": i["title"], "outlets": i["source"], "summary": i["title"],
             "link": i.get("link"), "image": i.get("image"), "images": i.get("images") or []} for i in items[:n]]


def _outlet_key(story):
    first = ((story.get("outlets") or "").split(",")[0]).strip().lower()
    if not first:
        return ""
    if first.startswith("tg @") or first.startswith("tg:"):
        return first.replace(" ", "")
    return first.split(" ")[0]


def pick_story(fresh, brand, desk=None, strict=False):
    recent_o = recent_outlets[brand][-2:]
    window = recent_picks[brand][-(BALANCE_WINDOW - 1):]
    bbc_used = any(r["outlet"] == "bbc" for r in window)
    conflict_used = any(r["topic"] == "conflict" for r in window)
    wmap = learn_weights(brand)
    classify = classify_topic if brand == "media" else classify_sport_topic
    last_topic = recent_picks[brand][-1]["topic"] if recent_picks[brand] else None

    def info(st):
        return _outlet_key(st), classify(f"{st.get('title', '')} {st.get('summary', '')}")

    def allowed(st):
        outlet, topic = info(st)
        if brand == "media":
            if outlet == "bbc" and bbc_used:
                return False
            if topic == "conflict" and conflict_used:
                return False
        return outlet not in recent_o

    pool = [st for st in fresh if allowed(st)]
    if not pool:
        if strict:
            log.info("balance rules: no suitable %s story in this scan - skipped", brand)
            return None
        pool = [st for st in fresh if info(st)[0] != "bbc" and info(st)[1] != "conflict"] or list(fresh)

    def rank(st):
        outlet, topic = info(st)
        learned = -wmap.get(topic, 1.0)
        if brand != "media":
            return (0 if topic == desk else 1, 1 if topic == last_topic else 0, learned, -st.get("importance", 0))
        tier = 0 if topic == desk else (1 if topic in PRIORITY_TOPICS else (2 if topic in ("other", "politics") else 3))
        return (tier, 1 if outlet == "bbc" else 0, learned, -st.get("importance", 0))

    chosen = sorted(pool, key=rank)[0]
    outlet, topic = info(chosen)
    recent_outlets[brand].append(outlet)
    del recent_outlets[brand][:-6]
    recent_picks[brand].append({"outlet": outlet, "topic": topic})
    del recent_picks[brand][:-8]
    log.info("picked %s story: outlet=%s topic=%s (desk=%s)", brand, outlet, topic, desk)
    return chosen


_UNSAFE_INCITEMENT = re.compile(
    r"(kill\s+(?:all|every)\s+(?:amhara|oromo|tigray|muslim|christian|orthodox)|"
    r"ethnic\s+cleansing|genocide\s+call|burn\s+(?:church|mosque)|"
    r"በዘር\s*ማጥፋት|መስጊድ\s*አቃጥሉ|ቤተክርስቲያን\s*አቃጥሉ)", re.I
)


def safety_gate_check(text):
    """Editorial Balance Gate: allows ministries, utilities, social/creators & faith updates; blocks ONLY ethnic/religious hate/incitement."""
    if not text:
        return False
    return not bool(_UNSAFE_INCITEMENT.search(text))


def hunt_once(manual=False, min_importance=None, brand="media", force_strict=None):
    """Proactive & manual news scan. Never skips silently if Gemini search fails — immediately falls back to RSS + Google Trends."""
    lock = hunt_locks[brand]
    if not lock.acquire(blocking=False):
        return "busy"
    try:
        _last_ai_error["hunt"] = ""
        if not GEMINI_API_KEY and not OPENROUTER_API_KEY:
            return "nokey"
        today = addis_now().strftime("%Y-%m-%d")
        daily = hunt_daily[brand]
        if daily["date"] != today:
            daily.update(date=today, count=0)
        cap = int(os.environ.get("HUNT_MAX_PER_DAY", "60") if brand == "media"
                  else os.environ.get("HUNT_SPORT_MAX_PER_DAY", "40"))
        if not manual and daily["count"] >= cap:
            return "cap"
        threshold = min_importance if min_importance is not None else int(os.environ.get("HUNT_MIN_IMPORTANCE", "4"))
        focus, desk, segment = HUNT_FOCUS[brand], None, None
        if brand == "media":
            wheel_topic = TOPIC_WHEEL[_wheel["media"] % len(TOPIC_WHEEL)]
            _wheel["media"] += 1
            desk = choose_desk("media", wheel_topic, PRIORITY_TOPICS)
            if desk == "coffee":
                segment = "coffee"
            focus = ("PRIORITY DESK FOR THIS SCAN: " + TOPIC_DESK[desk] + ". Return at least 3 stories from this desk or "
                     "the other priority topics. " + focus)
        else:
            wheel_topic = SPORT_WHEEL[_wheel["sport"] % len(SPORT_WHEEL)]
            _wheel["sport"] += 1
            desk = choose_desk("sport", wheel_topic, list(SPORT_DESKS))
            focus = "DESK FOR THIS SCAN: " + SPORT_DESKS[desk] + ". Also include other top sport news. " + focus
        note = learning_scan_note(brand)
        if note:
            focus += " " + note

        tg_items = fetch_tg_channel_items(brand, max_age_hours=24, per_channel=3)
        feed_items = fetch_feed_items(brand, max_age_hours=24, per_feed=5)
        trends = fetch_google_trends(max_age_hours=24) if brand == "media" else []
        besoccer_items = fetch_besoccer_items(max_items=8) if brand == "sport" else []
        gnews_items = fetch_google_news_rss(brand, max_age_hours=24, per_query=3)
        rss_pool = tg_items + feed_items + gnews_items + besoccer_items + trends[:5]
        feeds_txt = feed_items_text(rss_pool)

        def unseen(lst, min_imp=None):
            return [s for s in lst if (min_imp is None or s["importance"] >= min_imp)
                    and not is_seen(f"{s['title']} {s['summary']}")]

        if brand == "media":
            rss_pool = [it for it in rss_pool if not has_any(it["title"], SPORT_WORDS)]
        else:
            _wom_skip = ("women's super league", "wsl", "man city women", "manchester city women", "arsenal women", "chelsea women", "barcelona femeni", "golf", "tiger woods", "tennis", "wimbledon", "us open tennis", "cricket", "rugby", "baseball", "nfl", "nba", "nhl", "kuwait", "shareholder", "boardroom", "lpga", "pga tour", "disciplinary", "committee", "executive", "chairman", "governing body", "lawsuit", "court ruling", "ownership", "takeover bid")
            rss_pool = [it for it in rss_pool if not any(w in it["title"].lower() for w in _wom_skip) or "ethiopia" in it["title"].lower()]
        feeds_txt = feed_items_text(rss_pool)
        feed_stories = stories_from_feeds(rss_pool, n=25) if rss_pool else []
        unseen_feeds = unseen(feed_stories, threshold) or unseen(feed_stories)
        # Save Gemini quota: only call scan_stories when feeds have <4 unseen items or on manual breaking/sport
        if manual or len(unseen_feeds) < 4:
            stories, scan_sources = scan_stories(focus, hours=24, n=10, feeds=feeds_txt)
        else:
            stories, scan_sources = [], []
        combined = [s for s in (list(stories) + feed_stories) if safety_gate_check(f"{s.get('title','')} {s.get('summary','')}")]
        fresh = unseen(combined, threshold) or unseen(combined)
        if not fresh and manual:
            fresh = combined[:12]
        if not fresh:
            none_streak[brand] += 1
            return "none"

        use_strict = (force_strict if force_strict is not None
                      else not (manual or daily["count"] == 0 or none_streak[brand] >= 1))
        story = pick_story(fresh, brand, desk, strict=use_strict)
        if story is None:
            story = pick_story(fresh, brand, desk, strict=False)
        if story is None:
            none_streak[brand] += 1
            return "none"
        none_streak[brand] = 0
        mark_seen(f"{story['title']} {story['summary']}")
        start = time.time()
        topic = f"{story['title']}. {story['summary']}"
        # Inherit matching real link/image from rss_pool if scan_stories omitted them
        s_keys = story_keys(topic)
        best_match_img = story.get("image")
        best_extra_imgs = list(story.get("images") or [])
        best_match_links = [story["link"]] if story.get("link") else []
        for it in rss_pool:
            if len(s_keys & story_keys(it.get("title", ""))) >= 2:
                if not best_match_img and it.get("image"):
                    best_match_img = it["image"]
                for im_u in (it.get("images") or []):
                    if im_u and im_u not in best_extra_imgs:
                        best_extra_imgs.append(im_u)
                if it.get("link") and it["link"] not in best_match_links:
                    best_match_links.append(it["link"])
        # Skip extra Gemini research call if TG/RSS item already has >=25 words of real context
        if len(topic.split()) >= 25 and not manual:
            facts = f"{story['title']}. {story['summary']} (Reported by: {story['outlets']})"
            sources = scan_sources or ([(story['outlets'] or "Sekela Media Desk", story.get("link") or BRANDS[brand]["tg_link"])])
        else:
            facts, sources = research_for(topic)
            if not facts:
                facts = f"{story['title']}. {story['summary']} (Reported by: {story['outlets']})"
                sources = scan_sources or ([(story['outlets'] or "Sekela Media Desk", story.get("link") or BRANDS[brand]["tg_link"])])
        d = build_draft(topic, facts=facts, sources=sources, allow_fallback=True, brand=brand,
                        photo_urls=best_match_links, story_image=best_match_img, segment=segment, extra_images=best_extra_imgs)
        if not d:
            return "failed"
        hdr = HUNT_HEADERS["coffee"] if segment == "coffee" else HUNT_HEADERS[brand]
        present_draft(OWNER_ID, "ሰበር ዜና", d, hdr, start)
        daily["count"] += 1
        bot_stats["breaking"] += 1
        offer_log.append((time.time(), brand))
        del offer_log[:-12]
        return "sent"
    except Exception as e:
        _last_ai_error["hunt"] = short_reason(e, 90)
        log.error("hunt failed (%s): %s", brand, e)
        return "failed"
    finally:
        lock.release()


def hunter_loop(brand):
    time.sleep(15 if brand == "media" else 75)
    interval_env = "HUNT_INTERVAL_MIN" if brand == "media" else "HUNT_SPORT_INTERVAL_MIN"
    while True:
        try:
            if not in_quiet_hours():
                today = addis_now().strftime("%Y-%m-%d")
                eat_h = addis_now().hour
                daily_n = hunt_daily[brand]["count"] if hunt_daily[brand]["date"] == today else 0
                morning_guarantee = (7 <= eat_h <= 10 and daily_n == 0)
                st = hunt_once(brand=brand, min_importance=4 if (morning_guarantee or daily_n == 0) else None,
                               force_strict=False if (morning_guarantee or daily_n == 0) else None)
                log.info("hunt %s: %s (daily=%d)", brand, st, daily_n)
        except Exception as e:
            log.error("hunter loop error (%s): %s", brand, e)
        _sec = int(os.environ.get("FETCH_INTERVAL_SECONDS", "0") or 0) or (max(2, int(os.environ.get(interval_env, "2" if brand == "media" else "8"))) * 60)
        time.sleep(max(60, _sec))


def start_hunter():
    if os.environ.get("HUNTER_ENABLED", "1") == "0":
        return
    threading.Thread(target=hunter_loop, args=("media",), daemon=True, name="hunter-media").start()
    if os.environ.get("HUNTER_SPORT_ENABLED", "1") != "0":
        threading.Thread(target=hunter_loop, args=("sport",), daemon=True, name="hunter-sport").start()
    log.info("Hunters started (media every %s min, sport every %s min, quiet %s)",
             os.environ.get("HUNT_INTERVAL_MIN", "30"), os.environ.get("HUNT_SPORT_INTERVAL_MIN", "40"),
             os.environ.get("HUNT_QUIET", "23-6") or "off")


# ----------------------------------------------------------------------------
# Ethiopian-fan Sports Engine + 1080x1350 3-4-3 Match Result Cards
# ----------------------------------------------------------------------------
ESPN_LEAGUES = {"eng.1": "Premier League", "uefa.champions": "Champions League", "uefa.europa": "Europa League",
                "esp.1": "La Liga", "ita.1": "Serie A", "ger.1": "Bundesliga", "fra.1": "Ligue 1",
                "fifa.worldq.caf": "WC Qualifiers CAF", "caf.nations": "AFCON / CAF", "fifa.friendly": "International Friendly"}
LEAGUE_AM = {"eng.1": "ፕሪሚየር ሊግ", "uefa.champions": "ቻምፒየንስ ሊግ", "uefa.europa": "ዩሮፓ ሊግ", "esp.1": "ላ ሊጋ",
             "ita.1": "ሴሪኤ", "ger.1": "ቡንደስሊጋ", "fra.1": "ሊግ 1",
             "fifa.worldq.caf": "የዓለም ዋንጫ ማጣሪያ (አፍሪካ)", "caf.nations": "የአፍሪካ ዋንጫ", "fifa.friendly": "ዓለም አቀፍ የወዳጅነት ጨዋታ"}
LEAGUE_TAGS = {"eng.1": "#PremierLeague #ፕሪሚየርሊግ", "uefa.champions": "#ChampionsLeague #ቻምፒየንስሊግ",
               "uefa.europa": "#EuropaLeague", "esp.1": "#LaLiga #ላሊጋ", "ita.1": "#SerieA #ሴሪኤ",
               "ger.1": "#Bundesliga", "fra.1": "#Ligue1",
               "fifa.worldq.caf": "#Walias #Ethiopia #CAF", "caf.nations": "#AFCON #Walias", "fifa.friendly": "#InternationalFriendly"}
TEAM_AM = {"ethiopia": "ኢትዮጵያ (ዋልያዎቹ)", "manchester united": "ማንቸስተር ዩናይትድ", "manchester city": "ማንቸስተር ሲቲ", "arsenal": "አርሰናል", "chelsea": "ቼልሲ",
           "liverpool": "ሊቨርፑል", "tottenham": "ቶተንሃም", "newcastle": "ኒውካስል", "aston villa": "አስቶን ቪላ",
           "everton": "ኤቨርተን", "west ham": "ዌስትሃም", "brighton": "ብራይተን", "barcelona": "ባርሴሎና", "real madrid": "ሪያል ማድሪድ",
           "atletico": "አትሌቲኮ ማድሪድ", "bayern": "ባየርን ሙኒክ", "dortmund": "ቦሩሲያ ዶርትሙንድ", "paris saint": "ፓሪስ ሴንት ዠርሜን",
           "juventus": "ጁቬንቱስ", "inter": "ኢንተር ሚላን", "milan": "ኤሲ ሚላን", "napoli": "ናፖሊ", "roma": "ሮማ"}
DEFAULT_FAVORITES = ["Ethiopia", "Arsenal", "Chelsea", "Liverpool", "Manchester United", "Manchester City", "Tottenham", "Newcastle",
                     "Barcelona", "Real Madrid", "Bayern", "Paris Saint", "Juventus", "Inter", "Milan", "Atletico", "Dortmund"]
WEEKDAY_AM = ["ሰኞ", "ማክሰኞ", "ረቡዕ", "ሐሙስ", "ዓርብ", "ቅዳሜ", "እሁድ"]
_sport_state = {}
_sport_sent = set()
_sport_sched = {"ts": 0.0, "matches": []}
_sport_daily = {"date": "", "count": 0}
_UA = {"User-Agent": "Mozilla/5.0 SekelaSportBot"}


def team_am(name):
    low = (name or "").lower()
    for k, v in TEAM_AM.items():
        if k in low:
            return v
    return name


def fmt_eat(dt_utc):
    t = dt_utc.astimezone(timezone(timedelta(hours=3)))
    h = t.hour
    part = "ሌሊት" if h < 6 else ("ጠዋት" if h < 12 else ("ቀን" if h < 18 else "ምሽት"))
    h12 = h % 12 or 12
    return f"{part} {h12}:{t.minute:02d}"


def sport_leagues():
    raw = os.environ.get("SPORT_LEAGUES", "")
    return [x.strip() for x in raw.split(",") if x.strip()] or list(ESPN_LEAGUES)


def fetch_besoccer_items(max_items=8):
    """Scrapes BeSoccer livescore/today page for live scores, upcoming big matches, and match stories."""
    out = []
    for url in ("https://www.besoccer.com/livescore", "https://www.besoccer.com/"):
        try:
            r = requests.get(url, headers=_PHOTO_HEADERS, timeout=(5, 10))
            if r.status_code != 200:
                continue
            html = r.text[:350000]
            # Extract match links & titles like /match/arsenal/manchester-united/...
            seen_m = set()
            for m in re.finditer(r'href="(https?://www\.besoccer\.com/match/[^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
                href = m.group(1)
                if href in seen_m:
                    continue
                txt = re.sub(r"<[^>]+>", " ", m.group(2))
                txt = re.sub(r"\s+", " ", html_lib.unescape(txt)).strip()
                if len(txt) < 6 or len(txt) > 120:
                    continue
                seen_m.add(href)
                out.append({
                    "source": "BeSoccer",
                    "title": f"Match Update: {txt}",
                    "link": href,
                    "image": None,
                    "images": [],
                    "age_h": 1.0,
                })
                if len(out) >= max_items:
                    return out
        except Exception as e:
            log.info("BeSoccer fetch skipped: %s", short_reason(e, 70))
    return out


def espn_scoreboard(league, dates=None):
    try:
        r = None
        for attempt in (1, 2):
            try:
                resp = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard",
                                    params={"dates": dates} if dates else None, headers=_UA, timeout=(5, 12))
                if resp.status_code == 200:
                    r = resp
                    break
            except requests.RequestException as e:
                log.info("ESPN %s attempt %d failed: %s", league, attempt, short_reason(e, 80))
            time.sleep(1)
        if r is None:
            return []
        out = []
        for ev in r.json().get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            cs = comp.get("competitors") or []
            home = next((c for c in cs if c.get("homeAway") == "home"), None)
            away = next((c for c in cs if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            st = ev.get("status") or comp.get("status") or {}
            typ = st.get("type") or {}
            try:
                ko = datetime.fromisoformat((ev.get("date") or "").replace("Z", "+00:00"))
                if ko.tzinfo is None:
                    ko = ko.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            goals = []
            for dt in comp.get("details") or []:
                if dt.get("scoringPlay"):
                    ath = ((dt.get("athletesInvolved") or [{}])[0] or {}).get("displayName", "")
                    goals.append({"min": (dt.get("clock") or {}).get("displayValue", ""),
                                  "team_id": str((dt.get("team") or {}).get("id", "")), "player": ath,
                                  "own": bool(dt.get("ownGoal")), "pen": bool(dt.get("penaltyKick"))})
            try:
                hs, as_ = int(home.get("score") or 0), int(away.get("score") or 0)
            except Exception:
                hs = as_ = 0
            out.append({"id": str(ev.get("id")), "league": league, "league_name": ESPN_LEAGUES.get(league, league),
                        "kickoff": ko, "state": typ.get("state", "pre"), "status_name": typ.get("name", ""),
                        "detail": typ.get("shortDetail", ""), "home": (home.get("team") or {}).get("displayName", "?"),
                        "away": (away.get("team") or {}).get("displayName", "?"),
                        "home_id": str((home.get("team") or {}).get("id", "")),
                        "away_id": str((away.get("team") or {}).get("id", "")), "hs": hs, "as": as_, "goals": goals})
        return out
    except Exception as e:
        log.info("ESPN %s failed: %s", league, short_reason(e, 100))
        return []


def find_match_photo(m):
    if not m or not m.get("id") or not m.get("league"):
        return None
    try:
        r = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{m['league']}/summary",
                         params={"event": m["id"]}, headers=_UA, timeout=8)
        if r.status_code != 200:
            return None
        js = r.json()
        img_urls, page_urls = [], []
        for im in (js.get("article") or {}).get("images") or []:
            if im.get("url") and int(im.get("width") or 700) >= 600:
                img_urls.append(im["url"])
        for n in js.get("news") or []:
            for im in n.get("images") or []:
                if im.get("url") and int(im.get("width") or 700) >= 600:
                    img_urls.append(im["url"])
            href = ((n.get("links") or {}).get("web") or {}).get("href")
            if href:
                page_urls.append(href)
        for u in img_urls[:3]:
            b = download_photo(u)
            if b:
                return b
        if page_urls:
            return find_real_photo(page_urls[:2], max_pages=2)
    except Exception as e:
        log.info("match photo lookup skipped: %s", short_reason(e, 80))
    return None


def sport_favorites():
    return DEFAULT_FAVORITES + [t for t in learn_favorites() if t not in DEFAULT_FAVORITES]


def notable_match(m):
    if os.environ.get("SPORT_ALERT_ALL", "0") == "1" or m["league"] == "uefa.champions":
        return True
    names = f"{m['home']} {m['away']}".lower()
    return any(f.lower() in names for f in sport_favorites())


def refresh_schedule(force=False):
    if not force and time.time() - _sport_sched["ts"] < 3 * 3600 and _sport_sched["matches"]:
        return _sport_sched["matches"]
    base = datetime.now(timezone.utc)
    days = [(base + timedelta(days=d)).strftime("%Y%m%d") for d in (-1, 0, 1)]
    jobs = [(lg, dstr) for lg in sport_leagues() for dstr in days]
    seen, allm = set(), []

    def _one(job):
        try:
            return espn_scoreboard(job[0], job[1])
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=8) as ex:
        for rows in ex.map(_one, jobs):
            for m in rows:
                if m["id"] not in seen:
                    seen.add(m["id"])
                    allm.append(m)
    if allm:
        _sport_sched.update(ts=time.time(), matches=allm)
        return allm
    _sport_sched["ts"] = time.time() - 3 * 3600 + 600
    log.warning("ESPN schedule refresh returned nothing - keeping %d cached matches", len(_sport_sched["matches"]))
    return _sport_sched["matches"]


def _scorer_lines(m):
    goals = m.get("goals") or []
    if not goals:
        return []
    home_g, away_g = [], []
    for g in goals:
        who = (g.get("player") or "").strip()
        if not who:
            continue
        last_name = who.split()[-1] if len(who) > 14 else who
        mn = re.sub(r"[^\d+]", "", g.get("min") or "")
        tag = " (P)" if g.get("pen") else (" (OG)" if g.get("own") else "")
        item = f"{last_name} {mn}'{tag}" if mn else f"{last_name}{tag}"
        if g.get("team_id") == m.get("home_id"):
            home_g.append(item)
        else:
            away_g.append(item)
    if not home_g and not away_g:
        return []
    parts = []
    if home_g:
        parts.append(", ".join(home_g[:4]))
    if away_g:
        parts.append(", ".join(away_g[:4]))
    return ["  |  ".join(parts)]


def compose_score_card(title, m, line1="", line2="", bg_bytes=None):
    if not PIL_OK:
        return None
    eth = find_ethiopic_font()
    if not eth:
        return None
    try:
        W, H = 1080, 1350
        th = CARD_THEMES["sport"]
        has_photo = False
        if bg_bytes:
            try:
                img = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
                img = _cover_crop_resize(img, W, H)
                img = ImageEnhance.Contrast(img).enhance(1.07)
                img = _apply_seamless_gradient(img, start_ratio=0.50, tint=(6, 18, 14), max_alpha=248)
                has_photo = True
            except Exception:
                has_photo = False
        if not has_photo:
            img = Image.new("RGB", (W, H), (6, 30, 24))
            bg_draw = ImageDraw.Draw(img, "RGBA")
            for y in range(H):
                k = y / H
                bg_draw.line([(0, y), (W, y)], fill=(int(6 + 8 * k), int(44 - 22 * k), int(34 - 16 * k)))
            bg_draw.ellipse([-320, 620, 1400, 1650], outline=(255, 255, 255, 18), width=6)
            bg_draw.line([(0, 880), (W, 880)], fill=(255, 255, 255, 14), width=4)

        d = ImageDraw.Draw(img, "RGBA")
        d.rectangle([0, 0, W, 10], fill=th["gold"] + (255,))
        f_eth = lambda sz: ImageFont.truetype(eth, sz)
        margin = 54

        fe_p, fl_p = f_eth(34), _latin_font(32)
        pw = int(_measure(title, fe_p, fl_p)) + 52
        d.rounded_rectangle([margin, 38, margin + pw, 98], radius=30, fill=th["gold"] + (250,))
        _draw_mixed(d, margin + 26, 80, title, fe_p, fl_p, (16, 18, 16, 255))
        lg_txt = f"{LEAGUE_AM.get(m['league'], m['league_name'])} · {m['league_name']}"
        fe_l, fl_l = f_eth(28), _latin_font(26)
        _draw_bold_shadow(d, margin, 142, lg_txt, fe_l, fl_l, (240, 244, 242, 245))

        badge_h = 56
        badge_y = H - 46 - badge_h
        scorers = _scorer_lines(m)
        extra_lines = scorers + ([line1] if (line1 and not scorers) else []) + ([line2] if line2 else [])

        score_str = f"{m['hs']}  -  {m['as']}"
        f_sc_e, f_sc_l = f_eth(106), _latin_font(106)
        sc_w = _measure(score_str, f_sc_e, f_sc_l)
        col_w = (W - 2 * margin - sc_w - 48) / 2

        home_name, away_name = team_am(m["home"]), team_am(m["away"])
        t_fs = 48
        for t_fs in range(48, 28, -2):
            fe_t, fl_t = f_eth(t_fs), _latin_font(t_fs)
            hl = _wrap(home_name, fe_t, fl_t, col_w)
            al = _wrap(away_name, fe_t, fl_t, col_w)
            if len(hl) <= 2 and len(al) <= 2:
                break

        fe_t, fl_t = f_eth(t_fs), _latin_font(t_fs)
        hl = _wrap(home_name, fe_t, fl_t, col_w)[:2]
        al = _wrap(away_name, fe_t, fl_t, col_w)[:2]

        extra_h = len(extra_lines) * 44
        box_h = 190 + extra_h
        box_y1 = badge_y - 28
        box_y0 = box_y1 - box_h
        d.rounded_rectangle([margin, box_y0, W - margin, box_y1], radius=24,
                            fill=(8, 22, 18, 215), outline=th["gold"] + (180,), width=2)

        sc_x = (W - sc_w) / 2
        _draw_bold_shadow(d, sc_x, box_y0 + 122, score_str, f_sc_e, f_sc_l, th["gold"] + (255,))

        left_cx = margin + 16 + col_w / 2
        for idx, ln in enumerate(hl):
            lw = _measure(ln, fe_t, fl_t)
            y_ln = box_y0 + (78 if len(hl) == 2 else 98) + idx * (t_fs + 10)
            _draw_bold_shadow(d, left_cx - lw / 2, y_ln, ln, fe_t, fl_t, (255, 252, 245, 255))

        right_cx = W - margin - 16 - col_w / 2
        for idx, ln in enumerate(al):
            lw = _measure(ln, fe_t, fl_t)
            y_ln = box_y0 + (78 if len(al) == 2 else 98) + idx * (t_fs + 10)
            _draw_bold_shadow(d, right_cx - lw / 2, y_ln, ln, fe_t, fl_t, (255, 252, 245, 255))

        if extra_lines:
            d.line([(margin + 40, box_y0 + 152), (W - margin - 40, box_y0 + 152)], fill=(255, 255, 255, 45), width=2)
            ey = box_y0 + 196
            for el in extra_lines[:2]:
                s_fs = 30
                fe_s, fl_s = f_eth(s_fs), _latin_font(s_fs)
                while _measure(el, fe_s, fl_s) > W - 2 * margin - 60 and s_fs > 20:
                    s_fs -= 2
                    fe_s, fl_s = f_eth(s_fs), _latin_font(s_fs)
                ew = _measure(el, fe_s, fl_s)
                _draw_mixed(d, (W - ew) / 2, ey, el, fe_s, fl_s, (220, 248, 234, 255))
                ey += 38

        b_eth, b_lat = f_eth(30), _latin_font(28)
        text = BRANDS["sport"]["badge"]
        bw = int(_measure(text, b_eth, b_lat)) + 44
        d.rounded_rectangle([margin, badge_y, margin + bw, badge_y + badge_h], radius=28,
                            fill=th["main"] + (248,), outline=th["gold"] + (255,), width=2)
        _draw_mixed(d, margin + 22, badge_y + 39, text, b_eth, b_lat, (255, 255, 255, 255))
        _draw_social_footer(d, W - margin, badge_y + badge_h // 2, BRANDS["sport"].get("tg_handle", "@SekelaSportET"))

        out = io.BytesIO()
        img.save(out, "JPEG", quality=92)
        return out.getvalue()
    except Exception as e:
        log.warning("score card failed: %s", e)
        return None


def _min_am(minute):
    mm = re.sub(r"[^\d+]", "", minute or "")
    return f"{mm}ኛው ደቂቃ" if mm else ""


def _goal_line(g, m):
    team = m["home"] if g.get("team_id") == m["home_id"] else m["away"]
    who, mn = g.get("player", ""), _min_am(g.get("min"))
    if g.get("own"):
        return f"{who} በራሱ መረብ ላይ" + (f" በ{mn}" if mn else "") + " አስቆጠረ"
    how = " ከፍጹም ቅጣት ምት" if g.get("pen") else ""
    return (f"{who}" if who else team_am(team)) + (f" በ{mn}" if mn else "") + how + (f" ለ{team_am(team)}" if who else "") + " ጎል አስቆጠረ"


def push_sport_alert(kind, m, headline, body, card_title=None, line1="", line2="", tags_extra=""):
    try:
        _push_sport_alert_impl(kind, m, headline, body, card_title, line1, line2, tags_extra)
    except Exception as e:
        log.warning("sport alert %s failed: %s", kind, short_reason(e, 120))


def _push_sport_alert_impl(kind, m, headline, body, card_title=None, line1="", line2="", tags_extra=""):
    if in_quiet_hours() and kind in ("goal", "pre", "ht"):
        return
    today = addis_now().strftime("%Y-%m-%d")
    if _sport_daily["date"] != today:
        _sport_daily.update(date=today, count=0)
    if kind in ("goal", "pre", "ht", "ft") and _sport_daily["count"] >= int(os.environ.get("SPORT_ALERT_MAX_PER_DAY", "30")):
        return
    tags = " ".join(build_tags("sport", (LEAGUE_TAGS.get(m["league"], "") + " " + tags_extra).strip(), headline)) if m else \
        " ".join(build_tags("sport", tags_extra, headline))
    post = f"🔴 {headline}\n\n{body}\n\n{brand_footer('sport')}\n\n{tags}"
    bg = find_match_photo(m) if (card_title and m and kind in ("ft", "ht", "goal")) else None
    card = compose_score_card(card_title, m, line1, line2, bg_bytes=bg) if (card_title and m) else None
    d = {"post": post, "headline": headline, "image": card, "card": bool(card), "sources": [("ESPN", "https://www.espn.com/soccer/")],
         "brand": "sport", "pkg": {}, "facts": "", "bg": bg, "real": bool(bg), "unverified": False, "fixed": True,
         "text_only": card is None, "topic": {"goal": "live_goal", "pre": "prematch", "ht": "halftime", "ft": "fulltime",
                                             "md": "matchday"}.get(kind, kind)}
    header = {"goal": "⚽🔥 ጎል! ለሰከላ ስፖርት ፖስት እናድርገው?", "pre": "⏰ የቅድመ-ጨዋታ ማሳሰቢያ ዝግጁ ነው",
              "ht": "⏸ የእረፍት ውጤት ዝግጁ ነው", "ft": "🏁 የጨዋታ ፍጻሜ ውጤት ዝግጁ ነው",
              "md": "📅 የጨዋታ ፕሮግራም ዝግጁ ነው"}.get(kind, "⚽ ሄኖክ፣ አዲስ የስፖርት ረቂቅ ዝግጁ ነው")
    present_draft(OWNER_ID, "", d, header)
    _sport_daily["count"] += 1


def build_matchday(day_offset=0, force=False):
    eat = addis_now() + timedelta(days=day_offset)
    key = ("md", eat.strftime("%Y-%m-%d"))
    if key in _sport_sent and not force:
        return False
    ms = [m for m in refresh_schedule(force=force)
          if m["kickoff"].astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d") == eat.strftime("%Y-%m-%d")]
    ms.sort(key=lambda m: (not notable_match(m), m["kickoff"]))
    ms = ms[:14]
    if not ms:
        return False
    _sport_sent.add(key)
    by_league = {}
    for m in sorted(ms, key=lambda m: m["kickoff"]):
        by_league.setdefault(m["league"], []).append(m)
    lines = []
    for lg, items in by_league.items():
        lines.append(f"🏆 {LEAGUE_AM.get(lg, lg)}")
        lines += [f"• {fmt_eat(m['kickoff'])} — {team_am(m['home'])} ከ {team_am(m['away'])}" for m in items]
        lines.append("")
    label = "የዛሬ" if day_offset == 0 else "የነገ"
    headline = f"{label} ጨዋታዎች በኢትዮጵያ ሰዓት"
    body = (f"📅 {WEEKDAY_AM[eat.weekday()]} {eat.strftime('%d %b')} — {label} ጨዋታዎች\n\n" + "\n".join(lines).strip() +
            "\n\n⏰ ሰዓቱ በምስራቅ አፍሪካ ሰዓት (EAT) ነው።")
    push_sport_alert("md", None, headline, body, tags_extra="#MatchDay #የዛሬጨዋታ #እግርኳስ")
    return True


TABLE_ALIASES = {
    "epl": "eng.1", "pl": "eng.1", "premier": "eng.1",
    "laliga": "esp.1", "liga": "esp.1", "spain": "esp.1",
    "seriea": "ita.1", "italy": "ita.1",
    "bundesliga": "ger.1", "germany": "ger.1",
    "ligue1": "fra.1", "france": "fra.1",
    "ucl": "uefa.champions", "champions": "uefa.champions",
    "uel": "uefa.europa", "europa": "uefa.europa",
}


def _safe_int(val, default=0):
    try:
        return int(float(val)) if val is not None else default
    except Exception:
        return default


def espn_standings(league="eng.1", top=10):
    try:
        r = requests.get(f"https://site.api.espn.com/apis/v2/sports/soccer/{league}/standings",
                         headers=_PHOTO_HEADERS, timeout=(5, 15))
        if r.status_code == 200:
            js = r.json()
            groups = js.get("children") or [js]
            rows = []
            for g in groups:
                for e in ((g.get("standings") or {}).get("entries") or []):
                    team = ((e.get("team") or {}).get("displayName") or "").strip()
                    stats = {s.get("name"): (s.get("value") if s.get("value") is not None else s.get("displayValue"))
                             for s in (e.get("stats") or []) if s.get("name")}
                    if team:
                        rows.append({
                            "rank": _safe_int(stats.get("rank"), len(rows) + 1),
                            "team": team,
                            "gp": _safe_int(stats.get("gamesPlayed"), 0),
                            "gd": _safe_int(stats.get("pointDifferential"), 0),
                            "pts": _safe_int(stats.get("points"), 0),
                        })
            if rows:
                rows.sort(key=lambda x: (x["rank"], -x["pts"], -x["gd"]))
                return rows[:top]
    except Exception as e:
        log.info("ESPN standings %s failed: %s", league, short_reason(e, 80))
    return []


def format_standings_post(league="eng.1", top=10):
    rows = espn_standings(league, top=top)
    if not rows:
        return None
    lg_am = LEAGUE_AM.get(league, ESPN_LEAGUES.get(league, league))
    lg_tag = LEAGUE_TAGS.get(league, "#Football")
    lines = [f"📊 የ{lg_am} የደረጃ ሰንጠረዥ (Top {len(rows)})", ""]
    for i, r in enumerate(rows, 1):
        gd = f"+{r['gd']}" if r['gd'] > 0 else str(r['gd'])
        lines.append(f"{i}. {team_am(r['team'])} — {r['pts']} ነጥብ ({r['gp']} ጨዋታ | GD {gd})")
    lines += ["", "📌 ምንጭ፡ ESPN / Sekela Sport", brand_footer("sport"), f"{lg_tag} #SekelaSport #ሰከላስፖርት"]
    return "\n".join(lines), f"የ{lg_am} የደረጃ ሰንጠረዥ"


def sport_engine_tick():
    now = datetime.now(timezone.utc)
    eat = addis_now()
    day = eat.strftime("%Y-%m-%d")
    if eat.hour >= 7 and ("md", day) not in _sport_sent:
        build_matchday(0)
    if eat.hour >= 19 and ("md", (eat + timedelta(days=1)).strftime("%Y-%m-%d")) not in _sport_sent:
        build_matchday(1)
    sched = refresh_schedule()
    live_leagues = {m["league"] for m in sched
                    if now - timedelta(hours=3, minutes=15) <= m["kickoff"] <= now + timedelta(minutes=40)}
    for lg in live_leagues:
        for m in espn_scoreboard(lg):
            st = _sport_state.get(m["id"])
            if st is None:
                _sport_state[m["id"]] = {"hs": m["hs"], "as": m["as"], "goals": len(m["goals"]), "pre": m["state"] != "pre",
                                         "ht": m["state"] != "pre" and m["status_name"] != "STATUS_FIRST_HALF",
                                         "ft": m["state"] == "post"}
                st = _sport_state[m["id"]]
                if m["state"] == "pre":
                    st.update(pre=False, ht=False, ft=False)
            if not notable_match(m):
                st.update(hs=m["hs"], **{"as": m["as"]}, goals=len(m["goals"]))
                continue
            mins_to_ko = (m["kickoff"] - now).total_seconds() / 60
            if m["state"] == "pre" and not st["pre"] and 18 <= mins_to_ko <= 40:
                st["pre"] = True
                body = (f"⏰ በ30 ደቂቃ ውስጥ ይጀምራል!\n\n{team_am(m['home'])} ከ {team_am(m['away'])} — "
                        f"{LEAGUE_AM.get(m['league'], m['league_name'])}\n🕒 {fmt_eat(m['kickoff'])} (EAT)")
                push_sport_alert("pre", m, f"{team_am(m['home'])} ከ {team_am(m['away'])} በቅርቡ ይጀምራል", body, "KICK-OFF SOON",
                                 line1=f"🕒 {fmt_eat(m['kickoff'])} (EAT)", tags_extra="#MatchDay #እግርኳስ")
            if m["state"] in ("in", "post") and (m["hs"] + m["as"]) > (st["hs"] + st["as"]):
                new_goals = m["goals"][st["goals"]:] if len(m["goals"]) > st["goals"] else []
                g = new_goals[-1] if new_goals else {}
                scorer = _goal_line(g, m) if g else "ጎል ተቆጥሯል"
                body = (f"⚽ ጎል!\n\n{team_am(m['home'])} {m['hs']} - {m['as']} {team_am(m['away'])}\n{scorer}።\n"
                        f"🏆 {LEAGUE_AM.get(m['league'], m['league_name'])}")
                push_sport_alert("goal", m, f"ጎል! {team_am(m['home'])} {m['hs']}-{m['as']} {team_am(m['away'])}", body, "GOAL!",
                                 line1=(g.get("player") or "") + (f" {g.get('min')}" if g.get("min") else ""),
                                 tags_extra="#Goal #ጎል #እግርኳስ")
            st.update(hs=m["hs"], **{"as": m["as"]}, goals=len(m["goals"]))
            if m["status_name"] == "STATUS_HALFTIME" and not st["ht"]:
                st["ht"] = True
                gl = "\n".join("⚽ " + _goal_line(g, m) for g in m["goals"][:6])
                body = (f"⏸ እረፍት\n\n{team_am(m['home'])} {m['hs']} - {m['as']} {team_am(m['away'])}\n"
                        f"🏆 {LEAGUE_AM.get(m['league'], m['league_name'])}" + (f"\n\n{gl}" if gl else ""))
                push_sport_alert("ht", m, f"እረፍት፡ {team_am(m['home'])} {m['hs']}-{m['as']} {team_am(m['away'])}", body, "HALF TIME",
                                 tags_extra="#HalfTime #እግርኳስ")
            if m["state"] == "post" and not st["ft"]:
                st["ft"] = True
                gl = "\n".join("⚽ " + _goal_line(g, m) for g in m["goals"][:8])
                body = (f"🏁 ጨዋታው ተጠናቀቀ\n\n{team_am(m['home'])} {m['hs']} - {m['as']} {team_am(m['away'])}\n"
                        f"🏆 {LEAGUE_AM.get(m['league'], m['league_name'])}" + (f"\n\n{gl}" if gl else ""))
                push_sport_alert("ft", m, f"የጨዋታ ፍጻሜ፡ {team_am(m['home'])} {m['hs']}-{m['as']} {team_am(m['away'])}", body,
                                 "FULL TIME", tags_extra="#FullTime #ውጤት #እግርኳስ")
    if len(_sport_state) > 400:
        for k in list(_sport_state)[:200]:
            _sport_state.pop(k, None)


def sport_engine_loop():
    time.sleep(120)
    while True:
        try:
            sport_engine_tick()
        except Exception as e:
            log.warning("sport engine error: %s", e)
        time.sleep(max(30, int(os.environ.get("SPORT_POLL_SEC", "75"))))


# ---- 60-second highlight clips -> video drafts ---------------------------------------------------------------------
video_drafts = {}
HL_SEEN_FILE = os.environ.get("HL_SEEN_FILE", os.path.join(tempfile.gettempdir(), "sekela_highlights_seen.json"))
_hl_seen = None
HL_GOOD = ["goal", "goals", "highlights", "stunning", "screamer", "save", "saves", "brace", "hat-trick", "hat trick", "winner"]
HL_BAD = ["full match", "press conference", "live stream", "podcast", "interview", "preview", "predictions", "quiz"]


def _hl_load():
    global _hl_seen
    if _hl_seen is None:
        try:
            with open(HL_SEEN_FILE, "r", encoding="utf-8") as f:
                _hl_seen = set(json.load(f))
        except Exception:
            _hl_seen = set()


def _hl_mark(vid):
    _hl_load()
    _hl_seen.add(vid)
    try:
        with open(HL_SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(_hl_seen)[-400:], f)
    except Exception:
        pass


def highlight_channels():
    raw = os.environ.get("SPORT_HIGHLIGHT_CHANNELS", "Premier League|UC6c1z7bA__85CIWZ_jpCK-Q")
    out = []
    for it in raw.split(","):
        if "|" in it:
            n, c = [x.strip() for x in it.split("|", 1)]
            if c:
                out.append((n, c))
    return out


def yt_video_info(url):
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30,
                           "skip_download": True}) as y:
        return y.extract_info(url, download=False)


def _sekela_video_overlay(brand="media", width=1280, height=720):
    """Generates a transparent PNG watermark/badge overlay so every video carries Sekela branding."""
    if not PIL_OK:
        return None
    try:
        ov = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(ov, "RGBA")
        accent = (0, 135, 81) if brand == "sport" else (200, 16, 46)
        sub_en = "SPORT" if brand == "sport" else "MEDIA"
        handle = BRANDS.get(brand, BRANDS["media"]).get("tg_handle", "@sekelamediaET")
        sw = _draw_sekela_s_emblem(draw, 28, 24, 46, accent)
        f_logo = _latin_font(22)
        f_sub = _latin_font(12)
        tx = 28 + sw + 12
        draw.text((tx, 24), "SEKELA", font=f_logo, fill=(255, 255, 255, 240))
        sb_w = int(f_sub.getlength(sub_en))
        draw.rounded_rectangle([tx, 50, tx + sb_w + 18, 69], radius=9, fill=accent + (235,))
        draw.text((tx + 9, 53), sub_en, font=f_sub, fill=(255, 255, 255, 255))
        f_foot = _latin_font(18)
        fw = int(f_foot.getlength(handle))
        draw.rounded_rectangle([width - fw - 44, height - 46, width - 22, height - 18], radius=12, fill=(20, 21, 24, 175))
        draw.text((width - fw - 33, height - 42), handle, font=f_foot, fill=(255, 255, 255, 235))
        p_ov = os.path.join(tempfile.gettempdir(), f"sekela_vid_ov_{brand}_{width}x{height}.png")
        ov.save(p_ov, "PNG")
        return p_ov
    except Exception:
        return None


def normalize_highlight(ffmpeg, src, out, max_sec, brand="sport"):
    for crf in ("26", "32"):
        ov_path = _sekela_video_overlay(brand=brand, width=1280, height=720)
        if ov_path and os.path.isfile(ov_path):
            cmd = [
                ffmpeg, "-y", "-i", src, "-i", ov_path, "-t", str(max_sec),
                "-filter_complex",
                "[0:v]scale=w='min(iw,1280)':h='min(ih,1280)':force_original_aspect_ratio=decrease:force_divisible_by=2[v];"
                "[1:v][v]scale2ref=w=iw:h=ih[ov][base];[base][ov]overlay=0:0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", crf, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", out
            ]
        else:
            cmd = [ffmpeg, "-y", "-i", src, "-t", str(max_sec), "-vf",
                   "scale=w='min(iw,1280)':h='min(ih,1280)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", crf, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, timeout=420)
        if r.returncode != 0 and ov_path:
            cmd = [ffmpeg, "-y", "-i", src, "-t", str(max_sec), "-vf",
                   "scale=w='min(iw,1280)':h='min(ih,1280)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", crf, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", out]
            r = subprocess.run(cmd, capture_output=True, timeout=420)
        if r.returncode != 0 or not os.path.isfile(out):
            raise RuntimeError("ffmpeg failed: " + r.stderr.decode("utf-8", "ignore")[-160:])
        if os.path.getsize(out) < 48 * 1024 * 1024:
            return
    raise RuntimeError("clip is larger than 48 MB")


def prepare_video_draft(url, source_name="", chat_id=None):
    chat_id = chat_id or OWNER_ID
    max_sec = int(os.environ.get("SPORT_CLIP_MAX_SEC", "60"))
    workdir = tempfile.mkdtemp(prefix="hl-")
    keep = False
    try:
        ffmpeg = get_ffmpeg()
        if not ffmpeg:
            return False, "ffmpeg አልተገኘም"
        try:
            info = yt_video_info(url)
        except Exception as e:
            return False, "የቪዲዮ መረጃ ማግኘት አልተቻለም: " + short_reason(e, 120)
        dur = int(info.get("duration") or 0)
        if dur and dur > max_sec + 5:
            return False, f"ቪዲዮው {dur} ሰከንድ ነው (ከ{max_sec} ሰከንድ በላይ) — አጭር ክሊፕ ላክ"
        title = info.get("title") or ""
        desc = (info.get("description") or "")[:500]
        source_name = source_name or info.get("uploader") or info.get("channel") or "ምንጭ"
        src, err = download_youtube(url, workdir)
        if not src:
            return False, "ማውረድ አልተቻለም: " + str(err)
        out = os.path.join(workdir, "clip.mp4")
        normalize_highlight(ffmpeg, src, out, max_sec)
        facts = f"Video title: {title}\nDescription: {desc}\nPublished by: {source_name}\nVideo link: {url}"
        brief = (NEWS_BRIEF.replace("{topic}", title).replace("{brand_note}", BRANDS["sport"]["brief_note"])
                 .replace("{facts}", facts).replace("{mode_note}", "This is a SHORT VIDEO HIGHLIGHT post. Describe what "
                                                    "the clip shows using only the facts above (teams, scorer, minute only "
                                                    "if given). LEAD 1-2 sentences, CONTEXT 3-4 sentences. Put NONE in "
                                                    "OFFICIAL.\n") + learning_block("sport"))
        text, _ = llm_write(brief, system=NEWSROOM_SYSTEM, max_tokens=2500, temperature=0.5, allow_gemini=True,
                            models=NEWS_WRITER_MODELS)
        pkg = parse_package(text or "")
        headline = clean_headline(pkg.get("headline"), _para(pkg.get("lead")), max_words=7) or "የጎል ቪዲዮ"
        tags = " ".join(build_tags("sport", pkg.get("tags"), headline))
        parts = [f"🔴 {headline}", _para(pkg.get("lead")), _para(pkg.get("context"))]
        full = "\n\n".join(p for p in parts if p) + f"\n\n🎥 ምንጭ፡ {source_name}\n\n{brand_footer('sport')}\n\n{tags}"
        if len(full) > 1024:
            ctx = " ".join(re.split(r"(?<=።)\s*", _para(pkg.get("context")))[:2])
            full = "\n\n".join(p for p in [parts[0], parts[1], ctx] if p) + f"\n\n🎥 ምንጭ፡ {source_name}\n\n{brand_footer('sport')}\n\n{tags}"
        if len(full) > 1024:
            full = f"🔴 {headline}\n\n{_para(pkg.get('lead'))}\n\n🎥 ምንጭ፡ {source_name}\n\n{tags}"[:1020]
        vid = uuid.uuid4().hex[:10]
        with state_lock:
            video_drafts[vid] = {"path": out, "caption": full, "headline": headline, "busy": False, "tg": False, "fb": False,
                                 "topic": "highlight", "brand": "sport", "ts": time.time(), "url": url}
        keep = True
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ አጽድቅና ፖስት አድርግ", callback_data=f"vk:{vid}"),
               types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"vn:{vid}"))
        with open(out, "rb") as vf:
            bot.send_video(chat_id, vf, caption=full[:1020], reply_markup=kb, supports_streaming=True, timeout=180)
        bot.send_message(chat_id, "🎬 የ60 ሰከንድ ቪዲዮ ረቂቅ ዝግጁ ነው — 🔒 ✅ ካልነካህ አይለጠፍም።\n"
                                  f"⚠️ የቪዲዮው መብት የ «{source_name}» ነው፤ ከመለጠፍህ በፊት መጠቀም እንደሚፈቀድ አረጋግጥ።")
        prune_video_drafts()
        return True, "ok"
    except Exception as e:
        log.warning("highlight draft failed: %s", e)
        return False, short_reason(e, 150)
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def prune_video_drafts(max_age=4 * 3600):
    now = time.time()
    with state_lock:
        old = [k for k, v in video_drafts.items() if now - v.get("ts", 0) > max_age]
        items = [video_drafts.pop(k) for k in old]
    for v in items:
        shutil.rmtree(os.path.dirname(v["path"]), ignore_errors=True)


def fb_video_publish(path, caption, brand="sport"):
    pid = BRANDS[brand]["fb_id"]
    with open(path, "rb") as f:
        data = f.read()
    js = _fb_request("POST", f"{pid}/videos", data={"description": caption, "published": "true"},
                     files={"source": ("clip.mp4", data, "video/mp4")}, timeout=600, brand=brand)
    return js.get("id")


def approve_video(chat_id, msg_id, vid):
    with state_lock:
        d = video_drafts.get(vid)
        if d is None or d["busy"]:
            expired = d is None
            d = None
        else:
            expired = False
            d["busy"] = True
    if d is None:
        if expired:
            bot.send_message(chat_id, "⚠️ ይህ ቪዲዮ ጊዜው አልፏል ወይም ቀድሞ ተስተናግዷል።")
        return
    b = BRANDS["sport"]
    tg_err = fb_err = None
    try:
        try:
            bot.edit_message_caption("⏳ እየለጠፍኩ ነው...", chat_id, msg_id)
        except Exception:
            pass
        if not d["tg"]:
            try:
                with open(d["path"], "rb") as vf:
                    bot.send_video(b["tg"], vf, caption=d["caption"][:1024], supports_streaming=True, timeout=300)
                d["tg"] = True
                bot_stats["tg_posts"] += 1
            except Exception as e:
                tg_err = short_reason(e, 160)
        if fb_enabled("sport") and not d["fb"]:
            try:
                fb_video_publish(d["path"], d["caption"], "sport")
                d["fb"] = True
                bot_stats["fb_posts"] += 1
            except Exception as e:
                fb_err = short_reason(e, 160)
    finally:
        d["busy"] = False
    if (d["tg"] or d["fb"]) and not d.get("learned"):
        d["learned"] = True
        learn_event("approve", d)
    fb_on = fb_enabled("sport")
    done = d["tg"] and (d["fb"] or not fb_on)
    text = (f"{'✅ ተለጥፏል 🚀' if done else '⚠️ በከፊል ተሳክቷል'} ({brand_label('sport')})\n"
            f"• Telegram: {'✅' if d['tg'] else '❌ ' + (tg_err or '')}\n"
            f"• Facebook: {'✅' if d['fb'] else ('⚪ አልተዋቀረም' if not fb_on else '❌ ' + (fb_err or ''))}")
    kb = None
    if not done:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔁 ያልተሳካውን እንደገና ሞክር", callback_data=f"vk:{vid}"),
               types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"vn:{vid}"))
    try:
        bot.edit_message_caption(text, chat_id, msg_id, reply_markup=kb)
    except Exception:
        bot.send_message(chat_id, text, reply_markup=kb)
    if done:
        with state_lock:
            video_drafts.pop(vid, None)
        shutil.rmtree(os.path.dirname(d["path"]), ignore_errors=True)


def highlight_poll_once():
    made = 0
    _hl_load()
    for name, cid in highlight_channels():
        try:
            r = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", headers=_UA, timeout=20)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
            for en in root.findall("a:entry", ns)[:8]:
                vid = en.findtext("yt:videoId", default="", namespaces=ns)
                title = en.findtext("a:title", default="", namespaces=ns)
                low = title.lower()
                if not vid or vid in _hl_seen:
                    continue
                _hl_mark(vid)
                if any(b in low for b in HL_BAD) or not any(g in low for g in HL_GOOD):
                    continue
                if os.environ.get("SPORT_HIGHLIGHT_ALL", "0") != "1" and not any(f.lower() in low for f in sport_favorites()):
                    continue
                if in_quiet_hours():
                    continue
                ok, msg = prepare_video_draft(f"https://www.youtube.com/watch?v={vid}", name)
                log.info("highlight %s: %s %s", vid, ok, msg)
                if ok:
                    made += 1
                if made >= 2:
                    return made
        except Exception as e:
            log.info("highlight feed %s failed: %s", name, short_reason(e, 100))
    return made


def highlight_loop():
    time.sleep(240)
    while True:
        try:
            highlight_poll_once()
        except Exception as e:
            log.warning("highlight loop error: %s", e)
        time.sleep(max(5, int(os.environ.get("SPORT_HIGHLIGHT_INTERVAL_MIN", "20"))) * 60)


def start_sport_engine():
    if os.environ.get("SPORT_ENGINE", "1") != "0":
        threading.Thread(target=sport_engine_loop, daemon=True, name="sport-engine").start()
    if os.environ.get("SPORT_HIGHLIGHTS", "1") != "0" and highlight_channels():
        threading.Thread(target=highlight_loop, daemon=True, name="sport-highlights").start()
    log.info("Sport engine started (leagues: %s)", ",".join(sport_leagues()))


# ----------------------------------------------------------------------------
# YouTube module
# ----------------------------------------------------------------------------
class YTError(Exception):
    pass


def _load_yt_channels():
    chans, seen = [], set()

    def add(name, tok):
        tok = (tok or "").strip()
        if tok and tok not in seen:
            seen.add(tok)
            chans.append({"name": (name or f"YouTube {len(chans) + 1}").strip(), "refresh": tok})

    add(os.environ.get("YOUTUBE_NAME", "") or "YouTube", YT_REFRESH_TOKEN)
    for i in range(1, 10):
        add(os.environ.get(f"YOUTUBE_NAME_{i}", ""), os.environ.get(f"YOUTUBE_REFRESH_TOKEN_{i}", ""))
    raw = os.environ.get("YOUTUBE_TOKENS_JSON", "").strip()
    if raw:
        try:
            js = json.loads(raw)
            if isinstance(js, dict):
                js = [{"name": k, "refresh_token": v} for k, v in js.items()]
            for it in js if isinstance(js, list) else []:
                if isinstance(it, str):
                    it = {"refresh_token": it}
                add(it.get("name"), it.get("refresh_token") or it.get("token"))
        except Exception as e:
            log.warning("YOUTUBE_TOKENS_JSON is not valid JSON: %s", e)
    return chans


YT_CHANNELS = _load_yt_channels()
_yt_tokens = {}
_yt_lock = threading.Lock()


def yt_enabled():
    return bool(YT_CLIENT_ID and YT_CLIENT_SECRET and YT_CHANNELS)


def yt_access_token(idx=0):
    if not yt_enabled():
        raise YTError("YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN አልተሞሉም")
    if not 0 <= idx < len(YT_CHANNELS):
        raise YTError("ይህ የ YouTube ቻናል አልተዋቀረም")
    with _yt_lock:
        cache = _yt_tokens.setdefault(idx, {"token": "", "exp": 0.0})
        if cache["token"] and time.time() < cache["exp"] - 60:
            return cache["token"]
        try:
            r = requests.post("https://oauth2.googleapis.com/token", data={
                "client_id": YT_CLIENT_ID, "client_secret": YT_CLIENT_SECRET,
                "refresh_token": YT_CHANNELS[idx]["refresh"], "grant_type": "refresh_token"}, timeout=30)
        except requests.RequestException as e:
            raise YTError(f"network error: {short_reason(e, 100)}")
        if r.status_code != 200:
            raise YTError(f"token HTTP {r.status_code}: {short_reason(r.text, 150)}")
        js = r.json()
        cache.update(token=js.get("access_token", ""), exp=time.time() + int(js.get("expires_in", 3000)))
        if not cache["token"]:
            raise YTError("no access_token returned")
        return cache["token"]


def _yt_get(path, params, idx=0):
    token = yt_access_token(idx)
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


def yt_channel_stats(idx=0):
    js = _yt_get("channels", {"part": "snippet,statistics,contentDetails", "mine": "true"}, idx)
    items = js.get("items") or []
    if not items:
        raise YTError("ለዚህ ቶክን ቻናል አልተገኘም")
    ch = items[0]
    st = ch.get("statistics", {})
    out = {"idx": idx, "title": (ch.get("snippet") or {}).get("title", "?"),
           "subs": int(st.get("subscriberCount", 0)), "views": int(st.get("viewCount", 0)),
           "videos": int(st.get("videoCount", 0)), "hidden_subs": bool(st.get("hiddenSubscriberCount")),
           "recent": []}
    uploads = ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    if uploads:
        try:
            pl = _yt_get("playlistItems", {"part": "contentDetails", "playlistId": uploads, "maxResults": 8}, idx)
            ids = [i["contentDetails"]["videoId"] for i in pl.get("items", []) if i.get("contentDetails")]
            if ids:
                vs = _yt_get("videos", {"part": "snippet,statistics", "id": ",".join(ids)}, idx)
                byid = {v["id"]: v for v in vs.get("items", [])}
                for vid in ids:
                    v = byid.get(vid)
                    if not v:
                        continue
                    sx = v.get("statistics", {})
                    out["recent"].append({"id": vid, "title": (v.get("snippet") or {}).get("title", ""),
                                          "published": ((v.get("snippet") or {}).get("publishedAt") or "")[:10],
                                          "views": int(sx.get("viewCount", 0)), "likes": int(sx.get("likeCount", 0)),
                                          "comments": int(sx.get("commentCount", 0))})
        except YTError as e:
            log.info("recent uploads unavailable: %s", e)
    return out


def yt_analytics_window(idx, days):
    try:
        token = yt_access_token(idx)
        end = addis_now().date()
        start = end - timedelta(days=max(1, days) - 1)
        r = requests.get("https://youtubeanalytics.googleapis.com/v2/reports", params={
            "ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
            "metrics": "views,estimatedMinutesWatched,subscribersGained,subscribersLost,likes,comments",
            "dimensions": "day", "sort": "day"}, headers={"Authorization": f"Bearer {token}"}, timeout=40)
        if r.status_code != 200:
            return None
        js = r.json()
        cols = [c.get("name") for c in js.get("columnHeaders", [])]
        rows = js.get("rows") or []
        if not rows:
            return None
        tot = {c: 0 for c in cols if c != "day"}
        series = []
        for row in rows:
            rec = dict(zip(cols, row))
            for c in tot:
                tot[c] += rec.get(c, 0) or 0
            series.append((str(rec.get("day", ""))[5:], int(rec.get("views", 0) or 0)))
        return {"views": int(tot.get("views", 0)), "minutes": int(tot.get("estimatedMinutesWatched", 0)),
                "gained": int(tot.get("subscribersGained", 0)), "lost": int(tot.get("subscribersLost", 0)),
                "likes": int(tot.get("likes", 0)), "comments": int(tot.get("comments", 0)), "series": series}
    except Exception as e:
        log.info("YouTube Analytics window failed: %s", short_reason(e, 120))
        return None


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
    token = yt_access_token(0)
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
# Long video -> 9:16 Shorts clipper
# ----------------------------------------------------------------------------
CLIP_PROMPT = (
    "You are a viral Shorts editor. Watch this video and choose the {n} best self-contained segments for YouTube "
    "Shorts / TikTok / Reels: each 25-55 seconds long, starting on a strong hook and ending on a complete thought. "
    "Return ONLY a JSON array (no markdown, no comments) of objects with keys: "
    "\"start\" (MM:SS), \"end\" (MM:SS), \"title\" (max 70 chars), \"description\" (2-3 short lines), "
    "\"hashtags\" (4-6 hashtags), \"score\" (1-10). Write title/description in natural Amharic if the video is Amharic."
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
        if s is None or e is None or e <= s or e - s < 8:
            continue
        if e - s > 59:
            e = s + 59
        tags = [t if str(t).startswith("#") else "#" + str(t) for t in (c.get("hashtags") or []) if str(t).strip()]
        clips.append({"start": s, "end": e, "title": _para(str(c.get("title", "")))[:100] or "Short",
                      "description": _para(str(c.get("description", ""))), "tags": tags[:8],
                      "score": c.get("score", "")})
    return clips[:max_clips]


def get_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def cut_vertical_clip(ffmpeg, src, start, end, out):
    cmd = [ffmpeg, "-y", "-ss", f"{start:.2f}", "-i", src, "-t", f"{end - start:.2f}",
           "-vf", "crop='min(iw,ih*9/16)':ih,scale=1080:1920:flags=lanczos,setsar=1",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, timeout=420)
    if r.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) < 5000:
        raise RuntimeError("ffmpeg failed: " + r.stderr.decode("utf-8", "ignore")[-200:])


def download_youtube(url, outdir):
    try:
        import yt_dlp
        from yt_dlp.utils import match_filter_func
    except Exception:
        return None, "yt-dlp አልተጫነም"
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
    start = time.time()
    ack_id = send_progress(chat_id, "✂️ ሄኖክ፣ ምርጥ ክፍሎችን እየመረጥኩ እና ወደ 9:16 Shorts እየቆረጥኩ ነው (ከ1-3 ደቂቃ)...")
    workdir = tempfile.mkdtemp(prefix="shorts-")
    keep = False
    try:
        ffmpeg = get_ffmpeg()
        if not ffmpeg:
            deliver(chat_id, ack_id, "ሄኖክ፣ ffmpeg አልተገኘም 😕 requirements.txt ላይ imageio-ffmpeg ጨምር።")
            return
        part = {"fileData": {"fileUri": url}} if url else _media_part(data, "video/mp4", "video")
        raw = multimodal_generate([{"text": CLIP_PROMPT.replace("{n}", "3")}, part],
                                  temperature=0.3, max_tokens=8000, thinking_budget=1024)
        clips = parse_clips(raw)
        if not clips:
            deliver(chat_id, ack_id, "ሄኖክ፣ ለ Shorts የሚሆኑ ክፍሎችን መምረጥ አልቻልኩም 😕 ሌላ ቪዲዮ ሞክር።")
            return
        if url:
            src, err = download_youtube(url, workdir)
            if not src:
                plan = "\n".join(f"{i}. {int(c['start']) // 60:02d}:{int(c['start']) % 60:02d}-"
                                 f"{int(c['end']) // 60:02d}:{int(c['end']) % 60:02d} — {c['title']}"
                                 for i, c in enumerate(clips, 1))
                deliver(chat_id, ack_id, f"ሄኖክ፣ ቪዲዮውን ማውረድ አልተቻለም ({err})። የታዩ ክፍሎች:\n" + plan)
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
        learn_track({"brand": "media", "headline": d["title"], "topic": "other"}, yt_id=vid)
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
        try:
            bot.edit_message_caption(f"❌ መጫን አልተሳካም: {short_reason(e, 200)}", chat_id, msg_id, reply_markup=kb)
        except Exception:
            bot.send_message(chat_id, f"❌ መጫን አልተሳካም: {short_reason(e, 200)}", reply_markup=kb)


# ----------------------------------------------------------------------------
# Analytics: collection, branded PNG dashboard, proactive alerts
# ----------------------------------------------------------------------------
def tg_member_count(channel=None):
    fn = getattr(bot, "get_chat_member_count", None) or getattr(bot, "get_chat_members_count", None)
    try:
        return int(fn(channel or TARGET_CHANNEL)) if fn else None
    except Exception:
        return None


def collect_analytics():
    data = {"tg": tg_member_count(), "yt": None, "fb": {}, "fb_text": ""}
    if yt_enabled():
        try:
            data["yt"] = yt_channel_stats(0)
            hist_record("yt0:subs", data["yt"]["subs"])
            hist_record("yt0:views", data["yt"]["views"])
        except Exception as e:
            data["yt"] = {"error": short_reason(e, 150)}
    if data["tg"] is not None:
        hist_record("tg:media", data["tg"])
    data["fb_text"] = fb_analytics_text("media", data["fb"])
    if isinstance(data["fb"].get("followers"), int):
        hist_record("fb:media:followers", data["fb"]["followers"])
    return data


def _fmt(n):
    return f"{n:,}" if isinstance(n, (int, float)) else "—"


def render_analytics_card(data):
    if not PIL_OK:
        return None
    try:
        W, H = 1080, 1340
        NAVY, PANEL, LINE = (14, 18, 28), (24, 31, 48), (45, 55, 80)
        GOLD, TXT, MUTED, RED = (235, 180, 45), (230, 235, 245), (160, 170, 190), (255, 110, 110)
        BLUE, GREEN, PURPLE = (40, 165, 255), (80, 210, 130), (180, 120, 255)
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

        panel(140, 150, f"Telegram — {TARGET_CHANNEL}", BLUE)
        kpi(76, 196, "Members", _fmt(data.get("tg")), BLUE if data.get("tg") is not None else RED)

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


STATS_FILE = os.environ.get("STATS_FILE", os.path.join(tempfile.gettempdir(), "sekela_stats_history.json"))
_hist_lock = threading.Lock()
_hist = None


def _hist_load():
    global _hist
    if _hist is None:
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                _hist = json.load(f)
            if not isinstance(_hist, dict):
                _hist = {}
        except Exception:
            _hist = {}


def hist_record(key, value):
    if not isinstance(value, (int, float)):
        return
    day = addis_now().strftime("%Y-%m-%d")
    with _hist_lock:
        _hist_load()
        _hist.setdefault(day, {})[key] = value
        for d in sorted(_hist)[:-60]:
            _hist.pop(d, None)
        try:
            tmp = STATS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_hist, f)
            os.replace(tmp, STATS_FILE)
        except Exception:
            pass


def hist_delta(key, current, days):
    if not isinstance(current, (int, float)):
        return None
    today = addis_now().date()
    with _hist_lock:
        _hist_load()
        past = []
        for d, vals in _hist.items():
            try:
                dd = datetime.strptime(d, "%Y-%m-%d").date()
            except Exception:
                continue
            if dd < today and key in vals:
                past.append((dd, vals[key]))
    if not past:
        return None
    past.sort()
    target = today - timedelta(days=days)
    older = [p for p in past if p[0] <= target]
    ref = older[-1] if older else past[0]
    return int(current - ref[1]), max(1, (today - ref[0]).days)


TIMEFRAMES = {"today": ("ዛሬ", 1), "week": ("7 ቀን", 7), "month": ("30 ቀን", 30), "last": ("የቅርብ ቪዲዮ/ፖስት", 0)}
AN_PLATFORMS = {"media": "📰 ሰከላ ሚዲያ", "sport": "⚽ ሰከላ ስፖርት", "yt": "▶️ YouTube", "all": "📊 ሁሉም (All)"}


def _insight_series(js):
    pts = []
    for item in js.get("data", [])[:1]:
        for v in item.get("values", []):
            val = v.get("value")
            if isinstance(val, dict):
                val = sum(x for x in val.values() if isinstance(x, (int, float)))
            if isinstance(val, (int, float)):
                pts.append(((v.get("end_time") or "")[5:10], int(val)))
    return pts


def _fb_err_text(e):
    return short_reason(e, 110) + (" — " + fb_error_hint(e) if fb_error_hint(e) else "")


def fb_window(brand, days):
    out = {"_ok": False, "totals": {}, "series": {}, "posts": [], "_notes": []}
    if not fb_enabled(brand):
        out["_err"] = "no token"
        return out
    pid = BRANDS[brand]["fb_id"]
    try:
        me = _fb_request("GET", "me", params={"fields": "name,id"}, timeout=40, brand=brand)
        out.update(_ok=True, name=me.get("name") or BRANDS[brand]["fb_name"])
    except FBError as e:
        out.update(_err=short_reason(e, 100), _hint=fb_error_hint(e))
        return out
    t = out["totals"]
    try:
        pg = _fb_request("GET", pid, params={"fields": "fan_count,followers_count"}, timeout=40, brand=brand)
        if isinstance(pg.get("followers_count"), int):
            t["followers"] = pg["followers_count"]
        if isinstance(pg.get("fan_count"), int):
            t["likes"] = pg["fan_count"]
            t.setdefault("followers", pg["fan_count"])
    except FBError as e:
        out["_notes"].append("Page fans: " + _fb_err_text(e))
    now = int(time.time())
    since = now - max(days, 2) * 86400 if days else now - 8 * 86400
    fails = 0
    for metric, label, mode, key in FB_INSIGHT_CANDIDATES:
        try:
            js = _fb_request("GET", f"{pid}/insights", params={"metric": metric, "period": "day", "since": since,
                                                              "until": now}, timeout=40, brand=brand)
            pts = _insight_series(js)
            if not pts:
                continue
            out["series"][key] = pts
            if mode == "last":
                t[key] = pts[-1][1]
            elif days == 1:
                t[key] = pts[-1][1]
            else:
                t[key] = sum(v for _, v in pts[-days:]) if days else sum(v for _, v in pts)
        except FBError as e:
            fails += 1
            if fails == 1:
                out["_notes"].append(f"Insights ({metric}): " + short_reason(e, 110))
    rich = ("message,created_time,permalink_url,shares,reactions.summary(total_count).limit(0),"
            "comments.summary(total_count).limit(0)")
    posts_raw, last_err = [], None
    for edge, fields in (("posts", rich), ("published_posts", rich), ("feed", rich), ("posts", "message,created_time")):
        try:
            posts_raw = _fb_request("GET", f"{pid}/{edge}", params={"fields": fields, "limit": 40}, timeout=45,
                                    brand=brand).get("data", [])
            if posts_raw:
                break
        except FBError as e:
            last_err = e
    if not posts_raw and last_err is not None:
        out["_notes"].append("Posts: " + _fb_err_text(last_err))
    day_eng, tot_r = {}, [0, 0, 0, 0]
    cutoff = (addis_now() - timedelta(days=days if days else 7)).strftime("%Y-%m-%d")
    for p in posts_raw:
        react = ((p.get("reactions") or {}).get("summary") or {}).get("total_count", 0) or 0
        comm = ((p.get("comments") or {}).get("summary") or {}).get("total_count", 0) or 0
        shares = (p.get("shares") or {}).get("count", 0) or 0
        date = (p.get("created_time") or "")[:10]
        rec = {"id": p.get("id"), "date": date, "reactions": react, "comments": comm, "shares": shares,
               "text": re.sub(r"\s+", " ", p.get("message") or "(ያለ ጽሑፍ)")[:48]}
        if len(out["posts"]) < 6:
            out["posts"].append(rec)
        if date >= cutoff:
            tot_r[0] += 1
            tot_r[1] += react
            tot_r[2] += comm
            tot_r[3] += shares
            day_eng[date[5:]] = day_eng.get(date[5:], 0) + react + comm + shares
    if posts_raw:
        t["posts_n"], t["reactions"], t["comments"], t["shares"] = tot_r
        out["series"]["post_engagement"] = sorted(day_eng.items())
        if "engagement" not in t:
            t["engagement"] = tot_r[1] + tot_r[2] + tot_r[3]
            out["_notes"].append("Engagement ከፖስቶች ድምር ነው የተሰላው")
    return out


def _sign(n):
    return f"+{n:,}" if n > 0 else f"{n:,}"


def section_brand(brand, tf):
    label, days = TIMEFRAMES[tf]
    b = BRANDS[brand]
    sec = {"title": f"{b['name_en']} — {label}", "brand": brand, "kpis": [], "bars": [], "bars_title": "", "chart": "h",
           "facts": [], "notes": []}
    win_days = days if days else 7
    win_txt = "latest day" if days == 1 else f"{win_days} days"
    short = "1d" if days == 1 else f"{win_days}d"
    tg = tg_member_count(b["tg"])
    if tg is not None:
        hist_record(f"tg:{brand}", tg)
        d = hist_delta(f"tg:{brand}", tg, max(days, 1))
        sec["kpis"].append(("Telegram members", f"{tg:,}", _sign(d[0]) if d else ""))
        sec["facts"].append(f"Telegram {b['tg']}: {tg:,} members" + (f", change {_sign(d[0])} over {d[1]} day(s)" if d else ""))
    else:
        sec["notes"].append("Telegram ቁጥር ማምጣት አልተቻለም (ቦቱ የቻናሉ አድሚን መሆን አለበት)")
    fb = fb_window(brand, win_days)
    if not fb["_ok"]:
        sec["notes"].append("Facebook አልተገናኘም: " + (fb.get("_err") or "token") + (" — " + fb["_hint"] if fb.get("_hint") else ""))
        return sec
    t = fb["totals"]
    if "followers" in t:
        hist_record(f"fb:{brand}:followers", t["followers"])
        d = hist_delta(f"fb:{brand}:followers", t["followers"], max(days, 1))
        sec["kpis"].append(("FB followers", f"{t['followers']:,}", _sign(d[0]) if d else ""))
        sec["facts"].append(f"Facebook followers: {t['followers']:,}" + (f", change {_sign(d[0])} over {d[1]} day(s)" if d else ""))
    if "likes" in t and t.get("likes") != t.get("followers"):
        sec["kpis"].append(("Page likes", f"{t['likes']:,}", ""))
    for key, lbl in (("reach", "Reach"), ("views", "Views / Impressions"), ("engagement", "Engaged"), ("new_follows", "New followers")):
        if key in t:
            sec["kpis"].append((f"{lbl} ({short})", f"{t[key]:,}", ""))
            sec["facts"].append(f"Facebook {lbl.lower()} ({win_txt}): {t[key]:,}")
    if "posts_n" in t:
        sec["kpis"].append((f"Posts ({short})", f"{t['posts_n']:,}", ""))
        sec["kpis"].append((f"Reactions ({short})", f"{t['reactions']:,}", ""))
        sec["kpis"].append((f"Comments / Shares ({short})", f"{t['comments']:,} / {t['shares']:,}", ""))
        sec["facts"].append(f"Facebook posts in {win_txt}: {t['posts_n']} posts, {t['reactions']} reactions, {t['comments']} comments, {t['shares']} shares")
    posts = fb["posts"]
    if tf == "last":
        if posts:
            p0 = posts[0]
            sec["kpis"] = sec["kpis"][:1] + [("Latest post", p0["date"], ""), ("Reactions", f"{p0['reactions']:,}", ""),
                                             ("Comments", f"{p0['comments']:,}", ""), ("Shares", f"{p0['shares']:,}", "")]
            sec["bars_title"] = "Latest posts — engagement"
            sec["bars"] = [(f"{p['date'][5:]} {p['text']}", p["reactions"] + p["comments"] + p["shares"]) for p in posts]
            sec["facts"].append(f"Latest Facebook post ({p0['date']}): {p0['reactions']} reactions, {p0['comments']} comments, {p0['shares']} shares")
        else:
            sec["notes"].append("የቅርብ ፖስት አልተገኘም")
    else:
        ser = fb["series"].get("views") or fb["series"].get("reach")
        title = "Facebook daily views" if fb["series"].get("views") else "Facebook daily reach"
        if not ser or len(ser) < 3:
            ser, title = fb["series"].get("post_engagement"), "Facebook engagement per day (from posts)"
        if ser and len(ser) >= 3:
            sec["chart"], sec["bars_title"], sec["bars"] = "v", title, ser[-(win_days if win_days > 1 else 7):]
        else:
            rows = [(lbl, t[k]) for k, lbl in (("views", "Views"), ("reach", "Reach"), ("engagement", "Engaged"),
                                                ("reactions", "Reactions"), ("comments", "Comments"),
                                                ("shares", "Shares"), ("new_follows", "New followers")) if k in t]
            sec["bars_title"], sec["bars"] = "Facebook — key numbers", rows
    for n in fb["_notes"][:2]:
        sec["notes"].append(n)
    return sec


def section_youtube(idx, tf):
    label, days = TIMEFRAMES[tf]
    sec = {"title": "", "brand": "yt", "kpis": [], "bars": [], "bars_title": "", "chart": "h", "facts": [], "notes": []}
    try:
        yt = yt_channel_stats(idx)
    except Exception as e:
        sec["title"] = f"YouTube — {YT_CHANNELS[idx]['name'] if idx < len(YT_CHANNELS) else '?'}"
        sec["notes"].append("YouTube ማምጣት አልተቻለም: " + short_reason(e, 150))
        return sec
    sec["title"] = f"YouTube — {yt['title']} — {label}"
    hist_record(f"yt{idx}:subs", yt["subs"])
    hist_record(f"yt{idx}:views", yt["views"])
    subs_txt = "hidden" if yt["hidden_subs"] else f"{yt['subs']:,}"
    if tf == "last":
        vids = yt["recent"][:6]
        if vids:
            v0 = vids[0]
            sec["kpis"] = [("Subscribers", subs_txt, ""), ("Views", f"{v0['views']:,}", ""), ("Likes", f"{v0['likes']:,}", ""),
                           ("Comments", f"{v0['comments']:,}", "")]
            sec["bars_title"] = "Latest videos — views"
            sec["bars"] = [(f"{v['published'][5:]} {v['title'].replace(chr(10), ' ')[:34]}", v["views"]) for v in vids]
            sec["facts"].append(f"Latest video «{v0['title'][:60]}»: {v0['views']:,} views, {v0['likes']:,} likes, {v0['comments']:,} comments")
        else:
            sec["notes"].append("የቅርብ ቪዲዮ አልተገኘም")
        return sec
    ana = yt_analytics_window(idx, days)
    ds = hist_delta(f"yt{idx}:subs", yt["subs"], days)
    dv = hist_delta(f"yt{idx}:views", yt["views"], days)
    sec["kpis"].append(("Subscribers", subs_txt, _sign(ds[0]) if ds else ""))
    if ana:
        sec["kpis"] += [(f"Views ({days}d)", f"{ana['views']:,}", ""), ("Watch hours", f"{ana['minutes'] / 60:,.1f}", ""),
                        ("Subs gained", f"{ana['gained'] - ana['lost']:+,}", ""), ("Likes", f"{ana['likes']:,}", ""),
                        ("Comments", f"{ana['comments']:,}", "")]
        sec["facts"].append(f"YouTube {yt['title']} last {days} day(s): {ana['views']:,} views, {ana['minutes'] / 60:,.1f} watch hours, net subs {ana['gained'] - ana['lost']:+,}")
        if len(ana["series"]) >= 3:
            sec["chart"], sec["bars_title"], sec["bars"] = "v", "Daily views", ana["series"][-30:]
    else:
        sec["kpis"].append(("Total views", f"{yt['views']:,}", _sign(dv[0]) if dv else ""))
        sec["kpis"].append(("Videos", f"{yt['videos']:,}", ""))
        cutoff = (addis_now() - timedelta(days=days)).strftime("%Y-%m-%d")
        new = [v for v in yt["recent"] if v["published"] >= cutoff]
        sec["kpis"].append((f"New videos ({days}d)", str(len(new)), ""))
        sec["facts"].append(f"YouTube {yt['title']}: {subs_txt} subscribers, {yt['views']:,} total views, {yt['videos']:,} videos")
        vids = yt["recent"][:6]
        sec["bars_title"] = "Recent videos — views"
        sec["bars"] = [(f"{v['published'][5:]} {v['title'].replace(chr(10), ' ')[:34]}", v["views"]) for v in vids]
    return sec


def collect_sections(platform, tf):
    secs = []
    if platform in ("media", "all"):
        secs.append(section_brand("media", tf))
    if platform in ("sport", "all"):
        secs.append(section_brand("sport", tf))
    if platform in ("all", "yta") or re.fullmatch(r"yt\d", platform):
        if not yt_enabled():
            secs.append({"title": "YouTube", "brand": "yt", "kpis": [], "bars": [], "bars_title": "", "chart": "h",
                         "facts": [], "notes": ["YouTube አልተዋቀረም — YOUTUBE_CLIENT_ID / SECRET / REFRESH_TOKEN ጨምር"]})
        else:
            idxs = range(len(YT_CHANNELS)) if platform in ("all", "yta") else [int(platform[2:])]
            for i in idxs:
                secs.append(section_youtube(i, tf))
    return secs


def render_dashboard_png(secs, heading, sub):
    if not PIL_OK or not secs:
        return None
    try:
        W = 1080
        NAVY, PANEL, LINE = (12, 16, 26), (22, 29, 44), (44, 54, 78)
        TXT, MUTED, GOLD = (232, 237, 246), (150, 160, 182), (214, 175, 62)
        ACC = {"media": (176, 36, 62), "sport": (22, 160, 112), "yt": (235, 64, 64)}
        compact = len(secs) >= 3
        chart_h = 230 if compact else 330
        eth = find_ethiopic_font()

        def fonts(size):
            f_lat = _latin_font(size)
            return (ImageFont.truetype(eth, size) if eth else f_lat), f_lat

        def sec_height(sec):
            rows = max(1, -(-len(sec["kpis"]) // 3))
            h = 86 + rows * 118 + 18
            if sec["bars"]:
                h += 52 + chart_h
            h += 34 * len(sec["notes"]) + 20
            return h

        H = 150 + sum(sec_height(x) + 26 for x in secs) + 70
        img = Image.new("RGB", (W, H), NAVY)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 8], fill=GOLD)

        def put(x, y, text, size, fill, max_w=None):
            f_eth, f_lat = fonts(size)
            text = str(text)
            if max_w:
                orig = text
                while len(text) > 1 and _measure(text, f_eth, f_lat) > max_w:
                    text = text[:-1]
                if text != orig:
                    text = text.rstrip() + "…"
            _draw_mixed(d, x, y + size, text, f_eth, f_lat, fill)

        put(48, 28, heading, 40, GOLD, max_w=W - 96)
        put(48, 84, sub + "  ·  " + addis_now().strftime("%Y-%m-%d %H:%M") + " (Addis Ababa)", 22, MUTED, max_w=W - 96)

        y = 140
        for sec in secs:
            h = sec_height(sec)
            col = ACC.get(sec["brand"], GOLD)
            d.rounded_rectangle([36, y, W - 36, y + h], radius=20, fill=PANEL, outline=LINE, width=2)
            d.rounded_rectangle([36, y, 46, y + h], radius=5, fill=col)
            put(70, y + 20, sec["title"], 30, TXT, max_w=W - 170)
            ky = y + 78
            for i, (lbl, val, delta) in enumerate(sec["kpis"]):
                cx, cy = 70 + (i % 3) * 330, ky + (i // 3) * 118
                put(cx, cy, lbl, 20, MUTED, max_w=300)
                put(cx, cy + 28, val, 44, GOLD if i == 0 else TXT, max_w=300)
                if delta:
                    put(cx, cy + 82, delta, 20, (110, 220, 150) if str(delta).startswith("+") else
                        ((255, 140, 140) if str(delta).startswith("-") else MUTED), max_w=300)
            cy0 = ky + max(1, -(-len(sec["kpis"]) // 3)) * 118 + 6
            if sec["bars"]:
                put(70, cy0, sec["bars_title"], 22, TXT, max_w=W - 170)
                top = max([v for _, v in sec["bars"]] + [1])
                area_y, area_h = cy0 + 48, chart_h - 20
                if sec["chart"] == "v":
                    n = len(sec["bars"])
                    area_w = W - 170
                    bw = max(6, int(area_w / n * 0.66))
                    step = area_w / n
                    for j, (lab, val) in enumerate(sec["bars"]):
                        bh = max(4, int((area_h - 56) * val / top))
                        bx = 70 + int(j * step)
                        d.rounded_rectangle([bx, area_y + (area_h - 36) - bh, bx + bw, area_y + (area_h - 36)], radius=4, fill=col)
                        if n <= 10 or val == top:
                            put(bx, area_y + (area_h - 36) - bh - 24, f"{val:,}" if val < 100000 else f"{val // 1000}k", 16, MUTED)
                        if j % max(1, -(-n // 8)) == 0:
                            put(bx, area_y + area_h - 28, lab, 16, MUTED)
                else:
                    rows = sec["bars"][:6]
                    row_h = min(46, area_h // max(1, len(rows)))
                    for j, (lab, val) in enumerate(rows):
                        yy = area_y + j * row_h
                        put(70, yy, lab, 20, TXT, max_w=340)
                        wbar = max(8, int(430 * val / top))
                        d.rounded_rectangle([430, yy + 3, 430 + wbar, yy + 27], radius=7, fill=col)
                        put(430 + wbar + 10, yy, f"{val:,}", 20, MUTED)
                cy0 += 52 + chart_h
            for k, note in enumerate(sec["notes"]):
                put(70, cy0 + k * 34, "• " + note, 20, (255, 170, 120), max_w=W - 170)
            y += h + 26
        put(48, H - 52, "Sekela Media · Sekela Sport — private analytics (read-only)", 18, MUTED)
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as e:
        log.warning("dashboard render failed: %s", e)
        return None


AN_INSIGHT_PROMPT = (
    "You are the social-media growth analyst of ሄኖክ (owner of ሰከላ ሚዲያ, ሰከላ ስፖርት and YouTube channels). Using ONLY "
    "the numbers below, write a short growth note in natural, fluent Amharic with EXACTLY these two headed parts:\n"
    "📈 ምን ጥሩ እየሠራ ነው?  - 2-3 '•' bullets: strongest numbers, growth, best post or topic.\n"
    "💡 ምን ይሻሻል?  - 2-3 '•' bullets: weakest numbers or gaps, each with ONE concrete action.\n"
    "Never invent a number. Speak to ሄኖክ in masculine singular (አንተ). Plain text, max 150 words.\n\nPeriod: {period}\nNUMBERS:\n{facts}"
)


def an_insights(secs, period):
    facts = "\n".join(f for sec in secs for f in sec["facts"])
    lf = learn_summary_facts()
    if lf and facts:
        facts += "\n" + lf
    if not facts:
        return "ℹ️ ለዚህ ምርጫ ቁጥሮች አልተገኙም — ከላይ ያሉትን ማስታወሻዎች አረጋግጥ።"
    text, _ = llm_write(AN_INSIGHT_PROMPT.replace("{period}", period).replace("{facts}", facts), max_tokens=900,
                        temperature=0.4, models=NEWS_WRITER_MODELS)
    if text:
        return text if "📈" in text else "📈 ግንዛቤ\n" + text
    return "📊 ቁጥሮች\n" + "\n".join("• " + f for f in facts.splitlines())


def an_platform_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📰 ሰከላ ሚዲያ", callback_data="an:p:media"),
           types.InlineKeyboardButton("⚽ ሰከላ ስፖርት", callback_data="an:p:sport"))
    kb.add(types.InlineKeyboardButton("▶️ YouTube", callback_data="an:p:yt"),
           types.InlineKeyboardButton("📊 ሁሉም (All)", callback_data="an:p:all"))
    kb.add(types.InlineKeyboardButton("⬅️ ወደ ዋናው መምረጫ", callback_data="hub:main"))
    return kb


def an_timeframe_keyboard(platform):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(types.InlineKeyboardButton("ዛሬ (Today)", callback_data=f"an:t:{platform}:today"),
           types.InlineKeyboardButton("7 ቀን (Week)", callback_data=f"an:t:{platform}:week"),
           types.InlineKeyboardButton("30 ቀን (Month)", callback_data=f"an:t:{platform}:month"))
    kb.add(types.InlineKeyboardButton("🎬 የቅርብ ቪዲዮ", callback_data=f"an:t:{platform}:last"))
    kb.add(types.InlineKeyboardButton("⬅️ ተመለስ", callback_data="an:m"))
    return kb


def an_youtube_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, ch in enumerate(YT_CHANNELS):
        kb.add(types.InlineKeyboardButton(f"▶️ {ch['name']}", callback_data=f"an:y:{i}"))
    kb.add(types.InlineKeyboardButton("📺 ሁሉም ቻናሎች", callback_data="an:y:a"))
    kb.add(types.InlineKeyboardButton("⬅️ ተመለስ", callback_data="an:m"))
    return kb


def platform_label(platform):
    if platform in AN_PLATFORMS:
        return AN_PLATFORMS[platform]
    if platform == "yta":
        return "▶️ YouTube (ሁሉም ቻናሎች)"
    m = re.fullmatch(r"yt(\d)", platform)
    if m and int(m.group(1)) < len(YT_CHANNELS):
        return "▶️ " + YT_CHANNELS[int(m.group(1))]["name"]
    return platform


def platform_label_en(platform):
    base = {"media": "Sekela Media", "sport": "Sekela Sport", "yt": "YouTube", "all": "All platforms",
            "yta": "YouTube (all channels)"}
    if platform in base:
        return base[platform]
    m = re.fullmatch(r"yt(\d)", platform)
    if m and int(m.group(1)) < len(YT_CHANNELS):
        return "YouTube: " + YT_CHANNELS[int(m.group(1))]["name"]
    return platform


def show_analytics_menu(chat_id, edit_id=None):
    text = "📊 ሄኖክ፣ ምን ማየት ትፈልጋለህ? ብራንድ ወይም ፕላትፎርም ምረጥ 👇"
    if edit_id:
        try:
            bot.edit_message_text(text, chat_id, edit_id, reply_markup=an_platform_keyboard())
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=an_platform_keyboard())


def run_analytics(chat_id, platform, tf, msg_id=None):
    start = time.time()
    label = TIMEFRAMES[tf][0]
    head = f"✅ {platform_label(platform)} · {label}"
    wait = head + "\n⏳ ዳሽቦርዱን እያዘጋጀሁ ነው (ከ15-40 ሰከንድ)..."
    if msg_id:
        try:
            bot.edit_message_text(wait, chat_id, msg_id)
        except Exception:
            msg_id = None
    if not msg_id:
        msg_id = send_progress(chat_id, wait)
    try:
        secs = collect_sections(platform, tf)
        png = render_dashboard_png(secs, "SEKELA ANALYTICS", f"{platform_label_en(platform)} · {label}")
        insights = an_insights(secs, label if tf != "last" else "latest video / post")
    except Exception as e:
        log.error("analytics failed: %s", e)
        deliver(chat_id, msg_id, head + f"\nሄኖክ፣ analytics ማምጣት አልተሳካም 😅 ({short_reason(e)})")
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔄 ሌላ ዳሽቦርድ", callback_data="an:m"))
    if png:
        try:
            if len(png) < 9_500_000:
                bot.send_photo(chat_id, io.BytesIO(png), caption=f"📊 {platform_label(platform)} — {label}")
            else:
                f = io.BytesIO(png)
                f.name = "sekela-analytics.png"
                bot.send_document(chat_id, f, caption=f"📊 {platform_label(platform)} — {label}")
        except Exception as e:
            log.warning("dashboard send failed: %s", e)
    deliver(chat_id, None, insights + "\n\n" + took(start), kb)
    try:
        bot.edit_message_text(head + " ✔️", chat_id, msg_id)
    except Exception:
        pass


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
# Audio / video / photo analysis (+ Bego photo/voice ingestion)
# ----------------------------------------------------------------------------
TRANSCRIBE_PROMPT = (
    "Transcribe this audio faithfully and completely, word for word, from the first second to the last. "
    "Write Amharic in Ethiopic script (keep English words in Latin letters). If several people speak, start a new "
    "line for each speaker change. Mark unclear parts as [...]. Do not summarize, translate, comment or repeat. "
    "Output ONLY the transcript."
)
SUMMARY_PROMPT = (
    "Below is a verbatim transcript of a voice note from ሄኖክ (it may contain small transcription errors). "
    "Write in natural Amharic, using ONLY what is in the transcript:\n"
    "📌 ማጠቃለያ: 3-6 lines with the key message.\n"
    "🔑 ዋና ዋና ነጥቦች: short • bullets.\n"
    "✅ Action Items: a checklist of tasks/decisions.\n"
    "❓ ግልጽ ያልሆኑ ነገሮች: only if something important is unclear.\n"
    "Plain text, no markdown.\n\n[TRANSCRIPT]\n"
)
VIDEO_PROMPT = (
    "Analyze this video for Henok (video producer who clips for Whop / TikTok / Shorts). Write in natural "
    "Amharic (keep English terms like Hook, CTA, CapCut). Provide:\n"
    "1) 🎬 Scene-by-scene breakdown with timestamps (MM:SS).\n"
    "2) 📌 Short summary.\n"
    "3) 🔥 THREE viral clip candidates, each 30-45s: ⏱ MM:SS-MM:SS, 🪝 Hook caption, 📈 Virality X/10, ✂️ editing tip, #️⃣ hashtags.\n"
    "Rank best first. Plain text, no markdown."
)
PHOTO_PROMPT = (
    "Analyze this image for Henok's Canva design work. Natural Amharic. Give: 1) 🖼 what you see, 2) composition & "
    "layout, 3) dominant colors as HEX codes + matching palette, 4) strengths, 5) weaknesses, 6) 4-6 Canva steps. Plain text."
)


def normalize_audio_mime(mime):
    m = (mime or "audio/ogg").split(";")[0].strip().lower()
    return {"audio/mpeg": "audio/mp3", "audio/x-m4a": "audio/aac", "audio/m4a": "audio/aac",
            "audio/mp4": "audio/aac", "audio/x-wav": "audio/wav", "application/ogg": "audio/ogg",
            "audio/opus": "audio/ogg"}.get(m, m)


def detect_media(message):
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
        return "ከ40-90 ሰከንድ" if (duration and duration > 180) else ("ከ25-50 ሰከንድ" if (duration and duration > 60) else "ከ10-25 ሰከንድ")
    if kind == "video":
        return "ከ30-50 ሰከንድ" if size < 6 * 1024 * 1024 else "ከ45-90 ሰከንድ"
    return ""


def _media_part(data, mime, kind):
    if len(data) > INLINE_LIMIT_BYTES:
        uri = gemini_upload_file(data, mime, f"{kind}-{uuid.uuid4().hex[:6]}")
        return {"fileData": {"mimeType": mime, "fileUri": uri}}
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}}


def convert_audio_to_mp3(data):
    ffmpeg = get_ffmpeg()
    if not ffmpeg or not data:
        return None, None
    tmp = tempfile.mkdtemp(prefix="voice-")
    try:
        src = os.path.join(tmp, "in.oga")
        with open(src, "wb") as f:
            f.write(data)
        for ext, mime, args in (("mp3", "audio/mp3", ["-c:a", "libmp3lame", "-b:a", "64k"]),
                                ("wav", "audio/wav", ["-c:a", "pcm_s16le"])):
            dst = os.path.join(tmp, "out." + ext)
            cmd = [ffmpeg, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000"] + args + [dst]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=120)
            except Exception:
                continue
            if r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 800:
                with open(dst, "rb") as f:
                    return f.read(), mime
        return None, None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def openrouter_transcribe(data, mime):
    if not OPENROUTER_API_KEY or mime not in ("audio/mp3", "audio/wav"):
        return None
    fmt = "mp3" if mime == "audio/mp3" else "wav"
    b64 = base64.b64encode(data).decode("ascii")
    for model in [m.strip() for m in os.environ.get("OR_AUDIO_MODELS", "google/gemini-2.0-flash-001,google/gemini-2.5-flash").split(",") if m.strip()]:
        try:
            r = requests.post(f"{OPENROUTER_URL}/chat/completions",
                              headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json",
                                       "HTTP-Referer": "https://t.me/HenokAssistantBot", "X-Title": "Henok Assistant"},
                              json={"model": model, "temperature": 0.1, "max_tokens": 8000,
                                    "messages": [{"role": "user", "content": [
                                        {"type": "text", "text": TRANSCRIBE_PROMPT},
                                        {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}}]}]},
                              timeout=180)
            if r.status_code != 200:
                continue
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if txt.strip():
                return clean_md(txt)
        except Exception as e:
            log.warning("OpenRouter audio %s failed: %s", model, short_reason(e, 100))
    return None


def transcribe_voice(data, mime):
    orig_mime = normalize_audio_mime(mime)
    candidates = []
    mp3, mp3_mime = convert_audio_to_mp3(data)
    if mp3:
        candidates.append((mp3, mp3_mime))
    candidates.append((data, orig_mime))
    transcript, last_err = None, None
    for blob, bmime in candidates:
        try:
            part = _media_part(blob, bmime, "audio")
            transcript = clean_md(multimodal_generate([{"text": TRANSCRIBE_PROMPT}, part], temperature=0.1,
                                                      max_tokens=32000, thinking_budget=0))
            break
        except Exception as e:
            last_err = e
            log.warning("voice transcript attempt (%s) failed: %s", bmime, e)
    if not transcript and mp3:
        transcript = openrouter_transcribe(mp3, mp3_mime)
    if not transcript:
        raise last_err or GeminiError("voice transcription failed")
    return transcript


def analyze_voice(data, mime):
    transcript = transcribe_voice(data, mime)
    analysis, _ = llm_write(SUMMARY_PROMPT + transcript, max_tokens=2500, temperature=0.3)
    if not analysis:
        analysis = "⚠️ ማጠቃለያውን አሁን ማዘጋጀት አልተቻለም — ሙሉ ትራንስክሪፕቱ ከታች ነው።"
    return analysis + "\n\n━━━━━━━━━━━━\n📝 ሙሉ ትራንስክሪፕት:\n" + transcript


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith(("tpn:", "tpb:", "tpv:", "tpx:")))
def on_tip_action(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "የግል ነው")
        return
    act, tid = call.data.split(":", 1)
    tip = _tip_box.get(tid)
    if act == "tpx":
        _tip_box.pop(tid, None)
        bot.answer_callback_query(call.id, "ተሰርዟል")
        drop_message(call.message.chat.id, call.message.message_id)
        return
    if not tip:
        bot.answer_callback_query(call.id, "ጥቆማው አልተገኘም")
        return
    bot.answer_callback_query(call.id, "እየተዘጋጀ ነው...")
    txt = tip.get("text") or "የተላከ የህዝብ ጥቆማ"
    photo_bytes = None
    if tip.get("photo_id"):
        try:
            photo_bytes = bot.download_file(bot.get_file(tip["photo_id"]).file_path)
        except Exception:
            photo_bytes = None
    if act == "tpb":
        threading.Thread(target=run_bego_draft, args=(call.message.chat.id, txt, photo_bytes), daemon=True).start()
    elif act == "tpn":
        threading.Thread(target=run_post, args=(call.message.chat.id, "📩 የህዝብ ጥቆማ", txt[:120]),
                         kwargs={"context_text": txt, "context_sources": [("የህዝብ ጥቆማ", BRANDS["media"]["tg_link"])],
                                 "brand": "media", "custom_photo": photo_bytes}, daemon=True).start()
    elif act == "tpv" and tip.get("video_id"):
        def _prep_tip_vid():
            workdir = tempfile.mkdtemp(prefix="tipv-")
            try:
                ffmpeg = get_ffmpeg()
                if not ffmpeg:
                    bot.send_message(call.message.chat.id, "ffmpeg አልተገኘም")
                    return
                raw_v = bot.download_file(bot.get_file(tip["video_id"]).file_path)
                src_p = os.path.join(workdir, "in.mp4")
                out_p = os.path.join(workdir, "branded.mp4")
                with open(src_p, "wb") as f:
                    f.write(raw_v)
                normalize_highlight(ffmpeg, src_p, out_p, 60, brand="media")
                cap = f"{txt[:400]}\n#Ethiopia #ኢትዮጵያ\n\n{brand_footer('media')}\n\n#SekelaMedia #ሰከላሚዲያ"[:1020]
                vid = uuid.uuid4().hex[:10]
                with state_lock:
                    video_drafts[vid] = {"path": out_p, "caption": cap, "headline": txt[:60], "busy": False,
                                         "tg": False, "fb": False, "topic": "tip_video", "brand": "media",
                                         "ts": time.time(), "url": ""}
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("✅ አጽድቅና ፖስት አድርግ", callback_data=f"vk:{vid}"),
                       types.InlineKeyboardButton("❌ ሰርዝ", callback_data=f"vn:{vid}"))
                with open(out_p, "rb") as vf:
                    bot.send_video(call.message.chat.id, vf, caption=cap, reply_markup=kb, supports_streaming=True, timeout=180)
            except Exception as e:
                bot.send_message(call.message.chat.id, f"ቪዲዮውን ማዘጋጀት አልተቻለም: {short_reason(e, 120)}")
        threading.Thread(target=_prep_tip_vid, daemon=True).start()


@bot.message_handler(content_types=["voice", "audio", "video", "video_note", "photo", "document"])
@private_owner_only
def handle_media(message):
    start = time.time()
    chat_id, uid = message.chat.id, message.from_user.id
    info = detect_media(message)
    if not info:
        bot.send_message(chat_id, "ሄኖክ፣ ይህን የፋይል አይነት ገና አልተረዳሁም 🙈 ድምፅ፣ ቪዲዮ ወይም ፎቶ ላክልኝ።")
        return
    kind, file_id, mime, size, duration = info
    if size and size > MAX_MEDIA_BYTES:
        bot.send_message(chat_id, f"ሄኖክ፣ ፋይሉ {size / 1048576:.1f}MB ነው — እስከ 18MB ብቻ ነው የምተነትነው።")
        return

    with state_lock:
        pending_mode = awaiting_topic.get(uid)
    if pending_mode == "bego" and kind in ("photo", "audio"):
        with state_lock:
            awaiting_topic.pop(uid, None)
        ack_id = send_progress(chat_id, "🤍 ሄኖክ፣ የሰከላ በጎ መረጃውን ተቀብዬ 1080x1350 ካርድ እያዘጋጀሁ ነው...")
        try:
            data = bot.download_file(bot.get_file(file_id).file_path)
            caption = (message.caption or "").strip()
            if kind == "photo":
                raw_info = caption
                if not raw_info:
                    raw_info = clean_md(multimodal_generate(
                        [{"text": "Extract all details from this Ethiopian humanitarian/missing-person image (name, age, "
                                  "location, phone number, bank account, appeal reason). Plain text."},
                         _media_part(data, mime, "photo")], temperature=0.2, max_tokens=2000))
                drop_message(chat_id, ack_id)
                if not raw_info or len(raw_info) < 10:
                    with state_lock:
                        awaiting_topic[uid] = "bego"
                    bot.send_message(chat_id, "ሄኖክ፣ ፎቶው ላይ ጽሑፍ አልነበረም — እባክህ ፎቶውን ከመግለጫ (caption) ጋር ላክልኝ ወይም ዝርዝሩን ጻፍልኝ 🤍")
                    return
                run_bego_draft(chat_id, raw_info, photo_bytes=data)
            else:
                transcript = transcribe_voice(data, mime)
                drop_message(chat_id, ack_id)
                run_bego_draft(chat_id, (caption + "\n" + transcript).strip())
            return
        except Exception as e:
            log.error("bego media failed: %s", e)
            deliver(chat_id, ack_id, f"ሄኖክ፣ የሰከላ በጎ ረቂቁን አሁን ማዘጋጀት አልተቻለም 😅 ({short_reason(e)})")
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
                    prune(video_cache, 2)
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("✂️ ክሊፖቹን ቆርጠህ 9:16 Shorts አዘጋጅ", callback_data=f"vc:{vid}"))
        deliver(chat_id, ack_id, result + "\n\n" + took(start), markup)
    except Exception as e:
        log.error("media handling failed: %s", e)
        what = {"audio": "ድምፁን", "video": "ቪዲዮውን", "photo": "ፎቶውን"}.get(kind, "ፋይሉን")
        deliver(chat_id, ack_id, f"ሄኖክ፣ {what} አሁን ማጠናቀቅ አልቻልኩም 😅\n(ምክንያት: {short_reason(e)})")


# ----------------------------------------------------------------------------
# Canva Connect API
# ----------------------------------------------------------------------------
CANVA_AUTH_URL = "https://www.canva.com/api/oauth/authorize"
CANVA_TOKEN_URL = "https://api.canva.com/rest/v1/oauth/token"
CANVA_API = "https://api.canva.com/rest/v1"
CANVA_TOKEN_FILE = os.environ.get("CANVA_TOKEN_FILE", os.path.join(tempfile.gettempdir(), "sekela_canva_tokens.json"))
_canva_lock = threading.Lock()
_canva_tokens = {}
canva_pending = {}


def canva_enabled():
    return bool(CANVA_CLIENT_ID and CANVA_CLIENT_SECRET)


def canva_redirect_uri():
    explicit = os.environ.get("CANVA_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    base = RENDER_EXTERNAL_URL.rstrip("/") if RENDER_EXTERNAL_URL else "https://henok-agent.onrender.com"
    return base + "/canva/callback"


def _canva_load():
    if _canva_tokens:
        return
    try:
        with open(CANVA_TOKEN_FILE, "r", encoding="utf-8") as f:
            _canva_tokens.update(json.load(f))
    except Exception:
        pass
    if not _canva_tokens.get("refresh") and os.environ.get("CANVA_REFRESH_TOKEN", "").strip():
        _canva_tokens.update(refresh=os.environ["CANVA_REFRESH_TOKEN"].strip(), access="", exp=0)


def _canva_save():
    try:
        tmp = CANVA_TOKEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_canva_tokens, f)
        os.replace(tmp, CANVA_TOKEN_FILE)
    except Exception:
        pass


def _canva_basic():
    return base64.b64encode(f"{CANVA_CLIENT_ID}:{CANVA_CLIENT_SECRET}".encode()).decode()


def _canva_token_request(form):
    r = requests.post(CANVA_TOKEN_URL, data=form, timeout=30, headers={
        "Authorization": "Basic " + _canva_basic(), "Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code != 200:
        raise RuntimeError(f"Canva token HTTP {r.status_code}: {short_reason(r.text, 160)}")
    js = r.json()
    if not js.get("access_token"):
        raise RuntimeError("Canva returned no access_token")
    _canva_tokens.update(access=js["access_token"], exp=time.time() + int(js.get("expires_in", 14400)))
    if js.get("refresh_token"):
        _canva_tokens["refresh"] = js["refresh_token"]
    _canva_save()


def canva_authorize_url():
    if not canva_enabled():
        raise RuntimeError("CANVA_CLIENT_ID / CANVA_CLIENT_SECRET አልተሞሉም")
    verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = uuid.uuid4().hex
    with _canva_lock:
        canva_pending[state] = {"verifier": verifier, "ts": time.time()}
        for k in [k for k, v in canva_pending.items() if time.time() - v["ts"] > 900]:
            canva_pending.pop(k, None)
    return CANVA_AUTH_URL + "?" + urlencode({
        "code_challenge": challenge, "code_challenge_method": "s256", "scope": CANVA_SCOPES, "response_type": "code",
        "client_id": CANVA_CLIENT_ID, "state": state, "redirect_uri": canva_redirect_uri()})


def canva_handle_callback(query):
    first = lambda k: (query.get(k) or [""])[0]
    if first("error"):
        return False, f"Canva: {first('error')} {first('error_description')}".strip()
    code, state = first("code"), first("state")
    if not code or not state:
        return False, "ይህ የ Canva callback ገጽ ነው — ለመገናኘት በ Telegram ላይ /canva ብለህ ጀምር።"
    with _canva_lock:
        pend = canva_pending.pop(state, None)
    if not pend:
        return False, "የግንኙነቱ ጊዜ አልፏል ወይም ልክ አይደለም — በ Telegram ላይ /canva ብለህ እንደገና ሞክር።"
    try:
        with _canva_lock:
            _canva_token_request({"grant_type": "authorization_code", "code": code, "code_verifier": pend["verifier"],
                                  "redirect_uri": canva_redirect_uri()})
    except Exception as e:
        return False, "Canva ቶክን ማግኘት አልተቻለም: " + short_reason(e, 160)
    try:
        who = canva_profile()
    except Exception:
        who = ""
    try:
        bot.send_message(OWNER_ID, "🎨 Canva ተገናኝቷል ✅" + (f" — {who}" if who else ""))
    except Exception:
        pass
    return True, "Canva ከሄኖክ ረዳት ጋር ተገናኝቷል ✅ — ወደ Telegram መመለስ ትችላለህ።"


def canva_result_page(ok, msg):
    color = "#1a9e6a" if ok else "#c0392b"
    return ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Canva · Henok Assistant</title></head><body style='font-family:sans-serif;background:#0e121c;color:#e8edf6;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            f"<div style='max-width:520px;padding:32px;text-align:center'><h2 style='color:{color}'>{'✅' if ok else '⚠️'} Canva</h2>"
            f"<p>{html_lib.escape(msg)}</p></div></body></html>")


def canva_access_token():
    if not canva_enabled():
        raise RuntimeError("CANVA_CLIENT_ID / CANVA_CLIENT_SECRET አልተሞሉም")
    with _canva_lock:
        _canva_load()
        if _canva_tokens.get("access") and time.time() < _canva_tokens.get("exp", 0) - 120:
            return _canva_tokens["access"]
        if not _canva_tokens.get("refresh"):
            raise RuntimeError("Canva ገና አልተፈቀደም — /canva ብለህ ግባ")
        _canva_token_request({"grant_type": "refresh_token", "refresh_token": _canva_tokens["refresh"]})
        return _canva_tokens["access"]


def canva_get(path, params=None):
    token = canva_access_token()
    r = requests.get(CANVA_API + path, params=params or {}, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Canva {path} HTTP {r.status_code}: {short_reason(r.text, 140)}")
    return r.json()


def canva_profile():
    js = canva_get("/users/me/profile")
    return ((js.get("profile") or {}).get("display_name") or "").strip()


def canva_recent_designs(limit=5):
    js = canva_get("/designs", {"limit": limit, "sort_by": "modified_descending"})
    return [{"title": d.get("title") or "(ያለ ርዕስ)", "url": (d.get("urls") or {}).get("view_url", "")}
            for d in js.get("items", [])[:limit]]


def _canva_status_lines():
    lines = ["🎨 Canva:"]
    if not canva_enabled():
        return lines + ["  ⚪ CANVA_CLIENT_ID / CANVA_CLIENT_SECRET አልተሞሉም"]
    lines.append("  ✅ Client ID + Secret ተሞልተዋል")
    lines.append(f"  🔗 Callback: {canva_redirect_uri()}")
    with _canva_lock:
        _canva_load()
        has_refresh = bool(_canva_tokens.get("refresh"))
    if not has_refresh:
        return lines + ["  ⚠️ ገና አልተፈቀደም — /canva ብለህ ግባ"]
    try:
        who = canva_profile()
        lines.append("  ✅ ተገናኝቷል" + (f": {who}" if who else ""))
    except Exception as e:
        lines.append(f"  ❌ {short_reason(e, 150)} — /canva ብለህ እንደገና ግባ")
    return lines


# ----------------------------------------------------------------------------
# Multi-engine AI image & logo studio (/image) — Gemini Imagen FIRST, fast <30s
# ----------------------------------------------------------------------------
image_jobs = {}
IMAGE_TRIGGERS = ["ሎጎ", "logo", "ምስል ስራ", "ምስል ሥራ", "ምስል ሰራ", "ምስል አዘጋጅ", "ምስል ፍጠር", "ሎጎ ስራ",
                  "generate image", "generate an image", "image of"]
LOGO_WORDS = ["ሎጎ", "logo", "emblem", "brand mark"]
IMAGE_PROMPT_SYSTEM = (
    "You write prompts for AI image generators. Turn the user's request (Amharic or English) into ONE detailed English "
    "prompt of 40-70 words: subject, style, composition, colours, lighting, mood. If it is a LOGO: flat vector logo, "
    "simple bold shapes, centred on a plain background, limited colour palette, no text or letters inside the image. "
    "Output only the prompt."
)


def build_studio_prompt(request):
    request = re.sub(r"\s+", " ", request or "").strip()
    is_logo = has_any(request, LOGO_WORDS)
    text, _ = llm_write(("Request (this is a LOGO): " if is_logo else "Request: ") + request, system=IMAGE_PROMPT_SYSTEM,
                        max_tokens=300, temperature=0.6, models=NEWS_WRITER_MODELS)
    prompt = (text or "").strip().strip('"')
    if len(prompt) < 20 or re.search(r"[ሀ-፼]", prompt):
        prompt = (request if not re.search(r"[ሀ-፼]", request) else "modern professional brand design") + \
                 (", flat vector logo, simple bold shapes, plain background, no text" if is_logo else ", highly detailed, professional")
    return prompt[:700], is_logo


def _valid_image(data, ctype=""):
    if not data or len(data) < 3000:
        return False
    if ctype and not ctype.startswith("image/"):
        return False
    if PIL_OK:
        try:
            Image.open(io.BytesIO(data)).verify()
        except Exception:
            return False
    return True


def gen_pollinations(prompt, models, seed, timeout=20):
    last = None
    for model in models:
        url = (f"https://image.pollinations.ai/prompt/{quote(prompt[:520], safe='')}"
               f"?model={model}&width=1024&height=1024&nologo=true&seed={seed}")
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200 and _valid_image(r.content, r.headers.get("Content-Type", "")):
                return r.content
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = short_reason(e, 80)
    raise RuntimeError(f"Pollinations {models}: {last}")


def gen_gemini_image(prompt, aspect="1:1", timeout=28):
    if not GEMINI_API_KEY:
        raise RuntimeError("no GEMINI_API_KEY")
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    errs = []
    for model in [m.strip() for m in os.environ.get(
            "GEMINI_IMAGEN_MODELS", "imagen-4.0-fast-generate-001,imagen-4.0-generate-001").split(",") if m.strip()]:
        try:
            r = requests.post(f"{GEMINI_BASE}/models/{model}:predict", headers=headers, timeout=timeout, json={
                "instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1, "aspectRatio": aspect}})
            if r.status_code != 200:
                errs.append(f"{model}=HTTP {r.status_code}")
                continue
            preds = r.json().get("predictions") or []
            if preds and preds[0].get("bytesBase64Encoded"):
                data = base64.b64decode(preds[0]["bytesBase64Encoded"])
                if _valid_image(data):
                    return data
            errs.append(f"{model}=no image")
        except Exception as e:
            errs.append(f"{model}={short_reason(e, 60)}")
    for model in [m.strip() for m in os.environ.get(
            "GEMINI_IMAGE_MODELS", "gemini-2.5-flash-image,gemini-2.0-flash-preview-image-generation").split(",") if m.strip()]:
        try:
            r = requests.post(f"{GEMINI_BASE}/models/{model}:generateContent", headers=headers, timeout=timeout, json={
                "contents": [{"role": "user", "parts": [{"text": f"Generate an image ({aspect} aspect ratio): " + prompt}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}})
            if r.status_code != 200:
                errs.append(f"{model}=HTTP {r.status_code}")
                continue
            for cand in r.json().get("candidates", []):
                for part in (cand.get("content") or {}).get("parts", []):
                    inl = part.get("inlineData") or part.get("inline_data")
                    if inl and inl.get("data"):
                        data = base64.b64decode(inl["data"])
                        if _valid_image(data):
                            return data
            errs.append(f"{model}=no image")
        except Exception as e:
            errs.append(f"{model}={short_reason(e, 60)}")
    raise RuntimeError("Gemini image: " + "; ".join(errs))


def gen_huggingface(prompt):
    if not HF_TOKEN:
        raise RuntimeError("no HF_TOKEN")
    last = None
    for url in ("https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
                "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {HF_TOKEN}", "Accept": "image/png"},
                              json={"inputs": prompt}, timeout=25)
            if r.status_code == 200 and _valid_image(r.content, r.headers.get("Content-Type", "image/png")):
                return r.content
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = short_reason(e, 80)
    raise RuntimeError(f"HuggingFace FLUX.1-schnell: {last}")


def gen_together(prompt):
    if not TOGETHER_API_KEY:
        raise RuntimeError("no TOGETHER_API_KEY")
    r = requests.post("https://api.together.xyz/v1/images/generations",
                      headers={"Authorization": f"Bearer {TOGETHER_API_KEY}", "Content-Type": "application/json"},
                      json={"model": os.environ.get("TOGETHER_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell-Free"),
                            "prompt": prompt, "width": 1024, "height": 1024, "steps": 4, "n": 1,
                            "response_format": "b64_json"}, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Together HTTP {r.status_code}: {short_reason(r.text, 100)}")
    items = r.json().get("data") or []
    if items and items[0].get("b64_json"):
        data = base64.b64decode(items[0]["b64_json"])
        if _valid_image(data):
            return data
    raise RuntimeError("Together: no image")


def image_engines(prompt):
    seed = int(time.time())
    eng = []
    if GEMINI_API_KEY:
        eng.append(("Gemini Imagen (ስሪት 1)", lambda: gen_gemini_image(prompt, aspect="1:1", timeout=25)))
        eng.append(("Gemini Imagen (ስሪት 2)", lambda: gen_gemini_image(prompt + ", clean minimalist composition", aspect="1:1", timeout=25)))
    if HF_TOKEN:
        eng.append(("HuggingFace · FLUX.1-schnell", lambda: gen_huggingface(prompt)))
    if TOGETHER_API_KEY:
        eng.append(("Together · FLUX.1-schnell", lambda: gen_together(prompt)))
    eng.append(("Pollinations · flux", lambda: gen_pollinations(prompt, ["flux"], seed, timeout=20)))
    eng.append(("Pollinations · turbo", lambda: gen_pollinations(prompt, ["turbo"], seed + 1, timeout=20)))
    return eng[:4]


def _tg_safe(data):
    if len(data) < 9_000_000 or not PIL_OK:
        return data
    out = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def run_image_studio(chat_id, request, prompt=None):
    start = time.time()
    ack = send_progress(chat_id, "🎨 ሄኖክ፣ ምስሎቹን በፍጥነት በ Gemini Imagen እና በሌሎች AI ሞተሮች እያዘጋጀሁ ነው (<30 ሰከንድ)...")
    try:
        is_logo = has_any(request, LOGO_WORDS)
        if not prompt:
            with Typing(chat_id, "upload_photo"):
                prompt, is_logo = build_studio_prompt(request)
        engines = image_engines(prompt)
        results, errors = [], []
        with Typing(chat_id, "upload_photo"):
            with ThreadPoolExecutor(max_workers=len(engines)) as ex:
                futs = [(label, ex.submit(fn)) for label, fn in engines]
                for label, fut in futs:
                    try:
                        results.append((label, fut.result(timeout=32)))
                    except Exception as e:
                        errors.append(f"{label}: {short_reason(e, 90)}")
        if not results:
            deliver(chat_id, ack, "ሄኖክ፣ ምስሎቹን አሁን መስራት አልተቻለም 😕 ከ1 ደቂቃ በኋላ ደግመህ ሞክር።\n\n"
                    + "\n".join("• " + e for e in errors[:4]))
            return
        jid = uuid.uuid4().hex[:10]
        with state_lock:
            image_jobs[jid] = {"prompt": prompt, "request": request, "results": results, "ts": time.time()}
        prune(image_jobs, 3)
        drop_message(chat_id, ack)
        media = []
        for i, (label, data) in enumerate(results):
            f = io.BytesIO(_tg_safe(data))
            f.name = f"variation-{i + 1}.jpg"
            media.append(types.InputMediaPhoto(f, caption=f"{NUM_EMOJI[i]} {label}"))
        try:
            if len(media) >= 2:
                bot.send_media_group(chat_id, media)
            else:
                bot.send_photo(chat_id, media[0].media, caption=media[0].caption)
        except Exception:
            for i, (label, data) in enumerate(results):
                f = io.BytesIO(_tg_safe(data))
                f.name = f"variation-{i + 1}.jpg"
                bot.send_photo(chat_id, f, caption=f"{NUM_EMOJI[i]} {label}")
        kb = types.InlineKeyboardMarkup(row_width=5)
        kb.add(*[types.InlineKeyboardButton(NUM_EMOJI[i], callback_data=f"im:{jid}:{i}") for i in range(len(results))])
        kb.add(types.InlineKeyboardButton("🔁 ሌሎች ስሪቶች", callback_data=f"imr:{jid}"))
        note = "\n💡 ሎጎ ላይ የአማርኛ ፊደል በ Canva ላይ ጨምር።" if is_logo else ""
        deliver(chat_id, None, f"✅ {len(results)} ስሪቶች ዝግጁ ናቸው — ምርጡን ቁጥር ንካ 👇{note}\n\n{took(start)}", kb)
    except Exception as e:
        log.error("image studio failed: %s", e)
        deliver(chat_id, ack, f"ሄኖክ፣ ምስሎቹን አሁን ማዘጋጀት አልቻልኩም 😅 ({short_reason(e)})")


def pick_image(chat_id, jid, idx):
    with state_lock:
        job = image_jobs.get(jid)
    if not job or idx >= len(job["results"]):
        bot.send_message(chat_id, "ሄኖክ፣ ይህ ስራ ጊዜው አልፏል — /image ብለህ እንደገና ጠይቀኝ 🙏")
        return
    label, data = job["results"][idx]
    f = io.BytesIO(data)
    f.name = f"sekela-image-{idx + 1}.png" if data[:4] == b"\x89PNG" else f"sekela-image-{idx + 1}.jpg"
    bot.send_document(chat_id, f, caption=f"{NUM_EMOJI[idx]} ተመርጧል — {label}\n(ሙሉ ጥራት ፋይል፤ ለ Canva ዝግጁ)")


def regenerate_image(chat_id, jid):
    with state_lock:
        job = image_jobs.get(jid)
    if not job:
        bot.send_message(chat_id, "ሄኖክ፣ ይህ ስራ ጊዜው አልፏል — /image ብለህ እንደገና ጠይቀኝ 🙏")
        return
    run_image_studio(chat_id, job["request"], prompt=job["prompt"])


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
@bot.message_handler(commands=["sekela", "start", "menu", "help"])
@private_owner_only
def cmd_sekela(message):
    show_sekela_hub(message.chat.id)


@bot.message_handler(commands=["news", "trends"])
@private_owner_only
def cmd_news(message):
    run_news(message.chat.id, "📰 ዋና ዋና ዜናዎች", HUNT_FOCUS["media"], brand="media")


@bot.message_handler(commands=["post"])
@private_owner_only
def cmd_post(message):
    show_sekela_hub(message.chat.id)


@bot.message_handler(commands=["coffeetime", "coffee"])
@private_owner_only
def cmd_coffeetime(message):
    arg = (message.text or "").split(None, 1)
    custom = arg[1].strip() if len(arg) > 1 else None
    run_coffee_auto(message.chat.id, custom_topic=custom)


@bot.message_handler(commands=["bego"])
@private_owner_only
def cmd_bego(message):
    chat_id, uid = message.chat.id, message.from_user.id
    arg = (message.text or "").split(None, 1)
    if len(arg) > 1 and arg[1].strip():
        run_bego_draft(chat_id, arg[1].strip())
        return
    with state_lock:
        awaiting_topic[uid] = "bego"
    bot.send_message(chat_id, "🤍 ሰከላ በጎ — የእርዳታ ወይም የአፋልጉኝ መረጃ (ጽሑፍ፣ ፎቶ ከመግለጫ ጋር ወይም ድምፅ) ላክልኝ፤ "
                              "ወይም ከማህበራዊ ሚዲያ ልፈልግልህ? 👇", reply_markup=sekela_bego_keyboard())


@bot.message_handler(commands=["cancel"])
@private_owner_only
def cmd_cancel(message):
    with state_lock:
        awaiting_topic.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "እሺ ሄኖክ፣ ተሰርዟል ✅", reply_markup=sekela_hub_keyboard())


@bot.message_handler(commands=["analytics", "fb_status"])
@private_owner_only
def cmd_analytics(message):
    show_analytics_menu(message.chat.id)


@bot.message_handler(commands=["youtube"])
@private_owner_only
def cmd_youtube(message):
    chat_id = message.chat.id
    if not yt_enabled():
        bot.send_message(chat_id, "ሄኖክ፣ YouTube አልተዋቀረም — YOUTUBE_CLIENT_ID / SECRET / REFRESH_TOKEN ጨምር።")
    elif len(YT_CHANNELS) > 1:
        bot.send_message(chat_id, "▶️ የትኛውን ቻናል ልየው? 👇", reply_markup=an_youtube_keyboard())
    else:
        bot.send_message(chat_id, "▶️ YouTube — የጊዜ ክልል ምረጥ 👇", reply_markup=an_timeframe_keyboard("yt0"))


@bot.message_handler(commands=["image", "logo"])
@private_owner_only
def cmd_image(message):
    chat_id, uid = message.chat.id, message.from_user.id
    arg = (message.text or "").split(None, 1)
    arg = arg[1].strip() if len(arg) > 1 else ""
    if message.text and message.text.lower().startswith("/logo") and arg and not has_any(arg, LOGO_WORDS):
        arg = "logo: " + arg
    if arg:
        run_image_studio(chat_id, arg)
        return
    with state_lock:
        awaiting_topic[uid] = "image"
    bot.send_message(chat_id, "🎨 እሺ ሄኖክ — ምን ልሥራልህ? የፈለግከውን ምስል ወይም ሎጎ ግለጽልኝ (ለመሰረዝ /cancel)")


@bot.message_handler(commands=["rules", "learn"])
@private_owner_only
def cmd_rules(message):
    chat_id = message.chat.id
    parts = (message.text or "").split(None, 2)
    if len(parts) >= 2 and parts[1].lower() == "add" and len(parts) == 3:
        r = learn_add_rule(parts[2])
        bot.send_message(chat_id, f"✅ ህጉ ተቀምጧል:\n«{r['rule']}»")
        return
    if len(parts) >= 2 and parts[1].lower() == "clear":
        with _learn_lock:
            L = learn_load()
            L["rules"].clear()
            L["boosts"].clear()
        learn_touch(important=True)
        bot.send_message(chat_id, "🗑 ሁሉም ህጎች ተሰርዘዋል።")
        return
    text, kb = learn_rules_message()
    bot.send_message(chat_id, text, reply_markup=kb)


@bot.message_handler(commands=["schedule", "matchday"])
@private_owner_only
def cmd_schedule(message):
    chat_id = message.chat.id
    ack = send_progress(chat_id, "📅 የዛሬና የነገ ጨዋታዎችን እያመጣሁ ነው...")
    try:
        sent = build_matchday(0, force=True)
        sent = build_matchday(1, force=True) or sent
        drop_message(chat_id, ack)
        if not sent:
            bot.send_message(chat_id, "ሄኖክ፣ በተከታተልናቸው ሊጎች ዛሬ/ነገ ጨዋታ አላገኘሁም።")
    except Exception as e:
        deliver(chat_id, ack, f"ሄኖክ፣ ፕሮግራሙን ማምጣት አልተቻለም 😅 ({short_reason(e)})")


@bot.message_handler(commands=["highlight", "clip"])
@private_owner_only
def cmd_highlight(message):
    chat_id, uid = message.chat.id, message.from_user.id
    m = re.search(r"https?://\S+", message.text or "")
    if not m:
        with state_lock:
            awaiting_topic[uid] = "highlight"
        bot.send_message(chat_id, "🎬 እሺ ሄኖክ — የጎል/ሃይላይት ቪዲዮ ሊንክ (≤60 ሰከንድ) ላክልኝ (ለመሰረዝ /cancel)")
        return
    run_highlight(chat_id, m.group(0))


def run_highlight(chat_id, url):
    ack = send_progress(chat_id, "🎬 ቪዲዮውን እያወረድኩ እና የአማርኛ መግለጫ እያዘጋጀሁ ነው...")
    ok, msg = prepare_video_draft(url, chat_id=chat_id)
    if ok:
        drop_message(chat_id, ack)
    else:
        deliver(chat_id, ack, f"ሄኖክ፣ ቪዲዮውን ማዘጋጀት አልተቻለም 😕\n{msg}")


def _send_canva_info(chat_id):
    if not canva_enabled():
        bot.send_message(chat_id, "ሄኖክ፣ CANVA_CLIENT_ID እና CANVA_CLIENT_SECRET በ Render ላይ አልተሞሉም።")
        return
    lines = _canva_status_lines()
    kb = types.InlineKeyboardMarkup()
    try:
        kb.add(types.InlineKeyboardButton("🔐 ከ Canva ጋር ግባ / እንደገና ግባ", url=canva_authorize_url()))
    except Exception as e:
        lines.append(f"⚠️ {short_reason(e)}")
    with _canva_lock:
        _canva_load()
        linked = bool(_canva_tokens.get("refresh"))
    if linked:
        try:
            designs = canva_recent_designs(5)
            if designs:
                lines.append("\n🖼 የቅርብ ጊዜ ዲዛይኖች:")
                lines += [f"• {d['title']}\n  {d['url']}" for d in designs]
        except Exception as e:
            lines.append(f"ℹ️ ዲዛይኖችን ማምጣት አልተቻለም ({short_reason(e, 100)})")
    bot.send_message(chat_id, "\n".join(lines), reply_markup=kb, disable_web_page_preview=True)


@bot.message_handler(commands=["canva"])
@private_owner_only
def cmd_canva(message):
    _send_canva_info(message.chat.id)


@bot.message_handler(commands=["shorts", "clips"])
@private_owner_only
def cmd_shorts(message):
    chat_id, uid = message.chat.id, message.from_user.id
    m = re.search(r"https?://\S+", message.text or "")
    if m:
        url = m.group(0)
        if not is_youtube_url(url):
            bot.send_message(chat_id, "ሄኖክ፣ ለአሁን የ YouTube ሊንክ ብቻ ነው የምቀበለው 🙏")
            return
        run_shorts(chat_id, url=url)
        return
    with state_lock:
        awaiting_topic[uid] = "shorts"
    bot.send_message(chat_id, "እሺ ሄኖክ ✂️ የረጅሙን ቪዲዮ የ YouTube ሊንክ ወይም ፋይል (≤18MB) ላክልኝ (ለመሰረዝ /cancel)")


def _manual_hunt(chat_id, brand, min_importance=5):
    start = time.time()
    what = "ሰበር ዜናዎችን" if brand == "media" else "የስፖርት ዜናዎችን እና ቀጥታ ጎሎችን"
    ack_id = send_progress(chat_id, f"⏳ ሄኖክ፣ {what} እየፈተሽኩ ነው (ከ15-40 ሰከንድ)...")
    status = hunt_once(manual=True, min_importance=min_importance, brand=brand, force_strict=False)
    if status == "sent":
        drop_message(chat_id, ack_id)
        return
    msgs = {
        "none": "ሄኖክ፣ አሁን አዲስ ያልቀረበ ዜና አልተገኘም። ሲገኝ በራሴ አሳውቅሃለሁ 🔔",
        "failed": "ሄኖክ፣ ዜናውን አሁን ማዘጋጀት አልቻልኩም 😅 ጥቂት ቆይተህ እንደገና ሞክር።",
        "busy": "ሄኖክ፣ ፍተሻው አሁን እየተካሄደ ነው — ጥቂት ቆይተህ ሞክር።",
        "nokey": "GEMINI_API_KEY / OPENROUTER_API_KEY አልተሞላም።",
        "cap": "የዛሬው የዜና ገደብ ደርሷል።",
    }
    hint = _ai_error_hint() if status == "failed" else ""
    deliver(chat_id, ack_id, msgs.get(status, msgs["failed"]) + hint + "\n\n" + took(start))


@bot.message_handler(commands=["breaking", "hunt"])
@private_owner_only
def cmd_breaking(message):
    _manual_hunt(message.chat.id, "media")


@bot.message_handler(commands=["sport", "goals"])
@private_owner_only
def cmd_sport(message):
    _manual_hunt(message.chat.id, "sport")


@bot.message_handler(commands=["table", "standings", "tableepl", "tablelaliga", "tableucl"])
@private_owner_only
def cmd_table(message):
    parts = (message.text or "").split()
    cmd0 = (parts[0] if parts else "").lstrip("/").lower()
    arg = parts[1].lower() if len(parts) > 1 else (cmd0[5:] if cmd0.startswith("table") and len(cmd0) > 5 else "epl")
    league = TABLE_ALIASES.get(arg, arg if "." in arg else "eng.1")
    ack = send_progress(message.chat.id, "📊 የደረጃ ሰንጠረዥ እና ነጥቦችን እያመጣሁ ነው...")
    res = format_standings_post(league, top=10)
    if not res:
        deliver(message.chat.id, ack, "ሄኖክ፣ የደረጃ ሰንጠረዡን አሁን ማምጣት አልቻልኩም 😕 (ለምሳሌ: `/table epl`, `/table laliga`, `/table ucl`)")
        return
    post, headline = res
    bg = _fallback_backdrop(brand="sport")
    card = compose_card(bg, headline, badge="ሰከላ ስፖርት", ai=False, brand="sport")
    d = {"post": post, "headline": headline, "image": card or bg, "card": bool(card),
         "sources": [("ESPN", f"https://www.espn.com/soccer/standings/_/league/{league}")],
         "unverified": False, "brand": "sport", "pkg": {}, "facts": post, "bg": bg, "real": False, "is_ai": False}
    present_draft(message.chat.id, "📊 ሰንጠረዥ", d, "📊 የሊግ ደረጃ ሰንጠረዥ ረቂቅ ዝግጁ ነው!")
    drop_message(message.chat.id, ack)


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
                lines.append(f"  ❌ {m} — 404")
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
        r = requests.get(f"{OPENROUTER_URL}/auth/key", headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"}, timeout=20)
        if r.status_code == 200:
            d = r.json().get("data", {})
            limit, usage = d.get("limit"), d.get("usage")
            if limit is not None and usage is not None:
                lines.append(f"✅ OpenRouter ቁልፍ ንቁ — ቀሪ credit ≈ ${float(limit) - float(usage):.2f}")
            else:
                lines.append(f"✅ OpenRouter ቁልፍ ንቁ — ጥቅም ላይ የዋለ ${float(usage or 0):.2f}")
        else:
            lines.append(f"⚠️ OpenRouter ቁልፍ: HTTP {r.status_code}")
    except Exception as e:
        lines.append(f"⚠️ OpenRouter: {short_reason(e, 60)}")
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
            lines.append(f"  ❌ {m} ({role}) — የለም")
    for m in active:
        if m not in OPENROUTER_BASE_MODELS:
            lines.append(f"  ✅ {m} (ተጨማሪ)")
    return lines


def _channel_status_lines(brand):
    b = BRANDS[brand]
    lines = [f"📣 {brand_label(brand)} — Telegram: {b['tg']}"]
    try:
        chat = bot.get_chat(b["tg"])
        lines.append(f"  ✅ ቻናሉ ተገኝቷል: {getattr(chat, 'title', '?')} (id {chat.id})")
        member = bot.get_chat_member(chat.id, bot.get_me().id)
        status = getattr(member, "status", "?")
        can_post = getattr(member, "can_post_messages", None)
        if status == "administrator" and can_post is not False:
            lines.append("  ✅ ቦቱ አድሚን ነው — መለጠፍ ይችላል")
        else:
            lines.append(f"  ❌ ቦቱ አድሚን አይደለም (status: {status})")
    except Exception as e:
        lines.append(f"  ❌ ቻናሉን ማግኘት አልተቻለም: {short_reason(e, 120)}")
    return lines


def _fb_status_lines(brand):
    b = BRANDS[brand]
    lines = [f"📘 {brand_label(brand)} — Facebook Page: {b['fb_name']} (id {b['fb_id']})"]
    if not b["fb_token"]:
        env = "FB_PAGE_ACCESS_TOKEN" if brand == "media" else "FB_SPORT_ACCESS_TOKEN"
        return lines + [f"  ⚪ {env} አልተሞላም"]
    try:
        me = _fb_request("GET", "me", params={"fields": "name,id"}, timeout=30, brand=brand)
        lines.append(f"  ✅ ተገናኝቷል: {me.get('name')} (id {me.get('id')})")
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
        for i, ch in enumerate(YT_CHANNELS):
            tag = "ዋና (Shorts)" if i == 0 else f"ቻናል {i + 1}"
            try:
                st = yt_channel_stats(i)
                lines.append(f"  ✅ {st['title']} — {tag} ({st['subs']:,} subs)")
            except Exception as e:
                lines.append(f"  ❌ {ch['name']} — {tag}: {short_reason(e, 120)}")
    lines.append("  ✅ ffmpeg ዝግጁ" if get_ffmpeg() else "  ❌ ffmpeg የለም")
    return lines


def _engine_status_lines():
    with _learn_lock:
        L = learn_load()
        n_rules, n_ev, n_pend = len(L["rules"]), len(L["events"]), len([p for p in L["pending"] if not p["done"]])
    lines = ["🧠 ራስ-ትምህርት:", f"  ✅ {n_rules} ህጎች · {n_ev} ውሳኔዎች · {n_pend} በመጠባበቅ ላይ", "⚽ Sport Engine:"]
    try:
        ms = refresh_schedule()
        lines.append(f"  ✅ ESPN ተገናኝቷል — {len(ms)} ጨዋታዎች" if ms else "  ⚠️ ESPN ጨዋታ አላመጣም")
    except Exception as e:
        lines.append(f"  ❌ ESPN: {short_reason(e, 100)}")
    return lines


def _image_status_lines():
    return ["🎨 Image Engine (1080x1350):",
            ("  ✅ Gemini Imagen (ዋና)" if GEMINI_API_KEY else "  ⚪ Gemini Imagen — GEMINI_API_KEY የለም"),
            "  ✅ Pollinations flux + turbo (20s fallback)",
            "  ✅ Branded 1080x1350 Dark Backdrop (100% fallback)"]


def send_ai_status(chat_id):
    start = time.time()
    with Typing(chat_id):
        lines = ["🩺 System Status\n━━━━━━━━━━━━", "🧠 ዋና ጸሐፊ — OpenRouter (ከወደቀ Gemini fallback):"]
        lines += _openrouter_status_lines()
        lines.append("\n🔎 ምርምር/ሚዲያ — Gemini:")
        lines += _gemini_status_lines()
        lines.append("")
        lines += _channel_status_lines("media")
        lines.append("")
        lines += _channel_status_lines("sport")
        lines.append("")
        lines += _fb_status_lines("media")
        lines.append("")
        lines += _fb_status_lines("sport")
        lines.append("")
        lines += _yt_status_lines()
        lines.append("")
        lines += _canva_status_lines()
        lines.append("")
        lines += _image_status_lines()
        lines.append("")
        lines += _engine_status_lines()
        font = find_ethiopic_font() if PIL_OK else None
        lines.append("\n🖼 Ethiopic font: " + ("✅ ተገኝቷል (1080x1350)" if font else "❌ የለም"))
        lines.append(f"👤 Owner ID: {OWNER_ID}")
        lines.append(f"🕒 አዲስ አበባ ሰዓት: {addis_now().strftime('%Y-%m-%d %H:%M')}")
        lines.append("\n" + took(start))
    deliver(chat_id, None, "\n".join(lines))


@bot.message_handler(commands=["ai_status"])
@private_owner_only
def cmd_ai_status(message):
    send_ai_status(message.chat.id)


# ----------------------------------------------------------------------------
# Callbacks (Unified Hub + Categories + Approval Gates)
# ----------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    msg = getattr(call, "message", None)
    if msg is None or not is_private(msg.chat):
        return
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
        # ---- Unified /sekela hub callbacks -------------------------------------------------
        if data == "hub:main":
            show_sekela_hub(chat_id, edit_id=msg_id)

        elif data == "hub:media":
            bot.edit_message_text("📰 ሰከላ ሚዲያ — ምን ላዘጋጅልህ? 👇", chat_id, msg_id, reply_markup=sekela_media_keyboard())

        elif data == "hub:media:break":
            _manual_hunt(chat_id, "media", min_importance=6)

        elif data == "hub:media:news":
            run_news(chat_id, "📰 ዋና ዜናዎች", HUNT_FOCUS["media"], brand="media")

        elif data == "hub:media:fast":
            run_fast_post(chat_id, brand="media")

        elif data == "hub:media:custom":
            with state_lock:
                awaiting_topic[uid] = "post_media"
            bot.send_message(chat_id, "✍️ እሺ ሄኖክ — ለሰከላ ሚዲያ የሚሆን ርዕስ ጻፍልኝ (ለመሰረዝ /cancel):")

        elif data == "hub:sport":
            bot.edit_message_text("⚽ ሰከላ ስፖርት — ምን ላዘጋጅልህ? 👇", chat_id, msg_id, reply_markup=sekela_sport_keyboard())

        elif data == "hub:sport:break":
            _manual_hunt(chat_id, "sport", min_importance=5)

        elif data == "hub:sport:sched":
            ack = send_progress(chat_id, "📅 የዛሬና የነገ ጨዋታዎችን እያመጣሁ ነው...")
            sent = build_matchday(0, force=True)
            sent = build_matchday(1, force=True) or sent
            drop_message(chat_id, ack)
            if not sent:
                bot.send_message(chat_id, "ሄኖክ፣ በተከታተልናቸው ሊጎች ዛሬ/ነገ ጨዋታ አላገኘሁም።")

        elif data == "hub:sport:fast":
            run_fast_post(chat_id, brand="sport")

        elif data == "hub:sport:custom":
            with state_lock:
                awaiting_topic[uid] = "post_sport"
            bot.send_message(chat_id, "✍️ እሺ ሄኖክ — ለሰከላ ስፖርት የሚሆን ርዕስ ጻፍልኝ (ለመሰረዝ /cancel):")

        elif data == "hub:coffee":
            run_coffee_auto(chat_id)

        elif data == "hub:bego":
            with state_lock:
                awaiting_topic[uid] = "bego"
            bot.send_message(chat_id, "🤍 ሰከላ በጎ (እርዳታ እና አፋልጉኝ)\n"
                                      "መረጃውን በጽሑፍ፣ በፎቶ (ከመግለጫ ጋር) ወይም በድምፅ ላክልኝ (ስም፣ ቦታ፣ ስልክ/ባንክ ካለ) — "
                                      "ወይም ከታች ባለው ቁልፍ ከማህበራዊ ሚዲያ ልፈልግልህ? 👇",
                             reply_markup=sekela_bego_keyboard())

        elif data == "hub:bego:search":
            with state_lock:
                awaiting_topic.pop(uid, None)
            run_bego_search(chat_id)

        elif data == "hub:an":
            show_analytics_menu(chat_id, edit_id=msg_id)

        elif data == "hub:img":
            bot.edit_message_text("🎨 Image & Canva Studio — ምን ልሥራልህ? 👇", chat_id, msg_id,
                                  reply_markup=sekela_image_keyboard())

        elif data == "hub:img:ai":
            with state_lock:
                awaiting_topic[uid] = "image"
            bot.send_message(chat_id, "🎨 እሺ ሄኖክ — የምትፈልገውን ምስል ወይም ሎጎ ግለጽልኝ (ለመሰረዝ /cancel)")

        elif data == "hub:img:canva":
            _send_canva_info(chat_id)

        elif data == "hub:rules":
            text, kb = learn_rules_message()
            bot.send_message(chat_id, text, reply_markup=kb)

        elif data == "hub:status":
            send_ai_status(chat_id)

        # ---- Legacy category & story callbacks ---------------------------------------------
        elif data.startswith("cat:"):
            _, mode, key = data.split(":", 2)
            if key == "custom":
                with state_lock:
                    awaiting_topic[uid] = mode
                bot.edit_message_text(f"✅ የመረጥከው: {CUSTOM_LABEL}", chat_id, msg_id)
                bot.send_message(chat_id, "እሺ ሄኖክ ✍️ ርዕሱን ጻፍልኝ (ለመሰረዝ /cancel):")
                return
            if key == "bego":
                with state_lock:
                    awaiting_topic[uid] = "bego"
                bot.edit_message_text("✅ የመረጥከው: 🤍 ሰከላ በጎ", chat_id, msg_id)
                bot.send_message(chat_id, "🤍 መረጃውን (ጽሑፍ/ፎቶ/ድምፅ) ላክልኝ ወይም ከማህበራዊ ሚዲያ ልፈልግ? 👇",
                                 reply_markup=sekela_bego_keyboard())
                return
            label, topic = CATEGORIES[key]
            brand = "sport" if key == "sport" else "media"
            segment = "coffee" if key == "coffee" else None
            bot.edit_message_text(f"✅ የመረጥከው: {label}", chat_id, msg_id)
            if mode == "news":
                run_news(chat_id, label, topic, brand=brand, segment=segment)
            else:
                if segment == "coffee":
                    run_coffee_auto(chat_id)
                else:
                    run_post(chat_id, label, topic, brand=brand, segment=segment)

        elif data.startswith("mk:"):
            nid = data.split(":", 1)[1]
            with state_lock:
                item = news_cache.get(nid)
            if not item:
                bot.send_message(chat_id, "ሄኖክ፣ ይህ መረጃ ጊዜው አልፏል — /sekela ብለህ እንደገና ጠይቀኝ 🙏")
                return
            run_post(chat_id, item["label"], item["topic"], brand=item.get("brand"), segment=item.get("segment"))

        elif data.startswith("st:"):
            _, nid, idx = data.split(":", 2)
            with state_lock:
                item = news_cache.get(nid)
            if not item or not item["stories"] or int(idx) >= len(item["stories"]):
                bot.send_message(chat_id, "ሄኖክ፣ ይህ መረጃ ጊዜው አልፏል — /sekela ብለህ እንደገና ጠይቀኝ 🙏")
                return
            s = item["stories"][int(idx)]
            run_post(chat_id, item["label"], f"{s['title']}. {s['summary']}", brand=item.get("brand"),
                     photo_urls=[s["link"]] if s.get("link") else None, story_image=s.get("image"),
                     segment=item.get("segment"))

        elif data.startswith("ok:"):
            did = data.split(":", 1)[1]
            with state_lock:
                d = drafts.get(did)
            if d is not None and d.get("unverified") and not d.get("confirmed"):
                bot.send_message(chat_id, "⚠️ ይህ ረቂቅ ያልተረጋገጠ ነው። በእርግጥ ማጽደቅ ትፈልጋለህ?")
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("⚠️ አዎ፣ ፖስት አድርግ", callback_data=f"okc:{did}"))
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
                dd = drafts.pop(did, None)
            if dd:
                learn_event("cancel", dd)
            bot.edit_message_text("❌ ተሰርዟል — ምንም አልተለጠፈም።", chat_id, msg_id)

        elif data == "noop":
            return

        elif data.startswith("vk:"):
            approve_video(chat_id, msg_id, data.split(":", 1)[1])

        elif data.startswith("vn:"):
            with state_lock:
                vd = video_drafts.pop(data.split(":", 1)[1], None)
            if vd:
                learn_event("cancel", vd)
                shutil.rmtree(os.path.dirname(vd["path"]), ignore_errors=True)
            try:
                bot.edit_message_caption("❌ ተሰርዟል — ምንም አልተለጠፈም።", chat_id, msg_id)
            except Exception:
                bot.send_message(chat_id, "❌ ተሰርዟል — ምንም አልተለጠፈም።")

        elif data.startswith("rl:"):
            ok = learn_delete_rule(data.split(":", 1)[1])
            text, kb = learn_rules_message()
            try:
                bot.edit_message_text(("🗑 ህጉ ተሰርዟል\n\n" if ok else "") + text, chat_id, msg_id, reply_markup=kb)
            except Exception:
                bot.send_message(chat_id, text, reply_markup=kb)

        elif data == "an:m":
            show_analytics_menu(chat_id, msg_id)

        elif data.startswith("an:p:"):
            platform = data.split(":", 2)[2]
            if platform == "yt":
                if not yt_enabled():
                    bot.edit_message_text("ሄኖክ፣ YouTube አልተዋቀረም — YOUTUBE_CLIENT_ID / SECRET / REFRESH_TOKEN ጨምር።", chat_id, msg_id)
                elif len(YT_CHANNELS) > 1:
                    bot.edit_message_text("▶️ YouTube — የትኛውን ቻናል ልየው? 👇", chat_id, msg_id, reply_markup=an_youtube_keyboard())
                else:
                    bot.edit_message_text("▶️ YouTube — የጊዜ ክልል ምረጥ 👇", chat_id, msg_id, reply_markup=an_timeframe_keyboard("yt0"))
            else:
                bot.edit_message_text(f"{platform_label(platform)} — የጊዜ ክልል ምረጥ 👇", chat_id, msg_id,
                                      reply_markup=an_timeframe_keyboard(platform))

        elif data.startswith("an:y:"):
            platform = "yta" if data.endswith(":a") else "yt" + data.split(":", 2)[2]
            bot.edit_message_text(f"{platform_label(platform)} — የጊዜ ክልል ምረጥ 👇", chat_id, msg_id,
                                  reply_markup=an_timeframe_keyboard(platform))

        elif data.startswith("an:t:"):
            _, _, platform, tf = data.split(":", 3)
            if tf in TIMEFRAMES:
                run_analytics(chat_id, platform, tf, msg_id)

        elif data.startswith("im:"):
            _, jid, idx = data.split(":", 2)
            pick_image(chat_id, jid, int(idx))

        elif data.startswith("imr:"):
            regenerate_image(chat_id, data.split(":", 1)[1])

        elif data.startswith("sw:"):
            rebrand_draft(chat_id, msg_id, data.split(":", 1)[1])

        elif data.startswith("pin:"):
            pin_published(chat_id, data.split(":", 1)[1])

        elif data.startswith("ed:"):
            pid = data.split(":", 1)[1]
            with state_lock:
                known = pid in published
                if known:
                    awaiting_topic[uid] = f"edit:{pid}"
            if known:
                bot.send_message(chat_id, "✏️ እሺ ሄኖክ፣ የፖስቱን አዲስ ሙሉ ጽሑፍ ላክልኝ (ለመሰረዝ /cancel)።")
            else:
                bot.send_message(chat_id, "ሄኖክ፣ ይህ ፖስት ከማህደረ ትውስታ ተሰርዟል 🙏")

        elif data.startswith("vc:"):
            vid = data.split(":", 1)[1]
            with state_lock:
                item = video_cache.pop(vid, None)
            if not item:
                bot.send_message(chat_id, "ሄኖክ፣ ቪዲዮው ከማህደረ ትውስታ ተሰርዟል 🙏")
                return
            try:
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
            except Exception:
                pass
            run_shorts(chat_id, data=item["data"])

        elif data.startswith("yk:"):
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

    with state_lock:
        pending_mode = awaiting_topic.pop(uid, None)
    if pending_mode and pending_mode.startswith("edit:"):
        edit_published(chat_id, pending_mode.split(":", 1)[1], text)
        return
    if pending_mode == "bego":
        run_bego_draft(chat_id, text)
        return
    if pending_mode == "post_media":
        run_post(chat_id, f"✍️ {text[:40]}", text, brand="media")
        return
    if pending_mode == "post_sport":
        run_post(chat_id, f"⚽ {text[:40]}", text, brand="sport")
        return
    if pending_mode == "image":
        run_image_studio(chat_id, text)
        return
    if pending_mode == "highlight":
        mm = re.search(r"https?://\S+", text)
        if mm:
            run_highlight(chat_id, mm.group(0))
        else:
            bot.send_message(chat_id, "ሄኖክ፣ ሊንክ (https://...) ላክልኝ ወይም /cancel።")
            with state_lock:
                awaiting_topic[uid] = "highlight"
        return
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

    if is_greeting(text):
        bot.send_message(chat_id, greeting_reply(), reply_markup=sekela_hub_keyboard())
        return
    if is_thanks(text):
        bot.send_message(chat_id, "ምንም አይደለም ሄኖክ! 🙌 ሌላ ነገር ካለ ንገረኝ።")
        return

    low_t = text.strip().lower()
    if any(low_t.startswith(t.lower()) for t in RULE_TRIGGERS):
        with Typing(chat_id):
            r = learn_add_rule(re.sub(r"^(ህግ|ሕግ|rule)\s*[:፦]\s*", "", text.strip(), flags=re.I))
        extra = ""
        if r.get("topic") and r.get("direction"):
            extra = f"\n📊 ርዕስ «{TOPIC_AM.get(r['topic'], r['topic'])}» " + ("ይጨመራል" if r["direction"] == "more" else "ይቀነሳል")
        if r.get("team"):
            extra += f"\n⚽ ክለብ «{r['team']}» ወደ ክትትል ዝርዝር ተጨምሯል"
        bot.send_message(chat_id, f"✅ ህጉ ለዘላለም ተቀምጧል!\n«{r['rule']}»{extra}\n\n/rules ብለህ ማየት ትችላለህ።")
        return

    if has_any(text, IMAGE_TRIGGERS):
        run_image_studio(chat_id, text)
        return

    if has_any(text, COFFEE_WORDS):
        custom = None if len(text.split()) <= 4 else text
        run_coffee_auto(chat_id, custom_topic=custom)
        return

    if has_any(text, BEGO_WORDS):
        if len(text.split()) <= 4:
            with state_lock:
                awaiting_topic[uid] = "bego"
            bot.send_message(chat_id, "🤍 ሰከላ በጎ — መረጃውን (ጽሑፍ/ፎቶ/ድምፅ) ላክልኝ ወይም ከማህበራዊ ሚዲያ ልፈልግ? 👇",
                             reply_markup=sekela_bego_keyboard())
        else:
            run_bego_draft(chat_id, text)
        return

    mode = "post" if has_any(text, POST_WORDS) else ("news" if has_any(text, NEWS_WORDS) else None)
    if mode:
        if len(text.split()) <= 5:
            show_sekela_hub(chat_id)
        else:
            (run_news if mode == "news" else run_post)(chat_id, f"✍️ {text[:40]}", text)
        return

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
    log.info("Starting Henok Assistant agent... Media: %s (FB %s) | Sport: %s (FB %s) | YouTube: %s",
             TARGET_CHANNEL, "on" if fb_enabled("media") else "off", SPORT_CHANNEL,
             "on" if fb_enabled("sport") else "off", "on" if yt_enabled() else "off")

    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is missing.")
        time.sleep(5)
        sys.exit(1)
    if not OPENROUTER_API_KEY:
        log.warning("OPENROUTER_API_KEY missing - Gemini will write everything.")
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY missing - live research and media analysis are disabled.")
    if not PIL_OK:
        log.warning("Pillow missing - cards will be sent without the baked headline.")

    try:
        apihelper.READ_TIMEOUT = 90
        apihelper.CONNECT_TIMEOUT = 15
    except Exception:
        pass
    if PIL_OK:
        threading.Thread(target=find_ethiopic_font, daemon=True).start()
    if OPENROUTER_API_KEY:
        threading.Thread(target=get_openrouter_models, daemon=True).start()
    start_learning()
    start_hunter()
    start_sport_engine()
    start_alerts()

    try:
        bot.remove_webhook()
    except Exception as e:
        log.warning("remove_webhook failed: %s", e)
    try:
        bot.set_my_commands([
            types.BotCommand("sekela", "ዋና የቁጥጥር ማዕከል (ሚዲያ / ስፖርት / ኮፊ ታይም / በጎ)"),
            types.BotCommand("post", "ፈጣን ካርድ + ፖስት ማዕከል"),
            types.BotCommand("coffeetime", "☕ ሰከላ ኮፊ ታይም — ቫይራል ታሪክ"),
            types.BotCommand("bego", "🤍 ሰከላ በጎ — እርዳታ እና አፋልጉኝ"),
            types.BotCommand("analytics", "📊 የቻናል እና ገጽ ዳሽቦርድ"),
            types.BotCommand("image", "🎨 AI ምስል እና ሎጎ"),
            types.BotCommand("ai_status", "🩺 የሲስተም ሁኔታ"),
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
