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
INLINE_MAX = 12 * 1024 * 1024   # ከዚህ በላይ በGemini Files API ይጫናል
TG_MAX = 20 * 1024 * 1024       # ቴሌግራም ለbotሎች የሚፈቅደው ከፍተኛ መጠን


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
        return "\n".join(html.unes
