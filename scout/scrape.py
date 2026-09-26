"""Job-Scout: sammelt Stellenanzeigen aus Jobbörsen, Studio- und Hochschul-Karriereseiten.

Ausgabe:
  data/latest.json  – alle Anzeigen dieses Laufs
  data/new.json     – neu seit dem letzten Lauf
  data/recent.json  – alle, die in den letzten `recent_days` Tagen zum ersten Mal gefunden wurden
  data/seen.json    – url -> first_seen (Tracking)
  data/status.json  – Zusammenfassung pro Quelle
  debug/            – Zähler, Fehler, Screenshots von Quellen ohne Treffer
Aufruf: python scout/scrape.py [quelle ...]   (ohne Argumente: alle)
"""
import datetime as dt
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA, DEBUG = ROOT / "data", ROOT / "debug"
DATA.mkdir(exist_ok=True)
DEBUG.mkdir(exist_ok=True)
(DEBUG / "shots").mkdir(exist_ok=True)

CFG = yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))
INCLUDE = re.compile(re.sub(r"\s+", "", CFG["include_terms"]), re.I)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()


def clean(s, n=300):
    s = re.sub(r"\s+", " ", html.unescape(s or "")).strip()
    return s[:n]


def norm_url(u):
    p = urllib.parse.urlsplit(u)
    q = urllib.parse.parse_qsl(p.query)
    q = [(k, v) for k, v in q if not k.lower().startswith(("utm_", "trk", "refid", "trackingid", "position", "pagenum"))]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), urllib.parse.urlencode(q), ""))


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de-AT,de;q=0.9,en;q=0.8", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- Browser-Extraktion
EXTRACT_JS = r"""
(pattern) => {
  const re = new RegExp(pattern, 'i');
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href;
    if (!href || href.startsWith('javascript') || href.startsWith('mailto')) continue;
    if (!re.test(href)) continue;
    if (href.split('#')[0] === location.href.split('#')[0]) continue;
    // Karte: nach oben laufen, solange der Container nur diesen einen Anzeigen-Link enthält
    let card = a, best = a;
    for (let i = 0; i < 6 && card.parentElement; i++) {
      card = card.parentElement;
      const links = new Set([...card.querySelectorAll('a[href]')].map(x => x.href).filter(h => re.test(h)));
      const t = (card.innerText || '').trim();
      if (links.size > 1 || t.length > 700) break;
      best = card;
    }
    const title = (a.innerText || a.getAttribute('aria-label') || a.title || '').trim();
    const text = (best.innerText || '').trim();
    const key = href.split('#')[0];
    if (seen.has(key)) { continue; }
    seen.add(key);
    out.push({href: key, title, text});
  }
  return out;
}
"""

NAV_WORDS = re.compile(r"^(jobs?|karriere|career|careers|stellen(angebote)?|alle jobs|mehr|more|details|apply|bewerben|"
                       r"zur(ück)?|next|weiter|login|anmelden|home|about|kontakt|contact|impressum|datenschutz)$", re.I)


def browse(page, url, pattern, scrolls):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    # Cookie-Banner wegklicken (best effort)
    for label in ["Alle akzeptieren", "Akzeptieren", "Accept all", "Accept All", "Alle zulassen", "Zustimmen", "OK", "Accept"]:
        try:
            b = page.get_by_role("button", name=label, exact=False)
            if b.count():
                b.first.click(timeout=1500)
                break
        except Exception:
            pass
    for _ in range(scrolls):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(900)
    cards = []
    for fr in page.frames:  # auch eingebettete Job-Widgets (iframes, z. B. Personio/Recruitee)
        try:
            cards += fr.evaluate(EXTRACT_JS, pattern)
        except Exception:
            pass
    return cards


def cards_to_jobs(cards, source, filt):
    jobs = []
    for c in cards:
        title = clean(c["title"], 160)
        text = clean(c["text"], 400)
        if not title or NAV_WORDS.match(title) or len(title) < 4:
            # Titel ggf. aus erster Zeile der Karte
            first = clean((c["text"] or "").split("\n")[0], 160)
            if not first or NAV_WORDS.match(first) or len(first) < 4:
                continue
            title = first
        if filt and not INCLUDE.search(title + " " + text[:120]):
            continue
        jobs.append({"title": title, "snippet": text, "url": norm_url(c["href"]), "source": source})
    return jobs


# ---------------------------------------------------------------- LinkedIn (Gast-API, HTML-Fragmente)
def linkedin(src, keywords, dbg):
    jobs = []
    for kw, start in [(k, s) for k in keywords for s in (0, 10, 20)]:
        params = {"keywords": kw, "location": src["location"], "f_TPR": "r1209600", "start": str(start)}  # letzte 14 Tage
        if src.get("remote"):
            params["f_WT"] = "2"
        url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urllib.parse.urlencode(params)
        try:
            body = http_get(url)
        except Exception as e:
            dbg["errors"].append(f"{kw}: {e}")
            continue
        n = 0
        for li in re.findall(r"<li>(.*?)</li>", body, re.S):
            m_url = re.search(r'class="base-card__full-link[^"]*"\s+href="([^"]+)"', li) or re.search(r'href="(https://[^"]*linkedin\.com/jobs/view/[^"]+)"', li)
            m_t = re.search(r'base-search-card__title[^>]*>(.*?)</h3>', li, re.S)
            if not (m_url and m_t):
                continue
            m_c = re.search(r'base-search-card__subtitle[^>]*>(.*?)</h4>', li, re.S)
            m_l = re.search(r'job-search-card__location[^>]*>(.*?)</span>', li, re.S)
            m_d = re.search(r'datetime="([\d-]+)"', li)
            strip = lambda s: clean(re.sub(r"<[^>]+>", " ", s or ""))
            jobs.append({
                "title": strip(m_t.group(1)),
                "company": strip(m_c.group(1)) if m_c else "",
                "location": strip(m_l.group(1)) if m_l else "",
                "posted": m_d.group(1) if m_d else "",
                "url": norm_url(html.unescape(m_url.group(1)).split("?")[0]),
                "source": src["name"],
                "keyword": kw,
            })
            n += 1
        dbg["per_url"][f"{kw}@{start}"] = n
    return jobs


# ---------------------------------------------------------------- JSON-APIs
def json_api(src, dbg):
    jobs = []
    if src["api"] == "remotive":
        for cat in ["design", "all-others"]:
            d = json.loads(http_get(f"https://remotive.com/api/remote-jobs?category={cat}&limit=300"))
            for j in d.get("jobs", []):
                if not INCLUDE.search(j.get("title", "")):
                    continue
                loc = j.get("candidate_required_location", "")
                if loc and not re.search(r"worldwide|anywhere|europe|emea|austria|germany|dach|cet|eu\b", loc, re.I):
                    continue
                jobs.append({"title": clean(j["title"], 160), "company": j.get("company_name", ""), "location": "Remote – " + loc,
                             "posted": (j.get("publication_date") or "")[:10], "url": norm_url(j["url"]), "source": src["name"],
                             "snippet": clean(re.sub(r"<[^>]+>", " ", j.get("description", "")), 300)})
            dbg["per_url"][cat] = len(jobs)
    elif src["api"] == "arbeitnow":
        for p in range(1, 6):
            d = json.loads(http_get(f"https://www.arbeitnow.com/api/job-board-api?page={p}"))
            for j in d.get("data", []):
                if not INCLUDE.search(j.get("title", "")):
                    continue
                if not (j.get("remote") or re.search(r"wien|vienna|austria|österreich", j.get("location", ""), re.I)):
                    continue
                jobs.append({"title": clean(j["title"], 160), "company": j.get("company_name", ""),
                             "location": ("Remote – " if j.get("remote") else "") + j.get("location", ""),
                             "posted": dt.datetime.fromtimestamp(j.get("created_at", 0), dt.timezone.utc).date().isoformat(),
                             "url": norm_url(j["url"]), "source": src["name"],
                             "snippet": clean(re.sub(r"<[^>]+>", " ", j.get("description", "")), 300)})
            dbg["per_url"][f"page{p}"] = len(jobs)
    return jobs


# ---------------------------------------------------------------- Hauptlauf
def main(only):
    all_jobs, status = [], {}
    kws = CFG["keywords"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-http2"])
        ctx = browser.new_context(user_agent=UA, locale="de-AT", viewport={"width": 1366, "height": 2200})
        page = ctx.new_page()
        for src in CFG["sources"]:
            name = src["name"]
            if only and name not in only:
                continue
            dbg = {"errors": [], "per_url": {}}
            jobs = []
            try:
                if src["type"] == "linkedin":
                    jobs = linkedin(src, kws, dbg)
                elif src["type"] == "json":
                    jobs = json_api(src, dbg)
                else:
                    if src["type"] == "search":
                        urls = [(kw, src["url"].format(q=urllib.parse.quote_plus(kw), slug=re.sub(r"[^a-z0-9]+", "-", kw.lower()).strip("-"))) for kw in kws]
                    else:
                        urls = [(u, u) for u in src["urls"]]
                    for label, url in urls:
                        try:
                            cards = browse(page, url, src["link_pattern"], CFG.get("scrolls", 3))
                            found = cards_to_jobs(cards, name, src.get("filter", False))
                            for j in found:
                                j.setdefault("keyword", label if src["type"] == "search" else "")
                            dbg["per_url"][url] = {"links": len(cards), "jobs": len(found), "final_url": page.url, "title": page.title()[:80]}
                            if not found:
                                safe = re.sub(r"[^a-z0-9]+", "_", (name + "_" + label).lower())[:60]
                                page.screenshot(path=str(DEBUG / "shots" / f"{safe}.png"))
                                # Stichprobe aller Links auf der Seite für die Diagnose
                                dbg["per_url"][url]["sample_links"] = page.evaluate(
                                    "() => [...document.querySelectorAll('a[href]')].map(a => a.href).filter(h => h.startsWith('http')).slice(0, 60)")
                            jobs += found
                        except Exception as e:
                            dbg["errors"].append(f"{url}: {str(e)[:200]}")
            except Exception as e:
                dbg["errors"].append(str(e)[:300])
            # Duplikate innerhalb der Quelle entfernen
            uniq = {}
            for j in jobs:
                uniq.setdefault(j["url"], j)
            jobs = list(uniq.values())
            status[name] = {"jobs": len(jobs), "errors": len(dbg["errors"])}
            (DEBUG / f"{name}.json").write_text(json.dumps(dbg, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{name}: {len(jobs)} Anzeigen, {len(dbg['errors'])} Fehler", flush=True)
            all_jobs += jobs
        browser.close()

    # Duplikate über Quellen hinweg
    uniq = {}
    for j in all_jobs:
        uniq.setdefault(j["url"], j)
    all_jobs = list(uniq.values())

    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    new = []
    for j in all_jobs:
        if j["url"] not in seen:
            seen[j["url"]] = TODAY
            new.append(j)
        j["first_seen"] = seen[j["url"]]

    # recent.json: alles aus den letzten N Tagen – auch Anzeigen, die heute nicht mehr gefunden wurden
    cutoff = (NOW.date() - dt.timedelta(days=CFG.get("recent_days", 8))).isoformat()
    prev_recent = json.loads((DATA / "recent.json").read_text()).get("jobs", []) if (DATA / "recent.json").exists() else []
    recent = {j["url"]: j for j in prev_recent if j.get("first_seen", "") >= cutoff}
    for j in all_jobs:
        if j["first_seen"] >= cutoff:
            recent[j["url"]] = j
    # seen.json schlank halten (> 120 Tage raus)
    old = (NOW.date() - dt.timedelta(days=120)).isoformat()
    seen = {u: d for u, d in seen.items() if d >= old}

    meta = {"generated": NOW.isoformat(timespec="seconds")}
    def dump(fn, obj):
        (DATA / fn).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    dump("latest.json", {**meta, "count": len(all_jobs), "jobs": all_jobs})
    dump("new.json", {**meta, "count": len(new), "jobs": new})
    dump("recent.json", {**meta, "days": CFG.get("recent_days", 8), "count": len(recent), "jobs": sorted(recent.values(), key=lambda j: j["first_seen"], reverse=True)})
    dump("seen.json", seen)
    dump("status.json", {**meta, "jobs": len(all_jobs), "new": len(new), "recent": len(recent), "sources": status})
    print(f"Gesamt: {len(all_jobs)} | neu: {len(new)} | recent: {len(recent)}")


if __name__ == "__main__":
    main(set(sys.argv[1:]))
