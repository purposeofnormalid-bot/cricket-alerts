import os
import sys
import json
import html
import math
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ==========================================
# 1. CONFIGURATION & API KEYS
# ==========================================
BDT = timezone(timedelta(hours=6))
STATE_FILE = "state.json"

CRICAPI_KEY = os.getenv("CRICAPI_KEY") or os.getenv("CRICKET_API_KEY", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID", "")

IS_MANUAL_RUN = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

# Relevant T20 / ODI / International / Franchise keywords
ALLOWED_FORMATS = ("t20", "odi", "t20i")
WOMENS_KEYWORDS = ("women", "womens", "wbbl", "wbpl", "wpl", "fairbreak", "girls")


# ==========================================
# 2. STATE MANAGEMENT (ZERO-CREDIT MEMORY)
# ==========================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "schedule_date": "",
        "credits_used_today": 0,
        "matches": [],
        "paper_record": {"wins": 0, "losses": 0, "net_units": 0.0}
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ==========================================
# 3. TELEGRAM HTML SENDER (SAFE ESCAPING)
# ==========================================
def esc(text):
    return html.escape(str(text)) if text is not None else ""


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials missing.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"Telegram Error {r.status_code}: {r.text}")
            return False
        return True
    except Exception as e:
        print(f"Telegram Exception: {e}")
        return False


# ==========================================
# 4. QUANTITATIVE & BETTING MATH ENGINE
# ==========================================
def devig_odds(odds_a, odds_b):
    """Removes bookmaker overround (vig) to find true probabilities."""
    if not odds_a or not odds_b or odds_a <= 1.0 or odds_b <= 1.0:
        return None, None, 0.0
    imp_a = 1.0 / odds_a
    imp_b = 1.0 / odds_b
    total_imp = imp_a + imp_b
    vig_pct = (total_imp - 1.0) * 100.0
    true_a = (imp_a / total_imp) * 100.0
    true_b = (imp_b / total_imp) * 100.0
    return round(true_a, 2), round(true_b, 2), round(vig_pct, 2)


def calc_ev_and_kelly(model_prob_pct, market_odds):
    """Calculates +EV% and 0.25x Quarter-Kelly stake in units (1u = 1% bankroll)."""
    if not market_odds or market_odds <= 1.01 or not model_prob_pct:
        return 0.0, 0.0, 0.0
    p = model_prob_pct / 100.0
    q = 1.0 - p
    b = market_odds - 1.0
    ev_pct = ((p * market_odds) - 1.0) * 100.0
    fair_odds = round(1.0 / max(p, 0.01), 2)
    kelly_full = ((b * p) - q) / b
    quarter_kelly_units = max(0.0, round(kelly_full * 0.25 * 100.0, 2))
    # Cap single stake at 2.50u for strict risk control
    quarter_kelly_units = min(quarter_kelly_units, 2.50)
    return round(ev_pct, 2), quarter_kelly_units, fair_odds


def normal_cdf(x, mean, std):
    """Cumulative normal distribution for Over/Under session probabilities."""
    if std <= 0:
        return 0.5
    z = (x - mean) / (std * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def build_session_projections(par_total, par_std, is_chasing=False, target=None, dew_high=False):
    """Generates Line, Projection Range, Over% and Under% for 6-ov, 10-ov, and 20-ov."""
    adj_total = float(par_total)
    if is_chasing and target:
        # Chasing pace adjusts toward target
        adj_total = (adj_total * 0.45) + (float(target) * 0.55)
        if dew_high:
            adj_total += 4.5

    # Standard T20 phase proportions: 6-ov (~29%), 10-ov (~47.5%), 20-ov (100%)
    pp_proj = adj_total * 0.292
    ov10_proj = adj_total * 0.478
    ov20_proj = adj_total

    # Synthetic bookie lines (rounded to .5)
    pp_line = math.floor(par_total * 0.290) + 0.5
    ov10_line = math.floor(par_total * 0.475) + 0.5
    ov20_line = math.floor(par_total) + 0.5

    pp_std = max(5.5, par_std * 0.42)
    ov10_std = max(8.0, par_std * 0.60)
    ov20_std = max(12.0, par_std)

    def calc_ou(line, proj, std):
        under_p = normal_cdf(line, proj, std) * 100.0
        under_p = min(max(round(under_p), 18), 82)
        over_p = 100 - under_p
        low_r = int(round(proj - std * 0.25))
        high_r = int(round(proj + std * 0.25))
        return {
            "line": line,
            "proj_low": low_r,
            "proj_high": high_r,
            "over_p": over_p,
            "under_p": under_p
        }

    return {
        "pp6": calc_ou(pp_line, pp_proj, pp_std),
        "ov10": calc_ou(ov10_line, ov10_proj, ov10_std),
        "ov20": calc_ou(ov20_line, ov20_proj, ov20_std)
    }


# ==========================================
# 5. DATA SOURCES (CRICAPI, ESPN RSS, ODDS, GEMINI)
# ==========================================
def is_womens_match(name, series=""):
    blob = f"{name} {series}".lower()
    return any(k in blob for k in WOMENS_KEYWORDS)


def fetch_daily_matches_bdt(today_str, state):
    """
    Fetches ALL matches occurring between 00:00:00 BDT and 23:59:59 BDT of today_str.
    Properly converts UTC timestamps to BDT first so 12 AM - 5 AM BDT matches are never missed.
    """
    if not CRICAPI_KEY:
        print("⚠️ CRICAPI_KEY not set.")
        return []

    url = f"https://api.cricapi.com/v1/currentMatches?apikey={CRICAPI_KEY}&offset=0"
    try:
        r = requests.get(url, timeout=15)
        state["credits_used_today"] = state.get("credits_used_today", 0) + 1
        data = r.json()
        if data.get("status") != "success":
            print("CricAPI Status:", data)
            return []

        raw_matches = data.get("data", [])
        bdt_matches = []

        for m in raw_matches:
            m_type = str(m.get("matchType", "")).lower()
            if m_type and m_type not in ALLOWED_FORMATS:
                continue

            dt_gmt_str = m.get("dateTimeGMT", "")
            if not dt_gmt_str:
                continue

            try:
                # Parse ISO UTC string and convert strictly to Asia/Dhaka (UTC+6)
                dt_utc = datetime.fromisoformat(dt_gmt_str.replace("Z", "+00:00"))
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_bdt = dt_utc.astimezone(BDT)
            except Exception:
                continue

            # Strict 24-hour BDT check (12:00 AM to 11:59 PM BDT)
            if dt_bdt.strftime("%Y-%m-%d") != today_str:
                continue

            teams = m.get("teams", [])
            team_a = teams[0] if len(teams) > 0 else "Team A"
            team_b = teams[1] if len(teams) > 1 else "Team B"
            name = m.get("name", f"{team_a} vs {team_b}")
            series = m.get("series_id", "")
            womens = is_womens_match(name, str(m.get("status", "")))
            toss_offset = 45 if womens else 30

            toss_dt = dt_bdt - timedelta(minutes=toss_offset)
            pre_toss_dt = toss_dt - timedelta(minutes=20)

            bdt_matches.append({
                "id": m.get("id"),
                "name": name,
                "team_a": team_a,
                "team_b": team_b,
                "venue": m.get("venue", "Unknown Venue"),
                "match_type": m_type.upper() or "T20",
                "is_womens": womens,
                "toss_offset_min": toss_offset,
                "start_bdt": dt_bdt.isoformat(),
                "toss_bdt": toss_dt.isoformat(),
                "pre_toss_bdt": pre_toss_dt.isoformat(),
                "sent": {
                    "pre_toss": False,
                    "toss_sessions": False,
                    "pp_review": False,
                    "innings_break": False,
                    "post_match": False
                },
                "cached_pre_toss": {},
                "sniper_picks": []
            })

        # Sort chronologically from 12:00 AM BDT to 11:59 PM BDT
        bdt_matches.sort(key=lambda x: x["start_bdt"])
        return bdt_matches

    except Exception as e:
        print(f"Fetch Daily Matches Error: {e}")
        return []


def fetch_single_match_live(match_id, state):
    """Fetches single match scorecard/toss info (1 credit) with ESPN RSS fallback."""
    match_data = {}
    if CRICAPI_KEY and match_id:
        url = f"https://api.cricapi.com/v1/match_info?apikey={CRICAPI_KEY}&id={match_id}"
        try:
            r = requests.get(url, timeout=15)
            state["credits_used_today"] = state.get("credits_used_today", 0) + 1
            res = r.json()
            if res.get("status") == "success" and res.get("data"):
                match_data = res["data"]
        except Exception as e:
            print(f"CricAPI match_info error: {e}")

    # Backup live score text from ESPN RSS (0 credits)
    espn_summary = ""
    try:
        rss_r = requests.get("http://static.cricinfo.com/rss/livescores.xml", timeout=10)
        if rss_r.status_code == 200:
            root = ET.fromstring(rss_r.text)
            for item in root.findall(".//item"):
                title = item.findtext("title", "")
                if title:
                    espn_summary += title + " | "
    except Exception:
        pass

    match_data["_espn_rss_blob"] = espn_summary
    return match_data


def fetch_h2h_odds(team_a, team_b):
    """Fetches H2H cricket odds from The Odds API (free tier friendly)."""
    if not ODDS_API_KEY:
        return None, None
    sports = ["cricket_icc_world_cup", "cricket_international_t20", "cricket_big_bash", "cricket_ipl"]
    ta_low = team_a.lower().split()[0]
    tb_low = team_b.lower().split()[0]

    for sp in sports:
        url = f"https://api.the-odds-api.com/v4/sports/{sp}/odds/?apiKey={ODDS_API_KEY}&regions=uk,au,eu&markets=h2h"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                continue
            for ev in r.json():
                home = ev.get("home_team", "").lower()
                away = ev.get("away_team", "").lower()
                if (ta_low in home or ta_low in away) and (tb_low in home or tb_low in away):
                    bookmakers = ev.get("bookmakers", [])
                    if not bookmakers:
                        continue
                    outcomes = bookmakers[0].get("markets", [{}])[0].get("outcomes", [])
                    oa, ob = None, None
                    for out in outcomes:
                        oname = out.get("name", "").lower()
                        if ta_low in oname:
                            oa = float(out.get("price", 0))
                        elif tb_low in oname:
                            ob = float(out.get("price", 0))
                    if oa and ob:
                        return oa, ob
        except Exception:
            continue
    return None, None


def ask_gemini_json(prompt_text):
    """Calls Gemini REST API directly to get structured JSON tactical & venue insights."""
    if not GEMINI_API_KEY:
        return {}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.25,
            "responseMimeType": "application/json"
        }
    }
    try:
        r = requests.post(url, json=payload, timeout=25)
        if r.status_code == 200:
            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(raw)
    except Exception as e:
        print(f"Gemini JSON Error: {e}")
    return {}


# ==========================================
# 6. MESSAGE BUILDERS (5 MODULAR SESSIONS)
# ==========================================
def send_session0_daily_schedule(today_str, matches, state):
    if not matches:
        msg = (
            f"📅 <b>DAILY CRICKET RADAR • {esc(today_str)} (BDT)</b>\n"
            f"<i>No eligible T20/ODI matches found between 12:00 AM and 11:59 PM BDT today.</i>\n"
            f"💤 Gatekeeper Active: <code>0 credits</code> will be used until midnight."
        )
        send_telegram(msg)
        return

    lines = [
        f"📅 <b>DAILY CRICKET SCHEDULE LOCKED • {esc(today_str)}</b>",
        f"🕒 Window: <code>12:00 AM – 11:59 PM BDT</code> | Matches: <code>{len(matches)}</code>",
        f"💳 Credits Used Today: <code>{state.get('credits_used_today', 1)}/100</code>\n"
    ]

    for idx, m in enumerate(matches, 1):
        st = datetime.fromisoformat(m["start_bdt"]).strftime("%I:%M %p")
        tt = datetime.fromisoformat(m["toss_bdt"]).strftime("%I:%M %p")
        pt = datetime.fromisoformat(m["pre_toss_bdt"]).strftime("%I:%M %p")
        cat = "Women's (-45m Toss)" if m["is_womens"] else "Men's (-30m Toss)"
        lines.append(
            f"<b>{idx}. {esc(m['team_a'])} vs {esc(m['team_b'])}</b> ({esc(m['match_type'])})\n"
            f"   🏟️ <i>{esc(m['venue'])}</i> • <code>{cat}</code>\n"
            f"   ⏰ Pre-Toss: <code>{pt}</code> | Toss: <code>{tt}</code> | Match: <code>{st} BDT</code>"
        )

    lines.append("\n<blockquote>💤 <b>Zero-Credit Gatekeeper Active:</b> Bot will sleep in off-hours and wake up strictly at each Pre-Toss, Toss, Powerplay, Innings Break &amp; Match-End window.</blockquote>")
    send_telegram("\n".join(lines))


def run_session1_pre_toss(m, state):
    ta, tb, venue = m["team_a"], m["team_b"], m["venue"]
    st_str = datetime.fromisoformat(m["start_bdt"]).strftime("%I:%M %p")
    tt_str = datetime.fromisoformat(m["toss_bdt"]).strftime("%I:%M %p")
    pt_str = datetime.fromisoformat(m["pre_toss_bdt"]).strftime("%I:%M %p")
    offset_label = f"-{m['toss_offset_min']}m"

    odds_a, odds_b = fetch_h2h_odds(ta, tb)
    true_a, true_b, vig = devig_odds(odds_a, odds_b)

    prompt = f"""
    Analyze cricket match: {ta} vs {tb} at {venue} ({m['match_type']}, Womens={m['is_womens']}).
    Return strictly JSON with these keys:
    {{
      "chase_boost_pct": "+5.5%",
      "weather_line": "Local Evening | Rain 15% | Cloud 40% | Dew 19.5°C (Moderate-High)",
      "dew_high": true,
      "needs_toss_more": "{tb}",
      "needs_toss_reason": "concise 1-sentence reason why this team needs toss more",
      "cap_a": "Captain A (Last-5 Toss: 3/5, Pref: Bowl) — 1-line tactical DNA",
      "cap_b": "Captain B (Last-5 Toss: 2/5, Pref: Bowl) — 1-line tactical DNA",
      "par_mean": 168.0,
      "par_std": 14.5,
      "pp_avg": 48.0,
      "chase_bias_pct": 58.0,
      "pitch_note": "concise 1-sentence pitch & dew behavior",
      "model_prob_a": 54.0,
      "if_a_wins_call": "Bowl First (68%)",
      "if_a_wins_prob": 59.5,
      "if_a_wins_note": "1-sentence tactical advantage",
      "if_b_wins_call": "Bowl First (70%)",
      "if_b_wins_prob": 53.0,
      "if_b_wins_note": "1-sentence tactical advantage",
      "highest_toss_need": "{tb}",
      "expected_call": "Bowl First (70%)"
    }}
    """
    ai = ask_gemini_json(prompt)

    par_mean = float(ai.get("par_mean", 152.0 if m["is_womens"] else 168.0))
    par_std = float(ai.get("par_std", 13.5 if m["is_womens"] else 15.0))
    pp_avg = float(ai.get("pp_avg", round(par_mean * 0.29, 1)))
    chase_bias = float(ai.get("chase_bias_pct", 56.0))
    model_a = float(ai.get("model_prob_a", 52.0))
    model_b = round(100.0 - model_a, 2)

    # Save baseline for Session 2, 3, 4
    m["cached_pre_toss"] = {
        "par_mean": par_mean,
        "par_std": par_std,
        "dew_high": bool(ai.get("dew_high", False)),
        "model_a": model_a,
        "model_b": model_b,
        "odds_a": odds_a,
        "odds_b": odds_b,
        "if_a_wins_prob": float(ai.get("if_a_wins_prob", model_a + 4.0)),
        "if_b_wins_prob": float(ai.get("if_b_wins_prob", model_b + 4.0))
    }

    # Fair odds for conditional scenarios
    fair_a_win_toss = round(100.0 / max(m["cached_pre_toss"]["if_a_wins_prob"], 1.0), 2)
    fair_b_win_toss = round(100.0 / max(m["cached_pre_toss"]["if_b_wins_prob"], 1.0), 2)

    # Market Math Block
    if odds_a and odds_b and true_a is not None:
        edge_a = round(model_a - true_a, 2)
        edge_b = round(model_b - true_b, 2)
        best_side = ta if edge_a >= edge_b else tb
        best_prob = model_a if edge_a >= edge_b else model_b
        best_odds = odds_a if edge_a >= edge_b else odds_b
        best_edge = max(edge_a, edge_b)
        ev_pct, kelly_u, _ = calc_ev_and_kelly(best_prob, best_odds)

        market_block = (
            f"• <b>Raw Odds:</b> {esc(ta)} <code>{odds_a}</code> vs {esc(tb)} <code>{odds_b}</code> (Vig: <code>{vig}%</code>)\n"
            f"• <b>No-Vig True Prob:</b> {esc(ta)} <code>{true_a}%</code> vs {esc(tb)} <code>{true_b}%</code>\n"
            f"• <b>Model Fair Prob:</b> {esc(ta)} <code>{model_a}%</code> vs {esc(tb)} <code>{model_b}%</code>\n"
            f"• <b>Quant Edge ({esc(best_side)}):</b> <code>{best_edge:+.2f}%</code> | <b>+EV:</b> <code>{ev_pct:+.2f}%</code>"
        )
        stake_note = f"<code>{kelly_u:.2f}u</code> on {esc(best_side)}" if ev_pct >= 4.0 else "<code>0.00u</code> (Wait for Toss)"
    else:
        market_block = "• <b>Market Status:</b> <code>Market Unopened</code> <i>(No Verified Public/Sharp Flow Yet — Quant Baseline Active)</i>"
        stake_note = "<code>0.00u</code> (Wait for Toss)"

    msg = (
        f"🏏 <b>{esc(m['match_type'])} • PRE-TOSS QUANT BLUEPRINT</b>\n"
        f"🔵 <b>{esc(ta)}</b> vs 🟠 <b>{esc(tb)}</b>\n"
        f"🏟️ <i>{esc(venue)}</i>\n"
        f"🕒 Match: <code>{st_str}</code> | Toss (<code>{offset_label}</code>): <code>{tt_str}</code> | Alert: <code>{pt_str} BDT</code>\n\n"
        f"——— 🪙 <b>TOSS STRATEGY, STAKES &amp; SCENARIOS</b> ———\n"
        f"<b>1. Toss Impact &amp; Dependency:</b> Chase Boost (<code>{esc(ai.get('chase_boost_pct', '+5.0%'))}</code>) "
        f"<i>({esc(ai.get('weather_line', 'Clear Evening Conditions'))})</i> | "
        f"<b>Needs Toss More: {esc(ai.get('needs_toss_more', tb))}</b> — <i>{esc(ai.get('needs_toss_reason', 'Conditions favor chasing team under lights.'))}</i>\n\n"
        f"<b>2. Formation &amp; Captaincy DNA:</b>\n"
        f"• 🔵 <b>{esc(ta)}:</b> <i>{esc(ai.get('cap_a', 'Balanced top-order & pace attack.'))}</i>\n"
        f"• 🟠 <b>{esc(tb)}:</b> <i>{esc(ai.get('cap_b', 'Aggressive powerplay & spin depth.'))}</i>\n\n"
        f"<b>3. Pitch History, Venue Par &amp; Table Stakes:</b>\n"
        f"• <b>Venue Math (μ ± σ):</b> Par 1st Inn <code>{par_mean} ± {par_std}</code> | 6-Ov PP Avg: <code>{pp_avg}</code> | Chase Bias: <code>{chase_bias}%</code>\n"
        f"• <b>Surface Note:</b> <i>{esc(ai.get('pitch_note', 'True bounce early; dew plays key role in second half.'))}</i>\n\n"
        f"<b>4. Conditional Scenarios (If–Then Blueprint):</b>\n"
        f"• <b>If 🔵 {esc(ta)} wins → {esc(ai.get('if_a_wins_call', 'Bowl First (65%)'))}:</b> "
        f"<i>{esc(ai.get('if_a_wins_note', 'Maximizes early movement.'))}</i> Win prob shifts to <b><code>{m['cached_pre_toss']['if_a_wins_prob']}%</code></b> <i>(Fair Odds: <code>{fair_a_win_toss}</code>)</i>.\n"
        f"• <b>If 🟠 {esc(tb)} wins → {esc(ai.get('if_b_wins_call', 'Bowl First (68%)'))}:</b> "
        f"<i>{esc(ai.get('if_b_wins_note', 'Avoids bowling with wet ball.'))}</i> Win prob shifts to <b><code>{m['cached_pre_toss']['if_b_wins_prob']}%</code></b> <i>(Fair Odds: <code>{fair_b_win_toss}</code>)</i>.\n\n"
        f"<b>5. De-Vigged Market Math &amp; Sentiment (H2H):</b>\n"
        f"{market_block}\n\n"
        f"<blockquote>🪙 <b>Pre-Toss Blueprint:</b> Coin Flip: <code>50/50 (Skip)</code> | "
        f"<b>Highest Toss Need: {esc(ai.get('highest_toss_need', tb))}</b> | "
        f"<b>Expected Call: {esc(ai.get('expected_call', 'Bowl First (68%)'))}</b> | "
        f"<b>0.25x Kelly Stake:</b> {stake_note}</blockquote>"
    )

    if send_telegram(msg):
        m["sent"]["pre_toss"] = True


def run_session2_confirmed_toss(m, state):
    live = fetch_single_match_live(m["id"], state)
    toss_winner = live.get("tossWinner", "")
    toss_choice = live.get("tossChoice", "")
    status_txt = live.get("status", "")

    # If toss not yet updated on CricAPI, wait for next 5-min run (until start_time + 10m)
    if not toss_winner and "won the toss" not in status_txt.lower():
        now_bdt = datetime.now(BDT)
        start_bdt = datetime.fromisoformat(m["start_bdt"])
        if now_bdt < start_bdt + timedelta(minutes=8):
            print(f"⏳ Toss pending for {m['name']}, will retry next tick.")
            return
        toss_str = status_txt or "Toss Confirmed (Check Live Feed)"
    else:
        toss_str = f"{toss_winner} won the toss & elected to {toss_choice.upper()}" if toss_winner else status_txt

    ta, tb = m["team_a"], m["team_b"]
    cache = m.get("cached_pre_toss", {})
    par_mean = cache.get("par_mean", 164.0)
    par_std = cache.get("par_std", 14.5)
    dew_high = cache.get("dew_high", False)

    # Determine batting first team
    bat_first = ta
    if toss_winner:
        if (ta.lower() in toss_winner.lower() and "bowl" in toss_choice.lower()) or \
           (tb.lower() in toss_winner.lower() and "bat" in toss_choice.lower()):
            bat_first = tb

    # Post-toss model adjustment
    prob_a = cache.get("if_a_wins_prob", 55.0) if (toss_winner and ta.lower() in toss_winner.lower()) else cache.get("model_a", 52.0)
    if toss_winner and tb.lower() in toss_winner.lower():
        prob_b = cache.get("if_b_wins_prob", 55.0)
        prob_a = round(100.0 - prob_b, 2)
    else:
        prob_b = round(100.0 - prob_a, 2)

    odds_a, odds_b = fetch_h2h_odds(ta, tb)
    if not odds_a:
        odds_a, odds_b = cache.get("odds_a"), cache.get("odds_b")

    true_a, true_b, _ = devig_odds(odds_a, odds_b)
    if odds_a and odds_b and true_a:
        edge_a = round(prob_a - true_a, 2)
        edge_b = round(prob_b - true_b, 2)
        fav_team = ta if prob_a >= prob_b else tb
        fav_prob = max(prob_a, prob_b)
        fav_edge = edge_a if prob_a >= prob_b else edge_b
        odds_line = (
            f"• <b>Latest Market Odds:</b> {esc(ta)} <code>{odds_a}</code> (<code>{true_a}%</code>) vs {esc(tb)} <code>{odds_b}</code> (<code>{true_b}%</code>)\n"
            f"• <b>Model Fair Prob:</b> {esc(ta)} <code>{prob_a}%</code> vs {esc(tb)} <code>{prob_b}%</code> (<b>Net Edge: <code>{fav_edge:+.2f}%</code></b>)"
        )
    else:
        fav_team = ta if prob_a >= prob_b else tb
        fav_prob = max(prob_a, prob_b)
        fav_edge = round(fav_prob - 50.0, 2)
        odds_line = (
            f"• <b>Market Odds Status:</b> <code>Market Unopened</code> (Using Fair Value Model)\n"
            f"• <b>Model Fair Prob:</b> {esc(ta)} <code>{prob_a}%</code> vs {esc(tb)} <code>{prob_b}%</code>"
        )

    sessions = build_session_projections(par_mean, par_std, is_chasing=False, dew_high=dew_high)
    pp6, ov10, ov20 = sessions["pp6"], sessions["ov10"], sessions["ov20"]

    # Strict A+ Sniper qualification (>= 80% confidence OR >= +7% edge with >= 62% prob)
    sniper_lines = []
    if fav_prob >= 80.0 or (fav_edge >= 7.0 and fav_prob >= 62.0):
        conf = int(min(round(fav_prob + max(fav_edge, 0)), 92))
        pick_label = f"{fav_team} Match Winner"
        sniper_lines.append(f"🎯 <b>A+ SNIPER: {esc(pick_label)} ({conf}%)</b>")
        m["sniper_picks"].append({"label": pick_label, "conf": conf, "type": "winner", "team": fav_team})

    # Check if 6-over PP session has strong >= 66% directional edge
    if pp6["under_p"] >= 66:
        pick_label = f"{bat_first} 6-Over PP UNDER {pp6['line']}"
        sniper_lines.append(f"🎯 <b>A+ SESSION: {esc(pick_label)} ({pp6['under_p']}%)</b>")
        m["sniper_picks"].append({"label": pick_label, "conf": pp6["under_p"], "type": "pp_under", "line": pp6["line"]})
    elif pp6["over_p"] >= 66:
        pick_label = f"{bat_first} 6-Over PP OVER {pp6['line']}"
        sniper_lines.append(f"🎯 <b>A+ SESSION: {esc(pick_label)} ({pp6['over_p']}%)</b>")
        m["sniper_picks"].append({"label": pick_label, "conf": pp6["over_p"], "type": "pp_over", "line": pp6["line"]})

    if not sniper_lines:
        sniper_lines.append("👀 <b>WATCHLIST / NO A+ SNIPER EDGE YET</b> — <i>Wait for 6-Over Powerplay live entry.</i>")

    msg = (
        f"🪙 <b>CONFIRMED TOSS &amp; 1ST INN SESSIONS</b>\n"
        f"🔵 <b>{esc(ta)}</b> vs 🟠 <b>{esc(tb)}</b>\n"
        f"📢 <b>Toss:</b> <code>{esc(toss_str)}</code>\n\n"
        f"📊 <b>MARKET vs FAIR VALUE</b>\n"
        f"{odds_line}\n\n"
        f"🔢 <b>1ST INNINGS SESSIONS ({esc(bat_first)} Batting)</b>\n"
        f"• <b>6-Over PP:</b> Line <code>{pp6['line']}</code> | Proj: <code>{pp6['proj_low']}–{pp6['proj_high']}</code> | Over <code>{pp6['over_p']}%</code> / Under <code>{pp6['under_p']}%</code>\n"
        f"• <b>10-Over:</b> Line <code>{ov10['line']}</code> | Proj: <code>{ov10['proj_low']}–{ov10['proj_high']}</code> | Over <code>{ov10['over_p']}%</code> / Under <code>{ov10['under_p']}%</code>\n"
        f"• <b>20-Over Total:</b> Line <code>{ov20['line']}</code> | Proj: <code>{ov20['proj_low']}–{ov20['proj_high']}</code> | Over <code>{ov20['over_p']}%</code> / Under <code>{ov20['under_p']}%</code>\n\n"
        f"<blockquote>{chr(10).join(sniper_lines)}</blockquote>"
    )

    if send_telegram(msg):
        m["sent"]["toss_sessions"] = True


def run_session3_pp_review(m, state):
    live = fetch_single_match_live(m["id"], state)
    scores = live.get("score", [])
    if not scores:
        return

    inn1 = scores[0]
    runs = int(inn1.get("r", 0))
    wkts = int(inn1.get("w", 0))
    overs = float(inn1.get("o", 0.0))

    # Wait until at least 5.4 overs completed
    if overs < 5.4:
        return

    crr = round(runs / max(overs, 0.1), 2)
    cache = m.get("cached_pre_toss", {})
    par_mean = cache.get("par_mean", 164.0)
    par_std = cache.get("par_std", 14.5)

    # Recalculate 20-over projection based on actual PP runs & wickets lost
    rem_overs = max(20.0 - overs, 0.0)
    wicket_penalty = wkts * 6.5
    revised_20 = round(runs + (rem_overs * max(crr * 0.96, 6.2)) - wicket_penalty, 1)
    revised_15 = round(runs + (max(15.0 - overs, 0.0) * max(crr * 0.94, 6.0)) - (wkts * 3.5), 1)

    line_15 = math.floor(par_mean * 0.72) + 0.5
    line_20 = math.floor(par_mean) + 0.5

    u15 = min(max(round(normal_cdf(line_15, revised_15, par_std * 0.75) * 100), 18), 82)
    u20 = min(max(round(normal_cdf(line_20, revised_20, par_std * 0.85) * 100), 18), 82)

    m["cached_pre_toss"]["actual_pp1"] = f"{runs}/{wkts} ({overs} ov)"
    m["cached_pre_toss"]["actual_pp1_runs"] = runs

    msg = (
        f"⚡ <b>1ST INN POWERPLAY REVIEW ({overs} OV)</b>\n"
        f"🏏 <b>{esc(inn1.get('inning', m['team_a']))}:</b> <code>{runs}/{wkts}</code> • CRR: <code>{crr}</code>\n\n"
        f"🔄 <b>REVISED 1ST INNINGS SESSIONS</b>\n"
        f"• <b>15-Over Session:</b> Line <code>{line_15}</code> | Proj: <code>{int(revised_15-4)}–{int(revised_15+4)}</code> | Over <code>{100-u15}%</code> / Under <code>{u15}%</code>\n"
        f"• <b>20-Over Total:</b> Line <code>{line_20}</code> | Proj: <code>{int(revised_20-6)}–{int(revised_20+6)}</code> | Over <code>{100-u20}%</code> / Under <code>{u20}%</code>\n\n"
        f"<blockquote>📈 <b>Live Momentum Note:</b> At <code>{runs}/{wkts}</code> after <code>{overs}</code> overs, projected par shifts to <code>{int(revised_20)}</code>. Adjust live positions accordingly.</blockquote>"
    )

    if send_telegram(msg):
        m["sent"]["pp_review"] = True


def run_session4_innings_break(m, state):
    live = fetch_single_match_live(m["id"], state)
    scores = live.get("score", [])
    status = live.get("status", "").lower()

    # Check if 1st innings is completed (either 2nd innings started or innings break in status)
    if not scores:
        return
    inn1 = scores[0]
    inn1_overs = float(inn1.get("o", 0.0))
    inn1_wkts = int(inn1.get("w", 0))
    if len(scores) < 2 and inn1_overs < 19.5 and inn1_wkts < 10 and "innings break" not in status:
        return

    first_score = int(inn1.get("r", 0))
    target = first_score + 1
    req_rr = round(target / 20.0, 2)

    cache = m.get("cached_pre_toss", {})
    par_mean = cache.get("par_mean", 164.0)
    par_std = cache.get("par_std", 14.5)
    dew_high = cache.get("dew_high", False)

    # Identify chasing team
    inn1_name = inn1.get("inning", "").lower()
    chasing_team = m["team_b"] if m["team_a"].lower().split()[0] in inn1_name else m["team_a"]
    defending_team = m["team_a"] if chasing_team == m["team_b"] else m["team_b"]

    # Chasing win probability based on target vs venue par + dew
    diff_from_par = (par_mean + (5.0 if dew_high else 0.0)) - target
    chase_win_prob = round(min(max(50.0 + (diff_from_par * 1.45), 15.0), 86.0), 1)
    def_win_prob = round(100.0 - chase_win_prob, 1)

    chase_sessions = build_session_projections(par_mean, par_std, is_chasing=True, target=target, dew_high=dew_high)
    pp6, ov10, ov20 = chase_sessions["pp6"], chase_sessions["ov10"], chase_sessions["ov20"]

    sniper_call = "👀 <b>WATCHLIST:</b> Wait for 2nd Inn Over 2.0 before entering chase market."
    if chase_win_prob >= 76.0:
        pick = f"{chasing_team} Chase Win"
        sniper_call = f"🎯 <b>A+ CHASE SNIPER: {esc(pick)} ({chase_win_prob}%)</b> — <i>Below-par target {'+ heavy dew' if dew_high else ''}.</i>"
        m["sniper_picks"].append({"label": pick, "conf": int(chase_win_prob), "type": "winner", "team": chasing_team})
    elif def_win_prob >= 76.0:
        pick = f"{defending_team} Defend Win"
        sniper_call = f"🎯 <b>A+ DEFEND SNIPER: {esc(pick)} ({def_win_prob}%)</b> — <i>Above-par scoreboard pressure.</i>"
        m["sniper_picks"].append({"label": pick, "conf": int(def_win_prob), "type": "winner", "team": defending_team})

    msg = (
        f"🌗 <b>INNINGS BREAK &amp; FULL 2ND INN SETUP</b>\n"
        f"🎯 <b>Target:</b> <code>{target}</code> in 20 Overs (Req RR: <code>{req_rr}</code>)\n"
        f"📊 <b>Win Prob:</b> {esc(chasing_team)} <code>{chase_win_prob}%</code> vs {esc(defending_team)} <code>{def_win_prob}%</code>\n\n"
        f"🔢 <b>2ND INNINGS SESSIONS ({esc(chasing_team)} Chasing)</b>\n"
        f"• <b>6-Over Chase PP:</b> Line <code>{pp6['line']}</code> | Proj: <code>{pp6['proj_low']}–{pp6['proj_high']}</code> | Over <code>{pp6['over_p']}%</code> / Under <code>{pp6['under_p']}%</code>\n"
        f"• <b>10-Over Chase:</b> Line <code>{ov10['line']}</code> | Proj: <code>{ov10['proj_low']}–{ov10['proj_high']}</code> | Over <code>{ov10['over_p']}%</code> / Under <code>{ov10['under_p']}%</code>\n"
        f"• <b>20-Over Chase Total:</b> Line <code>{ov20['line']}</code> | Proj: <code>{ov20['proj_low']}–{ov20['proj_high']}</code> | Over <code>{ov20['over_p']}%</code> / Under <code>{ov20['under_p']}%</code>\n\n"
        f"<blockquote>{sniper_call}</blockquote>"
    )

    if send_telegram(msg):
        m["sent"]["innings_break"] = True


def run_session5_post_match_review(m, state):
    live = fetch_single_match_live(m["id"], state)
    status = live.get("status", "")
    match_ended = live.get("matchEnded", False) or ("won by" in status.lower()) or ("no result" in status.lower()) or ("tied" in status.lower())

    if not match_ended:
        return

    scores = live.get("score", [])
    score_summary = " | ".join([f"{s.get('inning','')}: {s.get('r',0)}/{s.get('w',0)} ({s.get('o',0)})" for s in scores])
    picks = m.get("sniper_picks", [])
    record = state.setdefault("paper_record", {"wins": 0, "losses": 0, "net_units": 0.0})

    review_lines = []
    if not picks:
        review_lines.append("• <i>No A+ Sniper trades were triggered in this match (Disciplined Skip).</i>")
    else:
        pp1_runs = m.get("cached_pre_toss", {}).get("actual_pp1_runs")
        for idx, p in enumerate(picks, 1):
            won = None
            if p["type"] == "winner":
                if p["team"].lower().split()[0] in status.lower() and "won" in status.lower():
                    won = True
                elif "won" in status.lower():
                    won = False
            elif p["type"] == "pp_under" and pp1_runs is not None:
                won = pp1_runs < p["line"]
            elif p["type"] == "pp_over" and pp1_runs is not None:
                won = pp1_runs > p["line"]

            if won is True:
                record["wins"] += 1
                record["net_units"] = round(record["net_units"] + 0.85, 2)
                review_lines.append(f"{idx}. <b>{esc(p['label'])} ({p['conf']}%)</b> → ✅ <b>WON</b>")
            elif won is False:
                record["losses"] += 1
                record["net_units"] = round(record["net_units"] - 1.00, 2)
                review_lines.append(f"{idx}. <b>{esc(p['label'])} ({p['conf']}%)</b> → ❌ <b>LOST</b>")
            else:
                review_lines.append(f"{idx}. <b>{esc(p['label'])} ({p['conf']}%)</b> → ⚪ <b>VOID / UNGRADED</b>")

    total_graded = record["wins"] + record["losses"]
    acc = round((record["wins"] / total_graded) * 100.0, 1) if total_graded > 0 else 0.0

    msg = (
        f"🏁 <b>MATCH ENDED &amp; A+ SNIPER REVIEW</b>\n"
        f"🔵 <b>{esc(m['team_a'])}</b> vs 🟠 <b>{esc(m['team_b'])}</b>\n"
        f"🏆 <b>Result:</b> <code>{esc(status)}</code>\n"
        f"📊 <b>Scorecard:</b> <i>{esc(score_summary or 'Final Scorecard Logged')}</i>\n\n"
        f"📋 <b>THIS MATCH'S A+ SNIPER CALLS</b>\n"
        f"{chr(10).join(review_lines)}\n\n"
        f"<blockquote>📈 <b>OVERALL PAPER RECORD:</b> <code>{record['wins']}W - {record['losses']}L</code> | "
        f"Accuracy: <code>{acc}%</code> | Net: <code>{record['net_units']:+.2f}u</code></blockquote>"
    )

    if send_telegram(msg):
        m["sent"]["post_match"] = True


# ==========================================
# 7. MAIN EXECUTION & ZERO-CREDIT GATEKEEPER
# ==========================================
def main():
    now_bdt = datetime.now(BDT)
    today_str = now_bdt.strftime("%Y-%m-%d")
    state = load_state()

    # STEP 1: Midnight-to-Midnight BDT Daily Fetch (Runs ONCE per BDT day)
    if state.get("schedule_date") != today_str:
        print(f"🌙 New BDT Day ({today_str}): Fetching 24-hour match schedule...")
        state["credits_used_today"] = 0
        matches = fetch_daily_matches_bdt(today_str, state)
        state["schedule_date"] = today_str
        state["matches"] = matches
        save_state(state)
        send_session0_daily_schedule(today_str, matches, state)
        return

    matches = state.get("matches", [])
    if not matches:
        if IS_MANUAL_RUN:
            send_telegram(f"ℹ️ <b>Manual Check ({today_str}):</b> No matches scheduled today. Credits used: <code>{state.get('credits_used_today',0)}/100</code>")
        print("💤 No matches today. 0 credits used.")
        return

    # STEP 2: Zero-Credit Gatekeeper — Check if ANY match is inside an active window
    any_action_taken = False
    for m in matches:
        sent = m["sent"]
        pre_toss_dt = datetime.fromisoformat(m["pre_toss_bdt"])
        toss_dt = datetime.fromisoformat(m["toss_bdt"])
        start_dt = datetime.fromisoformat(m["start_bdt"])

        # Window 1: Pre-Toss (20 mins before toss until 5 mins before toss)
        if not sent["pre_toss"] and (pre_toss_dt - timedelta(minutes=3) <= now_bdt <= toss_dt - timedelta(minutes=4)):
            print(f"🚀 Triggering Session 1 (Pre-Toss) for {m['name']}")
            run_session1_pre_toss(m, state)
            any_action_taken = True

        # Window 2: Confirmed Toss & 1st Inn Sessions (from toss time until start + 12m)
        elif not sent["toss_sessions"] and (toss_dt <= now_bdt <= start_dt + timedelta(minutes=12)):
            print(f"🚀 Triggering Session 2 (Confirmed Toss) for {m['name']}")
            run_session2_confirmed_toss(m, state)
            any_action_taken = True

        # Window 3: 1st Inn 6-Over Powerplay Review (start + 26m to start + 55m)
        elif not sent["pp_review"] and (start_dt + timedelta(minutes=26) <= now_bdt <= start_dt + timedelta(minutes=55)):
            print(f"🚀 Triggering Session 3 (PP Review) for {m['name']}")
            run_session3_pp_review(m, state)
            any_action_taken = True

        # Window 4: Innings Break & 2nd Inn Full Setup (start + 80m to start + 135m)
        elif not sent["innings_break"] and (start_dt + timedelta(minutes=80) <= now_bdt <= start_dt + timedelta(minutes=135)):
            print(f"🚀 Triggering Session 4 (Innings Break) for {m['name']}")
            run_session4_innings_break(m, state)
            any_action_taken = True

        # Window 5: Post-Match A+ Sniper Review (start + 175m to start + 330m)
        elif not sent["post_match"] and (start_dt + timedelta(minutes=175) <= now_bdt <= start_dt + timedelta(minutes=330)):
            print(f"🚀 Triggering Session 5 (Post-Match Review) for {m['name']}")
            run_session5_post_match_review(m, state)
            any_action_taken = True

    if any_action_taken:
        save_state(state)
    else:
        print(f"💤 Gatekeeper Active ({now_bdt.strftime('%I:%M %p BDT')}): Outside active windows. 0 API credits spent.")
        if IS_MANUAL_RUN:
            # Show clean status summary when user manually triggers workflow
            upcoming = [
                f"• <b>{esc(x['team_a'])} vs {esc(x['team_b'])}</b> — Pre-Toss: <code>{datetime.fromisoformat(x['pre_toss_bdt']).strftime('%I:%M %p')}</code> | Toss: <code>{datetime.fromisoformat(x['toss_bdt']).strftime('%I:%M %p')}</code>"
                for x in matches if not x["sent"]["post_match"]
            ]
            status_body = "\n".join(upcoming) if upcoming else "All today's matches completed."
            send_telegram(
                f"✅ <b>Manual Workflow Check ({now_bdt.strftime('%I:%M %p BDT')})</b>\n"
                f"💳 Credits Used Today: <code>{state.get('credits_used_today',0)}/100</code>\n\n"
                f"<b>Today's Locked Radar:</b>\n{status_body}"
            )


if __name__ == "__main__":
    main()