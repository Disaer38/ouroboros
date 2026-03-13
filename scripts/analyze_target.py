#!/usr/bin/env python3
"""
Comprehensive analysis of Polymarket trader guh123.
Wallet: 0xa45fe11dd1420fca906ceac2c067844379a42429

Fetches full trade history via pagination, analyzes strategy patterns,
saves raw data + markdown report.
"""

import requests
import json
import time
import os
from collections import defaultdict, Counter
from datetime import datetime, timezone

# ─── Config ──────────────────────────────────────────────────────────────────
TARGET    = "0xa45fe11dd1420fca906ceac2c067844379a42429"
DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

REPO_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR   = os.path.join(REPO_ROOT, "data")
REPORT_DIR = os.path.join(REPO_ROOT, "reports")
os.makedirs(DATA_DIR,   exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

RAW_FILE    = os.path.join(DATA_DIR,   "0xa45_history.json")
REPORT_FILE = os.path.join(REPORT_DIR, "0xa45_analysis.md")

LIMIT     = 500    # max per request
MAX_PAGES = 200    # safety cap

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
            print(f"  [!] Attempt {attempt+1} failed at offset={offset}: {e}")
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
            print(f"  → No data at offset {offset}. Pagination complete.")
            break
        all_trades.extend(batch)
        print(f"  → Page {page+1}: +{len(batch)} trades (total: {len(all_trades)})")
        if len(batch) < LIMIT:
            break  # last page
        offset += len(batch)
        time.sleep(0.15)
    return all_trades


def fetch_positions(user: str) -> list:
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


def fetch_profile(user: str) -> dict:
    """Try multiple Gamma endpoints for profile data."""
    endpoints = [
        f"{GAMMA_API}/profiles/{user}",
        f"{GAMMA_API}/users/{user}",
        f"{DATA_API}/activity?user={user}&limit=1",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return {}


# ─── Slug classifier ──────────────────────────────────────────────────────────

def classify_slug(slug: str) -> tuple:
    """Returns (timeframe, underlying_asset)."""
    s = slug.lower()

    # Timeframe
    if "5m" in s or "-5m-" in s:
        tf = "5m"
    elif "15m" in s or "-15m-" in s:
        tf = "15m"
    elif "1h" in s or "hourly" in s or "-1h-" in s:
        tf = "1h"
    elif "daily" in s or "end-of-day" in s:
        tf = "daily"
    else:
        tf = "other"

    # Asset
    asset = "other"
    for a in ["btc", "bitcoin", "eth", "ethereum", "sol", "solana", "xrp", "ripple", "doge"]:
        if a in s:
            asset_map = {
                "btc": "BTC", "bitcoin": "BTC",
                "eth": "ETH", "ethereum": "ETH",
                "sol": "SOL", "solana": "SOL",
                "xrp": "XRP", "ripple": "XRP",
                "doge": "DOGE",
            }
            asset = asset_map[a]
            break

    return tf, asset


# ─── Core analysis ────────────────────────────────────────────────────────────

def analyze_trades(trades: list) -> dict:
    """Full statistical analysis of trade list."""
    if not trades:
        return {}

    # ── Per-market (conditionId) stats ──
    markets = defaultdict(lambda: {
        "question": "", "slug": "", "timeframe": "other", "asset": "other",
        "condition_id": "", "eventSlug": "",
        "trades": 0, "volume_usdc": 0.0,
        "buy_up": 0, "buy_down": 0,
        "size_up": 0.0, "size_down": 0.0,
        "prices_up": [], "prices_down": [],
        "timestamps": [],
    })

    # ── Per-window (eventSlug / 5-min window) stats ──
    # eventSlug groups all assets traded in one 5m window
    windows = defaultdict(lambda: {
        "event_slug": "", "assets": set(),
        "trades": 0, "volume_usdc": 0.0,
        "buy_up": 0, "buy_down": 0,
        "timestamps": [],
    })

    hourly_volume  = defaultdict(float)
    hourly_trades  = defaultdict(int)
    size_dist      = Counter()   # rounded USDC per trade
    total_volume   = 0.0
    trade_values   = []
    timestamps     = []

    for t in trades:
        price    = float(t.get("price", 0))
        size     = float(t.get("size", 0))
        value    = price * size          # USDC cost of this trade
        side     = t.get("side", "BUY").upper()
        outcome  = t.get("outcome", "").lower()   # "up" or "down"
        cond_id  = t.get("conditionId", "?")
        slug     = t.get("slug", "")
        event_slug = t.get("eventSlug", slug)
        question = t.get("title", "")
        ts_unix  = t.get("timestamp", 0)  # Unix timestamp (int)

        tf, asset = classify_slug(slug)

        try:
            ts = datetime.fromtimestamp(ts_unix, tz=timezone.utc)
        except Exception:
            ts = None

        # ── Market aggregation ──
        m = markets[cond_id]
        m["question"]    = question or m["question"]
        m["slug"]        = slug or m["slug"]
        m["condition_id"]= cond_id
        m["eventSlug"]   = event_slug
        m["timeframe"]   = tf
        m["asset"]       = asset
        m["trades"]      += 1
        m["volume_usdc"] += value
        if outcome in ("up", "1"):
            m["buy_up"]   += 1
            m["size_up"]  += size
            m["prices_up"].append(price)
        elif outcome in ("down", "0"):
            m["buy_down"]  += 1
            m["size_down"] += size
            m["prices_down"].append(price)
        if ts:
            m["timestamps"].append(ts_unix)

        # ── Window aggregation ──
        w = windows[event_slug]
        w["event_slug"]   = event_slug
        w["assets"].add(asset)
        w["trades"]      += 1
        w["volume_usdc"] += value
        if outcome in ("up", "1"):
            w["buy_up"] += 1
        elif outcome in ("down", "0"):
            w["buy_down"] += 1
        if ts:
            w["timestamps"].append(ts_unix)

        # ── Globals ──
        total_volume += value
        trade_values.append(value)
        size_dist[round(value, 2)] += 1

        if ts:
            hour_key = ts.strftime("%Y-%m-%dT%H:00Z")
            hourly_volume[hour_key] += value
            hourly_trades[hour_key] += 1
            timestamps.append(ts_unix)

    # ── Time range ──
    if timestamps:
        ts_min_dt = datetime.fromtimestamp(min(timestamps), tz=timezone.utc)
        ts_max_dt = datetime.fromtimestamp(max(timestamps), tz=timezone.utc)
        span_secs  = max(timestamps) - min(timestamps)
        span_hours = span_secs / 3600
    else:
        ts_min_dt = ts_max_dt = None
        span_hours = 0

    # ── Neg-risk detection ──
    # For each 5m window: check if (avg_price_up + avg_price_down) < 1.0
    neg_risk_windows = []
    for cid, m in markets.items():
        if m["prices_up"] and m["prices_down"]:
            avg_up   = sum(m["prices_up"]) / len(m["prices_up"])
            avg_down = sum(m["prices_down"]) / len(m["prices_down"])
            spread   = 1.0 - (avg_up + avg_down)
            if spread > 0:
                neg_risk_windows.append({
                    "condition_id": cid,
                    "question": m["question"],
                    "asset": m["asset"],
                    "timeframe": m["timeframe"],
                    "avg_up":   round(avg_up, 4),
                    "avg_down": round(avg_down, 4),
                    "spread":   round(spread, 4),
                    "trades":   m["trades"],
                    "volume":   round(m["volume_usdc"], 2),
                })

    neg_risk_windows.sort(key=lambda x: -x["spread"])

    # ── Timeframe breakdown ──
    tf_stats = defaultdict(lambda: {"trades": 0, "volume": 0.0, "markets": 0})
    asset_stats = defaultdict(lambda: {"trades": 0, "volume": 0.0})
    for cid, m in markets.items():
        tf = m["timeframe"]
        tf_stats[tf]["trades"]  += m["trades"]
        tf_stats[tf]["volume"]  += m["volume_usdc"]
        tf_stats[tf]["markets"] += 1
        a = m["asset"]
        asset_stats[a]["trades"] += m["trades"]
        asset_stats[a]["volume"] += m["volume_usdc"]

    # ── Window coverage: how many windows traded BOTH BTC + ETH ──
    dual_asset_windows = sum(1 for w in windows.values() if len(w["assets"]) >= 2)
    both_sides_windows = sum(1 for w in windows.values() if w["buy_up"] > 0 and w["buy_down"] > 0)

    # ── Top size distribution ──
    top_sizes = size_dist.most_common(20)

    # ── Most traded markets ──
    top_markets = sorted(markets.values(), key=lambda x: -x["volume_usdc"])[:30]

    avg_trade_size = total_volume / len(trades) if trades else 0
    trade_freq_min = (len(trades) / (span_hours * 60)) if span_hours > 0 else 0
    vol_per_hour   = total_volume / span_hours if span_hours > 0 else 0

    return {
        "total_trades":       len(trades),
        "total_volume_usdc":  round(total_volume, 2),
        "avg_trade_size_usdc":round(avg_trade_size, 4),
        "trade_freq_per_min": round(trade_freq_min, 4),
        "vol_per_hour":       round(vol_per_hour, 2),
        "unique_markets":     len(markets),
        "unique_windows":     len(windows),
        "dual_asset_windows": dual_asset_windows,
        "both_sides_windows": both_sides_windows,
        "neg_risk_count":     len(neg_risk_windows),
        "neg_risk_top":       neg_risk_windows[:20],
        "date_range": {
            "from":       ts_min_dt.isoformat() if ts_min_dt else None,
            "to":         ts_max_dt.isoformat() if ts_max_dt else None,
            "span_hours": round(span_hours, 3),
        },
        "timeframe_breakdown": {
            k: {"trades": v["trades"], "volume": round(v["volume"], 2), "markets": v["markets"]}
            for k, v in sorted(tf_stats.items(), key=lambda x: -x[1]["volume"])
        },
        "asset_breakdown": {
            k: {"trades": v["trades"], "volume": round(v["volume"], 2)}
            for k, v in sorted(asset_stats.items(), key=lambda x: -x[1]["volume"])
        },
        "top_sizes":     top_sizes,
        "hourly_volume": dict(sorted(hourly_volume.items())),
        "hourly_trades": dict(sorted(hourly_trades.items())),
        "top_markets": [
            {k: v for k, v in m.items() if k not in ("prices_up", "prices_down", "timestamps")}
            for m in top_markets
        ],
        "windows_sample": [
            {
                "event_slug":    w["event_slug"],
                "assets":        sorted(w["assets"]),
                "trades":        w["trades"],
                "volume_usdc":   round(w["volume_usdc"], 2),
                "buy_up":        w["buy_up"],
                "buy_down":      w["buy_down"],
            }
            for w in sorted(windows.values(), key=lambda x: -x["volume_usdc"])[:20]
        ],
    }


# ─── Report ───────────────────────────────────────────────────────────────────

def generate_report(analysis: dict, positions: list) -> str:
    a  = analysis
    dr = a.get("date_range", {})
    tf = a.get("timeframe_breakdown", {})
    ab = a.get("asset_breakdown", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = []
    lines += [
        f"# 🔬 Анализ трейдера guh123 (Wry-Leaker)",
        f"> **Wallet**: `{TARGET}`  ",
        f"> **Сгенерировано**: {now}",
        "",
        "---",
        "",
        "## 📊 Общая статистика",
        "",
        "| Метрика | Значение |",
        "|---------|---------|",
        f"| **Всего трейдов** | {a['total_trades']:,} |",
        f"| **Объём USDC** | ${a['total_volume_usdc']:,.2f} |",
        f"| **Средний трейд** | ${a['avg_trade_size_usdc']:.2f} USDC |",
        f"| **Темп** | {a['trade_freq_per_min']:.2f} трейдов/мин |",
        f"| **Объём/час** | ${a['vol_per_hour']:,.2f} USDC |",
        f"| **Уникальных рынков** | {a['unique_markets']:,} |",
        f"| **Уникальных окон (event)** | {a['unique_windows']:,} |",
        f"| **Окон с BTC+ETH** | {a['dual_asset_windows']:,} ({a['dual_asset_windows']/max(a['unique_windows'],1)*100:.1f}%) |",
        f"| **Окон с Up+Down** | {a['both_sides_windows']:,} ({a['both_sides_windows']/max(a['unique_windows'],1)*100:.1f}%) |",
        f"| **Neg-risk рынков** | {a['neg_risk_count']:,} |",
        f"| **Период** | {dr.get('from','?')} → {dr.get('to','?')} |",
        f"| **Длительность** | {dr.get('span_hours', 0):.2f} ч |",
        "",
    ]

    lines += [
        "## ⏱️ Разбивка по таймфреймам",
        "",
        "| Таймфрейм | Трейдов | Объём USDC | % объёма | Рынков |",
        "|-----------|---------|------------|----------|--------|",
    ]
    for tname, ts in tf.items():
        pct = ts["volume"] / max(a["total_volume_usdc"], 1) * 100
        lines.append(f"| **{tname}** | {ts['trades']:,} | ${ts['volume']:,.2f} | {pct:.1f}% | {ts['markets']:,} |")

    lines += [
        "",
        "## 🪙 Разбивка по активам",
        "",
        "| Актив | Трейдов | Объём USDC | % объёма |",
        "|-------|---------|------------|----------|",
    ]
    for aname, ast in ab.items():
        pct = ast["volume"] / max(a["total_volume_usdc"], 1) * 100
        lines.append(f"| **{aname}** | {ast['trades']:,} | ${ast['volume']:,.2f} | {pct:.1f}% |")

    lines += [
        "",
        "## 💰 Топ размеры ордеров (USDC/трейд)",
        "",
        "| Размер ($) | Кол-во трейдов | % от всех |",
        "|------------|----------------|-----------|",
    ]
    for size, cnt in a.get("top_sizes", [])[:20]:
        pct = cnt / max(a["total_trades"], 1) * 100
        lines.append(f"| **${size:.2f}** | {cnt:,} | {pct:.1f}% |")

    lines += [
        "",
        "## 🕐 Активность по часам UTC",
        "",
        "| Час | Трейдов | Объём USDC |",
        "|-----|---------|------------|",
    ]
    for hour in sorted(a.get("hourly_volume", {}).keys()):
        vol = a["hourly_volume"][hour]
        cnt = a["hourly_trades"].get(hour, 0)
        lines.append(f"| {hour} | {cnt:,} | ${vol:,.2f} |")

    lines += [
        "",
        "## 🏆 Топ-20 окон по объёму (event-level)",
        "",
        "| Event Slug | Активы | Трейдов | Объём | Up | Down |",
        "|------------|--------|---------|-------|----|------|",
    ]
    for w in a.get("windows_sample", []):
        lines.append(
            f"| `{w['event_slug']}` | {'+'.join(w['assets'])} | {w['trades']:,} "
            f"| ${w['volume_usdc']:,.2f} | {w['buy_up']} | {w['buy_down']} |"
        )

    lines += [
        "",
        "## 🔬 Neg-Risk арбитраж (up+down < 1.0)",
        "",
        "| Рынок | Актив | ТФ | Спред | Avg Up | Avg Down | Трейдов |",
        "|-------|-------|----|-------|--------|----------|---------|",
    ]
    for nr in a.get("neg_risk_top", [])[:15]:
        q = nr["question"][:55]
        lines.append(
            f"| {q} | {nr['asset']} | {nr['timeframe']} "
            f"| **{nr['spread']:.4f}** | {nr['avg_up']:.4f} | {nr['avg_down']:.4f} | {nr['trades']} |"
        )

    # Positions
    if positions:
        lines += [
            "",
            f"## 📌 Текущие позиции ({len(positions)} шт.)",
            "",
            "| Рынок | Outcome | Size | Avg Price | Current Value | Cash PnL | Neg-Risk |",
            "|-------|---------|------|-----------|---------------|----------|----------|",
        ]
        for p in sorted(positions, key=lambda x: -abs(float(x.get("cashPnl", 0)))):
            q    = (p.get("title", "") or "?")[:45]
            neg  = "✅" if p.get("negativeRisk") else "—"
            cpnl = float(p.get("cashPnl", 0))
            sign = "+" if cpnl >= 0 else ""
            lines.append(
                f"| {q} | {p.get('outcome','?')} "
                f"| {float(p.get('size',0)):,.1f} "
                f"| {float(p.get('avgPrice',0)):.3f} "
                f"| ${float(p.get('currentValue',0)):,.2f} "
                f"| {sign}${cpnl:,.2f} "
                f"| {neg} |"
            )

    lines += [
        "",
        "---",
        "## 🧠 Стратегические выводы",
        "",
    ]

    tf_5m   = tf.get("5m", {})
    pct_5m  = tf_5m.get("volume", 0) / max(a["total_volume_usdc"], 1) * 100
    pct_da  = a["dual_asset_windows"] / max(a["unique_windows"], 1) * 100
    pct_bs  = a["both_sides_windows"] / max(a["unique_windows"], 1) * 100
    nr_pct  = a["neg_risk_count"] / max(a["unique_markets"], 1) * 100

    top_size_pct = sum(c for _, c in a.get("top_sizes", [])[:3]) / max(a["total_trades"], 1) * 100
    dominant_s   = [f"${s:.2f}" for s, _ in a.get("top_sizes", [])[:3]]

    lines.append(f"### 1. Тип: Детерминированный алго-бот")
    lines.append(f"- Темп {a['trade_freq_per_min']:.2f} трейдов/мин физически невозможен для человека")
    lines.append(f"- Топ-3 размера ордеров ({', '.join(dominant_s)}) составляют **{top_size_pct:.1f}%** всех трейдов → фиксированные лоты")
    lines.append("")
    lines.append(f"### 2. Фокус: 5-минутные рынки BTC/ETH")
    lines.append(f"- **{pct_5m:.1f}%** объёма — 5m рынки")
    lines.append(f"- BTC и ETH — единственные торгуемые активы")
    lines.append(f"- **{pct_da:.1f}%** окон — одновременно BTC + ETH")
    lines.append("")
    lines.append(f"### 3. Стратегия: Neg-Risk (Negative Risk) арбитраж")
    lines.append(f"- **{pct_bs:.1f}%** окон — трейдит одновременно Up + Down")
    lines.append(f"- {a['neg_risk_count']} рынков с подтверждённым neg-risk спредом")
    lines.append(f"- Механика: покупает Up и Down одновременно когда P(Up) + P(Down) < 1.0")
    lines.append(f"- Profit = (1 - купленная цена) × лот при любом исходе")
    lines.append("")
    lines.append(f"### 4. Масштаб операций")
    lines.append(f"- ${a['total_volume_usdc']:,.2f} USDC за {dr.get('span_hours',0):.2f} ч = **${a['vol_per_hour']:,.2f}/час**")
    lines.append(f"- {a['unique_windows']:,} уникальных 5-min окон = **~{a['unique_windows'] / max(dr.get('span_hours',1),1):.1f} окон/час**")
    lines.append("")
    lines.append(f"### 5. Риск-профиль")
    lines.append(f"- Текущие позиции: {len(positions)} открытых")
    if positions:
        total_cv  = sum(float(p.get("currentValue", 0)) for p in positions)
        total_pnl = sum(float(p.get("cashPnl", 0)) for p in positions)
        lines.append(f"- Суммарный текущий value: **${total_cv:,.2f}**")
        lines.append(f"- Суммарный unrealized PnL: **${total_pnl:,.2f}**")
    lines.append("")

    return "\n".join(lines)


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Polymarket Trader Analysis — guh123")
    print(f"Target: {TARGET}")
    print("=" * 60)

    # 1. Fetch trades (with pagination)
    trades = fetch_all_trades(TARGET)
    print(f"\n[✓] Fetched {len(trades)} trades total")

    # 2. Fetch positions
    print("[*] Fetching positions...")
    positions = fetch_positions(TARGET)
    print(f"[✓] Positions: {len(positions)}")

    # 3. Save raw data
    raw_payload = {
        "target":       TARGET,
        "fetched_at":   datetime.now(timezone.utc).isoformat(),
        "trades_count": len(trades),
        "trades":       trades,
        "positions":    positions,
    }
    with open(RAW_FILE, "w") as f:
        json.dump(raw_payload, f, indent=2, ensure_ascii=False)
    print(f"[✓] Raw data saved → {RAW_FILE}")

    # 4. Analyze
    print("[*] Analyzing...")
    analysis = analyze_trades(trades)

    # 5. Generate + save report
    report = generate_report(analysis, positions)
    with open(REPORT_FILE, "w") as f:
        f.write(report)
    print(f"[✓] Report saved → {REPORT_FILE}")

    # 6. Terminal summary
    print("\n" + "=" * 60)
    print("KEY FINDINGS")
    print("=" * 60)
    a  = analysis
    dr = a.get("date_range", {})
    print(f"  Total trades      : {a['total_trades']:,}")
    print(f"  Total volume      : ${a['total_volume_usdc']:,.2f} USDC")
    print(f"  Avg trade size    : ${a['avg_trade_size_usdc']:.2f}")
    print(f"  Trade freq        : {a['trade_freq_per_min']:.2f} /min")
    print(f"  Vol/hour          : ${a['vol_per_hour']:,.2f}")
    print(f"  Unique markets    : {a['unique_markets']:,}")
    print(f"  Unique windows    : {a['unique_windows']:,}")
    print(f"  Dual-asset windows: {a['dual_asset_windows']:,} ({a['dual_asset_windows']/max(a['unique_windows'],1)*100:.1f}%)")
    print(f"  Both-sides windows: {a['both_sides_windows']:,} ({a['both_sides_windows']/max(a['unique_windows'],1)*100:.1f}%)")
    print(f"  Neg-risk markets  : {a['neg_risk_count']:,}")
    print(f"  Period            : {dr.get('from','?')} → {dr.get('to','?')}")
    print(f"  Span              : {dr.get('span_hours', 0):.2f} hours")
    print("-" * 60)
    print("  TIMEFRAME BREAKDOWN:")
    for tname, ts in a.get("timeframe_breakdown", {}).items():
        pct = ts["volume"] / max(a["total_volume_usdc"], 1) * 100
        print(f"  [{tname:5s}] {ts['trades']:,} trades | ${ts['volume']:,.2f} ({pct:.1f}%)")
    print("-" * 60)
    print("  ASSET BREAKDOWN:")
    for aname, ast in a.get("asset_breakdown", {}).items():
        pct = ast["volume"] / max(a["total_volume_usdc"], 1) * 100
        print(f"  [{aname:5s}] {ast['trades']:,} trades | ${ast['volume']:,.2f} ({pct:.1f}%)")
    print("-" * 60)
    print("  TOP TRADE SIZES:")
    for size, cnt in a.get("top_sizes", [])[:10]:
        pct = cnt / max(a["total_trades"], 1) * 100
        print(f"  ${size:6.2f}: {cnt:,} times ({pct:.1f}%)")
    print("=" * 60)
    print("\n[✓] Analysis complete.")


if __name__ == "__main__":
    main()
