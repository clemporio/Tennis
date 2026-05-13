"""Tests for scrape_completed_results — focus on tiebreak superscript handling.

Bug history: TennisExplorer renders tiebreak set scores as
`<td class="score">6<sup>10</sup></td>`. `get_text(strip=True)` flattens that
to "610", which `int()` then reads as six hundred and ten, beating the
opponent's "7". The 7-6 set was therefore credited to the loser, sometimes
inverting the overall match winner. See 2026-05-12 Zverev/Rublev settlements.
"""

from __future__ import annotations

from types import SimpleNamespace

import tennis_dry_run


def _fake_response(html: str):
    return SimpleNamespace(text=html, raise_for_status=lambda: None)


# Two matches from 2026-05-12 Rome, both decided by a 7-6 set won by the
# eventual match winner. Cell shapes mirror TennisExplorer output verbatim.
TIEBREAK_HTML = """
<table class="result">
  <tr class="head"><td>ATP - Rome</td></tr>

  <tr>
    <td class="time">12:00</td>
    <td class="t-name">Darderi L.(18)</td>
    <td class="score">1</td>
    <td class="score">7</td>
    <td class="score">6</td>
    <td class="score">&nbsp;</td>
    <td class="score">&nbsp;</td>
  </tr>
  <tr>
    <td class="time"></td>
    <td class="t-name">Zverev A.(2)</td>
    <td class="score">6</td>
    <td class="score">6<sup>10</sup></td>
    <td class="score">0</td>
    <td class="score">&nbsp;</td>
    <td class="score">&nbsp;</td>
  </tr>

  <tr>
    <td class="time">14:00</td>
    <td class="t-name">Rublev A.(12)</td>
    <td class="score">3</td>
    <td class="score">7</td>
    <td class="score">6</td>
    <td class="score">&nbsp;</td>
    <td class="score">&nbsp;</td>
  </tr>
  <tr>
    <td class="time"></td>
    <td class="t-name">Basilashvili N.</td>
    <td class="score">6</td>
    <td class="score">6<sup>5</sup></td>
    <td class="score">2</td>
    <td class="score">&nbsp;</td>
    <td class="score">&nbsp;</td>
  </tr>
</table>
"""


def test_tiebreak_superscript_does_not_flip_set_winner(monkeypatch):
    monkeypatch.setattr(
        tennis_dry_run.requests, "get",
        lambda *a, **kw: _fake_response(TIEBREAK_HTML),
    )

    results = tennis_dry_run.scrape_completed_results()

    by_pair = {
        frozenset((r["player_a"], r["player_b"])): r for r in results
    }

    darderi_zverev = by_pair[frozenset(("Darderi L.", "Zverev A."))]
    assert darderi_zverev["winner"] == "Darderi L.", (
        "Darderi won 1-6, 7-6(10), 6-0; tiebreak superscript must not promote "
        f"Zverev's '610' to 610. Got winner={darderi_zverev['winner']!r}."
    )

    rublev_basilashvili = by_pair[frozenset(("Rublev A.", "Basilashvili N."))]
    assert rublev_basilashvili["winner"] == "Rublev A.", (
        "Rublev won 3-6, 7-6(5), 6-2; tiebreak superscript must not promote "
        f"Basilashvili's '65' to 65. Got winner={rublev_basilashvili['winner']!r}."
    )


def test_straight_sets_without_tiebreak_still_parses(monkeypatch):
    """Regression guard: the simple 6-3, 6-4 path must keep working."""
    html = """
    <table class="result">
      <tr class="head"><td>ATP - Rome</td></tr>
      <tr>
        <td class="time">12:00</td>
        <td class="t-name">Winner W.</td>
        <td class="score">6</td>
        <td class="score">6</td>
        <td class="score">&nbsp;</td>
      </tr>
      <tr>
        <td class="time"></td>
        <td class="t-name">Loser L.</td>
        <td class="score">3</td>
        <td class="score">4</td>
        <td class="score">&nbsp;</td>
      </tr>
    </table>
    """
    monkeypatch.setattr(
        tennis_dry_run.requests, "get",
        lambda *a, **kw: _fake_response(html),
    )

    results = tennis_dry_run.scrape_completed_results()
    assert len(results) == 1
    assert results[0]["winner"] == "Winner W."
