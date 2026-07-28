#!/usr/bin/env python3
"""
Crypto Signal Bot - CoinAnk-style market scanner + Position Tracker
======================================================================
يستخدم Bybit API (بدل Binance اللي يحظر IP مالت GitHub Actions بخطأ 451).

1. يفحص أهم N عملة بالسيولة على Bybit Futures (linear/USDT perpetuals).
2. يختار أفضل الفرص فقط (Top picks) - مو كل شي يطابق الشروط.
3. يفتح "صفقة افتراضية" لكل فرصة مختارة (يحفظها بملف positions.json).
4. بكل تشغيلة يراقب الصفقات المفتوحة: إذا وصل السعر الهدف يرسل تنبيه ربح
   ويقفل الصفقة، وإذا ضرب وقف الخسارة يرسل تنبيه خسارة ويقفلها.

الحالة (الصفقات المفتوحة) تُحفظ بملف positions.json ويتم رفعه (commit) تلقائياً
من قبل GitHub Actions بعد كل تشغيلة، عشان الصفقات تضل محفوظة بين التشغيلات.
"""

import os
import sys
import json
import time
from datetime import datetime, timezone

import requests

BYBIT_API = "https://api.bybit.com"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")

# ---- إعدادات المسح ----
SCAN_POOL_SIZE = 30
OI_CHANGE_THRESHOLD = 5.0
FUNDING_EXTREME = 0.03
LS_RATIO_HIGH = 1.8
LS_RATIO_LOW = 0.6
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
MIN_SCORE_TO_ALERT = 3

# ---- إعدادات الصفقات ----
MAX_OPEN_POSITIONS = 5
MAX_NEW_PER_RUN = 3
STOP_PCT = 2.0
TARGET_PCT = 4.0

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (signal-bot)"})


# ============================== أدوات عامة ==============================

def safe_get(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("retCode") != 0:
                print(f"  [warn] Bybit API error: {data.get('retMsg')}", file=sys.stderr)
                return None
            return data.get("result")
        except Exception as e:
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


# ============================== إدارة الصفقات (الحالة) ==============================

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
    result = safe_get(f"{BYBIT_API}/v5/market/tickers", params={"category": "linear", "symbol": symbol})
    if not result or not result.get("list"):
        return None
    return float(result["list"][0]["lastPrice"])


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

        time.sleep(0.15)

    return still_open


# ============================== تحليل السوق (Bybit) ==============================

def get_top_symbols(n=SCAN_POOL_SIZE):
    """أهم عقود USDT الدائمة (linear) بحسب حجم التداول خلال 24 ساعة"""
    result = safe_get(f"{BYBIT_API}/v5/market/tickers", params={"category": "linear"})
    if not result or not result.get("list"):
        return []
    usdt_pairs = [d for d in result["list"] if d["symbol"].endswith("USDT")]
    usdt_pairs.sort(key=lambda d: float(d.get("turnover24h", 0)), reverse=True)
    return usdt_pairs[:n]


def get_oi_change(symbol):
    """نسبة تغيّر Open Interest خلال آخر 4 ساعات (شموع 1H)"""
    result = safe_get(
        f"{BYBIT_API}/v5/market/open-interest",
        params={"category": "linear", "symbol": symbol, "intervalTime": "1h", "limit": 5},
    )
    if not result or not result.get("list") or len(result["list"]) < 5:
        return None
    points = sorted(result["list"], key=lambda p: int(p["timestamp"]))
    oldest = float(points[0]["openInterest"])
    newest = float(points[-1]["openInterest"])
    if oldest == 0:
        return None
    return (newest - oldest) / oldest * 100


def get_long_short_ratio(symbol):
    """نسبة buyRatio/sellRatio من حسابات المتداولين (Account Ratio) آخر ساعة"""
    result = safe_get(
        f"{BYBIT_API}/v5/market/account-ratio",
        params={"category": "linear", "symbol": symbol, "period": "1h", "limit": 1},
    )
    if not result or not result.get("list"):
        return None
    entry = result["list"][0]
    buy_ratio = float(entry["buyRatio"])
    sell_ratio = float(entry["sellRatio"])
    if sell_ratio == 0:
        return None
    return buy_ratio / sell_ratio


def get_rsi(symbol, period=14, interval="60"):
    """حساب RSI يدوياً من شموع Bybit (interval='60' = 1 ساعة)"""
    result = safe_get(
        f"{BYBIT_API}/v5/market/kline",
        params={"category": "linear", "symbol": symbol, "interval": interval, "limit": period + 50},
    )
    if not result or not result.get("list") or len(result["list"]) < period + 1:
        return None
    # Bybit يرجع الشموع بترتيب تنازلي (الأحدث أول) - نعكسها
    klines = sorted(result["list"], key=lambda k: int(k[0]))
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
    price_change_pct = float(ticker.get("price24hPcnt", 0)) * 100
    funding = float(ticker.get("fundingRate", 0)) * 100  # كنسبة مئوية

    oi_change = get_oi_change(symbol)
    ls_ratio = get_long_short_ratio(symbol)
    rsi = get_rsi(symbol)

    if None in (oi_change, ls_ratio, rsi):
        return None

    long_score, short_score, reasons_long, reasons_short = 0, 0, [], []

    if oi_change > OI_CHANGE_THRESHOLD and price_change_pct > 0:
        long_score += 1
        reasons_long.append(f"OI+{oi_change:.1f}% مع سعر مرتفع (دخول سيولة جديدة)")
    if oi_change > OI_CHANGE_THRESHOLD and price_change_pct < 0:
        short_score += 1
        reasons_short.append(f"OI+{oi_change:.1f}% مع سعر منخفض (دخول سيولة شورت)")

    if funding <= -FUNDING_EXTREME:
        long_score += 1
        reasons_long.append(f"فاندنغ سالب ({funding:.3f}%) - الشورت مزدحمين")
    if funding >= FUNDING_EXTREME:
        short_score += 1
        reasons_short.append(f"فاندنغ موجب مرتفع ({funding:.3f}%) - خطر تصفية لونغ")

    if ls_ratio <= LS_RATIO_LOW:
        long_score += 1
        reasons_long.append(f"ازدحام بيع (L/S={ls_ratio:.2f}) - احتمال ارتداد صعودي")
    if ls_ratio >= LS_RATIO_HIGH:
        short_score += 1
        reasons_short.append(f"ازدحام شراء (L/S={ls_ratio:.2f}) - احتمال تصحيح هبوطي")

    if rsi <= RSI_OVERSOLD:
        long_score += 1
        reasons_long.append(f"RSI تشبع بيعي ({rsi:.0f})")
    if rsi >= RSI_OVERBOUGHT:
        short_score += 1
        reasons_short.append(f"RSI تشبع شرائي ({rsi:.0f})")

    direction, score, reasons = None, 0, []
    if long_score > short_score and long_score >= MIN_SCORE_TO_ALERT:
        direction, score, reasons = "LONG", long_score, reasons_long
    elif short_score > long_score and short_score >= MIN_SCORE_TO_ALERT:
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
        f"📊 الأسباب (قوة الإشارة: {sig['score']}/4):\n{reasons_txt}\n\n"
        f"⚠️ تحليل آلي وليس نصيحة استثمارية - أدر رأس مالك بحذر."
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
        print("[*] جلب أهم العملات بالسيولة من Bybit...")
        top = get_top_symbols()
        candidates = []
        print(f"[*] فحص {len(top)} عملة...")
        for t in top:
            if t["symbol"] in open_symbols:
                continue
            sig = analyze_symbol(t)
            if sig:
                candidates.append(sig)
            time.sleep(0.25)

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
        print(f"[*] وصلنا الحد الأقصى للصفقات المفتوحة ({MAX_OPEN_POSITIONS}) - ما راح نفتح جديد.")

    save_positions(positions)
    print(f"[*] الصفقات المفتوحة الآن: {len(positions)}")


if __name__ == "__main__":
    main()
