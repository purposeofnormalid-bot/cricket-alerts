import os
import re
import json
import html
import math
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ==========================================
# 1. CONFIGURATION & API KEYS (v22)
# ==========================================
ENGINE_VERSION = "v22"
BDT = timezone(timedelta(hours=6))
STATE_FILE = "state.json"

CRICAPI_KEY = os.getenv("CRICAPI_KEY") or os.getenv("CRICKET_API_KEY", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_ENV_MODEL = os.getenv("GEMINI_MODEL", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID", "")

IS_MANUAL_RUN = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

ALLOWED_FORMATS = ("t20", "odi", "t20i")
WOMENS_KEYWORDS = ("women", "womens", "women's", "wbbl", "wbpl", "wpl", "fairbreak", "-w ", "(w)")
BAD_KEYWORDS = (
    "u19", "under-19", "under 19", "emerging", "development",
    "academy", "softball", "rising stars", "t10", "ecs ", "italia"
)
DELAY_KEYWORDS = (
    "delay", "rain", "wet", "damp", "inspection", "bad light",
    "interrupt", "postpon", "drizzle", "shower", "covers", "delayed start"
)

FULL_NATIONS = {
    "india", "australia", "england", "pakistan", "south africa",
    "new zealand", "sri lanka", "bangladesh", "west indies",
    "afghanistan", "zimbabwe", "ireland"
}
MAJOR_LEAGUE_RE = re.compile(
    r"\b(ipl|indian premier league|big bash|bbl|wbbl|women'?s big bash|womens big bash|"
    r"bpl|wbpl|bangladesh premier|psl|pakistan super league|sa20|ilt20|"
    r"international league t20|cpl|caribbean premier|the hundred|super smash|"
    r"major league cricket|mlc|wpl|women'?s premier league|womens premier league|"
    r"icc|acc|asia cup|world cup|champions trophy)\b"
)
MAJOR_FRANCHISES = (
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
)
CITY_WORDS = {
    "melbourne", "sydney", "adelaide", "perth", "brisbane", "hobart",
    "dubai", "sharjah", "abu", "dhabi", "mumbai", "delhi", "kolkata",
    "chennai", "punjab", "gujarat", "lucknow", "rajasthan", "lahore",
    "karachi", "islamabad", "peshawar", "multan", "quetta", "dhaka",
    "chattogram", "sylhet", "rangpur", "khulna", "barishal", "comilla",
    "cape", "town", "durban", "pretoria", "paarl", "joburg",
    "new", "south", "west", "sri", "northern", "central"
}
DLS_W = [1.00, 0.96, 0.91, 0.84, 0.75, 0.63, 0.49, 0.34, 0.20, 0.08, 0.0]

API_INFO = {"hits": None, "limit": 100, "error": ""}


# ==========================================
# 2. STATE MANAGEMENT & HARD CREDIT CAP
# ==========================================
def load_state():
    base = {
        "engine_version": ENGINE_VERSION,
        "schedule_date": "",
        "credits_used_today": 0,
        "api_hits": None,
        "api_limit": 100,
        "last_discovery_ts": "",
        "morning_sync_done": False,
        "odds_cache": [],
        "odds_cache_ts": "",
        "matches": [],
        "paper_record": {"wins": 0, "losses": 0, "net_units": 0.0}
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            if s.get("engine_version") != ENGINE_VERSION:
                base["paper_record"] = s.get("paper_record", base["paper_record"])
                base["api_hits"] = s.get("api_hits")
                base["api_limit"] = s.get("api_limit", 100)
                return base
            base.update(s)
        except Exception:
            pass
    return base


def save_state(state):
    state["engine_version"] = ENGINE_VERSION
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def can_poll_stage(m, stage, max_attempts, gap_min, now_bdt):
    """Hard Credit Cap: enforces max attempts & minimum gap between API calls per stage."""
    polls = m.setdefault("stage_polls", {})
    rec = polls.setdefault(stage, {"count": 0, "last": ""})
    if rec["count"] >= max_attempts:
        return False
    if rec["last"]:
        try:
            if (now_bdt - datetime.fromisoformat(rec["last"])) < timedelta(minutes=gap_min):
                return False
        except Exception:
            pass
    return True


def mark_stage_poll(m, stage, now_bdt):
    polls = m.setdefault("stage_polls", {})
    rec = polls.setdefault(stage, {"count": 0, "last": ""})
    rec["count"] = int(rec.get("count", 0)) + 1
    rec["last"] = now_bdt.isoformat()


# ==========================================
# 3. TELEGRAM HTML SENDER
# ==========================================
def esc(text):
    return html.escape(str(text)) if text is not None else ""


def send_telegram(message, use_html=True):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials missing.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message[:4000],
        "disable_web_page_preview": True
    }
    if use_html:
        payload["parse_mode"] = "HTML"
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            if use_html:
                plain = re.sub(r"<[^>]+>", "", html.unescape(message))
                return send_telegram(plain, use_html=False)
            print(f"Telegram Error {r.status_code}: {r.text}")
            return False
        return True
    except Exception as e:
        print(f"Telegram Exception: {e}")
        return False


def credit_label(state):
    hits = API_INFO["hits"] if API_INFO["hits"] is not None else state.get("api_hits")
    limit = API_INFO["limit"] or state.get("api_limit") or 100
    if hits is not None:
        return f"{hits}/{limit}"
    return f"{state.get('credits_used_today', 0)}/{limit}"


def health_badge(venue_ok=None, wx_ok=None, odds_ok=None, ai_ok=None, squad_ok=None):
    parts = []
    if venue_ok is not None:
        parts.append(f"Venue {'✅' if venue_ok else '❌'}")
    if wx_ok is not None:
        parts.append(f"Weather {'✅' if wx_ok else '❌'}")
    if squad_ok is not None:
        parts.append(f"Squad {'✅' if squad_ok else '❌'}")
    if odds_ok is not None:
        parts.append(f"Live Odds {'✅' if odds_ok else '❌'}")
    if ai_ok is not None:
        parts.append(f"AI Search {'✅' if ai_ok else '❌'}")
    return f"🔎 <i>Data Health: {' | '.join(parts)}</i>"


# ==========================================
# 4. MATH, TARGET-CAP & DLS ENGINE
# ==========================================
def ov_to_dec(o):
    try:
        s = str(o).strip()
        if "." in s:
            w, b = s.split(".", 1)
            return int(w) + min(int(b[0]), 6) / 6.0
        return float(s)
    except Exception:
        return 0.0


def devig_odds(odds_a, odds_b):
    if not odds_a or not odds_b or odds_a <= 1.0 or odds_b <= 1.0:
        return None, None, 0.0
    imp_a = 1.0 / odds_a
    imp_b = 1.0 / odds_b
    total_imp = imp_a + imp_b
    vig_pct = (total_imp - 1.0) * 100.0
    return round((imp_a / total_imp) * 100.0, 2), round((imp_b / total_imp) * 100.0, 2), round(vig_pct, 2)


def normal_cdf(x, mean, std):
    if std <= 0:
        return 0.5
    z = (x - mean) / (std * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def session_lean(proj, baseline_par, std, cap=None):
    if cap is not None:
        proj = min(proj, float(cap))
    low = max(1, int(round(proj - std * 0.25)))
    high = int(round(proj + std * 0.25))
    if cap is not None:
        high = min(high, int(cap))
        low = min(low, high)
    diff = proj - baseline_par
    under_p = normal_cdf(baseline_par, proj, std) * 100.0
    over_p = 100.0 - under_p
    if over_p >= 60.0 and diff >= 2.0:
        lean = f"<b>OVER</b> ({int(round(over_p))}%)"
    elif under_p >= 60.0 and diff <= -2.0:
        lean = f"<b>UNDER</b> ({int(round(under_p))}%)"
    else:
        lean = "<b>SKIP</b> (Close)"
    return low, high, lean


def build_direct_sessions(par_mean, par_std, eff_max_ov=20.0, is_odi=False, is_chasing=False, target=None, dew_high=False):
    std_ov = 50.0 if is_odi else 20.0
    ratio = min(max(eff_max_ov / std_ov, 0.25), 1.0)
    scaled_par = float(par_mean) * ratio
    adj_total = scaled_par

    if is_chasing and target is not None:
        adj_total = min(float(target), (scaled_par * 0.40) + (float(target) * 0.60) + (4.5 if dew_high else 0.0))

    if eff_max_ov <= 10.0:
        pp_ov = max(2, int(round(eff_max_ov * 0.30)))
        milestones = [(pp_ov, f"{pp_ov}-Over PP", pp_ov / eff_max_ov, 0.42)]
    elif is_odi:
        milestones = [
            (10, "10-Over PP", 0.205, 0.40),
            (25, "25-Over", 0.485, 0.62),
            (int(eff_max_ov), f"{int(eff_max_ov)}-Over Total", 1.0, 1.0)
        ]
    else:
        milestones = [
            (6, "6-Over PP", 0.292, 0.42),
            (10, "10-Over", 0.478, 0.60),
            (int(eff_max_ov), f"{int(eff_max_ov)}-Over Total", 1.0, 1.0)
        ]

    rows = []
    for ms_ov, lbl, frac, std_mul in milestones:
        if ms_ov > eff_max_ov:
            continue
        if is_chasing and target is not None and ms_ov >= eff_max_ov:
            continue
        proj = adj_total * frac
        base_line = scaled_par * frac
        std = max(4.5, par_std * ratio * std_mul)
        cap = int(target) if (is_chasing and target is not None) else None
        lo, hi, lean = session_lean(proj, base_line, std, cap=cap)
        rows.append({"label": lbl, "lo": lo, "hi": hi, "lean": lean})
    return rows


def detect_dls_and_target(m, live):
    is_odi = m["match_type"] == "ODI"
    std_ov = 50.0 if is_odi else 20.0
    scores = live.get("score") or []
    status = str(live.get("status") or "")
    st_low = status.lower()

    r1 = int(scores[0].get("r", 0)) if scores else 0
    w1 = int(scores[0].get("w", 0)) if scores else 0
    ov1 = ov_to_dec(scores[0].get("o", 0.0)) if scores else 0.0

    target = r1 + 1 if scores else None
    eff_max_ov = std_ov
    is_dls = any(k in st_low for k in ("dls", "d/l", "reduced", "rain", "shortened"))

    m_need_ov = re.search(r"need\s+(\d+)\s+runs?\s+(?:in|from|off)\s+(\d+(?:\.\d+)?)\s*ov", st_low)
    m_need_b = re.search(r"need\s+(\d+)\s+runs?\s+(?:in|from|off)\s+(\d+)\s*ball", st_low)
    m_red = re.search(r"reduced\s+to\s+(\d+(?:\.\d+)?)\s*ov", st_low)
    m_tgt = re.search(r"target\s*(?:is|of|:)?\s*(\d+)", st_low)

    if m_tgt:
        target = int(m_tgt.group(1))
    if m_red:
        eff_max_ov = float(m_red.group(1))
        is_dls = True
    if m_need_ov:
        need_runs = int(m_need_ov.group(1))
        rem_ov = float(m_need_ov.group(2))
        if len(scores) < 2 or ov_to_dec(scores[1].get("o", 0)) == 0:
            target = need_runs
            eff_max_ov = rem_ov
            if rem_ov < std_ov - 0.5:
                is_dls = True
    elif m_need_b and (len(scores) < 2 or ov_to_dec(scores[1].get("o", 0)) == 0):
        need_runs = int(m_need_b.group(1))
        rem_ov = round(int(m_need_b.group(2)) / 6.0, 1)
        target = need_runs
        eff_max_ov = rem_ov
        if rem_ov < std_ov - 0.5:
            is_dls = True

    inn1_done = (len(scores) >= 2) or ("break" in st_low) or ("need" in st_low)
    if inn1_done and w1 < 10 and 1.0 <= ov1 < (std_ov - 0.9):
        is_dls = True
        if eff_max_ov == std_ov:
            eff_max_ov = max(5.0, float(math.ceil(ov1)))

    return target, eff_max_ov, is_dls


# ==========================================
# 5. DERBY-SAFE TEAM & MATCH FILTERS
# ==========================================
def clean_team(t):
    return re.sub(r"\s*\[.*?\]", "", str(t or "")).strip()


def _team_tokens(t):
    s = clean_team(t).lower()
    s = re.sub(r"\b(inning\s*\d*|women|womens|women's|men|xi|cricket|club|team)\b", " ", s)
    s = re.sub(r"(\s*-w|\s*\(w\))", " ", s)
    toks = [w for w in re.findall(r"[a-z]+", s) if len(w) >= 2]
    return " ".join(toks), toks


def team_match(t1, t2):
    s1, w1 = _team_tokens(t1)
    s2, w2 = _team_tokens(t2)
    if not s1 or not s2:
        return False
    if s1 == s2:
        return True
    c1, c2 = set(w1) & CITY_WORDS, set(w2) & CITY_WORDS
    if c1 and c2 and not (c1 & c2):
        return False
    d1, d2 = set(w1) - CITY_WORDS, set(w2) - CITY_WORDS
    if d1 and d2:
        return bool(d1 & d2)
    return set(w1) == set(w2)


def same_matchup(a1, b1, a2, b2):
    return (team_match(a1, a2) and team_match(b1, b2)) or (team_match(a1, b2) and team_match(b1, a2))


def is_womens_match(name, series=""):
    blob = f"{name} {series}".lower() + " "
    return any(k in blob for k in WOMENS_KEYWORDS)


def is_major_match(name, series, team_a, team_b, m_type):
    blob = f"{name} {series} {team_a} {team_b}".lower()
    if any(b in blob for b in BAD_KEYWORDS):
        return False
    ta, tb = clean_team(team_a).lower(), clean_team(team_b).lower()
    if not ta or not tb or "tbc" in ta or "tbc" in tb or ta.endswith(" a") or tb.endswith(" a"):
        return False
    if m_type and m_type.lower() not in ALLOWED_FORMATS:
        return False
    if MAJOR_LEAGUE_RE.search(blob) or any(f in blob for f in MAJOR_FRANCHISES):
        return True
    base_a = re.sub(r"(\s+women|\s*-w|\s*\(w\))$", "", ta).strip()
    base_b = re.sub(r"(\s+women|\s*-w|\s*\(w\))$", "", tb).strip()
    return (base_a in FULL_NATIONS) and (base_b in FULL_NATIONS)


# ==========================================
# 6. WEATHER + STRICT DATE-LOCKED DATA SOURCES
# ==========================================
def fetch_weather(venue, start_dt_bdt):
    if not venue or venue.lower() in ("venue pending", "unknown venue"):
        return {"ok": False, "txt": "Weather N/A (Venue Pending)", "is_night": start_dt_bdt.hour >= 17, "high_dew": False, "rain_prob": 0}
    try:
        parts = [p.strip() for p in venue.split(",") if p.strip()]
        city_candidates = []
        if len(parts) >= 3:
            city_candidates.append(parts[-2])
        if len(parts) >= 2:
            city_candidates.append(parts[-1])
        city_candidates.append(parts[0])

        geo = None
        for city in city_candidates:
            r = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1}, timeout=10).json()
            if r.get("results"):
                geo = r["results"][0]
                break
        if not geo:
            raise ValueError("City not geocoded")

        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": geo["latitude"],
                "longitude": geo["longitude"],
                "hourly": "precipitation_probability,cloud_cover,relative_humidity_2m,dew_point_2m",
                "forecast_days": 3,
                "timezone": "auto"
            },
            timeout=12
        ).json()
        w = resp["hourly"]
        offset_sec = int(resp.get("utc_offset_seconds", 0))
        start_utc = start_dt_bdt.astimezone(timezone.utc)
        local_start = start_utc + timedelta(seconds=offset_sec)
        target_hr = local_start.strftime("%Y-%m-%dT%H:00")
        idx = w["time"].index(target_hr) if target_hr in w["time"] else 0

        is_night = local_start.hour >= 17 or local_start.hour <= 4
        rain = int(w["precipitation_probability"][idx] or 0)
        rh = int(w["relative_humidity_2m"][idx] or 0)
        dp = float(w["dew_point_2m"][idx] or 0.0)
        high_dew = is_night and (rh >= 68 or dp >= 16.5)
        dew_tag = "High" if high_dew else ("Moderate" if (is_night and dp >= 14.0) else "Low")
        time_tag = "Night" if is_night else "Day"
        txt = f"Local {local_start:%I:%M %p} ({time_tag}) | Rain {rain}% | Hum {rh}% | Dew {dp:.1f}°C ({dew_tag})"
        return {"ok": True, "txt": txt, "is_night": is_night, "high_dew": high_dew, "rain_prob": rain, "dew_tag": dew_tag}
    except Exception:
        return {"ok": False, "txt": "Weather N/A (Open-Meteo unreachable)", "is_night": start_dt_bdt.hour >= 17, "high_dew": False, "rain_prob": 0, "dew_tag": "Unknown"}


def cricapi_get(endpoint, state, **params):
    if not CRICAPI_KEY:
        API_INFO["error"] = "CRICAPI_KEY missing"
        return {}
    try:
        r = requests.get(
            f"https://api.cricapi.com/v1/{endpoint}",
            params={"apikey": CRICAPI_KEY, **params},
            timeout=20
        )
        state["credits_used_today"] = state.get("credits_used_today", 0) + 1
        data = r.json()
        info = data.get("info") or {}
        if info.get("hitsToday") is not None:
            API_INFO["hits"] = info["hitsToday"]
            state["api_hits"] = info["hitsToday"]
        if info.get("hitsLimit") is not None:
            API_INFO["limit"] = info["hitsLimit"]
            state["api_limit"] = info["hitsLimit"]
        if data.get("status") != "success":
            API_INFO["error"] = str(data.get("reason") or data.get("status") or "API failure")
            print(f"CricAPI {endpoint} failed: {API_INFO['error']}")
            return {}
        return data
    except Exception as e:
        API_INFO["error"] = str(e)
        print(f"CricAPI {endpoint} exception: {e}")
        return {}


def fetch_odds_events(state, force=False, sport_key=None):
    if not ODDS_API_KEY:
        return []
    if sport_key:
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
                params={"apiKey": ODDS_API_KEY, "regions": "uk,au", "markets": "h2h", "oddsFormat": "decimal"},
                timeout=12
            )
            if r.status_code == 200:
                fresh = r.json()
                for ev in fresh:
                    ev["_sport_key"] = sport_key
                return fresh
        except Exception as e:
            print(f"Single-league Odds Refresh Error ({sport_key}): {e}")
        return state.get("odds_cache", [])

    ts = state.get("odds_cache_ts")
    if not force and ts and state.get("odds_cache"):
        try:
            if datetime.now(BDT) - datetime.fromisoformat(ts) < timedelta(minutes=90):
                return state["odds_cache"]
        except Exception:
            pass

    events = []
    try:
        sp_r = requests.get("https://api.the-odds-api.com/v4/sports/", params={"apiKey": ODDS_API_KEY}, timeout=15)
        if sp_r.status_code != 200:
            return state.get("odds_cache", [])
        keys = [
            s["key"] for s in sp_r.json()
            if s.get("key", "").startswith("cricket") and s.get("active") and "test" not in s.get("key", "")
        ]
        for k in keys:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{k}/odds/",
                params={"apiKey": ODDS_API_KEY, "regions": "uk,au", "markets": "h2h", "oddsFormat": "decimal"},
                timeout=12
            )
            if r.status_code == 200:
                for ev in r.json():
                    ev["_sport_key"] = k
                    events.append(ev)
        if events:
            state["odds_cache"] = events
            state["odds_cache_ts"] = datetime.now(BDT).isoformat()
    except Exception as e:
        print(f"Odds API Error: {e}")
    return state.get("odds_cache", [])


def get_h2h_from_events(m, events):
    team_a, team_b = m["team_a"], m["team_b"]
    target_date = m.get("match_date_bdt")
    for ev in (events or []):
        if target_date:
            dt_bdt = parse_utc_to_bdt(ev.get("commence_time"))
            if dt_bdt and dt_bdt.strftime("%Y-%m-%d") != target_date:
                continue
        ha, aw = ev.get("home_team", ""), ev.get("away_team", "")
        if same_matchup(team_a, team_b, ha, aw):
            if ev.get("_sport_key"):
                m["odds_sport_key"] = ev["_sport_key"]
            oa_list, ob_list = [], []
            for bm in ev.get("bookmakers", []):
                for mk in bm.get("markets", []):
                    if mk.get("key") != "h2h":
                        continue
                    for out in mk.get("outcomes", []):
                        nm, pr = out.get("name", ""), out.get("price")
                        if not pr:
                            continue
                        if team_match(team_a, nm):
                            oa_list.append(float(pr))
                        elif team_match(team_b, nm):
                            ob_list.append(float(pr))
            if oa_list and ob_list:
                return round(sum(oa_list) / len(oa_list), 2), round(sum(ob_list) / len(ob_list), 2)
    return None, None


def fetch_all_espn_rss():
    titles = []
    for url in ("https://static.cricinfo.com/rss/livescores.xml", "http://static.cricinfo.com/rss/livescores.xml"):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall(".//item"):
                    t = (item.findtext("title") or "").strip()
                    if t:
                        titles.append(t)
                if titles:
                    break
        except Exception:
            continue
    return titles


def parse_espn_for_match(team_a, team_b, rss_titles):
    score_re = re.compile(r"(?<![A-Za-z])\d{1,3}(?:/\d{1,2})?")
    for title in (rss_titles or []):
        parts = re.split(r"\s+v\s+", title, flags=re.I)
        if len(parts) != 2:
            continue
        p1, p2 = parts[0], parts[1]
        if same_matchup(team_a, team_b, p1, p2):
            tl = title.lower()
            s1 = bool(score_re.search(p1))
            s2 = bool(score_re.search(p2))
            inn_cnt = int(s1) + int(s2)
            has_star = "*" in title
            ended_kw = any(k in tl for k in ("won by", "won ", "beat ", "defeated", "abandoned", "no result", "tied", "draw"))
            is_ended = ended_kw or (inn_cnt >= 2 and not has_star)
            is_live = (not is_ended) and (has_star or inn_cnt >= 1)
            return {"raw": title, "is_live": is_live, "is_ended": is_ended, "inn_cnt": inn_cnt}
    return None


def parse_utc_to_bdt(dt_str):
    if not dt_str:
        return None
    try:
        dt_utc = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(BDT)
    except Exception:
        return None


def apply_schedule_time(m, dt_bdt, time_priority):
    toss_offset = m.get("toss_offset_min", 45 if m.get("is_womens") else 30)
    toss_dt = dt_bdt - timedelta(minutes=toss_offset)
    pre_toss_dt = toss_dt - timedelta(minutes=20)
    m["match_date_bdt"] = dt_bdt.strftime("%Y-%m-%d")
    m["start_bdt"] = dt_bdt.isoformat()
    m["toss_bdt"] = toss_dt.isoformat()
    m["pre_toss_bdt"] = pre_toss_dt.isoformat()
    m["time_priority"] = time_priority


def make_match_record(mid, name, team_a, team_b, venue, m_type, series, dt_bdt, time_priority=1, sport_key=""):
    womens = is_womens_match(f"{name} {team_a} {team_b}", series)
    toss_offset = 45 if womens else 30
    m = {
        "id": str(mid),
        "name": name,
        "team_a": clean_team(team_a),
        "team_b": clean_team(team_b),
        "venue": venue or "Venue Pending",
        "series": series or "",
        "match_type": (m_type or "T20").upper(),
        "is_womens": womens,
        "toss_offset_min": toss_offset,
        "time_priority": time_priority,
        "odds_sport_key": sport_key,
        "delay_shift_min": 0,
        "last_delay_sig": "",
        "stage_polls": {},
        "sent": {
            "pre_toss": False,
            "toss_sessions": False,
            "pp_review": False,
            "innings_break": False,
            "post_match": False
        },
        "cached_pre_toss": {},
        "sniper_picks": []
    }
    apply_schedule_time(m, dt_bdt, time_priority)
    return m


def is_match_expired(m, now_bdt):
    """Auto-expires old matches (T20 > 4.5h, ODI > 8.5h past eff_start) so they never block newer matches."""
    eff_start = datetime.fromisoformat(m["start_bdt"]) + timedelta(minutes=int(m.get("delay_shift_min", 0)))
    max_life = timedelta(hours=8, minutes=30) if m.get("match_type") == "ODI" else timedelta(hours=4, minutes=30)
    return now_bdt > (eff_start + max_life)


def fetch_daily_matches_bdt(today_str, state, odds_events):
    """
    STRICT TODAY-ONLY 3-Source Discovery (Zero previous-day carry-over!).
    Automatically drops any match already expired (> 4.5h past start for T20).
    """
    now_bdt = datetime.now(BDT)
    raw_candidates = []

    # Source 1: cricScore (1 credit)
    cs_data = cricapi_get("cricScore", state).get("data") or []
    for x in cs_data:
        dt_bdt = parse_utc_to_bdt(x.get("dateTimeGMT"))
        if not dt_bdt or dt_bdt.strftime("%Y-%m-%d") != today_str:
            continue
        if str(x.get("ms") or "").lower() == "result":
            continue
        mid = str(x.get("id") or "")
        ta, tb = clean_team(x.get("t1")), clean_team(x.get("t2"))
        m_type = str(x.get("matchType") or "T20").upper()
        series = str(x.get("series") or "")
        name = f"{ta} vs {tb}, {m_type}"
        if is_major_match(name, series, ta, tb, m_type):
            rec = make_match_record(mid, name, ta, tb, "", m_type, series, dt_bdt, time_priority=1)
            if not is_match_expired(rec, now_bdt):
                raw_candidates.append(rec)

    # Source 2: currentMatches (1 credit)
    cm_data = cricapi_get("currentMatches", state, offset=0).get("data") or []
    for m in cm_data:
        if m.get("matchEnded"):
            continue
        dt_bdt = parse_utc_to_bdt(m.get("dateTimeGMT"))
        if not dt_bdt or dt_bdt.strftime("%Y-%m-%d") != today_str:
            continue
        teams = m.get("teams") or []
        if len(teams) < 2:
            continue
        mid = str(m.get("id") or "")
        ta, tb = clean_team(teams[0]), clean_team(teams[1])
        m_type = str(m.get("matchType") or "T20").upper()
        name = m.get("name") or f"{ta} vs {tb}"
        series = str(m.get("series_id") or "")
        if is_major_match(name, series, ta, tb, m_type):
            rec = make_match_record(mid, name, ta, tb, m.get("venue", ""), m_type, series, dt_bdt, time_priority=2)
            if not is_match_expired(rec, now_bdt):
                raw_candidates.append(rec)

    # Source 3: Odds API UK+AU (0 CricAPI credits — highest clock priority)
    for ev in (odds_events or []):
        dt_bdt = parse_utc_to_bdt(ev.get("commence_time"))
        if not dt_bdt or dt_bdt.strftime("%Y-%m-%d") != today_str:
            continue
        ta, tb = clean_team(ev.get("home_team")), clean_team(ev.get("away_team"))
        sport_title = ev.get("sport_title") or ev.get("_sport_key", "")
        m_type = "ODI" if "odi" in sport_title.lower() else ("TEST" if "test" in sport_title.lower() else "T20")
        name = f"{ta} vs {tb} ({sport_title})"
        if is_major_match(name, sport_title, ta, tb, m_type):
            rec = make_match_record(f"odds_{ev['id']}", name, ta, tb, "", m_type, sport_title, dt_bdt, time_priority=3, sport_key=ev.get("_sport_key", ""))
            if not is_match_expired(rec, now_bdt):
                raw_candidates.append(rec)

    deduped = []
    for cand in raw_candidates:
        match_found = None
        for ex in deduped:
            if ex["id"] == cand["id"] or (
                ex["match_date_bdt"] == cand["match_date_bdt"]
                and same_matchup(ex["team_a"], ex["team_b"], cand["team_a"], cand["team_b"])
            ):
                match_found = ex
                break
        if not match_found:
            deduped.append(cand)
        else:
            if match_found["id"].startswith("odds_") and not cand["id"].startswith("odds_"):
                match_found["id"] = cand["id"]
            if match_found["venue"] in ("", "Venue Pending") and cand["venue"] not in ("", "Venue Pending"):
                match_found["venue"] = cand["venue"]
            if not match_found["series"] and cand["series"]:
                match_found["series"] = cand["series"]
            if not match_found.get("odds_sport_key") and cand.get("odds_sport_key"):
                match_found["odds_sport_key"] = cand["odds_sport_key"]
            if cand.get("time_priority", 1) > match_found.get("time_priority", 1):
                apply_schedule_time(match_found, datetime.fromisoformat(cand["start_bdt"]), cand["time_priority"])

    deduped.sort(key=lambda x: x["start_bdt"])
    return deduped


def merge_matches(existing, discovered):
    merged = list(existing)
    added = 0
    time_fixed = 0
    for cand in discovered:
        found = None
        for ex in merged:
            if ex["id"] == cand["id"] or (
                ex.get("match_date_bdt") == cand.get("match_date_bdt")
                and same_matchup(ex["team_a"], ex["team_b"], cand["team_a"], cand["team_b"])
            ):
                found = ex
                break
        if not found:
            merged.append(cand)
            added += 1
        else:
            if found["id"].startswith("odds_") and not cand["id"].startswith("odds_"):
                found["id"] = cand["id"]
            if found.get("venue") in ("", "Venue Pending") and cand.get("venue") not in ("", "Venue Pending"):
                found["venue"] = cand["venue"]
            if not found.get("odds_sport_key") and cand.get("odds_sport_key"):
                found["odds_sport_key"] = cand["odds_sport_key"]
            if cand.get("time_priority", 1) > found.get("time_priority", 1) and cand["start_bdt"] != found.get("start_bdt"):
                apply_schedule_time(found, datetime.fromisoformat(cand["start_bdt"]), cand["time_priority"])
                time_fixed += 1
    merged.sort(key=lambda x: x["start_bdt"])
    return merged, added, time_fixed


def fetch_single_match_live(m, state, rss_titles=None):
    """Strictly date-locked live fetch (rejects any historical match from another date)."""
    match_data = {}
    mid = m["id"]
    m_date = m.get("match_date_bdt") or datetime.fromisoformat(m["start_bdt"]).strftime("%Y-%m-%d")

    if mid.startswith("odds_"):
        cm = cricapi_get("currentMatches", state, offset=0).get("data") or []
        for x in cm:
            dt_bdt = parse_utc_to_bdt(x.get("dateTimeGMT"))
            if not dt_bdt or dt_bdt.strftime("%Y-%m-%d") != m_date:
                continue
            teams = x.get("teams") or []
            if len(teams) >= 2 and same_matchup(m["team_a"], m["team_b"], teams[0], teams[1]):
                m["id"] = str(x["id"])
                mid = m["id"]
                if x.get("venue"):
                    m["venue"] = x["venue"]
                match_data = x
                break

    if not mid.startswith("odds_"):
        res = cricapi_get("match_info", state, id=mid)
        if res.get("data"):
            d = res["data"]
            d_dt = parse_utc_to_bdt(d.get("dateTimeGMT"))
            if not d_dt or d_dt.strftime("%Y-%m-%d") == m_date:
                match_data = d
                if match_data.get("venue") and m.get("venue") in ("", "Venue Pending"):
                    m["venue"] = match_data["venue"]

    espn = parse_espn_for_match(m["team_a"], m["team_b"], rss_titles if rss_titles is not None else fetch_all_espn_rss())
    match_data["_espn_line"] = espn["raw"] if espn else ""
    return match_data


def extract_xi_text(live):
    parts = []
    for tb in (live.get("players") or [])[:2]:
        plist = [p.get("name", "") for p in (tb.get("players") or [])[:11] if p.get("name")]
        if plist:
            parts.append(f"{tb.get('teamName', '')}: {', '.join(plist)}")
    return " | ".join(parts) if parts else "Pending (Use Google Search)"


# ==========================================
# 7. GEMINI AI WITH LIVE GOOGLE SEARCH
# ==========================================
def _extract_json(raw_txt):
    if not raw_txt:
        return None
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_txt.strip(), flags=re.S).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            pass
    return None


def ask_gemini_json(prompt_text, use_search=True):
    if not GEMINI_API_KEY:
        return {}, False, False
    models = []
    for m in (GEMINI_ENV_MODEL, "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"):
        if m and m not in models:
            models.append(m)

    modes = (True, False) if use_search else (False,)
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for search_on in modes:
            payload = {
                "contents": [{"parts": [{"text": prompt_text}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200}
            }
            if search_on:
                payload["tools"] = [{"google_search": {}}]
            else:
                payload["generationConfig"]["responseMimeType"] = "application/json"
            try:
                r = requests.post(url, headers={"x-goog-api-key": GEMINI_API_KEY}, json=payload, timeout=35)
                if r.status_code == 200:
                    cands = r.json().get("candidates") or []
                    if not cands:
                        continue
                    parts = cands[0].get("content", {}).get("parts") or []
                    raw = "".join(p.get("text", "") for p in parts).strip()
                    parsed = _extract_json(raw)
                    if isinstance(parsed, dict) and parsed:
                        return parsed, True, search_on
            except Exception as e:
                print(f"Gemini ({model}, search={search_on}) Error: {e}")
    return {}, False, False


# ==========================================
# 8. MESSAGE BUILDERS (PURE TOSS + DELAY + DIRECT CALLS)
# ==========================================
def send_session0_daily_schedule(today_str, matches, state):
    if not matches:
        err_note = f"\n⚠️ <b>API Note:</b> <code>{esc(API_INFO['error'])}</code>" if API_INFO["error"] else ""
        msg = (
            f"📅 <b>DAILY CRICKET RADAR ({ENGINE_VERSION}) • {esc(today_str)}</b>\n"
            f"<i>No active major T20/ODI matches remaining today.</i>{err_note}\n"
            f"💳 CricAPI Hits: <code>{credit_label(state)}</code>\n"
            f"💤 Zero-Credit Sleep Active until next scheduled check."
        )
        send_telegram(msg)
        return

    lines = [
        f"📅 <b>DAILY CRICKET SCHEDULE ({ENGINE_VERSION}) • {esc(today_str)}</b>",
        f"🕒 Window: <code>12:00 AM – 11:59 PM BDT</code> | Active Matches: <code>{len(matches)}</code>",
        f"💳 CricAPI Hits: <code>{credit_label(state)}</code>\n"
    ]
    for idx, m in enumerate(matches, 1):
        st = datetime.fromisoformat(m["start_bdt"]).strftime("%I:%M %p")
        tt = datetime.fromisoformat(m["toss_bdt"]).strftime("%I:%M %p")
        pt = datetime.fromisoformat(m["pre_toss_bdt"]).strftime("%I:%M %p")
        cat = "Women's (-45m Toss)" if m["is_womens"] else "Men's (-30m Toss)"
        clk = "✓BookieClock" if m.get("time_priority", 1) == 3 else "CricAPI"
        lines.append(
            f"<b>{idx}. {esc(m['team_a'])} vs {esc(m['team_b'])}</b> ({esc(m['match_type'])} • {clk})\n"
            f"   🏟️ <i>{esc(m['venue'])}</i> • <code>{cat}</code>\n"
            f"   ⏰ Pre-Toss: <code>{pt}</code> | Toss: <code>{tt}</code> | Match: <code>{st} BDT</code>"
        )

    lines.append("\n<blockquote>🔒 <b>Hard Credit Cap Active:</b> Max 7–9 credits/match | 0 credits in off-hours | Old matches auto-expire.</blockquote>")
    send_telegram("\n".join(lines))


def run_session1_pre_toss(m, state, rss_titles=None):
    if m.get("venue") in ("", "Venue Pending"):
        fetch_single_match_live(m, state, rss_titles)

    ta, tb, venue = m["team_a"], m["team_b"], m["venue"]
    start_dt = datetime.fromisoformat(m["start_bdt"])
    st_str = start_dt.strftime("%I:%M %p")
    tt_str = datetime.fromisoformat(m["toss_bdt"]).strftime("%I:%M %p")
    pt_str = datetime.fromisoformat(m["pre_toss_bdt"]).strftime("%I:%M %p")
    offset_label = f"-{m['toss_offset_min']}m"

    wx = fetch_weather(venue, start_dt)
    venue_ok = venue not in ("", "Venue Pending", "Unknown Venue")

    prompt = f"""Use Google Search to check real recent toss history, captains, and venue conditions for today's cricket match:
Match: {ta} vs {tb} ({m['match_type']}, Series: {m.get('series','')})
Venue: {venue}
Weather: {wx['txt']}

Output ONLY a single valid JSON object (no markdown) with these exact keys:
{{
  "cap_a_name": "Real captain name of {ta}",
  "cap_a_last5": "e.g. 3/5",
  "cap_b_name": "Real captain name of {tb}",
  "cap_b_last5": "e.g. 2/5",
  "streak_hint": "1 crisp line explaining which captain is on a hot/cold toss streak",
  "h2h_toss_record": "e.g. {ta} 3 - 2 {tb} (Last 5)",
  "h2h_hint": "1 crisp line explaining head-to-head toss trend",
  "venue_coin_trend": "e.g. 1st-listed/Home team won 4 of last 6 tosses here",
  "venue_trend_hint": "1 crisp line on series/venue toss trend",
  "toss_hints_lean": "{ta} or {tb}",
  "signals_aligned": "e.g. 2/3 Signals",
  "highest_toss_need": "{ta} or {tb}",
  "toss_need_level": "High Need / Moderate Need",
  "toss_need_reason": "1 crisp line explaining why pitch/dew/squad makes winning the toss critical for this team",
  "if_a_wins_call": "Bowl First (70%) or Bat First (65%) + 6-word reason",
  "if_b_wins_call": "Bowl First (75%) or Bat First (60%) + 6-word reason",
  "likely_call": "Bowl First or Bat First",
  "venue_par_mean": 164.0,
  "venue_par_std": 14.5
}}
Rules: NEVER guess fake stats; if exact last-5 toss record is unavailable via search, write "Unverified". Do NOT include match winner or session predictions."""

    ai, ai_ok, used_search = ask_gemini_json(prompt, use_search=True)

    is_odi = m["match_type"] == "ODI"
    default_par = (230.0 if m["is_womens"] else 272.0) if is_odi else (152.0 if m["is_womens"] else 166.0)
    par_mean = float(ai.get("venue_par_mean") or default_par)
    par_std = float(ai.get("venue_par_std") or (22.0 if is_odi else 14.5))

    m["cached_pre_toss"] = {
        "par_mean": par_mean,
        "par_std": par_std,
        "dew_high": wx["high_dew"],
        "wx_txt": wx["txt"],
        "wx_ok": wx["ok"]
    }

    if ai_ok:
        cap_a = f"{ai.get('cap_a_name', ta)} ({ai.get('cap_a_last5', 'N/A')})"
        cap_b = f"{ai.get('cap_b_name', tb)} ({ai.get('cap_b_last5', 'N/A')})"
        hints_block = (
            f"<b>1. Captain's Last-5 Streak:</b> 🔵 <code>{esc(cap_a)}</code> vs 🟠 <code>{esc(cap_b)}</code>\n"
            f"   <i>↳ {esc(ai.get('streak_hint', 'N/A'))}</i>\n"
            f"<b>2. H2H Toss History:</b> <code>{esc(ai.get('h2h_toss_record', 'Unverified'))}</code>\n"
            f"   <i>↳ {esc(ai.get('h2h_hint', 'N/A'))}</i>\n"
            f"<b>3. Venue / Series Coin Trend:</b> <code>{esc(ai.get('venue_coin_trend', 'Unverified'))}</code>\n"
            f"   <i>↳ {esc(ai.get('venue_trend_hint', 'N/A'))}</i>"
        )
        need_block = (
            f"• <b>Highest Toss Need:</b> <b><code>{esc(ai.get('highest_toss_need', 'Balanced'))}</code></b> (<i>{esc(ai.get('toss_need_level', 'Moderate'))}</i>)\n"
            f"  <i>↳ {esc(ai.get('toss_need_reason', 'N/A'))}</i>\n"
            f"• <b>Expected Call (If Won):</b>\n"
            f"  • <b>If 🔵 {esc(ta)} Wins:</b> <code>{esc(ai.get('if_a_wins_call', 'Bowl First'))}</code>\n"
            f"  • <b>If 🟠 {esc(tb)} Wins:</b> <code>{esc(ai.get('if_b_wins_call', 'Bowl First'))}</code>"
        )
        verdict = (
            f"🪙 <b>Toss Hints Lean:</b> <b><code>{esc(ai.get('toss_hints_lean', '50/50'))}</code></b> (<i>{esc(ai.get('signals_aligned', 'N/A'))}</i>) | "
            f"<b>Needs Toss More:</b> <b><code>{esc(ai.get('highest_toss_need', 'N/A'))}</code></b> | "
            f"<b>Likely Call:</b> <code>{esc(ai.get('likely_call', 'Bowl First'))}</code>"
        )
    else:
        hints_block = "⚠️ <i>AI Toss History Search unavailable right now — no fake hints generated.</i>"
        need_block = f"• <b>Weather Factor:</b> <code>{'Night/Dew favors Bowl First' if wx['high_dew'] else 'Check pitch report at toss'}</code>"
        verdict = "🪙 <b>Toss Hints:</b> <code>N/A (AI Search Failed)</code> — Wait for Confirmed Toss."

    msg = (
        f"🪙 <b>{esc(m['match_type'])} • PRE-TOSS HINTS &amp; NEED RADAR</b>\n"
        f"🔵 <b>{esc(ta)}</b> vs 🟠 <b>{esc(tb)}</b>\n"
        f"🏟️ <i>{esc(venue)}</i>\n"
        f"🕒 Match: <code>{st_str}</code> | Toss (<code>{offset_label}</code>): <code>{tt_str}</code> | Alert: <code>{pt_str} BDT</code>\n"
        f"💧 <b>Weather:</b> <code>{esc(wx['txt'])}</code>\n\n"
        f"——— 🔍 <b>TOSS WIN HINTS (3 SIGNALS)</b> ———\n"
        f"{hints_block}\n\n"
        f"——— ⚖️ <b>TOSS NEED &amp; EXPECTED CALL</b> ———\n"
        f"{need_block}\n\n"
        f"<blockquote>{verdict}</blockquote>\n"
        f"{health_badge(venue_ok=venue_ok, wx_ok=wx['ok'], ai_ok=(ai_ok and used_search))}"
    )
    if send_telegram(msg):
        m["sent"]["pre_toss"] = True


def check_and_send_delay_update(m, live, now_bdt):
    status = str(live.get("status") or "").strip()
    espn_line = str(live.get("_espn_line") or "").strip()
    blob = f"{status} {espn_line}".lower()

    has_delay_kw = any(k in blob for k in DELAY_KEYWORDS)
    toss_dt = datetime.fromisoformat(m["toss_bdt"])
    is_silent_delay = (now_bdt >= toss_dt + timedelta(minutes=12)) and not live.get("tossWinner") and not live.get("score") and not live.get("matchStarted")
    if not has_delay_kw and not is_silent_delay:
        return False

    start_dt = datetime.fromisoformat(m["start_bdt"])
    eff_start = start_dt + timedelta(minutes=int(m.get("delay_shift_min", 0)))
    if now_bdt >= eff_start - timedelta(minutes=15):
        needed_shift = int((now_bdt + timedelta(minutes=25) - start_dt).total_seconds() // 60)
        m["delay_shift_min"] = min(max(int(m.get("delay_shift_min", 0)), needed_shift), 180)

    sig_key = f"{status.lower()[:50]}|{m['delay_shift_min'] // 30}"
    if sig_key == m.get("last_delay_sig"):
        return True

    ai_delay, ai_ok, _ = ask_gemini_json(
        f"Check live cricket match delay update via Google Search for {m['team_a']} vs {m['team_b']} at {m['venue']} today. "
        f"Current API status: '{status or 'Toss Delayed'}'. "
        f"Return ONLY JSON: {{\"reason\": \"Rain / Wet Outfield / Previous Match Overrun / Toss Delayed\", "
        f"\"next_inspection\": \"Revised toss or inspection time in BDT (or Pending)\", "
        f"\"dew_impact\": \"1 short line on how delay affects dew/toss decision\"}}",
        use_search=True
    )

    reason = ai_delay.get("reason") if ai_ok else (status or "Toss Delayed / Waiting for Official Update")
    next_insp = ai_delay.get("next_inspection") if ai_ok else "Next check in 15m"
    dew_imp = ai_delay.get("dew_impact") if ai_ok else "Later start may increase evening dew / shorten overs."

    msg = (
        f"⏳ <b>TOSS / MATCH DELAY UPDATE</b>\n"
        f"🔵 <b>{esc(m['team_a'])}</b> vs 🟠 <b>{esc(m['team_b'])}</b>\n"
        f"🏟️ <i>{esc(m['venue'])}</i>\n\n"
        f"• <b>Original Toss:</b> <code>{toss_dt.strftime('%I:%M %p BDT')}</code> ➔ <b>Status:</b> <code>DELAYED</code>\n"
        f"• <b>Reason:</b> <code>{esc(reason)}</code>\n"
        f"• <b>Next Inspection / Update:</b> <code>{esc(next_insp)}</code>\n"
        f"• <b>Delay Impact:</b> <i>{esc(dew_imp)}</i>\n\n"
        f"<blockquote>🔄 <b>Auto-Shift Active (+{m['delay_shift_min']}m):</b> Windows shifted automatically. Checking every 15m (Credit-Capped).</blockquote>"
    )
    if send_telegram(msg):
        m["last_delay_sig"] = sig_key
    return True


def run_squad_audit_and_fair_prob(m, live, wx, stage_name, base_prob_a, target_info=""):
    xi_txt = extract_xi_text(live)
    prompt = f"""Use Google Search to audit the confirmed Playing XI, missing key players, recent form, and pitch matchup for {m['team_a']} vs {m['team_b']} at {m['venue']} ({m['match_type']}).
Stage: {stage_name}
Toss: {live.get('tossWinner', 'Pending')} {live.get('tossChoice', '')}
API Playing XI: {xi_txt}
Weather/Dew: {wx['txt']}
Match Situation: {target_info or 'Pre-1st Innings'}

Output ONLY valid JSON:
{{
  "squad_summary": "1 crisp sentence comparing confirmed Playing XI strengths, bowling/batting depth vs pitch",
  "squad_veto_team": "None OR exact team name that has a major XI red flag (weak death bowling/missing stars)",
  "veto_reason": "short reason if veto applies else None",
  "squad_edge_adj_a": 0.0,
  "tactical_note": "1 crisp sentence on why favored team wins or why match is 50/50"
}}
Rules: Keep squad_edge_adj_a strictly between -5.0 and +5.0 (positive favors {m['team_a']}, negative favors {m['team_b']})."""

    ai, ai_ok, _ = ask_gemini_json(prompt, use_search=True)
    if not ai_ok:
        return base_prob_a, "Playing XI Search unavailable (math-only audit)", "None", "", False

    adj = max(min(float(ai.get("squad_edge_adj_a") or 0.0), 5.0), -5.0)
    adj_prob_a = round(min(max(base_prob_a + adj, 10.0), 90.0), 1)
    summary = str(ai.get("squad_summary") or "Full-strength XIs reported.")
    veto_team = str(ai.get("squad_veto_team") or "None")
    veto_reason = str(ai.get("veto_reason") or "")
    tactical = str(ai.get("tactical_note") or "")
    return adj_prob_a, summary, veto_team, (veto_reason if veto_team.lower() != "none" else tactical), True


def run_session2_confirmed_toss(m, state, odds_events, now_bdt, rss_titles=None):
    live = fetch_single_match_live(m, state, rss_titles)
    toss_winner = str(live.get("tossWinner") or "").strip()
    toss_choice = str(live.get("tossChoice") or "").strip()
    status_txt = str(live.get("status") or "").strip()
    espn_line = str(live.get("_espn_line") or "").strip()

    if not toss_winner and "won the toss" not in status_txt.lower() and not live.get("score") and not live.get("matchStarted"):
        check_and_send_delay_update(m, live, now_bdt)
        return

    toss_str = f"{toss_winner} won toss & elected to {toss_choice.upper()}" if toss_winner else (status_txt or espn_line or "Match Live")
    ta, tb = m["team_a"], m["team_b"]
    is_odi = m["match_type"] == "ODI"
    start_dt = datetime.fromisoformat(m["start_bdt"])
    cache = m.get("cached_pre_toss", {})

    wx = {"ok": cache.get("wx_ok", False), "txt": cache.get("wx_txt", ""), "high_dew": cache.get("dew_high", False)}
    if not wx["txt"]:
        wx = fetch_weather(m["venue"], start_dt)

    default_par = (230.0 if m["is_womens"] else 272.0) if is_odi else (152.0 if m["is_womens"] else 166.0)
    par_mean = float(cache.get("par_mean") or default_par)
    par_std = float(cache.get("par_std") or (22.0 if is_odi else 14.5))

    fresh_events = fetch_odds_events(state, sport_key=m.get("odds_sport_key")) if m.get("odds_sport_key") else odds_events
    odds_a, odds_b = get_h2h_from_events(m, fresh_events)
    true_a, true_b, _ = devig_odds(odds_a, odds_b)
    odds_ok = bool(odds_a and odds_b and true_a is not None)

    bat_first = ta
    if toss_winner:
        if (team_match(ta, toss_winner) and "bowl" in toss_choice.lower()) or \
           (team_match(tb, toss_winner) and "bat" in toss_choice.lower()):
            bat_first = tb
    elif live.get("score"):
        if team_match(tb, str(live["score"][0].get("inning", ""))):
            bat_first = tb

    chasing_team = tb if bat_first == ta else ta
    dew_boost = 4.5 if wx["high_dew"] else 1.5
    base_a = true_a if odds_ok else 50.0
    base_a = min(base_a + dew_boost, 88.0) if chasing_team == ta else max(base_a - dew_boost, 12.0)

    prob_a, squad_summary, veto_team, squad_note, ai_ok = run_squad_audit_and_fair_prob(
        m, live, wx, "Confirmed Post-Toss & Playing XI", base_a
    )
    prob_b = round(100.0 - prob_a, 1)

    fav_team = ta if prob_a >= prob_b else tb
    fav_prob = max(prob_a, prob_b)
    fav_odds = (odds_a if fav_team == ta else odds_b) if odds_ok else None
    fav_edge = round((prob_a - true_a) if fav_team == ta else (prob_b - true_b), 1) if odds_ok else 0.0

    squad_vetoed = (veto_team.lower() != "none") and team_match(fav_team, veto_team)
    is_dls = any(k in status_txt.lower() for k in ("dls", "d/l", "reduced", "rain"))

    if is_dls:
        verdict = "⛔ <b>MATCH CALL: HARD SKIP (RAIN / DLS VARIANCE)</b>\n<i>↳ Rain-affected match — A+ Sniper locked off.</i>"
    elif squad_vetoed:
        verdict = f"⛔ <b>MATCH CALL: SKIP (SQUAD RISK)</b>\n<i>↳ Squad Audit Veto on {esc(fav_team)}: {esc(squad_note)}</i>"
    elif (odds_ok and fav_prob >= 72.0 and fav_edge >= 4.5) or (odds_ok and fav_prob >= 78.0 and fav_edge >= -1.0) or (not odds_ok and fav_prob >= 78.0 and ai_ok):
        odds_tag = f" @ <code>{fav_odds}</code>" if fav_odds else ""
        verdict = (
            f"🎯 <b>A+ SNIPER PICK: <code>{esc(fav_team)} WIN</code>{odds_tag} ({fav_prob}%)</b>\n"
            f"<i>↳ {esc(squad_note or 'Pitch, dew, market & Playing XI all aligned.')}</i>"
        )
        m["sniper_picks"].append({
            "label": f"{fav_team} WIN" + (f" @ {fav_odds}" if fav_odds else ""),
            "conf": int(round(fav_prob)),
            "type": "winner",
            "team": fav_team,
            "odds": fav_odds
        })
    else:
        verdict = f"⛔ <b>MATCH CALL: SKIP (NO A+ SNIPER YET)</b>\n<i>↳ Close matchup ({esc(ta)} {prob_a}% vs {esc(tb)} {prob_b}%) — waiting for 1st Innings Break.</i>"

    sessions = build_direct_sessions(par_mean, par_std, eff_max_ov=(50.0 if is_odi else 20.0), is_odi=is_odi, is_chasing=False, dew_high=wx["high_dew"])
    sess_lines = "\n".join(f"• <b>{s['label']}:</b> Expected <code>{s['lo']}–{s['hi']}</code> ➔ Lean: {s['lean']}" for s in sessions)
    live_sc = ""
    if live.get("score"):
        sc0 = live["score"][0]
        live_sc = f"\n🏏 <b>Live:</b> <code>{esc(sc0.get('inning',''))}: {sc0.get('r',0)}/{sc0.get('w',0)} ({sc0.get('o',0)} ov)</code>"
    odds_txt = f"{esc(ta)} <code>{odds_a}</code> vs {esc(tb)} <code>{odds_b}</code>" if odds_ok else "<code>N/A (Market Unlisted/Suspended)</code>"

    msg = (
        f"🪙 <b>CONFIRMED TOSS &amp; 1ST INN DIRECT CALL</b>\n"
        f"🔵 <b>{esc(ta)}</b> vs 🟠 <b>{esc(tb)}</b>\n"
        f"📢 <b>Toss:</b> <code>{esc(toss_str)}</code>{live_sc}\n"
        f"👥 <b>Squad Audit:</b> <i>{esc(squad_summary)}</i>\n"
        f"📊 <b>Live Odds:</b> {odds_txt} | <b>Model:</b> 🔵 <code>{prob_a}%</code> vs 🟠 <code>{prob_b}%</code>\n\n"
        f"🔢 <b>1ST INNINGS SESSIONS ({esc(bat_first)} Batting)</b>\n"
        f"{sess_lines}\n\n"
        f"<blockquote>{verdict}</blockquote>\n"
        f"{health_badge(venue_ok=m.get('venue') not in ('', 'Venue Pending'), wx_ok=wx['ok'], squad_ok=ai_ok, odds_ok=odds_ok)}"
    )
    if send_telegram(msg):
        m["sent"]["toss_sessions"] = True


def run_session3_pp_review(m, state, rss_titles=None):
    live = fetch_single_match_live(m, state, rss_titles)
    scores = live.get("score") or []
    if not scores:
        return
    is_odi = m["match_type"] == "ODI"
    _, eff_max_ov, is_dls = detect_dls_and_target(m, live)
    pp_gate = 9.4 if is_odi else (3.4 if eff_max_ov <= 10.0 else 5.4)

    inn1 = scores[0]
    runs = int(inn1.get("r", 0))
    wkts = int(inn1.get("w", 0))
    overs_raw = float(inn1.get("o", 0.0))
    overs_dec = ov_to_dec(overs_raw)
    if overs_dec < pp_gate or overs_dec >= eff_max_ov - 1.0:
        return

    crr = round(runs / max(overs_dec, 0.1), 2)
    cache = m.get("cached_pre_toss", {})
    par_mean = float(cache.get("par_mean") or (265.0 if is_odi else (152.0 if m["is_womens"] else 164.0)))
    par_std = float(cache.get("par_std") or (22.0 if is_odi else 14.5))
    scaled_par = par_mean * (eff_max_ov / (50.0 if is_odi else 20.0))

    rem_ov = max(eff_max_ov - overs_dec, 0.0)
    w_fac = DLS_W[min(wkts, 10)]
    revised_full = runs + (rem_ov * max(crr * 0.94, 5.2 if is_odi else 6.2) * w_fac)
    lo_f, hi_f, lean_f = session_lean(revised_full, scaled_par, max(6.0, par_std * 0.75))

    m.setdefault("cached_pre_toss", {})["actual_pp1_runs"] = runs
    dls_tag = " • 🌧️ SHORTENED" if is_dls else ""
    msg = (
        f"⚡ <b>1ST INN POWERPLAY REVIEW ({overs_raw} OV{dls_tag})</b>\n"
        f"🏏 <b>{esc(inn1.get('inning', m['team_a']))}:</b> <code>{runs}/{wkts}</code> • CRR: <code>{crr}</code>\n\n"
        f"🔄 <b>REVISED 1ST INNINGS PROJECTION ({int(eff_max_ov)} OV)</b>\n"
        f"• <b>Final 1st Inn Total:</b> Expected <code>{lo_f}–{hi_f}</code> ➔ Lean: {lean_f}\n\n"
        f"<blockquote>📈 <b>Momentum:</b> At <code>{runs}/{wkts}</code> ({overs_raw} ov), projected par is <code>{int(round(revised_full))}</code> vs baseline <code>{int(round(scaled_par))}</code>.</blockquote>"
    )
    if send_telegram(msg):
        m["sent"]["pp_review"] = True


def run_session4_innings_break(m, state, odds_events, rss_titles=None):
    live = fetch_single_match_live(m, state, rss_titles)
    scores = live.get("score") or []
    status = str(live.get("status") or "")
    st_low = status.lower()
    if not scores:
        return

    is_odi = m["match_type"] == "ODI"
    std_ov = 50.0 if is_odi else 20.0
    inn1 = scores[0]
    inn1_overs = ov_to_dec(inn1.get("o", 0.0))
    inn1_wkts = int(inn1.get("w", 0))

    target, eff_max_ov, is_dls = detect_dls_and_target(m, live)
    is_break = (len(scores) >= 2) or (inn1_overs >= eff_max_ov - 0.5) or (inn1_wkts >= 10) or ("break" in st_low) or ("need" in st_low)
    if not is_break or not target:
        return

    first_score = int(inn1.get("r", 0))
    req_rr = round(target / max(eff_max_ov, 1.0), 2)
    cache = m.get("cached_pre_toss", {})
    par_mean = float(cache.get("par_mean") or (265.0 if is_odi else (152.0 if m["is_womens"] else 164.0)))
    par_std = float(cache.get("par_std") or (22.0 if is_odi else 14.5))
    wx = {"ok": cache.get("wx_ok", False), "txt": cache.get("wx_txt", "Dew Unknown"), "high_dew": cache.get("dew_high", False)}

    inn1_name = str(inn1.get("inning", ""))
    chasing_team = m["team_b"] if team_match(m["team_a"], inn1_name) else m["team_a"]
    defending_team = m["team_a"] if chasing_team == m["team_b"] else m["team_b"]

    ratio = min(max(eff_max_ov / std_ov, 0.25), 1.0)
    scaled_par = par_mean * ratio
    eff_par = (0.45 * first_score) + (0.55 * scaled_par)
    scale = max(8.0, (26.0 if is_odi else 18.0) * math.sqrt(ratio))
    dew_runs = (6.0 if wx["high_dew"] else 0.0) * ratio
    logit = ((eff_par + dew_runs - target) / scale) + (0.18 if wx["high_dew"] else 0.0)
    raw_chase_p = round(min(max(100.0 / (1.0 + math.exp(-logit)), 12.0), 88.0), 1)
    base_prob_a = raw_chase_p if chasing_team == m["team_a"] else round(100.0 - raw_chase_p, 1)

    fresh_events = fetch_odds_events(state, sport_key=m.get("odds_sport_key")) if m.get("odds_sport_key") else odds_events
    odds_a, odds_b = get_h2h_from_events(m, fresh_events)
    true_a, true_b, _ = devig_odds(odds_a, odds_b)
    odds_ok = bool(odds_a and odds_b and true_a is not None)

    if is_dls:
        prob_a, squad_summary, veto_team, squad_note, ai_ok = base_prob_a, "Rain-shortened match — high variance.", "None", "", True
    else:
        prob_a, squad_summary, veto_team, squad_note, ai_ok = run_squad_audit_and_fair_prob(
            m, live, wx, "1st Innings Break", base_prob_a,
            target_info=f"{defending_team} scored {first_score}/{inn1_wkts} ({inn1.get('o',0)} ov); {chasing_team} needs {target} in {eff_max_ov:.0f} overs (RRR {req_rr})."
        )
    prob_b = round(100.0 - prob_a, 1)
    chase_win_p = prob_a if chasing_team == m["team_a"] else prob_b
    def_win_p = round(100.0 - chase_win_p, 1)

    fav_team = chasing_team if chase_win_p >= def_win_p else defending_team
    fav_prob = max(chase_win_p, def_win_p)
    fav_odds = (odds_a if fav_team == m["team_a"] else odds_b) if odds_ok else None
    fav_mkt = (true_a if fav_team == m["team_a"] else true_b) if odds_ok else None
    fav_edge = round(fav_prob - fav_mkt, 1) if fav_mkt is not None else 0.0

    sessions = build_direct_sessions(
        par_mean, par_std, eff_max_ov=eff_max_ov, is_odi=is_odi,
        is_chasing=True, target=target, dew_high=wx["high_dew"]
    )
    sess_lines = "\n".join(f"• <b>{s['label']}:</b> Expected <code>{s['lo']}–{s['hi']}</code> ➔ Lean: {s['lean']}" for s in sessions)
    if not sess_lines:
        sess_lines = f"• <b>Short Chase Cap:</b> All runs strictly capped at Target <code>{target}</code>."

    squad_vetoed = (veto_team.lower() != "none") and team_match(fav_team, veto_team)

    if is_dls:
        verdict = "⛔ <b>MATCH CALL: HARD SKIP (DLS / RAIN VARIANCE)</b>\n<i>↳ Rain-shortened overs create extreme lottery variance — A+ Sniper disabled.</i>"
    elif squad_vetoed:
        verdict = f"⛔ <b>MATCH CALL: SKIP (SQUAD RISK)</b>\n<i>↳ Squad Audit Veto on {esc(fav_team)}: {esc(squad_note)}</i>"
    elif (odds_ok and fav_prob >= 72.0 and fav_edge >= 4.0) or (odds_ok and fav_prob >= 78.0 and fav_edge >= -2.0) or (not odds_ok and fav_prob >= 78.0 and ai_ok):
        odds_tag = f" @ <code>{fav_odds}</code>" if fav_odds else ""
        verdict = (
            f"🎯 <b>A+ SNIPER PICK: <code>{esc(fav_team)} WIN</code>{odds_tag} ({fav_prob}%)</b>\n"
            f"<i>↳ {esc(squad_note or 'Clear target-vs-par gap + Squad Audit passed.')}</i>"
        )
        if not any(p.get("type") == "winner" and team_match(p.get("team", ""), fav_team) for p in m["sniper_picks"]):
            m["sniper_picks"].append({
                "label": f"{fav_team} WIN" + (f" @ {fav_odds}" if fav_odds else ""),
                "conf": int(round(fav_prob)),
                "type": "winner",
                "team": fav_team,
                "odds": fav_odds
            })
    else:
        verdict = f"⛔ <b>MATCH CALL: SKIP (NO A+ SNIPER EDGE)</b>\n<i>↳ Target ({target}) is too close to Adaptive Par ({int(round(eff_par))}) or lacks clear market edge.</i>"

    hdr = "🌗 <b>1ST INNINGS BREAK • 🌧️ RAIN-SHORTENED (DLS)</b>" if is_dls else "🌗 <b>1ST INNINGS BREAK • DIRECT CALL</b>"
    odds_line = (
        f"• <b>Live Break Odds:</b> {esc(m['team_a'])} <code>{odds_a}</code> vs {esc(m['team_b'])} <code>{odds_b}</code>"
        if odds_ok else "• <b>Live Break Odds:</b> <code>Suspended / N/A</code> (No fake odds used)"
    )

    msg = (
        f"{hdr}\n"
        f"🔵 <b>{esc(defending_team)}</b> <code>{first_score}/{inn1_wkts} ({inn1.get('o',0)} ov)</code> ➔ "
        f"🟠 <b>{esc(chasing_team)}</b> needs <b><code>{target}</code></b> in <code>{eff_max_ov:.0f}</code> ov (<code>RRR {req_rr}</code>)\n"
        f"🏟️ <b>Pitch &amp; Dew:</b> Adaptive Par <code>{int(round(eff_par))}</code> | {esc(wx['txt'])}\n"
        f"👥 <b>Squad Audit:</b> <i>{esc(squad_summary)}</i>\n"
        f"{odds_line}\n"
        f"📊 <b>Model Win Prob:</b> {esc(chasing_team)} <code>{chase_win_p}%</code> vs {esc(defending_team)} <code>{def_win_p}%</code>\n\n"
        f"🔢 <b>2ND INNINGS SESSIONS (Capped at Target {target})</b>\n"
        f"{sess_lines}\n\n"
        f"<blockquote>{verdict}</blockquote>\n"
        f"{health_badge(wx_ok=wx['ok'], squad_ok=ai_ok, odds_ok=odds_ok)}"
    )
    if send_telegram(msg):
        m["sent"]["innings_break"] = True


def run_session5_post_match_review(m, state, rss_titles=None):
    live = fetch_single_match_live(m, state, rss_titles)
    status = str(live.get("status") or "")
    espn_line = str(live.get("_espn_line") or "")
    st_low = (status or espn_line).lower()
    match_ended = bool(live.get("matchEnded")) or (
        bool(live.get("matchStarted") or live.get("score"))
        and any(k in st_low for k in ("won by", "won ", "no result", "abandoned", "tied"))
    )
    if not match_ended:
        return

    scores = live.get("score") or []
    score_summary = " | ".join(f"{s.get('inning','')}: {s.get('r',0)}/{s.get('w',0)} ({s.get('o',0)})" for s in scores)
    picks = m.get("sniper_picks", [])
    record = state.setdefault("paper_record", {"wins": 0, "losses": 0, "net_units": 0.0})

    review_lines = []
    if not picks:
        review_lines.append("• <i>No A+ Sniper call in this match (Disciplined SKIP).</i>")
    else:
        winner_text = st_low.split(" won")[0].strip() if " won" in st_low else ""
        for idx, p in enumerate(picks, 1):
            won = None
            if p.get("type") == "winner" and winner_text:
                won = team_match(p["team"], winner_text)
            real_odds = p.get("odds")
            if won is True:
                record["wins"] += 1
                if real_odds:
                    pnl = round(float(real_odds) - 1.0, 2)
                    record["net_units"] = round(record["net_units"] + pnl, 2)
                    review_lines.append(f"{idx}. <b>{esc(p['label'])} ({p['conf']}%)</b> → ✅ <b>WON (+{pnl:.2f}u)</b>")
                else:
                    review_lines.append(f"{idx}. <b>{esc(p['label'])} ({p['conf']}%)</b> → ✅ <b>WON</b>")
            elif won is False:
                record["losses"] += 1
                if real_odds:
                    record["net_units"] = round(record["net_units"] - 1.0, 2)
                    review_lines.append(f"{idx}. <b>{esc(p['label'])} ({p['conf']}%)</b> → ❌ <b>LOST (-1.00u)</b>")
                else:
                    review_lines.append(f"{idx}. <b>{esc(p['label'])} ({p['conf']}%)</b> → ❌ <b>LOST</b>")
            else:
                review_lines.append(f"{idx}. <b>{esc(p['label'])} ({p['conf']}%)</b> → ⚪ <b>VOID / NO RESULT</b>")

    total = record["wins"] + record["losses"]
    acc = round((record["wins"] / total) * 100.0, 1) if total else 0.0

    msg = (
        f"🏁 <b>MATCH ENDED &amp; A+ SNIPER REVIEW</b>\n"
        f"🔵 <b>{esc(m['team_a'])}</b> vs 🟠 <b>{esc(m['team_b'])}</b>\n"
        f"🏆 <b>Result:</b> <code>{esc(status or espn_line)}</code>\n"
        f"📊 <b>Scorecard:</b> <i>{esc(score_summary or espn_line or 'Logged')}</i>\n\n"
        f"📋 <b>THIS MATCH'S A+ SNIPER CALLS</b>\n"
        f"{chr(10).join(review_lines)}\n\n"
        f"<blockquote>📈 <b>A+ SNIPER RECORD:</b> <code>{record['wins']}W - {record['losses']}L</code> | "
        f"Success Rate: <b><code>{acc}%</code></b> | Verified Odds Net: <code>{record['net_units']:+.2f}u</code></blockquote>"
    )
    if send_telegram(msg):
        m["sent"]["post_match"] = True


# ==========================================
# 9. MAIN EXECUTION (STRICT CREDIT-CAPPED GATEKEEPER)
# ==========================================
def main():
    now_bdt = datetime.now(BDT)
    today_str = now_bdt.strftime("%Y-%m-%d")
    state = load_state()

    is_new_day = state.get("schedule_date") != today_str
    matches = state.get("matches", [])
    odds_events = []

    # 1. Midnight Discovery (Runs ONCE per BDT day — 2 CricAPI credits)
    if is_new_day:
        print(f"🌙 New BDT Day ({today_str} • {ENGINE_VERSION}): Locking 24h schedule...")
        state["credits_used_today"] = 0
        state["morning_sync_done"] = False
        odds_events = fetch_odds_events(state, force=True)
        discovered = fetch_daily_matches_bdt(today_str, state, odds_events)
        state["schedule_date"] = today_str
        state["matches"] = discovered
        state["last_discovery_ts"] = now_bdt.isoformat()
        save_state(state)
        send_session0_daily_schedule(today_str, discovered, state)
        matches = discovered

    # 2. Single 9:00 AM Backup Sync ONLY if midnight returned 0 matches (or on manual run)
    elif IS_MANUAL_RUN or (not matches and now_bdt.hour >= 9 and not state.get("morning_sync_done")):
        odds_events = fetch_odds_events(state, force=IS_MANUAL_RUN)
        discovered = fetch_daily_matches_bdt(today_str, state, odds_events)
        prev_count = len(matches)
        matches, added, time_fixed = merge_matches(matches, discovered)
        state["matches"] = matches
        state["last_discovery_ts"] = now_bdt.isoformat()
        if now_bdt.hour >= 9:
            state["morning_sync_done"] = True
        save_state(state)
        if added > 0 or time_fixed > 0 or (prev_count == 0 and IS_MANUAL_RUN):
            send_session0_daily_schedule(today_str, matches, state)

    if not matches:
        print(f"💤 No active matches today ({now_bdt.strftime('%I:%M %p BDT')}). 0 credits used.")
        return

    # Auto-expire old matches (> 4.5h past start for T20, > 8.5h for ODI)
    state_dirty = False
    for m in matches:
        sent = m.setdefault("sent", {})
        for k in ("pre_toss", "toss_sessions", "pp_review", "innings_break", "post_match"):
            sent.setdefault(k, False)
        if not sent["post_match"] and is_match_expired(m, now_bdt):
            sent["post_match"] = True
            state_dirty = True

    active_matches = [m for m in matches if not m["sent"]["post_match"]]
    if not active_matches:
        if state_dirty:
            save_state(state)
        print(f"💤 All today's matches completed/expired ({now_bdt.strftime('%I:%M %p BDT')}). 0 credits used.")
        return

    rss_titles = fetch_all_espn_rss()
    any_action = False

    # 3. STRICT CHECKPOINT + MAX-ATTEMPTS GATEKEEPER (Max 7–9 credits per match!)
    for m in active_matches:
        sent = m["sent"]
        is_odi = m["match_type"] == "ODI"
        shift = timedelta(minutes=int(m.get("delay_shift_min", 0)))
        pre_toss_dt = datetime.fromisoformat(m["pre_toss_bdt"])
        toss_dt = datetime.fromisoformat(m["toss_bdt"])
        start_dt = datetime.fromisoformat(m["start_bdt"])
        eff_start = start_dt + shift

        pp_s, pp_e = (45, 95) if is_odi else (30, 65)
        brk_s, brk_e = (195, 270) if is_odi else (85, 150)
        end_s, end_e = (360, 510) if is_odi else (185, 270)

        # Stage 1: Pure Pre-Toss Hints (25m before toss until toss time — max 1 attempt)
        if not sent["pre_toss"] and (pre_toss_dt - timedelta(minutes=5) <= now_bdt <= toss_dt):
            if can_poll_stage(m, "pre_toss", max_attempts=1, gap_min=15, now_bdt=now_bdt):
                mark_stage_poll(m, "pre_toss", now_bdt)
                run_session1_pre_toss(m, state, rss_titles=rss_titles)
                any_action = True

        # Stage 2: Confirmed Toss / Delay Update (toss time to eff_start + 40m — max 3 attempts, 15m gap)
        elif not sent["toss_sessions"] and (toss_dt <= now_bdt <= eff_start + timedelta(minutes=40)):
            if can_poll_stage(m, "toss", max_attempts=3, gap_min=15, now_bdt=now_bdt):
                mark_stage_poll(m, "toss", now_bdt)
                if not odds_events:
                    odds_events = fetch_odds_events(state)
                run_session2_confirmed_toss(m, state, odds_events, now_bdt, rss_titles=rss_titles)
                any_action = True

        # Stage 3: 1st Inn Powerplay Review (eff_start + 30m to +65m — max 2 attempts, 15m gap)
        elif not sent["pp_review"] and (eff_start + timedelta(minutes=pp_s) <= now_bdt <= eff_start + timedelta(minutes=pp_e)):
            if can_poll_stage(m, "pp", max_attempts=2, gap_min=15, now_bdt=now_bdt):
                mark_stage_poll(m, "pp", now_bdt)
                run_session3_pp_review(m, state, rss_titles=rss_titles)
                any_action = True

        # Stage 4: 1st Innings Break & Direct Call (eff_start + 85m to +150m — max 2 attempts, 15m gap)
        elif not sent["innings_break"] and (eff_start + timedelta(minutes=brk_s) <= now_bdt <= eff_start + timedelta(minutes=brk_e)):
            if can_poll_stage(m, "break", max_attempts=2, gap_min=15, now_bdt=now_bdt):
                mark_stage_poll(m, "break", now_bdt)
                if not odds_events:
                    odds_events = fetch_odds_events(state)
                run_session4_innings_break(m, state, odds_events, rss_titles=rss_titles)
                any_action = True

        # Stage 5: Post-Match A+ Sniper Review (eff_start + 185m to +270m — max 2 attempts, 20m gap)
        elif not sent["post_match"] and (eff_start + timedelta(minutes=end_s) <= now_bdt <= eff_start + timedelta(minutes=end_e)):
            if can_poll_stage(m, "post", max_attempts=2, gap_min=20, now_bdt=now_bdt):
                mark_stage_poll(m, "post", now_bdt)
                run_session5_post_match_review(m, state, rss_titles=rss_titles)
                any_action = True

    if any_action or state_dirty:
        save_state(state)

    if IS_MANUAL_RUN:
        upcoming = [
            f"• <b>{esc(x['team_a'])} vs {esc(x['team_b'])}</b> — "
            f"Pre-Toss: <code>{datetime.fromisoformat(x['pre_toss_bdt']).strftime('%I:%M %p')}</code> | "
            f"Toss: <code>{datetime.fromisoformat(x['toss_bdt']).strftime('%I:%M %p')}</code> | "
            f"Match: <code>{datetime.fromisoformat(x['start_bdt']).strftime('%I:%M %p')} BDT</code>"
            for x in active_matches
        ]
        send_telegram(
            f"✅ <b>Manual Workflow Check ({ENGINE_VERSION} • {now_bdt.strftime('%I:%M %p BDT')})</b>\n"
            f"💳 CricAPI Hits: <code>{credit_label(state)}</code>\n\n"
            f"<b>Today's Active Radar ({len(upcoming)}):</b>\n" + "\n".join(upcoming)
        )


if __name__ == "__main__":
    main()