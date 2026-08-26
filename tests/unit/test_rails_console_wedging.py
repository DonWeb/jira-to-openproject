r"""Regression tests for the Rails console wedging outage of 2026-08-03.

Three consecutive migration runs died before any component executed, all with
``Console not ready after 90s`` and the pane parked at
``open-project(prod):053*``. Reproduced by hand against the live console: the
exact failing command runs cleanly, and wedges the console once the trailing
whitespace the script templates emit is appended.

Root cause and amplifiers, each covered below:

* the command carried a trailing ``\\n`` + indentation, so ``send-keys`` typed
  a whitespace-only line and pressed Enter *while the block it had just closed
  was still evaluating*. Reline 0.6.3 / IRB 1.18.0 corrupts its line buffer on
  input-during-eval; IRB 1.17.0 tolerated it, which is why identical bytes
  worked until the container was upgraded.
* the readiness check searched the whole line for ``>``, so a continuation
  line like ``open-project(prod):357*  rescue => e`` read as ready and the next
  command was typed into the open buffer.
* ``_stabilize_console`` sent space+Enter before Ctrl+C, appending yet another
  continuation line to the buffer it was meant to clear.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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


# ── the root cause ───────────────────────────────────────────────────────────


def test_command_is_stripped_before_reaching_tmux(client: RailsConsoleClient) -> None:
    r"""No whitespace-only line may follow the command.

    ``send-keys`` is called with the command *and* an explicit ``Enter``. A
    command ending in ``\\n`` + indentation therefore submits its last real
    line, then submits the leftover whitespace as a second line — landing while
    the console is still evaluating the first.
    """
    command = 'puts "hi"\nbegin\n  1\nend # MARKER\n        '
    sent: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        sent.append(argv)
        return MagicMock(stdout="open-project(prod):001>", returncode=0)

    with (
        patch.object(client, "_wait_for_console_ready", return_value=True),
        patch.object(client, "_wait_for_console_output", return_value=(True, "")),
        patch("subprocess.run", side_effect=fake_run),
        patch("time.sleep"),
    ):
        client._send_command_to_tmux(command, timeout=1)

    send_keys = [a for a in sent if "send-keys" in a]
    assert send_keys, "no send-keys call was issued"
    payload = send_keys[0][4]
    assert payload == command.rstrip()
    assert not payload.endswith((" ", "\n")), "trailing whitespace still reaches the console"


def test_script_templates_would_otherwise_emit_trailing_whitespace() -> None:
    r"""Pin the shape that made the strip necessary.

    The templates are indented triple-quoted literals, so they genuinely end in
    ``\\n`` plus indentation. If someone dedents them later the strip becomes
    belt-and-braces rather than load-bearing — but it must stay either way,
    since callers can pass any string.
    """
    from src.infrastructure.openproject import rails_console_client as mod

    source = mod.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    assert 'end # %s\n        """' in text, (
        "script_template no longer ends with indentation; the strip in "
        "_send_command_to_tmux is what keeps that from wedging the console"
    )


# ── the readiness detector ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "open-project(prod):053*",
        "open-project(prod):357*         rescue => e",
        "open-project(prod):350*           begin",
        'open-project(prod):046*             puts bt.join("\\n")[0..2000]',
        "open-project(prod):049*         ensure",
    ],
)
def test_continuation_prompt_is_never_reported_ready(client: RailsConsoleClient, line: str) -> None:
    """A ``*`` prompt means an open buffer, whatever the echoed source contains.

    ``rescue => e`` is the case that broke it: the old check asked whether
    ``">"`` appeared anywhere in the line, and ``=>`` satisfied that. The IRB
    preamble this client sends contains two such lines.
    """
    state = client._get_console_state(line)
    assert state["ready"] is False
    assert state["state"] == "awaiting_input"


@pytest.mark.parametrize(
    "line",
    [
        "open-project(prod):040>",
        "open-project(prod):113>         end # --SCRIPT_END--R",
        "irb(main):001:0>",
    ],
)
def test_complete_prompt_is_reported_ready(client: RailsConsoleClient, line: str) -> None:
    state = client._get_console_state(line)
    assert state["ready"] is True
    assert state["state"] == "ready"


def test_object_inspection_ending_in_angle_bracket_is_not_a_prompt(
    client: RailsConsoleClient,
) -> None:
    """``#<CustomField … has_comment: false>`` is output, not a prompt.

    Real pane content: ``find_by`` on a CustomField prints an inspection whose
    last line ends in ``>``. Treating that as a ready prompt would let the
    client fire the next command at a console that is still printing.
    """
    state = client._get_console_state(" formula: nil,\n has_comment: false>")
    assert state["ready"] is False


def test_unrecognised_output_is_not_reported_ready(client: RailsConsoleClient) -> None:
    """No prompt found means not ready — waiting is cheaper than guessing."""
    assert client._get_console_state("Loading production environment (Rails 8.1.3)")["ready"] is False
    assert client._get_console_state("")["ready"] is False


# ── telling a broken console from a broken script ────────────────────────────


def test_an_ordinary_ruby_error_is_not_a_fatal_console_error(
    client: RailsConsoleClient,
) -> None:
    """A NameError from a generated script must not read as a crashed console.

    Every Ruby error raised inside IRB carries ``IRB::Irb#run`` in its
    backtrace, so keeping that term on the fatal list flagged script bugs as
    terminal failures. Verbatim from the 2026-08-06 run, where the real defect
    was a JSON ``null`` reaching Ruby source.
    """
    pane = (
        "Ruby error: NameError: undefined local variable or method 'null' for main\n"
        "/tmp/j2o_runner_3b958054.rb:5:in '<top (required)>'\n"
        "/app/vendor/bundle/ruby/4.0.0/gems/irb-1.18.0/lib/irb.rb:193:in 'block in IRB::Irb#run'\n"
        "/app/vendor/bundle/ruby/4.0.0/gems/irb-1.18.0/lib/irb.rb:192:in 'IRB::Irb#run'\n"
    )

    assert client._has_fatal_console_error(pane) is False


@pytest.mark.parametrize(
    "pane",
    [
        "ungetbyte failed (IOError)",
        "Reline::ANSI#cursor_pos",
        "Reline::Core#readmultiline",
        "SystemStackError",
        "stack level too deep",
    ],
)
def test_genuine_console_failures_are_still_fatal(client: RailsConsoleClient, pane: str) -> None:
    """Removing the noisy term must not blunt the real signals."""
    assert client._has_fatal_console_error(pane) is True


def test_is_executing_distinguishes_working_from_settled(client: RailsConsoleClient) -> None:
    """A settled console — ready or wedged — is not going to produce anything."""
    with patch.object(client, "capture_pane_tail", return_value="open-project(prod):040>"):
        assert client.is_executing() is False
    with patch.object(client, "capture_pane_tail", return_value="open-project(prod):053*"):
        assert client.is_executing() is False
    # Mid-evaluation: output with no prompt yet.
    with patch.object(client, "capture_pane_tail", return_value="--EXEC_START--abc123"):
        assert client.is_executing() is True


def test_is_executing_assumes_working_when_it_cannot_tell(client: RailsConsoleClient) -> None:
    """An unreadable pane must never cut a legitimate wait short."""
    with patch.object(client, "capture_pane_tail", side_effect=OSError("no tmux")):
        assert client.is_executing() is True


def test_last_ruby_error_surfaces_the_script_failure(client: RailsConsoleClient) -> None:
    """The console holds the cause behind a result file that never appeared."""
    pane = (
        "--EXEC_START--abc\n"
        "Ruby error: NameError: undefined local variable or method 'null' for main\n"
        "/tmp/j2o_runner_3b958054.rb:5:in '<top (required)>'\n"
    )
    with patch.object(client, "capture_pane_tail", return_value=pane):
        assert client.last_ruby_error() == (
            "Ruby error: NameError: undefined local variable or method 'null' for main"
        )

    with patch.object(client, "capture_pane_tail", return_value="open-project(prod):040>"):
        assert client.last_ruby_error() is None


# ── recovery ─────────────────────────────────────────────────────────────────


def test_stabilize_sends_ctrl_c_before_anything_else(client: RailsConsoleClient) -> None:
    """Ctrl+C is the only key here that closes an open multi-line expression.

    Sending space+Enter first appends another continuation line to the very
    buffer being cleared. Confirmed live: one Ctrl+C took a session stuck at
    ``open-project(prod):053*`` back to a usable prompt.
    """
    sent: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        sent.append(argv)
        return MagicMock(stdout="", returncode=0)

    with patch("subprocess.run", side_effect=fake_run), patch("time.sleep"):
        client._stabilize_console()

    keys = [argv[-1] for argv in sent if "send-keys" in argv]
    assert keys[0] == "C-c", f"first key must be Ctrl+C, got {keys[0]!r}"
    assert keys.count("C-c") >= 2, "a console mid-evaluation needs a second interrupt"
    assert " " not in keys, "space+Enter deepens an open buffer instead of clearing it"
    # Clearing must come after the interrupt, or the evidence is wiped first.
    assert keys.index("C-l") > keys.index("C-c")
