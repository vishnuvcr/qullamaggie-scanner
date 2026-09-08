import os
import requests
import yfinance as yf
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
        "parse_mode": "HTML"  
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

        # Distance calculation
        dist_pct = ((orh_line - curr_price) / orh_line) * 100
        clean_ticker = t.replace(".NS", "")

        # --- BREAKOUT LOGIC (Last 2 Hourly Candles) ---
        h_vol_ma = h['Volume'].rolling(20).mean()
        
        # Last candle (-1)
        close_h_1 = float(h['Close'].iloc[-1])
        vol_h_1 = float(h['Volume'].iloc[-1])
        avg_h_vol_1 = float(h_vol_ma.iloc[-1])
        
        # Previous candle (-2)
        close_h_2 = float(h['Close'].iloc[-2])
        vol_h_2 = float(h['Volume'].iloc[-2])
        avg_h_vol_2 = float(h_vol_ma.iloc[-2])

        breakout_c1 = (close_h_1 >= orh_line) and (vol_h_1 > avg_h_vol_1 * 1.1)
        breakout_c2 = (close_h_2 >= orh_line) and (vol_h_2 > avg_h_vol_2 * 1.1)
        is_breakout = breakout_c1 or breakout_c2

        # --- SETUP LOGIC (Strictly Below ORH & Within Proximity) ---
        is_setup = (curr_price < orh_line) and (0 < dist_pct <= PROXIMITY_PCT)

        if is_breakout:
            return {"type": "BREAKOUT", "ticker": clean_ticker, "price": curr_price, "orh": orh_line, "dist": dist_pct}
        elif is_setup:
            return {"type": "SETUP", "ticker": clean_ticker, "price": curr_price, "orh": orh_line, "dist": dist_pct}

        return None

    except Exception:
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

    # Sort results by distance percentage for a cleaner read
    breakouts = sorted(breakouts, key=lambda x: x['dist'])
    setups = sorted(setups, key=lambda x: x['dist'])

    # --- TELEGRAM MESSAGE FORMATTING ---
    if not breakouts and not setups:
        print("🏁 Scan complete. No active breakouts or setups found.")
        return

    # Calculate current IST Time
    ist_time = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p")

    msg = f"<b>🕒 Qullamaggie Scan Report</b>\n"
    msg += f"<i>{ist_time}</i>\n\n"

    if breakouts:
        msg += "<b>🚀 BREAKOUTS (Last 2 Hrs)</b>\n"
        msg += "<pre>\n"
        msg += f"{'TICKER':<10} | {'PRICE':<7} | {'ORH':<7}\n"
        msg += "-" * 30 + "\n"
        for b in breakouts:
            msg += f"{b['ticker']:<10} | {b['price']:<7.2f} | {b['orh']:<7.2f}\n"
        msg += "</pre>\n"

    if setups:
        msg += f"<b>👀 SETUPS (Below ORH, Within {PROXIMITY_PCT}%)</b>\n"
        msg += "<pre>\n"
        msg += f"{'TICKER':<10} | {'PRICE':<7} | {'ORH':<7} | {'DIST'}\n"
        msg += "-" * 37 + "\n"
        for s in setups:
            msg += f"{s['ticker']:<10} | {s['price']:<7.2f} | {s['orh']:<7.2f} | {-s['dist']:+.1f}%\n"
        msg += "</pre>"

    # Telegram has a 4096 char limit. Truncate if the list is absurdly massive.
    if len(msg) > 4000:
        msg = msg[:4000] + "\n\n... [Message Truncated]"

    send_telegram(msg)
    print("✅ Alert report compiled and sent to Telegram!")

if __name__ == "__main__":
    scan()
