"""Concurrent state.json writers must not clobber each other."""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
from pathlib import Path

import pytest


if sys.platform == "win32":
    pytest.skip(
        "flock-based locking requires POSIX; production VPS is Linux",
        allow_module_level=True,
    )


def _writer(state_path: Path, key: str, value: dict) -> None:
    from tennis_dry_run import update_state_atomic

    def _update(state):
        state.setdefault("open_picks", {})[key] = value
        return state

    update_state_atomic(state_path, _update)


def test_concurrent_writers_no_clobber(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"open_picks": {}}), encoding="utf-8")

    workers = [
        mp.Process(target=_writer, args=(state_file, f"p{i}", {"pick": f"P{i}"}))
        for i in range(20)
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    final = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(final["open_picks"]) == 20, (
        f"expected 20 picks, got {len(final['open_picks'])} — "
        "concurrent writes clobbered"
    )


def test_update_state_atomic_callable_exists():
    """Sanity check that the helper exists with the expected signature."""
    from tennis_dry_run import update_state_atomic
    assert callable(update_state_atomic)


# ── save_state merge semantics ───────────────────────────────────────────────
#
# The bot's in-memory `state` is a snapshot taken at the top of `main()`.
# During the bot's run, the placer may have added new picks to disk that the
# bot doesn't know about. Plain `save_state(state)` would clobber those.
#
# Required merge:
#   final.open_picks = (disk.open_picks ∪ bot.open_picks) − settled_ids
#
# All other state keys (balance, wins, losses, total_pnl, …) come from the
# bot's in-memory snapshot — the bot is authoritative for those.


def test_save_state_merges_placer_added_picks(tmp_path):
    """save_state must preserve picks the placer wrote to disk while the
    bot was running (the bot doesn't know about them, but they must survive)."""
    from tennis_dry_run import save_state

    state_file = tmp_path / "state.json"

    # Disk state: the placer added p2_PLACER_ADDED while the bot was running.
    state_file.write_text(
        json.dumps({
            "balance": 999.0,  # stale — bot is authoritative
            "open_picks": {
                "p1": {"pick": "PlayerA", "stake": 25.0},
                "p2_PLACER_ADDED": {"pick": "PlayerB", "stake": 25.0},
            },
        }),
        encoding="utf-8",
    )

    # Bot's in-memory state: knows about p1 (carried from prior load_state),
    # added p3_BOT_ADDED itself, doesn't know about p2_PLACER_ADDED, settled
    # nothing this iteration.
    bot_state = {
        "balance": 500.0,
        "total_pnl": 10.0,
        "wins": 1,
        "losses": 0,
        "open_picks": {
            "p1": {"pick": "PlayerA", "stake": 25.0},
            "p3_BOT_ADDED": {"pick": "PlayerC", "stake": 25.0},
        },
    }

    save_state(bot_state, state_file=state_file)

    final = json.loads(state_file.read_text(encoding="utf-8"))
    assert set(final["open_picks"].keys()) == {"p1", "p2_PLACER_ADDED", "p3_BOT_ADDED"}, (
        f"expected union of disk + bot picks, got {set(final['open_picks'].keys())}"
    )
    # Bot is authoritative for balance / pnl / counters.
    assert final["balance"] == 500.0
    assert final["total_pnl"] == 10.0
    assert final["wins"] == 1


def test_save_state_with_settled_ids_removes_settled_but_preserves_placer(tmp_path):
    """When the bot settled a pick (p1), the merge must remove it from disk
    even though disk still has it — while preserving placer-added picks."""
    from tennis_dry_run import save_state

    state_file = tmp_path / "state.json"

    state_file.write_text(
        json.dumps({
            "balance": 999.0,
            "open_picks": {
                "p1": {"pick": "PlayerA", "stake": 25.0},
                "p2_PLACER_ADDED": {"pick": "PlayerB", "stake": 25.0},
            },
        }),
        encoding="utf-8",
    )

    # Bot: p1 was settled (removed from in-memory open_picks); doesn't know
    # about p2_PLACER_ADDED.
    bot_state = {
        "balance": 525.0,
        "total_pnl": 25.0,
        "wins": 2,
        "losses": 0,
        "open_picks": {},
    }

    save_state(bot_state, state_file=state_file, settled_ids={"p1"})

    final = json.loads(state_file.read_text(encoding="utf-8"))
    assert set(final["open_picks"].keys()) == {"p2_PLACER_ADDED"}, (
        f"expected p1 removed and p2_PLACER_ADDED preserved, "
        f"got {set(final['open_picks'].keys())}"
    )
    assert final["balance"] == 525.0
    assert final["wins"] == 2
