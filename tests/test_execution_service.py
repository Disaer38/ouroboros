"""
Unit tests for ExecutionService (batch order refactor).

Covers:
- Simulation mode: happy path, profit accounting, stats
- Live mode: batch round-trip (mocked client), partial failure, full failure
- _parse: all response shapes (filled, failed, unexpected)
"""
from __future__ import annotations

import asyncio
import sys
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch, call

import pytest

# Allow running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from polymarket_arb.services.execution import ExecutionService, OrderResult, TradeResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UP_TOKEN   = "token-up-001"
DOWN_TOKEN = "token-down-001"
SLUG       = "btc-updown-5m-test"

UP_ASK   = Decimal("0.47")
DOWN_ASK = Decimal("0.46")
SHARES   = Decimal("10")


def run(coro):
    """Run a coroutine synchronously (compatible with all pytest versions)."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Simulation mode
# ---------------------------------------------------------------------------

class TestSimulationMode:
    def setup_method(self):
        self.svc = ExecutionService(simulation=True)

    def test_returns_trade_result(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert isinstance(result, TradeResult)

    def test_simulated_flag_set(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.simulated is True

    def test_both_legs_status_simulated(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.up_order.status   == "simulated"
        assert result.down_order.status == "simulated"

    def test_success_property_true(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.success is True

    def test_trade_counter_increments(self):
        for i in range(3):
            run(self.svc.execute_neg_risk(
                SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
            ))
        assert self.svc.stats["trades"] == 3

    def test_profit_accounting_positive(self):
        """Edge = 1 - 0.47 - 0.46 = 0.07; fees = 0.04; net per share = 0.03; × 10 sh = 0.30"""
        run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert self.svc.stats["simulated_profit"] == pytest.approx(0.30, abs=1e-4)

    def test_no_edge_zero_or_negative_profit(self):
        """Entry sum = 0.99 → edge = 0.01 < 0.04 fee → negative net profit."""
        run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, Decimal("0.50"), Decimal("0.49"), SHARES
        ))
        # Net should be negative (fee exceeds edge)
        assert self.svc.stats["simulated_profit"] < 0

    def test_total_cost_correct(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        expected_cost = float(UP_ASK) * float(SHARES) + float(DOWN_ASK) * float(SHARES)
        assert result.total_cost == pytest.approx(expected_cost, rel=1e-6)

    def test_order_ids_contain_leg_suffix(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert "up"   in result.up_order.order_id
        assert "down" in result.down_order.order_id

    def test_stats_mode_is_simulation(self):
        assert self.svc.stats["mode"] == "simulation"


# ---------------------------------------------------------------------------
# Live mode — mocked py-clob-client
# ---------------------------------------------------------------------------

BATCH_SUCCESS_RESP = [
    {"orderID": "live-order-001", "status": "matched", "errorMsg": None},
    {"orderID": "live-order-002", "status": "matched", "errorMsg": None},
]

BATCH_PARTIAL_RESP = [
    {"orderID": "live-order-003", "status": "matched", "errorMsg": None},
    {"orderID": "",               "status": "failed",  "errorMsg": "insufficient liquidity"},
]

BATCH_FULL_FAIL_RESP = [
    {"orderID": "", "status": "failed", "errorMsg": "slippage"},
    {"orderID": "", "status": "failed", "errorMsg": "slippage"},
]


def _make_live_svc(batch_return_value):
    """
    Build an ExecutionService in live mode with a mocked CLOB client.
    The mock intercepts create_order and post_orders.
    """
    svc = ExecutionService.__new__(ExecutionService)
    svc.simulation        = False
    svc._trades_executed  = 0
    svc._total_profit_simulated = 0.0
    svc._last_latency_ms  = 0.0
    svc._latency_samples  = []

    mock_client = MagicMock()
    # create_order returns a sentinel object (just the args back)
    mock_client.create_order.side_effect = lambda args: {"_args": args}
    mock_client.post_orders.return_value = batch_return_value
    svc._client = mock_client
    return svc


class TestLiveModeSuccess:
    def setup_method(self):
        self.svc = _make_live_svc(BATCH_SUCCESS_RESP)

    def test_returns_trade_result(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert isinstance(result, TradeResult)

    def test_simulated_flag_false(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.simulated is False

    def test_both_legs_filled(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.up_order.status   == "filled"
        assert result.down_order.status == "filled"

    def test_success_property_true(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.success is True

    def test_order_ids_populated(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.up_order.order_id   == "live-order-001"
        assert result.down_order.order_id == "live-order-002"

    def test_single_batch_call(self):
        """post_orders must be called exactly once — one network round-trip."""
        run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        self.svc._client.post_orders.assert_called_once()

    def test_post_orders_receives_two_orders(self):
        run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        args, kwargs = self.svc._client.post_orders.call_args
        assert len(args[0]) == 2  # list of two orders

    def test_latency_recorded(self):
        run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert self.svc._last_latency_ms >= 0
        assert len(self.svc._latency_samples) == 1


class TestLiveModePartialFailure:
    def setup_method(self):
        self.svc = _make_live_svc(BATCH_PARTIAL_RESP)

    def test_returns_trade_result(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert isinstance(result, TradeResult)

    def test_up_filled_down_failed(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.up_order.status   == "filled"
        assert result.down_order.status == "failed"

    def test_success_false_on_partial_failure(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.success is False

    def test_down_order_error_message_populated(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.down_order.error == "insufficient liquidity"


class TestLiveModeFullFailure:
    def setup_method(self):
        self.svc = _make_live_svc(BATCH_FULL_FAIL_RESP)

    def test_both_legs_failed(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.up_order.status   == "failed"
        assert result.down_order.status == "failed"

    def test_success_false(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.success is False


class TestLiveModeNetworkException:
    def setup_method(self):
        svc = ExecutionService.__new__(ExecutionService)
        svc.simulation        = False
        svc._trades_executed  = 0
        svc._total_profit_simulated = 0.0
        svc._last_latency_ms  = 0.0
        svc._latency_samples  = []

        mock_client = MagicMock()
        mock_client.create_order.side_effect = lambda args: {"_args": args}
        mock_client.post_orders.side_effect  = ConnectionError("network timeout")
        svc._client = mock_client
        self.svc = svc

    def test_returns_failed_trade_result(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert isinstance(result, TradeResult)
        assert result.success is False

    def test_both_orders_status_failed(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.up_order.status   == "failed"
        assert result.down_order.status == "failed"

    def test_error_message_propagated(self):
        result = run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert "network timeout" in (result.up_order.error or "")

    def test_latency_still_recorded_on_error(self):
        run(self.svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert len(self.svc._latency_samples) == 1


class TestLiveModeMalformedResponse:
    """Edge case: batch returns wrong shape."""

    def _make(self, bad_resp):
        svc = ExecutionService.__new__(ExecutionService)
        svc.simulation        = False
        svc._trades_executed  = 0
        svc._total_profit_simulated = 0.0
        svc._last_latency_ms  = 0.0
        svc._latency_samples  = []
        mock_client = MagicMock()
        mock_client.create_order.side_effect = lambda args: {"_args": args}
        mock_client.post_orders.return_value = bad_resp
        svc._client = mock_client
        return svc

    def test_empty_list_raises_and_returns_failed(self):
        svc = self._make([])
        result = run(svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.success is False

    def test_single_item_list_returns_failed(self):
        svc = self._make([{"orderID": "x", "status": "matched"}])
        result = run(svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.success is False

    def test_non_list_response_returns_failed(self):
        svc = self._make("unexpected string response")
        result = run(svc.execute_neg_risk(
            SLUG, UP_TOKEN, DOWN_TOKEN, UP_ASK, DOWN_ASK, SHARES
        ))
        assert result.success is False
