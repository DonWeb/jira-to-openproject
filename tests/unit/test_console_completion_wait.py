r"""The suppressed-output path must wait for the script, not for the paste.

``execute(..., suppress_output=True)`` is how every file-based operation runs:
bulk work-package creation, large JSON queries, the journal batches. It used to
send its script and then ask only whether the console showed a prompt — which,
immediately after a paste, is the prompt on the *echoed command line*. The call
therefore returned within a second of sending a script that had barely begun.

Callers took that as "finished" and fell through to polling for the result file
with a much shorter window of their own (180s for bulk create). A batch that
outran that window looked like a failure and was re-submitted in sub-batches
while the first copy was still writing rows — duplicate work packages, and the
more issues in the project the likelier it got.

The fix waits for a completion marker the script prints. The marker cannot be
spelled literally in the source, because tmux echoes the source into the same
pane the wait reads: Ruby concatenates it at run time, so only real output
matches.
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
    instance.command_timeout = 180
    instance.inactivity_timeout = 30
    from src.utils.file_manager import FileManager

    instance.file_manager = FileManager()
    return instance


def _capture_execute(client: RailsConsoleClient, *, suppress_output: bool) -> tuple[str, str | None]:
    """Run ``execute`` against a stubbed tmux; return (sent script, awaited marker)."""
    recorded: dict[str, object] = {}

    def fake_send(
        command: str,
        timeout: int,
        wait_for_line: str | None = None,
        script_end_marker: str | None = None,
    ) -> str:
        recorded["command"] = command
        recorded["wait_for_line"] = wait_for_line
        # Enough of a pane for the non-suppressed parser to find its markers.
        start = next(ln for ln in command.split("\n") if "--EXEC_START--" in ln)
        marker_id = start.split("--EXEC_START--")[1].split('"')[0]
        return f"--EXEC_START--{marker_id}\nok\n--EXEC_END--{marker_id}"

    with patch.object(client, "_send_command_to_tmux", side_effect=fake_send):
        client.execute("Project.count", timeout=5, suppress_output=suppress_output)

    return str(recorded["command"]), recorded["wait_for_line"]  # type: ignore[return-value]


def test_suppressed_execute_waits_for_a_completion_marker(client: RailsConsoleClient) -> None:
    """Returning on the prompt alone is what let callers race their own script."""
    _command, wait_for_line = _capture_execute(client, suppress_output=True)

    assert wait_for_line is not None, (
        "the suppressed path sent its script and waited for nothing; the caller "
        "cannot tell a finished script from one that just started"
    )
    assert "--EXEC_DONE--" in wait_for_line


def test_completion_marker_never_appears_verbatim_in_the_script(client: RailsConsoleClient) -> None:
    """The pane the wait reads also carries tmux's echo of the script.

    A marker spelled literally in the source is matched by its own echo, so the
    wait succeeds before Ruby has run a line of it.
    """
    command, wait_for_line = _capture_execute(client, suppress_output=True)

    assert wait_for_line is not None
    assert wait_for_line not in command, (
        f"marker {wait_for_line!r} is spelled verbatim in the script; the echo will satisfy the wait immediately"
    )
    # It is emitted, just split across a Ruby concatenation.
    marker_id = wait_for_line.removeprefix("--EXEC_DONE--")
    assert f'"--EXEC_" + "DONE--{marker_id}"' in command


def test_marker_line_is_still_filtered_out_of_parsed_output() -> None:
    """The extra marker must not leak into the output callers parse."""
    from src.infrastructure.openproject.rails_console_client import filter_console_output_lines

    kept = filter_console_output_lines(["42", "--EXEC_DONE--abc123", "--EXEC_END--abc123"])

    assert kept == "42"


def test_unsuppressed_execute_still_keys_off_the_script_end_echo(client: RailsConsoleClient) -> None:
    """The output-returning path is untouched: it has a working signal already.

    670 archived pane captures parse correctly through the script-end echo plus
    ``--EXEC_END--``; this path must keep using them.
    """
    _command, wait_for_line = _capture_execute(client, suppress_output=False)

    assert wait_for_line is not None
    assert "--EXEC_END--" in wait_for_line
    assert "--EXEC_DONE--" not in wait_for_line
