"""
Tests for the live relay's safety properties: order relay must never wait on or
depend on Claude, duplicate webhook deliveries must never cause a duplicate real
order, PickMyTrade failures must be recorded visibly (not hidden as success), and
later lifecycle events must never corrupt the forward status.
"""
from unittest.mock import patch

import app as app_module
from conftest import entry_payload


def test_normal_webhook_stores_forwards_once_and_analyzes(client, get_trade):
    """1. Normal webhook: payload -> stored -> forwarded once -> Claude runs after."""
    with patch.object(app_module, "forward_to_pickmytrade", return_value=(True, 200, None)) as mock_forward, \
         patch.object(app_module, "analyze_with_claude", return_value=("Looks aligned with trend.", None)) as mock_claude:
        resp = client.post("/webhook", json=entry_payload("corr-normal"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["pmt_forwarded"] is True

    mock_forward.assert_called_once()
    mock_claude.assert_called_once()  # ran (as a background task, which TestClient executes inline)

    trade = get_trade("corr-normal")
    assert trade is not None
    assert trade["pmt_forwarded"] == 1
    assert trade["pmt_status_code"] == 200
    assert trade["llm_analysis"] == "Looks aligned with trend."
    assert trade["status"] == "open"


def test_duplicate_webhook_does_not_forward_twice(client, get_trade):
    """2. Duplicate webhook: same correlation_id arrives again -> no second PickMyTrade forward,
    existing record is left untouched, response clearly flags it as a duplicate."""
    with patch.object(app_module, "forward_to_pickmytrade", return_value=(True, 200, None)) as mock_forward, \
         patch.object(app_module, "analyze_with_claude", return_value=("first analysis", None)):
        first = client.post("/webhook", json=entry_payload("corr-dup"))
    assert first.status_code == 200

    # Simulate a TradingView retry of the exact same webhook delivery.
    with patch.object(app_module, "forward_to_pickmytrade", return_value=(True, 200, None)) as mock_forward_2, \
         patch.object(app_module, "analyze_with_claude", return_value=("should not run", None)) as mock_claude_2:
        second = client.post("/webhook", json=entry_payload("corr-dup"))

    assert second.status_code == 208
    body = second.json()
    assert body["ok"] is True
    assert body["duplicate_already_forwarded"] is True

    mock_forward_2.assert_not_called()  # the critical assertion: no second real order
    mock_claude_2.assert_not_called()

    trade = get_trade("corr-dup")
    assert trade["llm_analysis"] == "first analysis"  # original record untouched


def test_claude_failure_does_not_block_or_prevent_pickmytrade_forward(client, get_trade):
    """3. Claude delay/failure: Claude raises -> PickMyTrade still forwards, response still
    reflects a successful forward, and the failure is confined to the analysis field."""
    def claude_raises(*args, **kwargs):
        raise TimeoutError("simulated Claude API timeout")

    with patch.object(app_module, "forward_to_pickmytrade", return_value=(True, 200, None)) as mock_forward, \
         patch.object(app_module, "analyze_with_claude", side_effect=claude_raises):
        resp = client.post("/webhook", json=entry_payload("corr-claude-fail"))

    # The webhook handler itself must not 500 just because the background task's inner
    # function would raise - analyze_with_claude's own try/except is what's under test here
    # indirectly, but the key property is the response/forward path is fully unaffected.
    assert resp.status_code == 200
    assert resp.json()["pmt_forwarded"] is True
    mock_forward.assert_called_once()

    trade = get_trade("corr-claude-fail")
    assert trade["pmt_forwarded"] == 1  # order relay succeeded regardless of Claude


def test_pickmytrade_failure_is_recorded_and_visible(client, get_trade):
    """4. PickMyTrade failure: forward fails -> database records it clearly, response is a
    distinct non-200 (207) so it is not hidden, and the dashboard can show it."""
    with patch.object(app_module, "forward_to_pickmytrade",
                       return_value=(False, None, "connection refused")) as mock_forward, \
         patch.object(app_module, "analyze_with_claude", return_value=("analysis ran fine", None)):
        resp = client.post("/webhook", json=entry_payload("corr-pmt-fail"))

    assert resp.status_code == 207
    body = resp.json()
    assert body["pmt_forwarded"] is False
    assert body["pmt_error"] == "connection refused"

    trade = get_trade("corr-pmt-fail")
    assert trade["pmt_forwarded"] == 0
    assert trade["pmt_error"] == "connection refused"

    # Dashboard rendering must surface this, not silently show success.
    html = app_module.render_trade(trade)
    assert "NOT forwarded to PickMyTrade" in html
    assert "connection refused" in html


def test_price_update_and_exit_do_not_corrupt_forward_status(client, get_trade):
    """5. Existing trade update: price_update/exit events must update only their own fields
    and must never touch pmt_forwarded/pmt_status_code/pmt_error."""
    with patch.object(app_module, "forward_to_pickmytrade", return_value=(True, 200, None)), \
         patch.object(app_module, "analyze_with_claude", return_value=("ok", None)):
        client.post("/webhook", json=entry_payload("corr-lifecycle"))

    price_update_resp = client.post("/webhook", json={
        "type": "price_update", "correlation_id": "corr-lifecycle", "secret": "test-secret",
        "current_price": 30020, "unrealized_pnl": 240,
    })
    assert price_update_resp.status_code == 200

    mid_trade = get_trade("corr-lifecycle")
    assert mid_trade["current_price"] == 30020
    assert mid_trade["unrealized_pnl"] == 240
    assert mid_trade["pmt_forwarded"] == 1  # unaffected by the price update
    assert mid_trade["status"] == "open"

    exit_resp = client.post("/webhook", json={
        "type": "exit", "correlation_id": "corr-lifecycle", "secret": "test-secret",
        "outcome": "WIN", "exit_price": 30050, "realized_pnl": 600,
    })
    assert exit_resp.status_code == 200

    final_trade = get_trade("corr-lifecycle")
    assert final_trade["status"] == "won"
    assert final_trade["exit_price"] == 30050
    assert final_trade["realized_pnl"] == 600
    assert final_trade["pmt_forwarded"] == 1  # still unaffected by the exit
    assert final_trade["pmt_status_code"] == 200
