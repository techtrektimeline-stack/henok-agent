"""ሄኖክ AI ኤጀንት v0.2 — ጽሑፍ፣ ድምጽ፣ ምስል፣ ቪዲዮ፣ PDF እና ፋይሎችን ይረዳል።
ረቂቅ ሲሠራ በTelegram ፍቃድ ይጠይቃል፤ የጸደቀውን ያስታውሳል።"""
import os, io, re, json, time, base64, html, zipfile, mimetypes, datetime, pathlib
import requests

_here = pathlib.Path(__file__).resolve().parent
ROOT = _here.parent if _here.name == "agent" else _here
STATE_F = ROOT / "state.json"
VOICE_F = ROOT / "voice.md"
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG = f"https://api.telegram.org/bot{TOKEN}"
CHAT = str(os.environ["TELEGRAM_CHAT_ID"])
GKEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
GAPI = "https://generativelanguage.googleapis.com"
GH = {"x-goog-api-key": GKEY}
INLINE_MAX = 12 * 1024 * 1024
TG_MAX = 20 * 1024 * 1024


class UserError(Exception):
    """ለሄኖክ በቀጥታ የሚነገር ግልጽ መልእክት።"""


# ---------- state ----------
def load():
    s = json.loads(STATE_F.read_text(encoding="utf-8")) if STATE_F.exists() else {}
    for k, v in (("offset", 0), ("pending", {}), ("history", []), ("last_daily", ""), ("chat", [])):
        s.setdefault(k, v)
    return s


def save(s):
    STATE_F.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- telegram ----------
def tg(method, **data):
    r = requests.post(f"{TG}/{method}", json=data, timeout=30)
    r.raise_for_status()
    return r.json()["result"]


def say(text):
    text = (text or "").strip() or "(ባዶ ምላሽ)"
    for i in range(0, len(text), 3900):
        tg("sendMessage", chat_id=CHAT, text=text[i:i + 3900])


def tg_download(file_id, size):
    if size and size > TG_MAX:
        raise UserError("ፋይሉ ከ20MB በላይ ነው። ቴሌግራም ለbotሎች ከዚህ በላይ አይፈቅድም፤ አሳጥረህ ላክ።")
    r = requests.post(f"{TG}/getFile", json={"file_id": file_id}, timeout=30)
    if r.status_code != 200:
        raise UserError("ፋይሉን ማውረድ አልቻልኩም (ከ20MB በላይ ሊሆን ይችላል)።")
    path = r.json()["result"]["file_path"]
    d = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=180)
    d.raise_for_status()
    return d.content


# ---------- ፋይል → Gemini part ----------
def office_text(data, ext):
    z = zipfile.ZipFile(io.BytesIO(data))
    clean = lambda x: html.unescape(re.sub(r"<[^>]+>", "", x))
    if ext == "docx":
        x = z.read("word/document.xml").decode("utf8", "ignore")
        return clean(x.replace("</w:p>", "\n"))
    if ext == "pptx":
        names = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml", n)),
                       key=lambda n: int(re.findall(r"\d+", n)[0]))
        out = []
        for i, n in enumerate(names, 1):
            t = re.findall(r"<a:t>(.*?)</a:t>", z.read(n).decode("utf8", "ignore"), re.S)
            out.append(f"--- ስላይድ {i} ---\n" + "\n".join(html.unescape(a) for a in t))
        return "\n".join(out)
    if ext == "xlsx":
        if "xl/sharedStrings.xml" not in z.namelist():
            return ""
        x = z.read("xl/sharedStrings.xml").decode("utf8", "ignore")
        return "\n".join(html.unescape(t) for t in re.findall(r"<t[^>]*>(.*?)</t>", x, re.S))
    return ""


TEXT_EXT = {"txt", "md", "csv", "json", "html", "xml", "srt", "log", "py", "js", "tsv"}


def to_part(data, mime, name):
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if mime in ("application/octet-stream", "", None):
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if ext in ("docx", "pptx", "xlsx"):
        text = office_text(data, ext)
        return {"text": f"[የፋይል '{name}' ይዘት]\n{text[:80000]}"}
    if mime.startswith("text/") or ext in TEXT_EXT:
        return {"text": f"[የፋይል '{name}' ይዘት]\n{data.decode('utf8', 'ignore')[:80000]}"}
    if not (mime.startswith(("image/", "audio/", "video/")) or mime == "application/pdf"):
        raise UserError(f"የ'{name}' ዓይነት ({mime}) ገና አልተደገፈም። PDF፣ ምስል፣ ድምጽ፣ ቪዲዮ፣ Word፣ PowerPoint፣ Excel ወይም ጽሑፍ ላክ።")
    if len(data) > INLINE_MAX:
        return gemini_upload(data, mime, name)
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}}


def gemini_upload(data, mime, name):
    st = requests.post(
        f"{GAPI}/upload/v1beta/files",
        headers={**GH, "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                 "X-Goog-Upload-Header-Content-Length": str(len(data)),
                 "X-Goog-Upload-Header-Content-Type": mime, "Content-Type": "application/json"},
        json={"file": {"display_name": name}}, timeout=60)
    st.raise_for_status()
    up = requests.post(
        st.headers["x-goog-upload-url"],
        headers={"Content-Length": str(len(data)), "X-Goog-Upload-Offset": "0",
                 "X-Goog-Upload-Command": "upload, finalize"},
        data=data, timeout=300)
    up.raise_for_status()
    f = up.json()["file"]
    for _ in range(48):
        state = f.get("state", "ACTIVE")
        if state == "ACTIVE":
            return {"fileData": {"mimeType": mime, "fileUri": f["uri"]}}
        if state == "FAILED":
            raise UserError("Gemini ፋይሉን ማስኬድ አልቻለም።")
        time.sleep(5)
        f = requests.get(f"{GAPI}/v1beta/{f['name']}", headers=GH, timeout=30).json()
    raise UserError("ፋይሉን ለማስኬድ ብዙ ጊዜ ወሰደ፤ አሳጥረህ ላክ።")


def media_of(msg):
    """(ዓይነት, file_id, mime, ስም, መጠን) ወይም None"""
    if msg.get("voice"):
        v = msg["voice"]; return ("ድምጽ", v["file_id"], v.get("mime_type", "audio/ogg"), "voice.ogg", v.get("file_size"))
    if msg.get("audio"):
        v = msg["audio"]; return ("ድምጽ", v["file_id"], v.get("mime_type", "audio/mpeg"), v.get("file_name", "audio.mp3"), v.get("file_size"))
    if msg.get("photo"):
        v = msg["photo"][-1]; return ("ምስል", v["file_id"], "image/jpeg", "photo.jpg", v.get("file_size"))
    if msg.get("video"):
        v = msg["video"]; return ("ቪዲዮ", v["file_id"], v.get("mime_type", "video/mp4"), v.get("file_name", "video.mp4"), v.get("file_size"))
    if msg.get("video_note"):
        v = msg["video_note"]; return ("ቪዲዮ", v["file_id"], "video/mp4", "note.mp4", v.get("file_size"))
    if msg.get("animation"):
        v = msg["animation"]; return ("ቪዲዮ", v["file_id"], v.get("mime_type", "video/mp4"), v.get("file_name", "anim.mp4"), v.get("file_size"))
    if msg.get("document"):
        v = msg["document"]; return ("ፋይል", v["file_id"], v.get("mime_type", ""), v.get("file_name", "file"), v.get("file_size"))
    return None


# ---------- አንጎል ----------
SYSTEM = """አንተ የሄኖክ የግል ረዳት ነህ። ሄኖክ ኢትዮጵያዊ ይዘት ፈጣሪ ነው (ዩቲዩብ፣ ቲክቶክ፣ ግራፊክ ዲዛይን፣ AI ይዘት፣ ዲጂታል ጥበብ፣ ቪዲዮ)። ሁልጊዜ በአማርኛ መልስ ስጥ (ሄኖክ በሌላ ቋንቋ ካልጠየቀ)።
የሚመጣልህ ግብዓት ጽሑፍ፣ ድምጽ (አዳምጥና ተረዳ)፣ ምስል፣ ቪዲዮ (እይና አዳምጥ)፣ PDF ወይም ሌላ ፋይል ሊሆን ይችላል። ሄኖክ በድምጽ የሚያዝህ እንደ ጽሑፍ ትእዛዝ ነው።
ውጤትህ ከዚህ ቅርጽ ውጭ ምንም ሳይኖር JSON ብቻ ይሁን:
{"heard": "ምን እንደተረዳህ በአንድ-ሁለት አጭር አረፍተ ነገር (ለድምጽ/ምስል/ቪዲዮ/ፋይል የምትረዳውን ማጠቃለያ)", "type": "draft" ወይም "answer", "text": "ዋናው ውጤት"}
ደንቦች:
- ሄኖክ ፖስት፣ ካፕሽን፣ ስክሪፕት፣ ርዕስ ወይም ለመለጠፍ የሚሆን ይዘት ከጠየቀ፣ ወይም ምስል/ቪዲዮ/ፋይል ሰጥቶ "ፖስት አድርግ" ካለ: type="draft" እና text ውስጥ ዝግጁ የሆነ ይዘት (ካፕሽን + ሃሽታጎች) ብቻ።
- ጥያቄ፣ ማብራሪያ፣ ትንተና ወይም ውይይት ከሆነ: type="answer"።
- ምንም መመሪያ ሳይኖር ፋይል/ምስል/ቪዲዮ ብቻ ከተላከ: type="answer" ሆኖ ይዘቱን አጠቃልል፣ ዋና ነጥቦችንና ሊሠሩ የሚችሉ እርምጃዎችን ዘርዝር፣ ከዚያ ምን እንዲደረግ እንደሚፈልግ ጠይቅ።
- የማታውቀውን አትፈጥር፤ ግልጽ ካልሆነ ግልጽ አድርገህ ጠይቅ።
{FORCE}
የሄኖክ ስታይል:
{VOICE}

ቀደም ሲል ያጸደቃቸው ፖስቶች (ስታይሉን ተከተል):
{LIKED}
"""


def think(state, parts, force_draft=False):
    voice = VOICE_F.read_text(encoding="utf-8") if VOICE_F.exists() else ""
    liked = "\n---\n".join(h["text"] for h in state["history"][-5:])
    system = (SYSTEM.replace("{VOICE}", voice).replace("{LIKED}", liked)
              .replace("{FORCE}", 'ይህ ጊዜ type ሁልጊዜ "draft" ይሁን።' if force_draft else ""))
    contents = [{"role": h["role"], "parts": [{"text": h["text"]}]} for h in state["chat"][-8:]]
    contents.append({"role": "user", "parts": parts})
    r = requests.post(
        f"{GAPI}/v1beta/models/{MODEL}:generateContent", headers=GH, timeout=300,
        json={"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
              "generationConfig": {"responseMimeType": "application/json"}})
    if r.status_code == 429:
        raise UserError("የGemini ነፃ ኮታ ለአሁን አልቋል። ከጥቂት ደቂቃ ወይም ነገ ሞክር።")
    r.raise_for_status()
    cparts = r.json()["candidates"][0]["content"]["parts"]
    raw = "".join(p.get("text", "") for p in cparts).strip()
    try:
        out = json.loads(raw)
        if not isinstance(out, dict):
            raise ValueError
    except Exception:
        out = {"type": "answer", "heard": "", "text": raw}
    out.setdefault("heard", ""); out.setdefault("text", ""); out.setdefault("type", "answer")
    if force_draft:
        out["type"] = "draft"
    return out


def remember(state, user_text, model_text):
    state["chat"].append({"role": "user", "text": user_text[:1500]})
    state["chat"].append({"role": "model", "text": model_text[:1500]})
    state["chat"] = state["chat"][-10:]


def send_draft(state, text, task):
    did = str(int(time.time() * 1000))
    kb = {"inline_keyboard": [[{"text": "✅ አጽድቅ", "callback_data": f"ok:{did}"},
                               {"text": "❌ ተው", "callback_data": f"no:{did}"}]]}
    m = tg("sendMessage", chat_id=CHAT, reply_markup=kb,
           text=f"📝 ረቂቅ:\n\n{text[:3500]}\n\n(ለማስተካከል ይህን መልእክት Reply አድርገህ ምን እንደሚቀየር ጻፍ ወይም በድምጽ ንገረኝ)")
    state["pending"][did] = {"text": text, "task": task, "msg_id": m["message_id"]}


def deliver(state, out, user_label, force_draft=False):
    if out["heard"]:
        say("🎧 የተረዳሁት: " + out["heard"])
    remember(state, user_label, out["text"] or out["heard"])
    if out["type"] == "draft" or force_draft:
        send_draft(state, out["text"], user_label)
    else:
        say(out["text"])


# ---------- መልእክት አያያዝ ----------
def handle_callback(state, cb):
    act, did = cb["data"].split(":")
    d = state["pending"].pop(did, None)
    tg("answerCallbackQuery", callback_query_id=cb["id"])
    if not d:
        return
    if act == "ok":
        state["history"].append({"text": d["text"], "date": str(datetime.date.today())})
        say("✅ ጸድቋል። ይህን ለጥፍ (ራስ-ሰር መለጠፍ በቀጣይ ደረጃ ይጨመራል):\n\n" + d["text"])
    else:
        say("❌ ተሰርዟል።")


def handle_message(state, msg):
    text = (msg.get("text") or msg.get("caption") or "").strip()
    media = media_of(msg)
    low = text.lower().lstrip("/")
    reply_to = msg.get("reply_to_message", {}).get("message_id")
    old = next((k for k, v in state["pending"].items() if v["msg_id"] == reply_to), None)

    if not media and not old:
        if low == "start":
            say("ሰላም ሄኖክ! 👋 በጽሑፍ፣ በድምጽ፣ በምስል፣ በቪዲዮ፣ በPDF ወይም በፋይል ላክልኝ፤ እረዳለሁ። "
                "ፖስት ከፈለግህ ርዕስ ንገረኝ፤ ረቂቅ አዘጋጅቼ ፍቃድህን እጠይቃለሁ።\n(ምላሽ እስከ ~30 ደቂቃ ሊዘገይ ይችላል።)")
            return
        if low == "id":
            say(f"ቁጥርህ: {CHAT}")
            return
        if low.startswith("idea"):
            text = text[4:].strip() if text.lower().startswith("idea") else text[5:].strip()
            text = text or "ለዛሬ ለማህበራዊ ሚዲያ አንድ አዲስ ፖስት ሀሳብ አዘጋጅ"

    parts, label = [], text
    if old:
        d = state["pending"].pop(old)
        parts.append({"text": f"ቀዳሚ ረቂቅ:\n{d['text']}\n\nየሄኖክ እርማት (ጽሑፍ/ድምጽ): {text}"})
        label = d["task"]
    elif text:
        parts.append({"text": text})
    if media:
        kind, fid, mime, name, size = media
        parts.append(to_part(tg_download(fid, size), mime, name))
        if not text and not old:
            parts.insert(0, {"text": f"(ሄኖክ {kind} ብቻ ላከ፤ ጽሑፍ የለውም)"})
        label = (text or f"[{kind}: {name}]")
    if not parts:
        return
    out = think(state, parts, force_draft=bool(old))
    deliver(state, out, label, force_draft=bool(old))


def handle_update(state, u):
    cb = u.get("callback_query")
    if cb:
        if str(cb["message"]["chat"]["id"]) == CHAT:
            handle_callback(state, cb)
        return
    msg = u.get("message")
    if msg and str(msg["chat"]["id"]) == CHAT:
        handle_message(state, msg)


def daily(state):
    today = str(datetime.date.today())
    if state["last_daily"] != today and datetime.datetime.utcnow().hour >= 5:
        state["last_daily"] = today
        out = think(state, [{"text": "ለዛሬ ለማህበራዊ ሚዲያ አንድ አዲስ ፖስት ሀሳብ አዘጋጅ"}], force_draft=True)
        send_draft(state, out["text"], "ዕለታዊ ፖስት")


def safe(e):
    return str(e).replace(TOKEN, "***").replace(GKEY, "***")[:300]


def main():
    s = load()
    ups = requests.get(f"{TG}/getUpdates", params={"offset": s["offset"], "timeout": 0}, timeout=30).json()["result"]
    for u in ups:
        s["offset"] = u["update_id"] + 1
        try:
            handle_update(s, u)
        except UserError as e:
            say(f"⚠️ {e}")
        except Exception as e:
            print("ERR", type(e).__name__, safe(e))
            say(f"⚠️ ችግር ተፈጠረ ({type(e).__name__})። እንደገና ሞክር።")
        save(s)
    try:
        daily(s)
    except Exception as e:
        print("ERR daily", type(e).__name__, safe(e))
    save(s)


if __name__ == "__main__":
    main()
