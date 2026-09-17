r"""A console that is still working must not be reported as settled.

``_read_result_file`` polls for the JSON a Rails script writes, and gives up
early once ``is_executing()`` says the console has gone idle three checks in a
row — roughly 15 seconds. That shortcut exists for a good reason: waiting out
the full 600s window for a script that failed 7 seconds in cost one run 595
seconds of nothing.

But ``is_executing()`` read the pane's last line and accepted any IRB prompt on
it as idle. Right after a ``load``, that line is the prompt plus the command
the console echoed and is now evaluating. Every long script therefore looked
finished the moment it started.

The 2026-09-16 run shows what that costs at scale: ten ``QueryExecutionError``
raises, all from the same line, all reading "waited 15.0s". Between them they
took down five bulk-comment batches (13893 comments), the sprint/epic pass
(6943 work packages), the category backfill (3407) and story points (3262) —
four components reported as failures for work the console was in the middle of
doing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.infrastructure.openproject.rails_console_client import RailsConsoleClient


@pytest.fixture
def client() -> RailsConsoleClient:
    """A client with no tmux session behind it; only pure logic is exercised."""
    instance = RailsConsoleClient.__new__(RailsConsoleClient)
    instance.tmux_session_name = "rails_console"
    instance.window = 0
    instance.pane = 0
    instance._tmux_path = "/usr/bin/tmux"
    return instance


def _with_pane(client: RailsConsoleClient, pane: str) -> bool:
    with patch.object(client, "capture_pane_tail", return_value=pane):
        return client.is_executing()


# ── still working ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pane",
    [
        "open-project(prod):005> load '/tmp/j2o_bulk_ab12.rb'",
        "open-project(prod):8967> load '/tmp/j2o_query_1789611852_23367.rb'",
        ">> load '/tmp/j2o_runner_3b958054.rb'",
    ],
)
def test_an_echoed_command_is_not_an_idle_console(client: RailsConsoleClient, pane: str) -> None:
    """The verbatim shape behind all ten failures of the 2026-09-16 run."""
    assert _with_pane(client, pane) is True


def test_output_with_no_prompt_is_still_working(client: RailsConsoleClient) -> None:
    """Mid-script output means the script is mid-flight."""
    assert _with_pane(client, "J2O bulk item 412: saved id=7781") is True


def test_an_unreadable_pane_keeps_the_caller_waiting(client: RailsConsoleClient) -> None:
    """Unknown beats a wrong answer: a wrong "idle" discards real work."""
    with patch.object(client, "capture_pane_tail", side_effect=OSError("tmux gone")):
        assert client.is_executing() is True


# ── genuinely settled ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pane",
    [
        "open-project(prod):040>",
        "irb(main):001:0>",
        ">>",
    ],
)
def test_a_bare_prompt_is_idle(client: RailsConsoleClient, pane: str) -> None:
    """A finished script leaves a fresh prompt with nothing typed after it.

    This is what preserves the early give-up: a script that died without
    writing its result file is over, and the caller should hear so at once
    rather than polling for ten more minutes.
    """
    assert _with_pane(client, pane) is False


def test_a_continuation_prompt_is_idle(client: RailsConsoleClient) -> None:
    """Parked on an open buffer: waiting for input, not evaluating.

    No result file is coming, and the caller is better off being told.
    """
    assert _with_pane(client, "open-project(prod):053*") is False


def test_the_last_line_is_what_counts(client: RailsConsoleClient) -> None:
    """Earlier echoes in the scrollback say nothing about the current state."""
    pane = "open-project(prod):004> load '/tmp/previous.rb'\n=> true\nopen-project(prod):005>"

    assert _with_pane(client, pane) is False
