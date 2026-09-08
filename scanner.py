import os
import html
import time
import re
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
        print("⚠️ Telegram credentials missing! Skipping Telegram notification.")
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

        # Fallback to plain text if HTML tags cause a 400 Bad Request
        print(f"⚠️ Telegram HTML error ({res.status_code}): {res.text}. Retrying plain text...")
        plain = re.sub(r'<a href="[^"]*">([^<]*)</a>', r'\1', text)
        plain = plain.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("&amp;", "&")
        
        fallback_payload = {
            "chat_id": CHAT_ID,
            "text": plain,
            "disable_web_page_preview": True
        }
        fb_res = requests.post(url, json=fallback_payload, timeout=15)
        return fb_res.status_code == 200

    except Exception as e:
        print(f"❌ Network failure sending Telegram alert: {e}")
        return False

def send_telegram_chunks(full_msg):
    """Splits messages cleanly within Telegram's 4096 character limit."""
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

def generate_html_page(ist_time, fresh_breakouts, active_breakouts, top_3_setups, other_setups):
    """Builds a responsive, dark-mode index.html dashboard for GitHub Pages."""
    def render_list(items, extra_fields=False):
        if not items:
            return "<p class='empty'>No tickers detected.</p>"
        html_out = "<ul>"
        for item in items:
            ticker_link = f"<a href='{item['url']}' target='_blank'>{item['safe_ticker']}</a>"
            if extra_fields:
                html_out += (
                    f"<li>{ticker_link} &mdash; <strong>₹{item['price']:.2f}</strong> "
                    f"<span class='badge'>-{item['dist']:.1f}% to ORH</span> "
                    f"<span class='badge'>RVOL: {item['rvol']:.1f}x</span> "
                    f"<span class='badge'>Tests: {item['tests']}x</span></li>"
                )
            else:
                html_out += (
                    f"<li>{ticker_link} &mdash; <strong>₹{item['price']:.2f}</strong> "
                    f"<span class='badge'>ORH: ₹{item['orh']:.2f}</span></li>"
                )
        html_out += "</ul>"
        return html_out

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Qullamaggie Market Scanner</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background-color: #0e1117;
            color: #e6edf3;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            padding: 24px 16px;
            display: flex;
            justify-content: center;
        }}
        .card {{
            width: 100%;
            max-width: 680px;
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 24px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.5);
        }}
        h1 {{ font-size: 1.5rem; margin-bottom: 4px; color: #58a6ff; }}
        .timestamp {{ font-size: 0.85rem; color: #8b949e; margin-bottom: 24px; }}
        h2 {{
            font-size: 1.05rem;
            margin: 20px 0 10px 0;
            padding-bottom: 6px;
            border-bottom: 1px solid #21262d;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        ul {{ list-style: none; }}
        li {{
            padding: 10px 12px;
            margin-bottom: 8px;
            background: #0d1117;
            border: 1px solid #21262d;
            border-radius: 6px;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 8px;
        }}
        a {{ color: #58a6ff; text-decoration: none; font-weight: 600; }}
        a:hover {{ text-decoration: underline; }}
        .badge {{
            font-size: 0.75rem;
            background: #21262d;
            color: #c9d1d9;
            padding: 2px 8px;
            border-radius: 12px;
        }}
        .empty {{ color: #484f58; font-size: 0.9rem; font-style: italic; padding: 4px 0; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>📊 Qullamaggie Market Scan</h1>
        <div class="timestamp">Last Updated: {ist_time} (IST)</div>

        <h2>🚨 Fresh Breakouts (Last 1 Hour)</h2>
        {render_list(fresh_breakouts)}

        <h2>🚀 Active Breakouts</h2>
        {render_list(active_breakouts)}

        <h2>🌟 Top 3 Prime Setups (Immediate Surge Probability)</h2>
        {render_list(top_3_setups, extra_fields=True)}

        <h2>👀 Other Setups (Within {PROXIMITY_PCT}%)</h2>
        {render_list(other_setups, extra_fields=True)}
    </div>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("🌐 index.html generated successfully for GitHub Pages!")

def process_ticker(t):
    """Scans daily momentum, hourly levels, volume expansion, and resistance tests."""
    try:
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

        ema10 = float(d['Close'].ewm(span=10, adjust=False).mean().iloc[-1])
        ema21 = float(d['Close'].ewm(span=21, adjust=False).mean().iloc[-1])
        sma50 = float(d['Close'].rolling(50).mean().iloc[-1])

        if not (close_d > ema10 and ema10 > ema21 and ema21 > sma50):
            return None

        three_mo_ago = float(d['Close'].iloc[-64])
        if ((close_d - three_mo_ago) / three_mo_ago) * 100 < 30.0:
            return None

        # --- STAGE 2: Hourly Data ---
        h = ticker_obj.history(period="5d", interval="1h")
        if h.empty or len(h) < 10:
            return None
        h = h.dropna(subset=['Close', 'High', 'Low'])

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

        # --- BREAKOUT & RELATIVE VOLUME ---
        h_vol_ma = h['Volume'].rolling(20).mean()

        high_h_1 = float(h['High'].iloc[-1])
        vol_h_1 = float(h['Volume'].iloc[-1])
        avg_h_vol_1 = float(h_vol_ma.iloc[-1]) if not pd.isna(h_vol_ma.iloc[-1]) and h_vol_ma.iloc[-1] > 0 else 1.0

        high_h_2 = float(h['High'].iloc[-2])
        vol_h_2 = float(h['Volume'].iloc[-2])
        avg_h_vol_2 = float(h_vol_ma.iloc[-2]) if not pd.isna(h_vol_ma.iloc[-2]) and h_vol_ma.iloc[-2] > 0 else 1.0

        rvol = vol_h_1 / avg_h_vol_1

        is_fresh_breakout = (high_h_1 >= orh_line) and (vol_h_1 > avg_h_vol_1 * 1.1)
        is_active_breakout = (high_h_2 >= orh_line) and (vol_h_2 > avg_h_vol_2 * 1.1) and not is_fresh_breakout

        # Setup qualification & tests against resistance
        is_setup = (curr_price < orh_line) and (0 < dist_pct <= PROXIMITY_PCT)
        resistance_tests = int((h['High'] >= (orh_line * 0.985)).sum())

        # Immediate Surge Probability Ranking Formula
        surge_score = (rvol * 5.0) + (resistance_tests * 2.0) - (dist_pct * 3.0) - (dist_to_6m_high * 0.5)
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
            "score": surge_score
        }

        if is_fresh_breakout:
            base_data["type"] = "FRESH_BREAKOUT"
            return base_data
        elif is_active_breakout:
            base_data["type"] = "ACTIVE_BREAKOUT"
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
    fresh_breakouts, active_breakouts, setups = [], [], []

    print(f"🚀 Starting multi-threaded scan across {len(tickers)} tickers...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_ticker, t): t for t in tickers}
        for future in as_completed(futures):
            res = future.result()
            if res:
                if res["type"] == "FRESH_BREAKOUT":
                    fresh_breakouts.append(res)
                elif res["type"] == "ACTIVE_BREAKOUT":
                    active_breakouts.append(res)
                elif res["type"] == "SETUP":
                    setups.append(res)

    # Sort results
    fresh_breakouts = sorted(fresh_breakouts, key=lambda x: x['dist'])
    active_breakouts = sorted(active_breakouts, key=lambda x: x['dist'])
    setups = sorted(setups, key=lambda x: x['score'], reverse=True)

    top_3_setups = setups[:3]
    other_setups = setups[3:]

    ist_time = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p")

    # Generate the web page
    generate_html_page(ist_time, fresh_breakouts, active_breakouts, top_3_setups, other_setups)

    # Build and transmit the Telegram alert
    if not (fresh_breakouts or active_breakouts or setups):
        print("🏁 Scan complete. No active breakouts or setups found.")
        return

    msg = f"<b>🕒 Qullamaggie Scan Report</b>\n<i>{ist_time}</i>\n\n"

    if fresh_breakouts:
        msg += "<b>🚨 FRESH BREAKOUTS (Last 1 Hour)</b>\n"
        for b in fresh_breakouts:
            msg += f"• <a href='{b['url']}'>{b['safe_ticker']}</a>: ₹{b['price']:.2f} (ORH: ₹{b['orh']:.2f})\n"
        msg += "\n"

    if active_breakouts:
        msg += "<b>🚀 ACTIVE BREAKOUTS</b>\n"
        for b in active_breakouts:
            msg += f"• <a href='{b['url']}'>{b['safe_ticker']}</a>: ₹{b['price']:.2f} (ORH: ₹{b['orh']:.2f})\n"
        msg += "\n"

    if top_3_setups:
        msg += "<b>🌟 TOP 3 PRIME SETUPS</b> <i>(Immediate Surge Probability)</i>\n"
        for s in top_3_setups:
            msg += (
                f"• <a href='{s['url']}'>{s['safe_ticker']}</a>: ₹{s['price']:.2f} "
                f"| -{s['dist']:.1f}% | RVOL: {s['rvol']:.1f}x | Tests: {s['tests']}x\n"
            )
        msg += "\n"

    if other_setups:
        msg += f"<b>👀 OTHER SETUPS (Within {PROXIMITY_PCT}%)</b>\n"
        for s in other_setups:
            msg += f"• <a href='{s['url']}'>{s['safe_ticker']}</a>: ₹{s['price']:.2f} | -{s['dist']:.1f}%\n"

    send_telegram_chunks(msg)

if __name__ == "__main__":
    scan()
