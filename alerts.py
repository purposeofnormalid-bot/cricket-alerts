import os, json, re, html
from datetime import datetime, timedelta, timezone
import requests

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
CRIC = os.environ["CRICAPI_KEY"]
ODDS = os.environ["ODDS_API_KEY"]
GEM = os.environ["GEMINI_API_KEY"]
PRIMARY_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
FALLBACK_MODEL = "gemini-3.5-flash-lite"

INCLUDE_WOMEN = False
STATE_FILE = "state.json"
STATE_VERSION = 10
BDT = timezone(timedelta(hours=6))
NOW = datetime.now(timezone.utc)

FULL = {
    "india", "australia", "england", "pakistan", "south africa",
    "new zealand", "sri lanka", "bangladesh", "west indies",
    "afghanistan", "zimbabwe", "ireland"
}
LEAGUE_RE = re.compile(
    r"\b(ipl|indian premier league|big bash|bbl|wbbl|bpl|bangladesh premier|"
    r"psl|pakistan super league|sa20|ilt20|international league t20|cpl|"
    r"caribbean premier|the hundred|major league cricket|mlc|wpl|icc|acc|"
    r"asia cup|world cup|champions trophy)\b"
)
FRANCHISE = [
    "super kings", "mumbai indians", "royal challengers", "knight riders",
    "sunrisers", "super giants", "punjab kings", "gujarat titans",
    "rajasthan royals", "delhi capitals", "lucknow", "sydney sixers",
    "sydney thunder", "perth scorchers", "adelaide strikers", "brisbane heat",
    "hobart hurricanes", "melbourne stars", "melbourne renegades", "qalandars",
    "peshawar zalmi", "karachi kings", "multan sultans", "islamabad united",
    "quetta gladiators", "rangpur riders", "comilla victorians", "khulna tigers",
    "chattogram", "sylhet strikers", "fortune barishal", "dhaka", "desert vipers",
    "gulf giants", "mi emirates", "sharjah warriors", "dubai capitals",
    "abu dhabi knight riders", "paarl royals", "joburg super kings", "durban",
    "pretoria capitals", "mi cape town", "sunrisers eastern cape"
]
BAD_ALWAYS = ("u19", "under-19", "under 19", "emerging", "development", "academy", "softball", "rising stars")
WOMEN = ("women", "wbbl", "wpl", "-w ", " w vs", "(w)")
SERIES_KEYWORDS = [
    "ILT20", "International League T20", "Indian Premier League", "Big Bash",
    "Bangladesh Premier", "Pakistan Super League", "SA20", "Caribbean Premier",
    "The Hundred", "Major League Cricket"
]
YEARS = ("2026", "2027")
esc = html.escape
API_DIAG = {"hits": "?", "limit": "?", "reason": ""}


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        if s.get("v") in (7, 8, 9, STATE_VERSION):
            s["v"] = STATE_VERSION
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
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=data, timeout=20)
        if not r.ok and use_html:
            return tg(re.sub(r"<[^>]+>", "", html.unescape(text)), use_html=False)
        return r.ok
    except Exception:
        return False


def parse(s):
    return datetime.fromisoformat(s)


def parse_dt(s):
    try:
        d = parse(str(s).replace("Z", ""))
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    except Exception:
        return None


def api(ep, **params):
    try:
        j = requests.get(f"https://api.cricapi.com/v1/{ep}", params={"apikey": CRIC, **params}, timeout=25).json()
    except Exception:
        return {}
    info = j.get("info", {})
    if info:
        API_DIAG["hits"] = info.get("hitsToday", API_DIAG["hits"])
        API_DIAG["limit"] = info.get("hitsLimit", API_DIAG["limit"])
    if j.get("status") != "success":
        API_DIAG["reason"] = j.get("reason", "unknown")
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
    if len(teams) < 2 or any(t in ("", "tbc") for t in teams) or any(t.endswith(" a") for t in teams):
        return True
    return (not INCLUDE_WOMEN) and any(t.endswith("-w") or t.endswith(" women") for t in teams)


def is_major(m):
    s = f"{m.get('name', '')} {m.get('series', '')}".lower()
    teams = [t.lower() for t in (m.get("teams") or [])]
    if LEAGUE_RE.search(s) or any(f in s for f in FRANCHISE):
        return True
    return len(teams) == 2 and all(t in FULL for t in teams)


def clean_team(t):
    return re.sub(r"\s*\[.*?\]", "", t or "").strip()


def cricscore_matches():
    out = []
    for x in api("cricScore").get("data") or []:
        if x.get("ms") == "result" or not x.get("dateTimeGMT"):
            continue
        teams = [clean_team(x.get("t1")), clean_team(x.get("t2"))]
        out.append({
            "id": x["id"], "name": f"{teams[0]} vs {teams[1]}, {x.get('matchType', '')}",
            "venue": "", "teams": teams, "type": (x.get("matchType") or "").upper(),
            "series": x.get("series") or "", "dt": x["dateTimeGMT"], "trusted": False
        })
    return out


def active_series(state):
    ts = state.get("series_ts")
    if ts and NOW - parse(ts) < timedelta(days=3):
        return state.get("series_ids", [])
    found = {}
    for kw in SERIES_KEYWORDS:
        for s in api("series", offset=0, search=kw).get("data") or []:
            name = s.get("name", "")
            if not any(y in name for y in YEARS) or bad_text(name):
                continue
            end = parse_dt(s.get("endDate", ""))
            if end and end < NOW - timedelta(days=2):
                continue
            found[s["id"]] = name
    ids = [[i, n] for i, n in found.items()][:8]
    state["series_ts"] = NOW.isoformat()
    state["series_ids"] = ids
    return ids


def series_matches(state):
    out = []
    for sid, sname in active_series(state):
        for m in (api("series_info", id=sid).get("data") or {}).get("matchList") or []:
            if m.get("matchEnded") or not m.get("dateTimeGMT"):
                continue
            teams = m.get("teams") or [t.get("name", "") for t in m.get("teamInfo", [])]
            out.append({
                "id": m["id"], "name": m.get("name", ""), "venue": m.get("venue", ""),
                "teams": teams, "type": (m.get("matchType") or "").upper(),
                "series": sname, "dt": m["dateTimeGMT"], "trusted": True
            })
    return out


def fetch_matches(state):
    ts = state.get("matches_ts")
    if ts and NOW - parse(ts) < timedelta(hours=6):
        return state.get("matches", [])
    out, seen, keys = [], set(), set()
    for m in series_matches(state) + cricscore_matches():
        if is_bad(m):
            continue
        start = parse_dt(m["dt"])
        if not start or start - NOW > timedelta(hours=36) or NOW - start > timedelta(hours=9):
            continue
        key = (tuple(sorted(t.lower() for t in m["teams"])), start.strftime("%Y-%m-%d"))
        if m["id"] in seen or key in keys or not (m["trusted"] or is_major(m)):
            continue
        seen.add(m["id"])
        keys.add(key)
        out.append({
            "id": m["id"], "name": m["name"], "venue": m["venue"], "teams": m["teams"],
            "type": m["type"], "series": m["series"], "start": start.isoformat()
        })
    state["matches_ts"] = (NOW if out else NOW - timedelta(hours=3)).isoformat()
    state["matches"] = out
    return out


def fetch_live_map(matches, state, force=False):
    if not any(-360 <= (parse(m["start"]) - NOW).total_seconds() / 60 <= 65 for m in matches):
        return {}
    ts = state.get("live_ts")
    if not force and ts and NOW - parse(ts) < timedelta(minutes=10):
        return state.get("live_cache", {})
    live_map = {x["id"]: x for x in (api("currentMatches", offset=0).get("data") or [])}
    state["live_ts"] = NOW.isoformat()
    state["live_cache"] = live_map
    return live_map


def match_details(mid, live_obj=None):
    toss = "Pending (Toss ~30m pre-match)"
    venue = ""
    status = (live_obj or {}).get("status", "")
    score = (live_obj or {}).get("score") or []
    started = bool((live_obj or {}).get("matchStarted"))
    ended = bool((live_obj or {}).get("matchEnded"))
    xi_txt = "Pending"
    try:
        d = api("match_info", id=mid).get("data") or {}
        venue = d.get("venue") or venue
        status = d.get("status") or status
        score = d.get("score") or score
        started = bool(d.get("matchStarted", started))
        ended = bool(d.get("matchEnded", ended))
        if d.get("tossWinner"):
            toss = f"{d['tossWinner']} won toss & elected to {d.get('tossChoice', '?')}"
        xi_parts = []
        for tb in (d.get("players") or [])[:2]:
            plist = [p.get("name", "") for p in (tb.get("players") or [])[:11]]
            if plist:
                xi_parts.append(f"{tb.get('teamName', '')}: {', '.join(plist)}")
        if xi_parts:
            xi_txt = " | ".join(xi_parts)
    except Exception:
        pass
    score_txt = " | ".join(
        f"{s.get('inning', 'Inning')}: {s.get('r', 0)}/{s.get('w', 0)} ({s.get('o', 0)} ov)"
        for s in score
    ) or "Not started"
    return {"toss": toss, "venue": venue, "status": status, "score": score, "score_txt": score_txt, "started": started, "ended": ended, "xi": xi_txt}


def detect_inplay_stage(m_type, info):
    if info["ended"] or not info["started"] or not info["score"]:
        return None
    score, st = info["score"], (info["status"] or "").lower()
    if len(score) == 1:
        try:
            ov = float(score[0].get("o", 0))
        except Exception:
            ov = 0.0
        if "break" in st or (m_type in ("T20", "T20I") and ov >= 18.5) or (m_type == "ODI" and ov >= 48.5):
            return "break"
        if m_type in ("T20", "T20I", "ODI") and 9.0 <= ov <= 13.5:
            return "10ov"
        if m_type in ("T20", "T20I", "ODI") and 5.0 <= ov <= 8.5:
            return "6ov"
    if len(score) >= 2:
        try:
            if float(score[1].get("o", 99)) <= 2.5:
                return "break"
        except Exception:
            pass
    return None


def weather_info(venue, start):
    try:
        parts = [p.strip() for p in (venue or "").split(",")]
        city = parts[-2] if len(parts) >= 3 else parts[-1]
        p = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1}, timeout=15).json()["results"][0]
        w = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": p["latitude"], "longitude": p["longitude"],
            "hourly": "precipitation_probability,cloud_cover,relative_humidity_2m,dew_point_2m",
            "forecast_days": 3, "timezone": "UTC"
        }, timeout=15).json()["hourly"]
        i = w["time"].index(start.strftime("%Y-%m-%dT%H:00"))
        is_night = start.astimezone(BDT).hour >= 17
        dew_tag = "High" if (is_night and (w["relative_humidity_2m"][i] >= 68 or w["dew_point_2m"][i] >= 17)) else "Low"
        return f"Rain {w['precipitation_probability'][i]}% | Cloud {w['cloud_cover'][i]}% | Dew {w['dew_point_2m'][i]}°C ({dew_tag})"
    except Exception:
        return "Weather N/A"


def research_baseline(m, start, wx):
    fmt = (m.get("type") or "").upper()
    is_night = start.astimezone(BDT).hour >= 17
    high_dew = "(High)" in wx
    if "TEST" in fmt:
        return "High Impact (+4.9% Edge; 47% vs 34% in 5 yrs)", "Test Home Bias (~59% Win | +0.50 Log-Odds)", "Bat First"
    if "ODI" in fmt:
        ti = "Critical Dew Edge (+5.5% Night Chase)" if high_dew else "Low Impact (+2.2% Baseline)"
        return ti, "ODI Home Bias (~59% Win | +0.50 Log-Odds)", ("Bowl First" if (high_dew or is_night) else "Bat First")
    ti = "Critical Dew Edge (+6.5% Win Boost)" if high_dew else "Low-Moderate (+2.8% Baseline)"
    return ti, "T20 Neutral Home (50% Baseline)", ("Bowl First" if (high_dew or is_night) else "Bat First")


def fetch_odds():
    events = []
    try:
        sports = requests.get("https://api.the-odds-api.com/v4/sports/", params={"apiKey": ODDS}, timeout=20).json()
        for k in [s["key"] for s in sports if s["key"].startswith("cricket") and s.get("active")]:
            r = requests.get(f"https://api.the-odds-api.com/v4/sports/{k}/odds/", params={"apiKey": ODDS, "regions": "uk,au,eu", "markets": "h2h", "oddsFormat": "decimal"}, timeout=20)
            if r.ok:
                events += r.json()
    except Exception:
        pass
    return events


def team_match(t1, t2):
    a, b = t1.lower().strip(), t2.lower().strip()
    if a in b or b in a:
        return True
    wa = set(a.split()) - {"cricket", "club", "team"}
    wb = set(b.split()) - {"cricket", "club", "team"}
    return bool(wa and wb and (wa & wb))


def probs_for(match, events):
    teams = match["teams"]
    if len(teams) < 2:
        return {}
    for ev in events:
        ha, aw = ev.get("home_team", ""), ev.get("away_team", "")
        if (team_match(teams[0], ha) and team_match(teams[1], aw)) or (team_match(teams[0], aw) and team_match(teams[1], ha)):
            acc_p, acc_o, cnt = {}, {}, {}
            for bm in ev.get("bookmakers", []):
                for mk in bm.get("markets", []):
                    if mk["key"] != "h2h":
                        continue
                    inv = {o["name"]: 1 / o["price"] for o in mk["outcomes"] if o.get("price")}
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
                return {n: {"o": round(acc_o[n] / cnt[n], 2), "p": round(acc_p[n] / cnt[n] * 100, 1)} for n in cnt}
    return {}


def analyze_market_flow(opening, probs):
    if not probs or not opening:
        return "Odds Pending (Not Listed Yet)", "Pending Market Open"
    parts, max_shift, flow_team = [], 0.0, ""
    for t, v in probs.items():
        if t in opening:
            diff = round(v["p"] - opening[t]["p"], 1)
            parts.append(f"{t} {v['o']} ({v['p']}%)")
            if abs(diff) > abs(max_shift):
                max_shift, flow_team = diff, t
    odds_txt = " vs ".join(parts) + f" | Shift: {max_shift:+.1f}%"
    if max_shift >= 3.5:
        flow_txt = f"Heavy Flow on {flow_team} ({max_shift:+.1f}% Implied Shift)"
    elif max_shift >= 1.5:
        flow_txt = f"Mild Flow on {flow_team} ({max_shift:+.1f}% Shift)"
    elif max_shift <= -1.5:
        flow_txt = f"Drift Against {flow_team} ({max_shift:+.1f}% Shift)"
    else:
        flow_txt = "Balanced Market (No One-Sided Flow)"
    return odds_txt, flow_txt


def call_gemini(prompt, markers):
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        for use_search in (True, False):
            payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 4096, "temperature": 0.25}}
            if use_search:
                payload["tools"] = [{"google_search": {}}]
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", headers={"x-goog-api-key": GEM}, json=payload, timeout=90)
                if r.ok:
                    txt = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()
                    if txt and all(m in txt for m in markers):
                        return txt
            except Exception:
                pass
    return None


def generate_report(m, start, stage_key, wx, info, odds_txt, flow_txt, base_rules):
    vs = " vs ".join(m["teams"]) or m["name"]
    t1 = m["teams"][0] if len(m["teams"]) >= 1 else "Team A"
    t2 = m["teams"][1] if len(m["teams"]) >= 2 else "Team B"
    toss_impact, home_bias, expected_call = base_rules

    if stage_key == "toss":
        prompt = f"""You are a quantitative cricket analyst. Output ONLY clean English key points with concise explanations.
DATA: Match={vs} ({m.get('type', '')}) | Venue={m['venue']} | Weather={wx} | Toss Impact={toss_impact} | Optimal Call={expected_call} | Odds={odds_txt} | Flow={flow_txt}
RULES: 100% English only. No square brackets `[...]` in output. Pick ONE team for Expected Toss Winner (52% vs 48% to 56% vs 44%). Follow this EXACT structure:

1. **Toss Impact Rating:** {toss_impact}
2. **Captain & Venue Trend:** Captain A XX% vs Captain B YY% | Venue: ZZ% Bowl/Bat First
3. **Pitch & Dew Trigger:** Flat/Green/Dry Track | {wx}
4. **Public & Market Flow:** {odds_txt} | {flow_txt}
5. **Expected Toss Decision:** {expected_call} (XX% Probability)
6. **Toss Basis & Explanation:** (1-2 crisp sentences explaining why this toss call & team have the tactical edge here, noting coin flip variance)

> 🎯 **Expected Toss Winner:** Winner Name (XX% vs YY%) — Key tactical reason"""
        return call_gemini(prompt, ["Toss Impact Rating", "Expected Toss Winner"])

    stage_labels = {"prematch": "15m Post-Toss Final", "6ov": "Live 6-Over PP Pulse", "10ov": "Live 10-Over Pulse", "break": "1st Innings Break • Golden Window", "preview": "Live / Pre-Match Snapshot"}
    inn_line = "• **1st Innings Target:** Actual Target XXX | **Chase:** XX% | **Defend:** YY%" if stage_key == "break" else "• **1st Innings Par (Line: XXX.5):** XXX – YYY | **Over:** XX% | **Under:** YY%"
    prompt = f"""You are a quantitative cricket trading analyst. Output ONLY clean English key points for **{stage_labels.get(stage_key, stage_key)}** with clear explanations.
DATA: Match={vs} ({m.get('type', '')}) | Venue={m['venue']} | Live={info['score_txt']} | Toss={info['toss']} | XI={info['xi']} | Weather={wx} | Home={home_bias} | Odds={odds_txt} | Flow={flow_txt}
RULES: 100% English only. No square brackets `[...]` in output. Over%+Under%=100%, Chasing%+Defending%=100%, {t1}%+{t2}%=100%. Use CLEAR VALUE EDGE only if divergence >4.5%, else SLIGHT EDGE or NO CLEAR EDGE — SKIP. Follow this EXACT structure:

━━━ 🪙 **TOSS & MATCH KEY POINTS** ━━━
1. **Toss & Condition Reality:** {info['toss']} | Pitch & dew impact summary
2. **Market Odds (H2H):** {odds_txt}
3. **Public & Sharp Flow:** {flow_txt}
4. **Model vs. Market:** Model Fair Win: {t1} XX.X% vs {t2} YY.Y% | Gap: +/-X.X%
5. **Match Edge Explanation:** (1-2 crisp sentences explaining why the favored team holds the edge based on XI strength, pitch behavior, home bias, and live score/RRR)

> 🎯 **Expected Match Winner:** Team Name (XX.X%) — CLEAR VALUE EDGE / SLIGHT EDGE / NO CLEAR EDGE — SKIP

━━━ ⚡ **SESSION & CHASE PROBABILITIES** ━━━
• **6-Over PP (Line: XX.5):** XX – YY | **Over:** XX% | **Under:** YY%
• **10-Over Session (Line: XX.5):** XX – YY | **Over:** XX% | **Under:** YY%
{inn_line}
• **Match Outcome Split:** **Chasing Win:** XX% | **Defending Win:** YY%
• **Session & Chase Basis:** (1-2 crisp sentences explaining the exact statistical/pitch/dew/RRR basis for these session lines, Over/Under %, and Chase/Defend %)"""
    return call_gemini(prompt, ["Expected Match Winner", "Session & Chase Basis"])


def to_html(t):
    lines = []
    for raw_line in t.splitlines():
        stripped = raw_line.strip()
        is_quote = stripped.startswith(">")
        clean = stripped.lstrip(">").strip() if is_quote else raw_line
        line = esc(clean)
        line = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", line)
        line = line.replace("`", "").replace("**", "")
        lines.append(f"<blockquote>{line}</blockquote>" if is_quote else line)
    return "\n".join(lines)


def label(m):
    t, s = m.get("type", ""), m.get("series", "")
    return f"{t} • {s}" if s else t


def dispatch_card(m, start, stage_key, state, events, live_obj=None, info=None, mark_sent=True):
    saved = state["odds"].get(m["id"], {})
    probs = probs_for(m, events or []) or saved
    if probs:
        state["odds"][m["id"]] = probs
        state["open"].setdefault(m["id"], probs)
    odds_txt, flow_txt = analyze_market_flow(state["open"].get(m["id"], {}), probs)
    if info is None:
        if stage_key in ("prematch", "preview") or not m.get("venue"):
            info = match_details(m["id"], live_obj)
            if info["venue"] and not m.get("venue"):
                m["venue"] = info["venue"]
        else:
            info = {"toss": "Pending (Toss ~30m pre-match)", "venue": m.get("venue", ""), "status": "Scheduled", "score": [], "score_txt": "Not started", "xi": "Pending"}
    wx = weather_info(m["venue"], start)
    analysis = generate_report(m, start, stage_key, wx, info, odds_txt, flow_txt, research_baseline(m, start, wx))
    vs = " vs ".join(m["teams"]) or m["name"]
    if stage_key == "toss":
        head = f"🪙 <b>PRE-TOSS ANALYTICS (<code>50–55m Pre-Match</code>)</b>\n🏏 <b>{esc(label(m))}</b> | 🔥 <b>{esc(vs)}</b>\n🏟️ {esc(m['venue'])} | ⏰ <code>{start.astimezone(BDT):%I:%M %p} BDT</code>\n\n"
    else:
        badge = {"prematch": "15m Post-Toss Final", "6ov": "Live 6-Over PP Pulse", "10ov": "Live 10-Over Pulse", "break": "1st Innings Break • Golden Window", "preview": "Live / Pre-Match Snapshot"}.get(stage_key, stage_key)
        head = f"🏏 <b>{esc(label(m))}</b> | 🔥 <b>{esc(vs)}</b>\n🏟️ {esc(m['venue'])} | ⏰ <code>{badge}</code>\n\n"
    body = to_html(analysis[:3500]) if analysis else f"1. <b>Toss:</b> <code>{esc(info['toss'])}</code>\n2. <b>Market:</b> <code>{esc(odds_txt)}</code>\n3. <b>Flow:</b> <code>{esc(flow_txt)}</code>\n4. <b>Conditions:</b> <code>{esc(wx)}</code>"
    ok = tg(head + body)
    if ok and mark_sent:
        state["sent"].setdefault(m["id"], []).append(stage_key)
    return ok


def main():
    state = load_state()
    for k in ("sent", "odds", "open"):
        state.setdefault(k, {})
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    matches = fetch_matches(state)
    live_map = fetch_live_map(matches, state, force=is_manual)
    events = fetch_odds()
    if is_manual:
        tg(f"✅ <b>Final Pro Engine (v10.2 Clean) Active</b>\n📡 <code>CricAPI Hits: {API_DIAG['hits']}/{API_DIAG['limit']} | Cached Matches: {len(matches)}</code>")

    today = NOW.astimezone(BDT).date().isoformat()
    if state.get("schedule_date") != today:
        todays = sorted([m for m in matches if parse(m["start"]).astimezone(BDT).date().isoformat() == today], key=lambda m: m["start"])
        head = f"📅 <b>TODAY'S MAJOR CRICKET SCHEDULE (<code>{NOW.astimezone(BDT):%d %b %Y}</code>)</b> | 📊 <b>Total:</b> <code>{len(todays)}</code>\n\n"
        lines = [f"{i}. 🏏 <b>{esc(label(m))}</b> | 🔥 <b>{esc(' vs '.join(m['teams']) or m['name'])}</b> | ⏰ <code>{parse(m['start']).astimezone(BDT):%I:%M %p} BDT</code> | 🏟️ {esc(m['venue'])}" for i, m in enumerate(todays, 1)]
        if tg(head + ("\n".join(lines) if lines else "No major matches scheduled today.")):
            state["schedule_date"] = today

    cards_sent = 0
    for m in matches:
        start = parse(m["start"])
        mins = (start - NOW).total_seconds() / 60
        sent = state["sent"].setdefault(m["id"], [])
        live_obj = live_map.get(m["id"])
        stage_key, info = None, None
        if 46 < mins <= 62:
            stage_key = "toss"
        elif 4 < mins <= 25:
            stage_key = "prematch"
        elif mins <= 4 and live_obj:
            info = match_details(m["id"], live_obj)
            stage_key = detect_inplay_stage(m.get("type", ""), info)
        if stage_key and stage_key not in sent:
            if dispatch_card(m, start, stage_key, state, events, live_obj=live_obj, info=info, mark_sent=True):
                cards_sent += 1

    if is_manual and cards_sent == 0 and matches:
        target = next((m for m in matches if m["id"] in live_map), matches[0])
        dispatch_card(target, parse(target["start"]), "preview", state, events, live_obj=live_map.get(target["id"]), mark_sent=False)

    keep = {m["id"] for m in matches}
    for k in ("sent", "odds", "open"):
        state[k] = {i: v for i, v in state[k].items() if i in keep}
    save_state(state)


if __name__ == "__main__":
    main()
