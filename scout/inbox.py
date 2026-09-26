"""Telegram-Posteingang: Wünsche per Chat annehmen (z. B. „bitte keine Angebote mehr von Firma XYZ“).

Läuft alle 15 Minuten (inbox.yml). Liest neue Nachrichten an den Bot, akzeptiert nur Nachrichten von
TELEGRAM_CHAT_ID / TELEGRAM_CC_CHAT_IDS, lässt Gemini daraus Regeln ableiten und speichert sie in
data/preferences.json. digest.py wendet die Regeln beim nächsten Lauf an.

Befehle: /regeln (Regeln anzeigen), /hilfe, /zuruecksetzen (alle Regeln löschen).
"""
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from digest import gemini_models, post_json, send, esc, load, DATA, fmt  # noqa: E402

TOK = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
KEY = os.environ.get("GEMINI_API_KEY", "").strip()
ALLOWED = {c.strip() for c in (os.environ.get("TELEGRAM_CHAT_ID", "") + "," +
                              os.environ.get("TELEGRAM_CC_CHAT_IDS", "")).split(",") if c.strip()}
EMPTY = {"exclude_companies": [], "exclude_keywords": [], "notes": []}

HELP = ("Du kannst mir einfach schreiben, was ich bei der Jobsuche ändern soll, zum Beispiel:\n"
        "• <i>Bitte keine Angebote mehr von Firma XYZ</i>\n"
        "• <i>Keine Praktika mehr</i>\n"
        "• <i>Mehr Stellen in Graz, Salzburg ist mir zu weit</i>\n"
        "• <i>Firma XYZ darf wieder rein</i>\n\n"
        "Das gilt ab dem nächsten Update um 08:00.\n"
        "/regeln – aktuelle Wünsche anzeigen\n/zuruecksetzen – alle Wünsche löschen")

PROMPT = """Du verwaltest die Such-Wünsche einer Jobsuchenden (Realtime/Game Art, Wien) für einen Job-Bot.
Aktuelle Regeln (JSON): {rules}

Neue Chat-Nachricht von ihr: \"\"\"{msg}\"\"\"

Leite daraus Änderungen ab. Regeln:
- exclude_companies: Firmennamen, deren Stellen nie mehr gezeigt werden sollen (so wie sie in Anzeigen stehen, kurz).
- exclude_keywords: Begriffe, die im Stellentitel oder Ort nicht vorkommen dürfen (z. B. "Praktikum", "Linz").
- notes: kurze, klare Wünsche für die Bewertung, die nicht als harter Filter taugen (max. 150 Zeichen je Eintrag),
  z. B. "Graz bevorzugen", "Lehrstellen höher gewichten".
- Nur ändern, was sie wirklich will. Entfernen, wenn sie etwas zurücknimmt.
- Ist die Nachricht keine Anweisung (Dank, Smalltalk, Frage), keine Änderungen und freundlich kurz antworten.
- reply: kurze, freundliche Bestätigung auf Deutsch (du-Form, max. 200 Zeichen), was jetzt gilt.

Antworte NUR mit JSON:
{{"add_exclude_companies":[str],"remove_exclude_companies":[str],"add_exclude_keywords":[str],
"remove_exclude_keywords":[str],"add_notes":[str],"remove_notes":[str],"reply":str}}"""


def tg(method, **params):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"https://api.telegram.org/bot{TOK}/{method}?{q}", timeout=40) as r:
        return json.load(r)


def rules_text(p):
    out = []
    if p["exclude_companies"]:
        out.append("🚫 Firmen: " + esc(", ".join(p["exclude_companies"])))
    if p["exclude_keywords"]:
        out.append("🚫 Begriffe: " + esc(", ".join(p["exclude_keywords"])))
    for n in p["notes"]:
        out.append("📝 " + esc(n))
    return "\n".join(out) or "Noch keine eigenen Wünsche gespeichert."


def interpret(p, msg):
    payload = {"contents": [{"role": "user", "parts": [{"text": PROMPT.format(rules=json.dumps(p, ensure_ascii=False), msg=msg[:1000])}]}],
               "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json", "maxOutputTokens": 2000}}
    last = None
    for model in gemini_models(KEY)[:4]:
        try:
            r = post_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          payload, {"x-goog-api-key": KEY}, timeout=60)
            text = "".join(x.get("text", "") for x in r["candidates"][0]["content"]["parts"])
            return json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()), r.get("usageMetadata", {})
        except Exception as e:
            last = e
    raise RuntimeError(last)


def apply(p, ch):
    def upd(key, add, rem, limit):
        cur = [x for x in p[key] if x.lower() not in {r.lower() for r in ch.get(rem, [])}]
        for a in ch.get(add, []):
            a = str(a).strip()[:limit]
            if a and a.lower() not in {x.lower() for x in cur}:
                cur.append(a)
        p[key] = cur[:50]
    upd("exclude_companies", "add_exclude_companies", "remove_exclude_companies", 80)
    upd("exclude_keywords", "add_exclude_keywords", "remove_exclude_keywords", 60)
    upd("notes", "add_notes", "remove_notes", 150)


def main():
    if not TOK:
        raise SystemExit("TELEGRAM_BOT_TOKEN fehlt")
    state = load("tg_offset.json", {"offset": 0})
    p = {**EMPTY, **load("preferences.json", EMPTY)}
    upd = tg("getUpdates", offset=state["offset"], timeout=0).get("result", [])
    if not (DATA / "tg_offset.json").exists():
        # erster Start: alte Nachrichten (Chat-ID-Tests usw.) nicht beantworten
        if upd:
            state["offset"] = upd[-1]["update_id"] + 1
        (DATA / "tg_offset.json").write_text(json.dumps(state), encoding="utf-8")
        print(f"Initialisiert, {len(upd)} alte Nachrichten übersprungen.")
        return
    changed = False
    tokens = 0
    for u in upd:
        state["offset"] = u["update_id"] + 1
        changed = True
        m = u.get("message") or {}
        chat = str((m.get("chat") or {}).get("id", ""))
        text = (m.get("text") or "").strip()
        if not chat or not text:
            continue
        if chat not in ALLOWED:
            print(f"::notice::Nachricht von unbekanntem Chat {chat} ({(m.get('chat') or {}).get('first_name', '')}) ignoriert")
            continue
        cmd = text.split()[0].lower()
        if cmd in ("/start", "/hilfe", "/help"):
            send(TOK, chat, HELP)
        elif cmd in ("/regeln", "/rules"):
            send(TOK, chat, "<b>Deine Wünsche</b>\n" + rules_text(p))
        elif cmd in ("/zuruecksetzen", "/zurücksetzen", "/reset"):
            p = json.loads(json.dumps(EMPTY))
            send(TOK, chat, "Alles zurückgesetzt – ab dem nächsten Update gelten wieder nur die Grundeinstellungen.")
        else:
            try:
                ch, usage = interpret(p, text)
                tokens += usage.get("totalTokenCount", 0)
                apply(p, ch)
                reply = esc(ch.get("reply") or "Alles klar.")
                if any(ch.get(k) for k in ch if k != "reply"):
                    reply += "\n\n<b>Jetzt gilt:</b>\n" + rules_text(p)
                send(TOK, chat, reply)
            except Exception as e:
                print(f"::error::Gemini: {e}")
                send(TOK, chat, "Sorry, das konnte ich gerade nicht verarbeiten – versuch es bitte später nochmal.")
        print(f"Nachricht von {chat} verarbeitet.")
    if changed:
        p["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        (DATA / "preferences.json").write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
        (DATA / "tg_offset.json").write_text(json.dumps(state), encoding="utf-8")
        if tokens:
            u = load("inbox_usage.json", {})
            mon = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
            u[mon] = u.get(mon, 0) + tokens
            (DATA / "inbox_usage.json").write_text(json.dumps(u, indent=1), encoding="utf-8")
    print(f"{len(upd)} Updates, Tokens {fmt(tokens)}")


if __name__ == "__main__":
    main()
