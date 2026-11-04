# -*- coding: utf-8 -*-
"""
HENOK AI Telegram Agent
- Telegram polling + Render health-check web server
- Gemini 3.8 Flash primary model; OpenRouter optional fallback
- Amharic voice-note transcription, summary and action items
- Content package workflow with approval before publishing
"""

import os
import re
import json
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, List, Dict, Any

import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from video_producer import produce_henok_ai_video


# ============================================================
# Configuration
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

# Stable model IDs. You can override these in Render Environment.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL", "google/gemini-2.5-flash"
).strip()

TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID", "-1003098019114").strip()
OWNER_ID = os.getenv("OWNER_ID", "").strip()

# Telegram Bot API download limit is about 20 MB. Keep the inline
# Gemini request below 20 MB after Base64 expansion.
MAX_AUDIO_BYTES = 14 * 1024 * 1024
TELEGRAM_TEXT_LIMIT = 3900
HISTORY_LIMIT = 20
HTTP_TIMEOUT = (10, 55)
VIDEO_LOCK = threading.Lock()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is missing. Add it in Render Environment Variables."
    )

# Do not log tokens or API keys.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("henok-ai")

bot = telebot.TeleBot(
    TELEGRAM_BOT_TOKEN,
    parse_mode=None,
    threaded=True,
    num_threads=4,
)

HTTP = requests.Session()
STATE_LOCK = threading.RLock()

# RAM-only state: conversation history and pending packages reset on redeploy/restart.
HISTORY: Dict[int, List[Dict[str, str]]] = {}
SESSIONS: Dict[int, Dict[str, Any]] = {}
PACKAGE_COUNTER = 0


# ============================================================
# Render health-check server
# ============================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
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
        # Keep Render logs clean; application errors are logged separately.
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthCheckHandler)
        server.daemon_threads = True
        logger.info("Health-check server listening on port %s", port)
        server.serve_forever()
    except Exception:
        logger.exception("Health-check server could not start")


threading.Thread(target=start_health_server, daemon=True).start()


# ============================================================
# Assistant instructions
# ============================================================

SYSTEM_PROMPT = """
You are HENOK AI, a practical executive AI assistant for a digital creator
and digital-marketing professional based in Addis Ababa, Ethiopia.

LANGUAGE:
- Reply primarily in natural, clear, modern Amharic.
- Keep common technical terms in English when that is clearer
  (AI, YouTube, Canva, Hook, CTA, SEO, Reel, Caption, etc.).
- If the user writes in English, reply in English unless asked otherwise.

NORMAL CONVERSATION:
- Answer the actual question directly and helpfully.
- Be concise by default. Give steps only when useful.
- Do not automatically create a social-media package when the user asks
  an ordinary question about marketing, content, or social media.
- Do not show menus unless the user asks to create a post or chooses /post.
- Do not claim to have performed an action that you have not performed.

CONTENT CREATION:
- Only create a full post package when explicitly requested.
- For a post package, provide platform-appropriate copy, a clear Hook,
  useful body text, CTA and relevant hashtags.
- When asked for a design direction, include Canva size, layout,
  visual hierarchy, color palette with HEX codes and a concise image prompt.
- Avoid fabricated statistics, sources, prices or claims.
- If the user has not supplied a topic, ask one clear question.

SAFETY AND APPROVAL:
- Never reveal API keys, bot tokens, environment variables or private data.
- Never publish a post, send a message to a third party, buy anything,
  or perform an external action without the user's explicit approval.
- If uncertain, say what is uncertain instead of inventing an answer.
"""


TOPICS = {
    "edit": ("🎬 ቪዲዮ ኤዲቲንግ Tips", "የቪዲዮ ኤዲቲንግ ፈጣን Tips (Premiere Pro / CapCut)"),
    "free": ("💼 Freelancing", "Freelancing እና ደንበኛ አያያዝ"),
    "motion": ("⚡ Motion Graphics", "After Effects እና Motion Graphics"),
    "color": ("🎨 Color Grading", "Color Grading እና የዲዛይን መርሆዎች"),
    "ai": ("🤖 AI Tools", "አዳዲስ AI Tools ለዲጂታል ፈጣሪዎች"),
    "pick": ("🎲 አንተ ምረጥልኝ", "ተስማሚ የይዘት ርዕስ ራስህ ምረጥ"),
}

STYLES = {
    "dark": ("🖤 Dark + Neon", "Dark Theme with neon blue and purple accents"),
    "clean": ("⚪ Minimalist Clean", "Clean minimalist white, black and neutral palette"),
    "bold": ("🔥 Bold Contrast", "Bold high-contrast yellow and black palette"),
}

PLATFORMS = {
    "tg": ("📢 Telegram", "Telegram post: useful medium-length copy, emojis and clear CTA"),
    "tt": ("🎵 TikTok", "TikTok: short caption, 15-30 second spoken script and hashtags"),
    "ig": ("📸 Instagram", "Instagram: punchy caption, readable line breaks and hashtags"),
    "yt": ("▶️ YouTube Shorts", "YouTube Shorts: title, 30-45 second script and description"),
}


# ============================================================
# Small helpers
# ============================================================

def is_owner(user_id: Optional[int]) -> bool:
    """Publishing is disabled until OWNER_ID is configured in Render."""
    return bool(OWNER_ID and user_id is not None and str(user_id) == OWNER_ID)


def add_history(chat_id: int, role: str, text: str):
    with STATE_LOCK:
        items = HISTORY.setdefault(chat_id, [])
        items.append({"role": role, "text": text})
        del items[:-HISTORY_LIMIT]


def get_history(chat_id: int) -> List[Dict[str, str]]:
    with STATE_LOCK:
        return list(HISTORY.get(chat_id, [])[-HISTORY_LIMIT:])


def safe_send(chat_id: int, text: str, markup=None):
    text = str(text or "").strip()
    if not text:
        text = "ምላሽ አልተገኘም። እባክህ በድጋሚ ሞክር።"

    chunks = [
        text[i:i + TELEGRAM_TEXT_LIMIT]
        for i in range(0, len(text), TELEGRAM_TEXT_LIMIT)
    ]
    for index, chunk in enumerate(chunks):
        bot.send_message(
            chat_id,
            chunk,
            reply_markup=markup if index == len(chunks) - 1 else None,
            disable_web_page_preview=True,
        )


def error_text(response: requests.Response) -> str:
    """Return a short, sanitized API error detail for server logs."""
    try:
        payload = response.json()
        message = (
            payload.get("error", {}).get("message")
            or payload.get("message")
            or str(payload)
        )
    except Exception:
        message = response.text
    return str(message).replace("\n", " ")[:700]


# ============================================================
# Gemini and OpenRouter API calls
# ============================================================

def call_gemini(
    contents: List[Dict[str, Any]],
    timeout: int = 55,
) -> Optional[str]:
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY is not configured")
        return None

    model = GEMINI_MODEL
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{model}:generateContent"
    )
    payload = {
        "systemInstruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": contents,
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }

    for attempt in range(2):
        try:
            response = HTTP.post(
                url,
                headers=headers,
                json=payload,
                timeout=(10, timeout),
            )

            if response.status_code == 200:
                data = response.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    logger.warning("Gemini returned no candidates: %s", data)
                    return None

                parts = (
                    candidates[0]
                    .get("content", {})
                    .get("parts", [])
                )
                answer = "\n".join(
                    part.get("text", "")
                    for part in parts
                    if part.get("text")
                ).strip()

                if answer:
                    return answer

                logger.warning(
                    "Gemini returned an empty answer; finishReason=%s",
                    candidates[0].get("finishReason"),
                )
                return None

            detail = error_text(response)
            logger.error(
                "Gemini HTTP %s for model %s: %s",
                response.status_code, model, detail
            )

            if response.status_code not in (429, 500, 502, 503, 504):
                return None

        except requests.RequestException as exc:
            logger.error("Gemini request failed: %s", type(exc).__name__)
        except (ValueError, KeyError, TypeError):
            logger.exception("Could not parse Gemini response")

        if attempt == 0:
            time.sleep(1.5)

    return None


def call_openrouter(messages: List[Dict[str, str]]) -> Optional[str]:
    if not OPENROUTER_API_KEY:
        return None

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://henok-agent.onrender.com",
        "X-Title": "HENOK AI Telegram Agent",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
    }

    try:
        response = HTTP.post(
            url,
            headers=headers,
            json=payload,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code != 200:
            logger.error(
                "OpenRouter HTTP %s for model %s: %s",
                response.status_code,
                OPENROUTER_MODEL,
                error_text(response),
            )
            return None

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            logger.warning("OpenRouter returned no choices")
            return None

        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("text")
            )

        return str(content).strip() or None

    except requests.RequestException as exc:
        logger.error("OpenRouter request failed: %s", type(exc).__name__)
    except (ValueError, KeyError, TypeError):
        logger.exception("Could not parse OpenRouter response")

    return None


def reply_with_ai(
    chat_id: int,
    user_text: str,
    extra_parts: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    history = get_history(chat_id)

    # Gemini is the primary provider because it can process audio as well as text.
    gemini_contents = []
    for item in history:
        gemini_contents.append({
            "role": "user" if item["role"] == "user" else "model",
            "parts": [{"text": item["text"]}],
        })

    current_parts = list(extra_parts or [])
    if user_text:
        current_parts.append({"text": user_text})

    gemini_contents.append({
        "role": "user",
        "parts": current_parts,
    })

    answer = call_gemini(gemini_contents)
    if answer:
        return answer

    # OpenRouter is a text fallback. Audio is sent only to Gemini.
    if extra_parts:
        logger.warning("Gemini audio request failed; OpenRouter cannot be used as audio fallback")
        return None

    openrouter_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    openrouter_messages.extend(history)
    openrouter_messages.append({"role": "user", "content": user_text})
    return call_openrouter(openrouter_messages)


# ============================================================
# Menus and post package workflow
# ============================================================

def topic_menu():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(*[
        InlineKeyboardButton(label, callback_data=f"topic:{key}")
        for key, (label, _) in TOPICS.items()
    ])
    return keyboard


def style_menu():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(*[
        InlineKeyboardButton(label, callback_data=f"style:{key}")
        for key, (label, _) in STYLES.items()
    ])
    return keyboard


def platform_menu():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(*[
        InlineKeyboardButton(label, callback_data=f"platform:{key}")
        for key, (label, _) in PLATFORMS.items()
    ])
    keyboard.add(
        InlineKeyboardButton("🌐 ለሁሉም", callback_data="platform:all")
    )
    return keyboard


def package_menu(package_id: int, platform_key: str):
    label = (
        "✅ አጽድቅና በHenok Graphics ላይ ለጥፍ"
        if platform_key == "tg"
        else "✅ አጽድቅ"
    )
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton(label, callback_data=f"publish:{package_id}"),
        InlineKeyboardButton("🔄 ሌላ ስሪት", callback_data=f"regenerate:{package_id}"),
        InlineKeyboardButton("❌ ሰርዝ", callback_data=f"cancel:{package_id}"),
    )
    return keyboard


def lock_answer(call, label: str):
    try:
        original = call.message.text or ""
        bot.edit_message_text(
            f"{original}\n\n✅ የተመረጠው: {label}",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
    except Exception:
        logger.debug("Could not edit menu message", exc_info=True)


def make_package(chat_id: int, topic: str, style: str, platform_key: str):
    global PACKAGE_COUNTER

    if platform_key not in PLATFORMS:
        safe_send(chat_id, "የተመረጠው Platform አይታወቅም።")
        return

    bot.send_chat_action(chat_id, "typing")
    platform_description = PLATFORMS[platform_key][1]

    prompt = f"""
ለHenok Graphics ማህበረሰብ ጥራት ያለው የSocial Media Content Package አዘጋጅ።

TOPIC: {topic}
PLATFORM: {platform_description}
DESIGN STYLE: {style}

በትክክል በዚህ ቅደም ተከተል መልስ፦

===POST===
የሚለጠፈውን ጽሑፍ አዘጋጅ። ጠንካራ Hook፣ ጠቃሚ ዋና ይዘት፣ CTA እና ተዛማጅ Hashtags ይኑሩት።
የተመረጠውን Platform የጽሑፍ ርዝመትና ቅርጽ ተከተል።

===CANVA===
🎨 Color Palette: 3-4 HEX colors.
🔤 Overlay Hook: በDesign ላይ የሚታይ አጭር ጽሑፍ።
🖼 Composition: የLayout፣ hierarchy፣ focal point እና visual elements መመሪያ።
📐 Canvas Size: ለተመረጠው Platform ተስማሚ መጠን።
🪄 Canva Search Terms: ለElements እና Templates የሚረዱ ቃላት።

እውነት ያልሆኑ ቁጥሮችን ወይም ምንጭ የሌላቸውን ስታቲስቲክስ አትፍጠር።
"""

    full = reply_with_ai(chat_id, prompt)
    if not full:
        safe_send(
            chat_id,
            "ይቅርታ፣ የContent Package ማዘጋጀት አልተሳካም። "
            "Render Logs ውስጥ የAPI ስህተቱን መመልከት ያስፈልጋል።"
        )
        return

    post = full
    canva = ""

    if "===POST===" in full:
        post = full.split("===POST===", 1)[1]

    if "===CANVA===" in post:
        post, canva = post.split("===CANVA===", 1)
    elif "===CANVA===" in full:
        # Handles responses where the POST marker was omitted.
        before, canva = full.split("===CANVA===", 1)
        post = before.replace("===POST===", "", 1)

    post = post.strip()
    canva = canva.strip()

    with STATE_LOCK:
        PACKAGE_COUNTER += 1
        package_id = PACKAGE_COUNTER
        session = SESSIONS.setdefault(chat_id, {"pending": {}, "pkgs": {}})
        session.setdefault("pkgs", {})[package_id] = {
            "topic": topic,
            "style": style,
            "platform": platform_key,
            "post": post,
            "canva": canva,
        }

    body = (
        f"📢 POST — {PLATFORMS[platform_key][0]}\n\n{post}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎨 CANVA BLUEPRINT\n\n{canva or 'Canva Blueprint አልተመለሰም።'}"
    )
    safe_send(chat_id, body, package_menu(package_id, platform_key))


def advance_package_workflow(chat_id: int):
    with STATE_LOCK:
        session = SESSIONS.setdefault(chat_id, {"pending": {}, "pkgs": {}})
        pending = session.setdefault("pending", {})

        topic = pending.get("topic")
        style = pending.get("style")
        platforms = pending.get("platforms")

    if not topic:
        safe_send(
            chat_id,
            "❓ ስለ ምን ርዕስ ፖስት ላዘጋጅ?\n"
            "ከታች ምረጥ ወይም ርዕሱን በጽሑፍ ጻፍ።",
            topic_menu(),
        )
        return

    if not style:
        safe_send(chat_id, "❓ የCanva Design Style ምረጥ፦", style_menu())
        return

    if not platforms:
        safe_send(chat_id, "❓ ለየትኛው Platform ላዘጋጅ?", platform_menu())
        return

    for platform_key in platforms:
        make_package(chat_id, topic, style, platform_key)

    with STATE_LOCK:
        SESSIONS.setdefault(chat_id, {"pending": {}, "pkgs": {}})["pending"] = {}


def is_post_request(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if normalized in ("/post", "post", "ፖስት"):
        return True

    phrases = (
        "ፖስት አዘጋጅ",
        "ፖስት ፍጠር",
        "ፖስት አድርግ",
        "ይዘት አዘጋጅ",
        "create a post",
        "write a post",
        "make a post",
        "create content",
        "write content",
    )
    return any(phrase in normalized for phrase in phrases)


# ============================================================
# Telegram commands
# ============================================================

@bot.message_handler(commands=["start", "menu"])
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "ሰላም! 👋 እኔ HENOK AI ነኝ።\n\n"
        "• መደበኛ ጥያቄ ጠይቀኝ፤ በቀጥታ እመልሳለሁ።\n"
        "• Voice Note ላክ፤ ጽሑፍ፣ ማጠቃለያና Action Items አዘጋጃለሁ።\n"
        "• ፖስት ለመፍጠር /post ብለህ ጀምር።\n"
        "• 30 ሰከንድ ቪዲዮ ለማዘጋጀት /video ብለህ ጀምር።\n"
        "• /reset የውይይት ታሪክን ያጸዳል።\n"
        "• /id የTelegram IDህን ያሳያል።"
    )


@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(
        message,
        f"Telegram User ID: {message.from_user.id}\n"
        f"Chat ID: {message.chat.id}"
    )


@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    with STATE_LOCK:
        HISTORY.pop(message.chat.id, None)
        SESSIONS.pop(message.chat.id, None)
    bot.reply_to(message, "የዚህ ውይይት ታሪክ ተጸድቷል።")



@bot.message_handler(commands=["video"])
def cmd_video(message):
    """Start the owner-only 30-second Amharic motion-video job."""
    chat_id = message.chat.id
    if not is_owner(message.from_user.id):
        safe_send(
            chat_id,
            "🔒 የVideo generation ፈቃድ የለህም። "
            "በRender Environment ውስጥ OWNER_ID ከ/id ትእዛዝ የሚያሳየውን Telegram User ID ጋር አዛምድ።"
        )
        return

    if not GEMINI_API_KEY:
        safe_send(chat_id, "GEMINI_API_KEY በRender Environment ውስጥ አልተዘጋጀም።")
        return

    if not VIDEO_LOCK.acquire(blocking=False):
        safe_send(chat_id, "⏳ ሌላ ቪዲዮ እየተዘጋጀ ነው። እባክህ እስኪጠናቀቅ ጠብቅ።")
        return

    safe_send(
        chat_id,
        "🎬 HENOK AI ቪዲዮን ማዘጋጀት ጀምሯል። "
        "የአማርኛ Voice-over እና Motion Graphics ይፈጠራሉ፤ ሲጠናቀቅ MP4 እልክልሃለሁ።"
    )

    def worker():
        output_path = None
        try:
            import tempfile
            output_path = os.path.join(
                tempfile.gettempdir(),
                f"henok_ai_{int(time.time())}_{chat_id}.mp4",
            )
            bot.send_chat_action(chat_id, "record_video")
            output_path = produce_henok_ai_video(GEMINI_API_KEY, output_path)
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            if size_mb > 49:
                raise RuntimeError(
                    f"Generated MP4 is {size_mb:.1f} MB; Telegram upload limit safety threshold is 49 MB."
                )
            with open(output_path, "rb") as video_file:
                bot.send_video(
                    chat_id,
                    video_file,
                    caption="✅ HENOK AI — 30s Motion Graphics | 9:16 | Amharic Voice-over",
                    supports_streaming=True,
                    timeout=180,
                )
            logger.info("Video sent to chat %s (%.1f MB)", chat_id, size_mb)
        except Exception as exc:
            logger.exception("HENOK AI video job failed")
            safe_send(
                chat_id,
                "❌ ቪዲዮውን ማዘጋጀት አልተሳካም። "
                f"ስህተቱ: {type(exc).__name__}: {str(exc)[:300]} "
                "Render Logs ውስጥ ዝርዝሩን ተመልከት።"
            )
        finally:
            if output_path and os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    logger.warning("Could not remove temporary video file")
            VIDEO_LOCK.release()

    threading.Thread(target=worker, daemon=True, name="henok-video-job").start()


@bot.message_handler(commands=["post"])
def cmd_post(message):
    with STATE_LOCK:
        SESSIONS[message.chat.id] = {
            "pending": {"topic": "", "style": "", "platforms": []},
            "pkgs": {},
        }
    advance_package_workflow(message.chat.id)


# ============================================================
# Voice and audio handlers
# ============================================================

def process_audio_message(message, audio_object, default_mime: str):
    chat_id = message.chat.id
    file_size = getattr(audio_object, "file_size", None)

    if file_size and file_size > MAX_AUDIO_BYTES:
        safe_send(
            chat_id,
            "የድምፅ ፋይሉ በጣም ትልቅ ነው። "
            "እባክህ ከ14 MB በታች አድርገህ እንደገና ላክ።"
        )
        return

    bot.send_chat_action(chat_id, "typing")

    try:
        file_info = bot.get_file(audio_object.file_id)
        audio_bytes = bot.download_file(file_info.file_path)
    except Exception as exc:
        logger.exception("Telegram audio download failed")
        safe_send(
            chat_id,
            f"የድምፅ ፋይሉን ማውረድ አልተቻለም ({type(exc).__name__})።"
        )
        return

    if not audio_bytes:
        safe_send(chat_id, "የድምፅ ፋይሉ ባዶ ነው። እባክህ እንደገና ላክ።")
        return

    if len(audio_bytes) > MAX_AUDIO_BYTES:
        safe_send(chat_id, "የድምፅ ፋይሉ ከሚፈቀደው መጠን በላይ ነው።")
        return

    mime_type = (
        getattr(audio_object, "mime_type", None)
        or default_mime
        or "audio/ogg"
    ).split(";")[0].strip().lower()

    # Telegram voice notes are normally OGG/Opus. Gemini accepts audio/ogg.
    if not mime_type.startswith("audio/"):
        mime_type = default_mime

    prompt = """
ይህን የድምፅ መልዕክት በጥሞና አዳምጥ።
ተናጋሪው በአማርኛ ከተናገረ በአማርኛ ጻፍ፤ በሌላ ቋንቋ ከሆነ ዋናውን ቋንቋ ጠብቅ።
ተጠቃሚው ትርጉም ካልጠየቀ በስተቀር ቋንቋውን አትቀይር።
ያልተሰማ ቃል አትፍጠር፤ ካልተረጋገጠ [ያልተሰማ] ብለህ ምልክት አድርግ።

በዚህ ቅደም ተከተል መልስ፦
1. TRANSCRIPT: የተነገረውን በተቻለ መጠን ቃል በቃል ጻፍ።
2. SUMMARY: ዋናውን ሀሳብ በጥቂት ነጥቦች አጠቃልል።
3. ACTION ITEMS: ከድምፁ የሚወጡ ተግባራትን ዘርዝር።
ከድምፁ ውስጥ ተግባር ካልተገኘ "የተለየ Action Item የለም" በል።
"""

    extra_parts = [
        {"text": prompt},
        {
            "inlineData": {
                "mimeType": mime_type,
                "data": __import__("base64").b64encode(audio_bytes).decode("ascii"),
            }
        },
    ]

    try:
        answer = call_gemini(
            [{"role": "user", "parts": extra_parts}],
            timeout=80,
        )
    except Exception:
        logger.exception("Unexpected audio processing error")
        answer = None

    if answer:
        add_history(chat_id, "user", "[Voice message]")
        add_history(chat_id, "assistant", answer)
        safe_send(chat_id, f"🎙️ የድምፅ መልዕክት ውጤት\n\n{answer}")
    else:
        safe_send(
            chat_id,
            "የድምፅ መልዕክቱን ማቀናበር አልተሳካም። "
            "GEMINI_API_KEY፣ GEMINI_MODEL እና Render Logs መፈተሽ ያስፈልጋል።"
        )


@bot.message_handler(content_types=["voice"])
def handle_voice(message):
    process_audio_message(message, message.voice, "audio/ogg")


@bot.message_handler(content_types=["audio"])
def handle_audio(message):
    process_audio_message(message, message.audio, "audio/mpeg")


# ============================================================
# Text handler
# ============================================================

@bot.message_handler(content_types=["text"])
def handle_text(message):
    chat_id = message.chat.id
    user_text = (message.text or "").strip()

    if not user_text:
        return

    # If the user is in the middle of the post wizard, treat free text as topic.
    with STATE_LOCK:
        session = SESSIONS.get(chat_id)
        pending = session.get("pending", {}) if session else {}
        waiting_for_topic = bool(session and not pending.get("topic"))

    if waiting_for_topic and not user_text.startswith("/"):
        with STATE_LOCK:
            SESSIONS[chat_id]["pending"]["topic"] = user_text
        advance_package_workflow(chat_id)
        return

    if is_post_request(user_text):
        with STATE_LOCK:
            SESSIONS[chat_id] = {
                "pending": {"topic": "", "style": "", "platforms": []},
                "pkgs": {},
            }
        advance_package_workflow(chat_id)
        return

    bot.send_chat_action(chat_id, "typing")
    response = reply_with_ai(chat_id, user_text)

    if response:
        add_history(chat_id, "user", user_text)
        add_history(chat_id, "assistant", response)
        safe_send(chat_id, response)
    else:
        safe_send(
            chat_id,
            "አሁን ምላሽ ማዘጋጀት አልቻልኩም። "
            "Render Logs ውስጥ Gemini/OpenRouter የAPI ስህተት መፈተሽ ያስፈልጋል።"
        )


# ============================================================
# Inline button callbacks
# ============================================================

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    chat_id = call.message.chat.id
    data = call.data or ""

    with STATE_LOCK:
        session = SESSIONS.setdefault(
            chat_id, {"pending": {}, "pkgs": {}}
        )
        session.setdefault("pending", {})
        session.setdefault("pkgs", {})

    if data.startswith("topic:"):
        key = data.split(":", 1)[1]
        if key not in TOPICS:
            return
        label, topic = TOPICS[key]
        with STATE_LOCK:
            SESSIONS[chat_id]["pending"]["topic"] = topic
        lock_answer(call, label)
        advance_package_workflow(chat_id)
        return

    if data.startswith("style:"):
        key = data.split(":", 1)[1]
        if key not in STYLES:
            return
        label, style = STYLES[key]
        with STATE_LOCK:
            SESSIONS[chat_id]["pending"]["style"] = style
        lock_answer(call, label)
        advance_package_workflow(chat_id)
        return

    if data.startswith("platform:"):
        key = data.split(":", 1)[1]
        if key == "all":
            selected = ["tg", "tt", "ig", "yt"]
            label = "ለሁሉም Platform"
        elif key in PLATFORMS:
            selected = [key]
            label = PLATFORMS[key][0]
        else:
            return

        with STATE_LOCK:
            SESSIONS[chat_id]["pending"]["platforms"] = selected
        lock_answer(call, label)
        advance_package_workflow(chat_id)
        return

    if data.startswith("publish:"):
        package_id = int(data.split(":", 1)[1])

        if not is_owner(call.from_user.id):
            safe_send(
                chat_id,
                "🔒 የPublishing ፈቃድ የለህም። "
                "በRender Environment ውስጥ OWNER_ID በትክክል መዘጋጀቱን አረጋግጥ።"
            )
            return

        with STATE_LOCK:
            package = SESSIONS.get(chat_id, {}).get("pkgs", {}).get(package_id)

        if not package:
            safe_send(chat_id, "ይህ Post Package አልተገኘም። /post ብለህ እንደገና ጀምር።")
            return

        if package["platform"] != "tg":
            safe_send(
                chat_id,
                "ይህ ይዘት ለTelegram አልተዘጋጀም። "
                "በTelegram ላይ ለመለጠፍ Telegram Platform ምረጥ።"
            )
            return

        try:
            bot.send_message(
                TARGET_CHANNEL_ID,
                package["post"],
                disable_web_page_preview=True,
            )
            safe_send(chat_id, "✅ ፖስቱ ወደ Henok Graphics በተሳካ ሁኔታ ተልኳል።")
        except Exception as exc:
            logger.exception("Telegram publishing failed")
            safe_send(
                chat_id,
                f"⚠️ ፖስቱን መላክ አልተቻለም ({type(exc).__name__})። "
                "Bot በግሩፑ ውስጥ የመለጠፍ ፈቃድ እንዳለው አረጋግጥ።"
            )
        return

    if data.startswith("cancel:"):
        package_id = int(data.split(":", 1)[1])
        with STATE_LOCK:
            SESSIONS.get(chat_id, {}).get("pkgs", {}).pop(package_id, None)
        safe_send(chat_id, "❌ የPost Package ሂደቱ ተሰርዟል።")
        return

    if data.startswith("regenerate:"):
        package_id = int(data.split(":", 1)[1])
        with STATE_LOCK:
            package = SESSIONS.get(chat_id, {}).get("pkgs", {}).get(package_id)

        if package:
            make_package(
                chat_id,
                package["topic"],
                package["style"],
                package["platform"],
            )
        else:
            safe_send(chat_id, "የቀድሞው Package አልተገኘም።")
        return


# ============================================================
# Start polling
# ============================================================

if __name__ == "__main__":
    logger.info("HENOK AI Telegram Agent is starting")
    logger.info("Gemini model configured: %s", GEMINI_MODEL)
    logger.info(
        "OpenRouter fallback: %s",
        "enabled" if OPENROUTER_API_KEY else "disabled",
    )

    try:
        bot.infinity_polling(
            timeout=30,
            long_polling_timeout=30,
            skip_pending=True,
        )
    except KeyboardInterrupt:
        logger.info("HENOK AI stopped by keyboard interrupt")
    except Exception:
        logger.exception("Telegram polling stopped unexpectedly")
        raise
