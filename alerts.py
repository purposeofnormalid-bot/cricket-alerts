import os, json, re, html
from datetime import datetime, timedelta, timezone
import requests

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
CRIC = os.environ["CRICAPI_KEY"]
ODDS = os.environ["ODDS_API_KEY"]
GEM = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

INCLUDE_WOMEN = False   # Women's ম্যাচ চাইলে True করুন

STATE_FILE = "state.json"
STATE_VERSION = 6
BDT = timezone(timedelta(hours=6))
NOW = datetime.now(timezone.utc)

FULL = {"india", "australia", "england", "pakistan", "south africa",
        "new zealand", "sri lanka", "bangladesh", "west indies",
        "afghanistan", "zimbabwe", "ireland"}
LEAGUE_RE = re.compile(
    r"\b(ipl|indian premier league|big bash|bbl|wbbl|bpl|bangladesh premier|"
    r"psl|pakistan super league|sa20|ilt20|international league t20|cpl|"
    r"caribbean premier|the hundred|major league cricket|mlc|wpl|icc|acc|"
    r"asia cup|world cup|champions trophy)\b")
FRANCHISE = [
    "super kings", "mumbai indians", "royal challengers", "knight riders",
    "sunrisers", "super giants", "punjab kings", "gujarat titans",
    "rajasthan royals", "delhi capitals", "lucknow",
    "sydney sixers", "sydney thunder", "perth scorchers",
    "adelaide strikers", "brisbane heat", "hobart hurricanes",
    "melbourne stars", "melbourne renegades",
    "qalandars", "peshawar zalmi", "karachi kings", "multan sultans",
    "islamabad united", "quetta gladiators",
    "rangpur riders", "comilla victorians", "khulna tigers",
    "chattogram", "sylhet strikers", "fortune barishal", "dhaka",
    "desert vipers", "gulf giants", "mi emirates", "sharjah warriors",
    "dubai capitals", "abu dhabi knight riders",
    "paarl royals", "joburg super kings", "durban", "pretoria capitals",
    "mi cape town", "sunrisers eastern cape"]
BAD_ALWAYS = ("u19", "under-19", "under 19", "emerging", "development",
              "academy", "softball", "rising stars")
WOMEN = ("women", "wbbl", "wpl", "-w ", " w vs", "(w)")
SERIES_KEYWORDS = ["ILT20", "Indian Premier League", "Big Bash",
                   "Bangladesh Premier", "Pakistan Super League", "SA20",
                   "Caribbean Premier", "The Hundred", "Major League Cricket"]
YEARS = ("2026", "2027")

esc = html.escape


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        if s.get("v") == STATE_VERSION:
            return s
    except Exception:
        pass
    return {"v": STATE_VERSION}


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def tg(text, use_html=True):
    try:
        data = {"chat_id": TG_CHAT, "text": text[:4000]}
        if use_html:
            data["parse_mode"] = "HTML"
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=data, timeout=20)
        print("telegram status:", r.status_code)
        if not r.ok and use_html:
            plain = re.sub(r"<[^>]+>", "", html.unescape(text))
            return tg(plain, use_html=False)
        return r.ok
    except Exception as e:
        print("telegram error:", type(e).__name__)
        return False


def parse(s):
    return datetime.fromisoformat(s)


def parse_dt(s):
    try:
        d = parse(str(s).replace("Z", ""))
        if d.tzinfo is None:
            return d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def api(ep, **params):
    try:
        j = requests.get(f"https://api.cricapi.com/v1/{ep}",
                         params={"apikey": CRIC, **params},
                         timeout=25).json()
    except Exception as e:
        print(ep, "error:", type(e).__name__)
        return {}
    info = j.get("info", {})
    print(ep, {k: v for k, v in params.items() if k != "apikey"}, "->",
          j.get("status"), "| hits:", info.get("hitsToday"), "/",
          info.get("hitsLimit"))
    if j.get("status") != "success":
        print("CricAPI problem:", j.get("reason", "unknown"))
    return j


def bad_text(s):
    s = (s or "").lower() + " "
    if any(b in s for b in BAD_ALWAYS):
        return True
    return (not INCLUDE_WOMEN) and any(w in s for w in WOMEN)


def is_bad(m):
    if bad_text(m.get("name")) or bad_text(m.get("series")):
        return True
    teams = [t.lower().strip() for t in (m.get("teams") or [])]
    if len(teams) < 2 or any(t in ("", "tbc") for t in teams):
        return True
    if any(t.endswith(" a") for t in teams):
        return True
    if not INCLUDE_WOMEN and any(t.endswith("-w") or t.endswith(" women")
                                 for t in teams):
        return True
    return False


def is_major(m):
    s = f"{m.get('name', '')} {m.get('series', '')}".lower()
    teams = [t.lower() for t in (m.get("teams") or [])]
    if LEAGUE_RE.search(s):
        return True
    if any(f in s for f in FRANCHISE):
        return True
    return len(teams) == 2 and all(t in FULL for t in teams)


def clean_team(t):
    return re.sub(r"\s*\[.*?\]", "", t or "").strip()


def cricscore_matches():
    j = api("cricScore")
    data = j.get("data") or []
    print("cricScore rows:", len(data))
    sample = sorted({str(x.get("series")) for x in data})[:12]
    print("cricScore series sample:", sample)
    out = []
    for x in data:
        if x.get("ms") == "result" or not x.get("dateTimeGMT"):
            continue
        teams = [clean_team(x.get("t1")), clean_team(x.get("t2"))]
        out.append({"id": x["id"],
                    "name": f"{teams[0]} vs {teams[1]}, {x.get('matchType', '')}",
                    "venue": "", "teams": teams,
                    "type": (x.get("matchType") or "").upper(),
                    "series": x.get("series") or "",
                    "dt": x["dateTimeGMT"], "trusted": False})
    return out


def active_series(state):
    ts = state.get("series_ts")
    if ts and NOW - parse(ts) < timedelta(days=3):
        return state.get("series_ids", [])
    found = {}
    for kw in SERIES_KEYWORDS:
        j = api("series", offset=0, search=kw)
        for s in j.get("data") or []:
            name = s.get("name", "")
            if not any(y in name for y in YEARS) or bad_text(name):
                continue
            end = parse_dt(s.get("endDate", ""))
            if end and end < NOW - timedelta(days=2):
                continue
            found[s["id"]] = name
    ids = [[i, n] for i, n in found.items()][:8]
    print("series found:", [n for _, n in ids])
    state["series_ts"] = NOW.isoformat()
    state["series_ids"] = ids
    return ids


def series_matches(state):
    out = []
    for sid, sname in active_series(state):
        j = api("series_info", id=sid)
        for m in (j.get("data") or {}).get("matchList") or []:
            if m.get("matchEnded") or not m.get("dateTimeGMT"):
                continue
            teams = m.get("teams") or [t.get("name", "")
                                       for t in m.get("teamInfo", [])]
            out.append({"id": m["id"], "name": m.get("name", ""),
                        "venue": m.get("venue", ""), "teams": teams,
                        "type": (m.get("matchType") or "").upper(),
                        "series": sname, "dt": m["dateTimeGMT"],
                        "trusted": True})
    return out


def fetch_matches(state):
    ts = state.get("matches_ts")
    if ts and NOW - parse(ts) < timedelta(hours=6):
        return state.get("matches", [])

    cands = series_matches(state) + cricscore_matches()
    out, seen, keys, shown = [], set(), set(), 0
    for m in cands:
        if is_bad(m):
            continue
        start = parse_dt(m["dt"])
        if (not start or start - NOW > timedelta(hours=36)
                or NOW - start > timedelta(hours=8)):
            continue
        key = (tuple(sorted(t.lower() for t in m["teams"])),
               start.strftime("%Y-%m-%d"))
        if m["id"] in seen or key in keys:
            continue
        if not (m["trusted"] or is_major(m)):
            if shown < 15:
                print("not major:", m["name"], "|", m.get("series", ""))
                shown += 1
            continue
        seen.add(m["id"])
        keys.add(key)
        print("major:", m["name"], "|",
              start.astimezone(BDT).strftime("%d %b %I:%M %p"), "BDT")
        out.append({"id": m["id"], "name": m["name"], "venue": m["venue"],
                    "teams": m["teams"], "type": m["type"],
                    "series": m["series"], "start": start.isoformat()})
    print(f"candidates: {len(cands)}, major found: {len(out)}")
    # পাওয়া গেলে ৬ ঘণ্টা cache; না পেলে ৪ ঘণ্টা পর আবার চেষ্টা
    state["matches_ts"] = (NOW if out else NOW - timedelta(hours=2)).isoformat()
    state["matches"] = out
    return out


def match_info(mid):
    try:
        j = requests.get("https://api.cricapi.com/v1/match_info",
                         params={"apikey": CRIC, "id": mid}, timeout=25).json()
        d = j.get("data", {})
        if d.get("tossWinner"):
            toss = (f"Toss: {d['tossWinner']} won the toss and chose to "
                    f"{d.get('tossChoice', '?')}")
        else:
            toss = "Toss: not happened yet / not in data"
        return toss, d.get("venue", "")
    except Exception:
        return "Toss: data unavailable", ""


def weather(venue, start):
    try:
        parts = [p.strip() for p in (venue or "").split(",")]
        city = parts[-2] if len(parts) >= 3 else parts[-1]
        g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": city, "count": 1}, timeout=15).json()
        p = g["results"][0]
        w = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": p["latitude"], "longitude": p["longitude"],
            "hourly": "precipitation_probability,relative_humidity_2m,dew_point_2m",
            "forecast_days": 3, "timezone": "UTC"}, timeout=15).json()["hourly"]
        i = w["time"].index(start.strftime("%Y-%m-%dT%H:00"))
        return (f"Rain {w['precipitation_probability'][i]}%, "
                f"Humidity {w['relative_humidity_2m'][i]}%, "
                f"Dew point {w['dew_point_2m'][i]}C")
    except Exception:
        return "Weather: not available"


def fetch_odds():
    events = []
    try:
        sports = requests.get("https://api.the-odds-api.com/v4/sports/",
                              params={"apiKey": ODDS}, timeout=20).json()
        keys = [s["key"] for s in sports
                if s["key"].startswith("cricket") and s.get("active")]
        print("odds sport keys:", keys)
        for k in keys:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{k}/odds/",
                params={"apiKey": ODDS, "regions": "uk", "markets": "h2h",
                        "oddsFormat": "decimal"}, timeout=20)
            if r.ok:
                events += r.json()
    except Exception as e:
        print("Odds API error:", type(e).__name__)
    return events


def probs_for(match, events):
    teams = [t.lower() for t in match["teams"]]
    for ev in events:
        a, b = ev["home_team"].lower(), ev["away_team"].lower()
        if teams and all(any(t in x or x in t for x in (a, b)) for t in teams):
            acc_p, acc_o, cnt = {}, {}, {}
            for bm in ev.get("bookmakers", []):
                for mk in bm.get("markets", []):
                    if mk["key"] != "h2h":
                        continue
                    inv = {o["name"]: 1 / o["price"]
                           for o in mk["outcomes"] if o.get("price")}
                    s = sum(inv.values())
                    if not s:
                        continue
                    for o in mk["outcomes"]:
                        n = o["name"]
                        if n in inv:
                            acc_p[n] = acc_p.get(n, 0) + inv[n] / s
                            acc_o[n] = acc_o.get(n, 0) + o["price"]
                            cnt[n] = cnt.get(n, 0) + 1
            if cnt:
                return {n: {"o": round(acc_o[n] / cnt[n], 2),
                            "p": round(acc_p[n] / cnt[n] * 100, 1)}
                        for n in cnt}
    return {}


def gemini(m, start, stage, wx, toss, odds_txt):
    vs = " vs ".join(m["teams"]) or m["name"]
    prompt = f"""You are a pre-match cricket TRADING and probability analyst. Write the report for the match below.

DATA (the only source of numbers you may use):
- Match: {vs} ({m.get('type', '')}, {m.get('series', '')})
- Venue: {m['venue']}
- Start: {start.astimezone(BDT):%d %b %I:%M %p} BDT ({stage} minutes before start)
- Weather: {wx}
- {toss}
- Odds (h2h, average of bookmakers): {odds_txt}

RULES:
- Never invent odds, player names, captains, or stats. If something is not in DATA and you are not confident, write N/A.
- H2H, venue toss/bat-bowl % and pitch notes may come from your general knowledge only if confident; mark them (AI estimate).
- No toss market is in DATA: say toss market not available (standard ~1.90 each).
- Write reasons in short, simple Bengali; keep technical terms in English.
- Plain text. Bold only with **double asterisks** for headings. Max 2800 characters total. One short line per numbered point.
- Output EXACTLY this structure, naming both teams:

━━━ 🪙 **TOSS ANALYSIS (PRE-TOSS)** ━━━
1. **Toss Market & Odds:** ...
2. **Toss Sentiment & Trigger:** ...
3. **Trader & Sharp Toss Lean:** ...
4. **Captaincy & Venue Toss Stats:** ...
5. **Weather, Dew & Team News:** ...
• **Punter Plus On -** [Team]
• **Bookie Plus On -** [Team]
• **GO WITH [BOOKIE or PUNTER]** — (reason)
> 🎯 **AI Toss Verdict:** [predicted toss winner + bat/bowl call, or ⚠️ Skip]

━━━ 📈 **MATCH ANALYSIS (PRE-MATCH)** ━━━
1. **Match Market & Odds Shift:** both teams opening ➔ current and % shift
2. **Graph Direction & Catalyst:** ...
3. **Trader Movement & Sharp News:** ...
4. **H2H & Venue Edge:** ...
5. **Team News & Playing XI:** ...
• **Punter Plus On -** [Team]
• **Bookie Plus On -** [Team]
• **GO WITH [BOOKIE or PUNTER]** — (reason)
> 🎯 **AI Match Verdict:** [Team — clear edge, or ⚠️ NO CLEAR EDGE — SKIP]

━━━ ⚡ **SESSION BIAS (PRE-MATCH)** ━━━
• **6-Over PP:** [range] | **1st Inn Par:** [range] | **Bias:** [Over / Under / Neutral] — reason
"""
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
            headers={"x-goog-api-key": GEM},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"maxOutputTokens": 2500,
                                       "temperature": 0.4}},
            timeout=90)
        if not r.ok:
            print("Gemini error:", r.status_code, r.text[:300])
            return None
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print("Gemini exception:", type(e).__name__)
        return None


def to_html(t):
    t = esc(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return t.replace("`", "")


def label(m):
    t, s = m.get("type", ""), m.get("series", "")
    return f"{t} • {s}" if s else t


def main():
    state = load_state()
    for k in ("sent", "odds", "open"):
        state.setdefault(k, {})

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        tg("✅ Test: Cricket Alerts bot চালু আছে।")

    matches = fetch_matches(state)

    # Format A: daily digest (first run after 12:01 AM BDT)
    today = NOW.astimezone(BDT).date().isoformat()
    if state.get("schedule_date") != today:
        todays = sorted(
            [m for m in matches
             if parse(m["start"]).astimezone(BDT).date().isoformat() == today],
            key=lambda m: m["start"])
        d = NOW.astimezone(BDT).strftime("%d %b %Y")
        head = (f"📅 <b>TODAY'S MAJOR CRICKET SCHEDULE ({d})</b> | "
                f"📊 <b>Total Major Matches:</b> <code>{len(todays)}</code>\n\n")
        lines = []
        for i, m in enumerate(todays, 1):
            t = parse(m["start"]).astimezone(BDT).strftime("%I:%M %p")
            vs = " vs ".join(m["teams"]) or m["name"]
            lines.append(f"{i}. 🏏 <b>{esc(label(m))}</b> | 🔥 <b>{esc(vs)}</b> | "
                         f"⏰ <code>{t} BDT</code> | 🏟️ {esc(m['venue'])}")
        if tg(head + ("\n".join(lines) if lines else "আজ কোনো major ম্যাচ নেই।")):
            state["schedule_date"] = today

    # Format B: pre-match reports
    events = None
    for m in matches:
        start = parse(m["start"])
        mins = (start - NOW).total_seconds() / 60
        if -10 < mins <= 5:
            stage = 5
        elif 5 < mins <= 20:
            stage = 20
        elif 20 < mins <= 45:
            stage = 45
        else:
            continue
        sent = state["sent"].setdefault(m["id"], [])
        if stage in sent:
            continue

        saved = state["odds"].get(m["id"], {})
        probs = saved
        if stage in (45, 5) or not saved:
            if events is None:
                events = fetch_odds()
            probs = probs_for(m, events) or saved
        if probs:
            state["odds"][m["id"]] = probs
            state["open"].setdefault(m["id"], probs)
        opening = state["open"].get(m["id"], {})
        odds_txt = "; ".join(
            f"{t}: opening {opening[t]['o']} -> current {v['o']} "
            f"(implied {opening[t]['p']}% -> {v['p']}%)"
            for t, v in probs.items() if t in opening) or "not available"

        toss = "Toss: not happened yet"
        if stage in (20, 5) or not m.get("venue"):
            toss2, v = match_info(m["id"])
            if v and not m.get("venue"):
                m["venue"] = v
            if stage in (20, 5):
                toss = toss2
        wx = weather(m["venue"], start)
        analysis = gemini(m, start, stage, wx, toss, odds_txt)

        vs = " vs ".join(m["teams"]) or m["name"]
        head = (f"🏏 <b>{esc(label(m))}</b> | 🔥 <b>{esc(vs)}</b>\n"
                f"🏟️ Venue: {esc(m['venue'])} | ⏰ <code>{stage}m Pre-Match</code>\n"
                f"🕒 {start.astimezone(BDT):%d %b, %I:%M %p} BDT\n\n")
        if analysis:
            body = to_html(analysis[:3300])
        else:
            body = (f"(AI analysis এখন পাওয়া যায়নি)\n📊 {esc(odds_txt)}\n"
                    f"🌦 {esc(wx)}\n{esc(toss)}")
        if tg(head + body):
            sent.append(stage)

    keep = {m["id"] for m in matches}
    for k in ("sent", "odds", "open"):
        state[k] = {i: v for i, v in state[k].items() if i in keep}
    save_state(state)


main()
