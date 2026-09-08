import os
import html
import time
import requests
import yfinance as yf
import pandas as pd
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
PROXIMITY_PCT = 3.0  # Setups within 3% below ORH
MAX_WORKERS = 4      # Kept at 4 to prevent Yahoo Finance HTTP 429 rate limits

def load_tickers():
    if os.path.exists("tickers.txt"):
        with open("tickers.txt", "r") as f:
            return [line.strip() + ".NS" for line in f.readlines() if line.strip()]
    return ["MANALIPETC.NS", "TNPETRO.NS", "DIXON.NS", "LUPIN.NS"]

def init_yfinance_crumb():
    """Warms up the crumb cache synchronously once to avoid parallel thread 429s."""
    print("⏳ Initializing Yahoo Finance session...")
    try:
        dummy = yf.Ticker("^NSEI")
        dummy.history(period="1d")
        time.sleep(1)
    except Exception:
        pass

def send_telegram(text):
    """Sends message with HTML formatting and falls back to plain text on error."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ Telegram credentials missing! Check TELEGRAM_TOKEN and CHAT_ID secrets.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            return True

        # If Telegram rejected HTML parsing, retry as plain text fallback
        print(f"⚠️ Telegram HTML error ({res.status_code}): {res.text}. Retrying plain text...")
        import re
        plain = re.sub(r'<a href="[^"]*">([^<]*)</a>', r'\1', text)
        plain = plain.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("&amp;", "&")
        
        fallback_payload = {
            "chat_id": CHAT_ID,
            "text": plain,
            "disable_web_page_preview": True
        }
        fb_res = requests.post(url, json=fallback_payload, timeout=15)
        if fb_res.status_code == 200:
            return True
        else:
            print(f"❌ Fallback also failed ({fb_res.status_code}): {fb_res.text}")
            return False

    except Exception as e:
        print(f"❌ Network failure sending Telegram alert: {e}")
        return False

def send_telegram_chunks(full_msg):
    """Splits messages longer than Telegram's 4096 character limit cleanly by line."""
    if len(full_msg) <= 3900:
        if send_telegram(full_msg):
            print("✅ Alert report successfully delivered to Telegram!")
        return

    lines = full_msg.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3900:
            send_telegram(chunk)
            chunk = line + "\n"
        else:
            chunk += line + "\n"
    if chunk.strip():
        send_telegram(chunk)
    print("✅ Alert report (chunked) successfully delivered to Telegram!")

def process_ticker(t):
    """Processes a single ticker and returns structured data."""
    try:
        # Small jitter to prevent spamming Yahoo servers simultaneously
        time.sleep(0.05)
        ticker_obj = yf.Ticker(t)

        # --- STAGE 1: Daily Trend & Momentum Check ---
        d = ticker_obj.history(period="6mo", interval="1d")
        if d.empty or len(d) < 65:
            return None

        d = d.dropna(subset=['Close', 'High', 'Low'])
        if len(d) < 65:
            return None

        close_d = float(d['Close'].iloc[-1])
        high_6m = float(d['High'].max())

        # EMA Trend alignment
        ema10 = float(d['Close'].ewm(span=10, adjust=False).mean().iloc[-1])
        ema21 = float(d['Close'].ewm(span=21, adjust=False).mean().iloc[-1])
        sma50 = float(d['Close'].rolling(50).mean().iloc[-1])

        if not (close_d > ema10 and ema10 > ema21 and ema21 > sma50):
            return None

        # 3-Month Growth > 30%
        three_mo_ago = float(d['Close'].iloc[-64])
        three_mo_perf = ((close_d - three_mo_ago) / three_mo_ago) * 100
        if three_mo_perf < 30.0:
            return None

        # --- STAGE 2: Hourly Data ---
        h = ticker_obj.history(period="5d", interval="1h")
        if h.empty or len(h) < 10:
            return None
        h = h.dropna(subset=['Close', 'High', 'Low'])

        # Fix for Yahoo Finance daily candle lag
        last_d_date = d.index[-1].date()
        last_h_date = h.index[-1].date()

        if last_h_date > last_d_date:
            orh_line = float(d['High'].iloc[-1])
            curr_price = float(h['Close'].iloc[-1])
        else:
            orh_line = float(d['High'].iloc[-2])
            curr_price = float(d['Close'].iloc[-1])

        dist_pct = ((orh_line - curr_price) / orh_line) * 100
        dist_to_6m_high = ((high_6m - curr_price) / high_6m) * 100
        clean_ticker = t.replace(".NS", "")

        # --- BREAKOUT & VOLUME METRICS ---
        h_vol_ma = h['Volume'].rolling(20).mean()

        close_h_1 = float(h['Close'].iloc[-1])
        vol_h_1 = float(h['Volume'].iloc[-1])
        avg_h_vol_1 = float(h_vol_ma.iloc[-1]) if not pd.isna(h_vol_ma.iloc[-1]) and h_vol_ma.iloc[-1] > 0 else 1.0

        close_h_2 = float(h['Close'].iloc[-2])
        vol_h_2 = float(h['Volume'].iloc[-2])
        avg_h_vol_2 = float(h_vol_ma.iloc[-2]) if not pd.isna(h_vol_ma.iloc[-2]) and h_vol_ma.iloc[-2] > 0 else 1.0

        rvol = vol_h_1 / avg_h_vol_1

        breakout_c1 = (close_h_1 >= orh_line) and (vol_h_1 > avg_h_vol_1 * 1.1)
        breakout_c2 = (close_h_2 >= orh_line) and (vol_h_2 > avg_h_vol_2 * 1.1)
        is_breakout = breakout_c1 or breakout_c2

        # Setup criteria: strictly below resistance and within proximity window
        is_setup = (curr_price < orh_line) and (0 < dist_pct <= PROXIMITY_PCT)

        # Count how many hourly candles tested near resistance (within 1.5% of ORH)
        resistance_tests = int((h['High'] >= (orh_line * 0.985)).sum())

        # Setup scoring formula (higher is better):
        # Rewards elevated RVOL, tests at resistance, and proximity to 6M high / ORH
        setup_score = (rvol * 3.0) + (resistance_tests * 1.5) - (dist_pct * 2.0) - (dist_to_6m_high * 0.5)

        tv_url = f"https://in.tradingview.com/chart/?symbol=NSE:{quote(clean_ticker)}"

        base_data = {
            "ticker": clean_ticker,
            "safe_ticker": html.escape(clean_ticker),
            "price": curr_price,
            "orh": orh_line,
            "dist": dist_pct,
            "url": tv_url,
            "rvol": rvol,
            "tests": resistance_tests,
            "score": setup_score
        }

        if is_breakout:
            base_data["type"] = "BREAKOUT"
            return base_data
        elif is_setup:
            base_data["type"] = "SETUP"
            return base_data

        return None

    except Exception:
        return None

def scan():
    init_yfinance_crumb()
    tickers = load_tickers()
    breakouts = []
    setups = []
    total = len(tickers)

    print(f"🚀 Starting multi-threaded scan across {total} tickers...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_ticker, t): t for t in tickers}

        for future in as_completed(futures):
            res = future.result()
            if res:
                if res["type"] == "BREAKOUT":
                    breakouts.append(res)
                elif res["type"] == "SETUP":
                    setups.append(res)

    if not breakouts and not setups:
        print("🏁 Scan complete. No active breakouts or setups found.")
        return

    # Sort breakouts by proximity and setups by breakout probability score
    breakouts = sorted(breakouts, key=lambda x: x['dist'])
    setups = sorted(setups, key=lambda x: x['score'], reverse=True)

    top_3_setups = setups[:3]
    other_setups = setups[3:]

    # IST timestamp
    ist_time = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p")

    msg = "<b>🕒 Qullamaggie Scan Report</b>\n"
    msg += f"<i>{ist_time}</i>\n\n"

    if breakouts:
        msg += "<b>🚀 ACTIVE BREAKOUTS</b>\n"
        for b in breakouts:
            msg += f"• <a href='{b['url']}'>{b['safe_ticker']}</a>: ₹{b['price']:.2f} (ORH: ₹{b['orh']:.2f})\n"
        msg += "\n"

    if top_3_setups:
        msg += "<b>🌟 TOP 3 PRIME SETUPS</b> <i>(Volume + Resistance Tests)</i>\n"
        for s in top_3_setups:
            msg += (
                f"• <a href='{s['url']}'>{s['safe_ticker']}</a>: ₹{s['price']:.2f} "
                f"| -{s['dist']:.1f}% to ORH | RVOL: {s['rvol']:.1f}x | Tests: {s['tests']}x\n"
            )
        msg += "\n"

    if other_setups:
        msg += f"<b>👀 OTHER SETUPS (Within {PROXIMITY_PCT}%)</b>\n"
        for s in other_setups:
            msg += f"• <a href='{s['url']}'>{s['safe_ticker']}</a>: ₹{s['price']:.2f} | -{s['dist']:.1f}% to ORH\n"

    send_telegram_chunks(msg)

if __name__ == "__main__":
    scan()
