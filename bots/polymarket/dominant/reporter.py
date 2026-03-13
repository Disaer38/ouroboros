"""
Reporter — Telegram + Drive reporting for the Dominant Side paper trader.

Sends periodic status summaries and trade notifications to Telegram.
All events are also logged to Drive JSONL.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Optional

import httpx

from .config import DominantConfig
from .exchange import DominantTrade, PaperExchange

logger = logging.getLogger(__name__)


def _esc(text: object) -> str:
    return html.escape(str(text))


class Reporter:
    """
    Sends Telegram messages and manages reporting intervals.

    Usage:
        reporter = Reporter(config, bot_token, chat_id)
        # On trade:
        await reporter.on_trade(trade, exchange)
        # Periodically:
        await reporter.maybe_report(exchange)
    """

    def __init__(
        self,
        config: DominantConfig,
        bot_token: str,
        chat_id: str,
        http: Optional[httpx.AsyncClient] = None,
    ):
        self.config = config
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._http = http
        self._last_report_at = time.monotonic()

    async def send(self, text: str) -> None:
        """Send a Telegram message. Silently fails if not configured."""
        if not self.bot_token or not self.chat_id:
            logger.info(f"[REPORT (no TG)] {text[:200]}")
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}

        client = self._http or httpx.AsyncClient()
        try:
            resp = await client.post(url, json=payload, timeout=15.0)
            if resp.status_code != 200:
                logger.warning(f"Telegram send failed: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            logger.warning(f"Telegram send error: {e}")
        finally:
            if self._http is None:
                await client.aclose()

    async def send_startup(self, exchange: PaperExchange) -> None:
        cfg = self.config
        await self.send(
            f"▶️ <b>Dominant Side Paper Trader</b>\n"
            f"Assets: {', '.join(cfg.assets).upper()}\n"
            f"Threshold: P ≥ {cfg.min_dominant_prob:.0%} | "
            f"Bet: ${cfg.bet_size_usdc:.0f} | "
            f"Balance: ${exchange.balance:.0f}\n"
            f"Max open: {cfg.max_open_trades} | Daily loss limit: ${cfg.max_daily_loss_usdc:.0f}"
        )

    async def on_trade(self, trade: DominantTrade, exchange: PaperExchange) -> None:
        """Notify about a new trade entry."""
        if trade.resolved:
            # Resolution notification
            result = "✅ WIN" if trade.won else "❌ LOSS"
            pnl_str = f"${trade.pnl:+.2f}" if trade.pnl is not None else "?"
            await self.send(
                f"{result} | {_esc(trade.market_question[:50])}\n"
                f"Side: {trade.side} @ {trade.entry_price:.3f} | "
                f"PnL: {pnl_str} | Balance: ${exchange.balance:.2f}"
            )
        else:
            # Entry notification
            await self.send(
                f"📝 BUY {trade.side} | {_esc(trade.market_question[:50])}\n"
                f"Price: {trade.entry_price:.3f} | "
                f"Shares: {trade.shares:.2f} | "
                f"Cost: ${trade.cost_usdc:.2f} | "
                f"TTL: {trade.time_remaining_at_entry:.0f}s"
            )

    async def maybe_report(self, exchange: PaperExchange) -> None:
        """Send periodic status report if interval elapsed."""
        now = time.monotonic()
        if now - self._last_report_at < self.config.report_interval_sec:
            return
        await self.send_status(exchange)
        self._last_report_at = now

    async def send_status(self, exchange: PaperExchange) -> None:
        """Send full status summary."""
        s = exchange.summary()
        pnl_str = f"${s['realized_pnl']:+.2f}"
        sign = "📈" if s["realized_pnl"] >= 0 else "📉"

        await self.send(
            f"{sign} <b>Status Report</b>\n"
            f"Balance: ${s['balance']:.2f} | PnL: {pnl_str}\n"
            f"Open: {s['open_trades']} | Closed: {s['closed_trades']}\n"
            f"Win rate: {s['win_rate_pct']:.1f}% "
            f"({s['wins']}W / {s['losses']}L)\n"
            f"Exposure: ${s['open_exposure_usdc']:.2f} | "
            f"Daily loss: ${s['daily_loss']:.2f}"
        )

    async def send_shutdown(self, exchange: PaperExchange) -> None:
        s = exchange.summary()
        pnl_str = f"${s['realized_pnl']:+.2f}"
        await self.send(
            f"🛑 <b>Paper Trader Stopped</b>\n"
            f"Final balance: ${s['balance']:.2f} | PnL: {pnl_str}\n"
            f"Total trades: {s['closed_trades']} | "
            f"Win rate: {s['win_rate_pct']:.1f}%"
        )