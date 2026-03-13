#!/usr/bin/env python3
"""
Polymarket Live Spread Monitor — 5-min BTC/ETH/SOL crypto markets.
All HTTP traffic is routed through Tor SOCKS5 (127.0.0.1:9050).
"""
import csv
import os
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROXIES = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_URL  = "https://clob.polymarket.com/book"
KEYWORDS  = ("BTC", "ETH", "SOL")
DIR_WORDS = ("up", "down", "Up", "Down")
POLL_SEC  = 5

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH   = os.path.join(_REPO_ROOT, "logs", "spread_log.csv")
CSV_COLS   = ["timestamp", "market", "token_id", "best_bid", "best_ask",
              "spread", "mid_price", "time_to_expiry_sec"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_session() -> requests.Session:
    s = requests.Session()
    s.proxies.update(PROXIES)
    s.headers["User-Agent"] = "ouroboros-spread-monitor/1.0"
    return s


def init_csv() -> None:
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as fh:
            csv.writer(fh).writerow(CSV_COLS)


def append_row(row: dict) -> None:
    with open(CSV_PATH, "a", newline="") as fh:
        csv.DictWriter(fh, fieldnames=CSV_COLS).writerow(row)

# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

def find_markets(session: requests.Session) -> list:
    """Return active 5-min BTC/ETH/SOL markets expiring within 10 minutes."""
    now = datetime.now(timezone.utc)
    resp = session.get(GAMMA_URL,
                       params={"active": "true", "closed": "false", "limit": 200},
                       timeout=15)
    resp.raise_for_status()
    markets = []
    for m in resp.json():
        q = m.get("question") or ""
        if not any(k in q for k in KEYWORDS):
            continue
        if not any(d in q for d in DIR_WORDS):
            continue
        end_str = m.get("endDate") or ""
        if not end_str:
            continue
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        secs_left = (end_dt - now).total_seconds()
        if not (0 < secs_left <= 600):
            continue
        tokens = m.get("tokens") or []
        if not tokens:
            continue
        markets.append({"question": q, "end_dt": end_dt,
                         "secs_left": secs_left, "tokens": tokens})
    return markets

# ---------------------------------------------------------------------------
# Orderbook
# ---------------------------------------------------------------------------

def get_book(token_id: str, session: requests.Session) -> dict | None:
    resp = session.get(CLOB_URL, params={"token_id": token_id}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    bids = sorted([float(x["price"]) for x in data.get("bids", [])
                   if float(x.get("size", 0)) > 0], reverse=True)
    asks = sorted([float(x["price"]) for x in data.get("asks", [])
                   if float(x.get("size", 0)) > 0])
    if not bids or not asks:
        return None
    bb, ba = bids[0], asks[0]
    return {"best_bid": bb, "best_ask": ba,
            "spread": ba - bb, "mid": (bb + ba) / 2}

# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

def poll(session: requests.Session) -> None:
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_local = datetime.now().strftime("%H:%M:%S")

    markets = find_markets(session)
    if not markets:
        print(f"[{now_local}] No active 5-min markets found within 10 min window.")
        return

    for m in markets:
        yes_token = m["tokens"][0]
        token_id  = str(yes_token.get("token_id") or yes_token.get("tokenId") or "")
        if not token_id:
            continue
        book = get_book(token_id, session)
        if book is None:
            print(f"[{now_local}] {m['question'][:50]} | empty orderbook — skip")
            continue

        label = m["question"][:50]
        print(
            f"[{now_local}] {label:<50} | "
            f"bid={book['best_bid']:.3f}  ask={book['best_ask']:.3f}  "
            f"spread={book['spread']:.4f}  mid={book['mid']:.3f}  "
            f"expires={m['secs_left']:.0f}s"
        )
        append_row({
            "timestamp":         ts,
            "market":            m["question"],
            "token_id":          token_id,
            "best_bid":          book["best_bid"],
            "best_ask":          book["best_ask"],
            "spread":            round(book["spread"], 6),
            "mid_price":         round(book["mid"], 6),
            "time_to_expiry_sec": round(m["secs_left"], 1),
        })

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Polymarket Spread Monitor  |  Tor proxy: 127.0.0.1:9050")
    print(f"CSV log: {CSV_PATH}\nPress Ctrl+C to stop.\n")
    init_csv()
    session = get_session()
    try:
        while True:
            try:
                poll(session)
            except requests.exceptions.ConnectionError as exc:
                print(f"Connection error: {exc} — retrying in 10s")
                time.sleep(10)
                continue
            except Exception as exc:
                print(f"Error: {exc}")
            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
