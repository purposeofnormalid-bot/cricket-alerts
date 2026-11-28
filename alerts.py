import os
import re
import json
import html
import math
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ==========================================
# 1. CONFIGURATION & API KEYS (v19)
# ==========================================
ENGINE_VERSION = "v19"
BDT = timezone(timedelta(hours=6))
STATE_FILE = "state.json"

CRICAPI_KEY = os.getenv("CRICAPI_KEY") or os.getenv("CRICKET_API_KEY", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID", "")

IS_MANUAL_RUN = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

ALLOWED_FORMATS = ("t20", "odi", "t20i")
WOMENS_KEYWORDS = ("women", "womens", "women's", "wbbl", "wbpl", "wpl", "fairbreak", "-w ", "(w)")
BAD_KEYWORDS = ("u19", "under-19", "under 19", "emerging", "development", "academy", "softball", "rising stars", "t10", "ecs ", "italia")

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

API_INFO = {"hits": None, "limit": 100, "error": ""}


# ==========================================
# 2. STATE MANAGEMENT (ZERO-CREDIT MEMORY)
# ==========================================
def load_state():
    base = {
        "schedule_date": "",
        "credits_used_today": 0,
        "api_hits": None,
        "api_limit": 100,
        "last_discovery_ts": "",
        "midday_sync_done": False,
        "odds_cache": [],
        "odds_cache_ts": "",
        "matches": [],
        "paper_record": {"wins": 0, "losses": 0, "net_units": 0.0}
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            base.update(s)
        except Exception:
            pass
    return base


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


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


# ==========================================
# 4. QUANTITATIVE & BETTING MATH ENGINE
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


def calc_ev_and_kelly(model_prob_pct, market_odds):
    if not market_odds or market_odds <= 1.01 or not model_prob_pct:
        return 0.0, 0.0, 0.0
    p = model_prob_pct / 100.0
    q = 1.0 - p
    b = market_odds - 1.0
    ev_pct = ((p * market_odds) - 1.0) * 100.0
    fair_odds = round(1.0 / max(p, 0.01), 2)
    kelly_full = ((b * p) - q) / b
    q_kelly = min(max(0.0, round(kelly_full * 0.25 * 100.0, 2)), 2.50)
    return round(ev_pct, 2), q_kelly, fair_odds


def normal_cdf(x, mean, std):
    if std <= 0:
        return 0.5
    z = (x - mean) / (std * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def build_session_projections(par_total, par_std, is_odi=False, is_chasing=False, target=None, dew_high=False):
    adj_total = float(par_total)
    if is_chasing and target:
        adj_total = (adj_total * 0.45) + (float(target) * 0.55)
        if dew_high:
            adj_total += (7.0 if is_odi else 4.5)

    if is_odi:
        f1, f2 = 0.205, 0.485
        s1_mul, s2_mul = 0.40, 0.62
    else:
        f1, f2 = 0.292, 0.478
        s1_mul, s2_mul = 0.42, 0.60

    p1_proj, p2_proj, full_proj = adj_total * f1, adj_total * f2, adj_total
    p1_line = math.floor(par_total * (f1 - 0.002)) + 0.5
    p2_line = math.floor(par_total * (f2 - 0.003)) + 0.5
    full_line = math.floor(par_total) + 0.5

    def calc_ou(line, proj, std):
        under_p = min(max(round(normal_cdf(line, proj, std) * 100.0), 18), 82)
        over_p = 100 - under_p
        return {
            "line": line,
            "proj_low": max(1, int(round(proj - std * 0.25))),
            "proj_high": int(round(proj + std * 0.25)),
            "over_p": over_p,
            "under_p": under_p
        }

    return {
        "pp": calc_ou(p1_line, p1_proj, max(5.5, par_std * s1_mul)),
        "mid": calc_ou(p2_line, p2_proj, max(8.0, par_std * s2_mul)),
        "full": calc_ou(full_line, full_proj, max(12.0, par_std))
    }


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
    """Derby-safe matcher: Melbourne Renegades != Melbourne Stars; Sydney Sixers != Sydney Thunder."""
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
# 6. DATA SOURCES (CRICAPI + ODDS API [UK,AU] + ESPN RSS)
# ==========================================
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


def fetch_odds_events(state, force=False):
    """Fetches active cricket leagues across UK + AU (required for WBBL/BBL accurate start times)."""
    if not ODDS_API_KEY:
        return []
    ts = state.get("odds_cache_ts")
    if not force and ts and state.get("odds_cache"):
        try:
            age = datetime.now(BDT) - datetime.fromisoformat(ts)
            if age < timedelta(minutes=120):
                return state["odds_cache"]
        except Exception:
            pass

    events = []
    try:
        sp_r = requests.get(
            "https://api.the-odds-api.com/v4/sports/",
            params={"apiKey": ODDS_API_KEY},
            timeout=15
        )
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


def get_h2h_from_events(team_a, team_b, events):
    for ev in (events or []):
        ha, aw = ev.get("home_team", ""), ev.get("away_team", "")
        if same_matchup(team_a, team_b, ha, aw):
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


def apply_schedule_time(m, dt_bdt, time_priority):
    toss_offset = m.get("toss_offset_min", 45 if m.get("is_womens") else 30)
    toss_dt = dt_bdt - timedelta(minutes=toss_offset)
    pre_toss_dt = toss_dt - timedelta(minutes=20)
    m["start_bdt"] = dt_bdt.isoformat()
    m["toss_bdt"] = toss_dt.isoformat()
    m["pre_toss_bdt"] = pre_toss_dt.isoformat()
    m["time_priority"] = time_priority


def make_match_record(mid, name, team_a, team_b, venue, m_type, series, dt_bdt, time_priority=1):
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
        "last_override_ts": "",
        "sent": {
            "pre_toss": False,
            "toss_sessions": False,
            "pp_review": False,
            "innings_break": False,
            "hedge": False,
            "post_match": False
        },
        "cached_pre_toss": {},
        "sniper_picks": []
    }
    apply_schedule_time(m, dt_bdt, time_priority)
    return m


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


def fetch_daily_matches_bdt(today_str, state, odds_events):
    """
    3-Source Discovery + Direct Live/Ended Snapshots (no wasted API info!):
    Returns: (deduped_matches, live_snapshots_by_id, ended_snapshots_by_id)
    """
    raw_candidates = []
    live_snaps = {}
    ended_snaps = {}

    # Source 1: cricScore (1 credit)
    cs_data = cricapi_get("cricScore", state).get("data") or []
    for x in cs_data:
        mid = str(x.get("id") or "")
        ta, tb = clean_team(x.get("t1")), clean_team(x.get("t2"))
        ms = str(x.get("ms") or "").lower()
        t1s, t2s = str(x.get("t1s") or "").strip(), str(x.get("t2s") or "").strip()
        if ms == "result":
            ended_snaps[mid] = {"id": mid, "t1": ta, "t2": tb, "status": x.get("status", ""), "score_txt": f"{ta} {t1s} | {tb} {t2s}"}
            continue
        if ms == "live" or t1s or t2s:
            live_snaps[mid] = {"id": mid, "t1": ta, "t2": tb, "status": x.get("status", ""), "t1s": t1s, "t2s": t2s, "src": "cricScore"}
        dt_bdt = parse_utc_to_bdt(x.get("dateTimeGMT"))
        if not dt_bdt or dt_bdt.strftime("%Y-%m-%d") != today_str:
            continue
        m_type = str(x.get("matchType") or "T20").upper()
        series = str(x.get("series") or "")
        name = f"{ta} vs {tb}, {m_type}"
        if is_major_match(name, series, ta, tb, m_type):
            raw_candidates.append(make_match_record(mid, name, ta, tb, "", m_type, series, dt_bdt, time_priority=1))

    # Source 2: currentMatches (1 credit — carries live scores, matchStarted, venues!)
    cm_data = cricapi_get("currentMatches", state, offset=0).get("data") or []
    for m in cm_data:
        mid = str(m.get("id") or "")
        teams = m.get("teams") or []
        if len(teams) < 2:
            continue
        ta, tb = clean_team(teams[0]), clean_team(teams[1])
        if m.get("matchEnded"):
            ended_snaps[mid] = m
            continue
        if m.get("matchStarted") or (m.get("score") and len(m.get("score")) > 0):
            live_snaps[mid] = m  # Direct full live object!
        dt_bdt = parse_utc_to_bdt(m.get("dateTimeGMT"))
        if not dt_bdt or dt_bdt.strftime("%Y-%m-%d") != today_str:
            continue
        m_type = str(m.get("matchType") or "T20").upper()
        name = m.get("name") or f"{ta} vs {tb}"
        series = str(m.get("series_id") or "")
        if is_major_match(name, series, ta, tb, m_type):
            raw_candidates.append(make_match_record(mid, name, ta, tb, m.get("venue", ""), m_type, series, dt_bdt, time_priority=2))

    # Source 3: Odds API UK+AU (priority 3 — exact bookmaker start time!)
    for ev in (odds_events or []):
        dt_bdt = parse_utc_to_bdt(ev.get("commence_time"))
        if not dt_bdt or dt_bdt.strftime("%Y-%m-%d") != today_str:
            continue
        ta, tb = clean_team(ev.get("home_team")), clean_team(ev.get("away_team"))
        sport_title = ev.get("sport_title") or ev.get("_sport_key", "")
        m_type = "ODI" if "odi" in sport_title.lower() else ("TEST" if "test" in sport_title.lower() else "T20")
        name = f"{ta} vs {tb} ({sport_title})"
        if is_major_match(name, sport_title, ta, tb, m_type):
            raw_candidates.append(make_match_record(f"odds_{ev['id']}", name, ta, tb, "", m_type, sport_title, dt_bdt, time_priority=3))

    deduped = []
    for cand in raw_candidates:
        match_found = None
        for ex in deduped:
            if ex["id"] == cand["id"] or same_matchup(ex["team_a"], ex["team_b"], cand["team_a"], cand["team_b"]):
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
            if cand.get("time_priority", 1) > match_found.get("time_priority", 1):
                apply_schedule_time(match_found, datetime.fromisoformat(cand["start_bdt"]), cand["time_priority"])

    deduped.sort(key=lambda x: x["start_bdt"])
    return deduped, live_snaps, ended_snaps


def find_snap_for_match(m, snap_dict):
    if m["id"] in snap_dict:
        return snap_dict[m["id"]]
    for s in snap_dict.values():
        teams = s.get("teams") or [s.get("t1", ""), s.get("t2", "")]
        if len(teams) >= 2 and same_matchup(m["team_a"], m["team_b"], teams[0], teams[1]):
            return s
    return None


def merge_matches(existing, discovered):
    merged = list(existing)
    added = 0
    time_fixed = 0
    for cand in discovered:
        found = None
        for ex in merged:
            if ex["id"] == cand["id"] or same_matchup(ex["team_a"], ex["team_b"], cand["team_a"], cand["team_b"]):
                found = ex
                break
        if not found:
            merged.append(cand)
            added += 1
        else:
            found.setdefault("sent", {}).setdefault("hedge", False)
            if found["id"].startswith("odds_") and not cand["id"].startswith("odds_"):
                found["id"] = cand["id"]
            if found.get("venue") in ("", "Venue Pending") and cand.get("venue") not in ("", "Venue Pending"):
                found["venue"] = cand["venue"]
            if cand.get("time_priority", 1) > found.get("time_priority", 1) and cand["start_bdt"] != found.get("start_bdt"):
                apply_schedule_time(found, datetime.fromisoformat(cand["start_bdt"]), cand["time_priority"])
                time_fixed += 1
    merged.sort(key=lambda x: x["start_bdt"])
    return merged, added, time_fixed


def fetch_single_match_live(m, state, rss_titles=None):
    match_data = {}
    mid = m["id"]

    if mid.startswith("odds_"):
        cm = cricapi_get("currentMatches", state, offset=0).get("data") or []
        for x in cm:
            teams = x.get("teams") or []
            if len(teams) >= 2 and same_matchup(m["team_a"], m["team_b"], teams[0], teams[1]):
                m["id"] = x["id"]
                mid = x["id"]
                if x.get("venue"):
                    m["venue"] = x["venue"]
                match_data = x
                break

    if not mid.startswith("odds_"):
        res = cricapi_get("match_info", state, id=mid)
        if res.get("data"):
            match_data = res["data"]
            if match_data.get("venue") and m.get("venue") in ("", "Venue Pending"):
                m["venue"] = match_data["venue"]

    espn = parse_espn_for_match(m["team_a"], m["team_b"], rss_titles if rss_titles is not None else fetch_all_espn_rss())
    match_data["_espn_line"] = espn["raw"] if espn else ""
    return match_data


def ask_gemini_json(prompt_text):
    if not GEMINI_API_KEY:
        return {}
    for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite"):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {"temperature": 0.25, "responseMimeType": "application/json"}
        }
        try:
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_API_KEY}, json=payload, timeout=30)
            if r.status_code == 200:
                raw = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
                return json.loads(raw)
        except Exception as e:
            print(f"Gemini ({model}) JSON Error: {e}")
    return {}


# ==========================================
# 7. MESSAGE BUILDERS (5 SESSIONS + LIVE HEDGE)
# ==========================================
def send_session0_daily_schedule(today_str, matches, state):
    if not matches:
        err_note = f"\n⚠️ <b>API Note:</b> <code>{esc(API_INFO['error'])}</code>" if API_INFO["error"] else ""
        msg = (
            f"📅 <b>DAILY CRICKET RADAR ({ENGINE_VERSION}) • {esc(today_str)}</b>\n"
            f"<i>No major T20/ODI matches found right now.</i>{err_note}\n"
            f"💳 CricAPI Hits: <code>{credit_label(state)}</code>\n"
            f"🔄 <b>Smart Auto-Retry Active:</b> Re-checks every <code>3 hours</code>."
        )
        send_telegram(msg)
        return

    lines = [
        f"📅 <b>DAILY CRICKET SCHEDULE ({ENGINE_VERSION}) • {esc(today_str)}</b>",
        f"🕒 Window: <code>12:00 AM – 11:59 PM BDT</code> | Matches: <code>{len(matches)}</code>",
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

    lines.append("\n<blockquote>💤 <b>Zero-Credit Gatekeeper + Dual Live Sensor Active:</b> 0 credits in off-hours; auto-wakes via CricAPI Live + ESPN RSS if any match starts early.</blockquote>")
    send_telegram("\n".join(lines))


def run_session1_pre_toss(m, state, odds_events):
    ta, tb, venue = m["team_a"], m["team_b"], m["venue"]
    st_str = datetime.fromisoformat(m["start_bdt"]).strftime("%I:%M %p")
    tt_str = datetime.fromisoformat(m["toss_bdt"]).strftime("%I:%M %p")
    pt_str = datetime.fromisoformat(m["pre_toss_bdt"]).strftime("%I:%M %p")
    offset_label = f"-{m['toss_offset_min']}m"

    odds_a, odds_b = get_h2h_from_events(ta, tb, odds_events)
    true_a, true_b, vig = devig_odds(odds_a, odds_b)

    prompt = f"""
    Analyze cricket match: {ta} vs {tb} at {venue} ({m['match_type']}, Womens={m['is_womens']}, Series={m.get('series','')}).
    Return strictly valid JSON with these exact keys:
    {{
      "chase_boost_pct": "+5.5%",
      "weather_line": "Local Conditions | Rain 10% | Dew Moderate",
      "dew_high": false,
      "needs_toss_more": "{tb}",
      "needs_toss_reason": "concise 1-sentence reason why this team needs toss more",
      "cap_a": "Real Captain Name (Pref: Bowl/Bat) — 1-line tactical DNA",
      "cap_b": "Real Captain Name (Pref: Bowl/Bat) — 1-line tactical DNA",
      "par_mean": 165.0,
      "par_std": 14.5,
      "pp_avg": 47.5,
      "chase_bias_pct": 56.0,
      "pitch_note": "concise 1-sentence venue pitch & dew behavior",
      "model_prob_a": 53.0,
      "if_a_wins_call": "Bowl First (68%)",
      "if_a_wins_prob": 58.5,
      "if_a_wins_note": "1-sentence tactical advantage",
      "if_b_wins_call": "Bowl First (70%)",
      "if_b_wins_prob": 52.5,
      "if_b_wins_note": "1-sentence tactical advantage",
      "highest_toss_need": "{tb}",
      "expected_call": "Bowl First (68%)"
    }}
    """
    ai = ask_gemini_json(prompt)

    is_odi = m["match_type"] == "ODI"
    default_par = (230.0 if m["is_womens"] else 275.0) if is_odi else (152.0 if m["is_womens"] else 168.0)
    par_mean = float(ai.get("par_mean", default_par))
    par_std = float(ai.get("par_std", 22.0 if is_odi else (13.5 if m["is_womens"] else 15.0)))
    pp_avg = float(ai.get("pp_avg", round(par_mean * (0.205 if is_odi else 0.29), 1)))
    chase_bias = float(ai.get("chase_bias_pct", 56.0))
    model_a = float(ai.get("model_prob_a", 52.0))
    model_b = round(100.0 - model_a, 2)

    m["cached_pre_toss"] = {
        "par_mean": par_mean,
        "par_std": par_std,
        "dew_high": bool(ai.get("dew_high", False)),
        "model_a": model_a,
        "model_b": model_b,
        "odds_a": odds_a,
        "odds_b": odds_b,
        "if_a_wins_prob": float(ai.get("if_a_wins_prob", min(model_a + 4.5, 88.0))),
        "if_b_wins_prob": float(ai.get("if_b_wins_prob", min(model_b + 4.5, 88.0)))
    }

    fair_a_toss = round(100.0 / max(m["cached_pre_toss"]["if_a_wins_prob"], 1.0), 2)
    fair_b_toss = round(100.0 / max(m["cached_pre_toss"]["if_b_wins_prob"], 1.0), 2)

    if odds_a and odds_b and true_a is not None:
        edge_a = round(model_a - true_a, 2)
        edge_b = round(model_b - true_b, 2)
        best_side = ta if edge_a >= edge_b else tb
        best_prob = model_a if edge_a >= edge_b else model_b
        best_odds = odds_a if edge_a >= edge_b else odds_b
        best_edge = max(edge_a, edge_b)
        ev_pct, kelly_u, _ = calc_ev_and_kelly(best_prob, best_odds)

        cond_a_edge = round(m["cached_pre_toss"]["if_a_wins_prob"] - true_a, 1)
        cond_b_edge = round(m["cached_pre_toss"]["if_b_wins_prob"] - true_b, 1)
        trig_a = f" 🎯 <b>SNIPER TRIGGER (+{cond_a_edge}%)</b>" if cond_a_edge >= 5.5 else ""
        trig_b = f" 🎯 <b>SNIPER TRIGGER (+{cond_b_edge}%)</b>" if cond_b_edge >= 5.5 else ""

        market_block = (
            f"• <b>Raw Odds:</b> {esc(ta)} <code>{odds_a}</code> vs {esc(tb)} <code>{odds_b}</code> (Vig: <code>{vig}%</code>)\n"
            f"• <b>No-Vig True Prob:</b> {esc(ta)} <code>{true_a}%</code> vs {esc(tb)} <code>{true_b}%</code>\n"
            f"• <b>Model Fair Prob:</b> {esc(ta)} <code>{model_a}%</code> vs {esc(tb)} <code>{model_b}%</code>\n"
            f"• <b>Quant Edge ({esc(best_side)}):</b> <code>{best_edge:+.2f}%</code> | <b>+EV:</b> <code>{ev_pct:+.2f}%</code>"
        )
        stake_note = f"<code>{kelly_u:.2f}u</code> on {esc(best_side)}" if ev_pct >= 4.0 else "<code>0.00u</code> (Wait for Toss Lock)"
    else:
        trig_a = trig_b = ""
        market_block = "• <b>Market Status:</b> <code>Market Unopened</code> <i>(Quant Baseline Active — Checking Odds at Toss)</i>"
        stake_note = "<code>0.00u</code> (Wait for Toss Lock)"

    msg = (
        f"🏏 <b>{esc(m['match_type'])} • PRE-TOSS CONDITIONAL SNIPER BLUEPRINT</b>\n"
        f"🔵 <b>{esc(ta)}</b> vs 🟠 <b>{esc(tb)}</b>\n"
        f"🏟️ <i>{esc(venue)}</i>\n"
        f"🕒 Match: <code>{st_str}</code> | Toss (<code>{offset_label}</code>): <code>{tt_str}</code> | Alert: <code>{pt_str} BDT</code>\n\n"
        f"——— 🪙 <b>TOSS STRATEGY &amp; CONDITIONAL TRIGGERS</b> ———\n"
        f"<b>1. Toss Impact:</b> Chase Boost (<code>{esc(ai.get('chase_boost_pct', '+5.0%'))}</code>) "
        f"<i>({esc(ai.get('weather_line', 'Clear Conditions'))})</i> | "
        f"<b>Needs Toss More: {esc(ai.get('needs_toss_more', tb))}</b> — <i>{esc(ai.get('needs_toss_reason', 'Conditions favor chasing side.'))}</i>\n\n"
        f"<b>2. Captaincy &amp; Squad DNA:</b>\n"
        f"• 🔵 <b>{esc(ta)}:</b> <i>{esc(ai.get('cap_a', 'Balanced top-order & bowling depth.'))}</i>\n"
        f"• 🟠 <b>{esc(tb)}:</b> <i>{esc(ai.get('cap_b', 'Aggressive powerplay & spin control.'))}</i>\n\n"
        f"<b>3. Venue Par &amp; Surface:</b>\n"
        f"• <b>Venue Math (μ ± σ):</b> Par <code>{par_mean} ± {par_std}</code> | PP Avg: <code>{pp_avg}</code> | Chase Bias: <code>{chase_bias}%</code>\n"
        f"• <b>Surface Note:</b> <i>{esc(ai.get('pitch_note', 'Good batting surface early; dew impacts 2nd innings.'))}</i>\n\n"
        f"<b>4. Conditional If–Then Sniper Plan:</b>\n"
        f"• <b>IF 🔵 {esc(ta)} wins → {esc(ai.get('if_a_wins_call', 'Bowl First'))}:</b> "
        f"Win prob → <b><code>{m['cached_pre_toss']['if_a_wins_prob']}%</code></b> (Fair <code>{fair_a_toss}</code>){trig_a}\n"
        f"• <b>IF 🟠 {esc(tb)} wins → {esc(ai.get('if_b_wins_call', 'Bowl First'))}:</b> "
        f"Win prob → <b><code>{m['cached_pre_toss']['if_b_wins_prob']}%</code></b> (Fair <code>{fair_b_toss}</code>){trig_b}\n\n"
        f"<b>5. De-Vigged Market Math (H2H):</b>\n"
        f"{market_block}\n\n"
        f"<blockquote>🪙 <b>Pre-Toss Blueprint:</b> Coin Flip: <code>50/50 (Skip)</code> | "
        f"<b>Expected Call: {esc(ai.get('expected_call', 'Bowl First'))}</b> | "
        f"<b>0.25x Kelly:</b> {stake_note}</blockquote>"
    )
    if send_telegram(msg):
        m["sent"]["pre_toss"] = True


def run_session2_confirmed_toss(m, state, odds_events, live_data=None, rss_titles=None):
    live = live_data if live_data is not None else fetch_single_match_live(m, state, rss_titles)
    toss_winner = live.get("tossWinner", "")
    toss_choice = live.get("tossChoice", "")
    status_txt = live.get("status", "")
    espn_line = live.get("_espn_line", "")

    if not toss_winner and "won the toss" not in status_txt.lower() and not live.get("score") and not espn_line:
        now_bdt = datetime.now(BDT)
        start_bdt = datetime.fromisoformat(m["start_bdt"])
        if now_bdt < start_bdt + timedelta(minutes=8):
            print(f"⏳ Toss pending for {m['name']}, retrying next tick.")
            return
        toss_str = status_txt or "Toss Confirmed (Live Feed Sync)"
    else:
        toss_str = f"{toss_winner} won the toss & elected to {toss_choice.upper()}" if toss_winner else (status_txt or espn_line)

    ta, tb = m["team_a"], m["team_b"]
    is_odi = m["match_type"] == "ODI"
    cache = m.get("cached_pre_toss", {})
    default_par = (230.0 if m["is_womens"] else 275.0) if is_odi else (152.0 if m["is_womens"] else 166.0)
    par_mean = cache.get("par_mean", default_par)
    par_std = cache.get("par_std", 22.0 if is_odi else 14.5)
    dew_high = cache.get("dew_high", False)

    bat_first = ta
    if toss_winner:
        if (team_match(ta, toss_winner) and "bowl" in toss_choice.lower()) or \
           (team_match(tb, toss_winner) and "bat" in toss_choice.lower()):
            bat_first = tb
    elif live.get("score"):
        inn1_name = str(live["score"][0].get("inning", ""))
        if team_match(tb, inn1_name):
            bat_first = tb

    if toss_winner and team_match(ta, toss_winner):
        prob_a = cache.get("if_a_wins_prob", 56.0)
        prob_b = round(100.0 - prob_a, 2)
    elif toss_winner and team_match(tb, toss_winner):
        prob_b = cache.get("if_b_wins_prob", 56.0)
        prob_a = round(100.0 - prob_b, 2)
    else:
        prob_a = cache.get("model_a", 52.0)
        prob_b = round(100.0 - prob_a, 2)

    odds_a, odds_b = get_h2h_from_events(ta, tb, odds_events)
    if not odds_a:
        odds_a, odds_b = cache.get("odds_a"), cache.get("odds_b")
    true_a, true_b, _ = devig_odds(odds_a, odds_b)

    fav_team = ta if prob_a >= prob_b else tb
    fav_prob = max(prob_a, prob_b)
    fav_odds = (odds_a if prob_a >= prob_b else odds_b) or 1.85
    if odds_a and odds_b and true_a:
        fav_edge = round((prob_a - true_a) if prob_a >= prob_b else (prob_b - true_b), 2)
        odds_line = (
            f"• <b>Market Odds:</b> {esc(ta)} <code>{odds_a}</code> (<code>{true_a}%</code>) vs {esc(tb)} <code>{odds_b}</code> (<code>{true_b}%</code>)\n"
            f"• <b>Model Fair Prob:</b> {esc(ta)} <code>{prob_a}%</code> vs {esc(tb)} <code>{prob_b}%</code> (<b>Edge: <code>{fav_edge:+.2f}%</code></b>)"
        )
    else:
        fav_edge = round(fav_prob - 50.0, 2)
        odds_line = (
            f"• <b>Market Status:</b> <code>Market Unopened</code> (Fair Value Model)\n"
            f"• <b>Model Fair Prob:</b> {esc(ta)} <code>{prob_a}%</code> vs {esc(tb)} <code>{prob_b}%</code>"
        )

    sessions = build_session_projections(par_mean, par_std, is_odi=is_odi, is_chasing=False, dew_high=dew_high)
    pp, mid, full = sessions["pp"], sessions["mid"], sessions["full"]
    pp_lbl = "10-Over PP" if is_odi else "6-Over PP"
    mid_lbl = "25-Over" if is_odi else "10-Over"
    full_lbl = "50-Over Total" if is_odi else "20-Over Total"

    sniper_lines = []
    if (odds_a and odds_b and fav_edge >= 5.5 and fav_prob >= 58.0) or fav_prob >= 78.0:
        conf = int(min(round(fav_prob + max(fav_edge, 0) * 0.5), 92))
        pick_label = f"{fav_team} Match Winner @ {fav_odds}"
        sniper_lines.append(f"🎯 <b>A+ POST-TOSS SNIPER: {esc(pick_label)} ({conf}% | Edge {fav_edge:+.1f}%)</b>")
        m["sniper_picks"].append({"label": pick_label, "conf": conf, "type": "winner", "team": fav_team, "odds": fav_odds})

    if pp["under_p"] >= 66:
        pick_label = f"{bat_first} {pp_lbl} UNDER {pp['line']}"
        sniper_lines.append(f"🎯 <b>A+ SESSION: {esc(pick_label)} ({pp['under_p']}%)</b>")
        m["sniper_picks"].append({"label": pick_label, "conf": pp["under_p"], "type": "pp_under", "line": pp["line"], "odds": 1.90})
    elif pp["over_p"] >= 66:
        pick_label = f"{bat_first} {pp_lbl} OVER {pp['line']}"
        sniper_lines.append(f"🎯 <b>A+ SESSION: {esc(pick_label)} ({pp['over_p']}%)</b>")
        m["sniper_picks"].append({"label": pick_label, "conf": pp["over_p"], "type": "pp_over", "line": pp["line"], "odds": 1.90})

    if not sniper_lines:
        sniper_lines.append("👀 <b>WATCHLIST / NO A+ SNIPER EDGE YET</b> — <i>Wait for Powerplay or 1st Innings Break Sniper.</i>")

    live_sc = ""
    if live.get("score"):
        sc0 = live["score"][0]
        live_sc = f"\n🏏 <b>Live:</b> <code>{esc(sc0.get('inning',''))}: {sc0.get('r',0)}/{sc0.get('w',0)} ({sc0.get('o',0)} ov)</code>"

    msg = (
        f"🪙 <b>CONFIRMED TOSS &amp; 1ST INN SESSIONS</b>\n"
        f"🔵 <b>{esc(ta)}</b> vs 🟠 <b>{esc(tb)}</b>\n"
        f"📢 <b>Toss:</b> <code>{esc(toss_str)}</code>{live_sc}\n\n"
        f"📊 <b>MARKET vs FAIR VALUE</b>\n{odds_line}\n\n"
        f"🔢 <b>1ST INNINGS SESSIONS ({esc(bat_first)} Batting)</b>\n"
        f"• <b>{pp_lbl}:</b> Line <code>{pp['line']}</code> | Proj: <code>{pp['proj_low']}–{pp['proj_high']}</code> | Over <code>{pp['over_p']}%</code> / Under <code>{pp['under_p']}%</code>\n"
        f"• <b>{mid_lbl}:</b> Line <code>{mid['line']}</code> | Proj: <code>{mid['proj_low']}–{mid['proj_high']}</code> | Over <code>{mid['over_p']}%</code> / Under <code>{mid['under_p']}%</code>\n"
        f"• <b>{full_lbl}:</b> Line <code>{full['line']}</code> | Proj: <code>{full['proj_low']}–{full['proj_high']}</code> | Over <code>{full['over_p']}%</code> / Under <code>{full['under_p']}%</code>\n\n"
        f"<blockquote>{chr(10).join(sniper_lines)}</blockquote>"
    )
    if send_telegram(msg):
        m["sent"]["toss_sessions"] = True


def run_session3_pp_review(m, state, live_data=None, rss_titles=None):
    live = live_data if live_data is not None else fetch_single_match_live(m, state, rss_titles)
    scores = live.get("score") or []
    if not scores:
        return
    is_odi = m["match_type"] == "ODI"
    max_ov = 50.0 if is_odi else 20.0
    pp_gate = 9.4 if is_odi else 5.4
    inn1 = scores[0]
    runs = int(inn1.get("r", 0))
    wkts = int(inn1.get("w", 0))
    overs_raw = float(inn1.get("o", 0.0))
    overs_dec = ov_to_dec(overs_raw)
    if overs_dec < pp_gate:
        return

    crr = round(runs / max(overs_dec, 0.1), 2)
    cache = m.get("cached_pre_toss", {})
    par_mean = cache.get("par_mean", 265.0 if is_odi else (152.0 if m["is_womens"] else 164.0))
    par_std = cache.get("par_std", 22.0 if is_odi else 14.5)

    rem_ov = max(max_ov - overs_dec, 0.0)
    mid_target_ov = 35.0 if is_odi else 15.0
    revised_full = round(runs + (rem_ov * max(crr * 0.96, 5.2 if is_odi else 6.2)) - (wkts * 6.5), 1)
    revised_mid = round(runs + (max(mid_target_ov - overs_dec, 0.0) * max(crr * 0.94, 5.0 if is_odi else 6.0)) - (wkts * 3.5), 1)

    line_mid = math.floor(par_mean * (0.68 if is_odi else 0.72)) + 0.5
    line_full = math.floor(par_mean) + 0.5
    u_mid = min(max(round(normal_cdf(line_mid, revised_mid, par_std * 0.75) * 100), 18), 82)
    u_full = min(max(round(normal_cdf(line_full, revised_full, par_std * 0.85) * 100), 18), 82)

    m.setdefault("cached_pre_toss", {})["actual_pp1_runs"] = runs
    msg = (
        f"⚡ <b>1ST INN POWERPLAY REVIEW ({overs_raw} OV)</b>\n"
        f"🏏 <b>{esc(inn1.get('inning', m['team_a']))}:</b> <code>{runs}/{wkts}</code> • CRR: <code>{crr}</code>\n\n"
        f"🔄 <b>REVISED 1ST INNINGS SESSIONS</b>\n"
        f"• <b>{int(mid_target_ov)}-Over Session:</b> Line <code>{line_mid}</code> | Proj: <code>{int(revised_mid-4)}–{int(revised_mid+4)}</code> | Over <code>{100-u_mid}%</code> / Under <code>{u_mid}%</code>\n"
        f"• <b>{int(max_ov)}-Over Total:</b> Line <code>{line_full}</code> | Proj: <code>{int(revised_full-6)}–{int(revised_full+6)}</code> | Over <code>{100-u_full}%</code> / Under <code>{u_full}%</code>\n\n"
        f"<blockquote>📈 <b>Live Momentum:</b> At <code>{runs}/{wkts}</code> after <code>{overs_raw}</code> ov, projected 1st-inn total shifts to <code>{int(revised_full)}</code>.</blockquote>"
    )
    if send_telegram(msg):
        m["sent"]["pp_review"] = True


def run_session4_innings_break(m, state, odds_events, live_data=None, rss_titles=None):
    live = live_data if live_data is not None else fetch_single_match_live(m, state, rss_titles)
    scores = live.get("score") or []
    status = str(live.get("status", "")).lower()
    if not scores:
        return
    is_odi = m["match_type"] == "ODI"
    max_ov = 50.0 if is_odi else 20.0
    inn1 = scores[0]
    inn1_overs = ov_to_dec(inn1.get("o", 0.0))
    inn1_wkts = int(inn1.get("w", 0))
    if len(scores) < 2 and inn1_overs < (max_ov - 0.5) and inn1_wkts < 10 and "break" not in status and "need" not in status:
        return

    first_score = int(inn1.get("r", 0))
    target = first_score + 1
    req_rr = round(target / max_ov, 2)
    cache = m.get("cached_pre_toss", {})
    par_mean = cache.get("par_mean", 265.0 if is_odi else (152.0 if m["is_womens"] else 164.0))
    par_std = cache.get("par_std", 22.0 if is_odi else 14.5)
    dew_high = cache.get("dew_high", False)

    inn1_name = str(inn1.get("inning", ""))
    chasing_team = m["team_b"] if team_match(m["team_a"], inn1_name) else m["team_a"]
    defending_team = m["team_a"] if chasing_team == m["team_b"] else m["team_b"]

    eff_par = (0.45 * first_score) + (0.55 * par_mean)
    scale = 26.0 if is_odi else 18.0
    logit = ((eff_par + (6.0 if dew_high else 0.0) - target) / scale) + (0.18 if dew_high else 0.0)
    chase_win_prob = round(min(max(100.0 / (1.0 + math.exp(-logit)), 12.0), 88.0), 1)
    def_win_prob = round(100.0 - chase_win_prob, 1)

    odds_a, odds_b = get_h2h_from_events(m["team_a"], m["team_b"], odds_events)
    true_a, true_b, _ = devig_odds(odds_a, odds_b)
    chase_odds = (odds_a if chasing_team == m["team_a"] else odds_b) or 1.85
    def_odds = (odds_a if defending_team == m["team_a"] else odds_b) or 1.85
    chase_mkt = (true_a if chasing_team == m["team_a"] else true_b) if true_a else None
    def_mkt = (true_a if defending_team == m["team_a"] else true_b) if true_a else None

    chase_edge = round(chase_win_prob - chase_mkt, 1) if chase_mkt else 0.0
    def_edge = round(def_win_prob - def_mkt, 1) if def_mkt else 0.0

    chase_sessions = build_session_projections(par_mean, par_std, is_odi=is_odi, is_chasing=True, target=target, dew_high=dew_high)
    pp, mid, full = chase_sessions["pp"], chase_sessions["mid"], chase_sessions["full"]
    pp_lbl = "10-Over Chase PP" if is_odi else "6-Over Chase PP"
    mid_lbl = "25-Over Chase" if is_odi else "10-Over Chase"

    sniper_call = "👀 <b>WATCHLIST:</b> Close target vs par — wait for 2nd Inn Over 2.0 before entering."
    if chase_win_prob >= 66.0 or (chase_mkt and chase_edge >= 6.0):
        pick = f"{chasing_team} Chase Win @ {chase_odds}"
        sniper_call = f"🎯 <b>A+ BREAK SNIPER: {esc(pick)} ({chase_win_prob}%{' | Edge +' + str(chase_edge) + '%' if chase_mkt else ''})</b>"
        m["sniper_picks"].append({"label": pick, "conf": int(chase_win_prob), "type": "winner", "team": chasing_team, "odds": chase_odds})
    elif def_win_prob >= 66.0 or (def_mkt and def_edge >= 6.0):
        pick = f"{defending_team} Defend Win @ {def_odds}"
        sniper_call = f"🎯 <b>A+ BREAK SNIPER: {esc(pick)} ({def_win_prob}%{' | Edge +' + str(def_edge) + '%' if def_mkt else ''})</b>"
        m["sniper_picks"].append({"label": pick, "conf": int(def_win_prob), "type": "winner", "team": defending_team, "odds": def_odds})

    msg = (
        f"🌗 <b>1ST INNINGS BREAK &amp; 2ND INN SNIPER SETUP</b>\n"
        f"🎯 <b>Target:</b> <code>{target}</code> in {int(max_ov)} Overs (Req RR: <code>{req_rr}</code>)\n"
        f"📊 <b>Model Win Prob:</b> {esc(chasing_team)} <code>{chase_win_prob}%</code> vs {esc(defending_team)} <code>{def_win_prob}%</code>\n\n"
        f"🔢 <b>2ND INNINGS SESSIONS ({esc(chasing_team)} Chasing)</b>\n"
        f"• <b>{pp_lbl}:</b> Line <code>{pp['line']}</code> | Proj: <code>{pp['proj_low']}–{pp['proj_high']}</code> | Over <code>{pp['over_p']}%</code> / Under <code>{pp['under_p']}%</code>\n"
        f"• <b>{mid_lbl}:</b> Line <code>{mid['line']}</code> | Proj: <code>{mid['proj_low']}–{mid['proj_high']}</code> | Over <code>{mid['over_p']}%</code> / Under <code>{mid['under_p']}%</code>\n"
        f"• <b>Full Chase:</b> Line <code>{full['line']}</code> | Proj: <code>{full['proj_low']}–{full['proj_high']}</code> | Over <code>{full['over_p']}%</code> / Under <code>{full['under_p']}%</code>\n\n"
        f"<blockquote>{sniper_call}</blockquote>"
    )
    if send_telegram(msg):
        m["sent"]["innings_break"] = True


def run_live_hedge_check(m, state, odds_events, live_data=None, rss_titles=None):
    winner_picks = [p for p in m.get("sniper_picks", []) if p.get("type") == "winner"]
    if not winner_picks or m["sent"].get("hedge"):
        return
    live = live_data if live_data is not None else fetch_single_match_live(m, state, rss_titles)
    scores = live.get("score") or []
    if len(scores) < 2 or live.get("matchEnded"):
        return

    is_odi = m["match_type"] == "ODI"
    max_ov = 50.0 if is_odi else 20.0
    r1 = int(scores[0].get("r", 0))
    r2 = int(scores[1].get("r", 0))
    w2 = int(scores[1].get("w", 0))
    ov2 = ov_to_dec(scores[1].get("o", 0.0))
    if ov2 < (15.0 if is_odi else 7.0):
        return

    target = r1 + 1
    rem_ov = max(max_ov - ov2, 0.16)
    need = max(target - r2, 0)
    dls_w = [1.00, 0.96, 0.91, 0.84, 0.75, 0.63, 0.49, 0.34, 0.20, 0.08, 0.0][min(w2, 10)]
    exp_rem = (r1 * 0.95) * dls_w * (rem_ov / max_ov)
    scale = max(7.0, (28.0 if is_odi else 18.0) * math.sqrt(rem_ov / max_ov))
    chase_p = round(min(max(100.0 / (1.0 + math.exp(-((exp_rem - need) / scale))), 3.0), 97.0), 1)
    def_p = round(100.0 - chase_p, 1)

    inn1_name = str(scores[0].get("inning", ""))
    chasing_team = m["team_b"] if team_match(m["team_a"], inn1_name) else m["team_a"]
    pk = winner_picks[-1]
    backed_team = pk["team"]
    opp_team = m["team_b"] if team_match(backed_team, m["team_a"]) else m["team_a"]
    backed_p = chase_p if team_match(backed_team, chasing_team) else def_p

    if backed_p < 82.0:
        return

    o1 = float(pk.get("odds") or 1.85)
    opp_p = max(100.0 - backed_p, 4.0)
    o2 = round((100.0 / opp_p) * 0.94, 2)
    h_stake = round(o1 / o2, 2)
    locked = round(o1 - 1.0 - h_stake, 2)
    if locked <= 0.08:
        return

    msg = (
        f"🔒 <b>LOCK PROFIT / HEDGE WINDOW (2ND INN)</b>\n"
        f"🏏 <b>{esc(m['team_a'])} vs {esc(m['team_b'])}</b>\n"
        f"📊 Live: <code>{r2}/{w2} ({scores[1].get('o',0)} ov)</code> — Need <code>{need}</code> off <code>{rem_ov:.1f}</code> ov\n\n"
        f"• <b>Tracked Sniper:</b> <code>1.00u</code> on <b>{esc(backed_team)}</b> @ <code>{o1}</code>\n"
        f"• <b>Live Win Prob:</b> <b>{esc(backed_team)}</b> <code>{backed_p}%</code>\n"
        f"• <b>Hedge Stake:</b> Place <b><code>{h_stake}u</code></b> on <b>{esc(opp_team)}</b> @ ~<code>{o2}</code>\n"
        f"<blockquote>✅ <b>Guaranteed Profit Locked: <code>+{locked:.2f}u</code></b> regardless of finish.</blockquote>"
    )
    if send_telegram(msg):
        m["sent"]["hedge"] = True


def run_session5_post_match_review(m, state, live_data=None, rss_titles=None):
    live = live_data if live_data is not None else fetch_single_match_live(m, state, rss_titles)
    status = str(live.get("status", ""))
    espn_line = str(live.get("_espn_line", ""))
    st_low = (status or espn_line).lower()
    match_ended = bool(live.get("matchEnded")) or any(k in st_low for k in ("won by", "won ", "no result", "abandoned", "tied"))
    if not match_ended:
        return

    scores = live.get("score") or []
    score_summary = " | ".join(f"{s.get('inning','')}: {s.get('r',0)}/{s.get('w',0)} ({s.get('o',0)})" for s in scores) or live.get("score_txt", "")
    picks = m.get("sniper_picks", [])
    record = state.setdefault("paper_record", {"wins": 0, "losses": 0, "net_units": 0.0})

    review_lines = []
    if not picks:
        review_lines.append("• <i>No A+ Sniper trades triggered (Disciplined Skip).</i>")
    else:
        pp1_runs = m.get("cached_pre_toss", {}).get("actual_pp1_runs")
        winner_text = st_low.split(" won")[0].strip() if " won" in st_low else ""
        for idx, p in enumerate(picks, 1):
            won = None
            if p["type"] == "winner" and winner_text:
                won = team_match(p["team"], winner_text)
            elif p["type"] == "pp_under" and pp1_runs is not None:
                won = pp1_runs < p["line"]
            elif p["type"] == "pp_over" and pp1_runs is not None:
                won = pp1_runs > p["line"]

            o1 = float(p.get("odds") or 1.85)
            if won is True:
                pnl = round(o1 - 1.0, 2)
                record["wins"] += 1
                record["net_units"] = round(record["net_units"] + pnl, 2)
                review_lines.append(f"{idx}. <b>{esc(p['label'])} ({p['conf']}%)</b> → ✅ <b>WON (+{pnl:.2f}u)</b>")
            elif won is False:
                record["losses"] += 1
                record["net_units"] = round(record["net_units"] - 1.00, 2)
                review_lines.append(f"{idx}. <b>{esc(p['label'])} ({p['conf']}%)</b> → ❌ <b>LOST (-1.00u)</b>")
            else:
                review_lines.append(f"{idx}. <b>{esc(p['label'])} ({p['conf']}%)</b> → ⚪ <b>VOID / UNGRADED</b>")

    total_graded = record["wins"] + record["losses"]
    acc = round((record["wins"] / total_graded) * 100.0, 1) if total_graded else 0.0
    roi = round((record["net_units"] / max(total_graded, 1)) * 100.0, 1)

    msg = (
        f"🏁 <b>MATCH ENDED &amp; A+ SNIPER REVIEW</b>\n"
        f"🔵 <b>{esc(m['team_a'])}</b> vs 🟠 <b>{esc(m['team_b'])}</b>\n"
        f"🏆 <b>Result:</b> <code>{esc(status or espn_line)}</code>\n"
        f"📊 <b>Scorecard:</b> <i>{esc(score_summary or espn_line or 'Logged')}</i>\n\n"
        f"📋 <b>THIS MATCH'S A+ SNIPER CALLS</b>\n"
        f"{chr(10).join(review_lines)}\n\n"
        f"<blockquote>📈 <b>OVERALL PAPER RECORD:</b> <code>{record['wins']}W - {record['losses']}L</code> | "
        f"Win Rate: <code>{acc}%</code> | Net: <code>{record['net_units']:+.2f}u</code> (ROI: <code>{roi:+.1f}%</code>)</blockquote>"
    )
    if send_telegram(msg):
        m["sent"]["post_match"] = True


# ==========================================
# 8. MAIN EXECUTION & DUAL LIVE-SENSOR GATEKEEPER
# ==========================================
def main():
    now_bdt = datetime.now(BDT)
    today_str = now_bdt.strftime("%Y-%m-%d")
    state = load_state()

    is_new_day = state.get("schedule_date") != today_str
    matches = state.get("matches", [])
    live_snaps, ended_snaps = {}, {}

    need_retry_empty = False
    if not is_new_day and not matches:
        last_ts = state.get("last_discovery_ts")
        if not last_ts:
            need_retry_empty = True
        else:
            try:
                if now_bdt - datetime.fromisoformat(last_ts) >= timedelta(hours=3):
                    need_retry_empty = True
            except Exception:
                need_retry_empty = True

    need_midday_sync = (not is_new_day) and bool(matches) and (now_bdt.hour >= 9) and (not state.get("midday_sync_done"))
    odds_events = []

    if is_new_day:
        print(f"🌙 New BDT Day ({today_str}): Running 3-Source Discovery ({ENGINE_VERSION})...")
        state["credits_used_today"] = 0
        state["midday_sync_done"] = False
        odds_events = fetch_odds_events(state, force=True)
        discovered, live_snaps, ended_snaps = fetch_daily_matches_bdt(today_str, state, odds_events)
        state["schedule_date"] = today_str
        state["matches"] = discovered
        state["last_discovery_ts"] = now_bdt.isoformat()
        save_state(state)
        send_session0_daily_schedule(today_str, discovered, state)
        matches = discovered

    elif IS_MANUAL_RUN or need_retry_empty or need_midday_sync:
        odds_events = fetch_odds_events(state, force=IS_MANUAL_RUN)
        discovered, live_snaps, ended_snaps = fetch_daily_matches_bdt(today_str, state, odds_events)
        prev_count = len(matches)
        matches, added, time_fixed = merge_matches(matches, discovered)
        state["matches"] = matches
        state["last_discovery_ts"] = now_bdt.isoformat()
        if need_midday_sync or now_bdt.hour >= 9:
            state["midday_sync_done"] = True
        save_state(state)
        if added > 0 or time_fixed > 0 or (prev_count == 0 and IS_MANUAL_RUN):
            send_session0_daily_schedule(today_str, matches, state)

    if not matches:
        print(f"💤 No major matches found yet ({now_bdt.strftime('%I:%M %p BDT')}). Auto-retry in 3h.")
        return

    has_unfinished = any(not m.get("sent", {}).get("post_match") for m in matches)
    rss_titles = fetch_all_espn_rss() if has_unfinished else []

    any_action = False
    state_dirty = False

    for m in matches:
        sent = m.setdefault("sent", {})
        for k in ("pre_toss", "toss_sessions", "pp_review", "innings_break", "hedge", "post_match"):
            sent.setdefault(k, False)
        if sent["post_match"]:
            continue

        is_odi = m["match_type"] == "ODI"
        pre_toss_dt = datetime.fromisoformat(m["pre_toss_bdt"])
        toss_dt = datetime.fromisoformat(m["toss_bdt"])
        start_dt = datetime.fromisoformat(m["start_bdt"])

        pp_s, pp_e = (42, 85) if is_odi else (25, 58)
        brk_s, brk_e = (195, 260) if is_odi else (78, 138)
        hdg_s, hdg_e = (265, 420) if is_odi else (115, 195)
        end_s, end_e = (360, 620) if is_odi else (165, 420)

        espn = parse_espn_for_match(m["team_a"], m["team_b"], rss_titles)
        api_live = find_snap_for_match(m, live_snaps)
        api_ended = find_snap_for_match(m, ended_snaps)

        # Already ended in CricAPI cricScore/currentMatches -> settle with 0 extra credits!
        if api_ended:
            end_obj = dict(api_ended)
            end_obj["matchEnded"] = True
            end_obj["_espn_line"] = espn["raw"] if espn else ""
            run_session5_post_match_review(m, state, live_data=end_obj, rss_titles=rss_titles)
            any_action = True
            continue

        # DUAL LIVE SENSOR: CricAPI live flag OR ESPN RSS proves match is live despite wrong clock!
        is_early_live = (bool(api_live) or bool(espn and (espn["is_live"] or espn["is_ended"]))) and now_bdt < start_dt
        if is_early_live:
            last_ov = m.get("last_override_ts", "")
            can_poll = IS_MANUAL_RUN or not last_ov
            if not can_poll:
                try:
                    can_poll = (now_bdt - datetime.fromisoformat(last_ov)) >= timedelta(minutes=10)
                except Exception:
                    can_poll = True
            if can_poll:
                print(f"🚨 Dual Live Override triggered for {m['name']}!")
                if not odds_events:
                    odds_events = fetch_odds_events(state)
                # Use full currentMatches object directly if it has score, else fetch match_info (1 credit)
                if api_live and api_live.get("score") and api_live.get("tossWinner"):
                    live = dict(api_live)
                    live["_espn_line"] = espn["raw"] if espn else ""
                else:
                    live = fetch_single_match_live(m, state, rss_titles)
                m["last_override_ts"] = now_bdt.isoformat()
                state_dirty = True
                scores = live.get("score") or []
                max_ov = 50.0 if is_odi else 20.0
                pp_gate = 9.4 if is_odi else 5.4
                status_low = str(live.get("status", "")).lower()

                if (espn and espn["is_ended"]) or live.get("matchEnded") or "won" in status_low:
                    run_session5_post_match_review(m, state, live_data=live, rss_titles=rss_titles)
                    any_action = True
                elif len(scores) >= 2 or (len(scores) == 1 and (ov_to_dec(scores[0].get("o", 0)) >= max_ov - 0.5 or int(scores[0].get("w", 0)) >= 10 or "break" in status_low)):
                    if not sent["innings_break"]:
                        run_session4_innings_break(m, state, odds_events, live_data=live, rss_titles=rss_titles)
                        any_action = True
                    elif not sent["hedge"] and any(p.get("type") == "winner" for p in m.get("sniper_picks", [])):
                        run_live_hedge_check(m, state, odds_events, live_data=live, rss_titles=rss_titles)
                        any_action = True
                elif len(scores) == 1:
                    if ov_to_dec(scores[0].get("o", 0)) >= pp_gate and not sent["pp_review"]:
                        run_session3_pp_review(m, state, live_data=live, rss_titles=rss_titles)
                        any_action = True
                    elif not sent["toss_sessions"]:
                        run_session2_confirmed_toss(m, state, odds_events, live_data=live, rss_titles=rss_titles)
                        any_action = True
                elif not sent["toss_sessions"]:
                    run_session2_confirmed_toss(m, state, odds_events, live_data=live, rss_titles=rss_titles)
                    any_action = True
            continue

        # NORMAL SCHEDULE WINDOWS (0 credits outside active windows)
        if not sent["pre_toss"] and (pre_toss_dt - timedelta(minutes=5) <= now_bdt <= toss_dt - timedelta(minutes=3)):
            if not odds_events:
                odds_events = fetch_odds_events(state)
            run_session1_pre_toss(m, state, odds_events)
            any_action = True

        elif not sent["toss_sessions"] and (toss_dt <= now_bdt <= start_dt + timedelta(minutes=20)):
            if not odds_events:
                odds_events = fetch_odds_events(state)
            run_session2_confirmed_toss(m, state, odds_events, rss_titles=rss_titles)
            any_action = True

        elif not sent["pp_review"] and (start_dt + timedelta(minutes=pp_s) <= now_bdt <= start_dt + timedelta(minutes=pp_e)):
            run_session3_pp_review(m, state, rss_titles=rss_titles)
            any_action = True

        elif not sent["innings_break"] and (start_dt + timedelta(minutes=brk_s) <= now_bdt <= start_dt + timedelta(minutes=brk_e)):
            if not odds_events:
                odds_events = fetch_odds_events(state)
            run_session4_innings_break(m, state, odds_events, rss_titles=rss_titles)
            any_action = True

        elif not sent["hedge"] and any(p.get("type") == "winner" for p in m.get("sniper_picks", [])) and (
            start_dt + timedelta(minutes=hdg_s) <= now_bdt <= start_dt + timedelta(minutes=hdg_e)
        ):
            run_live_hedge_check(m, state, odds_events, rss_titles=rss_titles)
            any_action = True

        elif not sent["post_match"] and (
            (start_dt + timedelta(minutes=end_s) <= now_bdt <= start_dt + timedelta(minutes=end_e))
            or (espn and espn["is_ended"])
        ):
            run_session5_post_match_review(m, state, rss_titles=rss_titles)
            any_action = True

    if any_action or state_dirty:
        save_state(state)

    if IS_MANUAL_RUN:
        upcoming = []
        for x in matches:
            if x["sent"].get("post_match"):
                continue
            es = parse_espn_for_match(x["team_a"], x["team_b"], rss_titles)
            al = find_snap_for_match(x, live_snaps)
            live_tag = " 🔴 LIVE" if (al or (es and es["is_live"])) else ""
            upcoming.append(
                f"• <b>{esc(x['team_a'])} vs {esc(x['team_b'])}</b>{live_tag} — "
                f"Toss: <code>{datetime.fromisoformat(x['toss_bdt']).strftime('%I:%M %p')}</code> | "
                f"Match: <code>{datetime.fromisoformat(x['start_bdt']).strftime('%I:%M %p')} BDT</code>"
            )
        send_telegram(
            f"✅ <b>Manual Workflow Check ({ENGINE_VERSION} • {now_bdt.strftime('%I:%M %p BDT')})</b>\n"
            f"💳 CricAPI Hits: <code>{credit_label(state)}</code>\n\n"
            f"<b>Today's Active Radar ({len(upcoming)}):</b>\n" + ("\n".join(upcoming) if upcoming else "All matches completed.")
        )


if __name__ == "__main__":
    main()