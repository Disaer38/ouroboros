#!/usr/bin/env python3
"""
Comprehensive analysis of Polymarket trader 0xa45fe11dd1420fca906ceac2c067844379a42429 (guh123).
Fetches full trade history via pagination, analyzes strategy, saves raw data + report.
"""

import requests
import json
import time
import os
from collections import defaultdict
from datetime import datetime, timezone

# ─── Config ──────────────────────────────────────────────────────────────────
TARGET = "0xa45fe11dd1420fca906ceac2c067844379a42429"
DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR  = os.path.join(REPO_ROOT, "data")
REPORT_DIR= os.path.join(REPO_ROOT, "reports")
os.makedirs(DATA_DIR,   exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

RAW_FILE    = os.path.join(DATA_DIR,   "0xa45_history.json")
REPORT_FILE = os.path.join(REPORT_DIR, "0xa45_analysis.md")

LIMIT = 500   # max per request
MAX_PAGES = 200  # safety cap → up to 100,000 trades

# ─── Fetch helpers ────────────────────────────────────────────────────────────

def fetch_trades_page(user: str, offset: int) -> list:
    url = f"{DATA_API}/trades"
    params = {"user": user, "limit": LIMIT, "offset": offset}
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"  [!] Attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return []


def fetch_all_trades(user: str) -> list:
    """Paginate through all available trades for a user."""
    all_trades = []
    offset = 0
    print(f"[*] Fetching trades for {user}")
    for page in range(MAX_PAGES):
        batch = fetch_trades_page(user, offset)
        if not batch:
            print(f"  → Empty page at offset {offset}. Done.")
            break
        all_trades.extend(batch)
        print(f"  → Page {page+1}: +{len(batch)} trades (total: {len(all_trades)})")
        if len(batch) < LIMIT:
            break  # last page
        offset += len(batch)
        time.sleep(0.15)  # polite rate limiting
    return all_trades


def fetch_activity(user: str) -> dict:
    """Fetch aggregated activity/profile from Gamma API."""
    url = f"{GAMMA_API}/activity"
    params = {"user": user}
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [!] Activity fetch failed: {e}")
        return {}


def fetch_positions(user: str) -> list:
    """Fetch current positions."""
    url = f"{DATA_API}/positions"
    params = {"user": user, "limit": 500}
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  [!] Positions fetch failed: {e}")
        return []


# ─── Analysis ─────────────────────────────────────────────────────────────────

def classify_market(asset_id: str, question: str, slug: str) -> str:
    """Classify market into timeframe category."""
    text = (question + slug).lower()
    if "5m" in text or "5-min" in text or "5 min" in text:
        return "5m"
    if "15m" in text or "15-min" in text or "15 min" in text:
        return "15m"
    if "1h" in text or "hourly" in text or "1-hour" in text or "1 hour" in text:
        return "1h"
    if "daily" in text or "end of day" in text:
        return "daily"
    return "other"


def safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def analyze_trades(trades: list) -> dict:
    """Full analysis of trade list."""
    if not trades:
        return {}

    # ── Per-market aggregation ──
    market_stats = defaultdict(lambda: {
        "question": "", "slug": "", "asset_id": "",
        "condition_id": "", "timeframe": "other",
        "trades": 0, "volume_usdc": 0.0,
        "buy_yes": 0, "buy_no": 0,
        "shares_yes": 0.0, "shares_no": 0.0,
        "pnl_estimate": 0.0,
        "prices_yes": [], "prices_no": [],
        "timestamps": [],
    })

    hourly_volume = defaultdict(float)
    hourly_trades = defaultdict(int)
    size_buckets  = defaultdict(int)
    total_volume  = 0.0
    total_fees    = 0.0
    trade_sizes   = []
    timestamps    = []

    for t in trades:
        price    = safe_float(t.get("price"))
        size     = safe_float(t.get("size"))        # shares
        value    = price * size                      # USDC equivalent
        fee      = safe_float(t.get("feeRateBps", 0)) / 10000 * value
        side     = t.get("side", "").upper()         # BUY / SELL
        outcome  = t.get("outcome", "").upper()      # YES / NO
        cond_id  = t.get("conditionId", "unknown")
        question = t.get("title", t.get("question", ""))
        slug     = t.get("slug", "")
        asset_id = t.get("asset_id", t.get("assetId", ""))

        ts_str   = t.get("timestamp", t.get("createdAt", ""))
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            ts = None

        # Aggregate
        ms = market_stats[cond_id]
        ms["question"]    = question or ms["question"]
        ms["slug"]        = slug or ms["slug"]
        ms["asset_id"]    = asset_id or ms["asset_id"]
        ms["condition_id"]= cond_id
        if ms["timeframe"] == "other":
            ms["timeframe"] = classify_market(asset_id, question, slug)
        ms["trades"]      += 1
        ms["volume_usdc"] += value
        if ts:
            ms["timestamps"].append(ts.timestamp())

        if side == "BUY":
            if outcome in ("YES", "1"):
                ms["buy_yes"]    += 1
                ms["shares_yes"] += size
                ms["prices_yes"].append(price)
            elif outcome in ("NO", "0"):
                ms["buy_no"]     += 1
                ms["shares_no"]  += size
                ms["prices_no"].append(price)

        total_volume += value
        total_fees   += fee
        trade_sizes.append(value)

        # Hourly buckets
        if ts:
            hour_key = ts.strftime("%Y-%m-%dT%H:00Z")
            hourly_volume[hour_key] += value
            hourly_trades[hour_key] += 1
            timestamps.append(ts.timestamp())

        # Size buckets
        rounded = round(value)
        size_buckets[rounded] += 1

    # ── Neg-risk detection ──
    neg_risk_markets = {}
    for cid, ms in market_stats.items():
        if ms["prices_yes"] and ms["prices_no"]:
            avg_yes = sum(ms["prices_yes"]) / len(ms["prices_yes"])
            avg_no  = sum(ms["prices_no"])  / len(ms["prices_no"])
            spread  = 1.0 - (avg_yes + avg_no)
            if spread > 0:
                neg_risk_markets[cid] = {
                    "spread": round(spread, 4),
                    "avg_yes": round(avg_yes, 4),
                    "avg_no":  round(avg_no, 4),
                    "question": ms["question"],
                    "timeframe": ms["timeframe"],
                }

    # ── Timeframe breakdown ──
    timeframe_stats = defaultdict(lambda: {"trades": 0, "volume": 0.0, "markets": 0})
    for cid, ms in market_stats.items():
        tf = ms["timeframe"]
        timeframe_stats[tf]["trades"]  += ms["trades"]
        timeframe_stats[tf]["volume"]  += ms["volume_usdc"]
        timeframe_stats[tf]["markets"] += 1

    # ── Time range ──
    if timestamps:
        ts_min = datetime.fromtimestamp(min(timestamps), tz=timezone.utc)
        ts_max = datetime.fromtimestamp(max(timestamps), tz=timezone.utc)
        span_hours = (max(timestamps) - min(timestamps)) / 3600
    else:
        ts_min = ts_max = None
        span_hours = 0

    # ── Top size buckets ──
    top_sizes = sorted(size_buckets.items(), key=lambda x: -x[1])[:20]

    # ── Most active hours ──
    top_hours = sorted(hourly_volume.items(), key=lambda x: -x[1])[:24]

    # ── Top markets by volume ──
    top_markets = sorted(market_stats.values(), key=lambda x: -x["volume_usdc"])[:30]

    avg_trade_size = (total_volume / len(trades)) if trades else 0
    trade_freq_per_min = (len(trades) / (span_hours * 60)) if span_hours > 0 else 0

    # ── Neg-risk summary ──
    neg_risk_list = sorted(neg_risk_markets.values(), key=lambda x: -x["spread"])[:20]

    return {
        "total_trades": len(trades),
        "total_volume_usdc": round(total_volume, 2),
        "total_fees_est": round(total_fees, 2),
        "avg_trade_size_usdc": round(avg_trade_size, 4),
        "trade_freq_per_min": round(trade_freq_per_min, 4),
        "unique_markets": len(market_stats),
        "date_range": {
            "from": ts_min.isoformat() if ts_min else None,
            "to":   ts_max.isoformat() if ts_max else None,
            "span_hours": round(span_hours, 2),
        },
        "timeframe_breakdown": dict(timeframe_stats),
        "top_sizes": top_sizes,
        "top_hours_by_volume": top_hours,
        "top_markets_by_volume": [
            {k: v for k, v in m.items() if k not in ("prices_yes", "prices_no", "timestamps")}
            for m in top_markets
        ],
        "neg_risk_markets_count": len(neg_risk_markets),
        "neg_risk_best": neg_risk_list,
    }


# ─── Report generation ────────────────────────────────────────────────────────

def generate_report(analysis: dict, activity: dict, positions: list) -> str:
    a = analysis
    tf = a.get("timeframe_breakdown", {})
    dr = a.get("date_range", {})

    lines = []
    lines.append(f"# Анализ трейдера guh123")
    lines.append(f"> Wallet: `{TARGET}`")
    lines.append(f"> Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 📊 Общая статистика")
    lines.append("")
    lines.append(f"| Метрика | Значение |")
    lines.append(f"|---------|---------|")
    lines.append(f"| **Всего сделок** | {a['total_trades']:,} |")
    lines.append(f"| **Объём USDC** | ${a['total_volume_usdc']:,.2f} |")
    lines.append(f"| **Оценка комиссий** | ${a['total_fees_est']:,.2f} |")
    lines.append(f"| **Средняя сделка** | ${a['avg_trade_size_usdc']:.2f} |")
    lines.append(f"| **Темп** | {a['trade_freq_per_min']:.2f} трейдов/мин |")
    lines.append(f"| **Уникальных рынков** | {a['unique_markets']:,} |")
    lines.append(f"| **Neg-risk рынков** | {a['neg_risk_markets_count']:,} |")
    lines.append(f"| **Период** | {dr.get('from','?')} → {dr.get('to','?')} |")
    lines.append(f"| **Длительность** | {dr.get('span_hours', 0):.1f} ч |")
    lines.append("")

    lines.append("## ⏱️ Разбивка по таймфреймам")
    lines.append("")
    lines.append(f"| Таймфрейм | Трейдов | Объём USDC | Рынков |")
    lines.append(f"|-----------|---------|------------|--------|")
    for tname, tstats in sorted(tf.items(), key=lambda x: -x[1]["volume"]):
        lines.append(f"| **{tname}** | {tstats['trades']:,} | ${tstats['volume']:,.2f} | {tstats['markets']:,} |")
    lines.append("")

    lines.append("## 💰 Топ размеры ордеров (USDC)")
    lines.append("")
    lines.append(f"| Размер ($) | Кол-во трейдов |")
    lines.append(f"|------------|----------------|")
    for size, cnt in a.get("top_sizes", [])[:15]:
        lines.append(f"| **${size}** | {cnt:,} |")
    lines.append("")

    lines.append("## 🕐 Активность по часам (UTC)")
    lines.append("")
    lines.append(f"| Час UTC | Объём USDC |")
    lines.append(f"|---------|------------|")
    for hour, vol in sorted(a.get("top_hours_by_volume", [])[:24], key=lambda x: x[0]):
        lines.append(f"| {hour} | ${vol:,.2f} |")
    lines.append("")

    lines.append("## 🏆 Топ рынков по объёму")
    lines.append("")
    lines.append(f"| Рынок | ТФ | Трейдов | Объём | Up trades | Down trades |")
    lines.append(f"|-------|----|---------|-------|-----------|-------------|")
    for m in a.get("top_markets_by_volume", [])[:20]:
        q = (m["question"] or m["slug"] or m["condition_id"])[:60]
        lines.append(
            f"| {q} | {m['timeframe']} | {m['trades']:,} | ${m['volume_usdc']:,.2f} "
            f"| {m['buy_yes']} | {m['buy_no']} |"
        )
    lines.append("")

    lines.append("## 🔬 Neg-Risk арбитраж (лучшие спреды)")
    lines.append("")
    lines.append(f"| Рынок | ТФ | Спред | Avg Yes | Avg No |")
    lines.append(f"|-------|----|-------|---------|--------|")
    for nr in a.get("neg_risk_best", [])[:15]:
        q = (nr["question"])[:60]
        lines.append(
            f"| {q} | {nr['timeframe']} | **{nr['spread']:.4f}** "
            f"| {nr['avg_yes']:.4f} | {nr['avg_no']:.4f} |"
        )
    lines.append("")

    # Gamma activity
    if activity:
        lines.append("## 📈 Gamma API — Сводка профиля")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(activity, indent=2, ensure_ascii=False)[:3000])
        lines.append("```")
        lines.append("")

    # Positions
    if positions:
        lines.append(f"## 📌 Текущие позиции ({len(positions)} шт.)")
        lines.append("")
        lines.append(f"| Рынок | Outcome | Size | Value |")
        lines.append(f"|-------|---------|------|-------|")
        for p in sorted(positions, key=lambda x: -safe_float(x.get("value", 0)))[:20]:
            q = (p.get("title", p.get("market", "?")) or "?")[:50]
            lines.append(
                f"| {q} | {p.get('outcome','?')} "
                f"| {safe_float(p.get('size', 0)):.2f} "
                f"| ${safe_float(p.get('value', p.get('currentValue', 0))):.2f} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("## 🧠 Стратегические выводы")
    lines.append("")

    # Auto-derive conclusions
    total = a["total_trades"]
    nr_pct = (a["neg_risk_markets_count"] / a["unique_markets"] * 100) if a["unique_markets"] else 0
    tf_5m = tf.get("5m", {})
    tf_15m = tf.get("15m", {})
    pct_5m  = (tf_5m.get("volume", 0) / a["total_volume_usdc"] * 100) if a["total_volume_usdc"] else 0
    pct_15m = (tf_15m.get("volume", 0) / a["total_volume_usdc"] * 100) if a["total_volume_usdc"] else 0

    top_sizes_list = a.get("top_sizes", [])
    dominant_sizes = [s for s, c in top_sizes_list[:3] if c > total * 0.05]

    lines.append(f"1. **Тип**: Алго-бот (не человек). Темп {a['trade_freq_per_min']:.2f} трейдов/мин недостижим вручную.")
    lines.append(f"2. **Фокус**: {pct_5m:.1f}% объёма в 5m рынках, {pct_15m:.1f}% в 15m рынках.")
    lines.append(f"3. **Neg-risk арбитраж**: {nr_pct:.1f}% рынков имеют neg-risk структуру (up_price + down_price < 1.0).")
    if dominant_sizes:
        lines.append(f"4. **Фиксированные размеры ордеров**: Доминируют размеры {dominant_sizes} USDC → детерминированный бот.")
    lines.append(f"5. **Масштаб**: ${a['total_volume_usdc']:,.2f} USDC за {dr.get('span_hours',0):.1f} ч = ${a['total_volume_usdc']/max(dr.get('span_hours',1),1):,.2f}/ч.")
    lines.append(f"6. **Комиссии**: ~${a['total_fees_est']:,.2f} (оценка) = {a['total_fees_est']/max(a['total_volume_usdc'],1)*100:.3f}% от объёма.")
    lines.append("")

    return "\n".join(lines)


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"Polymarket Trader Analysis")
    print(f"Target: {TARGET}")
    print("=" * 60)

    # 1. Fetch trades
    trades = fetch_all_trades(TARGET)
    print(f"\n[✓] Fetched {len(trades)} trades total")

    # 2. Fetch auxiliary data in parallel (activity + positions)
    print("[*] Fetching activity and positions...")
    activity  = fetch_activity(TARGET)
    positions = fetch_positions(TARGET)
    print(f"[✓] Activity: {len(activity)} fields | Positions: {len(positions)}")

    # 3. Save raw data
    raw_payload = {
        "target": TARGET,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "trades_count": len(trades),
        "trades": trades,
        "activity": activity,
        "positions": positions,
    }
    with open(RAW_FILE, "w") as f:
        json.dump(raw_payload, f, indent=2, ensure_ascii=False)
    print(f"[✓] Raw data saved → {RAW_FILE}")

    # 4. Analyze
    print("[*] Analyzing...")
    analysis = analyze_trades(trades)

    # 5. Generate report
    report = generate_report(analysis, activity, positions)
    with open(REPORT_FILE, "w") as f:
        f.write(report)
    print(f"[✓] Report saved → {REPORT_FILE}")

    # 6. Print key stats
    print("\n" + "=" * 60)
    print("KEY FINDINGS")
    print("=" * 60)
    print(f"  Total trades    : {analysis.get('total_trades', 0):,}")
    print(f"  Total volume    : ${analysis.get('total_volume_usdc', 0):,.2f} USDC")
    print(f"  Avg trade size  : ${analysis.get('avg_trade_size_usdc', 0):.2f}")
    print(f"  Trade freq      : {analysis.get('trade_freq_per_min', 0):.2f} /min")
    print(f"  Unique markets  : {analysis.get('unique_markets', 0):,}")
    print(f"  Neg-risk mkts   : {analysis.get('neg_risk_markets_count', 0):,}")
    dr = analysis.get("date_range", {})
    print(f"  Period          : {dr.get('from','?')} → {dr.get('to','?')}")
    print(f"  Span            : {dr.get('span_hours', 0):.1f} hours")
    tf = analysis.get("timeframe_breakdown", {})
    for tfname in ("5m", "15m", "1h", "daily", "other"):
        if tfname in tf:
            s = tf[tfname]
            print(f"  [{tfname:5s}] trades={s['trades']:,} vol=${s['volume']:,.2f}")
    print("=" * 60)
    print("\n[✓] Analysis complete.")


if __name__ == "__main__":
    main()
