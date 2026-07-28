#!/usr/bin/env python3
"""
Crypto Signal Bot - Fast & Reliable (MEXC API)
======================================================================
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

import requests

MEXC_API = "https://api.mexc.com"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")

SCAN_POOL_SIZE = 25
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
MAX_OPEN_POSITIONS = 5
MAX_NEW_PER_RUN = 3

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})


def safe_get(url, params=None, retries=2):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(1)
    return None


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        SESSION.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"[error] فشل إرسال تيليغرام: {e}", file=sys.stderr)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_positions():
    if not os.path.exists(POSITIONS_FILE):
        return []
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_positions(positions):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


def get_current_price(symbol):
    data = safe_get(f"{MEXC_API}/api/v3/ticker/price", params={"symbol": symbol})
    if not data or "price" not in data:
        return None
    return float(data["price"])


def check_open_positions(positions):
    still_open = []
    for pos in positions:
        price = get_current_price(pos["symbol"])
        if price is None:
            still_open.append(pos)
            continue

        hit_target = (
            (pos["direction"] == "LONG" and price >= pos["target"])
            or (pos["direction"] == "SHORT" and price <= pos["target"])
        )
        hit_stop = (
            (pos["direction"] == "LONG" and price <= pos["stop_loss"])
            or (pos["direction"] == "SHORT" and price >= pos["stop_loss"])
        )

        if hit_target:
            pnl_pct = ((price - pos["entry"]) / pos["entry"] * 100) if pos["direction"] == "LONG" else ((pos["entry"] - price) / pos["entry"] * 100)
            send_telegram(f"✅ <b>تحقق الهدف الأول!</b> {pos['symbol']}\nالربح: +{pnl_pct:.2f}%")
        elif hit_stop:
            pnl_pct = ((price - pos["entry"]) / pos["entry"] * 100) if pos["direction"] == "LONG" else ((pos["entry"] - price) / pos["entry"] * 100)
            send_telegram(f"🛑 <b>ضرب وقف الخسارة</b> {pos['symbol']}\nالخسارة: {pnl_pct:.2f}%")
        else:
            still_open.append(pos)
        time.sleep(0.1)
    return still_open


def get_top_symbols(n=SCAN_POOL_SIZE):
    data = safe_get(f"{MEXC_API}/api/v3/ticker/24hr")
    if not data or not isinstance(data, list):
        return []
    usdt_pairs = [d for d in data if d.get("symbol", "").endswith("USDT")]
    usdt_pairs.sort(key=lambda d: float(d.get("quoteVolume", 0)), reverse=True)
    return usdt_pairs[:n]


def get_rsi(symbol, period=14):
    klines = safe_get(f"{MEXC_API}/api/v3/klines", params={"symbol": symbol, "interval": "60m", "limit": period + 30})
    if not klines or len(klines) < period + 1:
        return None
    closes = [float(k[4]) for k in klines]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyze_symbol(ticker):
    symbol = ticker["symbol"]
    price = float(ticker["lastPrice"])
    price_change = float(ticker["priceChangePercent"])

    rsi = get_rsi(symbol)
    if rsi is None:
        return None

    direction = None
    if rsi <= RSI_OVERSOLD and price_change > -5:
        direction = "LONG"
    elif rsi >= RSI_OVERBOUGHT and price_change < 5:
        direction = "SHORT"
    else:
        return None

    # حساب النطاقات والأهداف مثل شكل الصورة تماماً
    formatted_symbol = symbol.replace("USDT", "|USDT")
    buy_low = price * 0.985
    buy_high = price * 1.005
    
    t1 = price * 1.02
    t2 = price * 1.04
    t3 = price * 1.07
    t4 = price * 1.12
    t5 = price * 1.20
    stop = price * 0.95

    return {
        "symbol": symbol,
        "formatted_symbol": formatted_symbol,
        "direction": direction,
        "score": 2,
        "entry": price,
        "buy_range": f"{buy_low:.2f} - {buy_high:.2f}" if price > 1 else f"{buy_low:.4f} - {buy_high:.4f}",
        "t1": f"{t1:.2f}" if price > 1 else f"{t1:.4f}",
        "t2": f"{t2:.2f}" if price > 1 else f"{t2:.4f}",
        "t3": f"{t3:.2f}" if price > 1 else f"{t3:.4f}",
        "t4": f"{t4:.2f}" if price > 1 else f"{t4:.4f}",
        "t5": f"{t5:.2f}" if price > 1 else f"{t5:.4f}",
        "stop": f"{stop:.2f}" if price > 1 else f"{stop:.4f}",
        "target": t1,
        "stop_loss": stop
    }


def format_custom_telegram_msg(sig):
    """دالة تنسيق الرسالة لتكون مطابقة للصورة تماماً"""
    action_type = "Buy" if sig["direction"] == "LONG" else "Sell"
    
    msg = (
        f"🌹 <b>ربح التراكمي Scalp~سبوت</b> 🌹\n"
        f"❇️<b>{sig['formatted_symbol']}</b>\n\n"
        f"🔱 {action_type}: {sig['buy_range']}\n\n"
        f"T:🎯\n\n"
        f"T1: {sig['t1']}\n"
        f"T2: {sig['t2']}\n"
        f"T3: {sig['t3']}\n"
        f"T4: {sig['t4']}\n"
        f"T5: {sig['t5']}\n\n"
        f"🔴Stop: {sig['stop']} اغلاق 4ساعات اقل من"
    )
    return msg


def main():
    positions = load_positions()
    open_symbols = {p["symbol"] for p in positions}

    if positions:
        print(f"[*] مراقبة {len(positions)} صفقة مفتوحة...")
        positions = check_open_positions(positions)

    slots_available = MAX_OPEN_POSITIONS - len(positions)
    if slots_available > 0:
        print("[*] جلب أفضل العملات بالسيولة...")
        top = get_top_symbols()
        print(f"[*] تم جلب {len(top)} عملة، جاري الفحص...")
        
        candidates = []
        for t in top:
            if t["symbol"] in open_symbols:
                continue
            sig = analyze_symbol(t)
            if sig:
                candidates.append(sig)
            time.sleep(0.05)

        to_open = candidates[: min(slots_available, MAX_NEW_PER_RUN)]
        for sig in to_open:
            sig["opened_at"] = now_iso()
            positions.append(sig)
            
            # إرسال التنسيق الجديد
            msg = format_custom_telegram_msg(sig)
            print("--- إرسال رسالة بتنسيق الصورة ---")
            print(msg)
            send_telegram(msg)

        if not to_open:
            print("[*] ما فيه فرص جديدة الآن.")

    save_positions(positions)
    print(f"[*] الصفقات المفتوحة الآن: {len(positions)}")


if __name__ == "__main__":
    main()
