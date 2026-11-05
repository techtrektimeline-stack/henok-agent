"""ሄኖክ AI ኤጀንት v0.1 — ረቂቅ ያመነጫል፣ በTelegram ፍቃድ ይጠይቃል፣ የጸደቀውን ያስታውሳል።"""
import os, json, datetime, pathlib, requests

ROOT = pathlib.Path(__file__).parent.parent
STATE_F = ROOT / "state.json"
VOICE_F = ROOT / "voice.md"
TG = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
CHAT = str(os.environ["TELEGRAM_CHAT_ID"])
GKEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def load():
    return json.loads(STATE_F.read_text(encoding="utf-8"))


def save(s):
    STATE_F.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def ask(prompt):
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        headers={"x-goog-api-key": GKEY},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def tg(method, **data):
    r = requests.post(f"{TG}/{method}", json=data, timeout=30)
    r.raise_for_status()
    return r.json()["result"]


def build_prompt(task, state, extra=""):
    voice = VOICE_F.read_text(encoding="utf-8") if VOICE_F.exists() else ""
    liked = "\n---\n".join(h["text"] for h in state["history"][-5:])
    return (
        "አንተ የሄኖክ የይዘት ረዳት ነህ። እንደ ሄኖክ በአማርኛ ጻፍ።\n"
        f"የሄኖክ ስታይል:\n{voice}\n\nቀደም ሲል ያጸደቃቸው ፖስቶች:\n{liked}\n\n"
        f"ተግባር: {task}\n{extra}\n"
        "ውጤቱ ዝግጁ ፖስት ብቻ ይሁን (ካፕሽን + ሃሽታጎች)። ማብራሪያ አትጨምር።"
    )


def new_draft(state, task, extra=""):
    text = ask(build_prompt(task, state, extra))
    did = str(int(datetime.datetime.utcnow().timestamp()))
    kb = {"inline_keyboard": [[
        {"text": "✅ አጽድቅ", "callback_data": f"ok:{did}"},
        {"text": "❌ ተው", "callback_data": f"no:{did}"},
    ]]}
    m = tg("sendMessage", chat_id=CHAT,
           text=f"📝 ረቂቅ:\n\n{text}\n\n(ለማስተካከል ይህን መልእክት Reply አድርገህ ምን እንደሚቀየር ጻፍ)",
           reply_markup=kb)
    state["pending"][did] = {"text": text, "task": task, "msg_id": m["message_id"]}


def handle_updates(state):
    ups = requests.get(f"{TG}/getUpdates",
                       params={"offset": state["offset"], "timeout": 0}, timeout=30).json()["result"]
    for u in ups:
        state["offset"] = u["update_id"] + 1
        cb = u.get("callback_query")
        if cb and str(cb["message"]["chat"]["id"]) == CHAT:
            act, did = cb["data"].split(":")
            d = state["pending"].pop(did, None)
            tg("answerCallbackQuery", callback_query_id=cb["id"])
            if not d:
                continue
            if act == "ok":
                state["history"].append({"text": d["text"], "date": str(datetime.date.today())})
                tg("sendMessage", chat_id=CHAT,
                   text="✅ ጸድቋል። ይህን ለጥፍ (ራስ-ሰር ልጠፋ በደረጃ 2 ይጨመራል):\n\n" + d["text"])
            else:
                tg("sendMessage", chat_id=CHAT, text="❌ ተሰርዟል።")
            continue
        msg = u.get("message")
        if not msg or str(msg["chat"]["id"]) != CHAT or "text" not in msg:
            continue  # ከሄኖክ ውጭ ማንም አይታዘዝም
        txt = msg["text"]
        reply_to = msg.get("reply_to_message", {}).get("message_id")
        old = next((k for k, v in state["pending"].items() if v["msg_id"] == reply_to), None)
        if old:
            d = state["pending"].pop(old)
            new_draft(state, d["task"], f"ቀዳሚ ረቂቅ:\n{d['text']}\nየሄኖክ እርማት: {txt}")
        elif txt.startswith("/idea"):
            new_draft(state, txt[5:].strip() or "ዕለታዊ ፖስት")
        elif txt.startswith("/start"):
            tg("sendMessage", chat_id=CHAT,
               text="ሰላም ሄኖክ! /idea <ርዕስ> ጻፍልኝ ወይም በየቀኑ ራሴ ረቂቅ እልካለሁ።")


def daily(state):
    today = str(datetime.date.today())
    if state["last_daily"] != today and datetime.datetime.utcnow().hour >= 5:
        state["last_daily"] = today
        new_draft(state, "ለዛሬ ለማህበራዊ ሚዲያ አንድ አዲስ ፖስት ሀሳብ አዘጋጅ")


if __name__ == "__main__":
    s = load()
    handle_updates(s)
    daily(s)
    save(s)
