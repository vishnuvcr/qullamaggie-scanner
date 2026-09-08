import os
import requests
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
PROXIMITY_PCT = 3.0  
MAX_WORKERS = 8  # 8 parallel threads for speed without IP bans

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
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        requests.get(url, params=payload, timeout=10)
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

def process_ticker(t):
    """Processes a single ticker in two fast stages."""
    try:
        ticker_obj = yf.Ticker(t)
        
        # --- STAGE 1: Fast Daily Trend & Momentum Check ---
        d = ticker_obj.history(period="3mo", interval="1d")
        if d.empty or len(d) < 40:
            return None

        d = d.dropna(subset=['Close', 'High', 'Low'])
        if len(d) < 40:
            return None

        close_d = float(d['Close'].iloc[-1])
        ema10 = float(d['Close'].ewm(span=10).mean().iloc[-1])
        ema21 = float(d['Close'].ewm(span=21).mean().iloc[-1])
        sma50 = float(d['Close'].rolling(50).mean().iloc[-1])

        # Trend Stack: Price > 10 EMA > 21 EMA > 50 SMA
        if not (close_d > ema10 and ema10 > ema21 and ema21 > sma50):
            return None

        # 1-Month Momentum > 15%
        one_mo_ago = float(d['Close'].iloc[-21])
        one_mo_perf = ((close_d - one_mo_ago) / one_mo_ago) * 100
        if one_mo_perf < 15.0:
            return None

        # --- STAGE 2: Lazy-Load Hourly Data (Only for leaders) ---
        h = ticker_obj.history(period="5d", interval="1h")
        if h.empty or len(h) < 10:
            return None
        h = h.dropna(subset=['Close', 'High', 'Low'])

        orh_line = float(d['High'].iloc[-4:-1].max())
        curr_price = float(h['Close'].iloc[-1])
        curr_h_vol = float(h['Volume'].iloc[-1])
        avg_h_vol = float(h['Volume'].rolling(20).mean().iloc[-1])
        dist_pct = ((orh_line - curr_price) / orh_line) * 100

        log_msg = f"🎯 {t} PASSED | Price: {curr_price:.2f} | ORH: {orh_line:.2f} | Dist: {dist_pct:+.1f}% | 1M: {one_mo_perf:.1f}%"
        
        alert_msg = None
        if curr_price >= orh_line and curr_h_vol > (avg_h_vol * 1.1):
            alert_msg = f"🚀 BREAKOUT: {t} crossed ORH ({orh_line:.2f}) on volume! Now at {curr_price:.2f}"
        elif -1.5 <= dist_pct <= PROXIMITY_PCT:
            alert_msg = f"👀 SETUP: {t} near ORH ({orh_line:.2f}). Current: {curr_price:.2f} ({dist_pct:+.1f}% from line)"

        return {"log": log_msg, "alert": alert_msg}

    except Exception:
        return None

def scan():
    tickers = load_tickers()
    alerts = []
    trend_count = 0
    total = len(tickers)

    print(f"🚀 Starting multi-threaded scan across {total} tickers...")

    # Run 8 concurrent workers
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_ticker, t): t for t in tickers}
        
        for future in as_completed(futures):
            res = future.result()
            if res:
                trend_count += 1
                print(res["log"])
                if res["alert"]:
                    alerts.append(res["alert"])

    # Batch send Telegram alerts to avoid rate limits
    if alerts:
        # Send in groups of 10 if there are many setups
        chunk_size = 10
        for i in range(0, len(alerts), chunk_size):
            msg = "\n\n".join(alerts[i:i + chunk_size])
            send_telegram(msg)
        print(f"✅ Sent {len(alerts)} alerts to Telegram!")
    else:
        print(f"🏁 Scan complete. {trend_count} momentum leaders active, 0 at trigger point.")

if __name__ == "__main__":
    scan()
