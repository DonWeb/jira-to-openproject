"""One budget for a file-based query, watched twice.

``execute_large_query_to_json_file`` waits in two stages: the console watches
its pane for the script's completion marker, then the poller watches for the
JSON file over SSH. The two used to hold different numbers — 90s and 600s — so
any script slower than 90s logged a failure and then quietly succeeded on the
second wait.

``sprint_epic`` is one: its marker arrives at ~195s. The 2026-09-30 run logged

    ERROR  Marker '--EXEC_DONE--...' not found after 90s

for a component that went on to finish with ``updated=11918 failed=0``. Nothing
was wrong; the first wait was just shorter than the work.
"""

from __future__ import annotations

import pytest

from src.infrastructure.openproject import openproject_rails_runner_service as svc


def test_the_default_budget_is_the_full_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """90s was the console's own number, unrelated to what the caller waits."""
    monkeypatch.delenv("J2O_QUERY_RESULT_WAIT_SECONDS", raising=False)

    assert svc.result_wait_seconds() == 600


def test_the_override_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("J2O_QUERY_RESULT_WAIT_SECONDS", "42")

    assert svc.result_wait_seconds() == 42


def test_an_unparseable_override_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never to some unrelated shorter window — that shrank this wait 10x once."""
    monkeypatch.setenv("J2O_QUERY_RESULT_WAIT_SECONDS", "not-a-number")

    assert svc.result_wait_seconds() == 600


def test_the_console_wait_is_long_enough_for_a_sprint_epic_batch() -> None:
    """The regression this exists to stop, in the units it happened in.

    The failing marker wait was 90s against a script that needed ~195s.
    """
    assert svc.result_wait_seconds() > 195


def test_both_waits_read_the_same_source() -> None:
    """A second literal is how they drifted apart the first time.

    Not a style point: the console wait and the result-file poll bound the same
    operation, and a caller cannot reason about a budget that is really two.
    """
    source = svc.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    assert "timeout or 90" not in text, "the console path has its own hardcoded budget again"
    # Both the console path and the result-file poll go through the helper.
    assert text.count("result_wait_seconds()") >= 3
