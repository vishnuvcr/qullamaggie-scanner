import os
import requests
import yfinance as yf
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
PROXIMITY_PCT = 3.0  # Setups within 3% below ORH
MAX_WORKERS = 8  

def load_tickers():
    if os.path.exists("tickers.txt"):
        with open("tickers.txt", "r") as f:
            return [line.strip() + ".NS" for line in f.readlines() if line.strip()]
    return ["MANALIPETC.NS", "TNPETRO.NS", "DIXON.NS", "LUPIN.NS"]

def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram credentials missing!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID, 
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True # Prevents huge chart thumbnails in chat
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

def process_ticker(t):
    """Processes a single ticker and returns structured data."""
    try:
        ticker_obj = yf.Ticker(t)
        
        # --- STAGE 1: Fast Daily Trend & Momentum Check ---
        d = ticker_obj.history(period="6mo", interval="1d")
        if d.empty or len(d) < 65:
            return None

        d = d.dropna(subset=['Close', 'High', 'Low'])
        if len(d) < 65:
            return None

        close_d = float(d['Close'].iloc[-1])
        high_6m = float(d['High'].max())
        
        # Match Pine Script's exact EMA calculation
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

        # --- STAGE 2: Lazy-Load Hourly Data ---
        h = ticker_obj.history(period="5d", interval="1h")
        if h.empty or len(h) < 10:
            return None
        h = h.dropna(subset=['Close', 'High', 'Low'])

        # --- YAHOO FINANCE DAILY LAG FIX ---
        last_d_date = d.index[-1].date()
        last_h_date = h.index[-1].date()

        if last_h_date > last_d_date:
            # YF Daily is lagging. d.iloc[-1] is Yesterday.
            orh_line = float(d['High'].iloc[-1])
            curr_price = float(h['Close'].iloc[-1])
        else:
            # YF Daily is current. d.iloc[-1] is Today.
            orh_line = float(d['High'].iloc[-2])
            curr_price = float(d['Close'].iloc[-1])

        # Distance calculations
        dist_pct = ((orh_line - curr_price) / orh_line) * 100
        dist_to_6m_high = ((high_6m - curr_price) / high_6m) * 100
        clean_ticker = t.replace(".NS", "")

        # --- BREAKOUT & VOLUME LOGIC ---
        h_vol_ma = h['Volume'].rolling(20).mean()
        
        # Last candle (-1)
        close_h_1 = float(h['Close'].iloc[-1])
        vol_h_1 = float(h['Volume'].iloc[-1])
        avg_h_vol_1 = float(h_vol_ma.iloc[-1]) if not pd.isna(h_vol_ma.iloc[-1]) and h_vol_ma.iloc[-1] > 0 else 1.0
        
        # Previous candle (-2)
        close_h_2 = float(h['Close'].iloc[-2])
        vol_h_2 = float(h['Volume'].iloc[-2])
        avg_h_vol_2 = float(h_vol_ma.iloc[-2]) if not pd.isna(h_vol_ma.iloc[-2]) and h_vol_ma.iloc[-2] > 0 else 1.0

        rvol = vol_h_1 / avg_h_vol_1 # Relative volume for the current hour

        breakout_c1 = (close_h_1 >= orh_line) and (vol_h_1 > avg_h_vol_1 * 1.1)
        breakout_c2 = (close_h_2 >= orh_line) and (vol_h_2 > avg_h_vol_2 * 1.1)
        is_breakout = breakout_c1 or breakout_c2

        # --- SETUP LOGIC ---
        is_setup = (curr_price < orh_line) and (0 < dist_pct <= PROXIMITY_PCT)
        
        # Create a TV link (Assuming NSE. Change to BSE: if trading BSE stocks)
        tv_url = f"https://in.tradingview.com/chart/?symbol=NSE:{clean_ticker}"

        # Ranking Score: High RVOL + Tighter to 6-month high + Tighter to ORH = Better Score
        setup_score = (rvol * 5) - dist_pct - dist_to_6m_high

        base_data = {
            "ticker": clean_ticker, 
            "price": curr_price, 
            "orh": orh_line, 
            "dist": dist_pct, 
            "url": tv_url,
            "rvol": rvol,
            "score": setup_score
        }

        if is_breakout:
            base_data["type"] = "BREAKOUT"
            return base_data
        elif is_setup:
            base_data["type"] = "SETUP"
            return base_data

        return None

    except Exception as e:
        return None

def scan():
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

    # --- TELEGRAM MESSAGE FORMATTING ---
    if not breakouts and not setups:
        print("🏁 Scan complete. No active breakouts or setups found.")
        return

    # Sort results
    breakouts = sorted(breakouts, key=lambda x: x['dist'])
    
    # Sort setups by our custom setup_score (highest score first)
    setups = sorted(setups, key=lambda x: x['score'], reverse=True)
    
    top_3_setups = setups[:3]
    other_setups = setups[3:]

    # Calculate current IST Time
    ist_time = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p")

    msg = f"<b>🕒 Qullamaggie Scan Report</b>\n"
    msg += f"<i>{ist_time}</i>\n\n"

    if breakouts:
        msg += "<b>🚀 ACTIVE BREAKOUTS</b>\n"
        for b in breakouts:
            msg += f"• <a href='{b['url']}'>{b['ticker']}</a>: ₹{b['price']:.2f} (ORH: ₹{b['orh']:.2f})\n"
        msg += "\n"

    if top_3_setups:
        msg += "<b>🌟 TOP 3 PRIME SETUPS</b> <i>(High Vol & Tightest)</i>\n"
        for s in top_3_setups:
            # We show RVOL and negative distance to represent "below ORH"
            msg += f"• <a href='{s['url']}'>{s['ticker']}</a>: ₹{s['price']:.2f} | Dist: -{s['dist']:.1f}% | RVOL: {s['rvol']:.1f}x\n"
        msg += "\n"

    if other_setups:
        msg += f"<b>👀 OTHER SETUPS (Within {PROXIMITY_PCT}%)</b>\n"
        for s in other_setups:
            msg += f"• <a href='{s['url']}'>{s['ticker']}</a>: ₹{s['price']:.2f} | Dist: -{s['dist']:.1f}%\n"

    # Telegram has a 4096 char limit. Truncate if the list is absurdly massive.
    if len(msg) > 4000:
        msg = msg[:4000] + "\n\n... [Message Truncated]"

    send_telegram(msg)
    print("✅ Alert report compiled and sent to Telegram!")

if __name__ == "__main__":
    scan()
