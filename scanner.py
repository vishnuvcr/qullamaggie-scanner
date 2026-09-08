import os
import html
import requests
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
PROXIMITY_PCT = 3.0  
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
        "disable_web_page_preview": True
    }
    try:
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code != 200:
            print(f"Telegram API Error: {response.text}")
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
        
        ema10 = float(d['Close'].ewm(span=10, adjust=False).mean().iloc[-1])
        ema21 = float(d['Close'].ewm(span=21, adjust=False).mean().iloc[-1])
        sma50 = float(d['Close'].rolling(50).mean().iloc[-1])

        if not (close_d > ema10 and ema10 > ema21 and ema21 > sma50):
            return None

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
            orh_line = float(d['High'].iloc[-1])
            curr_price = float(h['Close'].iloc[-1])
        else:
            orh_line = float(d['High'].iloc[-2])
            curr_price = float(d['Close'].iloc[-1])

        dist_pct = ((orh_line - curr_price) /orh_line) * 100
        clean_ticker = t.replace(".NS", "")

        # --- BREAKOUT LOGIC (Last 2 Hourly Candles) ---
        h_vol_ma = h['Volume'].rolling(20).mean()
        
        close_h_1 = float(h['Close'].iloc[-1])
        vol_h_1 = float(h['Volume'].iloc[-1])
        avg_h_vol_1 = float(h_vol_ma.iloc[-1])
        
        close_h_2 = float(h['Close'].iloc[-2])
        vol_h_2 = float(h['Volume'].iloc[-2])
        avg_h_vol_2 = float(h_vol_ma.iloc[-2])

        breakout_c1 = (close_h_1 >= orh_line) and (vol_h_1 > avg_h_vol_1 * 1.1)
        breakout_c2 = (close_h_2 >= orh_line) and (vol_h_2 > avg_h_vol_2 * 1.1)
        is_breakout = breakout_c1 or breakout_c2

        # --- SETUP LOGIC ---
        is_setup = (curr_price < orh_line) and (0 < dist_pct <= PROXIMITY_PCT)

        result = {
            "ticker": clean_ticker, 
            "price": curr_price, 
            "orh": orh_line, 
            "dist": dist_pct,
            "momentum": three_mo_perf
        }

        if is_breakout:
            result["type"] = "BREAKOUT"
            return result
        elif is_setup:
            result["type"] = "SETUP"
            return result

        return None

    except Exception:
        return None

def format_row(ticker, price, orh, dist=None, momentum=None):
    """Formats a table row safely with HTML escaping and fixed column spacing."""
    safe_ticker = html.escape(ticker)
    tv_url = f"https://in.tradingview.com/chart/?symbol=NSE:{ticker}"
    
    # Calculate spacing padding to keep the monospaced table perfectly straight
    padding = " " * max(0, 10 - len(ticker))
    linked_ticker = f'<a href="{tv_url}">{safe_ticker}</a>{padding}'
    
    if dist is not None and momentum is not None:
        return f"{linked_ticker} | {price:<7.2f} | {orh:<7.2f} | {-dist:>5.1f}% | {momentum:>5.1f}%\n"
    elif dist is not None:
        return f"{linked_ticker} | {price:<7.2f} | {orh:<7.2f} | {-dist:>5.1f}%\n"
    else:
        return f"{linked_ticker} | {price:<7.2f} | {orh:<7.2f}\n"

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

    ist_time = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p")
    msg = f"<b>🕒 Qullamaggie Scan Report</b>\n<i>{ist_time}</i>\n\n"

    # 1. Breakouts
    if breakouts:
        breakouts = sorted(breakouts, key=lambda x: x['dist'])
        msg += "<b>🚀 BREAKOUTS (Last 2 Hrs)</b>\n<pre>\n"
        msg += f"{'TICKER':<10} | {'PRICE':<7} | {'ORH':<7}\n"
        msg += "-" * 30 + "\n"
        for b in breakouts:
            msg += format_row(b['ticker'], b['price'], b['orh'])
        msg += "</pre>\n"

    # 2. Top 3 Setups
    if setups:
        setups = sorted(setups, key=lambda x: x['momentum'], reverse=True)
        top_3 = setups[:3]
        other_setups = setups[3:]

        msg += f"<b>🔥 TOP 3 WATCHLIST (Highest 3M Growth)</b>\n<pre>\n"
        msg += f"{'TICKER':<10} | {'PRICE':<7} | {'ORH':<7} | {'DIST':<6} | {'3M %'}\n"
        msg += "-" * 46 + "\n"
        for s in top_3:
            msg += format_row(s['ticker'], s['price'], s['orh'], s['dist'], s['momentum'])
        msg += "</pre>\n"

        # 3. Remaining Setups
        if other_setups:
            other_setups = sorted(other_setups, key=lambda x: x['dist'])
            msg += f"<b>👀 OTHER SETUPS (Within {PROXIMITY_PCT}%)</b>\n<pre>\n"
            msg += f"{'TICKER':<10} | {'PRICE':<7} | {'ORH':<7} | {'DIST'}\n"
            msg += "-" * 37 + "\n"
            for s in other_setups:
                msg += format_row(s['ticker'], s['price'], s['orh'], s['dist'])
            msg += "</pre>"

    if len(msg) > 4000:
        msg = msg[:4000] + "\n\n... [Message Truncated]"

    send_telegram(msg)
    print("✅ Alert report compiled and sent to Telegram!")

if __name__ == "__main__":
    scan()
