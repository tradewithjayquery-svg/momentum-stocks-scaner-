"""
F&O Momentum Scanner (alerts only, koi order nahi lagta)
Bullish: price > VWAP  AND  price > ORB high  AND  volume >= 1.5x
Bearish: price < VWAP  AND  price < ORB low   AND  volume >= 1.5x
ORB = pehle 15 min (9:15-9:30) ki high/low
Volume ratio = aaj ka ab tak ka volume / pichle 10 din ka usi time tak ka average
"""
import os, io, gzip, json, time, sys
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from urllib.parse import quote
from collections import defaultdict
import requests

# ---------- Settings ----------
VOL_MULT = 1.5          # volume filter
SCAN_EVERY = 5          # minutes
SCAN_START = dtime(9, 30)   # ORB complete hone ke baad
END_TIME = dtime(13, 0)
BASE_DAYS = 10
API_SLEEP = 0.12        # rate limit se bachne ke liye

IST = ZoneInfo("Asia/Kolkata")
UPSTOX_TOKEN = os.environ["UPSTOX_ACCESS_TOKEN"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

HEAD = {"Authorization": f"Bearer {UPSTOX_TOKEN}", "Accept": "application/json"}
BASE = "https://api.upstox.com/v3/historical-candle"
INSTR_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


def now():
    return datetime.now(IST)


def tg(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": text}, timeout=15)
    except Exception as e:
        print("Telegram error:", e)


def get_fno_stocks():
    r = requests.get(INSTR_URL, timeout=60)
    r.raise_for_status()
    data = json.loads(gzip.decompress(r.content))
    stocks = {}
    for i in data:
        if i.get("segment") == "NSE_FO" and i.get("instrument_type") == "FUT":
            key = i.get("underlying_key", "")
            sym = i.get("underlying_symbol")
            if key.startswith("NSE_EQ|") and sym:
                stocks[sym] = key
    return stocks


def fetch(url):
    for _ in range(3):
        try:
            r = requests.get(url, headers=HEAD, timeout=20)
            if r.status_code == 200:
                out = []
                for c in r.json()["data"]["candles"]:
                    out.append({"dt": datetime.fromisoformat(c[0]).astimezone(IST),
                                "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]})
                out.sort(key=lambda x: x["dt"])
                return out
            if r.status_code == 429:
                time.sleep(3)
            else:
                print("API", r.status_code, r.text[:150])
                return []
        except Exception as e:
            print("fetch error:", e)
            time.sleep(1)
    return []


def load_baseline(key):
    """prev close + usi time tak ka avg cumulative volume"""
    today = now().date()
    url = f"{BASE}/{quote(key, safe='')}/minutes/5/{today}/{today - timedelta(days=21)}"
    candles = [c for c in fetch(url) if c["dt"].date() < today]
    if not candles:
        return None
    days = defaultdict(list)
    for c in candles:
        days[c["dt"].date()].append(c)
    last_days = sorted(days)[-BASE_DAYS:]
    prev_close = days[last_days[-1]][-1]["c"]
    sums, counts = defaultdict(float), defaultdict(int)
    for d in last_days:
        cum = 0
        for c in days[d]:
            cum += c["v"]
            k = c["dt"].strftime("%H:%M")
            sums[k] += cum
            counts[k] += 1
    avg = {k: sums[k] / counts[k] for k in sums}
    return {"prev_close": prev_close, "avg": avg}


def scan_stock(sym, key, base):
    url = f"{BASE}/intraday/{quote(key, safe='')}/minutes/5"
    n = now()
    candles = [c for c in fetch(url) if c["dt"].date() == n.date()]
    if not candles:
        return None, 0
    # sirf poori ho chuki candles
    done = [c for c in candles if c["dt"] + timedelta(minutes=5) <= n]
    if len(done) < 3:
        return None, len(candles)

    orb = done[:3]
    orb_high = max(c["h"] for c in orb)
    orb_low = min(c["l"] for c in orb)
    cum_vol = sum(c["v"] for c in done)
    pv = sum(((c["h"] + c["l"] + c["c"]) / 3) * c["v"] for c in done)
    if cum_vol == 0:
        return None, len(candles)
    vwap = pv / cum_vol
    price = done[-1]["c"]
    avg_vol = base["avg"].get(done[-1]["dt"].strftime("%H:%M"))
    if not avg_vol:
        return None, len(candles)
    ratio = cum_vol / avg_vol
    chg = (price - base["prev_close"]) / base["prev_close"] * 100

    side = None
    if ratio >= VOL_MULT:
        if price > vwap and price > orb_high:
            side = "BUY"
        elif price < vwap and price < orb_low:
            side = "SELL"
    if not side:
        return None, len(candles)

    icon = "🟢" if side == "BUY" else "🔴"
    ob = "ORB high breakout" if side == "BUY" else "ORB low breakdown"
    vw = "VWAP ke upar" if side == "BUY" else "VWAP ke neeche"
    msg = (f"{icon} MOMENTUM ALERT ({side} side)\n"
           f"{sym}\n"
           f"Price: {price:,.2f} ({chg:+.2f}%)\n"
           f"Volume: {ratio:.1f}x average\n"
           f"{vw} ({vwap:,.2f}) | {ob}\n"
           f"Time: {n.strftime('%I:%M %p')}")
    return (side, msg), len(candles)


def main():
    n = now()
    if n.time() >= END_TIME:
        print("Late start, exit.")
        return

    stocks = get_fno_stocks()
    print(f"F&O stocks: {len(stocks)}")
    if not stocks:
        tg("⚠️ F&O stock list nahi mili, scanner band.")
        return

    # baseline (9:15 se pehle hi ho jata hai)
    baselines = {}
    for sym, key in stocks.items():
        b = load_baseline(key)
        if b:
            baselines[sym] = b
        time.sleep(API_SLEEP)
    print(f"Baseline ready: {len(baselines)}")
    if not baselines:
        tg("⚠️ Upstox data nahi mila. Token check karo.")
        return

    tg(f"✅ Scanner start. {len(baselines)} F&O stocks, har {SCAN_EVERY} min scan.")

    alerted = set()
    first = True
    while True:
        n = now()
        if n.time() >= END_TIME:
            break
        if n.time() >= SCAN_START:
            total_candles = 0
            for sym, base in baselines.items():
                try:
                    res, nc = scan_stock(sym, stocks[sym], base)
                except Exception as e:
                    print(sym, "error:", e)
                    res, nc = None, 0
                total_candles += nc
                if res and (sym, res[0]) not in alerted:
                    alerted.add((sym, res[0]))
                    tg(res[1])
                time.sleep(API_SLEEP)
            print(f"{now():%H:%M:%S} scan done, alerts so far: {len(alerted)}")
            if first and total_candles == 0:
                tg("ℹ️ Aaj market data nahi (holiday?). Scanner band.")
                return
            first = False

        n = now()
        nxt = n.replace(minute=n.minute - n.minute % SCAN_EVERY, second=15,
                        microsecond=0) + timedelta(minutes=SCAN_EVERY)
        time.sleep(max(5, (nxt - n).total_seconds()))

    tg(f"🏁 Scanner band (1:00 PM). Aaj total alerts: {len(alerted)}")


if __name__ == "__main__":
    main()
