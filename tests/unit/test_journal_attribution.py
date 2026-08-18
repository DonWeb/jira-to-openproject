"""Journal attribution and timestamps on migrated work packages.

Measured against the live 17.6.0 instance on 2026-08-18, over the 520 work
packages carrying the ``J2O Origin Key`` custom field:

    {anonymous: 3, system: 1, current: 3, current_class: "AnonymousUser"}
    {v1_total: 520, v1_anonimos: 520, v2mas_anonimos: 653}
    {real_sin_notas: 0, real_con_notas: 600}
    {min: 1, max: 3, prom: 2.26, hist: [[1, 23], [2, 341], [3, 156]]}

Two thirds of the activity on migrated work packages was attributed to
Anonymous, every ``updated_at`` pointed at the migration window rather than at
Jira (median drift ~132 days), and not one journal carried a changelog change.
These tests pin the properties that fix each of those.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.application.components.wp_timestamp_restore_migration import (
    WpTimestampRestoreMigration,
)
from src.utils import rails_journal_user

_RUBY_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "ruby"


# --------------------------------------------------------------------------
# C1 — the journal author is a real user, configured, never Anonymous
# --------------------------------------------------------------------------


def test_unconfigured_journal_user_falls_back_to_system_not_anonymous() -> None:
    """An unset setting must still steer away from ``User.anonymous``.

    A fresh Rails console has ``User.current == User.anonymous``, so "do
    nothing" is not a neutral default — it is the broken one.
    """
    with patch.object(rails_journal_user, "_configured_user", return_value=""):
        command = rails_journal_user.console_command()

    assert "User.system" in command
    assert "User.current =" in command
    assert "User.anonymous" not in command


def test_numeric_setting_resolves_by_id_and_text_setting_by_login() -> None:
    with patch.object(rails_journal_user, "_configured_user", return_value="42"):
        assert rails_journal_user.lookup_expression() == "User.find_by(id: 42)"

    with patch.object(rails_journal_user, "_configured_user", return_value="migracion.jira"):
        assert rails_journal_user.lookup_expression() == "User.find_by(login: 'migracion.jira')"


def test_login_is_emitted_as_a_single_quoted_ruby_literal() -> None:
    """Double-quoted Ruby literals interpolate ``#{...}``; single-quoted do not.

    Same lesson as ``openproject_issue_priority_service``: escaping for JSON is
    not escaping for Ruby. ``json.dumps`` would emit a double-quoted literal and
    hand the console an interpolation site.
    """
    hostile = "admin'; system('rm -rf /'); '"
    with patch.object(rails_journal_user, "_configured_user", return_value=hostile):
        expression = rails_journal_user.lookup_expression()

    assert expression.startswith("User.find_by(login: '")
    assert '"' not in expression
    # The quote that would have closed the literal early is escaped.
    assert "\\'" in expression


def test_console_command_is_a_single_line() -> None:
    """Multi-line input is what wedged this console before (commit 33fecbb).

    An unterminated block sent to IRB leaves it in continuation state, silently
    accumulating later commands instead of running them.
    """
    with patch.object(rails_journal_user, "_configured_user", return_value="7"):
        command = rails_journal_user.console_command()

    assert "\n" not in command.strip()


def test_prepend_to_script_is_idempotent() -> None:
    """Retry paths may pass an already-prefixed script back through."""
    with patch.object(rails_journal_user, "_configured_user", return_value=""):
        once = rails_journal_user.prepend_to_script("puts 1")
        twice = rails_journal_user.prepend_to_script(once)

    assert twice == once
    assert once.endswith("puts 1")


def test_script_preamble_prints_nothing_on_the_happy_path() -> None:
    """Stray stdout would corrupt the JSON markers callers parse."""
    with patch.object(rails_journal_user, "_configured_user", return_value=""):
        preamble = rails_journal_user.script_preamble()

    # The only ``puts`` allowed is the one inside the rescue branch.
    assert preamble.count("puts") == 1
    assert "rescue" in preamble


# --------------------------------------------------------------------------
# C4 — no hardcoded builtin user ids
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    ["create_work_package_journals.rb", "create_work_package_journals_batch.rb"],
)
def test_journal_templates_do_not_hardcode_a_builtin_user_id(template: str) -> None:
    """``: 2`` was ``DeletedUser`` on this instance, not a safe fallback.

    Builtin ids are not stable across installs — here 1 is SystemUser, 2
    DeletedUser and 3 AnonymousUser — so the fallback has to be resolved from
    the database.
    """
    source = (_RUBY_DIR / template).read_text(encoding="utf-8")

    assert "rec.author_id : 2" not in source
    assert "j2o_fallback_user_id" in source
    assert "User.find_by(admin: true)" in source


def test_batch_template_resolves_the_fallback_once_outside_the_loop() -> None:
    """Resolving it per operation would be an N+1 against ``users``."""
    source = (_RUBY_DIR / "create_work_package_journals_batch.rb").read_text(encoding="utf-8")

    assignment = source.index("j2o_fallback_user_id = User.find_by(admin: true)")
    loop_start = source.index("input_data.each_with_index")

    assert assignment < loop_start


# --------------------------------------------------------------------------
# C2 — Jira's timestamps get restored, without creating a journal doing it
# --------------------------------------------------------------------------


@pytest.fixture
def restore_component() -> WpTimestampRestoreMigration:
    with (
        patch.object(WpTimestampRestoreMigration, "__init__", lambda self, **_: None),
    ):
        component = WpTimestampRestoreMigration()  # type: ignore[call-arg]
    component.logger = MagicMock()
    component.timestamp_migrator = MagicMock()
    component.timestamp_migrator._normalize_timestamp = lambda value: value
    return component


def test_restore_script_uses_update_columns_and_never_save() -> None:
    """``save``/``update`` would fire callbacks and journal the change.

    Restoring a timestamp must not itself produce the activity entry this
    component exists to undo.
    """
    script = WpTimestampRestoreMigration._rails_script()

    assert "update_columns" in script
    assert "wp.save" not in script
    assert "wp.update(" not in script


def test_restore_script_counts_already_correct_work_packages_separately() -> None:
    """A rerun that changes nothing must say so.

    Reporting every row as ``updated`` regardless is the "false green" pattern
    that made an earlier run look successful while writing nothing.
    """
    script = WpTimestampRestoreMigration._rails_script()

    assert "'unchanged'" in script
    assert "wp.updated_at.to_i != want.to_i" in script


def test_restore_script_emits_the_json_markers() -> None:
    """Without the markers the runner returns status="error" and no data."""
    script = WpTimestampRestoreMigration._rails_script()

    assert "$j2o_start_marker" in script
    assert "$j2o_end_marker" in script


def test_build_record_carries_both_timestamps(
    restore_component: WpTimestampRestoreMigration,
) -> None:
    issue = MagicMock()
    issue.fields.created = "2026-01-19T17:49:22.000+0000"
    issue.fields.updated = "2026-03-01T08:00:00.000+0000"

    record = restore_component._build_record(1312, issue)

    assert record == {
        "work_package_id": 1312,
        "created_at": "2026-01-19T17:49:22.000+0000",
        "updated_at": "2026-03-01T08:00:00.000+0000",
    }


def test_build_record_returns_none_when_jira_has_no_timestamps(
    restore_component: WpTimestampRestoreMigration,
) -> None:
    """Nothing to restore must not become a Rails call with an empty payload."""
    issue = MagicMock()
    issue.fields.created = None
    issue.fields.updated = None

    assert restore_component._build_record(1312, issue) is None


def test_build_record_reads_dict_shaped_issues(
    restore_component: WpTimestampRestoreMigration,
) -> None:
    """``_merge_batch_issues`` yields dicts on the batch-processor path."""
    issue = {"fields": {"created": "2026-02-03T17:03:16.000+0000", "updated": None}}

    record = restore_component._build_record(1326, issue)

    assert record is not None
    assert record["created_at"] == "2026-02-03T17:03:16.000+0000"
    assert record["updated_at"] is None
