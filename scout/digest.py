"""Job-Scout Digest: bewertet data/recent.json mit Gemini und schickt die Liste per Telegram.

Env:
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (Repository secrets)
  GEMINI_MODEL   optional, sonst automatische Auswahl
  DRY_RUN=1      nichts senden, Nachricht nur in debug/digest_preview.txt schreiben
  RESEND=1       auch bereits gesendete Stellen wieder berücksichtigen
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA, DEBUG = ROOT / "data", ROOT / "debug"
CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
DG = CFG["digest"]
NOW = dt.datetime.now(dt.timezone.utc)
DRY = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")
RESEND = os.environ.get("RESEND", "").strip() in ("1", "true", "yes")

# Grobe Vorfilterung, damit Gemini nur plausible Kandidaten sieht
EXCLUDE = re.compile(
    r"\b(senior|sr\.|lead|principal|director|head of|staff|manager|leitung|leiter)\b|"
    r"bim|cad|konstrukt|tragwerk|hkls|statik|nail|tattoo|make-?up|friseur|verkauf|sales|account|"
    r"backend|frontend|full-?stack|devops|engineer\b|developer|entwickler|programmer|programmier|"
    r"qa tester|tester\b|recruiter|marketing manager|buchhalt|controller|praktikum unbezahlt", re.I)


def load(fn, default):
    p = DATA / fn
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def post_json(url, payload, headers=None, timeout=240):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


USAGE = {}  # usageMetadata der letzten Gemini-Antwort
MOTIVATION = [""]

# ---------------------------------------------------------------- Gemini
def gemini_models(key):
    wanted = [os.environ.get("GEMINI_MODEL", "").strip()] if os.environ.get("GEMINI_MODEL", "").strip() else []
    try:
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                     headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            names = [m["name"].split("/")[-1] for m in json.load(r).get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception as e:
        print("Modellliste nicht abrufbar:", e)
        names = []
    # bevorzugt: aktuelles Flash-Modell (stabil vor preview), dann Pro
    def rank(n):
        return (0 if "flash" in n and "lite" not in n else 1 if "flash" in n else 2 if "pro" in n else 9,
                "preview" in n or "exp" in n, -len(re.findall(r"\d", n)), n)
    auto = sorted([n for n in names if n.startswith("gemini") and not re.search(r"image|tts|audio|live|embedding|thinking-exp", n)],
                  key=rank)
    # neueste Versionen zuerst innerhalb der Flash-Gruppe
    flash = sorted([n for n in auto if "flash" in n and "lite" not in n and "preview" not in n and "exp" not in n],
                   key=lambda n: [int(x) for x in re.findall(r"\d+", n)] or [0], reverse=True)
    fallback = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]
    out = []
    for n in wanted + flash + fallback + auto:
        if n and n not in out:
            out.append(n)
    return out


PROMPT = """Du bist Recruiting-Assistent. Wähle aus den Stellenanzeigen unten die passenden für diese Kandidatin aus.

KANDIDATIN:
{profile}
{targets}

REGELN:
- Nur echte Treffer, streng filtern. Lieber 20 gute als 100 mittelmäßige. Maximal {max_jobs}.
- Duplikate (gleiche Stelle über mehrere Quellen/Links) nur einmal aufnehmen, den besten Link wählen.
- Wenn Firma oder Ort fehlen, aus Titel/Snippet/URL ableiten; sonst leer lassen. Nichts erfinden.
- fit: "hoch" = Rolle und Level passen, Ort Wien/Österreich/Remote-EU; "mittel" = teilweise passend.
- region: "wien", "oesterreich", "remote" oder "ausland".
- reason: ein kurzer deutscher Satz (max. 90 Zeichen), warum es passt bzw. worauf zu achten ist.
- title: bereinigter Stellentitel ohne (m/w/d)-Zusätze, max. 70 Zeichen.
- id: die id aus den Daten.

Zusätzlich "motivation": 1–2 kurze, warme Sätze auf Deutsch (du-Form, max. 220 Zeichen), die sie für die Jobsuche
aufmuntern. Jeden Tag anders: mal ein konkreter Tipp (Portfolio, Bewerbung, Netzwerken, Game-Jams), mal Zuspruch,
mal ein kleiner Perspektivwechsel oder Humor. Bezug gern auf die heutigen Treffer oder ihr Profil. Nicht kitschig,
keine Floskeln wie "Du schaffst das!" allein, keine Emojis-Flut (höchstens eins). Nicht wiederholen, was hier schon kam:
{recent_motivation}

Antworte NUR mit JSON: {{"motivation":str,"jobs":[{{"id":int,"title":str,"company":str,"location":str,"region":str,"fit":str,"reason":str}}]}}

ANZEIGEN (JSON-Zeilen):
{jobs}
"""


def rate(jobs, key):
    lines = []
    for i, j in enumerate(jobs):
        lines.append(json.dumps({"id": i, "title": j.get("title", ""), "company": j.get("company", ""),
                                 "location": j.get("location", ""), "source": j.get("source", ""),
                                 "url": j["url"][:140], "snippet": (j.get("snippet") or "")[:220]},
                                ensure_ascii=False))
    prompt = PROMPT.format(profile=DG["profile"].strip(), targets=DG["targets"].strip(),
                           max_jobs=DG.get("max_jobs", 100), jobs="\n".join(lines),
                           recent_motivation="\n".join("- " + m for m in load("motivation.json", [])[-14:]) or "- (noch nichts)")
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json", "maxOutputTokens": 32000}}
    last = None
    for model in gemini_models(key):
        for attempt in range(3):
            try:
                print(f"Gemini: {model} (Versuch {attempt + 1}), {len(jobs)} Kandidaten")
                r = post_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              payload, {"x-goog-api-key": key})
                text = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
                text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
                parsed = json.loads(text)
                picked = parsed["jobs"]
                MOTIVATION[0] = (parsed.get("motivation") or "").strip()
                USAGE.update(r.get("usageMetadata") or {})
                return model, picked
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:300]
                last = f"{model}: HTTP {e.code} {body}"
                print(last)
                if e.code in (429, 500, 503):
                    time.sleep(20 * (attempt + 1))
                    continue
                break  # 400/404: nächstes Modell
            except Exception as e:
                last = f"{model}: {e}"
                print(last)
                time.sleep(5)
    raise RuntimeError(f"Gemini-Bewertung fehlgeschlagen – {last}")


# ---------------------------------------------------------------- Telegram
def esc(s):
    return html.escape(s or "", quote=False)


REGIONS = [("wien", "📍 Wien"), ("oesterreich", "🇦🇹 Österreich"), ("remote", "🌍 Remote"), ("ausland", "✈️ Ausland")]


def build_messages(picked, jobs, raw_count):
    items = []
    for p in picked:
        try:
            j = jobs[int(p["id"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        items.append({**p, "url": j["url"], "first_seen": j.get("first_seen", "")})
    fitrank = {"hoch": 0, "mittel": 1}
    items.sort(key=lambda x: (fitrank.get(x.get("fit"), 2), x.get("title", "")))

    day = NOW.astimezone(dt.timezone(dt.timedelta(hours=2))).strftime("%d.%m.")
    n_hoch = sum(1 for x in items if x.get("fit") == "hoch")
    word = "neue passende Stelle" if len(items) == 1 else "neue passende Stellen"
    head = (f"<b>🎯 Job-Update · {day}</b>\n"
            f"{len(items)} {word} ({n_hoch} × sehr passend 🟢)\n"
            f"<i>aus {raw_count} neuen Anzeigen</i>")
    if MOTIVATION[0]:
        head += f"\n\n💬 <i>{esc(MOTIVATION[0])}</i>"
    if not items:
        return [head], items

    blocks = [head]
    for reg, label in REGIONS:
        sub = [x for x in items if x.get("region") == reg]
        if not sub:
            continue
        blocks.append(f"\n<b>{label}</b>  ·  {len(sub)}")
        for x in sub:
            dot = "🟢" if x.get("fit") == "hoch" else "🟡"
            meta = " · ".join(v for v in [x.get("company", "").strip(), x.get("location", "").strip()] if v)
            entry = f"{dot} <b><a href=\"{html.escape(x['url'])}\">{esc(x.get('title', 'Stelle'))}</a></b>"
            if meta:
                entry += f"\n{esc(meta)}"
            if x.get("reason"):
                entry += f"\n<i>{esc(x['reason'])}</i>"
            blocks.append("\n" + entry)
    blocks.append("\n🟢 sehr passend · 🟡 teilweise passend")

    # in Nachrichten ≤ 3900 Zeichen aufteilen (Telegram-Limit 4096)
    msgs, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 1 > 3900:
            msgs.append(cur)
            cur = b.lstrip("\n")
        else:
            cur += ("\n" if cur else "") + b
    if cur:
        msgs.append(cur)
    ps = (DG.get("postscript") or "").strip()
    if ps and msgs:
        msgs[-1] += f"\n\n{esc(ps)}"
    return msgs, items


def send(token, chat, text):
    r = post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                  {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=30)
    if not r.get("ok"):
        raise RuntimeError(r)


def record_usage(model):
    """Tokens dieses Laufs in data/usage.json (pro Monat) aufsummieren und zurückgeben."""
    u = load("usage.json", {})
    month = NOW.strftime("%Y-%m")
    m = u.setdefault(month, {"runs": 0, "prompt": 0, "output": 0, "thoughts": 0, "total": 0})
    run = {"prompt": USAGE.get("promptTokenCount", 0), "output": USAGE.get("candidatesTokenCount", 0),
           "thoughts": USAGE.get("thoughtsTokenCount", 0), "total": USAGE.get("totalTokenCount", 0)}
    if run["total"]:
        m["runs"] += 1
        for k, v in run.items():
            m[k] += v
        m["model"] = model
    (DATA / "usage.json").write_text(json.dumps(u, indent=1), encoding="utf-8")
    return run, m


def admin(text):
    tok, adm = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    if tok and adm and not DRY:
        try:
            send(tok, adm, text)
        except Exception as e:
            print("Admin-Nachricht fehlgeschlagen:", e)


def fmt(n):
    return f"{n:,}".replace(",", ".")


def wait_for_send_time():
    """Geplante Läufe: nur einmal pro Tag, und erst um SEND_AT (Wiener Zeit) senden."""
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Vienna")
    now = dt.datetime.now(tz)
    hh, mm = map(int, str(DG.get("send_at", "08:00")).split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    last = load("digest_status.json", {}).get("scheduled_day", "")
    if last == now.date().isoformat():
        print("Heute schon gesendet – übersprungen."); return False
    if now < target - dt.timedelta(minutes=75):
        print(f"Zu früh ({now:%H:%M}) – der zweite Zeitplan-Eintrag übernimmt (Sommer-/Winterzeit)."); return False
    if now > target + dt.timedelta(hours=3):
        print(f"Zu spät ({now:%H:%M}) – übersprungen."); return False
    if now < target:
        secs = (target - now).total_seconds()
        print(f"Warte {secs/60:.0f} min bis {hh:02d}:{mm:02d} Wiener Zeit …", flush=True)
        time.sleep(secs)
    return True


def main():
    if not wait_for_send_time():
        return
    scraped = load("status.json", {}).get("generated", "")
    if scraped and scraped[:10] < (NOW - dt.timedelta(hours=30)).date().isoformat():
        admin(f"⚠️ <b>Job-Scout:</b> Die Scraper-Daten sind vom {esc(scraped[:10])} – der tägliche Scraper-Lauf "
              f"ist wohl fehlgeschlagen. github.com/LMXtinker/job-scout/actions")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    chats = [c.strip() for c in (chat + "," + os.environ.get("TELEGRAM_CC_CHAT_IDS", "")).split(",") if c.strip()]
    missing = [n for n, v in [("GEMINI_API_KEY", key), ("TELEGRAM_BOT_TOKEN", tok), ("TELEGRAM_CHAT_ID", chat)] if not v]
    if missing and not (DRY and key):
        raise SystemExit(f"Fehlende Secrets: {', '.join(missing)}")

    recent = load("recent.json", {}).get("jobs", [])
    sent = load("sent.json", {})
    rated = load("rated.json", {})
    cands = [j for j in recent if (RESEND or (j["url"] not in sent and j["url"] not in rated))
             and not EXCLUDE.search(j.get("title", ""))]
    print(f"recent={len(recent)} bereits bewertet={sum(1 for j in recent if j['url'] in rated)} Kandidaten={len(cands)}")

    status = {"generated": NOW.isoformat(timespec="seconds"), "candidates": len(cands)}
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and not DRY:
        from zoneinfo import ZoneInfo
        status["scheduled_day"] = dt.datetime.now(ZoneInfo("Europe/Vienna")).date().isoformat()
    model = ""
    if cands:
        model, picked = rate(cands, key)
        status["model"] = model
    else:
        picked = []
    msgs, items = build_messages(picked, cands, len(cands))
    status["picked"] = len(items)
    status["messages"] = len(msgs)
    DEBUG.mkdir(exist_ok=True)
    (DEBUG / "digest_preview.txt").write_text("\n\n=====\n\n".join(msgs), encoding="utf-8")

    if DRY:
        print("DRY_RUN – nichts gesendet.")
    else:
        if not items:
            print("Keine neuen passenden Stellen – nichts gesendet.")
        for c in chats if items else []:
            for m in msgs:
                send(tok, c, m)
                time.sleep(1.2)
        for x in items:
            sent[x["url"]] = NOW.date().isoformat()
        old = (NOW.date() - dt.timedelta(days=180)).isoformat()
        sent = {u: d for u, d in sent.items() if d >= old}
        for j in cands:
            rated[j["url"]] = NOW.date().isoformat()
        rated = {u: d for u, d in rated.items() if d >= old}
        (DATA / "rated.json").write_text(json.dumps(rated, indent=0), encoding="utf-8")
        if items and MOTIVATION[0]:
            mot = load("motivation.json", []) + [MOTIVATION[0]]
            (DATA / "motivation.json").write_text(json.dumps(mot[-30:], ensure_ascii=False, indent=1), encoding="utf-8")
        (DATA / "sent.json").write_text(json.dumps(sent, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{len(msgs)} Nachricht(en) mit {len(items)} Stellen gesendet.")
    run, month = record_usage(model)
    status["tokens"] = run
    if not DRY:
        src = load("status.json", {}).get("sources", {})
        zero = [k for k, v in src.items() if not v.get("jobs")]
        lines = [f"📊 <b>Job-Scout · Bericht</b>",
                 f"Neu bewertet: {len(cands)} · gesendet: {len(items)} an {len(chats)} Empfänger",
                 f"Gemini ({esc(model) or '–'}): {fmt(run['total'])} Tokens "
                 f"(Input {fmt(run['prompt'])}, Output {fmt(run['output'])}, Denken {fmt(run['thoughts'])})",
                 f"Monat {NOW.strftime('%m/%Y')}: {fmt(month['total'])} Tokens in {month['runs']} Läufen"]
        if zero:
            lines.append(f"<i>Quellen ohne Treffer: {esc(', '.join(zero))}</i>")
        admin("\n".join(lines))
    (DATA / "digest_status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Fehler als Annotation sichtbar machen und – wenn möglich – kurz an Telegram melden
        print(f"::error::{e}")
        admin(f"⚠️ <b>Job-Scout:</b> Bewertung/Versand fehlgeschlagen:\n{esc(str(e))[:500]}")
        sys.exit(1)
