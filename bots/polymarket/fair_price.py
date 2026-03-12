"""
Fair price estimator for BTC binary options and Up/Down directional markets.

Uses real-time Binance BTC/USDT price + rolling volatility to compute:
  - P_fair = P(BTC > K at expiry) via Black-Scholes binary call formula
  - P_up   = P(BTC higher at end of window than start) via GBM + momentum
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BINANCE_URL = "https://api.binance.us/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.us/api/v3/klines"
KRAKEN_URL = "https://api.kraken.com/0/public/Ticker"
KRAKEN_KLINES_URL = "https://api.kraken.com/0/public/OHLC"
SYMBOL = "BTCUSDT"
VOLATILITY_WINDOW = 20   # number of price samples for rolling σ
MOMENTUM_HISTORY_SIZE = 300  # max (ts, price) pairs stored for momentum (≈50min at 10s/sample)
MIN_P_FAIR = 0.02
MAX_P_FAIR = 0.98


@dataclass
class PriceState:
    """Rolling BTC price tracker with volatility estimation."""
    prices: deque = field(default_factory=lambda: deque(maxlen=VOLATILITY_WINDOW))
    # Timestamped price history for momentum computation: deque of (ts, price) tuples
    momentum_history: deque = field(default_factory=lambda: deque(maxlen=MOMENTUM_HISTORY_SIZE))
    last_price: float = 0.0
    last_fetch_ts: float = 0.0

    @property
    def sigma(self) -> float:
        """Per-sample σ of log returns. Returns 0 if insufficient data."""
        if len(self.prices) < 3:
            return 0.0
        log_returns = [
            math.log(self.prices[i] / self.prices[i - 1])
            for i in range(1, len(self.prices))
        ]
        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
        return math.sqrt(variance)

    def sigma_annualized(self, sample_interval_seconds: float = 10.0) -> float:
        """
        Annualized volatility.

        If insufficient data, returns default (0.80 = 80% annualized, typical BTC).
        Otherwise scales per-sample σ to annual units.
        """
        per_sample = self.sigma
        if per_sample == 0.0:
            return 0.80  # default: 80% annualized (typical BTC)
        samples_per_year = (365 * 24 * 3600) / sample_interval_seconds
        return per_sample * math.sqrt(samples_per_year)

    def momentum_return(self, lookback_seconds: float = 60.0) -> float:
        """
        Log return of BTC price over the last `lookback_seconds`.

        Returns 0.0 if insufficient history.
        """
        if not self.momentum_history or self.last_price <= 0:
            return 0.0
        cutoff = time.time() - lookback_seconds
        # Find oldest sample within lookback window
        ref_price = None
        for ts, price in self.momentum_history:
            if ts >= cutoff:
                ref_price = price
                break
        if ref_price is None or ref_price <= 0:
            return 0.0
        return math.log(self.last_price / ref_price)


_state = PriceState()


def bootstrap_from_kraken_ohlc(symbol: str = "XBTUSD", interval: int = 1, limit: int = 20) -> bool:
    """
    Seed price history from Kraken OHLC data.
    Call as fallback when Binance bootstrap fails.

    Returns True on success, False on failure.
    """
    try:
        resp = requests.get(
            KRAKEN_KLINES_URL,
            params={"pair": symbol, "interval": interval},
            timeout=10.0,
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        # Kraken OHLC entry: [time, open, high, low, close, vwap, volume, count]
        entries = result["XXBTZUSD"][-limit:]
        now = time.time()
        n = len(entries)
        for i, entry in enumerate(entries):
            close_price = float(entry[4])
            approx_ts = now - (n - 1 - i) * interval * 60.0
            _state.prices.append(close_price)
            _state.momentum_history.append((approx_ts, close_price))
            _state.last_price = close_price
        _state.last_fetch_ts = now
        logger.info(
            f"Bootstrapped from Kraken OHLC with {n} candles. "
            f"σ_annual={_state.sigma_annualized():.2%} | last_price=${_state.last_price:,.0f}"
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to bootstrap from Kraken OHLC: {e}")
        return False


def bootstrap_from_klines(symbol: str = SYMBOL, interval: str = "1m", limit: int = 20) -> bool:
    """
    Seed price history from Binance klines (1-minute candles).
    Call once at startup to immediately have meaningful σ and momentum history.
    Falls back to Kraken OHLC if Binance fails.

    Returns True on success, False on failure.
    """
    try:
        resp = requests.get(
            BINANCE_KLINES_URL,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10.0,
        )
        resp.raise_for_status()
        klines = resp.json()
        # kline format: [open_time, open, high, low, close, ...]
        # Approximate timestamps: klines are ~60s apart, last one is ~now
        now = time.time()
        n = len(klines)
        for i, kline in enumerate(klines):
            close_price = float(kline[4])
            approx_ts = now - (n - 1 - i) * 60.0
            _state.prices.append(close_price)
            _state.momentum_history.append((approx_ts, close_price))
            _state.last_price = close_price
        _state.last_fetch_ts = now
        logger.info(
            f"Bootstrapped with {len(klines)} klines. "
            f"σ_annual={_state.sigma_annualized():.2%} | last_price=${_state.last_price:,.0f}"
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to bootstrap from Binance klines: {e}")
        logger.info("Falling back to Kraken OHLC for bootstrap")
        return bootstrap_from_kraken_ohlc(limit=limit)


def fetch_btc_price_kraken(timeout: float = 5.0) -> Optional[float]:
    """Fetch current BTC/USD price from Kraken REST."""
    try:
        resp = requests.get(KRAKEN_URL, params={"pair": "XBTUSD"}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        price = float(data["result"]["XXBTZUSD"]["c"][0])
        now = time.time()
        _state.prices.append(price)
        _state.momentum_history.append((now, price))
        _state.last_price = price
        _state.last_fetch_ts = now
        return price
    except Exception as e:
        logger.warning(f"Kraken price fetch failed: {e}")
        return None


def fetch_btc_price(timeout: float = 5.0) -> Optional[float]:
    """Fetch current BTC/USDT price from Binance REST, falling back to Kraken."""
    try:
        resp = requests.get(BINANCE_URL, params={"symbol": SYMBOL}, timeout=timeout)
        if resp.status_code != 200:
            raise ValueError(f"Binance returned status {resp.status_code}")
        price = float(resp.json()["price"])
        now = time.time()
        _state.prices.append(price)
        _state.momentum_history.append((now, price))
        _state.last_price = price
        _state.last_fetch_ts = now
        return price
    except Exception as e:
        logger.warning(f"Binance price fetch failed: {e}")
        logger.info("Falling back to Kraken for price fetch")
        return fetch_btc_price_kraken(timeout=timeout)


def norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erfc."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def bs_binary_call(S: float, K: float, T: float, sigma: float) -> float:
    """
    Black-Scholes price for a binary call option (pays $1 if S > K at expiry).

    Args:
        S: Current underlying price.
        K: Strike price.
        T: Time to expiry in years.
        sigma: Annualized volatility (e.g. 0.8 = 80%).

    Returns:
        Probability in [0, 1].
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        # Expired: intrinsic value only
        return 1.0 if S > K else 0.0

    d2 = (math.log(S / K) - 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
    return norm_cdf(d2)


def compute_fair_price(
    strike: float,
    expiry_ts: float,
    btc_price: Optional[float] = None,
    sigma_override: Optional[float] = None,
) -> float:
    """
    Compute P_fair for a binary "BTC > strike at expiry" option.

    Args:
        strike:        Strike price in USD.
        expiry_ts:     Unix timestamp of option expiry.
        btc_price:     Override BTC price (fetches from Binance if None).
        sigma_override: Override annualized volatility (uses rolling estimate if None).

    Returns:
        P_fair in [MIN_P_FAIR, MAX_P_FAIR].
    """
    # Bootstrap if needed
    if len(_state.prices) < 3:
        bootstrap_from_klines()

    S = btc_price or fetch_btc_price() or 0.0
    if S <= 0:
        logger.warning("No BTC price available — returning 0.5 as fallback")
        return 0.5

    T = max(0.0, (expiry_ts - time.time()) / (365 * 24 * 3600))
    sigma = sigma_override or _state.sigma_annualized()

    p = bs_binary_call(S, strike, T, sigma)
    p_clamped = max(MIN_P_FAIR, min(MAX_P_FAIR, p))

    logger.debug(
        f"P_fair: S={S:.0f} K={strike:.0f} T={T*365*24*60:.1f}m "
        f"σ={sigma:.2%} → p={p:.4f} (clamped={p_clamped:.4f})"
    )
    return p_clamped


def compute_updown_fair_price(
    window_seconds: float,
    momentum_lookback: float = 60.0,
    momentum_weight: float = 0.3,
) -> float:
    """
    Compute P(BTC price UP over the next `window_seconds`).

    Model: Geometric Brownian Motion + short-term momentum signal.

    Base probability: 0.5 (no drift, efficient market assumption).
    Momentum adjustment: recent BTC return (last `momentum_lookback` seconds)
    suggests directional bias. We scale by:

        z = recent_log_return / sigma_per_window
        P_momentum = N(z * momentum_weight)

    where sigma_per_window = sigma_annual * sqrt(window_seconds / 31536000).

    The momentum_weight controls how much we trust the momentum signal.
    0.0 = pure 50/50, 1.0 = full momentum following.

    Args:
        window_seconds:    Duration of the prediction window.
        momentum_lookback: Seconds of price history to use as momentum signal.
        momentum_weight:   How strongly to incorporate momentum (0–1).

    Returns:
        P_up in [MIN_P_FAIR, MAX_P_FAIR].
    """
    # Bootstrap if needed
    if len(_state.prices) < 3:
        bootstrap_from_klines()

    sigma_annual = _state.sigma_annualized()
    sigma_window = sigma_annual * math.sqrt(window_seconds / (365 * 24 * 3600))

    if sigma_window <= 0:
        return 0.5

    # Base: 50% (no drift)
    p_base = 0.5

    # Momentum signal: z-score of recent return vs per-window σ
    recent_return = _state.momentum_return(lookback_seconds=momentum_lookback)
    if recent_return != 0.0:
        z = (recent_return / sigma_window) * momentum_weight
        # Blend: P_up = 0.5 + (N(z) - 0.5) = N(z) blended toward 0.5
        p_momentum = norm_cdf(z)
        p_up = p_base + (p_momentum - 0.5)
    else:
        p_up = p_base

    p_clamped = max(MIN_P_FAIR, min(MAX_P_FAIR, p_up))

    logger.debug(
        f"P_up: window={window_seconds:.0f}s σ_window={sigma_window:.4f} "
        f"recent_ret={recent_return:.6f} → p_up={p_up:.4f} (clamped={p_clamped:.4f})"
    )
    return p_clamped


def get_btc_state() -> PriceState:
    """Return the current price state (for external monitoring)."""
    return _state
