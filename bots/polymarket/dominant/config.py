"""
Configuration for the Dominant Side paper trading strategy.

All parameters are tunable. Defaults are conservative for initial paper run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/content/drive/MyDrive/Ouroboros")


@dataclass
class DominantConfig:
    """Strategy parameters for Dominant Side paper trading."""

    # ── Signal parameters ──────────────────────────────────────────────────
    min_dominant_prob: float = 0.60
    """Enter trade only if implied probability of dominant side >= this."""

    min_time_remaining_sec: float = 60.0
    """Skip trades in the last N seconds before expiry (too late, too noisy)."""

    # ── Risk management ────────────────────────────────────────────────────
    bet_size_usdc: float = 10.0
    """Fixed USDC per trade. Simple, controlled, easy to analyze."""

    initial_balance_usdc: float = 1000.0
    """Starting virtual balance."""

    max_open_trades: int = 4
    """Maximum concurrent open positions across all assets."""

    max_daily_loss_usdc: float = 100.0
    """Stop trading for the day if realized daily loss exceeds this."""

    # ── Market selection ───────────────────────────────────────────────────
    assets: list = field(default_factory=lambda: ["btc", "eth"])
    """Assets to trade. Supported: btc, eth, sol, xrp."""

    # ── Execution parameters ───────────────────────────────────────────────
    scan_interval_sec: float = 5.0
    """How often to poll orderbooks for each asset (seconds)."""

    resolution_poll_delay_sec: float = 30.0
    """How long to wait after market expiry before checking resolution."""

    resolution_max_retries: int = 3
    """Max attempts to fetch market resolution from Gamma API."""

    report_interval_sec: float = 300.0
    """How often to send status report to Telegram (default: 5 minutes)."""

    # ── Fees ───────────────────────────────────────────────────────────────
    taker_fee: float = 0.02
    """Polymarket taker fee (2% per fill)."""

    # ── Logging ────────────────────────────────────────────────────────────
    log_path: str = field(
        default_factory=lambda: os.path.join(
            os.environ.get("DRIVE_ROOT", "/content/drive/MyDrive/Ouroboros"),
            "logs",
            "dominant_trades.jsonl",
        )
    )
    """Path to JSONL trade log on Google Drive."""

    def validate(self) -> None:
        """Raise ValueError if config is invalid."""
        assert 0.5 < self.min_dominant_prob <= 1.0, "min_dominant_prob must be in (0.5, 1.0]"
        assert self.bet_size_usdc > 0, "bet_size_usdc must be positive"
        assert self.max_open_trades >= 1, "max_open_trades must be >= 1"
        assert all(a in ("btc", "eth", "sol", "xrp") for a in self.assets), \
            f"Unknown asset in {self.assets}"