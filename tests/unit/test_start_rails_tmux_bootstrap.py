"""The tmux session has to be created and given its scrollback in one call.

Two constraints pull against each other:

* a pane allocates its scrollback when it is created, so raising
  ``history-limit`` afterwards never reaches the pane the Rails console runs in;
* ``set-option`` cannot run first on its own, because with no server up it has
  nothing to connect to. On the lab host it exited 1 with
  ``error connecting to /tmp/tmux-0/default`` and took the whole script down
  before it created anything.

One command list satisfies both: tmux starts a server for the list, applies the
option, then creates the session. Splitting them again — in either order —
reintroduces one failure or the other, which is what these tests pin.

``scripts/`` is not a package, so the module is loaded through importlib, the
same way ``test_normalize_wp_mapping_script.py`` does it.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "start_rails_tmux.py"


def _load_script_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("start_rails_tmux_under_test", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Run ``start_tmux_session`` against a stubbed tmux, returning the argv list."""
    module = _load_script_module()
    recorded: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(module, "subprocess", type("S", (), {"run": staticmethod(fake_run)}))
    module.start_tmux_session(
        session="rails_console",
        ssh_host="op-host",
        ssh_user="root",
        container="op-container",
        log_path=Path("/dev/null"),
    )
    return recorded


def _bootstrap_call(calls: list[list[str]]) -> list[str]:
    matching = [c for c in calls if "new-session" in c]
    assert len(matching) == 1, "the session must be created exactly once"
    return matching[0]


def test_scrollback_and_session_creation_share_one_invocation(calls: list[list[str]]) -> None:
    """Two separate calls is the shape that failed on the lab host."""
    bootstrap = _bootstrap_call(calls)

    assert "set-option" in bootstrap, (
        "history-limit is set in a separate tmux call; with no server running "
        "that call exits 1 and the script never creates the session"
    )


def test_the_option_is_applied_before_the_session_exists(calls: list[list[str]]) -> None:
    """Order inside the list is what makes the pane inherit the new value."""
    bootstrap = _bootstrap_call(calls)

    assert bootstrap.index("set-option") < bootstrap.index("new-session")


def test_the_two_commands_are_separated_the_way_tmux_expects(calls: list[list[str]]) -> None:
    """A bare ``;`` as its own argv entry — there is no shell here to unescape one."""
    bootstrap = _bootstrap_call(calls)

    assert ";" in bootstrap
    assert bootstrap.index("set-option") < bootstrap.index(";") < bootstrap.index("new-session")


def test_the_configured_history_limit_is_the_one_sent(calls: list[list[str]]) -> None:
    """Guards against the value drifting away from the constant."""
    module = _load_script_module()
    bootstrap = _bootstrap_call(calls)

    assert str(module.HISTORY_LIMIT) in bootstrap
    assert module.HISTORY_LIMIT > 2000, "2000 is the tmux default this exists to raise"


def test_the_console_command_still_carries_ssh_keepalives(calls: list[list[str]]) -> None:
    """A dropped idle connection reads as a wedged console, so this must survive."""
    send_keys = [c for c in calls if "send-keys" in c]
    assert send_keys, "the ssh command is never sent"
    payload = " ".join(send_keys[0])

    assert "ServerAliveInterval=30" in payload
