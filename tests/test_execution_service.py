"""
Unit tests for polymarket_arb/services/execution.py — ExecutionService.

Covers:
  - Simulation mode trades and stats
  - Live mode: every branch of _parse inner function
  - Live mode: happy path (both filled)
  - Live mode: partial failure (one leg fails)
  - Live mode: malformed batch response (bad shape)
  - Live mode: exception in _build_and_post_batch
  - stats property (both modes)

No real network calls — py_clob_client is fully mocked.

Run:
    pytest tests/test_execution_service.py -v
"""
from __future__ import annotations

import asyncio
import sys
import types
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────
# Stub out py_clob_client so the module imports cleanly even
# when the real package is not installed.
# ─────────────────────────────────────────────────────────────
_clob_stub = types.ModuleType("py_clob_client")
_clob_client_stub = types.ModuleType("py_clob_client.client")
_clob_types_stub = types.ModuleType("py_clob_client.clob_types")


class _OrderArgs:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _OrderType:
    FOK = "FOK"


_clob_stub.client = _clob_client_stub
_clob_stub.clob_types = _clob_types_stub
_clob_client_stub.ClobClient = MagicMock()
_clob_types_stub.OrderArgs = _OrderArgs
_clob_types_stub.OrderType = _OrderType

sys.modules.setdefault("py_clob_client", _clob_stub)
sys.modules.setdefault("py_clob_client.client", _clob_client_stub)
sys.modules.setdefault("py_clob_client.clob_types", _clob_types_stub)

# Now safe to import
import importlib, pathlib, os

# Insert polymarket_arb parent so relative imports work
_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "polymarket_arb"))

from polymarket_arb.services.execution import ExecutionService, TradeResult, OrderResult


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

_UP_TOKEN   = "token-up-123"
_DOWN_TOKEN = "token-down-456"
_MARKET     = "btc-updown-5m-test"
_UP_ASK     = Decimal("0.47")
_DOWN_ASK   = Decimal("0.46")
_SHARES     = Decimal("10")


def _make_live_svc() -> ExecutionService:
    """Create a live-mode ExecutionService with _init_clob_client mocked out."""
    with patch.object(ExecutionService, "_init_clob_client"):
        svc = ExecutionService(simulation=False)
    svc._client = MagicMock()
    return svc


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _set_batch_response(svc: ExecutionService, response):
    """Make svc._client.post_orders(...) return `response`."""
    svc._client.create_order.side_effect = lambda args: MagicMock()
    svc._client.post_orders.return_value = response


# ─────────────────────────────────────────────────────────────
# TestSimulateMode
# ─────────────────────────────────────────────────────────────

class TestSimulateMode:
    """Simulation mode: no network, pure accounting."""

    def _svc(self):
        return ExecutionService(simulation=True)

    def test_returns_trade_result(self):
        svc = self._svc()
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert isinstance(result, TradeResult)

    def test_simulated_flag_true(self):
        svc = self._svc()
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.simulated is True

    def test_order_status_simulated(self):
        svc = self._svc()
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.status == "simulated"
        assert result.down_order.status == "simulated"

    def test_order_ids_contain_trade_number(self):
        svc = self._svc()
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert "sim-1" in result.up_order.order_id
        assert "sim-1" in result.down_order.order_id

    def test_order_prices_correct(self):
        svc = self._svc()
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert abs(result.up_order.price   - float(_UP_ASK))   < 1e-9
        assert abs(result.down_order.price - float(_DOWN_ASK)) < 1e-9

    def test_order_sizes_correct(self):
        svc = self._svc()
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert abs(result.up_order.size   - float(_SHARES)) < 1e-9
        assert abs(result.down_order.size - float(_SHARES)) < 1e-9

    def test_token_ids_assigned(self):
        svc = self._svc()
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.token_id   == _UP_TOKEN
        assert result.down_order.token_id == _DOWN_TOKEN

    def test_trade_counter_increments(self):
        svc = self._svc()
        for i in range(3):
            _run(svc.execute_neg_risk(
                _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
            ))
        assert svc.stats["trades"] == 3

    def test_total_cost_property(self):
        svc = self._svc()
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        expected = float(_UP_ASK) * float(_SHARES) + float(_DOWN_ASK) * float(_SHARES)
        assert abs(result.total_cost - expected) < 1e-9

    def test_success_property_true_for_sim(self):
        svc = self._svc()
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        # "simulated" != "failed" so success should be True
        assert result.success is True

    def test_stats_mode_simulation(self):
        svc = self._svc()
        assert svc.stats["mode"] == "simulation"

    def test_cumulative_simulated_profit_grows(self):
        """Profit accumulates over multiple trades."""
        svc = self._svc()
        _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, Decimal("100")
        ))
        _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, Decimal("100")
        ))
        # Two profitable trades — profit should be > 0
        assert svc.stats["simulated_profit"] > 0


# ─────────────────────────────────────────────────────────────
# TestLiveModeParseLogic
# Tests each branch of _parse by injecting mock batch responses.
# ─────────────────────────────────────────────────────────────

class TestLiveModeParseLogic:
    """Test every branch of the _parse inner function via full execute_neg_risk."""

    # ── CASE A: "orderID" key, status "matched" ──────────────

    def test_parse_case_a_orderID_matched(self):
        """Dict with 'orderID' and status 'matched' → status='filled'."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "abc-up-001",   "status": "matched"},
            {"orderID": "abc-down-001", "status": "matched"},
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.status   == "filled"
        assert result.down_order.status == "filled"
        assert result.up_order.order_id   == "abc-up-001"
        assert result.down_order.order_id == "abc-down-001"
        assert result.simulated is False

    # ── CASE B: "order_id" key (snake_case), status "matched" ──

    def test_parse_case_b_order_id_snake_case(self):
        """Dict with 'order_id' (snake_case) and status 'matched' → filled."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"order_id": "snake-up-001",   "status": "matched"},
            {"order_id": "snake-down-001", "status": "matched"},
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.status   == "filled"
        assert result.down_order.status == "filled"
        assert result.up_order.order_id   == "snake-up-001"
        assert result.down_order.order_id == "snake-down-001"

    # ── CASE C: "id" key only (fallback) ─────────────────────

    def test_parse_case_c_id_fallback(self):
        """Dict with only 'id' key → order_id=resp['id'], filled via truthy id."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"id": "fallback-up-001",   "status": "open"},
            {"id": "fallback-down-001", "status": "open"},
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        # order_id is truthy → resolved = "filled"
        assert result.up_order.order_id   == "fallback-up-001"
        assert result.down_order.order_id == "fallback-down-001"
        assert result.up_order.status   == "filled"
        assert result.down_order.status == "filled"

    # ── CASE D: empty orderID, non-matched status → failed ───

    def test_parse_case_d_empty_order_id_non_matched(self):
        """Empty orderID + status != 'matched' → status='failed'."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "",   "status": "cancelled"},
            {"orderID": "",   "status": "cancelled"},
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.status   == "failed"
        assert result.down_order.status == "failed"
        # Fallback id should contain leg name
        assert "up"   in result.up_order.order_id
        assert "down" in result.down_order.order_id

    # ── CASE E: errorMsg present ──────────────────────────────

    def test_parse_case_e_error_msg(self):
        """Dict with 'errorMsg' → order.error is set."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "", "status": "error", "errorMsg": "Insufficient balance"},
            {"orderID": "", "status": "error", "errorMsg": "Token not found"},
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.error   == "Insufficient balance"
        assert result.down_order.error == "Token not found"
        assert result.up_order.status   == "failed"
        assert result.down_order.status == "failed"

    # ── CASE F: "error" key ───────────────────────────────────

    def test_parse_case_f_error_key(self):
        """Dict with 'error' key (not 'errorMsg') → order.error is set."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "", "status": "rejected", "error": "Price out of range"},
            {"orderID": "down-ok-001", "status": "matched", "error": None},
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.error == "Price out of range"
        assert result.up_order.status == "failed"
        # Down has a valid order_id → filled, error is None
        assert result.down_order.error  is None
        assert result.down_order.status == "filled"

    # ── CASE G: resp is not a dict ────────────────────────────

    def test_parse_case_g_non_dict_string(self):
        """Non-dict response (string) → status='failed', error=str(resp)."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            "some-error-string",
            None,
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.status   == "failed"
        assert result.down_order.status == "failed"
        assert result.up_order.error   == "some-error-string"
        assert result.down_order.error == "None"

    def test_parse_case_g_non_dict_integer(self):
        """Non-dict response (integer) → status='failed'."""
        svc = _make_live_svc()
        _set_batch_response(svc, [42, 43])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.status   == "failed"
        assert result.down_order.status == "failed"
        assert result.up_order.error   == "42"
        assert result.down_order.error == "43"

    # ── CASE H: orderID present but status != "matched" ──────
    # Logic: resolved = "filled" if (status_raw == "matched" OR order_id) else "failed"
    # So having a non-empty orderID → filled even with status != "matched"

    def test_parse_case_h_order_id_present_non_matched_status(self):
        """orderID present but status is 'open' → still resolved as 'filled'."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "live-up-999",   "status": "open"},
            {"orderID": "live-down-999", "status": "open"},
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.status   == "filled"
        assert result.down_order.status == "filled"
        assert result.up_order.order_id   == "live-up-999"
        assert result.down_order.order_id == "live-down-999"

    # ── Mixed null-error edge cases ───────────────────────────

    def test_parse_empty_dict(self):
        """Completely empty dict → both legs failed, no crash."""
        svc = _make_live_svc()
        _set_batch_response(svc, [{}, {}])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.status   == "failed"
        assert result.down_order.status == "failed"

    def test_parse_preserves_token_ids(self):
        """After parse, token_id on each OrderResult matches the original token."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "u1", "status": "matched"},
            {"orderID": "d1", "status": "matched"},
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.up_order.token_id   == _UP_TOKEN
        assert result.down_order.token_id == _DOWN_TOKEN


# ─────────────────────────────────────────────────────────────
# TestLiveModeIntegration
# Tests full _execute_live behavior beyond _parse
# ─────────────────────────────────────────────────────────────

class TestLiveModeIntegration:

    def test_happy_path_success_true(self):
        """Both legs filled → TradeResult.success is True."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "u-ok", "status": "matched"},
            {"orderID": "d-ok", "status": "matched"},
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.success is True
        assert result.simulated is False

    def test_partial_failure_up_failed(self):
        """UP filled, DOWN failed → success is False."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "u-ok",  "status": "matched"},
            {"orderID": "",      "status": "cancelled"},
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.success is False
        assert result.up_order.status   == "filled"
        assert result.down_order.status == "failed"

    def test_partial_failure_down_filled(self):
        """UP failed, DOWN filled → success is False."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "",      "status": "rejected"},
            {"orderID": "d-ok",  "status": "matched"},
        ])
        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.success is False
        assert result.up_order.status   == "failed"
        assert result.down_order.status == "filled"

    def test_exception_in_post_orders(self):
        """post_orders raises → TradeResult with both legs 'failed'."""
        svc = _make_live_svc()
        svc._client.create_order.side_effect = lambda args: MagicMock()
        svc._client.post_orders.side_effect = Exception("network timeout")

        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.success   is False
        assert result.simulated is False
        assert result.up_order.status   == "failed"
        assert result.down_order.status == "failed"
        assert "network timeout" in (result.up_order.error or "")

    def test_bad_response_too_short(self):
        """Batch response with only 1 item → ValueError → both legs 'failed'."""
        svc = _make_live_svc()
        _set_batch_response(svc, [{"orderID": "only-one", "status": "matched"}])

        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.success   is False
        assert result.simulated is False
        assert "batch-error" in result.up_order.order_id

    def test_bad_response_not_a_list(self):
        """Batch response that is not a list → ValueError → both legs 'failed'."""
        svc = _make_live_svc()
        _set_batch_response(svc, {"orderID": "wrong-shape", "status": "matched"})

        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.success   is False
        assert result.simulated is False

    def test_bad_response_empty_list(self):
        """Empty list response → both legs 'failed'."""
        svc = _make_live_svc()
        _set_batch_response(svc, [])

        result = _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert result.success is False

    def test_latency_recorded(self):
        """After a live call, latency_samples is non-empty and last_latency_ms > 0."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "u1", "status": "matched"},
            {"orderID": "d1", "status": "matched"},
        ])
        _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert len(svc._latency_samples) == 1
        assert svc._last_latency_ms >= 0  # Could be 0 in fast CI

    def test_latency_recorded_on_exception(self):
        """Even on exception, latency sample is still recorded."""
        svc = _make_live_svc()
        svc._client.create_order.side_effect = lambda args: MagicMock()
        svc._client.post_orders.side_effect = RuntimeError("boom")

        _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert len(svc._latency_samples) == 1

    def test_post_orders_called_with_list(self):
        """post_orders must be called with a list (batch semantics)."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "u1", "status": "matched"},
            {"orderID": "d1", "status": "matched"},
        ])
        _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        args, kwargs = svc._client.post_orders.call_args
        # First positional argument must be a list of 2 orders
        assert isinstance(args[0], list)
        assert len(args[0]) == 2

    def test_create_order_called_twice(self):
        """create_order is called once for UP and once for DOWN."""
        svc = _make_live_svc()
        _set_batch_response(svc, [
            {"orderID": "u1", "status": "matched"},
            {"orderID": "d1", "status": "matched"},
        ])
        _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
        ))
        assert svc._client.create_order.call_count == 2


# ─────────────────────────────────────────────────────────────
# TestStats
# ─────────────────────────────────────────────────────────────

class TestStats:
    """stats property returns correct structure and values."""

    def test_stats_keys_present(self):
        svc = ExecutionService(simulation=True)
        s = svc.stats
        for key in ("trades", "simulated_profit", "mode",
                    "last_latency_ms", "avg_latency_ms", "p95_latency_ms"):
            assert key in s, f"Missing stats key: {key}"

    def test_stats_mode_live(self):
        svc = _make_live_svc()
        assert svc.stats["mode"] == "live"

    def test_stats_zero_trades(self):
        svc = ExecutionService(simulation=True)
        assert svc.stats["trades"]            == 0
        assert svc.stats["simulated_profit"]  == 0
        assert svc.stats["last_latency_ms"]   == 0
        assert svc.stats["avg_latency_ms"]    == 0
        assert svc.stats["p95_latency_ms"]    == 0

    def test_stats_latency_averages(self):
        """avg and p95 latency are computed from samples."""
        svc = _make_live_svc()
        # Inject known latency samples directly
        svc._latency_samples = [10.0, 20.0, 30.0, 40.0, 50.0]
        svc._last_latency_ms = 50.0
        s = svc.stats
        assert s["avg_latency_ms"] == pytest.approx(30.0, abs=0.1)
        assert s["p95_latency_ms"] >= 40.0

    def test_stats_trades_count_after_sim(self):
        svc = ExecutionService(simulation=True)
        for _ in range(5):
            _run(svc.execute_neg_risk(
                _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, _SHARES
            ))
        assert svc.stats["trades"] == 5

    def test_stats_profit_rounded(self):
        """Simulated profit is rounded to 4 decimal places."""
        svc = ExecutionService(simulation=True)
        _run(svc.execute_neg_risk(
            _MARKET, _UP_TOKEN, _DOWN_TOKEN, _UP_ASK, _DOWN_ASK, Decimal("100")
        ))
        profit = svc.stats["simulated_profit"]
        # Check it's a reasonable float, not a long repeating decimal
        assert isinstance(profit, float)
        assert round(profit, 4) == profit


# ─────────────────────────────────────────────────────────────
# TestOrderResultDataclass
# ─────────────────────────────────────────────────────────────

class TestOrderResultDataclass:
    """Sanity-checks on OrderResult and TradeResult dataclass contracts."""

    def test_order_result_defaults(self):
        o = OrderResult(
            order_id="x", token_id="t", side="BUY",
            price=0.5, size=10.0, status="filled"
        )
        assert o.error     is None
        assert o.timestamp > 0

    def test_trade_result_success_both_filled(self):
        up   = OrderResult("u", "t1", "BUY", 0.47, 10, "filled")
        down = OrderResult("d", "t2", "BUY", 0.46, 10, "filled")
        tr   = TradeResult(up_order=up, down_order=down, simulated=False)
        assert tr.success is True

    def test_trade_result_success_one_failed(self):
        up   = OrderResult("u", "t1", "BUY", 0.47, 10, "filled")
        down = OrderResult("d", "t2", "BUY", 0.46, 10, "failed")
        tr   = TradeResult(up_order=up, down_order=down, simulated=False)
        assert tr.success is False

    def test_trade_result_total_cost(self):
        up   = OrderResult("u", "t1", "BUY", 0.47, 10, "filled")
        down = OrderResult("d", "t2", "BUY", 0.46, 10, "filled")
        tr   = TradeResult(up_order=up, down_order=down)
        assert abs(tr.total_cost - (0.47 * 10 + 0.46 * 10)) < 1e-9
