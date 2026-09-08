import yfinance as yf
import requests
import os
import time

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
PROXIMITY_PCT = 3.0  

def load_tickers():
    # Place your tickers in a tickers.txt file in the repo, one per line
    if os.path.exists("tickers.txt"):
        with open("tickers.txt", "r") as f:
            return [line.strip() + ".NS" for line in f.readlines() if line.strip()]
    return ["MANALIPETC.NS", "TNPETRO.NS", "DIXON.NS", "LUPIN.NS"]

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    requests.get(url, params=payload)

def scan():
    tickers = load_tickers()
    alerts = []
    trend_count = 0
    
    print(f"Starting scan for {len(tickers)} tickers...")

    for t in tickers:
        # 1-second delay keeps Yahoo Finance happy on GitHub's IPs
        time.sleep(1)
        
        try:
            ticker_obj = yf.Ticker(t)
            d = ticker_obj.history(period="3mo", interval="1d")
            h = ticker_obj.history(period="5d", interval="1h")

            if d.empty or h.empty:
                print(f"{t}: Skipped (No data)")
                continue

            d = d.dropna(subset=['Close', 'High', 'Low'])
            h = h.dropna(subset=['Close', 'High', 'Low'])

            if len(d) < 40 or len(h) < 10:
                continue

            close_d = float(d['Close'].iloc[-1])
            
            # Trend Stack
            ema10 = float(d['Close'].ewm(span=10).mean().iloc[-1])
            ema21 = float(d['Close'].ewm(span=21).mean().iloc[-1])
            sma50 = float(d['Close'].rolling(50).mean().iloc[-1])

            if not (close_d > ema10 and ema10 > ema21 and ema21 > sma50):
                continue

            # Momentum
            one_mo_ago = float(d['Close'].iloc[-21])
            one_mo_perf = ((close_d - one_mo_ago) / one_mo_ago) * 100
            if one_mo_perf < 15.0:
                continue

            trend_count += 1
            orh_line = float(d['High'].iloc[-4:-1].max())
            curr_price = float(h['Close'].iloc[-1])
            curr_h_vol = float(h['Volume'].iloc[-1])
            avg_h_vol = float(h['Volume'].rolling(20).mean().iloc[-1])
            dist_pct = ((orh_line - curr_price) / orh_line) * 100

            print(f"{t} Passed Setup. Dist: {dist_pct:.1f}%")

            if curr_price >= orh_line and curr_h_vol > (avg_h_vol * 1.1):
                alerts.append(f"🚀 BREAKOUT: {t} crossed ORH ({orh_line:.2f}) on volume! Now at {curr_price:.2f}")
            elif -1.5 <= dist_pct <= PROXIMITY_PCT:
                alerts.append(f"👀 SETUP: {t} near ORH ({orh_line:.2f}). Current: {curr_price:.2f} ({dist_pct:+.1f}% from line)")

        except Exception as e:
            print(f"{t}: Error - {e}")
            continue

    if alerts:
        msg = "\n\n".join(alerts)
        send_telegram(msg)
        print("Alerts sent to Telegram!")
    else:
        print(f"Scan complete. {trend_count} trend setups found, zero triggers.")

if __name__ == "__main__":
    scan()
