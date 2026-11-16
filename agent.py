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
           FB_SPORT_PAGE_ID      (Sekela Sport page, default 499790239874374)
           FB_SPORT_ACCESS_TOKEN (Sekela Sport Page access token)
YouTube  : YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
           MORE CHANNELS (Brand accounts): YOUTUBE_REFRESH_TOKEN_1, _2, ... (+ optional YOUTUBE_NAME_1, _2 ...)
           or YOUTUBE_TOKENS_JSON='[{"name":"Henok Travel","refresh_token":"..."},{"name":"X","refresh_token":"..."}]'
Canva    : CANVA_CLIENT_ID, CANVA_CLIENT_SECRET (callback = <RENDER_EXTERNAL_URL>/canva/callback or CANVA_REDIRECT_URI;
           CANVA_SCOPES default "profile:read design:meta:read")
Images   : /image uses Pollinations (flux + turbo, no key) + Gemini image models (GEMINI_API_KEY) and, when present,
           HF_TOKEN (Hugging Face FLUX.1-schnell) and TOGETHER_API_KEY (FLUX.1-schnell-Free)
Learning : LEARN_FILE (default agent_learning.json next to agent.py; a pinned Telegram backup restores it after restarts)
Sport    : SPORT_ENGINE=1, SPORT_LEAGUES=eng.1,uefa.champions,..., SPORT_POLL_SEC=75, SPORT_ALERT_ALL=0, SPORT_ALERT_MAX_PER_DAY=30,
           SPORT_HIGHLIGHTS=1, SPORT_HIGHLIGHT_CHANNELS='Name|YouTubeChannelId,...', SPORT_CLIP_MAX_SEC=60
News AI  : NEWS_WRITER_MODELS (default anthropic/claude-haiku-5.5,openai/gpt-4o-mini,deepseek/deepseek-chat),
           NEWS_ALLOW_GEMINI=1 (OpenRouter writes first; automatic Gemini fallback if OpenRouter fails), NEWS_PROOFREAD=1
           YT_PRIVACY            (public | unlisted | private; default public - upload happens ONLY after approval tap)
Optional : OWNER_ID              (default 511440586)
           TARGET_CHANNEL        (ሰከላ ሚዲያ channel, default @sekelamediaET)
           SPORT_CHANNEL         (ሰከላ ስፖርት channel, default @SekelaSportET)
           SEEN_FILE             (48h story-dedup JSON file, default in the temp dir)
           HUNT_SPORT_INTERVAL_MIN (default 40), HUNT_SPORT_MAX_PER_DAY (default 16), HUNTER_SPORT_ENABLED=1
           SPORT_RSS_FEEDS="Name|url,Name|url"
           OPENROUTER_MODELS     (comma list; default deepseek/deepseek-chat,openai/gpt-4o-mini)
           OPENROUTER_AUTO_EXTRA (default 1: auto-add a verified Claude Haiku / Grok mini slug taken from OpenRouter's
                                 live /api/v1/models list as extra fallback; 0 disables)
           FONT_PATH, PORT (default 10000), RENDER_EXTERNAL_URL
           HUNTER_ENABLED=1, HUNT_INTERVAL_MIN=30, HUNT_MIN_IMPORTANCE=5, HUNT_MAX_PER_DAY=24, HUNT_QUIET=23-6,
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
- Writing (chat, news, posts, audio summaries, polishing): OpenRouter first (DeepSeek -> GPT-4o-mini -> verified
  Claude/Grok slug if available), Gemini as automatic fallback when OpenRouter is unreachable / out of credits.
- Research: Gemini + Google Search grounding across Ethiopian/global newsrooms, TikTok, YouTube, X, Facebook public
  pages, Google Trends RSS, and independent Ethiopian blogs (strict <=24h freshness filter).
- Cards: 1080x1350 (4:5 portrait) seamless-gradient cards (Two-Tone Ultra-Bold Headline, 3-4-3 Sport result cards with
  real match photos & goal scorers, and Coffee-Gold Sekela Coffee Time viral cards).
- Breaking-news hunter: scans 12h (automatic 24h fallback, then RSS fallback) with importance >= 5 and pushes a draft
  to the owner's DM with approve / cancel buttons. Nothing is posted without the approval tap.
- YouTube: channel analytics (Data API), long video -> 9:16 Shorts clipper (Gemini picks segments, ffmpeg cuts),
  upload ONLY after the owner taps approve.
- /analytics: branded PNG dashboard (Telegram + YouTube + Facebook) + proactive alerts on notable changes.
- STRICT NO-COMMENTING POLICY: the Facebook layer can only POST to /photos and /feed and READ page info, insights and
  post counters. Any path containing "comment" is refused in code (see _fb_request).
- The bot talks ONLY in a private chat with the owner; in groups/channels it is silent.
"""

import base64
import copy
import functools
import glob
import hashlib
import io
import html as html_lib
import json
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
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import requests
import telebot
from telebot import apihelper, types
from telebot.apihelper import ApiTelegramException

try:  # Pillow is optional: without it the bot still works, just without the composed headline card
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat
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
    """'@name' / 'name' / 't.me/name' -> '@name'; numeric ids -> int."""
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


# Two publishing pipelines. Every draft is routed to ONE brand: Telegram channel + Facebook Page (own token).
BRANDS = {
    "media": {
        "key": "media", "emoji": "📰", "name_am": "ሰከላ ሚዲያ", "name_en": "Sekela Media",
        "badge": "ሰከላ ሚዲያ | Sekela Media",
        "tg": TARGET_CHANNEL, "tg_link": _tg_link(TARGET_CHANNEL, "https://t.me/sekelamediaET"),
        "tg_handle": str(TARGET_CHANNEL) if str(TARGET_CHANNEL).startswith("@") else "@sekelamediaET",
        "fb_id": FB_PAGE_ID, "fb_token": FB_PAGE_ACCESS_TOKEN, "fb_name": FB_PAGE_NAME,
        "fb_link": f"https://www.facebook.com/{FB_PAGE_ID}",
        "tags": ["#ሰከላ_ሚዲያ", "#SekelaMedia", "#ሰከላ_ኒውስ", "#SekelaNews"],
        "brief_note": "This is a general news story (Ethiopia, Africa, world, economy, tech/AI, culture, trends) "
                      "for ሰከላ ሚዲያ.",
    },
    "sport": {
        "key": "sport", "emoji": "⚽", "name_am": "ሰከላ ስፖርት", "name_en": "Sekela Sport",
        "badge": "ሰከላ ስፖርት | Sekela Sport",
        "tg": SPORT_CHANNEL, "tg_link": _tg_link(SPORT_CHANNEL, "https://t.me/SekelaSportET"),
        "tg_handle": str(SPORT_CHANNEL) if str(SPORT_CHANNEL).startswith("@") else "@SekelaSportET",
        "fb_id": FB_SPORT_PAGE_ID, "fb_token": FB_SPORT_ACCESS_TOKEN, "fb_name": "ሰከላ ስፖርት",
        "fb_link": f"https://www.facebook.com/{FB_SPORT_PAGE_ID}",
        "tags": ["#ሰከላ_ስፖርት", "#SekelaSport", "#ሰከላ_ሚዲያ", "#SekelaMedia"],
        "brief_note": "This is a SPORT story for ሰከላ ስፖርት: use football/sport vocabulary. For a goal give the scorer, "
                      "the minute and the scoreline; for a transfer give the player, the clubs and the fee ONLY if they "
                      "are in the notes.",
    },
}
COFFEE_BADGE = "ሰከላ ኮፊ ታይም | Sekela Coffee Time"
COFFEE_TAGS = ["#ሰከላ_ኮፊ_ታይም", "#SekelaCoffeeTime", "#ሰከላ_ሚዲያ", "#SekelaMedia"]
ALL_BRAND_TAGS = {t.lower() for b in BRANDS.values() for t in b["tags"]} | {t.lower() for t in COFFEE_TAGS}


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
    return ("🔗 ፈጣን እና ታማኝ መረጃ ለማግኘት ይቀላቀሉን:\n"
            f"Telegram: {b['tg_link']}\nFacebook: {b['fb_link']}")


# Base models are only the two that /ai_status confirmed. The Claude / Grok slugs are NOT hardcoded any more:
# get_openrouter_models() looks them up in OpenRouter's live model list so a wrong slug can never show a red X.
OPENROUTER_BASE_MODELS = [m.strip() for m in os.environ.get(
    "OPENROUTER_MODELS", "anthropic/claude-haiku-5.5,openai/gpt-4o-mini,deepseek/deepseek-chat").split(",")
    if m.strip()]
# OpenRouter is always tried first; Gemini serves as automatic fallback when OpenRouter is unavailable/returns None.
NEWS_WRITER_MODELS = [m.strip() for m in os.environ.get(
    "NEWS_WRITER_MODELS", "anthropic/claude-haiku-5.5,openai/gpt-4o-mini,deepseek/deepseek-chat").split(",")
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
CAPTION_LIMIT = 1024  # Telegram photo-caption hard limit

AI_LABEL = True  # small "AI illustration" tag on cards (the background is generated, not a real photo)

BUSY_TEXT = ("ሄኖክ፣ አሁን መስመሮቹ ለአፍታ ተጨናንቀዋል 😅 መልእክትህ አልጠፋም — "
             "ከ1 ደቂቃ በኋላ ደግመህ ላክልኝ፣ ወዲያውኑ እመልሳለሁ።")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("henok-agent")


# ----------------------------------------------------------------------------
# Health server + self keep-alive (Render free tier / cron-job.org)
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
        if parsed.path.rstrip("/") == "/canva/callback":  # Canva Connect OAuth redirect target
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
awaiting_topic = {}   # user_id -> "news" | "post" | "shorts" | ...
drafts = {}           # draft_id -> draft dict (see present_draft)
news_cache = {}       # news_id -> {"label","text","sources","ts"}
video_cache = {}      # video_id -> {"data": bytes, "ts": float}  (for the Shorts clipper button)
shorts_drafts = {}    # short_id -> {"path","title","description","tags","busy","ts"}
published = {}        # pub_id -> channel post info (for the Pin / Edit buttons)
bot_stats = {"drafts": 0, "tg_posts": 0, "fb_posts": 0, "yt_uploads": 0, "breaking": 0}

CATEGORIES = {
    "ai": ("🤖 AI እና ቴክኖሎጂ", "AI, Technology, Meta/Facebook/TikTok platform updates & digital tools"),
    "crypto": ("🪙 Crypto እና ገበያ", "Crypto, Bitcoin & Markets"),
    "biz": ("💼 ኢኮኖሚና ቢዝነስ", "Ethiopia Economy, Business, Birr, Jobs & Opportunities"),
    "creator": ("🎬 ፈጣሪዎችና ሶሻል ሚዲያ", "Ethiopian Creators, TikTok, YouTube, Video Creation & Social Media trends"),
    "sport": ("⚽ ስፖርት (እግር ኳስ)", "Football and sports: Ethiopian Premier League, English Premier League, Champions League, European leagues, transfers (ዝውውሮች), Olympics, live goals"),
    "coffee": ("☕ ሰከላ ኮፊ ታይም", "Surprising, funny, heartwarming, or mind-blowing viral human-interest and weird-but-true stories from Ethiopia and around the world"),
}
CUSTOM_LABEL = "✍️ የራሴን ርዕስ እጽፋለሁ"

SPORT_WORDS = ["ስፖርት", "እግር ኳስ", "ኳስ", "ጎል", "ዝውውር", "ፕሪሚየር ሊግ", "ሻምፒየንስ ሊግ", "ኦሎምፒክ", "ቅዱስ ጊዮርጊስ",
               "football", "soccer", "premier league", "champions league", "la liga", "serie a", "bundesliga",
               "goal", "goals", "transfer", "arsenal", "chelsea", "liverpool", "man utd", "manchester united",
               "manchester city", "tottenham", "barcelona", "real madrid", "olympic", "olympics", "match", "fixture"]
COFFEE_WORDS = ["ኮፊ ታይም", "coffee time", "coffeetime", "ሰከላ ኮፊ"]
NEWS_WORDS = ["ዜና", "ትሬንድ", "ወቅታዊ", "አዲስ ነገር", "news", "trend", "trends", "latest", "trending"]
POST_WORDS = ["ፖስት", "ካፕሽን", "ለቻናል", "ለማህበረሰብ", "post", "caption"]
REALTIME_WORDS = ["ዛሬ", "አሁን", "ዋጋ", "ምንዛሬ", "ዜና", "ትሬንድ", "ወቅታዊ", "ቅርብ ጊዜ",
                  "today", "now", "price", "latest", "news", "trend", "current", "bitcoin", "btc",
                  "usd", "rate", "2026"]

GREET_FIRST_WORDS = {"hi", "hello", "hey", "selam", "salam", "ሰላም", "ታዲያስ", "ጤና"}
GREET_PHRASES = {"good morning", "good afternoon", "good evening", "እንዴት ነህ", "እንደምን ነህ",
                 "ጤና ይስጥልኝ", "ሰላም ነው", "እንዴት ነው", "ደህና ነህ", "እንዴት አለህ"}
THANKS_PHRASES = {"thanks", "thank you", "thx", "አመሰግናለሁ", "እናመሰግናለን", "ok", "okay", "እሺ", "ጥሩ"}

# Top-tier + Ethiopian independent/social outlets recognised for the mandatory "📌 ምንጭ፡" line
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
        "BRAND CONTEXT: media brand 'ሰከላ ሚዲያ (Sekela Media / Sekela News)' on Facebook (ሰከላ Sekela page) and Telegram (@sekelamediaET), including its viral human-interest segment '☕ ሰከላ ኮፊ ታይም (Sekela Coffee Time)'; sports brand 'ሰከላ ስፖርት (Sekela Sport)' on Facebook and Telegram (@SekelaSportET); YouTube "
        "'Henok Travel'; "
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
    return g + "\nዛሬ ምን ላግዝህ? 📰 /news · 📝 /post · ☕ /coffeetime · 📊 /analytics · ✂️ /shorts ወይም ማንኛውንም ነገር ጻፍልኝ።"


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
    The keyboard, if any, goes on the last chunk. Returns the id of the last message used."""
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
    extra = [c.get("refresh", "") for c in globals().get("YT_CHANNELS", [])]
    for secret in [FB_PAGE_ACCESS_TOKEN, GEMINI_API_KEY, OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, YT_CLIENT_SECRET,
                   YT_REFRESH_TOKEN, FB_SPORT_ACCESS_TOKEN, CANVA_CLIENT_SECRET, HF_TOKEN, TOGETHER_API_KEY] + extra:
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


_gm_lock = threading.Lock()
_gm_cache = {"ts": 0.0, "models": None}
_gm_bad = set()   # models that answered HTTP 404 for this key (skipped from then on)
# Priority order: gemini-2.0-flash -> gemini-flash-latest -> (2.5 flash) -> gemini-2.5-pro -> gemini-pro-latest.
# "omni", image, tts, live, audio-native, preview, lite... models are NEVER used (they broke voice notes).
GEMINI_PREFERRED = ["gemini-2.0-flash", "gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-pro", "gemini-pro-latest"]
_GEMINI_SKIP = re.compile(r"omni|image|imagen|tts|live|audio|embed|thinking|exp|preview|lite|8b|robotics|computer|"
                          r"learnlm|gemma|aqa|veo", re.I)


def gemini_active_models(force=False):
    """Active generateContent models for this API key (GET /models, cached 6h), in the priority order above."""
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
                log.warning("Gemini model list HTTP %s: %s", r.status_code, r.text[:120])
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
        log.info("Gemini active models: %s", models)
    else:
        models = list(GEMINI_PREFERRED)
    with _gm_lock:
        _gm_cache.update(ts=now if names else now - 21000, models=list(models))
    return [m for m in models if m not in _gm_bad] or models


get_gemini_models = gemini_active_models  # backwards-compatible name


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
            return text, sources, model
        except Exception as e:
            last_err = e
            log.warning("Gemini model failed: %s", e)
            if "HTTP 404" in str(e):
                _gm_bad.add(model)
    raise GeminiError(f"all Gemini models failed: {last_err}")


RESEARCH_PROMPT = (
    "Current Addis Ababa date/time: {now_eat} (UTC: {now_utc}).\n"
    "Topic: {topic}\n\n"
    "Search the web NOW across major outlets AND social platforms (TikTok, YouTube, X/Twitter, public Facebook pages, "
    "Google Trends, and independent/smaller Ethiopian media & Addis Ababa blogs like Addis Standard, Borkena, Capital "
    "Ethiopia, Addis Fortune, Sheger FM, Tikvah, Gursha, Behaja). Return concise ENGLISH research notes (max 380 words) "
    "for a newsroom: the verified facts (who, what, when, where), concrete numbers, background and any OFFICIAL "
    "statements or direct quotes with the speaker's name/title, public reactions, and next steps.\n"
    "FRESHNESS RULE: Prioritize facts published within the last 24 hours (prefer 2-12 hours). Verify dates against "
    "current time and ignore 2-3 day old stories unless directly required as background.\n"
    "Bullet points only; after EACH fact put the outlet/platform in brackets, e.g. [Reuters], [Tikvah], [X: @FanaBC], "
    "[TikTok], [YouTube], [Addis Standard]. Do not write Amharic. Do not invent anything."
)
RESEARCH_LITE_PROMPT = (
    "Current time: {now_utc} UTC. What are the latest verified facts (within the last 24 hours) about: {topic}? "
    "Check news sites, X, YouTube, TikTok and Ethiopian outlets. Answer in English bullet points (max 250 words), put "
    "the outlet/platform in brackets after each fact. No Amharic. Do not invent anything."
)


def research(topic, lite=False):
    """Live web facts via Gemini + Google Search. Returns (English notes, sources); ("", []) on failure."""
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
    """Audio/video/image analysis. Every ACTIVE model (404 models are skipped) is tried with three variants before the
    next model: (thinkingConfig + max_tokens) -> (no thinkingConfig + max_tokens) -> (no thinkingConfig + 8192)."""
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
                log.warning("Multimodal %s (thinking=%s, max=%s) failed with HTTP %s: %s", model, tb, mt, code, e)
                if "HTTP 404" in str(e):
                    _gm_bad.add(model)
                    break  # this model does not exist for the key: next model
    raise GeminiError(f"all models failed for media ({', '.join(tried)}) [{'; '.join(dict.fromkeys(errs))}]: {last_err}")


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


def ordered_openrouter_models(prefer=None):
    """Verified model list; `prefer` slugs (when they really exist on OpenRouter) go first, in the given order."""
    base = get_openrouter_models()
    if not prefer:
        return base
    with _or_lock:
        ids = _or_cache.get("ids")
    first = [m for m in prefer if ids is None or m in ids]
    return first + [m for m in base if m not in first]


def openrouter_chat(messages, max_tokens=1800, temperature=0.8, models=None):
    """Try each verified OpenRouter model in order. Returns (text, model) or (None, None).
    401/402 (bad key / out of credits) abort the whole provider immediately."""
    if not OPENROUTER_API_KEY:
        return None, None
    for model in ordered_openrouter_models(models):
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


def llm_write(user_text, history=None, system=None, allow_gemini=True, max_tokens=1800, temperature=0.8,
              models=None):
    """Writer used for chat, news, posts, summaries: OpenRouter first, Gemini fallback.
    Returns (text, engine) or (None, None)."""
    history = history or []
    sys_prompt = system or system_prompt()
    messages = [{"role": "system", "content": sys_prompt}]
    for role, txt in history:
        messages.append({"role": "assistant" if role == "model" else "user", "content": txt})
    messages.append({"role": "user", "content": user_text})
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
    kb.add(
        types.InlineKeyboardButton(CATEGORIES["sport"][0], callback_data=f"cat:{mode}:sport"),
        types.InlineKeyboardButton(CATEGORIES["coffee"][0], callback_data=f"cat:{mode}:coffee"),
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
UNVERIFIED_SOURCE = "ሰከላ ሚዲያ ዴስክ (በማረጋገጥ ላይ)"

NEWSROOM_SYSTEM = (
    "You are a veteran Ethiopian Amharic journalist and the senior editor of 'ሰከላ ሚዲያ (Sekela Media)' and 'ሰከላ ስፖርት "
    "(Sekela Sport)'. You THINK in Amharic and write the way the best Addis Ababa online newsrooms do - ZeHabesha, Gursha, "
    "Tikvah and Getu Temesgen: warm, vivid, fluent, informative. You never translate: you retell the story as an Ethiopian "
    "reporter who heard the facts in Amharic.\n"
    "NATIVE-VOICE RULES (highest priority):\n"
    "- ZERO calques. Do not follow English word order or clause stacking. Start with what matters to an Ethiopian reader, "
    "put time and place where Amharic puts them (e.g. «ማክሰኞ ዕለት በአዲስ አበባ ...»), and end sentences with the verb. Never "
    "translate English idioms or buzzwords literally (game-changer, boost, tip of the iceberg, landmark, spark, "
    "raise concerns...); say the same thing the way an Amharic speaker would.\n"
    "- Avoid noun-chains with «ማድረግ / መሆን / ተችሏል» repeated; prefer one strong verb: «አስታውቋል», «ይፋ አድርጓል», «ተፈራርመዋል», "
    "«አሻቅቧል», «ወርዷል», «ተጠናቋል». Vary sentence rhythm and open sentences differently.\n"
    "- Connect the story so it flows like a narration, not a list: «በተጨማሪም», «ይህም», «ከዚህ ቀደም», «በሌላ በኩል», «በመሆኑም», "
    "«ይሁን እንጂ», «እንደ ባለሙያዎች ገለጻ». Mostly 12-22 words per sentence, never more than 26.\n"
    "- Before answering read every sentence in your head: if it sounds like it was translated from English, rewrite it "
    "in natural Amharic.\n"
    "- PERFECT GRAMMAR: plural / respectful / collective subjects take PLURAL verbs («ግጭቶች እየተባባሱ ናቸው», «ባለሥልጣናቱ "
    "አስታውቀዋል», «ዋጋዎች ጨምረዋል»), never «ግጭቶች እየተባባሱ ነው»; singular subjects take singular verbs («ሚኒስቴሩ "
    "አስታውቋል»). One consistent tense inside a paragraph, correct plural/definite forms (-ዎች, -ኦች, -ዎቹ, -ው, -ዋ, -ቱ), no "
    "doubled words, spelling as Ethiopian media write it.\n"
    "- RICH AND SPECIFIC: never a bare 2-line stub. Give the complete story: who, what, where, when, the key numbers, "
    "names, amounts, dates, the background (what happened before), why it matters to Ethiopians and what happens next - "
    "all taken from the notes. A vague claim without who / where / how much is forbidden.\n"
    "- Neutral third person; NEVER address anyone (no አንተ/እናንተ/እኔ/እኛ, no questions, no greetings, never the name ሄኖክ).\n"
    "- Attribute every claim and quote to its source («ሮይተርስ እንደዘገበው», «የገንዘብ ሚኒስቴር አስታውቋል»).\n"
    "- Use ONLY facts, numbers, names and quotes present in the research notes. Never invent, embellish or speculate. "
    "Never mix facts of different events. Spell names the same way everywhere.\n"
    "- Plain text, no markdown, no emojis inside paragraphs; keep widely used English terms (AI, Bitcoin, TikTok, club "
    "and player names when no common Amharic spelling exists) in Latin letters."
)

NEWS_BRIEF = (
    "Write ONE Amharic news item from the research notes in this exact structure: bold headline -> hook -> full "
    "explanation -> sources. Write it as a native Ethiopian journalist who thinks in Amharic - no literal "
    "English-to-Amharic phrasing anywhere.\n"
    "Topic: {topic}\n{brand_note}\n\n[RESEARCH NOTES]\n{facts}\n\n{mode_note}"
    "SEO: the headline and the hook must naturally contain the main high-search keyword of the story (the key name, "
    "place, club or topic, in Amharic; add the common English spelling when people search for it that way).\n"
    "Check every sentence: perfect grammar, plural subject -> plural verb («እየተባባሱ ናቸው», never «እየተባባሱ ነው»), natural "
    "word order, mostly 12-22 words.\n"
    "Return EXACTLY these blocks and nothing else:\n"
    "===HEADLINE===\n4-7 words: a bold, factual, scroll-stopping headline that fits a photo card; no quotes, no emoji, "
    "no full stop.\n"
    "===LEAD===\nThe hook: 1-2 vivid, crystal-clear sentences (25-45 words in total) that answer who, what, where and "
    "when and make the reader want to continue.\n"
    "===CONTEXT===\nThe full explanation: 4-6 well-connected sentences (80-130 words in total) that tell the complete "
    "story - the key figures and names, the background, the reactions or reasons, why it matters to Ethiopians and "
    "what comes next. Never a bare two-line stub.\n"
    "===OFFICIAL===\nONE sentence with an official statement or direct quote, attributed to the named official or "
    "body. If the notes contain none, write exactly: NONE\n"
    "===SOURCE===\nThe outlet names taken from the notes, separated by ' / ' (Amharic names where established: "
    "ቢቢሲ አማርኛ, ሮይተርስ, ፋና, ኢቢሲ, ኢዜአ, ዋልታ, ቲክቫህ ኢትዮጵያ, ጉርሻ, 3-4-3 ስፖርት, አል-አይን, ዶቼ ቬለ አማርኛ; AP).\n"
    "===TAGS===\n4-6 topic-specific hashtags separated by spaces: a mix of high-search Amharic and English ones, e.g. "
    "#ኢትዮጵያ #Ethiopia #PremierLeague #አርሰናል #TikTokEthiopia. No spaces inside a tag. Do NOT include "
    "#ሰከላ_ሚዲያ, #SekelaMedia, #ሰከላ_ኒውስ, #SekelaNews, #ሰከላ_ስፖርት or #SekelaSport.\n"
    "===IMAGE===\nOne English paragraph, 25-45 words: a symbolic, cinematic vertical 4:5 photojournalism scene that "
    "visualizes the story's theme (setting, subject matter, lighting, lens, mood, high-contrast colour grade; keep main "
    "subject in upper 60% of frame). No identifiable real people, no text, no logos, no flags, no depiction of violence "
    "or casualties.\n"
)

COFFEE_BRIEF = (
    "Write ONE viral '☕ ሰከላ ኮፊ ታይም (Sekela Coffee Time)' story from the research notes - inspired by top viral "
    "60K+ liked Ethiopian social-media fact/human-interest cards (surprising, funny, heartwarming, or mind-blowing "
    "weird-but-true stories from Ethiopia or the world).\n"
    "Topic: {topic}\n\n[RESEARCH NOTES]\n{facts}\n\n{mode_note}"
    "TONE: Conversational, warm, witty and unforgettable Addis Ababa Amharic - like telling an amazing story over "
    "coffee. Zero robotic translations!\n"
    "Return EXACTLY these blocks and nothing else:\n"
    "===HEADLINE===\n3-5 punchy words: a huge, curiosity-sparking headline for a photo card; no quotes, no emoji, no full stop.\n"
    "===LEAD===\nONE unforgettable curiosity-sparking 1-line hook sentence (12-25 words) that makes the reader immediately "
    "stop scrolling.\n"
    "===CONTEXT===\n4-5 engaging, vivid sentences (70-110 words) telling the surprising details, the human angle or fun "
    "facts, and why people are talking about it online.\n"
    "===OFFICIAL===\nNONE\n"
    "===SOURCE===\nThe outlet/platform names taken from the notes separated by ' / '.\n"
    "===TAGS===\n4-6 viral Amharic & English hashtags (no #ሰከላ_ኮፊ_ታይም or #ሰከላ_ሚዲያ).\n"
    "===IMAGE===\nOne English paragraph, 25-45 words: a clean, striking, high-contrast vertical 4:5 photo depicting the "
    "story's subject in the upper 60% of the frame, warm cinematic lighting, no text, no logos.\n"
)

UNVERIFIED_NOTE = (
    "[NO LIVE RESEARCH WAS AVAILABLE] Write a careful editorial synthesis using ONLY well-established background "
    "facts you are certain of. Do NOT state specific recent numbers, dates, quotes or events. In ===SOURCE=== write "
    "exactly: " + UNVERIFIED_SOURCE + "\n\n"
)

IMAGE_STYLE = ("high-contrast cinematic news photography, dramatic lighting, shallow depth of field, ultra detailed, "
               "sharp focus, dark moody tones, vertical 4:5 portrait composition, main subject centered in upper half, "
               "no text, no letters, no watermark")
IMAGE_FALLBACKS = {
    "media": ("a dramatic dusk skyline of a modern African capital with glowing city lights and a newsroom "
              "feel, teal and orange colour grade"),
    "sport": ("an empty floodlit football stadium at night, green pitch, dramatic light beams, dark moody atmosphere, "
              "orange and teal colour grade"),
    "coffee": ("warm atmospheric Ethiopian coffee ceremony smoke rising in golden morning light, sleek modern table, "
               "cinematic photography"),
}

PKG_KEYS = ("headline", "lead", "context", "official", "source", "tags", "image")


def parse_package(text):
    """Split the writer output into its labelled blocks. Missing blocks stay empty."""
    out = {k: "" for k in PKG_KEYS}
    parts = re.split(r"===(HEADLINE|LEAD|CONTEXT|OFFICIAL|SOURCE|TAGS|IMAGE)===", text or "")
    for i in range(1, len(parts) - 1, 2):
        out[parts[i].lower()] = parts[i + 1].strip()
    return out


def _tidy_headline(s):
    s = re.sub(r"[\"'“”‘’«»]", "", s or "").strip()
    s = re.sub(r"^[^\wሀ-፼]+", "", s)  # leading emoji / bullets
    return s.rstrip(".።").strip()


def clean_headline(h, fallback_text="", max_words=7):
    """4-7 word headline: no quotes / leading emoji / trailing full stop; falls back to the lead's first words."""
    h = _tidy_headline(h) or _tidy_headline(fallback_text)
    return " ".join(h.split()[:max_words])


def detect_outlets(text):
    """Amharic display names of the recognised outlets mentioned in text."""
    low = (text or "").lower()
    found = [name for name, rx in OUTLETS if re.search(rx, low)]
    for generic, specific in (("ቢቢሲ", "ቢቢሲ አማርኛ"), ("ቢቢሲ", "ቢቢሲ ስፖርት"), ("ዶቼ ቬለ", "ዶቼ ቬለ አማርኛ")):
        if generic in found and specific in found:
            found.remove(generic)
    return found


def _blob(facts, sources):
    return (facts or "") + " " + " ".join(f"{t} {u}" for t, u in (sources or []))


def resolve_source(raw_src, facts, sources, unverified=False):
    """Name the outlet(s) for the mandatory source line."""
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
    """Brand hashtags (fixed per brand/segment) + 4-6 dynamic topic hashtags (Amharic + English)."""
    brand_tags = list(COFFEE_TAGS if segment == "coffee" else BRANDS[brand]["tags"])
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
    if len(dyn) < 4:  # top up so every post carries 4-6 topic tags
        words = [re.sub(r"[^\w]", "", w) for w in (headline or "").split()[:2]]
        extras = (["#" + "_".join(w for w in words if w)] if any(words) else [])
        extras += (["#CoffeeTime", "#ViralEthiopia"] if segment == "coffee"
                   else (["#ኢትዮጵያ", "#Ethiopia"] if brand == "media" else ["#እግር_ኳስ", "#Football"]))
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
    """True when the body is the kind of vague 2-line summary we do not want."""
    ctx = _para(pkg.get("context"))
    sents = [x for x in re.split(r"[።!?\n]+", ctx) if x.strip()]
    return len(sents) < 4 or len(ctx.split()) < 55


PROOF_PROMPT = (
    "You are a strict Amharic proofreader of a news desk. Below are the LEAD, CONTEXT and OFFICIAL blocks of a news "
    "post. Fix ONLY: grammar, subject-verb agreement (plural subjects need plural verbs: «ግጭቶች እየተባባሱ ናቸው», never "
    "«ነው»), tense consistency, spelling, doubled words, awkward phrasing, and EVERY sentence that sounds translated "
    "from English (literal calques, English word order, stacked clauses): rewrite those the way a native Addis Ababa "
    "journalist (ZeHabesha / Gursha / Getu Temesgen style) would say it, with smooth connectors between sentences. "
    "Do NOT add, remove or change any fact, number, name or attribution. Keep the same number of sentences (mostly "
    "12-22 words each). Return "
    "EXACTLY the same three labelled blocks and nothing else:\n===LEAD===\n...\n===CONTEXT===\n...\n"
    "===OFFICIAL===\n...\n\n"
)


def proofread_pkg(pkg):
    """One grammar pass by the OpenRouter writers. Any doubt -> the original package is returned unchanged."""
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
        log.info("proofread dropped a number - original kept")
        return pkg
    return new


def long_sentences(pkg, limit=27):
    """Sentences of the body that are too long for the crystal-clear style."""
    txt = " ".join([pkg.get("lead", ""), pkg.get("context", ""), pkg.get("official", "")])
    return [x for x in re.split(r"[።!?\n]+", txt) if len(x.split()) > limit]


def assemble_news_post(pkg, facts="", sources=None, unverified=False, brand="media", segment=None):
    """Build the post: bold headline -> hook -> details -> (official statement) -> sources -> brand footer -> hashtags.
    Returns (post, headline) or None if the writer's output is unusable."""
    lead, context = _para(pkg.get("lead")), _para(pkg.get("context"))
    official = _para(pkg.get("official"))
    if official.strip(" .።").upper() in ("", "NONE", "N/A"):
        official = ""
    if not lead or not context:
        return None
    max_words = 5 if segment == "coffee" else 7
    headline = clean_headline(pkg.get("headline"), lead, max_words=max_words)
    if not headline:
        return None
    src = resolve_source(pkg.get("source"), facts, sources, unverified)
    tags = " ".join(build_tags(brand, pkg.get("tags"), headline, segment=segment))
    lead_icon = "☕" if segment == "coffee" else "🔴"
    parts = [f"{lead_icon} {headline}", lead, context]
    if official:
        parts.append(official)
    parts += [f"📌 ምንጭ፡ {src}", brand_footer(brand), tags]
    return "\n\n".join(parts), headline


# --- story discovery (used by /news and by the breaking-news hunter) -------------------------------------------
STORY_PROMPT = (
    "Current Addis Ababa date/time: {now_eat} (UTC: {now_utc}).\n"
    "Search the web NOW and find the most important, trending and timely stories from the last {hours} hours about: {focus}\n"
    "STRICT 24-HOUR FRESHNESS RULE: Check the publication timestamp of every story. Reject ANYTHING older than 24 hours "
    "(prefer stories published in the last 2 to 12 hours). NEVER return 2-3 day old news.\n"
    "MULTI-PLATFORM COVERAGE: Search across major newsrooms AND social/viral platforms — TikTok, YouTube, X (Twitter), "
    "public Facebook pages, Google Trends, and independent/smaller Ethiopian media & Addis Ababa blogs (Fana BC, EBC, "
    "ENA, Walta, Addis Standard, Borkena, Capital Ethiopia, Addis Fortune, Sheger FM, Tikvah-style Addis local "
    "incidents, Gursha-style tech/Meta/social updates, Behaja-style job/opportunity alerts, 3-4-3/433-style football, "
    "DW Amharic, VOA Amharic, Al Ain Amharic, BBC Amharic, Reuters, AP, Fabrizio Romano). Each story must be a DIFFERENT event.\n"
    "BALANCE RULES (strict): use BBC Amharic for AT MOST ONE of the stories; include AT MOST ONE conflict / fighting / "
    "security story; make the majority about economy & business, technology/social-media updates, Addis Ababa & "
    "infrastructure, viral Ethiopian TikTok/YouTube/Facebook discussions, jobs/opportunities, diplomacy or culture.\n"
    "{feeds}"
    "Return {n} top stories, ONE per line, in EXACTLY this format (English):\n"
    "STORY|<importance 1-10>|<headline, max 14 words>|<outlet/platform names, comma separated>|<one sentence with the key fact>\n"
    "Importance: 8-10 = major breaking/viral story or decisive goal/transfer, 5-7 = strong daily/trending story."
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
    """Translate story headlines + summaries into Amharic news style. Returns {index: (headline, summary)}."""
    if not stories:
        return {}
    listing = "\n".join(f"{i}|{s['title']}|{s['summary']}" for i, s in enumerate(stories, 1))
    prompt = ("Translate these news items into Amharic news style: headline max 8 words, summary ONE sentence. "
              "Output exactly one line per item in the format N|<headline>|<summary> and nothing else.\n" + listing)
    text, _ = llm_write(prompt, system=NEWSROOM_SYSTEM, max_tokens=1500, temperature=0.3, models=NEWS_WRITER_MODELS)
    out = {}
    for ln in (text or "").splitlines():
        m = re.match(r"^\s*(\d+)\s*\|(.+?)\|(.+)$", ln)
        if m:
            out[int(m.group(1))] = (clean_headline(m.group(2)), _para(m.group(3)))
    return out


NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def detect_brand(text):
    """Sport topics go to ሰከላ ስፖርት, everything else to ሰከላ ሚዲያ."""
    return "sport" if has_any(text, SPORT_WORDS) else "media"


def run_news(chat_id, label, topic, brand=None, segment=None):
    """/news: verified top stories (using live search + RSS/Google Trends + strict RSS fallback)."""
    start = time.time()
    brand = brand or detect_brand(topic)
    try:
        with Typing(chat_id):
            feed_items = fetch_feed_items(brand, max_age_hours=24, per_feed=5)
            trends = fetch_google_trends(max_age_hours=24) if brand == "media" else []
            feeds_txt = feed_items_text(feed_items + trends)
            stories, sources = scan_stories(topic, hours=24, n=4, feeds=feeds_txt)
            if not stories and (feed_items or trends):
                # Match topic keywords in RSS items first, otherwise top interleaved fresh items
                kws = [w for w in re.findall(r"[A-Za-z0-9ሀ-፼]{3,}", topic.lower()) if w not in STOP_KEYS]
                pool = feed_items + trends
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
# Sekela News 1080x1350 (4:5 portrait) Cards + Real-Photo Quality Gate
# ----------------------------------------------------------------------------
def build_image_prompt(scene, brand="media", segment=None):
    scene = re.sub(r"\s+", " ", (scene or "")).strip()
    if not scene or re.search(r"[ሀ-፼]", scene):
        key = "coffee" if segment == "coffee" else brand
        scene = IMAGE_FALLBACKS.get(key, IMAGE_FALLBACKS["media"])
    return (scene[:420].rstrip(" ,.") + ", " + IMAGE_STYLE)[:640]


def pollinations_url(prompt):
    return (f"https://image.pollinations.ai/prompt/{quote(prompt, safe='')}"
            f"?width=1080&height=1350&nologo=true&seed={int(time.time())}")


def _fallback_backdrop(brand="media", segment=None, width=1080, height=1350):
    """Clean branded dark gradient backdrop if all external image generators fail."""
    if not PIL_OK:
        return None
    key = "coffee" if segment == "coffee" else brand
    th = CARD_THEMES.get(key, CARD_THEMES["media"])
    img = Image.new("RGB", (width, height), th["deep"])
    d = ImageDraw.Draw(img)
    r0, g0, b0 = th["main"]
    r1, g1, b1 = th["deep"]
    for y in range(height):
        k = (y / height) ** 0.85
        d.line([(0, y), (width, y)], fill=(
            int(r0 * (1 - k) * 0.55 + r1 * k),
            int(g0 * (1 - k) * 0.55 + g1 * k),
            int(b0 * (1 - k) * 0.55 + b1 * k),
        ))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


def fetch_image(prompt, attempts=2, brand="media", segment=None):
    """Fast AI background: Pollinations (25s timeout) -> Gemini Imagen fallback -> branded gradient backdrop."""
    url = pollinations_url(prompt)
    for i in range(attempts):
        try:
            r = requests.get(url, timeout=25)
            ctype = r.headers.get("Content-Type", "")
            if r.status_code == 200 and ctype.startswith("image/") and len(r.content) > 2000:
                return r.content
            log.warning("Pollinations attempt %d: HTTP %s %s", i + 1, r.status_code, ctype)
        except Exception as e:
            log.warning("Pollinations attempt %d failed: %s", i + 1, e)
        time.sleep(2)
    if GEMINI_API_KEY:
        try:
            return gen_gemini_image(prompt)
        except Exception as e:
            log.warning("Gemini image fallback failed: %s", short_reason(e, 100))
    return _fallback_backdrop(brand=brand, segment=segment)


_PHOTO_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SekelaNewsBot/1.0; +https://t.me/sekelamediaET)"}
_META_IMG = [
    re.compile(r'<meta[^>]+(?:property|name)\s*=\s*["\'](?:og:image(?::secure_url|:url)?|twitter:image(?::src)?)["\']'
               r'[^>]*?content\s*=\s*["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*?(?:property|name)\s*=\s*["\']'
               r'(?:og:image|twitter:image)["\']', re.I),
]
_BAD_IMG = re.compile(
    r"logo|favicon|placeholder|sprite|avatar|default[-_]?(?:image|thumb|share)|1x1|watermark|"
    r"gettyimages|shutterstock|alamy|istockphoto|depositphotos|123rf|dreamstime|"
    r"thumbnail|small[-_]?img|crest|badge|150x|300x|400x|500x", re.I
)


def download_photo(url, referer=None):
    """Photo Quality Gate: reject low-res (<600px wide), watermarked, extreme-banner or blurry images."""
    try:
        if not url or _BAD_IMG.search(url):
            return None
        headers = dict(_PHOTO_HEADERS)
        if referer:
            headers["Referer"] = referer
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image/") or len(r.content) < 30000:
            return None
        if PIL_OK:
            im = Image.open(io.BytesIO(r.content))
            w, h = im.size
            if w < 600 or h < 400:
                return None
            if w / max(1, h) > 2.6 or h / max(1, w) > 2.6:
                return None
            edge_std = ImageStat.Stat(im.convert("L").resize((180, 180)).filter(ImageFilter.FIND_EDGES)).stddev[0]
            if edge_std < 5.0:
                return None
        return r.content
    except Exception as e:
        log.info("photo download failed: %s", short_reason(e, 80))
        return None


def find_real_photo(urls, direct_image=None, max_pages=4):
    """REAL PHOTO FIRST: the RSS image if given, otherwise og:image / twitter:image of the source articles."""
    if direct_image:
        b = download_photo(direct_image)
        if b:
            return b
    seen, pages = set(), 0
    for u in urls or []:
        if not u or u in seen or pages >= max_pages:
            continue
        seen.add(u)
        pages += 1
        try:
            r = requests.get(u, headers=_PHOTO_HEADERS, timeout=15, allow_redirects=True)
            if r.status_code != 200 or "html" not in r.headers.get("Content-Type", ""):
                continue
            html = r.text[:250000]
            for rx in _META_IMG:
                m = rx.search(html)
                if not m:
                    continue
                img = urljoin(r.url, html_lib.unescape(m.group(1)).strip())
                if _BAD_IMG.search(img):
                    continue
                b = download_photo(img, referer=r.url)
                if b:
                    return b
        except Exception as e:
            log.info("article page fetch failed: %s", short_reason(e, 80))
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
    """Path of a bold Ethiopic font, or None."""
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
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


def _is_eth(ch):
    o = ord(ch)
    return 0x1200 <= o <= 0x139F or 0x2D80 <= o <= 0x2DDF or 0xAB00 <= o <= 0xAB2F


def _runs(text, f_eth, f_lat):
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


def _cover_crop_resize(img, width, height):
    """Aspect-fill crop to width x height, biased toward upper quarter so faces aren't cropped."""
    w, h = img.size
    target = width / height
    src = w / max(1, h)
    if src > target:
        new_w = int(h * target)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    elif src < target:
        new_h = int(w / target)
        top = max(0, (h - new_h) // 4)
        img = img.crop((0, top, w, top + new_h))
    return img.resize((width, height), Image.LANCZOS)


def _apply_seamless_gradient(img, start_ratio=0.56, tint=(12, 8, 12), max_alpha=245):
    """Seamless multi-stop smoothstep gradient from transparent (top ~60% untouched) to deep dark at bottom."""
    W, H = img.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    g0 = int(H * start_ratio)
    span = max(1, H - g0)
    for y in range(g0, H):
        t = (y - g0) / span
        s = t * t * (3.0 - 2.0 * t)  # smoothstep: 0 slope at g0, zero hard edge
        od.line([(0, y), (W, y)], fill=tint + (int(max_alpha * s),))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _draw_social_footer(draw, right_x, center_y, handle):
    """Crisp mini Telegram & Facebook vector icons + @handle text on the bottom-right of cards."""
    f_h = _latin_font(23)
    hw = int(f_h.getlength(handle))
    x_text = right_x - hw
    # Facebook mini circle
    r = 14
    fx = x_text - 14 - r
    draw.ellipse([fx - r, center_y - r, fx + r, center_y + r], fill=(24, 119, 242, 245))
    f_fb = _latin_font(21)
    draw.text((fx - 4, center_y + 8), "f", font=f_fb, fill=(255, 255, 255, 255), anchor="ls")
    # Telegram mini circle
    tx = fx - 2 * r - 8
    draw.ellipse([tx - r, center_y - r, tx + r, center_y + r], fill=(38, 165, 228, 245))
    draw.polygon([(tx - 7, center_y), (tx + 7, center_y - 6), (tx + 3, center_y + 7), (tx - 1, center_y + 2)],
                 fill=(255, 255, 255, 255))
    draw.text((x_text, center_y + 8), handle, font=f_h, fill=(238, 240, 246, 235), anchor="ls")


CARD_THEMES = {
    "media": {"main": (162, 20, 44), "deep": (16, 8, 14), "gold": (255, 212, 72), "accent": (255, 212, 72)},
    "sport": {"main": (4, 128, 90), "deep": (6, 20, 16), "gold": (255, 212, 72), "accent": (64, 232, 162)},
    "coffee": {"main": (118, 66, 24), "deep": (18, 11, 6), "gold": (255, 198, 68), "accent": (255, 198, 68)},
}


def _draw_bold_shadow(draw, x, baseline, text, f_eth, f_lat, fill, shadow=True):
    """Ultra-bold headline text with heavy drop shadow + crisp dark outline for maximum legibility."""
    if shadow:
        for dx, dy, a in ((0, 9, 75), (3, 9, 75), (6, 9, 65), (0, 6, 100), (3, 6, 100), (5, 6, 95), (2, 4, 130),
                          (4, 4, 130), (-2, 3, 140), (0, 3, 165), (3, 3, 165), (6, 3, 130)):
            _draw_mixed(draw, x + dx, baseline + dy, text, f_eth, f_lat, (0, 0, 0, a))
        for dx in (-3, -2, -1, 0, 1, 2, 3):
            for dy in (-3, -2, -1, 0, 1, 2, 3):
                if dx * dx + dy * dy in (4, 5, 8, 9, 10, 13, 18):
                    _draw_mixed(draw, x + dx, baseline + dy, text, f_eth, f_lat, (6, 6, 10, 240))
    for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        _draw_mixed(draw, x + dx, baseline + dy, text, f_eth, f_lat, fill)


def compose_card(bg_bytes, headline, badge=None, ai=True, width=1080, height=1350, brand="media", segment=None):
    """1080x1350 (4:5 portrait) Ethiopian viral news/coffee card:
    - Top ~60% of the photo completely clean and unobstructed
    - Seamless multi-stop gradient over the bottom ~40% (alpha=0 -> alpha=245)
    - Thin accent bar + ultra-bold two-tone headline (Crisp White + Warm Gold/Neon Accent)
    - Bottom sleek brand pill + Telegram/Facebook handle icons."""
    if not PIL_OK or not bg_bytes or not headline:
        return None
    eth_path = find_ethiopic_font()
    if not eth_path:
        return None
    theme_key = "coffee" if segment == "coffee" else brand
    theme = CARD_THEMES.get(theme_key, CARD_THEMES["media"])
    try:
        W, H = width, height
        img = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
        img = _cover_crop_resize(img, W, H)
        img = ImageEnhance.Contrast(img).enhance(1.07)
        img = _apply_seamless_gradient(img, start_ratio=0.56, tint=theme["deep"], max_alpha=245)
        draw = ImageDraw.Draw(img, "RGBA")

        margin, max_w = 56, W - 112
        start_fs = 114 if segment == "coffee" else 102
        fs, lines = start_fs, []
        for fs in range(start_fs, 55, -4):
            f_eth, f_lat = ImageFont.truetype(eth_path, fs), _latin_font(fs)
            lines = _wrap(headline, f_eth, f_lat, max_w)
            if len(lines) <= 3 and all(_measure(ln, f_eth, f_lat) <= max_w for ln in lines):
                break
        line_h = int(fs * 1.18)
        badge_h = 56
        badge_y = H - 46 - badge_h
        y0 = badge_y - 36 - line_h * len(lines)

        # Thin accent bar above headline
        draw.rounded_rectangle([margin, y0 - 28, margin + 124, y0 - 17], radius=5, fill=theme["accent"] + (255,))
        f_eth, f_lat = ImageFont.truetype(eth_path, fs), _latin_font(fs)
        for i, ln in enumerate(lines):
            base = y0 + i * line_h + int(fs * 0.96)
            accent_line = len(lines) > 1 and i == len(lines) - 1
            color = theme["accent"] + (255,) if accent_line else (255, 252, 245, 255)
            _draw_bold_shadow(draw, margin, base, ln, f_eth, f_lat, color)

        # Sleek bottom brand pill
        b_eth, b_lat = ImageFont.truetype(eth_path, 30), _latin_font(28)
        badge_text = badge or (COFFEE_BADGE if segment == "coffee" else BRANDS[brand]["badge"])
        bw = int(_measure(badge_text, b_eth, b_lat)) + 44
        draw.rounded_rectangle([margin, badge_y, margin + bw, badge_y + badge_h], radius=28,
                               fill=theme["main"] + (248,), outline=theme["gold"] + (255,), width=2)
        _draw_mixed(draw, margin + 22, badge_y + 39, badge_text, b_eth, b_lat, (255, 255, 255, 255))

        # Subtle social handles + icons on bottom right
        handle = BRANDS[brand].get("tg_handle", "@sekelamediaET")
        _draw_social_footer(draw, W - margin, badge_y + badge_h // 2, handle)

        # Subtle top-right badge (AI illustration when AI-generated, otherwise clean Today pill)
        if AI_LABEL and ai:
            a_font = _latin_font(20)
            label = "AI illustration"
            lw = int(a_font.getlength(label)) + 26
            draw.rounded_rectangle([W - margin - lw + 16, 32, W - 40, 32 + 34], radius=17, fill=(0, 0, 0, 115))
            draw.text((W - margin - lw + 29, 32 + 24), label, font=a_font, fill=(255, 255, 255, 210), anchor="ls")

        out = io.BytesIO()
        img.save(out, "JPEG", quality=92, optimize=True)
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
    """Single choke point for Graph API calls. Hard-coded policy: only POST to /photos, /feed and /videos, and refuse
    any path that touches comments."""
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
    """Photo with caption when an image exists, otherwise a plain feed post, on the brand's Facebook Page."""
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
    """Page name/id, 7-day insights and latest post counters."""
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
# Post package: Sekela News 1080x1350 card + copy -> preview -> approval -> Telegram + Facebook
# ----------------------------------------------------------------------------
def build_draft(topic, facts=None, sources=None, allow_fallback=True, brand=None, photo_urls=None, story_image=None,
                segment=None):
    """Research -> clear Amharic copy (+SEO tags) -> REAL photo (else AI background) -> 1080x1350 card.
    Always tries OpenRouter (NEWS_WRITER_MODELS) first, and automatically falls back to Gemini if OpenRouter
    returns None so /post and the hunter never fail silently."""
    if segment == "coffee":
        brand = "media"
    else:
        brand = brand or detect_brand(topic)
    sources = list(sources or [])
    photo_urls = list(photo_urls or [])
    if not facts:
        facts, sources = research_for(topic)
    if not facts:
        # Fast RSS / Google Trends fallback so /post still gets real <24h stories & photos
        pool = fetch_feed_items(brand, max_age_hours=24, per_feed=4)
        if brand == "media":
            pool += fetch_google_trends(max_age_hours=24)
        if pool:
            kws = [w for w in re.findall(r"[A-Za-z0-9ሀ-፼]{3,}", topic.lower()) if w not in STOP_KEYS]
            matched = [it for it in pool if any(k in it["title"].lower() for k in kws)] if kws else []
            picks = (matched or pool)[:4]
            facts = "\n".join(f"- {it['title']} [{it['source']}]" for it in picks)
            sources = [(it["source"], it["link"]) for it in picks if it.get("link")]
            photo_urls = [it["link"] for it in picks if it.get("link")] + photo_urls
            if not story_image:
                story_image = next((it.get("image") for it in picks if it.get("image")), None)
    unverified = not facts
    if unverified and not allow_fallback:
        return None
    rich = len((facts or "").split()) >= 60
    best, best_score, extra = None, None, ""
    brief_tpl = COFFEE_BRIEF if segment == "coffee" else NEWS_BRIEF
    for attempt in range(3):
        brief = (brief_tpl.replace("{topic}", topic)
                 .replace("{brand_note}", BRANDS[brand]["brief_note"])
                 .replace("{facts}", facts or "(none)")
                 .replace("{mode_note}", UNVERIFIED_NOTE if unverified else "")) + learning_block(brand) + extra
        # OpenRouter first, automatic Gemini fallback if OpenRouter fails/returns None
        text, engine = llm_write(brief, system=NEWSROOM_SYSTEM, max_tokens=3000,
                                 temperature=0.55 if segment == "coffee" else (0.5 if attempt == 0 else 0.3),
                                 allow_gemini=True, models=NEWS_WRITER_MODELS)
        if not text:
            continue
        pkg = parse_package(text)
        built = assemble_news_post(pkg, facts or "", sources, unverified, brand, segment=segment)
        if not built:
            continue
        longs, thin = long_sentences(pkg), (rich and segment != "coffee" and too_thin(pkg))
        score = len(longs) * 2 + (3 if thin else 0)
        if best is None or score < best_score:
            best, best_score = (pkg, built), score
        if score == 0:
            break
        extra = "\n\nREWRITE:"
        if longs:
            extra += " some sentences were longer than 26 words - split them (mostly 12-22 words each)."
        if thin:
            extra += (" the CONTEXT block was too thin - write 4-6 well-connected sentences (80-130 words) with the "
                      "concrete names, numbers, dates and background from the notes.")
        extra += " Keep all facts unchanged and keep the grammar perfect."
    if not best:
        log.error("news writer produced nothing (both OpenRouter and Gemini failed)")
        return None
    pkg, (post, headline) = best
    fixed = proofread_pkg(pkg)
    if fixed is not pkg:
        rebuilt = assemble_news_post(fixed, facts or "", sources, unverified, brand, segment=segment)
        if rebuilt:
            pkg, (post, headline) = fixed, rebuilt
    real = find_real_photo(list(photo_urls or []) + [u for _, u in sources[:4]], story_image)
    bg = real or fetch_image(build_image_prompt(pkg.get("image"), brand, segment=segment), brand=brand, segment=segment)
    badge = COFFEE_BADGE if segment == "coffee" else BRANDS[brand]["badge"]
    card = compose_card(bg, headline, badge, ai=not real, brand=brand, segment=segment) if bg else None
    bot_stats["drafts"] += 1
    mark_seen(headline + " " + _para(pkg.get("lead")))
    return {"post": post, "headline": headline, "image": card or bg, "card": bool(card), "sources": sources,
            "unverified": unverified, "brand": brand, "segment": segment, "pkg": pkg, "facts": facts or "",
            "bg": bg, "real": bool(real), "topic": "coffee" if segment == "coffee" else None}


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
    """Send the card preview + details with the approval buttons, and register the draft for publishing."""
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
                       "photo_msg": photo_msg, "details_msg": None, "pid": None, "tg": False, "fb": False,
                       "busy": False, "ts": time.time(), "topic": draft_topic(d),
                       "fixed": d.get("fixed", False) or segment == "coffee", "learned": False,
                       "fb_post_id": None, "outlet": ((d.get("sources") or [("", "")])[0][0] if d.get("sources") else "")}
    prune(drafts, 12)

    dest = f"{brand_label(brand)} (☕ ኮፊ ታይም)" if segment == "coffee" else brand_label(brand)
    details = [header + f"\n🎯 መድረሻ: {dest}"]
    if not (file_id and caption_fits):
        details.append("━━━━━━━━━━━━\n" + post)
    if d.get("unverified"):
        details.append("⚠️ ቀጥታ የዜና ፍለጋ አልተሳካም — ይህ ረቂቅ በታወቁ እውነታዎች ላይ ብቻ የተመሠረተ ነው፤ ምንጩ አልተረጋገጠም። "
                       "ከመለጠፍህ በፊት አረጋግጥ (ሲያጸድቅ ሁለተኛ ማረጋገጫ ይጠየቃል)።")
    if not image and not d.get("text_only"):
        details.append("⚠️ ምስሉ በዚህ ጊዜ አልተሳካም — ያለ ምስል መለጠፍ ወይም ሰርዘህ እንደገና መጠየቅ ትችላለህ።")
    elif d.get("real"):
        details.append("📷 ምስሉ ከምንጭ ዜናው የተወሰደ እውነተኛ ፎቶ ነው።")
        if not d.get("card"):
            details.append("⚠️ ርዕሱን በፎቶው ላይ ማተም አልተቻለም (Ethiopic font የለም) — ርዕሱ በፖስቱ ጽሑፍ ውስጥ አለ።")
    elif image and not d.get("card"):
        details.append("⚠️ ርዕሱን በምስሉ ላይ ማተም አልተቻለም (Ethiopic font የለም) — ርዕሱ በፖስቱ ጽሑፍ ውስጥ አለ። "
                       "NotoSansEthiopic-Bold.ttf ን ከ agent.py ጎን ኮሚት አድርግ።")
    src = format_sources(d.get("sources"), limit=4)
    if src:
        details.append(src)
    targets = [str(BRANDS[brand]["tg"])] + ([BRANDS[brand]["fb_name"] + " (Facebook)"] if fb_enabled(brand) else [])
    details.append("━━━━━━━━━━━━\n🔒 ✅ ከነካህ ብቻ ይለጠፋል → " + " + ".join(targets) +
                   "\nበፌስቡክ ኮመንቶች ላይ ቦቱ ፈጽሞ አይመልስም።")
    if start:
        details.append(took(start))
    mid = deliver(chat_id, None, "\n\n".join(details),
                  draft_keyboard(did, brand, swap=not d.get("fixed") and segment != "coffee"))
    with state_lock:
        if did in drafts:
            drafts[did]["details_msg"] = mid
    return did


def rebrand_draft(chat_id, msg_id, did):
    """One-tap switch Media <-> Sport: re-badge the card, swap footer links + brand hashtags, show a fresh preview."""
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
    card = compose_card(d["bg"], headline, BRANDS[new]["badge"], ai=not d.get("real"), brand=new) if d.get("bg") else None
    nd = {"post": post, "headline": headline, "image": card or d.get("bg"), "card": bool(card),
          "sources": d["sources"], "unverified": d["unverified"], "brand": new, "pkg": d["pkg"],
          "facts": d["facts"], "bg": d.get("bg"), "real": d.get("real", False)}
    for mid in {d.get("photo_msg"), d.get("details_msg"), msg_id}:
        drop_message(chat_id, mid)
    present_draft(chat_id, "", nd, f"🔄 ወደ {brand_label(new)} ተቀይሯል")


def run_post(chat_id, label, topic, context_text=None, context_sources=None, brand=None, photo_urls=None,
             story_image=None, segment=None):
    start = time.time()
    ack_id = send_progress(
        chat_id, "⏳ ሄኖክ፣ ጥናቱን አድርጌ ፖስቱን እና 1080x1350 ካርዱን እያዘጋጀሁ ነው (ከ20-50 ሰከንድ ይወስዳል)...")
    try:
        d = build_draft(topic, facts=context_text, sources=context_sources, allow_fallback=True, brand=brand,
                        photo_urls=photo_urls, story_image=story_image, segment=segment)
        if not d:
            deliver(chat_id, ack_id, "ሄኖክ፣ ዜናውን አሁን ማዘጋጀት አልቻልኩም 😕 ጥቂት ቆይተህ ደግመህ ሞክር።")
            return
        head = f"☕ የሰከላ ኮፊ ታይም ረቂቅ ዝግጁ ነው!" if segment == "coffee" else f"📝 ረቂቅ ዝግጁ ነው — {label}"
        present_draft(chat_id, label, d, head, start)
        drop_message(chat_id, ack_id)
    except Exception as e:
        log.error("post failed: %s", e)
        deliver(chat_id, ack_id, "ሄኖክ፣ ፖስቱን አሁን ማዘጋጀት አልቻልኩም 😅 ጥቂት ቆይተህ እንደገና ሞክር።")


def run_coffee_auto(chat_id, custom_topic=None):
    """Discover a fresh viral human-interest / weird-but-true story and draft a Coffee Time card for approval."""
    if custom_topic:
        run_post(chat_id, "☕ ሰከላ ኮፊ ታይም", custom_topic, brand="media", segment="coffee")
        return
    start = time.time()
    ack_id = send_progress(chat_id, "☕ ሄኖክ፣ አስገራሚ ቫይራል ታሪክ ፈልጌ የሰከላ ኮፊ ታይም ካርድ እያዘጋጀሁ ነው...")
    try:
        focus = ("Viral, surprising, funny, heartwarming, or weird-but-true human-interest facts and stories from "
                 "Ethiopia or the world (like viral Reddit/X/TikTok/BBC/Guardian culture & oddity stories, unusual "
                 "records, or heartwarming Addis Ababa community moments)")
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


def publish_telegram(d):
    """Photo + caption (or text) to the draft's brand channel."""
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
    """Approval gate: the brand's Telegram channel first, then its Facebook Page."""
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
            extra = ("\n\n💡 ቻናሉ public መሆኑን፣ ቻናሉ በትክክል መጻፉን እና ቦቱ አድሚን ('Post Messages') "
                     "መሆኑን አረጋግጥ — /ai_status ያሳይሃል።")
        bot.edit_message_text(f"⚠️ በከፊል ተሳክቷል ({brand_label(brand)})\n{tg_line}\n{fb_line}{extra}\n\n"
                              "የተሳካው እንደገና አይለጠፍም።", chat_id, msg_id, reply_markup=retry_keyboard(did, d.get("pid")))


def pin_published(chat_id, pid):
    with state_lock:
        p = published.get(pid)
    if not p:
        bot.send_message(chat_id, "ሄኖክ፣ ይህ ፖስት ከማህደረ ትውስታ ተሰርዟል (ቦቱ እንደገና ተነስቷል) — በቻናሉ ላይ በእጅ Pin አድርግ 🙏")
        return
    try:
        bot.pin_chat_message(p["chan"], p["msg_id"], disable_notification=True)
        bot.send_message(chat_id, f"📌 ፖስቱ በ {p['chan']} ላይ ተሰክቷል (Pin ተደርጓል)።")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Pin ማድረግ አልተሳካም: {short_reason(e, 140)}\n"
                                  "💡 ቦቱ በቻናሉ ላይ 'Pin Messages' ፈቃድ ሊኖረው ይገባል።")


def edit_published(chat_id, pid, new_text):
    """Replace the text/caption of a post the owner already approved on the Telegram channel."""
    with state_lock:
        p = published.get(pid)
    if not p:
        bot.send_message(chat_id, "ሄኖክ፣ ይህ ፖስት ከማህደረ ትውስታ ተሰርዟል — በቻናሉ ላይ በእጅ አርም 🙏")
        return
    if p["kind"] == "photo" and len(new_text) > CAPTION_LIMIT:
        with state_lock:
            awaiting_topic[OWNER_ID] = f"edit:{pid}"
        bot.send_message(chat_id, f"ሄኖክ፣ ጽሑፉ {len(new_text)} ፊደል ነው — ለፎቶ ፖስት ከ{CAPTION_LIMIT} መብለጥ አይችልም። "
                                  "አሳጥረህ እንደገና ላክልኝ።")
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
LEARN_TOPICS = ["economy", "tech", "addis", "diplomacy", "social", "culture", "coffee", "conflict", "politics",
                "ethiopian_football", "athletics", "boxing", "womens", "premier", "champions", "transfers", "other"]
TOPIC_AM = {"economy": "ኢኮኖሚ", "tech": "ቴክኖሎጂ", "addis": "አዲስ አበባ/መሠረተ ልማት", "diplomacy": "ዲፕሎማሲ", "social": "ማህበራዊ ሚዲያ",
            "culture": "ባህል", "coffee": "ኮፊ ታይም", "conflict": "ግጭት", "politics": "ፖለቲካ", "ethiopian_football": "የኢትዮጵያ እግር ኳስ",
            "athletics": "አትሌቲክስ", "boxing": "ቦክስ", "womens": "የሴቶች ስፖርት", "premier": "ፕሪሚየር ሊግ",
            "champions": "ቻምፒየንስ ሊግ", "transfers": "ዝውውር", "other": "ሌላ"}


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
    if d.get("segment") == "coffee":
        return "coffee"
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


def topic_weight(brand, topic):
    return learn_weights(brand).get(topic, 1.0)


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
            lines.append("Topics Henok approves / that perform best: " + ", ".join(good) + ".")
        if bad:
            lines.append("Topics he usually cancels (be extra selective): " + ", ".join(bad) + ".")
        win = [x["headline"] for x in winners if x.get("brand") == brand]
        if win:
            lines.append("Best-performing recent headlines (match their angle and clarity, never copy):\n" +
                         "\n".join("- " + h for h in win))
        if style:
            lines.append("Henok's own edits show his preferred wording (imitate this tone):\n" + "\n".join("- " + x for x in style))
        return ("\n\n" + "\n".join(lines))[:1800] if lines else ""
    except Exception as e:
        log.info("learning_block failed: %s", e)
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
    out = f"Henok's drafts in the last 30 days: {ap} approved, {ca} cancelled" + (f"; most approved topics: {top}" if top else "")
    if winners:
        out += "; best-performing posts: " + "; ".join(f"«{x['headline']}» score {x['score']:.0f}" for x in winners)
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
    ' or null>", "direction": "<more|less|null>", "team": "<football club in English or null>"}.\n'
    "Instruction: "
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
    lines = ["🧠 የሄኖክ ቋሚ የአርትኦት ህጎች (ያለ ኮድ ማስተካከያ):"]
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
    lines.append("\n➕ አዲስ ህግ ለመጨመር በቀጥታ ጻፍ: «ከዛሬ ጀምሮ የኢኮኖሚ ዜና አብዛ» ወይም /rules add <ህግ>")
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
        m = bot.send_document(OWNER_ID, f, caption="🧠 የራስ-ትምህርት ማህደረ ትውስታ backup — አታጥፋው (ቦቱ ሲጀምር ከዚህ ይመልሳል)",
                              disable_notification=True)
        try:
            bot.pin_chat_message(OWNER_ID, m.message_id, disable_notification=True)
        except Exception as e:
            log.info("backup pin failed: %s", short_reason(e, 80))
        if old:
            try:
                bot.delete_message(OWNER_ID, old)
            except Exception:
                pass
        with _learn_lock:
            learn_load()["backup"] = {"msg_id": m.message_id, "ts": time.time()}
        _learn_flags["important"] = False
        learn_save()
        log.info("learning backup sent (%d bytes)", len(data))
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
            log.info("learning memory restored from the pinned Telegram backup (%d rules)", len(js.get("rules", [])))
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
# Proactive breaking-news hunter + Google Trends + strict 24h freshness
# ----------------------------------------------------------------------------
HUNT_HEADERS = {
    "media": "🔔 ሄኖክ፣ አዲስ ሰበር ዜና አዘጋጅቼልሃለሁ! ለሰከላ ሚዲያ ፖስት እናድርገው?",
    "sport": "⚽ ሄኖክ፣ አዲስ የስፖርት ዜና አዘጋጅቼልሃለሁ! ለሰከላ ስፖርት ፖስት እናድርገው?",
    "coffee": "☕ ሄኖክ፣ አዲስ የሰከላ ኮፊ ታይም ቫይራል ታሪክ አዘጋጅቼልሃለሁ! ፖስት እናድርገው?",
}
HUNT_FOCUS = {
    "media": ("Ethiopia, BALANCED across topics - economy & business (birr, prices, trade, investment, banks, coffee "
              "exports, jobs/opportunities), technology & AI (Meta/Facebook/TikTok updates), Addis Ababa & "
              "infrastructure (local Addis incidents, corridor projects, roads, airports, housing), diplomacy, Ethiopian "
              "social-media and creator trends (TikTok, YouTube, X, Facebook public discussions) and culture - plus "
              "major international developments. Conflict news only when truly major, and at most one story"),
    "sport": ("Football and sport that Ethiopian fans care about, with LIVE results and goals from matches played in the "
              "last hours (scorer, minute, scoreline, teams); 3-4-3 / 433 style breaking football stories and transfers"),
}

PRIORITY_TOPICS = ("economy", "tech", "addis", "diplomacy", "social", "culture", "coffee")
TOPIC_DESK = {
    "economy": "Economy, Business & Jobs (ኢኮኖሚ/ንግድ/ስራ): birr, prices, trade, investment, banks, coffee, job/opportunity alerts",
    "tech": "Technology, AI & Social Platforms (ቴክኖሎጂ): AI, Meta/Facebook/TikTok changes, telecom, startups, digital Ethiopia",
    "addis": "Addis Ababa & Local Incidents: corridor projects, city updates, roads, airports, housing, local community news",
    "diplomacy": "Diplomacy: Ethiopia's foreign relations, summits, agreements, African Union",
    "social": "Ethiopian social / creator trends: viral TikTok, YouTube, X & Facebook discussions, awards",
    "culture": "Culture & tourism: festivals, music, film, heritage, holidays",
    "coffee": "Sekela Coffee Time: surprising, funny, heartwarming, or weird-but-true viral human-interest stories from Ethiopia or the world",
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
    "champions": "UEFA Champions League and the big European leagues (La Liga, Serie A, Bundesliga, Ligue 1): results, goals, stars",
    "transfers": "Confirmed football transfers and contract news (Fabrizio Romano style 'here we go' deals)",
    "ethiopian_football": ("Ethiopian football: Ethiopian Premier League, Saint George (ቅዱስ ጊዮርጊስ), Ethiopia Bunna, the Walia Ibex "
                           "national team, CAF qualifiers, Ethiopian players abroad"),
    "athletics": ("Ethiopian athletics and running (ሩጫ): marathons, Diamond League, World Athletics, Olympic and world "
                  "championship results of Ethiopian runners, new records"),
    "boxing": "Boxing (ቦክስ): major fights and results, heavyweight scene, East-African boxers",
    "womens": "Women's sports (የሴቶች ስፖርት): Women's Super League, women's Champions League, Ethiopian women athletes and footballers",
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
        "Borkena|https://borkena.com/feed/,"
        "DW Amharic|https://rss.dw.com/xml/rss-am-all,"
        "VOA Amharic|https://amharic.voanews.com/api/z$pqveymop,"
        "Addis Standard|https://addisstandard.com/feed/,"
        "ENA|https://www.ena.et/feed,"
        "Al Ain Amharic|https://am.al-ain.com/feed,"
        "EBC|https://www.ebc.et/feed/,"
        "Capital Ethiopia|https://www.capitalethiopia.com/feed/,"
        "Addis Fortune|https://addisfortune.news/feed/,"
        "The Reporter Ethiopia|https://www.thereporterethiopia.com/feed/,"
        "Ethio Negari|https://ethionegari.com/feed/"
    ),
    "sport": (
        "BBC Sport|https://feeds.bbci.co.uk/sport/football/rss.xml,"
        "Sky Sports|https://www.skysports.com/rss/12040,"
        "The Guardian|https://www.theguardian.com/football/rss,"
        "ESPN FC|https://www.espn.com/espn/rss/soccer/news,"
        "BBC Athletics|https://feeds.bbci.co.uk/sport/athletics/rss.xml,"
        "BBC Boxing|https://feeds.bbci.co.uk/sport/boxing/rss.xml,"
        "BBC Women's Football|https://feeds.bbci.co.uk/sport/football/womens/rss.xml"
    ),
}

SEEN_FILE = os.environ.get("SEEN_FILE", os.path.join(tempfile.gettempdir(), "sekela_seen_stories.json"))
SEEN_TTL = 48 * 3600
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
            if len(shared) >= 3 or (len(shared) >= 2 and len(shared) / max(1, min(len(keys), len(ik))) >= 0.5):
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
    """Parse RSS/Atom/ISO date strictly into UTC datetime, or None if unparseable."""
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
    """Fetch fresh (<24h) trending topics from Google Trends RSS (Ethiopia + Global/US)."""
    ht = "{https://trends.google.com/trending/rss}"
    out = []
    now_utc = datetime.now(timezone.utc)
    for geo in ("ET", "US"):
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


def fetch_feed_items(brand="media", max_age_hours=24, per_feed=5):
    """Recent items (<24h) from RSS feeds, interleaved round-robin across outlets."""
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
             "link": i.get("link"), "image": i.get("image")} for i in items[:n]]


def _outlet_key(story):
    first = ((story.get("outlets") or "").split(",")[0]).strip().lower()
    return first.split(" ")[0] if first else ""


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


def hunt_once(manual=False, min_importance=None, brand="media"):
    """One scan for the given brand (<24h freshness, multi-platform + RSS + Google Trends)."""
    lock = hunt_locks[brand]
    if not lock.acquire(blocking=False):
        return "busy"
    try:
        if not GEMINI_API_KEY:
            return "nokey"
        today = addis_now().strftime("%Y-%m-%d")
        daily = hunt_daily[brand]
        if daily["date"] != today:
            daily.update(date=today, count=0)
        cap = int(os.environ.get("HUNT_MAX_PER_DAY", "24") if brand == "media"
                  else os.environ.get("HUNT_SPORT_MAX_PER_DAY", "16"))
        if not manual and daily["count"] >= cap:
            return "cap"
        threshold = min_importance if min_importance is not None else int(os.environ.get("HUNT_MIN_IMPORTANCE", "5"))
        recent_b = [b for _, b in offer_log[-3:]]
        if not manual and brand == "sport" and len(recent_b) == 3 and all(b == "sport" for b in recent_b):
            return "wait"
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
        feed_items = fetch_feed_items(brand, max_age_hours=24, per_feed=5)
        trends = fetch_google_trends(max_age_hours=24) if brand == "media" else []
        feeds_txt = feed_items_text(feed_items + trends)

        def unseen(lst, min_imp=None):
            return [s for s in lst if (min_imp is None or s["importance"] >= min_imp)
                    and not is_seen(f"{s['title']} {s['summary']}")]

        stories, scan_sources = scan_stories(focus, hours=6 if brand == "sport" else 12, n=8, feeds=feeds_txt)
        fresh = unseen(stories, threshold)
        if not fresh:
            stories24, src24 = scan_stories(focus, hours=24, n=8, feeds=feeds_txt)
            if src24:
                scan_sources = src24
            stories = stories or stories24
            fresh = unseen(stories24, threshold) or unseen(stories24)
        if not fresh:
            fresh = unseen(stories_from_feeds(feed_items + trends))
        if not fresh and manual and stories:
            fresh = stories
        if not fresh:
            none_streak[brand] += 1
            return "none"

        story = pick_story(fresh, brand, desk, strict=not (manual or none_streak[brand] >= 2))
        if story is None:
            none_streak[brand] += 1
            return "none"
        none_streak[brand] = 0
        mark_seen(f"{story['title']} {story['summary']}")
        start = time.time()
        topic = f"{story['title']}. {story['summary']}"
        facts, sources = research_for(topic)
        if not facts:
            facts = f"{story['title']}. {story['summary']} (Reported by: {story['outlets']})"
            sources = scan_sources or [(story['outlets'] or "Sekela Media Desk", BRANDS[brand]["tg_link"])]
        photo_urls = [story["link"]] if story.get("link") else []
        d = build_draft(topic, facts=facts, sources=sources, allow_fallback=True, brand=brand,
                        photo_urls=photo_urls, story_image=story.get("image"), segment=segment)
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
        log.error("hunt failed (%s): %s", brand, e)
        return "failed"
    finally:
        lock.release()


def hunter_loop(brand):
    time.sleep(180 if brand == "media" else 420)
    interval_env = "HUNT_INTERVAL_MIN" if brand == "media" else "HUNT_SPORT_INTERVAL_MIN"
    while True:
        try:
            if not in_quiet_hours():
                log.info("hunt %s: %s", brand, hunt_once(brand=brand))
        except Exception as e:
            log.error("hunter loop error (%s): %s", brand, e)
        time.sleep(max(5, int(os.environ.get(interval_env, "30" if brand == "media" else "40"))) * 60)


def start_hunter():
    if os.environ.get("HUNTER_ENABLED", "1") == "0" or not GEMINI_API_KEY:
        return
    threading.Thread(target=hunter_loop, args=("media",), daemon=True, name="hunter-media").start()
    if os.environ.get("HUNTER_SPORT_ENABLED", "1") != "0":
        threading.Thread(target=hunter_loop, args=("sport",), daemon=True, name="hunter-sport").start()
    log.info("Hunters started (media every %s min, sport every %s min, quiet hours %s Addis time)",
             os.environ.get("HUNT_INTERVAL_MIN", "30"), os.environ.get("HUNT_SPORT_INTERVAL_MIN", "40"),
             os.environ.get("HUNT_QUIET", "23-6") or "off")


# ----------------------------------------------------------------------------
# Ethiopian-fan Sports Engine (ሰከላ ስፖርት) + 1080x1350 3-4-3 Match Result Cards
# ----------------------------------------------------------------------------
ESPN_LEAGUES = {"eng.1": "Premier League", "uefa.champions": "Champions League", "uefa.europa": "Europa League",
                "esp.1": "La Liga", "ita.1": "Serie A", "ger.1": "Bundesliga", "fra.1": "Ligue 1"}
LEAGUE_AM = {"eng.1": "ፕሪሚየር ሊግ", "uefa.champions": "ቻምፒየንስ ሊግ", "uefa.europa": "ዩሮፓ ሊግ", "esp.1": "ላ ሊጋ",
             "ita.1": "ሴሪኤ", "ger.1": "ቡንደስሊጋ", "fra.1": "ሊግ 1"}
LEAGUE_TAGS = {"eng.1": "#PremierLeague #ፕሪሚየርሊግ", "uefa.champions": "#ChampionsLeague #ቻምፒየንስሊግ",
               "uefa.europa": "#EuropaLeague", "esp.1": "#LaLiga #ላሊጋ", "ita.1": "#SerieA #ሴሪኤ",
               "ger.1": "#Bundesliga", "fra.1": "#Ligue1"}
TEAM_AM = {"manchester united": "ማንቸስተር ዩናይትድ", "manchester city": "ማንቸስተር ሲቲ", "arsenal": "አርሰናል", "chelsea": "ቼልሲ",
           "liverpool": "ሊቨርፑል", "tottenham": "ቶተንሃም", "newcastle": "ኒውካስል", "aston villa": "አስቶን ቪላ",
           "everton": "ኤቨርተን", "west ham": "ዌስትሃም", "brighton": "ብራይተን", "barcelona": "ባርሴሎና", "real madrid": "ሪያል ማድሪድ",
           "atletico": "አትሌቲኮ ማድሪድ", "bayern": "ባየርን ሙኒክ", "dortmund": "ቦሩሲያ ዶርትሙንድ", "paris saint": "ፓሪስ ሴንት ዠርሜን",
           "juventus": "ጁቬንቱስ", "inter": "ኢንተር ሚላን", "milan": "ኤሲ ሚላን", "napoli": "ናፖሊ", "roma": "ሮማ"}
DEFAULT_FAVORITES = ["Arsenal", "Chelsea", "Liverpool", "Manchester United", "Manchester City", "Tottenham", "Newcastle",
                     "Barcelona", "Real Madrid", "Bayern", "Paris Saint", "Juventus", "Inter", "Milan", "Atletico", "Dortmund"]
WEEKDAY_AM = ["ሰኞ", "ማክሰኞ", "ረቡዕ", "ሐሙስ", "ዓርብ", "ቅዳሜ", "እሁድ"]
_sport_state = {}                     # match id -> {"hs","as","goals","pre","ht","ft"}
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
    """Kick-off time in East Africa Time (UTC+3), 12-hour clock with the Amharic part of the day."""
    t = dt_utc.astimezone(timezone(timedelta(hours=3)))
    h = t.hour
    part = "ሌሊት" if h < 6 else ("ጠዋት" if h < 12 else ("ቀን" if h < 18 else "ምሽት"))
    h12 = h % 12 or 12
    return f"{part} {h12}:{t.minute:02d}"


def sport_leagues():
    raw = os.environ.get("SPORT_LEAGUES", "")
    keys = [x.strip() for x in raw.split(",") if x.strip()] or list(ESPN_LEAGUES)
    return keys


def espn_scoreboard(league, dates=None):
    """Normalised matches of one league (ESPN public scoreboard JSON). [] on any failure."""
    try:
        r = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard",
                         params={"dates": dates} if dates else None, headers=_UA, timeout=20)
        if r.status_code != 200:
            log.info("ESPN %s HTTP %s", league, r.status_code)
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
    """Fetch a real high-res match/player photo via ESPN event summary or linked recap article (10s max)."""
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
    """Matches of yesterday-today-tomorrow for every tracked league (cached 3 h)."""
    if not force and time.time() - _sport_sched["ts"] < 3 * 3600 and _sport_sched["matches"]:
        return _sport_sched["matches"]
    base = datetime.now(timezone.utc)
    days = [(base + timedelta(days=d)).strftime("%Y%m%d") for d in (-1, 0, 1)]
    seen, allm = set(), []
    for lg in sport_leagues():
        for dstr in days:
            for m in espn_scoreboard(lg, dstr):
                if m["id"] not in seen:
                    seen.add(m["id"])
                    allm.append(m)
    _sport_sched.update(ts=time.time(), matches=allm)
    return allm


def _scorer_lines(m):
    """Compact goal-scorer summary lines with minutes for the 3-4-3 style scoreboard card."""
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
    """1080x1350 (4:5 portrait) 3-4-3 Sport Match Card:
    - Real match/player photo when available (top ~56% clean, seamless emerald/black bottom gradient),
      else rich dark emerald stadium backdrop
    - Top status pill (GOAL! / FULL TIME / HALF TIME / KICK-OFF SOON) + league label
    - Bottom scoreboard (Team A  1 - 4  Team B) + goal scorers with minutes below
    - Bottom brand pill (ሰከላ ስፖርት | Sekela Sport) + social icons/handle."""
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

        # Top-left status pill & league label (keeps center of photo clean)
        fe_p, fl_p = f_eth(34), _latin_font(32)
        pw = int(_measure(title, fe_p, fl_p)) + 52
        d.rounded_rectangle([margin, 38, margin + pw, 98], radius=30, fill=th["gold"] + (250,))
        _draw_mixed(d, margin + 26, 80, title, fe_p, fl_p, (16, 18, 16, 255))
        lg_txt = f"{LEAGUE_AM.get(m['league'], m['league_name'])} · {m['league_name']}"
        fe_l, fl_l = f_eth(28), _latin_font(26)
        _draw_bold_shadow(d, margin, 142, lg_txt, fe_l, fl_l, (240, 244, 242, 245))

        # Bottom scoreboard panel (anchored near bottom 35%)
        badge_h = 56
        badge_y = H - 46 - badge_h
        scorers = _scorer_lines(m)
        extra_lines = scorers + ([line1] if (line1 and not scorers) else []) + ([line2] if line2 else [])

        # Measure score + team names cleanly in 3-4-3 horizontal layout
        score_str = f"{m['hs']}  -  {m['as']}"
        f_sc_e, f_sc_l = f_eth(106), _latin_font(106)
        sc_w = _measure(score_str, f_sc_e, f_sc_l)
        col_w = (W - 2 * margin - sc_w - 48) / 2

        home_name = team_am(m["home"])
        away_name = team_am(m["away"])
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

        # Vertical coordinates for scoreboard + scorers
        extra_h = len(extra_lines) * 44
        box_h = 190 + extra_h
        box_y1 = badge_y - 28
        box_y0 = box_y1 - box_h
        d.rounded_rectangle([margin, box_y0, W - margin, box_y1], radius=24,
                            fill=(8, 22, 18, 215), outline=th["gold"] + (180,), width=2)

        # Center score
        sc_x = (W - sc_w) / 2
        sc_base = box_y0 + 122
        _draw_bold_shadow(d, sc_x, sc_base, score_str, f_sc_e, f_sc_l, th["gold"] + (255,))

        # Home team (left column, centered)
        left_cx = margin + 16 + col_w / 2
        for idx, ln in enumerate(hl):
            lw = _measure(ln, fe_t, fl_t)
            y_ln = box_y0 + (78 if len(hl) == 2 else 98) + idx * (t_fs + 10)
            _draw_bold_shadow(d, left_cx - lw / 2, y_ln, ln, fe_t, fl_t, (255, 252, 245, 255))

        # Away team (right column, centered)
        right_cx = W - margin - 16 - col_w / 2
        for idx, ln in enumerate(al):
            lw = _measure(ln, fe_t, fl_t)
            y_ln = box_y0 + (78 if len(al) == 2 else 98) + idx * (t_fs + 10)
            _draw_bold_shadow(d, right_cx - lw / 2, y_ln, ln, fe_t, fl_t, (255, 252, 245, 255))

        # Goal scorers with minutes below score
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

        # Bottom brand pill + social footer
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
    """Build a Sekela Sport 1080x1350 draft and send it to Henok for approval."""
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
    """Schedule post for the East-Africa-Time day `day_offset` days from now."""
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


def normalize_highlight(ffmpeg, src, out, max_sec):
    for crf in ("26", "32"):
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
        facts = (f"Video title: {title}\nDescription: {desc}\nPublished by: {source_name}\nVideo link: {url}")
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
        old = [k for k, v in video_drafts.items() if now - v["ts"] > max_age]
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
    if d["tg"] or d["fb"]:
        if not d.get("learned"):
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
# YouTube module: analytics (Data API) + approval-gated upload
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
        raise YTError("ለዚህ ቶክን ቻናል አልተገኘም (ትክክለኛውን ቻናል/Brand account መርጠህ ቶክኑን ፍጠር)")
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
            log.info("YouTube Analytics API HTTP %s: %s", r.status_code, r.text[:120])
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
def tg_member_count(channel=None):
    fn = getattr(bot, "get_chat_member_count", None) or getattr(bot, "get_chat_members_count", None)
    try:
        return int(fn(channel or TARGET_CHANNEL)) if fn else None
    except Exception as e:
        log.info("telegram member count unavailable: %s", e)
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
            put(76, 912, "Create a never-expiring Page Access Token", 20, MUTED)

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
        except Exception as e:
            log.info("stats history not saved: %s", e)


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
            log.info("insight %s unavailable: %s", metric, e)
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
            out["_notes"].append("Engagement ከፖስቶች ድምር (reactions+comments+shares) ነው የተሰላው")
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
        sec["facts"].append(f"Facebook page followers: {t['followers']:,}" + (f", change {_sign(d[0])} over {d[1]} day(s)" if d else ""))
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
        sec["facts"].append(f"Facebook posts in {win_txt}: {t['posts_n']} posts, {t['reactions']} reactions, "
                            f"{t['comments']} comments, {t['shares']} shares")
    posts = fb["posts"]
    if tf == "last":
        if posts:
            p0 = posts[0]
            sec["kpis"] = sec["kpis"][:1] + [("Latest post", p0["date"], ""), ("Reactions", f"{p0['reactions']:,}", ""),
                                             ("Comments", f"{p0['comments']:,}", ""), ("Shares", f"{p0['shares']:,}", "")]
            sec["bars_title"] = "Latest posts — reactions + comments + shares"
            sec["bars"] = [(f"{p['date'][5:]} {p['text']}", p["reactions"] + p["comments"] + p["shares"]) for p in posts]
            sec["facts"].append(f"Latest Facebook post ({p0['date']}, \"{p0['text']}\"): {p0['reactions']} reactions, "
                                f"{p0['comments']} comments, {p0['shares']} shares")
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
    if not any(k in t for k in ("reach", "views", "engagement", "posts_n", "followers")):
        sec["notes"].append("Facebook ቁጥሮች አልተገኙም — Page token ላይ read_insights + pages_read_engagement ፈቃድ ያስፈልጋል")
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
            sec["facts"].append(f"Latest video «{v0['title'][:60]}» ({v0['published']}): {v0['views']:,} views, "
                                f"{v0['likes']:,} likes, {v0['comments']:,} comments; channel has {subs_txt} subscribers")
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
        sec["facts"].append(f"YouTube {yt['title']} last {days} day(s): {ana['views']:,} views, {ana['minutes'] / 60:,.1f} watch hours, "
                            f"net subscribers {ana['gained'] - ana['lost']:+,}, {ana['likes']:,} likes, {ana['comments']:,} comments; "
                            f"total subscribers {subs_txt}")
        if len(ana["series"]) >= 3:
            sec["chart"], sec["bars_title"], sec["bars"] = "v", "Daily views", ana["series"][-30:]
    else:
        sec["kpis"].append(("Total views", f"{yt['views']:,}", _sign(dv[0]) if dv else ""))
        sec["kpis"].append(("Videos", f"{yt['videos']:,}", ""))
        cutoff = (addis_now() - timedelta(days=days)).strftime("%Y-%m-%d")
        new = [v for v in yt["recent"] if v["published"] >= cutoff]
        sec["kpis"].append((f"New videos ({days}d)", str(len(new)), ""))
        sec["facts"].append(f"YouTube {yt['title']}: {subs_txt} subscribers, {yt['views']:,} total views, {yt['videos']:,} videos"
                            + (f"; views change {_sign(dv[0])} over {dv[1]} day(s)" if dv else "")
                            + (f"; subscribers change {_sign(ds[0])} over {ds[1]} day(s)" if ds else "")
                            + f"; {len(new)} new video(s) in the last {days} day(s)")
        sec["notes"].append("ዕለታዊ የ YouTube Analytics (yt-analytics.readonly ፈቃድ) ስለሌለ ድምር ቁጥሮች + ታሪካዊ ለውጥ ነው የሚታየው")
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
                    row_h = min(46, (area_h) // max(1, len(rows)))
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
    "📈 ምን ጥሩ እየሠራ ነው?  - 2-3 '•' bullets: the strongest numbers, growth, best post or topic.\n"
    "💡 ምን ይሻሻል?  - 2-3 '•' bullets: the weakest numbers or gaps, each with ONE concrete action (post format, topic, "
    "timing, hook) Henok can try next.\n"
    "Never invent or estimate a number; if a metric is missing say so in one short line. Speak to ሄኖክ in masculine "
    "singular (አንተ). Plain text, no markdown, at most 150 words.\n\nPeriod: {period}\nNUMBERS:\n{facts}"
)


def an_insights(secs, period):
    facts = "\n".join(f for sec in secs for f in sec["facts"])
    try:
        lf = learn_summary_facts()
        if lf and facts:
            facts += "\n" + lf
    except NameError:
        pass
    if not facts:
        return "ℹ️ ለዚህ ምርጫ ቁጥሮች አልተገኙም — ከላይ ያሉትን ማስታወሻዎች (ግንኙነት/ፈቃድ) አረጋግጥ።"
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
            except Exception as e:
                log.warning("ffmpeg %s conversion crashed: %s", ext, e)
                continue
            if r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 800:
                with open(dst, "rb") as f:
                    out = f.read()
                log.info("voice converted to %s: %d -> %d bytes", ext, len(data), len(out))
                return out, mime
            log.warning("ffmpeg %s conversion failed: %s", ext, r.stderr.decode("utf-8", "ignore")[-200:])
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
                log.warning("OpenRouter audio %s HTTP %s: %s", model, r.status_code, r.text[:140])
                continue
            txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if txt.strip():
                log.info("voice transcribed by OpenRouter %s", model)
                return clean_md(txt)
        except Exception as e:
            log.warning("OpenRouter audio %s failed: %s", model, short_reason(e, 100))
    return None


def analyze_voice(data, mime):
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
            log.warning("voice transcript attempt (%s, %d bytes) failed: %s", bmime, len(blob), e)
    if not transcript and mp3:
        transcript = openrouter_transcribe(mp3, mp3_mime)
    if not transcript:
        raise last_err or GeminiError("voice transcription failed")
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
                    prune(video_cache, 2)
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
    except Exception as e:
        log.info("canva tokens not saved: %s", e)


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
        log.error("canva code exchange failed: %s", e)
        return False, "Canva ቶክን ማግኘት አልተቻለም: " + short_reason(e, 160)
    try:
        who = canva_profile()
    except Exception:
        who = ""
    try:
        bot.send_message(OWNER_ID, "🎨 Canva ተገናኝቷል ✅" + (f" — {who}" if who else "") +
                         "\n/ai_status ላይ ሁኔታውን ማየት ትችላለህ።")
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
            raise RuntimeError("Canva ገና አልተፈቀደም — በ Telegram ላይ /canva ብለህ ግባ")
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
# Multi-engine AI image & logo studio (/image)
# ----------------------------------------------------------------------------
image_jobs = {}
IMAGE_TRIGGERS = ["ሎጎ", "logo", "ምስል ስራ", "ምስል ሥራ", "ምስል ሰራ", "ምስል አዘጋጅ", "ምስል ፍጠር", "ሎጎ ስራ",
                  "generate image", "generate an image", "image of"]
LOGO_WORDS = ["ሎጎ", "logo", "emblem", "brand mark"]
IMAGE_PROMPT_SYSTEM = (
    "You write prompts for AI image generators. Turn the user's request (Amharic or English) into ONE detailed English "
    "prompt of 40-70 words: subject, style, composition, colours, lighting, mood. If it is a LOGO: flat vector logo, "
    "simple bold shapes, centred on a plain background, limited colour palette, professional brand identity, no tiny "
    "details, and NO text or letters inside the image (text is added later in Canva). If the request mentions Sekela "
    "Media use deep crimson and gold; for Sekela Sport use emerald green and gold. Output only the prompt."
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


def gen_pollinations(prompt, models, seed):
    last = None
    for model in models:
        url = (f"https://image.pollinations.ai/prompt/{quote(prompt[:600], safe='')}"
               f"?model={model}&width=1024&height=1024&nologo=true&seed={seed}")
        for attempt in range(2):
            try:
                r = requests.get(url, timeout=100)
                if r.status_code == 200 and _valid_image(r.content, r.headers.get("Content-Type", "")):
                    return r.content
                last = f"HTTP {r.status_code}"
            except Exception as e:
                last = short_reason(e, 80)
            time.sleep(3)
        log.info("Pollinations %s failed: %s", model, last)
    raise RuntimeError(f"Pollinations {models}: {last}")


def gen_gemini_image(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("no GEMINI_API_KEY")
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    errs = []
    for model in [m.strip() for m in os.environ.get(
            "GEMINI_IMAGE_MODELS", "gemini-2.5-flash-image,gemini-2.0-flash-preview-image-generation").split(",") if m.strip()]:
        try:
            r = requests.post(f"{GEMINI_BASE}/models/{model}:generateContent", headers=headers, timeout=120, json={
                "contents": [{"role": "user", "parts": [{"text": "Generate an image: " + prompt}]}],
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
    for model in [m.strip() for m in os.environ.get(
            "GEMINI_IMAGEN_MODELS", "imagen-4.0-fast-generate-001,imagen-4.0-generate-001").split(",") if m.strip()]:
        try:
            r = requests.post(f"{GEMINI_BASE}/models/{model}:predict", headers=headers, timeout=120, json={
                "instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1, "aspectRatio": "1:1"}})
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
    raise RuntimeError("Gemini image: " + "; ".join(errs))


def gen_huggingface(prompt):
    if not HF_TOKEN:
        raise RuntimeError("no HF_TOKEN")
    last = None
    for url in ("https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
                "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {HF_TOKEN}", "Accept": "image/png"},
                              json={"inputs": prompt}, timeout=120)
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
                            "response_format": "b64_json"}, timeout=120)
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
    eng = [("Pollinations · flux", lambda: gen_pollinations(prompt, ["flux"], seed)),
           ("Pollinations · turbo", lambda: gen_pollinations(prompt, ["turbo", "flux-realism"], seed + 1))]
    if GEMINI_API_KEY:
        eng.append(("Gemini Imagen", lambda: gen_gemini_image(prompt)))
    if HF_TOKEN:
        eng.append(("HuggingFace · FLUX.1-schnell", lambda: gen_huggingface(prompt)))
    if TOGETHER_API_KEY:
        eng.append(("Together · FLUX.1-schnell", lambda: gen_together(prompt)))
    if len(eng) < 4:
        eng.append(("Pollinations · flux (ስሪት 2)", lambda: gen_pollinations(prompt, ["flux"], seed + 77)))
    if len(eng) < 4:
        eng.append(("Pollinations · turbo (ስሪት 2)", lambda: gen_pollinations(prompt, ["turbo"], seed + 99)))
    return eng[:5]


def _tg_safe(data):
    if len(data) < 9_000_000 or not PIL_OK:
        return data
    out = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def run_image_studio(chat_id, request, prompt=None):
    start = time.time()
    ack = send_progress(chat_id, "🎨 ሄኖክ፣ ምስሎቹን በበርካታ AI ሞተሮች እያዘጋጀሁ ነው (ከ30-90 ሰከንድ ይወስዳል)...")
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
                        results.append((label, fut.result(timeout=200)))
                    except Exception as e:
                        errors.append(f"{label}: {short_reason(e, 90)}")
                        log.warning("image engine failed - %s", errors[-1])
        if not results:
            deliver(chat_id, ack, "ሄኖክ፣ ምስሎቹን አሁን መስራት አልተቻለም 😕 ሁሉም ሞተሮች ተጨናንቀዋል — ከ1-2 ደቂቃ በኋላ ደግመህ ሞክር።\n\n"
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
                m = media[0]
                bot.send_photo(chat_id, m.media, caption=m.caption)
        except Exception as e:
            log.warning("media group failed (%s) - sending numbered photos", e)
            for i, (label, data) in enumerate(results):
                f = io.BytesIO(_tg_safe(data))
                f.name = f"variation-{i + 1}.jpg"
                bot.send_photo(chat_id, f, caption=f"{NUM_EMOJI[i]} {label}")
        kb = types.InlineKeyboardMarkup(row_width=5)
        kb.add(*[types.InlineKeyboardButton(NUM_EMOJI[i], callback_data=f"im:{jid}:{i}") for i in range(len(results))])
        kb.add(types.InlineKeyboardButton("🔁 ሌሎች ስሪቶች", callback_data=f"imr:{jid}"))
        note = ("\n💡 ሎጎ ላይ የአማርኛ ፊደል AI በትክክል አይጽፍም — ስሙን በ Canva ላይ ጨምር።" if is_logo else "")
        fail = (f"\n⚠️ {len(errors)} ሞተር አልሰራም (ሌሎቹ ሰርተዋል)።" if errors else "")
        deliver(chat_id, None, f"✅ {len(results)} ስሪቶች ዝግጁ ናቸው — ምርጡን ቁጥር ንካ 👇{note}{fail}\n\n{took(start)}", kb)
    except Exception as e:
        log.error("image studio failed: %s", e)
        deliver(chat_id, ack, f"ሄኖክ፣ ምስሎቹን አሁን ማዘጋጀት አልቻልኩም 😅 ({short_reason(e)})")


def pick_image(chat_id, jid, idx):
    with state_lock:
        job = image_jobs.get(jid)
    if not job or idx >= len(job["results"]):
        bot.send_message(chat_id, "ሄኖክ፣ ይህ ስራ ከማህደረ ትውስታ ተሰርዟል — /image ብለህ እንደገና ጠይቀኝ 🙏")
        return
    label, data = job["results"][idx]
    f = io.BytesIO(data)
    f.name = f"sekela-image-{idx + 1}.png" if data[:4] == b"\x89PNG" else f"sekela-image-{idx + 1}.jpg"
    bot.send_document(chat_id, f, caption=f"{NUM_EMOJI[idx]} ተመርጧል — {label}\n(ሙሉ ጥራት ፋይል፤ ለ Canva ዝግጁ)")


def regenerate_image(chat_id, jid):
    with state_lock:
        job = image_jobs.get(jid)
    if not job:
        bot.send_message(chat_id, "ሄኖክ፣ ይህ ስራ ከማህደረ ትውስታ ተሰርዟል — /image ብለህ እንደገና ጠይቀኝ 🙏")
        return
    run_image_studio(chat_id, job["request"], prompt=job["prompt"])


# ----------------------------------------------------------------------------
# Commands (instant, private-owner only)
# ----------------------------------------------------------------------------
HELP_TEXT = (
    "ሰላም ሄኖክ! 👋🔥 እኔ የአንተ የግል ረዳት ነኝ — 24/7 ዝግጁ።\n\n"
    "ምን ላደርግልህ እችላለሁ:\n"
    "📰 /news — ዋና ዋና ዜናዎች (ከታማኝ ምንጮች፣ Google Trends እና ሶሻል ሚዲያ) → ቁልፍ ስትነካ 1080x1350 ካርድ + ፖስት\n"
    "🔔 /breaking — ሰበር ዜና አሁን ፈልግ (ቦቱ በራሱም ይፈትሻል እና ያሳውቅሃል)\n"
    "☕ /coffeetime — ሰከላ ኮፊ ታይም: አስገራሚ እና ቫይራል ታሪኮች በልዩ 1080x1350 Coffee-Gold ካርድ\n"
    "⚽ /sport — የስፖርት ዜና፣ ዝውውሮች እና ቀጥታ ጎሎች (3-4-3 ስታይል 1080x1350 ካርድ)\n"
    "📝 /post — ካርድ + ፖስት (📰 ሰከላ ሚዲያ / ☕ ኮፊ ታይም / ⚽ ሰከላ ስፖርት) → ✅ ስትነካ ወደ Telegram እና Facebook\n"
    "📊 /analytics — በቁልፍ ብቻ ምረጥ: ብራንድ/YouTube + ዛሬ/7/30 ቀን/የቅርብ ቪዲዮ → PNG ዳሽቦርድ + ግንዛቤ\n"
    "🎨 /image — ሎጎ ወይም ምስል (ወይም «ሎጎ ስራልኝ» ብለህ ጻፍ) → 4-5 ስሪቶች ከበርካታ AI ሞተሮች → ምርጡን ምረጥ\n"
    "🖌 /canva — ከ Canva ጋር ግባ እና የቅርብ ዲዛይኖችን እይ\n"
    "🧠 /rules — ቋሚ ህጎችህን እይ/ሰርዝ፤ ወይም በቀጥታ ጻፍ: «ከዛሬ ጀምሮ የኢኮኖሚ ዜና አብዛ»\n"
    "📅 /schedule — የዛሬና የነገ ጨዋታዎች ፕሮግራም ረቂቅ\n"
    "🎬 /highlight — የጎል ቪዲዮ ሊንክ (≤60 ሰከንድ) → የቪዲዮ ረቂቅ\n"
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


@bot.message_handler(commands=["coffeetime", "coffee"])
@private_owner_only
def cmd_coffeetime(message):
    arg = (message.text or "").split(None, 1)
    custom = arg[1].strip() if len(arg) > 1 else None
    run_coffee_auto(message.chat.id, custom_topic=custom)


@bot.message_handler(commands=["cancel"])
@private_owner_only
def cmd_cancel(message):
    with state_lock:
        awaiting_topic.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "እሺ ሄኖክ፣ ተሰርዟል ✅")


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
    bot.send_message(chat_id, "🎨 እሺ ሄኖክ — ምን ልሥራልህ? የፈለግከውን ምስል ወይም ሎጎ በአማርኛ ወይም በእንግሊዝኛ ግለጽልኝ "
                              "(ለምሳሌ «የሰከላ ስፖርት ሎጎ፣ እግር ኳስ እና አረንጓዴ-ወርቃማ ቀለም»)። ለመሰረዝ /cancel")


@bot.message_handler(commands=["rules", "learn"])
@private_owner_only
def cmd_rules(message):
    chat_id = message.chat.id
    parts = (message.text or "").split(None, 2)
    if len(parts) >= 2 and parts[1].lower() == "add" and len(parts) == 3:
        r = learn_add_rule(parts[2])
        bot.send_message(chat_id, f"✅ ህጉ ተቀምጧል — ከዚህ በኋላ በራሴ እተገብረዋለሁ፤ ኮድ መቀየር አያስፈልግም።\n«{r['rule']}»")
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
            bot.send_message(chat_id, "ሄኖክ፣ በተከታተልናቸው ሊጎች ዛሬ/ነገ ጨዋታ አላገኘሁም (ወይም ESPN አልተገኘም)።")
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
        bot.send_message(chat_id, "🎬 እሺ ሄኖክ — የጎል/ሃይላይት ቪዲዮ (YouTube ወዘተ) ሊንክ ላክልኝ፤ እስከ 60 ሰከንድ መሆን አለበት። ለመሰረዝ /cancel")
        return
    run_highlight(chat_id, m.group(0))


def run_highlight(chat_id, url):
    ack = send_progress(chat_id, "🎬 ቪዲዮውን እያወረድኩ እና የአማርኛ መግለጫ እያዘጋጀሁ ነው (ከ1-2 ደቂቃ)...")
    ok, msg = prepare_video_draft(url, chat_id=chat_id)
    if ok:
        drop_message(chat_id, ack)
    else:
        deliver(chat_id, ack, f"ሄኖክ፣ ቪዲዮውን ማዘጋጀት አልተቻለም 😕\n{msg}")


@bot.message_handler(commands=["canva"])
@private_owner_only
def cmd_canva(message):
    chat_id = message.chat.id
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
            lines.append(f"ℹ️ ዲዛይኖችን ማምጣት አልተቻለም ({short_reason(e, 100)}) — CANVA_SCOPES ላይ design:meta:read መኖሩን አረጋግጥ።")
    lines.append("\nℹ️ Canva ቶክን የሚቀመጠው በ Render ፋይል ላይ ነው — ሰርቨሩ እንደገና ከጀመረ /canva ብለህ አንድ ጊዜ ግባ።")
    bot.send_message(chat_id, "\n".join(lines), reply_markup=kb, disable_web_page_preview=True)


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


def _manual_hunt(message, brand):
    start = time.time()
    chat_id = message.chat.id
    what = "ሰበር ዜናዎችን" if brand == "media" else "የስፖርት ዜናዎችን እና ቀጥታ ጎሎችን"
    ack_id = send_progress(chat_id, f"⏳ ሄኖክ፣ {what} እየፈተሽኩ ነው (ባለፉት ሰዓታት፣ አስፈላጊ ከሆነ 24 ሰዓት) — ከ40-90 ሰከንድ ይወስዳል...")
    status = hunt_once(manual=True, min_importance=5, brand=brand)
    if status == "sent":
        drop_message(chat_id, ack_id)
        return
    msgs = {
        "none": "ሄኖክ፣ አሁን አዲስ ያልቀረበ ዜና አልተገኘም — ባለፉት 48 ሰዓታት የተላኩልህን ዜናዎች ደግሜ አልልክም። ሲገኝ በራሴ አሳውቅሃለሁ 🔔",
        "failed": "ሄኖክ፣ ዜናውን አሁን ማዘጋጀት አልቻልኩም 😅 ጥቂት ቆይተህ እንደገና ሞክር።",
        "busy": "ሄኖክ፣ ፍተሻው አሁን እየተካሄደ ነው — ጥቂት ቆይተህ ሞክር።",
        "nokey": "GEMINI_API_KEY አልተሞላም — ዜና መፈለግ አልችልም።",
        "cap": "የዛሬው የዜና ገደብ ደርሷል።",
    }
    deliver(chat_id, ack_id, msgs.get(status, msgs["failed"]) + "\n\n" + took(start))


@bot.message_handler(commands=["breaking", "hunt"])
@private_owner_only
def cmd_breaking(message):
    _manual_hunt(message, "media")


@bot.message_handler(commands=["sport", "goals"])
@private_owner_only
def cmd_sport(message):
    _manual_hunt(message, "sport")


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
            lines.append(f"  ❌ ቦቱ አድሚን አይደለም ወይም 'Post Messages' ፈቃድ የለውም (status: {status})")
    except Exception as e:
        lines.append(f"  ❌ ቻናሉን ማግኘት አልተቻለም: {short_reason(e, 120)}")
        lines.append("  💡 ቻናሉ public መሆኑን፣ username በትክክል መጻፉን እና ቦቱ አድሚን መሆኑን አረጋግጥ።")
    return lines


def _fb_status_lines(brand):
    b = BRANDS[brand]
    lines = [f"📘 {brand_label(brand)} — Facebook Page: {b['fb_name']} (id {b['fb_id']})"]
    if not b["fb_token"]:
        env = "FB_PAGE_ACCESS_TOKEN" if brand == "media" else "FB_SPORT_ACCESS_TOKEN"
        return lines + [f"  ⚪ {env} አልተሞላም — ወደ ፌስቡክ አይለጠፍም"]
    try:
        me = _fb_request("GET", "me", params={"fields": "name,id"}, timeout=30, brand=brand)
        lines.append(f"  ✅ ተገናኝቷል: {me.get('name')} (id {me.get('id')})")
        if me.get("id") and str(me["id"]) != str(b["fb_id"]):
            lines.append("  ⚠️ ቶክኑ የሌላ ገጽ ነው — የገጹ ID እና ቶክኑ አይዛመዱም")
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
            tag = "ዋና (Shorts upload)" if i == 0 else f"ቻናል {i + 1}"
            try:
                st = yt_channel_stats(i)
                lines.append(f"  ✅ {st['title']} — {tag} ({st['subs']:,} subs · {st['videos']:,} videos)")
            except Exception as e:
                lines.append(f"  ❌ {ch['name']} — {tag}: {short_reason(e, 130)}")
    lines.append("  ✅ ffmpeg ዝግጁ (Shorts መቁረጥ + ድምፅ ወደ mp3 መቀየር ይሰራል)" if get_ffmpeg()
                 else "  ❌ ffmpeg የለም — requirements.txt ላይ imageio-ffmpeg ጨምር")
    return lines


def _engine_status_lines():
    with _learn_lock:
        L = learn_load()
        n_rules, n_ev, n_pend = len(L["rules"]), len(L["events"]), len([p for p in L["pending"] if not p["done"]])
        bk = (L.get("backup") or {}).get("ts", 0)
    lines = ["🧠 ራስ-ትምህርት:", f"  ✅ {n_rules} ህጎች · {n_ev} ውሳኔዎች · {n_pend} ፖስቶች ውጤታቸው እየተጠበቀ ነው",
             "  💾 Backup: " + (datetime.fromtimestamp(bk, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if bk else "ገና አልተላከም (ህግ ሲጨምር ይላካል)"),
             "⚽ Sport Engine:"]
    try:
        ms = refresh_schedule()
        lines.append(f"  ✅ ESPN ተገናኝቷል — {len(ms)} ጨዋታዎች (ትናንት-ነገ) በ {len(sport_leagues())} ሊጎች" if ms else
                     "  ⚠️ ESPN ጨዋታ አላመጣም (ዛሬ ጨዋታ ላይኖር ይችላል ወይም አልተገናኘም)")
    except Exception as e:
        lines.append(f"  ❌ ESPN: {short_reason(e, 100)}")
    lines.append(f"  🎬 Highlight ቻናሎች: {', '.join(n for n, _ in highlight_channels()) or 'አልተዋቀሩም'}")
    return lines


def _image_status_lines():
    lines = ["🎨 Image Studio (/image):", "  ✅ Pollinations flux + turbo (ያለ ቁልፍ)",
             ("  ✅ Gemini Imagen (GEMINI_API_KEY)" if GEMINI_API_KEY else "  ⚪ Gemini Imagen — GEMINI_API_KEY የለም"),
             ("  ✅ Hugging Face FLUX.1-schnell (HF_TOKEN)" if HF_TOKEN else "  ⚪ Hugging Face — HF_TOKEN አልተሞላም (አማራጭ)"),
             ("  ✅ Together FLUX.1-schnell (TOGETHER_API_KEY)" if TOGETHER_API_KEY else "  ⚪ Together — TOGETHER_API_KEY አልተሞላም (አማራጭ)")]
    return lines


@bot.message_handler(commands=["ai_status"])
@private_owner_only
def cmd_ai_status(message):
    start = time.time()
    chat_id = message.chat.id
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
        lines.append("\n🖼 የካርድ ርዕስ ፊደል (Ethiopic font): " + ("✅ ተገኝቷል (1080x1350)" if font else "❌ የለም — NotoSansEthiopic-Bold.ttf ኮሚት አድርግ"))
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
        if data.startswith("cat:"):
            _, mode, key = data.split(":", 2)
            if key == "custom":
                with state_lock:
                    awaiting_topic[uid] = mode
                bot.edit_message_text(f"✅ የመረጥከው: {CUSTOM_LABEL}", chat_id, msg_id)
                bot.send_message(chat_id, "እሺ ሄኖክ ✍️ ርዕሱን ጻፍልኝ (ለመሰረዝ /cancel):")
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
                bot.send_message(chat_id, "ሄኖክ፣ ይህ መረጃ ጊዜው አልፏል — /news ብለህ እንደገና ጠይቀኝ 🙏")
                return
            run_post(chat_id, item["label"], item["topic"], brand=item.get("brand"), segment=item.get("segment"))

        elif data.startswith("st:"):
            _, nid, idx = data.split(":", 2)
            with state_lock:
                item = news_cache.get(nid)
            if not item or not item["stories"] or int(idx) >= len(item["stories"]):
                bot.send_message(chat_id, "ሄኖክ፣ ይህ መረጃ ጊዜው አልፏል — /news ብለህ እንደገና ጠይቀኝ 🙏")
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
                bot.send_message(chat_id, "✏️ እሺ ሄኖክ፣ የፖስቱን አዲስ ሙሉ ጽሑፍ ላክልኝ — በቻናሉ ላይ እተካዋለሁ (ለመሰረዝ /cancel)።")
            else:
                bot.send_message(chat_id, "ሄኖክ፣ ይህ ፖስት ከማህደረ ትውስታ ተሰርዟል — በቻናሉ ላይ በእጅ አርም 🙏")

        elif data.startswith("vc:"):
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
        bot.send_message(chat_id, greeting_reply())
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
        bot.send_message(chat_id, f"✅ ህጉ ለዘላለም ተቀምጧል — ኮድ መቀየር አያስፈልግም።\n«{r['rule']}»{extra}\n\n/rules ብለህ ማየት ወይም መሰረዝ ትችላለህ።")
        return

    if has_any(text, IMAGE_TRIGGERS):
        run_image_studio(chat_id, text)
        return

    if has_any(text, COFFEE_WORDS):
        custom = None if len(text.split()) <= 4 else text
        run_coffee_auto(chat_id, custom_topic=custom)
        return

    mode = "post" if has_any(text, POST_WORDS) else ("news" if has_any(text, NEWS_WORDS) else None)
    if mode:
        if len(text.split()) <= 5:
            show_categories(chat_id, mode)
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
        log.error("TELEGRAM_BOT_TOKEN is missing. Set it in Render environment variables.")
        time.sleep(5)
        sys.exit(1)
    if not OPENROUTER_API_KEY:
        log.warning("OPENROUTER_API_KEY missing - Gemini will write everything.")
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY missing - live research and audio/video analysis are disabled.")
    if not PIL_OK:
        log.warning("Pillow missing - cards will be sent without the baked headline (pip install Pillow).")

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
            types.BotCommand("news", "ዋና ዋና ዜናዎች (24h ትኩስ + Google Trends)"),
            types.BotCommand("breaking", "ሰበር ዜና አሁን ፈልግ"),
            types.BotCommand("coffeetime", "☕ ሰከላ ኮፊ ታይም — ቫይራል ታሪክ"),
            types.BotCommand("sport", "የስፖርት ዜና እና ቀጥታ ጎሎች"),
            types.BotCommand("post", "1080x1350 ካርድ + ፖስት (ሚዲያ / ኮፊ ታይም / ስፖርት)"),
            types.BotCommand("analytics", "ዳሽቦርድ — ብራንድ እና ጊዜ በቁልፍ ምረጥ"),
            types.BotCommand("image", "ሎጎ / ምስል — 4-5 ስሪቶች ከብዙ AI ሞተሮች"),
            types.BotCommand("canva", "ከ Canva ጋር ግባ"),
            types.BotCommand("rules", "ቋሚ ህጎች እና ራስ-ትምህርት"),
            types.BotCommand("schedule", "የዛሬ/ነገ ጨዋታዎች ፕሮግራም"),
            types.BotCommand("highlight", "የጎል ቪዲዮ ሊንክ → ረቂቅ"),
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
