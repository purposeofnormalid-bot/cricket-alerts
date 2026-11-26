import os, json, re, html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import requests

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
CRIC = os.environ["CRICAPI_KEY"]
ODDS = os.environ["ODDS_API_KEY"]
GEM = os.environ["GEMINI_API_KEY"]
PRIMARY_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
FALLBACK_MODEL = "gemini-3.5-flash-lite"

INCLUDE_WOMEN = True
STATE_FILE = "state.json"
STATE_VERSION = 13
BDT = timezone(timedelta(hours=6))
NOW = datetime.now(timezone.utc)

FULL = {
    "india", "australia", "england", "pakistan", "south africa",
    "new zealand", "sri lanka", "bangladesh", "west indies",
    "afghanistan", "zimbabwe", "ireland"
}
LEAGUE_RE = re.compile(
    r"\b(ipl|indian premier league|big bash|bbl|wbbl|womens big bash|women's big bash|"
    r"bpl|bangladesh premier|psl|pakistan super league|sa20|ilt20|"
    r"international league t20|cpl|caribbean premier|the hundred|"
    r"major league cricket|mlc|wpl|womens premier league|women's premier league|"
    r"icc|acc|asia cup|world cup|champions trophy)\b"
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
    "pretoria capitals", "mi cape town", "sunrisers eastern cape",
    "up warriorz", "gujarat giants"
]
BAD_ALWAYS = ("u19", "under-19", "under 19", "emerging", "development", "academy", "softball", "rising stars")
WOMEN = ("women", "wbbl", "wpl", "-w ", " w vs", "(w)")
SERIES_KEYWORDS = [
    "ILT20", "International League T20", "Womens Big Bash", "Big Bash",
    "Indian Premier League", "Womens Premier League", "Bangladesh Premier",
    "Pakistan Super League", "SA20", "Caribbean Premier", "The Hundred", "Major League Cricket"
]
YEARS = ("2026", "2027")
esc = html.escape
API_DIAG = {"hits": "?", "limit": "?", "reason": ""}


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        if s.get("v") == STATE_VERSION:
            return s
        return {
            "v": STATE_VERSION,
            "sent": s.get("sent", {}),
            "odds": s.get("odds", {}),
            "open": s.get("open", {}),
            "pretoss_odds": s.get("pretoss_odds", {}),
            "toss_seen": s.get("toss_seen", {}),
            "snap": s.get("snap", {}),
            "mom_meta": s.get("mom_meta", {}),
            "series_ids": s.get("series_ids", []),
            "series_ts": s.get("series_ts"),
            "matches": s.get("matches", []),
            "matches_ts": s.get("matches_ts"),
            "schedule_date": s.get("schedule_date")
        }
    except Exception:
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


def base_country(t):
    t = t.lower().strip()
    if INCLUDE_WOMEN:
        t = re.sub(r"(\s+women|\s*-w|\s*\(w\))$", "", t).strip()
    return t


def is_major(m):
    s = f"{m.get('name', '')} {m.get('series', '')}".lower()
    teams = [base_country(t) for t in (m.get("teams") or [])]
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
    if ts and NOW - parse(ts) < timedelta(days=3) and state.get("series_ids"):
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
    ids = [[i, n] for i, n in found.items()][:10]
    if ids:
        state["series_ts"] = NOW.isoformat()
        state["series_ids"] = ids
    return state.get("series_ids", [])


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


def fetch_matches(state, force=False):
    ts = state.get("matches_ts")
    if not force and ts and NOW - parse(ts) < timedelta(hours=4) and state.get("matches"):
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
    if out:
        state["matches_ts"] = NOW.isoformat()
        state["matches"] = out
    return state.get("matches", [])


def fetch_espn_rss():
    items = []
    try:
        r = requests.get(
            "http://static.cricinfo.com/rss/livescores.xml",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12
        )
        if r.ok:
            root = ET.fromstring(r.content)
            for it in root.findall(".//item"):
                t = (it.findtext("title") or "").strip()
                if t:
                    items.append(t)
    except Exception:
        pass
    return items


def team_match(t1, t2):
    a, b = t1.lower().strip(), t2.lower().strip()
    if a in b or b in a:
        return True
    wa = set(a.split()) - {"cricket", "club", "team", "women", "men"}
    wb = set(b.split()) - {"cricket", "club", "team", "women", "men"}
    return bool(wa and wb and (wa & wb))


def parse_espn_score(m, rss_items):
    teams = m.get("teams") or []
    if len(teams) < 2:
        return None
    for title in rss_items:
        parts = re.split(r"\s+v\s+", title, flags=re.I)
        if len(parts) != 2:
            continue
        p1, p2 = parts[0], parts[1]
        if (team_match(teams[0], p1) and team_match(teams[1], p2)) or (
            team_match(teams[0], p2) and team_match(teams[1], p1)
        ):
            active_side = p1 if "*" in p1 else (p2 if "*" in p2 else title)
            nums = re.findall(r"(\d+)(?:/(\d+))?", active_side)
            r_val, w_val = (int(nums[-1][0]), int(nums[-1][1] or 0)) if nums else (0, 0)
            inn_cnt = sum(1 for side in (p1, p2) if re.search(r"\d+", side))
            return {"raw": title, "r": r_val, "w": w_val, "inn": inn_cnt}
    return None


def fetch_live_map(matches, state, force=False):
    if not any(-320 <= (parse(m["start"]) - NOW).total_seconds() / 60 <= 35 for m in matches):
        return {}
    ts = state.get("live_ts")
    if not force and ts and NOW - parse(ts) < timedelta(minutes=8):
        return state.get("live_cache", {})
    data = api("currentMatches", offset=0).get("data")
    if data is not None:
        live_map = {x["id"]: x for x in data}
        state["live_ts"] = NOW.isoformat()
        state["live_cache"] = live_map
    return state.get("live_cache", {})


def format_score_txt(score, espn_obj=None):
    if score:
        return " | ".join(
            f"{s.get('inning', 'Inning')}: {s.get('r', 0)}/{s.get('w', 0)} ({s.get('o', 0)} ov)"
            for s in score
        )
    if espn_obj:
        return f"ESPN Live: {espn_obj['raw']}"
    return "Not started"


def match_details(mid, live_obj=None, espn_obj=None, fetch_api=True):
    toss = "Pending (Toss ~30m pre-match)"
    venue = ""
    status = (live_obj or {}).get("status", "")
    score = (live_obj or {}).get("score") or []
    started = bool((live_obj or {}).get("matchStarted"))
    ended = bool((live_obj or {}).get("matchEnded"))
    xi_txt = "Pending"
    if fetch_api:
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
    return {
        "toss": toss, "venue": venue, "status": status, "score": score,
        "score_txt": format_score_txt(score, espn_obj), "started": started,
        "ended": ended, "xi": xi_txt
    }


def detect_inplay_stage(m_type, live_obj):
    if not live_obj or live_obj.get("matchEnded") or not live_obj.get("matchStarted"):
        return None
    score = live_obj.get("score") or []
    st = (live_obj.get("status") or "").lower()
    if not score:
        return None
    if len(score) == 1:
        try:
            ov = float(score[0].get("o", 0))
        except Exception:
            ov = 0.0
        if "break" in st or (m_type in ("T20", "T20I") and ov >= 18.0) or (m_type == "ODI" and ov >= 47.0):
            return "break"
        if m_type in ("T20", "T20I", "ODI") and 9.1 <= ov <= 14.5:
            return "10ov"
        if m_type in ("T20", "T20I", "ODI") and 4.5 <= ov <= 9.0:
            return "6ov"
    if len(score) >= 2:
        try:
            if float(score[1].get("o", 99)) <= 3.5:
                return "break"
        except Exception:
            pass
    return None


def detect_momentum_event(mid, live_obj, espn_obj, state):
    cur = None
    if live_obj and live_obj.get("score") and not live_obj.get("matchEnded"):
        sc = live_obj["score"][-1]
        cur = {
            "r": int(sc.get("r", 0)), "w": int(sc.get("w", 0)),
            "inn": len(live_obj["score"]), "st": (live_obj.get("status") or "").lower(),
            "ts": NOW.isoformat()
        }
    elif espn_obj:
        cur = {
            "r": espn_obj["r"], "w": espn_obj["w"], "inn": espn_obj["inn"],
            "st": "", "ts": NOW.isoformat()
        }
    if not cur:
        return None

    prev = state["snap"].get(mid)
    state["snap"][mid] = cur
    if not prev:
        return None

    meta = state["mom_meta"].get(mid, {"cnt": 0, "ts": ""})
    if meta["cnt"] >= 3:
        return None
    if meta["ts"] and NOW - parse(meta["ts"]) < timedelta(minutes=15):
        return None

    elapsed = (NOW - parse(prev["ts"])).total_seconds() / 60
    if cur["inn"] == prev["inn"] and elapsed <= 14:
        w_diff = cur["w"] - prev["w"]
        r_diff = cur["r"] - prev["r"]
        if w_diff >= 2:
            return f"SUDDEN COLLAPSE (+{w_diff} Wickets in ~{int(max(elapsed, 1))}m)"
        if r_diff >= 28:
            return f"SUDDEN RUN SURGE (+{r_diff} Runs in ~{int(max(elapsed, 1))}m)"

    if any(k in cur["st"] for k in ("rain", "delay", "interrupt", "wet")) and not any(
        k in prev.get("st", "") for k in ("rain", "delay", "interrupt", "wet")
    ):
        return "WEATHER / PLAY INTERRUPTION"
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
        target_hr = start.strftime("%Y-%m-%dT%H:00")
        i = w["time"].index(target_hr) if target_hr in w["time"] else 0
        is_night = start.astimezone(BDT).hour >= 17
        dew_tag = "High" if (is_night and (w["relative_humidity_2m"][i] >= 68 or w["dew_point_2m"][i] >= 17)) else "Low"
        return f"Rain {w['precipitation_probability'][i]}% | Cloud {w['cloud_cover'][i]}% | Dew {w['dew_point_2m'][i]}°C ({dew_tag})"
    except Exception:
        return "Clear/Dry Forecast (Open-Meteo N/A)"


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


def analyze_market_flow(opening, probs, pretoss=None):
    if not probs:
        return "Odds Pending (Not Listed Yet)", "Pending Market Open", "N/A (Pre-Toss / Odds Pending)"
    base = opening or probs
    parts, max_shift, flow_team = [], 0.0, ""
    for t, v in probs.items():
        diff = round(v["p"] - base.get(t, v)["p"], 1)
        parts.append(f"{t} {v['o']} ({v['p']}%)")
        if abs(diff) > abs(max_shift):
            max_shift, flow_team = diff, t
    odds_txt = " vs ".join(parts) + f" | Shift: {max_shift:+.1f}%"
    if max_shift >= 3.5:
        flow_txt = f"Heavy Sharp/Public Flow on {flow_team} ({max_shift:+.1f}% Shift)"
    elif max_shift >= 1.5:
        flow_txt = f"Mild Flow on {flow_team} ({max_shift:+.1f}% Shift)"
    elif max_shift <= -1.5:
        flow_txt = f"Market Drift Against {flow_team} ({max_shift:+.1f}% Shift)"
    else:
        flow_txt = "Balanced Market (No One-Sided Flow)"

    over_txt = "Monitoring Post-Toss Shift"
    if pretoss:
        pt_shift, pt_team = 0.0, ""
        for t, v in probs.items():
            if t in pretoss:
                d = round(v["p"] - pretoss[t]["p"], 1)
                if abs(d) > abs(pt_shift):
                    pt_shift, pt_team = d, t
        if abs(pt_shift) >= 4.0:
            over_txt = f"POSSIBLE PUBLIC OVERREACTION on {pt_team} ({pt_shift:+.1f}% Post-Toss Jump)"
        elif abs(pt_shift) >= 1.5:
            over_txt = f"Measured Post-Toss Adjustment on {pt_team} ({pt_shift:+.1f}%)"
        else:
            over_txt = "Calm Post-Toss Pricing (No Overreaction)"
    return odds_txt, flow_txt, over_txt


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


def generate_report(m, start, stage_key, wx, info, odds_txt, flow_txt, over_txt, base_rules, event_reason=""):
    vs = " vs ".join(m["teams"]) or m["name"]
    t1 = m["teams"][0] if len(m["teams"]) >= 1 else "Team 1"
    t2 = m["teams"][1] if len(m["teams"]) >= 2 else "Team 2"
    toss_impact, home_bias, expected_call = base_rules

    stage_labels = {
        "toss": "Pre-Toss Full Pulse (~50m Pre-Match)",
        "prematch": "Post-Toss Confirmed XI & Full Session Card",
        "6ov": "Live 6-Over Powerplay Pulse",
        "10ov": "Live 10-Over Mid-Session Pulse",
        "break": "1st Innings Break • Golden Chase Window",
        "momentum": f"LIVE MOMENTUM ALERT • {event_reason}",
        "preview": "Live / Pre-Match Full Snapshot"
    }
    stage_title = stage_labels.get(stage_key, stage_key)
    is_break = stage_key == "break"
    inn_line = (
        "• **1st Innings Target:** Actual Target XXX | **Chase:** XX% | **Defend:** YY%"
        if is_break else
        "• **1st Innings Par (Line: XXX.5):** XXX – YYY | **Over:** XX% | **Under:** YY%"
    )
    toss_block = (
        f"1. **Captains' Last-5 Toss Trend:** {t1} (Real Captain Name — X/5 wins, Bat/Bowl pref) vs {t2} (Real Captain Name — Y/5 wins, Bat/Bowl pref) — note Heads/Tails call trend only if publicly reported, else state coin flip is 50/50\n"
        f"2. **Toss Impact & Expected Decision:** {toss_impact} | Expected Call: {expected_call} (XX% Probability)\n"
        f"3. **Public vs. Traders' View:** {flow_txt} | Live public sentiment vs. sharp trader lean\n"
        f"4. **Toss & Pitch Explanation:** (1-2 crisp sentences on pitch, dew, venue % and why this toss call matters)"
        if stage_key == "toss" else
        f"1. **Confirmed Toss & XI Impact:** {info['toss']} | Key XI / batting-first impact\n"
        f"2. **Post-Toss Overreaction Check:** {over_txt} — state if public overreacted to the toss\n"
        f"3. **Public vs. Traders' Sentiment:** {flow_txt} | Sharp vs. public lean\n"
        f"4. **Condition & Momentum Basis:** (1-2 crisp sentences on {wx}, pitch behavior, and current match state)"
    )

    prompt = f"""You are a quantitative cricket trading analyst. Write a complete, sharp 3-block analytical report for **{stage_title}** in clean English.

DATA:
- Match: {vs} ({m.get('type', '')})
- Venue: {m['venue']}
- Live Score / Status: {info['score_txt']} ({info['status']})
- Live Event Trigger: {event_reason or 'Scheduled Stage'}
- Toss Info: {info['toss']}
- Playing XI: {info['xi']}
- Weather & Dew: {wx}
- Toss Baseline: {toss_impact} | Expected Call: {expected_call}
- Home Bias Rule: {home_bias}
- Market Odds: {odds_txt}
- Market Flow: {flow_txt}
- Post-Toss Overreaction: {over_txt}

STRICT RULES:
- 100% ENGLISH ONLY. NEVER write "Captain A" or "Captain B" — ALWAYS use Google Search to find real team & captain names for {t1} and {t2} (or write "{t1} Capt." if unannounced).
- Use Google Search to check recent last-5 toss win/decision trends for both captains/teams and venue stats for {m['venue']}.
- No square brackets `[...]` or unclosed quotes in output.
- Ensure Over% + Under% = 100%, Chasing Win% + Defending Win% = 100%, and {t1}% + {t2}% = 100%.
- Use `CLEAR VALUE EDGE` only if Model Fair Win % differs from Market Implied % by >4.5%; else `SLIGHT EDGE` or `NO CLEAR EDGE — SKIP`.
- Follow this EXACT 3-block structure:

━━━ 🪙 **TOSS, SENTIMENT & CONDITIONS** ━━━
{toss_block}

━━━ 📈 **MATCH PROBABILITY & EDGE** ━━━
• **Market Odds (H2H):** {odds_txt}
• **Model vs. Market:** Model Fair Win: {t1} XX.X% vs {t2} YY.Y% | Gap: +/-X.X%
• **Match Edge Explanation:** (1-2 crisp sentences explaining the edge from XI depth, toss/pitch, home bias, and live score/RRR)

> 🎯 **Expected Match Winner:** Team Name (XX.X%) — CLEAR VALUE EDGE / SLIGHT EDGE / NO CLEAR EDGE — SKIP

━━━ ⚡ **SESSION & CHASE FAVORITES** ━━━
• **6-Over PP (Line: XX.5):** XX – YY | **Over:** XX% | **Under:** YY%
• **10-Over Session (Line: XX.5):** XX – YY | **Over:** XX% | **Under:** YY%
{inn_line}
• **Match Outcome Split:** **Chasing Win:** XX% | **Defending Win:** YY%
• **Session & Chase Basis:** (1-2 crisp sentences explaining the exact basis for these session lines, Over/Under %, and Chase/Defend %)

> 🏆 **Session & Chase Favorite:** Best Session Call (e.g., 6-Ov Over/Under XX.5 at YY%) | Match: Chase/Defend (ZZ%)"""

    return call_gemini(prompt, ["Expected Match Winner", "Session & Chase Favorite"])


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


def dispatch_card(m, start, stage_key, state, events, live_obj=None, espn_obj=None, info=None, mark_sent=True, event_reason=""):
    saved = state["odds"].get(m["id"], {})
    probs = probs_for(m, events or []) or saved
    if probs:
        state["odds"][m["id"]] = probs
        state["open"].setdefault(m["id"], probs)
        if stage_key == "toss":
            state["pretoss_odds"][m["id"]] = probs
    pretoss = state["pretoss_odds"].get(m["id"]) if stage_key != "toss" else None
    odds_txt, flow_txt, over_txt = analyze_market_flow(state["open"].get(m["id"], {}), probs, pretoss)

    if info is None:
        need_api = stage_key in ("prematch", "preview", "6ov", "10ov", "break") or not m.get("venue")
        info = match_details(m["id"], live_obj, espn_obj, fetch_api=need_api)
        if info["venue"] and not m.get("venue"):
            m["venue"] = info["venue"]

    wx = weather_info(m["venue"], start)
    analysis = generate_report(m, start, stage_key, wx, info, odds_txt, flow_txt, over_txt, research_baseline(m, start, wx), event_reason)
    vs = " vs ".join(m["teams"]) or m["name"]
    badge = {
        "toss": "Pre-Toss Full Pulse",
        "prematch": "Post-Toss + Full Session",
        "6ov": "Live 6-Over PP Pulse",
        "10ov": "Live 10-Over Pulse",
        "break": "1st Innings Break • Golden Window",
        "momentum": f"⚡ {event_reason}",
        "preview": "Live / Pre-Match Snapshot"
    }.get(stage_key, stage_key)
    head = (
        f"🏏 <b>{esc(label(m))}</b> | 🔥 <b>{esc(vs)}</b>\n"
        f"🏟️ {esc(m['venue'])} | ⏰ <code>{badge}</code> (<code>{start.astimezone(BDT):%I:%M %p} BDT</code>)\n\n"
    )
    body = to_html(analysis[:3500]) if analysis else f"1. <b>Toss:</b> <code>{esc(info['toss'])}</code>\n2. <b>Score:</b> <code>{esc(info['score_txt'])}</code>\n3. <b>Market:</b> <code>{esc(odds_txt)}</code>"
    ok = tg(head + body)
    if ok and mark_sent and stage_key != "momentum":
        state["sent"].setdefault(m["id"], []).append(stage_key)
    if ok and stage_key == "momentum":
        meta = state["mom_meta"].get(m["id"], {"cnt": 0, "ts": ""})
        state["mom_meta"][m["id"]] = {"cnt": meta["cnt"] + 1, "ts": NOW.isoformat()}
    return ok


def main():
    state = load_state()
    for k in ("sent", "odds", "open", "pretoss_odds", "toss_seen", "snap", "mom_meta"):
        state.setdefault(k, {})

    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    matches = fetch_matches(state, force=is_manual)
    live_map = fetch_live_map(matches, state, force=is_manual)
    has_active = any(-320 <= (parse(m["start"]) - NOW).total_seconds() / 60 <= 15 for m in matches)
    rss_items = fetch_espn_rss() if (has_active or is_manual) else []
    events = fetch_odds()

    if is_manual:
        tg(
            f"✅ <b>Hybrid Pro Engine (v13) Active</b>\n"
            f"📡 <code>CricAPI Hits: {API_DIAG['hits']}/{API_DIAG['limit']} | Matches: {len(matches)} | ESPN RSS: {len(rss_items)}</code>"
        )

    bdt_now = NOW.astimezone(BDT)
    today = bdt_now.date().isoformat()
    todays = sorted([m for m in matches if parse(m["start"]).astimezone(BDT).date().isoformat() == today], key=lambda m: m["start"])
    if state.get("schedule_date") != today:
        if todays or (bdt_now.hour >= 6 and not API_DIAG["reason"]):
            head = f"📅 <b>TODAY'S MAJOR CRICKET SCHEDULE (<code>{bdt_now:%d %b %Y}</code>)</b> | 📊 <b>Total:</b> <code>{len(todays)}</code>\n\n"
            lines = [f"{i}. 🏏 <b>{esc(label(m))}</b> | 🔥 <b>{esc(' vs '.join(m['teams']) or m['name'])}</b> | ⏰ <code>{parse(m['start']).astimezone(BDT):%I:%M %p} BDT</code> | 🏟️ {esc(m['venue'])}" for i, m in enumerate(todays, 1)]
            if tg(head + ("\n".join(lines) if lines else "No major matches scheduled today.")):
                state["schedule_date"] = today

    cards_sent = 0
    for m in matches:
        mid = m["id"]
        start = parse(m["start"])
        mins = (start - NOW).total_seconds() / 60
        sent = state["sent"].setdefault(mid, [])
        live_obj = live_map.get(mid)
        espn_obj = parse_espn_score(m, rss_items) if rss_items else None
        stage_key, info, ev_reason = None, None, ""

        # 1. Pre-Toss (~50m pre-match, e.g. 7:40 PM for 8:30 PM match)
        if 40 < mins <= 65 and "toss" not in sent:
            stage_key = "toss"

        # 2. Event-Driven Post-Toss + Full Session (~8:08-8:15 PM for 8:30 PM match; fallback at 12m)
        elif -5 <= mins <= 34 and "prematch" not in sent:
            info = match_details(mid, live_obj, espn_obj, fetch_api=True)
            toss_done = "won toss" in info["toss"].lower()
            if toss_done:
                seen_ts = state["toss_seen"].get(mid)
                if not seen_ts:
                    state["toss_seen"][mid] = NOW.isoformat()
                    seen_ts = NOW.isoformat()
                waited = (NOW - parse(seen_ts)).total_seconds() / 60
                if mins <= 22 or waited >= 5:
                    stage_key = "prematch"
            elif mins <= 12:
                stage_key = "prematch"

        # 3. Live In-Play Stages (6ov / 10ov / Break) + Sudden Momentum (ESPN RSS + Live Score)
        elif mins <= 2:
            stage_key = detect_inplay_stage(m.get("type", ""), live_obj)
            if stage_key in sent:
                stage_key = None
            if not stage_key:
                ev_reason = detect_momentum_event(mid, live_obj, espn_obj, state)
                if ev_reason:
                    stage_key = "momentum"
                    info = match_details(mid, live_obj, espn_obj, fetch_api=False)
            else:
                detect_momentum_event(mid, live_obj, espn_obj, state)

        if stage_key:
            if dispatch_card(m, start, stage_key, state, events, live_obj=live_obj, espn_obj=espn_obj, info=info, mark_sent=True, event_reason=ev_reason):
                cards_sent += 1

    if is_manual and cards_sent == 0 and matches:
        target = next((m for m in matches if m["id"] in live_map), matches[0])
        dispatch_card(target, parse(target["start"]), "preview", state, events, live_obj=live_map.get(target["id"]), espn_obj=parse_espn_score(target, rss_items), mark_sent=False)

    keep = {m["id"] for m in matches}
    for k in ("sent", "odds", "open", "pretoss_odds", "toss_seen", "snap", "mom_meta"):
        state[k] = {i: v for i, v in state[k].items() if i in keep}
    save_state(state)


if __name__ == "__main__":
    main()