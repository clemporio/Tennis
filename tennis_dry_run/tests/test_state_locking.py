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
