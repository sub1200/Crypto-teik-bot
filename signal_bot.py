#!/usr/bin/env python3
"""
Crypto Signal Bot - CoinAnk-style market scanner + Position Tracker
======================================================================
1. يفحص أهم N عملة بالسيولة على Binance Futures عبر بروكسي يتجاوز حظر GitHub.
2. يختار أفضل الفرص فقط (Top picks).
3. يفتح "صفقة افتراضية" لكل فرصة مختارة (يحفظها بملف positions.json).
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

import requests

BINANCE_FAPI = "https://fapi.binance.com"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")

# ---- إعدادات المسح ----
SCAN_POOL_SIZE = 30         # عدد العملات اللي يتم فحصها كل تشغيلة
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

# ---- إعدادات الصفقات ----
MAX_OPEN_POSITIONS = 5      # أقصى عدد صفقات مفتوحة بنفس الوقت
MAX_NEW_PER_RUN = 3         # أعلى عدد صفقات جديدة تُفتح كل تشغيلة
STOP_PCT = 2.0               # % وقف الخسارة
TARGET_PCT = 4.0             # % الهدف

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
})

# استخدام بروكسي للالتفاف على حظر الـ IP لـ GitHub Actions
PROXIES = {
    "http": "http://api.allorigins.win/raw?url=",
    "https": "https://api.allorigins.win/raw?url="
}


# ============================== أدوات عامة ==============================

def safe_get(url, params=None, retries=3):
    # تحويل الرابط ليمر عبر الخدمة الوسيطة لمنع حظر 403
    proxy_url = f"https://api.allorigins.win/raw?url={url}"
    
    for attempt in range(retries):
        try:
            r = SESSION.get(proxy_url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            # محاولة مباشرة في حال فشل البروكسي
            try:
                r = SESSION.get(url, params=params, timeout=10)
                r.raise_for_status()
                return r.json()
            except Exception:
                pass
            print(f"  [warn] {url} failed (try {attempt+1}/{retries}): {e}", file=sys.stderr)
            time.sleep(1.5)
    return None


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[error] TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير مضبوطين", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = SESSION.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"[error] فشل إرسال تيليغرام: {e}", file=sys.stderr)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ============================== إدارة الصفقات ==============================

def load_positions():
    if not os.path.exists(POSITIONS_FILE):
        return []
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] تعذرت قراءة positions.json: {e}", file=sys.stderr)
        return []


def save_positions(positions):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


def get_current_price(symbol):
    data = safe_get(f"{BINANCE_FAPI}/fapi/v1/ticker/price", params={"symbol": symbol})
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
            pnl_pct = (
                (price - pos["entry"]) / pos["entry"] * 100
                if pos["direction"] == "LONG"
                else (pos["entry"] - price) / pos["entry"] * 100
            )
            send_telegram(
                f"✅ <b>تحقق الهدف!</b> {pos['symbol']} ({pos['direction']})\n\n"
                f"سعر الدخول: {pos['entry']:.5f}\n"
                f"السعر الحالي: {price:.5f}\n"
                f"الربح: +{pnl_pct:.2f}%\n"
                f"مفتوحة منذ: {pos['opened_at']}"
            )
        elif hit_stop:
            pnl_pct = (
                (price - pos["entry"]) / pos["entry"] * 100
                if pos["direction"] == "LONG"
                else (pos["entry"] - price) / pos["entry"] * 100
            )
            send_telegram(
                f"❌ <b>ضرب وقف الخسارة</b> {pos['symbol']} ({pos['direction']})\n\n"
                f"سعر الدخول: {pos['entry']:.5f}\n"
                f"السعر الحالي: {price:.5f}\n"
                f"الخسارة: {pnl_pct:.2f}%\n"
                f"مفتوحة منذ: {pos['opened_at']}"
            )
        else:
            still_open.append(pos)

        time.sleep(0.2)

    return still_open


# ============================== تحليل السوق ==============================

def get_top_symbols(n=SCAN_POOL_SIZE):
    data = safe_get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr")
    if not data or not isinstance(data, list):
        return []
    usdt_pairs = [d for d in data if isinstance(d, dict) and d.get("symbol", "").endswith("USDT")]
    usdt_pairs.sort(key=lambda d: float(d.get("quoteVolume", 0)), reverse=True)
    return usdt_pairs[:n]


def get_rsi(symbol, period=14, interval="1h"):
    klines = safe_get(
        f"{BINANCE_FAPI}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": period + 50},
    )
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
    price_change_pct = float(ticker["priceChangePercent"])

    rsi = get_rsi(symbol)
    if rsi is None:
        return None

    long_score, short_score, reasons_long, reasons_short = 0, 0, [], []

    if price_change_pct > 2.5:
        long_score += 1
        reasons_long.append(f"ارتفاع ملحوظ بالسعر (+{price_change_pct:.1f}%)")
    elif price_change_pct < -2.5:
        short_score += 1
        reasons_short.append(f"انخفاض ملحوظ بالسعر ({price_change_pct:.1f}%)")

    if rsi <= RSI_OVERSOLD:
        long_score += 2
        reasons_long.append(f"RSI تشبع بيعي ({rsi:.0f})")
    elif rsi >= RSI_OVERBOUGHT:
        short_score += 2
        reasons_short.append(f"RSI تشبع شرائي ({rsi:.0f})")

    direction, score, reasons = None, 0, []
    if long_score >= 2:
        direction, score, reasons = "LONG", long_score, reasons_long
    elif short_score >= 2:
        direction, score, reasons = "SHORT", short_score, reasons_short
    else:
        return None

    if direction == "LONG":
        stop_loss = price * (1 - STOP_PCT / 100)
        target = price * (1 + TARGET_PCT / 100)
    else:
        stop_loss = price * (1 + STOP_PCT / 100)
        target = price * (1 - TARGET_PCT / 100)

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "reasons": reasons,
        "entry": price,
        "stop_loss": stop_loss,
        "target": target,
    }


def format_new_position_alert(sig):
    emoji = "🟢" if sig["direction"] == "LONG" else "🔴"
    reasons_txt = "\n".join(f"• {r}" for r in sig["reasons"])
    return (
        f"{emoji} <b>صفقة جديدة: {sig['direction']}</b> - <b>{sig['symbol']}</b>\n\n"
        f"💰 سعر الدخول: {sig['entry']:.5f}\n"
        f"🎯 الهدف: {sig['target']:.5f}\n"
        f"🛑 وقف الخسارة: {sig['stop_loss']:.5f}\n\n"
        f"📊 الأسباب:\n{reasons_txt}\n\n"
        f"⚠️ تحليل آلي وليس نصيحة استثمارية."
    )


# ============================== التشغيل الرئيسي ==============================

def main():
    positions = load_positions()
    open_symbols = {p["symbol"] for p in positions}

    if positions:
        print(f"[*] مراقبة {len(positions)} صفقة مفتوحة...")
        positions = check_open_positions(positions)
    else:
        print("[*] ما فيه صفقات مفتوحة حالياً.")

    slots_available = MAX_OPEN_POSITIONS - len(positions)
    if slots_available > 0:
        print("[*] جلب أهم العملات بالسيولة من Binance...")
        top = get_top_symbols()
        candidates = []
        print(f"[*] فحص {len(top)} عملة...")
        for t in top:
            if t["symbol"] in open_symbols:
                continue
            sig = analyze_symbol(t)
            if sig:
                candidates.append(sig)
            time.sleep(0.1)

        candidates.sort(key=lambda s: s["score"], reverse=True)
        to_open = candidates[: min(slots_available, MAX_NEW_PER_RUN)]

        for sig in to_open:
            sig["opened_at"] = now_iso()
            positions.append(sig)
            msg = format_new_position_alert(sig)
            print(msg)
            send_telegram(msg)

        if not to_open:
            print("[*] ما فيه فرص جديدة تستاهل هسه.")
    else:
        print(f"[*] وصلنا الحد الأقصى للصفقات المفتوحة ({MAX_OPEN_POSITIONS}).")

    save_positions(positions)
    print(f"[*] الصفقات المفتوحة الآن: {len(positions)}")


if __name__ == "__main__":
    main()
