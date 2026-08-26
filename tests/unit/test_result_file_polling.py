"""Regression tests: waiting for a result file must end when the script does.

The Rails scripts write their output to a file inside the container, which the
Python side then reads over SSH. If the script dies before writing, that file
never appears — but the poll loop had no way to know, so it kept asking for up
to 600 seconds.

On 2026-08-06 that cost a migration 595 seconds on a script that had failed
7 seconds in: 38% of the whole run spent re-asking for a file that could not
exist, followed by a retry that succeeded in two seconds. The console had been
sitting at a prompt the entire time.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.openproject.openproject_rails_runner_service import (
    OpenProjectRailsRunnerService,
)

_MISSING = ("", "cat: /tmp/j2o_query_x.json: No such file or directory", 1)


def _service(*, is_executing: bool, ruby_error: str | None = None) -> OpenProjectRailsRunnerService:
    client = MagicMock()
    client.logger = MagicMock()
    client.container_name = "openproject-web-1"
    client.ssh_client.execute_command = MagicMock(return_value=_MISSING)
    client.rails_client.is_executing = MagicMock(return_value=is_executing)
    client.rails_client.last_ruby_error = MagicMock(return_value=ruby_error)

    service = OpenProjectRailsRunnerService.__new__(OpenProjectRailsRunnerService)
    service._client = client
    service._logger = client.logger
    return service


def _poll(service: OpenProjectRailsRunnerService) -> None:
    """Drive the read loop with sleeps removed."""
    with patch("time.sleep"):
        service._read_result_file("/tmp/j2o_query_x.json")


# ── the settled-console short circuit ────────────────────────────────────────


def test_gives_up_once_the_console_has_settled() -> None:
    """A console back at its prompt with no file means the script died."""
    service = _service(is_executing=False)

    with pytest.raises(QueryExecutionError, match="finished without writing"):
        _poll(service)

    # Three idle observations, ~5s apart — seconds, not the 600s ceiling.
    attempts = service._client.ssh_client.execute_command.call_count
    assert attempts < 60, f"gave up only after {attempts} polls; should short-circuit"


def test_the_error_carries_the_ruby_cause() -> None:
    """``No such file or directory`` alone explains nothing.

    The console still holds the real failure; putting it in the exception is
    the difference between a one-line diagnosis and an investigation.
    """
    service = _service(
        is_executing=False,
        ruby_error="Ruby error: NameError: undefined local variable or method 'null' for main",
    )

    with pytest.raises(QueryExecutionError, match="undefined local variable or method 'null'"):
        _poll(service)


def test_keeps_waiting_while_the_script_is_still_running() -> None:
    """A slow query must not be mistaken for a dead one."""
    service = _service(is_executing=True)

    with pytest.raises(QueryExecutionError):
        _poll(service)

    # Ran the full window rather than short-circuiting.
    assert service._client.ssh_client.execute_command.call_count > 1000


def test_a_console_it_cannot_inspect_does_not_shorten_the_wait() -> None:
    """Missing or stubbed console introspection must fail safe, not fail fast."""
    service = _service(is_executing=False)
    del service._client.rails_client.is_executing

    with pytest.raises(QueryExecutionError):
        _poll(service)

    assert service._client.ssh_client.execute_command.call_count > 1000


def test_a_file_that_arrives_late_is_still_read() -> None:
    """The short circuit must not pre-empt a write that lands during the grace period."""
    service = _service(is_executing=False)
    payload = '{"ok": true}'
    service._client.ssh_client.execute_command = MagicMock(
        side_effect=[_MISSING, _MISSING, (payload, "", 0)],
    )

    with patch("time.sleep"):
        assert service._read_result_file("/tmp/j2o_query_x.json") == {"ok": True}
