import os, json, re
from datetime import datetime, timedelta, timezone
import requests

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
CRIC = os.environ["CRICAPI_KEY"]
ODDS = os.environ["ODDS_API_KEY"]
GEM = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

STATE_FILE = "state.json"
BDT = timezone(timedelta(hours=6))
NOW = datetime.now(timezone.utc)

FULL = {"india", "australia", "england", "pakistan", "south africa",
        "new zealand", "sri lanka", "bangladesh", "west indies",
        "afghanistan", "zimbabwe", "ireland"}
LEAGUE_WORDS = ["indian premier league", "big bash", "bangladesh premier",
                "pakistan super league", "international league t20",
                "asia cup", "champions trophy", "world cup"]
LEAGUE_ABBR = r"\b(ipl|bbl|bpl|sa20|psl|ilt20|icc|acc)\b"
FRANCHISE = ["super kings", "mumbai indians", "royal challengers",
             "knight riders", "sunrisers", "super giants", "punjab kings",
             "sixers", "scorchers", "strikers", "hurricanes", "renegades",
             "qalandars", "zalmi", "gladiators", "sultans",
             "rangpur", "comilla", "khulna", "chattogram", "sylhet",
             "barishal", "desert vipers", "gulf giants", "mi emirates",
             "sharjah warriors", "dubai capitals", "paarl royals",
             "joburg", "durban", "pretoria capitals", "mi cape town"]


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def tg(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text[:4000]}, timeout=20)
        print("telegram status:", r.status_code)
        return r.ok
    except Exception as e:
        print("telegram error:", type(e).__name__)
        return False


def is_major(m):
    name = (m.get("name") or "").lower()
    teams = [t.lower() for t in (m.get("teams") or [])]
    if any(w in name for w in ("women", "u19", "under-19", "emerging")):
        return False
    if any(t.endswith(" a") for t in teams):
        return False
    if any(w in name for w in LEAGUE_WORDS) or re.search(LEAGUE_ABBR, name):
        return True
    if any(f in name for f in FRANCHISE):
        return True
    return len(teams) == 2 and all(t in FULL for t in teams)


def parse(s):
    return datetime.fromisoformat(s)


def fetch_matches(state):
    ts = state.get("matches_ts")
    if ts and NOW - parse(ts) < timedelta(hours=6):
        return state.get("matches", [])
    out, seen = [], set()
    for offset in (0, 25):
        try:
            j = requests.get("https://api.cricapi.com/v1/matches",
                             params={"apikey": CRIC, "offset": offset},
                             timeout=25).json()
            if j.get("status") != "success":
                print("CricAPI problem:", j.get("reason", "unknown"))
                break
            for m in j.get("data", []):
                if m.get("matchEnded") or not m.get("dateTimeGMT"):
                    continue
                if m["id"] in seen or not is_major(m):
                    continue
                seen.add(m["id"])
                start = parse(m["dateTimeGMT"]).replace(tzinfo=timezone.utc)
                out.append({"id": m["id"], "name": m.get("name", ""),
                            "venue": m.get("venue", ""),
                            "teams": m.get("teams", []),
                            "start": start.isoformat()})
        except Exception as e:
            print("CricAPI error:", type(e).__name__)
            break
    state["matches_ts"] = NOW.isoformat()
    if out:
        state["matches"] = out
    return state.get("matches", [])


def weather(venue, start):
    try:
        city = (venue or "").split(",")[-1].strip()
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
        return "Weather: পাওয়া যায়নি"


def fetch_odds():
    events = []
    try:
        sports = requests.get("https://api.the-odds-api.com/v4/sports/",
                              params={"apiKey": ODDS}, timeout=20).json()
        keys = [s["key"] for s in sports
                if s["key"].startswith("cricket") and s.get("active")]
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
            acc, n = {}, 0
            for bm in ev.get("bookmakers", []):
                for mk in bm.get("markets", []):
                    if mk["key"] != "h2h":
                        continue
                    inv = {o["name"]: 1 / o["price"]
                           for o in mk["outcomes"] if o.get("price")}
                    s = sum(inv.values())
                    if not s:
                        continue
                    for k, v in inv.items():
                        acc[k] = acc.get(k, 0) + v / s
                    n += 1
            if n:
                return {k: round(v / n * 100, 1) for k, v in acc.items()}
    return {}


def gemini(m, start, wx, probs):
    prompt = f"""You are a cricket market analyst.
Match: {m['name']}
Venue: {m['venue']}
Start: {start.astimezone(BDT):%d %b %I:%M %p} BDT
Weather: {wx}
Market implied probability: {probs or 'not available'}

Write a short pre-match report in simple Bengali (keep technical terms in English).
Sections:
1) Toss Analysis: which captain should bat or bowl first and why.
2) Match Analysis, with exactly these lines:
Punter Plus On: ...
Bookie Plus On: ...
Verdict: GO WITH BOOKIE or GO WITH PUNTER
3) Session Bias: powerplay and middle overs lean.
Max 900 characters. No markdown. Do not invent player names or stats; say when unsure."""
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
            headers={"x-goog-api-key": GEM},
            json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
        if not r.ok:
            print("Gemini error:", r.status_code, r.text[:300])
            return None
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print("Gemini exception:", type(e).__name__)
        return None


def main():
    state = load_state()
    for k in ("sent", "analysis", "odds"):
        state.setdefault(k, {})

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        tg("✅ Test: Cricket Alerts bot চালু আছে।")

    matches = fetch_matches(state)

    # Daily schedule (first run after 12:01 AM BDT)
    today = NOW.astimezone(BDT).date().isoformat()
    if state.get("schedule_date") != today:
        todays = sorted(
            [m for m in matches
             if parse(m["start"]).astimezone(BDT).date().isoformat() == today],
            key=lambda m: m["start"])
        if todays:
            lines = [f"{parse(m['start']).astimezone(BDT):%I:%M %p} - {m['name']}"
                     for m in todays]
            text = "📅 আজকের বড় ম্যাচ (BDT):\n\n" + "\n".join(lines)
        else:
            text = "📅 আজ কোনো বড় ম্যাচ পাওয়া যায়নি।"
        if tg(text):
            state["schedule_date"] = today

    # Pre-match reports
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

        shift = []
        for t, v in probs.items():
            if t in saved and abs(v - saved[t]) >= 3:
                shift.append(f"{t}: {saved[t]}% -> {v}%")
        if probs:
            state["odds"][m["id"]] = probs

        wx = weather(m["venue"], start)
        analysis = state["analysis"].get(m["id"])
        if not analysis:
            analysis = gemini(m, start, wx, probs)
            if analysis:
                state["analysis"][m["id"]] = analysis
            else:
                analysis = "(Gemini analysis এখন পাওয়া যায়নি)"

        title = {45: "⏳ 45 মিনিট বাকি", 20: "⏰ 20 মিনিট বাকি",
                 5: "🚨 5 মিনিট বাকি"}[stage]
        msg = (f"{title}\n🏏 {m['name']}\n📍 {m['venue']}\n"
               f"🕒 {start.astimezone(BDT):%d %b, %I:%M %p} BDT\n🌦 {wx}\n")
        if probs:
            msg += "📊 " + " | ".join(f"{k} {v}%" for k, v in probs.items()) + "\n"
        if shift:
            msg += "⚠️ Odds shift: " + "; ".join(shift) + "\n"
        msg += "\n" + analysis
        if tg(msg):
            sent.append(stage)

    keep = {m["id"] for m in matches}
    for k in ("sent", "analysis", "odds"):
        state[k] = {i: v for i, v in state[k].items() if i in keep}
    save_state(state)


main()
