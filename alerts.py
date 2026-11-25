import os
import json
import re
import html
from datetime import datetime, timedelta, timezone
import requests

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
CRIC = os.environ["CRICAPI_KEY"]
ODDS = os.environ["ODDS_API_KEY"]
GEM = os.environ["GEMINI_API_KEY"]
PRIMARY_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
FALLBACK_MODEL = "gemini-3.5-flash-lite"

INCLUDE_WOMEN = False  # Women's ম্যাচ চাইলে True করুন

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
    "mi cape town", "sunrisers eastern cape"
]
BAD_ALWAYS = (
    "u19", "under-19", "under 19", "emerging", "development",
    "academy", "softball", "rising stars"
)
WOMEN = ("women", "wbbl", "wpl", "-w ", " w vs", "(w)")
SERIES_KEYWORDS = [
    "ILT20", "International League T20", "Indian Premier League",
    "Big Bash", "Bangladesh Premier", "Pakistan Super League",
    "SA20", "Caribbean Premier", "The Hundred", "Major League Cricket"
]
YEARS = ("2026", "2027")

esc = html.escape
API_DIAG = {"hits": "?", "limit": "?", "reason": ""}


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
        # আগের ক্যাশ ধরে রাখবে যাতে বাড়তি CricAPI ক্রেডিট খরচ না হয়
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
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=data, timeout=20
        )
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
        j = requests.get(
            f"https://api.cricapi.com/v1/{ep}",
            params={"apikey": CRIC, **params},
            timeout=25
        ).json()
    except Exception as e:
        print(ep, "error:", type(e).__name__)
        return {}
    info = j.get("info", {})
    if info:
        API_DIAG["hits"] = info.get("hitsToday", API_DIAG["hits"])
        API_DIAG["limit"] = info.get("hitsLimit", API_DIAG["limit"])
    print(
        ep, {k: v for k, v in params.items() if k != "apikey"}, "->",
        j.get("status"), "| hits:", info.get("hitsToday"), "/",
        info.get("hitsLimit")
    )
    if j.get("status") != "success":
        API_DIAG["reason"] = j.get("reason", "unknown")
        print("CricAPI problem:", API_DIAG["reason"])
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
    if not INCLUDE_WOMEN and any(
        t.endswith("-w") or t.endswith(" women") for t in teams
    ):
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
    out = []
    for x in data:
        if x.get("ms") == "result" or not x.get("dateTimeGMT"):
            continue
        teams = [clean_team(x.get("t1")), clean_team(x.get("t2"))]
        out.append({
            "id": x["id"],
            "name": f"{teams[0]} vs {teams[1]}, {x.get('matchType', '')}",
            "venue": "",
            "teams": teams,
            "type": (x.get("matchType") or "").upper(),
            "series": x.get("series") or "",
            "dt": x["dateTimeGMT"],
            "trusted": False
        })
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
            teams = m.get("teams") or [
                t.get("name", "") for t in m.get("teamInfo", [])
            ]
            out.append({
                "id": m["id"],
                "name": m.get("name", ""),
                "venue": m.get("venue", ""),
                "teams": teams,
                "type": (m.get("matchType") or "").upper(),
                "series": sname,
                "dt": m["dateTimeGMT"],
                "trusted": True
            })
    return out


def fetch_matches(state):
    ts = state.get("matches_ts")
    if ts and NOW - parse(ts) < timedelta(hours=6):
        return state.get("matches", [])

    cands = series_matches(state) + cricscore_matches()
    out, seen, keys = [], set(), set()
    for m in cands:
        if is_bad(m):
            continue
        start = parse_dt(m["dt"])
        if (not start or start - NOW > timedelta(hours=36)
                or NOW - start > timedelta(hours=9)):
            continue
        key = (
            tuple(sorted(t.lower() for t in m["teams"])),
            start.strftime("%Y-%m-%d")
        )
        if m["id"] in seen or key in keys:
            continue
        if not (m["trusted"] or is_major(m)):
            continue
        seen.add(m["id"])
        keys.add(key)
        out.append({
            "id": m["id"],
            "name": m["name"],
            "venue": m["venue"],
            "teams": m["teams"],
            "type": m["type"],
            "series": m["series"],
            "start": start.isoformat()
        })
    print(f"candidates: {len(cands)}, major found: {len(out)}")
    state["matches_ts"] = (NOW if out else NOW - timedelta(hours=3)).isoformat()
    state["matches"] = out
    return out


def fetch_live_map(matches, state, force=False):
    need_live = any(
        -360 <= (parse(m["start"]) - NOW).total_seconds() / 60 <= 65
        for m in matches
    )
    if not need_live:
        return {}
    ts = state.get("live_ts")
    if not force and ts and NOW - parse(ts) < timedelta(minutes=10):
        return state.get("live_cache", {})
    j = api("currentMatches", offset=0)
    live_map = {x["id"]: x for x in (j.get("data") or [])}
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
        j = api("match_info", id=mid)
        d = j.get("data") or {}
        venue = d.get("venue") or venue
        status = d.get("status") or status
        score = d.get("score") or score
        started = bool(d.get("matchStarted", started))
        ended = bool(d.get("matchEnded", ended))
        if d.get("tossWinner"):
            toss = f"{d['tossWinner']} won toss & elected to {d.get('tossChoice', '?')}"
        players = d.get("players") or []
        if players:
            xi_parts = []
            for team_blk in players[:2]:
                tname = team_blk.get("teamName", "")
                plist = [
                    p.get("name", "") for p in (team_blk.get("players") or [])[:11]
                ]
                if plist:
                    xi_parts.append(f"{tname}: {', '.join(plist)}")
            if xi_parts:
                xi_txt = " | ".join(xi_parts)
    except Exception:
        pass

    score_lines = []
    for s in score:
        score_lines.append(
            f"{s.get('inning', 'Inning')}: {s.get('r', 0)}/{s.get('w', 0)} "
            f"({s.get('o', 0)} ov)"
        )
    score_txt = " | ".join(score_lines) if score_lines else "Not started"

    return {
        "toss": toss,
        "venue": venue,
        "status": status,
        "score": score,
        "score_txt": score_txt,
        "started": started,
        "ended": ended,
        "xi": xi_txt
    }


def detect_inplay_stage(m_type, info):
    if info["ended"] or not info["started"]:
        return None
    score = info["score"]
    st = (info["status"] or "").lower()
    if not score:
        return None

    if len(score) == 1:
        try:
            ov = float(score[0].get("o", 0))
        except Exception:
            ov = 0.0
        if "innings break" in st or "break" in st:
            return "break"
        if m_type in ("T20", "T20I") and ov >= 18.5:
            return "break"
        if m_type in ("ODI",) and ov >= 48.5:
            return "break"
        if m_type in ("T20", "T20I", "ODI") and 9.0 <= ov <= 13.5:
            return "10ov"
        if m_type in ("T20", "T20I", "ODI") and 5.0 <= ov <= 8.5:
            return "6ov"

    if len(score) >= 2:
        try:
            ov2 = float(score[1].get("o", 0))
        except Exception:
            ov2 = 99.0
        if ov2 <= 2.5:
            return "break"

    return None


def weather_info(venue, start):
    try:
        parts = [p.strip() for p in (venue or "").split(",")]
        city = parts[-2] if len(parts) >= 3 else parts[-1]
        g = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1}, timeout=15
        ).json()
        p = g["results"][0]
        w = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": p["latitude"],
                "longitude": p["longitude"],
                "hourly": "precipitation_probability,cloud_cover,relative_humidity_2m,dew_point_2m",
                "forecast_days": 3,
                "timezone": "UTC"
            },
            timeout=15
        ).json()["hourly"]
        i = w["time"].index(start.strftime("%Y-%m-%dT%H:00"))
        rain = w["precipitation_probability"][i]
        cloud = w["cloud_cover"][i]
        dew = w["dew_point_2m"][i]
        hum = w["relative_humidity_2m"][i]
        is_night = start.astimezone(BDT).hour >= 17
        dew_tag = "High" if (is_night and (hum >= 68 or dew >= 17)) else "Low"
        return f"Rain {rain}% | Cloud {cloud}% | Dew {dew}°C ({dew_tag})"
    except Exception:
        return "Weather N/A"


def research_baseline(m, start, wx):
    fmt = (m.get("type") or "").upper()
    is_night = start.astimezone(BDT).hour >= 17
    high_dew = "(High)" in wx

    if "TEST" in fmt:
        toss_impact = "High Impact (+4.9% Edge; 47% vs 34% in 5 yrs)"
        home_bias = "Test Home Bias (~59% Win | +0.50 Log-Odds)"
    elif "ODI" in fmt:
        toss_impact = "Critical Dew Edge (+5.5% Night Chase)" if high_dew else "Low Impact (+2.2% Baseline)"
        home_bias = "ODI Home Bias (~59% Win | +0.50 Log-Odds)"
    else:
        toss_impact = "Critical Dew Edge (+6.5% Win Boost)" if high_dew else "Low-Moderate (+2.8% Baseline)"
        home_bias = "T20 Neutral Home (50% Baseline)"

    expected_call = "Bowl First" if (high_dew or is_night) else "Bat First"
    return toss_impact, home_bias, expected_call


def fetch_odds():
    events = []
    try:
        sports = requests.get(
            "https://api.the-odds-api.com/v4/sports/",
            params={"apiKey": ODDS}, timeout=20
        ).json()
        keys = [
            s["key"] for s in sports
            if s["key"].startswith("cricket") and s.get("active")
        ]
        print("odds sport keys:", keys)
        for k in keys:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{k}/odds/",
                params={
                    "apiKey": ODDS,
                    "regions": "uk,au,eu",
                    "markets": "h2h",
                    "oddsFormat": "decimal"
                },
                timeout=20
            )
            if r.ok:
                events += r.json()
    except Exception as e:
        print("Odds API error:", type(e).__name__)
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
        if (team_match(teams[0], ha) and team_match(teams[1], aw)) or (
            team_match(teams[0], aw) and team_match(teams[1], ha)
        ):
            acc_p, acc_o, cnt = {}, {}, {}
            for bm in ev.get("bookmakers", []):
                for mk in bm.get("markets", []):
                    if mk["key"] != "h2h":
                        continue
                    inv = {
                        o["name"]: 1 / o["price"]
                        for o in mk["outcomes"] if o.get("price")
                    }
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
                return {
                    n: {
                        "o": round(acc_o[n] / cnt[n], 2),
                        "p": round(acc_p[n] / cnt[n] * 100, 1)
                    }
                    for n in cnt
                }
    return {}


def analyze_market_flow(opening, probs):
    if not probs or not opening:
        return "Odds Pending (Not Listed Yet)", "Pending Market Open"
    parts = []
    max_shift = 0.0
    flow_team = ""
    for t, v in probs.items():
        if t in opening:
            op_p, cur_p = opening[t]["p"], v["p"]
            diff = round(cur_p - op_p, 1)
            parts.append(f"{t} {v['o']} ({cur_p}%)")
            if abs(diff) > abs(max_shift):
                max_shift = diff
                flow_team = t

    shift_str = f" | Shift: {max_shift:+.1f}%" if max_shift != 0 else " | Shift: 0.0%"
    odds_txt = " vs ".join(parts) + shift_str
    if max_shift >= 3.5:
        flow_txt = f"Heavy Flow on {flow_team} ({max_shift:+.1f}% Implied Shift)"
    elif max_shift >= 1.5:
        flow_txt = f"Mild Flow on {flow_team} ({max_shift:+.1f}% Shift)"
    elif max_shift <= -1.5:
        flow_txt = f"Drift Against {flow_team} ({max_shift:+.1f}% Shift)"
    else:
        flow_txt = "Balanced Market (No One-Sided Flow)"
    return odds_txt, flow_txt


def call_gemini(prompt):
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        for use_search in (True, False):
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 1200, "temperature": 0.25}
            }
            if use_search:
                payload["tools"] = [{"google_search": {}}]
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": GEM},
                    json=payload,
                    timeout=90
                )
                if r.ok:
                    cand = r.json()["candidates"][0]["content"]["parts"]
                    txt = "".join(p.get("text", "") for p in cand).strip()
                    if txt:
                        return txt
                else:
                    print(f"Gemini {model} (search={use_search}) error:", r.status_code, r.text[:200])
            except Exception as e:
                print(f"Gemini {model} exception:", type(e).__name__)
    return None


def generate_report(m, start, stage_key, wx, info, odds_txt, flow_txt, base_rules):
    vs = " vs ".join(m["teams"]) or m["name"]
    t1 = m["teams"][0] if len(m["teams"]) >= 1 else "Team A"
    t2 = m["teams"][1] if len(m["teams"]) >= 2 else "Team B"
    toss_impact, home_bias, expected_call = base_rules

    if stage_key == "toss":
        prompt = f"""You are a quantitative cricket analyst. Output ONLY crisp 1-line English key points.

DATA:
- Match: {vs} ({m.get('type', '')})
- Venue: {m['venue']}
- Weather: {wx}
- Toss Impact: {toss_impact}
- Expected Call: {expected_call}
- Market Odds: {odds_txt}
- Public Flow: {flow_txt}

STRICT RULES:
- 100% ENGLISH ONLY. Zero Bengali. Zero paragraphs. Zero square brackets `[...]` in output.
- Every single bullet/number MUST fit on ONE short line.
- Use Google Search if available to find real Captain toss % and Venue Bat/Bowl First % for {m['venue']}.
- Pick ONE team for Expected Toss Winner with a realistic pattern-based split (`52% vs 48%` to `56% vs 44%`) and a 4-6 word reason.
- Output EXACTLY these 6 lines:

1. **Toss Impact Rating:** `{toss_impact}`
2. **Captain & Venue Trend:** `[Captain A XX% vs Captain B YY% | Venue: ZZ% Bowl/Bat First]`
3. **Pitch & Dew Trigger:** `[Flat/Green/Dry/Dead Track] | {wx}`
4. **Public & Market Flow:** `{odds_txt} | {flow_txt}`
5. **Expected Toss Decision:** `{expected_call} ([XX]% Probability — [3-5 word reason])`

> 🎯 **Expected Toss Winner:** `[Winner Name] ([XX]% vs [YY]%)` — *[4-6 word venue/captaincy reason]*"""

    else:
        stage_labels = {
            "prematch": "15m Post-Toss Final",
            "6ov": "Live 6-Over PP Pulse",
            "10ov": "Live 10-Over Pulse",
            "break": "1st Innings Break • Golden Window",
            "preview": "Live / Pre-Match Snapshot"
        }
        stage_title = stage_labels.get(stage_key, stage_key)
        is_break = stage_key == "break"
        inn_line = (
            "• **1st Innings Target (`Actual`):** `Target [XXX]` | **Chase:** `[XX]%` | **Defend:** `[YY]%`"
            if is_break else
            "• **1st Innings Par (`Line: [XXX].5`):** `[XXX] – [YYY]` | **Over:** `[XX]%` | **Under:** `[YY]%`"
        )

        prompt = f"""You are a quantitative cricket trading analyst. Output ONLY crisp 1-line English key points for **{stage_title}**.

DATA:
- Match: {vs} ({m.get('type', '')})
- Venue: {m['venue']}
- Live Score: {info['score_txt']}
- Toss: {info['toss']}
- Playing XI: {info['xi']}
- Weather: {wx}
- Home Bias Rule: {home_bias}
- Market Odds: {odds_txt}
- Public Flow: {flow_txt}

STRICT RULES:
- 100% ENGLISH ONLY. Zero Bengali. Zero paragraphs. Zero square brackets `[...]` in output.
- Every single item MUST be ONE compact line.
- Ensure Over% + Under% = 100%, Chasing Win% + Defending Win% = 100%, and {t1}% + {t2}% = 100%.
- Use `CLEAR VALUE EDGE` only if Model Fair Win % differs from Market Implied % by >4.5%; otherwise use `SLIGHT EDGE` or `NO CLEAR EDGE — SKIP`.
- Output EXACTLY this structure:

━━━ 🪙 **MATCH KEY POINTS** ━━━
1. **Confirmed Toss:** `{info['toss']}`
2. **Market Odds (H2H):** `{odds_txt}`
3. **Public & Sharp Flow:** `{flow_txt}`
4. **Model vs. Market:** `Model Fair Win: {t1} [XX.X]% vs {t2} [YY.Y]% | Gap: [+/-X.X]%`
5. **Core Edge Drivers:** `{home_bias} + [XI/Pitch/Live RRR 4-6 words]`

> 🎯 **Expected Match Winner:** `[Team Name] ([XX.X]%)` — `[CLEAR VALUE EDGE / SLIGHT EDGE / NO CLEAR EDGE — SKIP]`

━━━ ⚡ **SESSION & CHASE PROBABILITIES** ━━━
• **6-Over PP (`Line: [XX].5`):** `[XX] – [YY]` | **Over:** `[XX]%` | **Under:** `[YY]%`
• **10-Over Session (`Line: [XX].5`):** `[XX] – [YY]` | **Over:** `[XX]%` | **Under:** `[YY]%`
{inn_line}
• **Match Outcome Split:** **Chasing Win:** `[XX]%` | **Defending Win:** `[YY]%`"""

    return call_gemini(prompt)


def to_html(t):
    lines = []
    for raw_line in t.splitlines():
        stripped = raw_line.strip()
        is_quote = stripped.startswith(">")
        clean = stripped.lstrip(">").strip() if is_quote else raw_line
        line = esc(clean)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", line)
        if is_quote:
            lines.append(f"<blockquote>{line}</blockquote>")
        else:
            lines.append(line)
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
    opening = state["open"].get(m["id"], {})
    odds_txt, flow_txt = analyze_market_flow(opening, probs)

    if info is None:
        if stage_key in ("prematch", "preview") or not m.get("venue"):
            info = match_details(m["id"], live_obj)
            if info["venue"] and not m.get("venue"):
                m["venue"] = info["venue"]
        else:
            info = {
                "toss": "Pending (Toss ~30m pre-match)",
                "venue": m.get("venue", ""),
                "status": "Scheduled",
                "score": [],
                "score_txt": "Not started",
                "xi": "Pending"
            }

    wx = weather_info(m["venue"], start)
    base_rules = research_baseline(m, start, wx)
    analysis = generate_report(m, start, stage_key, wx, info, odds_txt, flow_txt, base_rules)

    vs = " vs ".join(m["teams"]) or m["name"]
    if stage_key == "toss":
        head = (
            f"🪙 <b>PRE-TOSS ANALYTICS (<code>50–55m Pre-Match</code>)</b>\n"
            f"🏏 <b>{esc(label(m))}</b> | 🔥 <b>{esc(vs)}</b>\n"
            f"🏟️ {esc(m['venue'])} | ⏰ <code>{start.astimezone(BDT):%I:%M %p} BDT</code>\n\n"
        )
    else:
        badge_map = {
            "prematch": "15m Post-Toss Final",
            "6ov": "Live 6-Over PP Pulse",
            "10ov": "Live 10-Over Pulse",
            "break": "1st Innings Break • Golden Window",
            "preview": "Live / Pre-Match Snapshot"
        }
        head = (
            f"🏏 <b>{esc(label(m))}</b> | 🔥 <b>{esc(vs)}</b>\n"
            f"🏟️ {esc(m['venue'])} | ⏰ <code>{badge_map.get(stage_key, stage_key)}</code>\n\n"
        )

    if analysis:
        body = to_html(analysis[:3400])
    else:
        body = (
            f"1. <b>Toss:</b> <code>{esc(info['toss'])}</code>\n"
            f"2. <b>Market:</b> <code>{esc(odds_txt)}</code>\n"
            f"3. <b>Flow:</b> <code>{esc(flow_txt)}</code>\n"
            f"4. <b>Conditions:</b> <code>{esc(wx)}</code>"
        )

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
        diag = f"CricAPI Hits: {API_DIAG['hits']}/{API_DIAG['limit']} | Cached Matches: {len(matches)}"
        tg(f"✅ <b>Final Pro Engine (v10) Active</b>\n📡 <code>{esc(diag)}</code>")

    # Format A: Daily Major Schedule (after 12:01 AM BDT)
    today = NOW.astimezone(BDT).date().isoformat()
    if state.get("schedule_date") != today:
        todays = sorted(
            [
                m for m in matches
                if parse(m["start"]).astimezone(BDT).date().isoformat() == today
            ],
            key=lambda m: m["start"]
        )
        d = NOW.astimezone(BDT).strftime("%d %b %Y")
        head = (
            f"📅 <b>TODAY'S MAJOR CRICKET SCHEDULE (<code>{d}</code>)</b> | "
            f"📊 <b>Total:</b> <code>{len(todays)}</code>\n\n"
        )
        lines = []
        for i, m in enumerate(todays, 1):
            t = parse(m["start"]).astimezone(BDT).strftime("%I:%M %p")
            vs = " vs ".join(m["teams"]) or m["name"]
            lines.append(
                f"{i}. 🏏 <b>{esc(label(m))}</b> | 🔥 <b>{esc(vs)}</b> | "
                f"⏰ <code>{t} BDT</code> | 🏟️ {esc(m['venue'])}"
            )
        if tg(head + ("\n".join(lines) if lines else "No major matches scheduled today.")):
            state["schedule_date"] = today

    # Scheduled Triggers: Format B1 (50-55m Pre-Toss) & Format B2 (15m Post-Toss + In-Play)
    cards_sent = 0
    for m in matches:
        start = parse(m["start"])
        mins = (start - NOW).total_seconds() / 60
        sent = state["sent"].setdefault(m["id"], [])
        live_obj = live_map.get(m["id"])

        stage_key = None
        info = None

        if 46 < mins <= 62:
            stage_key = "toss"
        elif 4 < mins <= 25:
            stage_key = "prematch"
        elif mins <= 4 and live_obj:
            info = match_details(m["id"], live_obj)
            stage_key = detect_inplay_stage(m.get("type", ""), info)

        if not stage_key or stage_key in sent:
            continue

        if dispatch_card(m, start, stage_key, state, events, live_obj=live_obj, info=info, mark_sent=True):
            cards_sent += 1

    # ম্যানুয়ালি Run workflow চাপলে ১টি প্রিভিউ কার্ড পাঠাবে
    if is_manual and cards_sent == 0 and matches:
        target = next((m for m in matches if m["id"] in live_map), matches[0])
        dispatch_card(
            target,
            parse(target["start"]),
            "preview",
            state,
            events,
            live_obj=live_map.get(target["id"]),
            mark_sent=False
        )

    keep = {m["id"] for m in matches}
    for k in ("sent", "odds", "open"):
        state[k] = {i: v for i, v in state[k].items() if i in keep}
    save_state(state)


if __name__ == "__main__":
    main()
