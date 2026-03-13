#!/usr/bin/env python3
"""
Polymarket MM Paper Trader
Strategy: Market Making on 5-min binary UP/DOWN markets
Based on: vague-sourdough behavior analysis

Assets: BTC, ETH, SOL, XRP
Entry:  buy both sides when ask_up + ask_dn < 0.97
        OR single-side when ask < 0.42 (skewed market, cheap side)
Exit:   sell when bid > entry * 1.05, or at expiry (collect $1.00)
"""

import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROXY = {
    "http":  "socks5h://127.0.0.1:9050",
    "https": "socks5h://127.0.0.1:9050",
}
ASSETS = ["btc", "eth", "sol", "xrp"]

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL  = "https://clob.polymarket.com"

CYCLE_INTERVAL_S  = 5      # main loop cadence
STATUS_INTERVAL_S = 30     # portfolio heartbeat
MAX_RETRIES       = 3
REQUEST_TIMEOUT   = 12

# Position sizing
MIN_TRADE_USDC = 5.0
MAX_TRADE_USDC = 20.0
DEFAULT_TRADE_USDC = 15.0

# Strategy thresholds
NEG_RISK_THRESHOLD   = 0.97   # buy both when ask_up + ask_dn < this
SINGLE_SIDE_MAX_ASK  = 0.50   # buy single side when ask < this
SINGLE_SIDE_MIN_BID  = 0.35   # only if bid > this (confirms real market)
PROFIT_TARGET_PCT    = 0.05   # sell when bid > entry * (1 + this)
MIN_SECS_TO_EXPIRY   = 45     # don't open new positions if < 45s left

# Paths
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_LOG    = os.path.join(_REPO_ROOT, "logs", "mm_bot_trades.csv")

CSV_HEADERS = [
    "timestamp", "action", "asset", "outcome",
    "price", "shares", "usdc", "balance", "pnl", "reason",
]

# ---------------------------------------------------------------------------
# ANSI colours (degrade gracefully if not a tty)
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def _green(t):  return _c("32", t)
def _red(t):    return _c("31", t)
def _yellow(t): return _c("33", t)
def _cyan(t):   return _c("36", t)
def _bold(t):   return _c("1",  t)
def _dim(t):    return _c("2",  t)

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")

def _ts_label() -> str:
    return _dim(f"[{_now_hms()}]")

def _fmt_time(secs: float) -> str:
    s = max(0, int(secs))
    m, s = divmod(s, 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"

def _floor5m(ts: int) -> int:
    return (ts // 300) * 300


# ---------------------------------------------------------------------------
# CSV trade logger
# ---------------------------------------------------------------------------

class TradeLogger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        exists = os.path.isfile(path)
        self._fh  = open(path, "a", newline="")
        self._csv = csv.DictWriter(self._fh, fieldnames=CSV_HEADERS)
        if not exists:
            self._csv.writeheader()
            self._fh.flush()

    def log(self, action: str, asset: str, outcome: str,
            price: float, shares: float, usdc: float,
            balance: float, pnl: float, reason: str) -> None:
        row = {
            "timestamp": _now_iso(),
            "action":    action,
            "asset":     asset,
            "outcome":   outcome,
            "price":     round(price,   4),
            "shares":    round(shares,  4),
            "usdc":      round(usdc,    4),
            "balance":   round(balance, 4),
            "pnl":       round(pnl,     4),
            "reason":    reason,
        }
        self._csv.writerow(row)
        self._fh.flush()

    def close(self):
        self._fh.close()


# ---------------------------------------------------------------------------
# Paper Exchange
# ---------------------------------------------------------------------------

class PaperExchange:
    """Virtual exchange — tracks balance, open positions, realized PnL."""

    def __init__(self, balance: float = 1000.0, logger: Optional[TradeLogger] = None):
        self.balance       = balance
        self.positions: dict = {}   # token_id -> position dict
        self.trades:    list = []
        self._realized_pnl = 0.0
        self._logger       = logger

    # ------------------------------------------------------------------
    def buy(self, token_id: str, outcome: str, price: float,
            shares: float, slug: str, asset: str = "") -> bool:
        """
        Buy `shares` of `token_id` at `price` each.
        Returns True on success.
        """
        cost = price * shares
        if cost > self.balance:
            print(f"{_ts_label()} {_yellow('⚠  Insufficient balance')} "
                  f"(${self.balance:.2f} < ${cost:.2f}) — skip {outcome}")
            return False

        self.balance -= cost

        if token_id in self.positions:
            # average-in
            pos = self.positions[token_id]
            total_shares = pos["shares"] + shares
            pos["avg_price"] = (pos["avg_price"] * pos["shares"] + price * shares) / total_shares
            pos["shares"]    = total_shares
            pos["usdc_in"]  += cost
        else:
            self.positions[token_id] = {
                "token_id":  token_id,
                "outcome":   outcome,
                "slug":      slug,
                "asset":     asset,
                "shares":    shares,
                "avg_price": price,
                "usdc_in":   cost,
            }

        record = {
            "action": "BUY", "token_id": token_id, "outcome": outcome,
            "price": price, "shares": shares, "usdc": cost,
            "balance": self.balance, "slug": slug,
        }
        self.trades.append(record)
        if self._logger:
            self._logger.log("BUY", asset, outcome, price, shares, cost,
                             self.balance, 0.0, f"slug={slug}")
        return True

    # ------------------------------------------------------------------
    def sell(self, token_id: str, outcome: str, price: float,
             shares: float, slug: str, asset: str = "") -> bool:
        """
        Sell `shares` of `token_id` at `price`. Returns True on success.
        """
        pos = self.positions.get(token_id)
        if pos is None:
            return False
        shares = min(shares, pos["shares"])

        proceeds = price * shares
        cost_basis = pos["avg_price"] * shares
        pnl = proceeds - cost_basis

        self.balance       += proceeds
        self._realized_pnl += pnl
        pos["shares"]      -= shares
        pos["usdc_in"]     -= cost_basis

        if pos["shares"] <= 1e-9:
            del self.positions[token_id]

        record = {
            "action": "SELL", "token_id": token_id, "outcome": outcome,
            "price": price, "shares": shares, "usdc": proceeds,
            "pnl": pnl, "balance": self.balance, "slug": slug,
        }
        self.trades.append(record)
        if self._logger:
            self._logger.log("SELL", asset, outcome, price, shares, proceeds,
                             self.balance, pnl, f"slug={slug}")
        return True

    # ------------------------------------------------------------------
    def settle_at_expiry(self, token_id: str, won: bool,
                         slug: str, asset: str = "") -> float:
        """
        Resolve a position at market expiry.
        winner pays $1.00/share; loser pays $0.00/share.
        Returns realized PnL for this position.
        """
        pos = self.positions.pop(token_id, None)
        if pos is None:
            return 0.0

        settle_price = 1.0 if won else 0.0
        proceeds     = settle_price * pos["shares"]
        pnl          = proceeds - pos["usdc_in"]
        self.balance       += proceeds
        self._realized_pnl += pnl

        outcome  = pos["outcome"]
        result   = _green("WIN") if won else _red("LOSS")
        pnl_str  = _green(f"+${pnl:.2f}") if pnl >= 0 else _red(f"-${abs(pnl):.2f}")
        print(f"{_ts_label()} {asset:<4} EXPIRY {result}  "
              f"{outcome}  {pos['shares']:.2f}sh  PnL={pnl_str}  "
              f"Bal={_bold(f'${self.balance:.2f}')}")

        if self._logger:
            self._logger.log("SETTLE", asset, outcome, settle_price,
                             pos["shares"], proceeds, self.balance, pnl,
                             f"won={won} slug={slug}")
        return pnl

    # ------------------------------------------------------------------
    def get_pnl(self) -> float:
        return self._realized_pnl

    def print_status(self):
        print(
            f"{_ts_label()} {_bold('Portfolio')}  "
            f"bal={_bold(f'${self.balance:.2f}')}  "
            f"open={len(self.positions)}  "
            f"realized={_green(f'+${self._realized_pnl:.2f}') if self._realized_pnl >= 0 else _red(f'${self._realized_pnl:.2f}')}"
        )
        for tid, pos in self.positions.items():
            print(f"  {_dim('|')} {pos['asset']:<4} {pos['outcome']:<5}  "
                  f"{pos['shares']:.2f}sh @ ${pos['avg_price']:.3f}  "
                  f"in=${pos['usdc_in']:.2f}  slug={pos['slug']}")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _make_session() -> requests.Session:
    s = requests.Session()
    s.proxies.update(PROXY)
    s.headers["User-Agent"] = "ouroboros-mm-bot/2.0"
    return s


def _get(session: requests.Session, url: str,
         params: Optional[dict] = None, retries: int = MAX_RETRIES) -> Optional[dict]:
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.ConnectionError as e:
            if attempt == retries:
                print(f"{_ts_label()} {_red('ConnError')} {url.split('/')[-1]}: {e}")
            else:
                time.sleep(1.5 * attempt)
        except requests.exceptions.Timeout:
            if attempt == retries:
                print(f"{_ts_label()} {_red('Timeout')} {url}")
            else:
                time.sleep(2)
        except requests.exceptions.HTTPError as e:
            print(f"{_ts_label()} {_red('HTTP')} {e}")
            return None
        except (json.JSONDecodeError, ValueError) as e:
            print(f"{_ts_label()} {_red('JSON')} {url}: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# MM Engine
# ---------------------------------------------------------------------------

class MMEngine:

    def __init__(self, exchange: PaperExchange):
        self.exchange  = exchange
        self.session   = _make_session()
        # active_markets: asset -> market info dict
        self.active_markets: dict = {}
        self._last_status_ts: float = 0.0
        self._cycle: int = 0

    # ------------------------------------------------------------------ discovery

    def find_active_market(self, asset: str) -> Optional[dict]:
        """
        Discover the current 5-min market for `asset`.
        Returns dict with keys: conditionId, slug, token_up, token_dn, expiry_ts
        or None on failure.
        """
        now_ts     = int(time.time())
        candle_ts  = _floor5m(now_ts)
        expiry_ts  = candle_ts + 300
        slug       = f"{asset}-updown-5m-{candle_ts}"

        # 1. Gamma: resolve slug → conditionId
        data = _get(self.session, f"{GAMMA_URL}/markets", {"slug": slug})
        if not data:
            return None
        markets = data if isinstance(data, list) else [data]
        if not markets:
            return None
        m = markets[0]
        condition_id = m.get("conditionId") or m.get("condition_id", "")
        if not condition_id:
            return None

        # 2. CLOB: resolve conditionId → token IDs
        clob = _get(self.session, f"{CLOB_URL}/markets/{condition_id}")
        if not clob:
            return None

        tokens = clob.get("tokens", [])
        token_up = token_dn = None
        for t in tokens:
            outcome = (t.get("outcome") or "").strip().lower()
            tid     = t.get("token_id") or t.get("tokenId") or ""
            if not tid:
                continue
            if outcome == "up":
                token_up = tid
            elif outcome == "down":
                token_dn = tid

        if not token_up or not token_dn:
            return None

        return {
            "conditionId": condition_id,
            "slug":        slug,
            "token_up":    token_up,
            "token_dn":    token_dn,
            "expiry_ts":   expiry_ts,
            "asset":       asset.upper(),
        }

    # ------------------------------------------------------------------ orderbook

    def get_orderbook(self, token_id: str) -> Optional[dict]:
        """
        Fetch CLOB orderbook for a token.
        Returns dict: {best_bid, best_ask, bids, asks}
        """
        data = _get(self.session, f"{CLOB_URL}/book", {"token_id": token_id})
        if not data:
            return None

        bids_raw = data.get("bids", [])
        asks_raw = data.get("asks", [])

        def parse_levels(levels):
            out = []
            for lvl in levels:
                try:
                    p = float(lvl.get("price", 0))
                    s = float(lvl.get("size",  0))
                    if p > 0 and s > 0:
                        out.append((p, s))
                except (TypeError, ValueError):
                    pass
            return out

        bids = sorted(parse_levels(bids_raw), key=lambda x: -x[0])  # descending
        asks = sorted(parse_levels(asks_raw), key=lambda x:  x[0])  # ascending

        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bids":     bids,
            "asks":     asks,
        }

    # ------------------------------------------------------------------ spread analysis

    def analyze_spread(self, ob_up: dict, ob_dn: dict) -> dict:
        """
        Given orderbooks for UP and DOWN tokens, decide what to do.

        Returns dict:
          should_trade: bool
          side: 'both' | 'up' | 'down' | None
          entry_price_up: float | None
          entry_price_dn: float | None
          reason: str
        """
        result = {
            "should_trade":    False,
            "side":            None,
            "entry_price_up":  None,
            "entry_price_dn":  None,
            "reason":          "",
        }

        ask_up = ob_up.get("best_ask")
        ask_dn = ob_dn.get("best_ask")
        bid_up = ob_up.get("best_bid")
        bid_dn = ob_dn.get("best_bid")

        if ask_up is None or ask_dn is None:
            result["reason"] = "missing ask"
            return result

        # ── Strategy 1: neg-risk arb — buy both sides when sum < threshold ──
        if ask_up + ask_dn < NEG_RISK_THRESHOLD:
            result.update({
                "should_trade":   True,
                "side":           "both",
                "entry_price_up": ask_up,
                "entry_price_dn": ask_dn,
                "reason":         f"neg-risk ask_up+ask_dn={ask_up+ask_dn:.3f}<{NEG_RISK_THRESHOLD}",
            })
            return result

        # ── Strategy 2: buy cheap side when the market is skewed ──
        # If ask_up < 0.50 and it's meaningfully cheaper than the "fair" 0.50
        # Edge = (1 - ask_up) - ask_dn  ... if positive, UP is undervalued
        # Or: edge_up = (1.0 - ask_up - ask_dn)  -- same thing, positive means sum < 1.0
        # But also: if ask is simply < 0.42 it's a directional bet worth taking

        edge_up = 1.0 - ask_up - ask_dn  # positive means neg-risk opportunity
        edge_dn = 1.0 - ask_dn - ask_up  # same thing, symmetric

        # Single-side: buy UP if ask_up < 0.42 (strong DOWN skew, UP is cheap)
        if ask_up < 0.42 and ask_up > 0.05 and bid_up > 0.02:
            return {
                "should_trade": True,
                "side": "UP",
                "entry_price_up": ask_up,
                "entry_price_dn": 0.0,
                "reason": f"cheap-UP ask={ask_up:.3f} (market skewed DOWN={ask_dn:.3f})"
            }

        # Single-side: buy DN if ask_dn < 0.42 (strong UP skew, DN is cheap)
        if ask_dn < 0.42 and ask_dn > 0.05 and bid_dn > 0.02:
            return {
                "should_trade": True,
                "side": "DN",
                "entry_price_up": 0.0,
                "entry_price_dn": ask_dn,
                "reason": f"cheap-DN ask={ask_dn:.3f} (market skewed UP={ask_up:.3f})"
            }

        # ── No signal ──
        result["reason"] = (
            f"no-signal ask_up={ask_up:.3f} ask_dn={ask_dn:.3f} "
            f"sum={ask_up+ask_dn:.3f}"
        )
        return result

    # ------------------------------------------------------------------ exit check

    def _check_exit(self, mkt: dict) -> None:
        """
        For each open position tied to this market, check sell conditions:
        - current bid > entry * (1 + PROFIT_TARGET_PCT)
        - expiry imminent (< 10s) → sell at current bid (or 0 if no bid)
        """
        slug     = mkt["slug"]
        asset    = mkt["asset"]
        expiry   = mkt["expiry_ts"]
        secs_left = expiry - time.time()

        tokens_to_check = [
            ("up",   mkt["token_up"]),
            ("down", mkt["token_dn"]),
        ]

        for side, tid in tokens_to_check:
            pos = self.exchange.positions.get(tid)
            if pos is None:
                continue

            # Expiry settlement — sell at $1 or $0 based on resolution
            # We don't know winner yet; for paper purposes at expiry we
            # let run_one_cycle handle it via expire_positions.
            if secs_left <= 10:
                continue  # handled by expire_positions

            # Fetch current bid for this token
            ob = self.get_orderbook(tid)
            if ob is None:
                continue
            cur_bid = ob.get("best_bid")
            if cur_bid is None:
                continue

            entry = pos["avg_price"]
            if cur_bid > entry * (1 + PROFIT_TARGET_PCT):
                shares = pos["shares"]
                ok = self.exchange.sell(tid, pos["outcome"], cur_bid, shares, slug, asset)
                if ok:
                    pnl_est = (cur_bid - entry) * shares
                    print(
                        f"{_ts_label()} {_green('SELL')} {asset} {side.upper()}  "
                        f"{shares:.2f}sh @ {_green(f'${cur_bid:.3f}')}  "
                        f"entry=${entry:.3f}  PnL≈{_green(f'+${pnl_est:.2f}')}"
                    )

    # ------------------------------------------------------------------ expiry

    def expire_positions(self, mkt: dict) -> None:
        """
        Called when the market clock has expired. Attempts to determine winner
        via CLOB; falls back to settling both sides at $0.50 if unresolved.
        """
        slug  = mkt["slug"]
        asset = mkt["asset"]
        cid   = mkt["conditionId"]

        clob = _get(self.session, f"{CLOB_URL}/markets/{cid}")
        winner_outcome = None  # 'up' or 'down'

        if clob:
            for t in clob.get("tokens", []):
                if t.get("winner") is True:
                    winner_outcome = (t.get("outcome") or "").strip().lower()
                    break

        for side, tid in [("up", mkt["token_up"]), ("down", mkt["token_dn"])]:
            pos = self.exchange.positions.get(tid)
            if pos is None:
                continue

            if winner_outcome:
                won = (side == winner_outcome)
                self.exchange.settle_at_expiry(tid, won, slug, asset)
            else:
                # Resolution unknown — book out at mid ($0.50) to avoid hanging
                mid_price = 0.50
                shares    = pos["shares"]
                self.exchange.sell(tid, pos["outcome"], mid_price, shares, slug, asset)
                print(f"{_ts_label()} {_yellow('EXPIRE-UNSETTLED')} {asset} {side}  "
                      f"booked out @ $0.50")

    # ------------------------------------------------------------------ one asset cycle

    def run_one_cycle(self, asset: str) -> None:
        mkt = self.active_markets.get(asset)
        now = time.time()

        # ── Refresh market if missing or expired ──────────────────────────
        if mkt is None or now >= mkt["expiry_ts"]:
            if mkt and now >= mkt["expiry_ts"]:
                self.expire_positions(mkt)
            new_mkt = self.find_active_market(asset)
            if new_mkt:
                self.active_markets[asset] = new_mkt
                mkt = new_mkt
                print(f"{_ts_label()} {_cyan('MARKET')} {asset}  "
                      f"slug={mkt['slug']}  "
                      f"expires in {_fmt_time(mkt['expiry_ts'] - now)}")
            else:
                print(f"{_ts_label()} {_dim(asset)} no market found")
                return

        secs_left = mkt["expiry_ts"] - now
        slug      = mkt["slug"]

        # ── Check exits on existing positions ────────────────────────────
        self._check_exit(mkt)

        # ── Don't open new positions if too close to expiry ──────────────
        if secs_left < MIN_SECS_TO_EXPIRY:
            print(f"{_ts_label()} {_dim(asset)} {_fmt_time(secs_left)} left — no new entries")
            return

        # ── Fetch orderbooks ─────────────────────────────────────────────
        ob_up = self.get_orderbook(mkt["token_up"])
        ob_dn = self.get_orderbook(mkt["token_dn"])

        if ob_up is None or ob_dn is None:
            print(f"{_ts_label()} {_dim(asset)} orderbook unavailable")
            return

        ask_up = ob_up.get("best_ask", 0) or 0
        ask_dn = ob_dn.get("best_ask", 0) or 0
        bid_up = ob_up.get("best_bid", 0) or 0
        bid_dn = ob_dn.get("best_bid", 0) or 0

        # ── Print tick ───────────────────────────────────────────────────
        has_pos_up = mkt["token_up"] in self.exchange.positions
        has_pos_dn = mkt["token_dn"] in self.exchange.positions
        pos_tag = ""
        if has_pos_up: pos_tag += _green(" [UP]")
        if has_pos_dn: pos_tag += _green(" [DN]")

        print(
            f"{_ts_label()} {_bold(asset):<8}  "
            f"UP  bid={bid_up:.3f} ask={_cyan(f'{ask_up:.3f}')}  "
            f"DN  bid={bid_dn:.3f} ask={_cyan(f'{ask_dn:.3f}')}  "
            f"sum={ask_up+ask_dn:.3f}  "
            f"{_fmt_time(secs_left)}{pos_tag}"
        )

        # ── Skip if both sides already held ──────────────────────────────
        if has_pos_up and has_pos_dn:
            return

        # ── Spread analysis ──────────────────────────────────────────────
        signal = self.analyze_spread(ob_up, ob_dn)
        if not signal["should_trade"]:
            return

        side = signal["side"]

        # ── Execute entries ───────────────────────────────────────────────
        if side in ("both", "up", "UP") and not has_pos_up:
            price  = signal["entry_price_up"]
            if price and price > 0:
                shares = round(DEFAULT_TRADE_USDC / price, 4)
                usdc   = price * shares
                ok     = self.exchange.buy(mkt["token_up"], "Up", price, shares, slug, asset)
                if ok:
                    print(
                        f"{_ts_label()} {_green('BUY')} {asset} UP  "
                        f"{shares:.2f}sh @ {_green(f'${price:.3f}')}  "
                        f"=${usdc:.2f}  [{signal['reason']}]"
                    )

        if side in ("both", "down", "DN") and not has_pos_dn:
            price  = signal["entry_price_dn"]
            if price and price > 0:
                shares = round(DEFAULT_TRADE_USDC / price, 4)
                usdc   = price * shares
                ok     = self.exchange.buy(mkt["token_dn"], "Down", price, shares, slug, asset)
                if ok:
                    print(
                        f"{_ts_label()} {_green('BUY')} {asset} DN  "
                        f"{shares:.2f}sh @ {_green(f'${price:.3f}')}  "
                        f"=${usdc:.2f}  [{signal['reason']}]"
                    )

    # ------------------------------------------------------------------ main loop

    def run(self) -> None:
        print("=" * 70)
        print(_bold("  Ouroboros MM Paper Trader — Market Making Strategy"))
        print("=" * 70)
        print(f"  Balance         : $1000.00 USDC")
        print(f"  Assets          : {', '.join(a.upper() for a in ASSETS)}")
        print(f"  Neg-risk entry  : ask_up + ask_dn < {NEG_RISK_THRESHOLD}")
        print(f"  Single-side     : ask < {SINGLE_SIDE_MAX_ASK}, bid > {SINGLE_SIDE_MIN_BID}")
        print(f"  Profit target   : {PROFIT_TARGET_PCT*100:.0f}% above entry")
        print(f"  Trade size      : ${DEFAULT_TRADE_USDC:.0f} USDC/side")
        print(f"  Cycle interval  : {CYCLE_INTERVAL_S}s")
        print(f"  Tor proxy       : socks5h://127.0.0.1:9050")
        print(f"  CSV log         : {CSV_LOG}")
        print("=" * 70)
        print("Press Ctrl+C to stop.\n")

        logger   = TradeLogger(CSV_LOG)
        self.exchange._logger = logger

        try:
            while True:
                self._cycle += 1
                cycle_start  = time.time()

                # Run one cycle per asset
                for asset in ASSETS:
                    try:
                        self.run_one_cycle(asset)
                    except Exception as exc:
                        print(f"{_ts_label()} {_red('ERROR')} {asset}: {exc}")

                # Portfolio heartbeat
                now = time.time()
                if now - self._last_status_ts >= STATUS_INTERVAL_S:
                    print()
                    self.exchange.print_status()
                    print()
                    self._last_status_ts = now

                # Pace the loop
                elapsed = time.time() - cycle_start
                sleep   = max(0.0, CYCLE_INTERVAL_S - elapsed)
                if sleep > 0:
                    time.sleep(sleep)

        except KeyboardInterrupt:
            print("\nStopped by user.\n")
        finally:
            logger.close()
            print("=" * 70)
            print(_bold("  Final Summary"))
            print("=" * 70)
            self.exchange.print_status()
            pnl = self.exchange.get_pnl()
            pnl_str = _green(f"+${pnl:.2f}") if pnl >= 0 else _red(f"-${abs(pnl):.2f}")
            print(f"\n  Realized PnL : {pnl_str}")
            print(f"  Total cycles : {self._cycle}")
            print(f"  Trade log    : {CSV_LOG}")
            print("=" * 70)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    engine = MMEngine(PaperExchange(balance=1000.0))
    engine.run()
