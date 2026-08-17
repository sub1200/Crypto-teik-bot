#!/usr/bin/env python3
"""
Crypto Signal Bot - نسخة احترافية (Bybit)
================================================================
يصطاد فقط الفرص اللي فيها انفجار سيولة حقيقي + اختراق سعري + توافق قوي
بين المؤشرات - مو أي إشارة عابرة.

شروط الدخول:
  1) فلتر إلزامي: انفجار حجم تداول (الحجم الحالي >> المعدل) - بدونه تُستبعد
     العملة كلياً بغض النظر عن باقي المؤشرات.
  2) اختراق سعري: السعر قريب من قمة/قاع 24 ساعة (زخم حقيقي مو حركة عشوائية).
  3) توافق 4 من 5 عوامل: حجم متفجر + اختراق + OI + Funding + Long/Short + RSI.
  4) وقف وهدف ديناميكي مبني على ATR (يتأقلم مع تقلب كل عملة لحالها) بدل نسبة ثابتة.
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
SCAN_POOL_SIZE = 40                 # نطاق أوسع بما إن الفلاتر أشد
OI_CHANGE_THRESHOLD = 5.0
FUNDING_EXTREME = 0.03
LS_RATIO_HIGH = 1.8
LS_RATIO_LOW = 0.6
RSI_OVERBOUGHT = 68
RSI_OVERSOLD = 32
VOLUME_SURGE_RATIO = 1.8            # الحجم الحالي لازم يكون 1.8x+ من المعدل (إلزامي)
BREAKOUT_PROXIMITY_PCT = 1.5        # يعتبر "اختراق" إذا السعر بحدود 1.5% من قمة/قاع 24h
MIN_SCORE_TO_ALERT = 4              # 4 من 5 عوامل (كان 3 من 4)

# ---- إعدادات الصفقات (ATR-based) ----
MAX_OPEN_POSITIONS = 5
MAX_NEW_PER_RUN = 3
ATR_STOP_MULTIPLIER = 1.5
ATR_TARGET_MULTIPLIER = 3.0         # نسبة مخاطرة:عائد تقريبية 1:2

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
})


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
    result = safe_get(f"{BYBIT_API}/v5/market/tickers", params={"category": "linear"})
    if not result or not result.get("list"):
        return []
    usdt_pairs = [d for d in result["list"] if d["symbol"].endswith("USDT")]
    usdt_pairs.sort(key=lambda d: float(d.get("turnover24h", 0)), reverse=True)
    return usdt_pairs[:n]


def get_oi_change(symbol):
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


def get_kline_data(symbol, period=14, interval="60", limit=48):
    """يجيب الشموع مرة وحدة ويحسب منها RSI + ATR + انفجار الحجم + القمة/القاع"""
    result = safe_get(
        f"{BYBIT_API}/v5/market/kline",
        params={"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
    )
    if not result or not result.get("list") or len(result["list"]) < period + 2:
        return None

    klines = sorted(result["list"], key=lambda k: int(k[0]))  # الأقدم أولاً
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    # ---- RSI ----
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    # ---- ATR (متوسط المدى الحقيقي) ----
    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)
    atr = sum(true_ranges[-period:]) / period

    # ---- انفجار الحجم: آخر شمعة مقابل معدل الـ 20 قبلها ----
    if len(volumes) >= 21:
        last_vol = volumes[-1]
        avg_prev_vol = sum(volumes[-21:-1]) / 20
        volume_surge = (last_vol / avg_prev_vol) if avg_prev_vol > 0 else 0
    else:
        volume_surge = 0

    # ---- قمة وقاع آخر 24 ساعة (24 شمعة إذا الفريم 1H) ----
    window = min(24, len(highs))
    recent_high = max(highs[-window:])
    recent_low = min(lows[-window:])

    return {
        "rsi": rsi,
        "atr": atr,
        "volume_surge": volume_surge,
        "recent_high": recent_high,
        "recent_low": recent_low,
    }


def analyze_symbol(ticker):
    symbol = ticker["symbol"]
    price = float(ticker["lastPrice"])
    price_change_pct = float(ticker.get("price24hPcnt", 0)) * 100
    funding = float(ticker.get("fundingRate", 0)) * 100

    kdata = get_kline_data(symbol)
    oi_change = get_oi_change(symbol)
    ls_ratio = get_long_short_ratio(symbol)

    if kdata is None or oi_change is None or ls_ratio is None:
        return None

    # ---- الفلتر الإلزامي: لازم فيه انفجار سيولة حقيقي ----
    if kdata["volume_surge"] < VOLUME_SURGE_RATIO:
        return None

    rsi = kdata["rsi"]
    atr = kdata["atr"]
    recent_high = kdata["recent_high"]
    recent_low = kdata["recent_low"]

    # ---- كشف الاختراق ----
    near_high = price >= recent_high * (1 - BREAKOUT_PROXIMITY_PCT / 100)
    near_low = price <= recent_low * (1 + BREAKOUT_PROXIMITY_PCT / 100)

    long_score, short_score, reasons_long, reasons_short = 0, 0, [], []

    # عامل أساسي: الحجم المتفجر (يُحسب لصالح الاتجاه اللي معه زخم سعري)
    if price_change_pct > 0:
        long_score += 1
        reasons_long.append(f"🔥 انفجار حجم تداول ({kdata['volume_surge']:.1f}x المعدل)")
    else:
        short_score += 1
        reasons_short.append(f"🔥 انفجار حجم تداول ({kdata['volume_surge']:.1f}x المعدل)")

    # عامل الاختراق
    if near_high:
        long_score += 1
        reasons_long.append(f"اختراق قرب قمة 24 ساعة ({recent_high:.5f})")
    if near_low:
        short_score += 1
        reasons_short.append(f"اختراق قرب قاع 24 ساعة ({recent_low:.5f})")

    # OI
    if oi_change > OI_CHANGE_THRESHOLD and price_change_pct > 0:
        long_score += 1
        reasons_long.append(f"OI+{oi_change:.1f}% مع سعر مرتفع (سيولة حقيقية جديدة)")
    if oi_change > OI_CHANGE_THRESHOLD and price_change_pct < 0:
        short_score += 1
        reasons_short.append(f"OI+{oi_change:.1f}% مع سعر منخفض (سيولة شورت جديدة)")

    # Funding
    if funding <= -FUNDING_EXTREME:
        long_score += 1
        reasons_long.append(f"فاندنغ سالب ({funding:.3f}%) - الشورت مزدحمين")
    if funding >= FUNDING_EXTREME:
        short_score += 1
        reasons_short.append(f"فاندنغ موجب مرتفع ({funding:.3f}%) - خطر تصفية لونغ")

    # Long/Short ratio
    if ls_ratio <= LS_RATIO_LOW:
        long_score += 1
        reasons_long.append(f"ازدحام بيع (L/S={ls_ratio:.2f})")
    if ls_ratio >= LS_RATIO_HIGH:
        short_score += 1
        reasons_short.append(f"ازدحام شراء (L/S={ls_ratio:.2f})")

    # RSI (تأكيد اتجاه الزخم مو تشبع معاكس - نطلب RSI يدعم نفس الاتجاه هون)
    if rsi >= 55 and price_change_pct > 0:
        long_score += 1
        reasons_long.append(f"RSI يدعم الزخم الصاعد ({rsi:.0f})")
    if rsi <= 45 and price_change_pct < 0:
        short_score += 1
        reasons_short.append(f"RSI يدعم الزخم الهابط ({rsi:.0f})")

    direction, score, reasons = None, 0, []
    if long_score > short_score and long_score >= MIN_SCORE_TO_ALERT:
        direction, score, reasons = "LONG", long_score, reasons_long
    elif short_score > long_score and short_score >= MIN_SCORE_TO_ALERT:
        direction, score, reasons = "SHORT", short_score, reasons_short
    else:
        return None

    # ---- وقف وهدف ديناميكي بناءً على ATR ----
    if direction == "LONG":
        stop_loss = price - (atr * ATR_STOP_MULTIPLIER)
        target = price + (atr * ATR_TARGET_MULTIPLIER)
    else:
        stop_loss = price + (atr * ATR_STOP_MULTIPLIER)
        target = price - (atr * ATR_TARGET_MULTIPLIER)

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "reasons": reasons,
        "entry": price,
        "stop_loss": stop_loss,
        "target": target,
        "volume_surge": kdata["volume_surge"],
    }


def format_new_position_alert(sig):
    emoji = "🟢" if sig["direction"] == "LONG" else "🔴"
    reasons_txt = "\n".join(f"• {r}" for r in sig["reasons"])
    risk_reward = ATR_TARGET_MULTIPLIER / ATR_STOP_MULTIPLIER
    return (
        f"{emoji} <b>صفقة قوية: {sig['direction']}</b> - <b>{sig['symbol']}</b>\n\n"
        f"💰 سعر الدخول: {sig['entry']:.5f}\n"
        f"🎯 الهدف: {sig['target']:.5f}\n"
        f"🛑 وقف الخسارة: {sig['stop_loss']:.5f}\n"
        f"⚖️ مخاطرة:عائد ≈ 1:{risk_reward:.1f}\n\n"
        f"📊 الأسباب (قوة الإشارة: {sig['score']}/5):\n{reasons_txt}\n\n"
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
        print(f"[*] فحص {len(top)} عملة (فلتر انفجار الحجم + الاختراق)...")
        for t in top:
            if t["symbol"] in open_symbols:
                continue
            sig = analyze_symbol(t)
            if sig:
                candidates.append(sig)
            time.sleep(0.25)

        candidates.sort(key=lambda s: (s["score"], s["volume_surge"]), reverse=True)
        to_open = candidates[: min(slots_available, MAX_NEW_PER_RUN)]

        for sig in to_open:
            sig["opened_at"] = now_iso()
            positions.append(sig)
            msg = format_new_position_alert(sig)
            print(msg)
            send_telegram(msg)

        if not to_open:
            print("[*] ما فيه فرص قوية تستاهل هسه (فلتر صارم - طبيعي ما يطلع شي كل تشغيلة).")
    else:
        print(f"[*] وصلنا الحد الأقصى للصفقات المفتوحة ({MAX_OPEN_POSITIONS}).")

    save_positions(positions)
    print(f"[*] الصفقات المفتوحة الآن: {len(positions)}")


if __name__ == "__main__":
    main()
