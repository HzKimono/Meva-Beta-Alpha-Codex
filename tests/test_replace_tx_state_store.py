from __future__ import annotations

from btcbot.services.state_store import StateStore, _is_replace_tx_forward_transition


def test_replace_tx_unknown_state_transition_rejected() -> None:
    try:
        _is_replace_tx_forward_transition("INIT", "NOT_A_STATE")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown replace tx state")


def test_replace_tx_metadata_mismatch_is_non_destructive(tmp_path) -> None:
    store = StateStore(str(tmp_path / "replace_tx.sqlite"))
    store.upsert_replace_tx(
        replace_tx_id="rpl:mismatch",
        symbol="BTC_TRY",
        side="buy",
        old_client_order_ids=["old-1"],
        new_client_order_id="new-1",
        state="INIT",
    )
    store.upsert_replace_tx(
        replace_tx_id="rpl:mismatch",
        symbol="ETH_TRY",
        side="sell",
        old_client_order_ids=["old-2"],
        new_client_order_id="new-2",
        state="CANCEL_SENT",
    )
    tx = store.get_replace_tx("rpl:mismatch")
    assert tx is not None
    assert tx.symbol == "BTCTRY"
    assert tx.side == "buy"
    assert tx.old_client_order_ids == ("old-1",)
    assert tx.new_client_order_id == "new-1"
    assert tx.last_error == "replace_tx_metadata_mismatch"
