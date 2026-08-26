"""Loose ends after the journal rebuild worked: F6, F8, and a third leak.

The 2026-08-20 re-run succeeded (391 work packages, 3691 journals, 0 failures),
which left three things visible that a failing run had masked:

* the orphan sweep removed **4299** rows, not the 3908 measured beforehand. The
  difference is exactly the 391 rebuilt work packages: replacing v1's payload
  inserts a new ``work_package_journals`` row and repoints ``data_id``, stranding
  the old one — one per rebuild, every run.
* the 44 issues with no Jira history are skipped before the template runs, so
  their creation journal keeps the anonymous author the rebuild would have fixed.
* ``_build_rails_ops_for_issue`` returned a partial operation list on an internal
  error, and the template deletes a work package's whole chain before rebuilding
  from what it is handed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.application.components.wp_journal_history_migration import (
    WpJournalHistoryMigration,
)

_TEMPLATE = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "ruby"
    / "create_work_package_journals_batch.rb"
).read_text(encoding="utf-8")


@pytest.fixture
def component() -> WpJournalHistoryMigration:
    with patch.object(WpJournalHistoryMigration, "__init__", lambda self, **_: None):
        instance = WpJournalHistoryMigration()  # type: ignore[call-arg]
    instance.logger = MagicMock()
    instance.op_client = MagicMock()
    instance.jira_client = MagicMock()
    instance._builder = None
    return instance


class TestPartialOperationListsDoNotEscape:
    """F6 — a half-built history must not replace a complete one."""

    def test_builder_reraises_instead_of_returning_what_it_has(self) -> None:
        source = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "application"
            / "components"
            / "work_package_migration.py"
        ).read_text(encoding="utf-8")

        marker = 'self.logger.exception("Failed to build rails_ops for %s", jira_key)'
        assert marker in source
        # The raise has to follow the log, not a bare ``return rails_ops``.
        assert source[source.index(marker) :].lstrip().splitlines()[1].strip() == "raise"

    def test_component_counts_a_build_failure_and_leaves_the_wp_alone(
        self,
        component: WpJournalHistoryMigration,
    ) -> None:
        """The counter existed but was unreachable while the builder swallowed."""
        builder = MagicMock()
        builder._build_rails_ops_for_issue.side_effect = RuntimeError("boom")
        component._get_builder = MagicMock(return_value=builder)  # type: ignore[method-assign]
        component._merge_batch_issues = MagicMock(return_value={"EF-38": MagicMock()})  # type: ignore[method-assign]

        with (
            patch("src.application.components.wp_journal_history_migration.config") as cfg,
            patch.object(WpJournalHistoryMigration, "_rails_script", return_value="RUBY"),
        ):
            cfg.mappings.get_mapping.return_value = {
                "10126": {"jira_key": "EF-38", "openproject_id": 1574},
            }
            result = component.run()

        component.op_client.execute_script_with_data.assert_not_called()
        assert result.details["skipped"] == {"ops_build_failed": 1}


class TestLoneCreationJournalAttribution:
    """F8 — the work packages the rebuild never reaches."""

    @pytest.fixture
    def script(self) -> str:
        return WpJournalHistoryMigration._v1_reattribution_script()

    def test_only_the_creation_journal_is_touched(self, script: str) -> None:
        assert "version: 1" in script
        assert "version > 1" not in script

    def test_builtin_ids_are_resolved_by_type_not_hardcoded(self, script: str) -> None:
        """1/2/3 are SystemUser/DeletedUser/AnonymousUser *on this instance only*."""
        assert "Principal.where(type: %w[AnonymousUser SystemUser DeletedUser]).pluck(:id)" in script

    def test_a_real_author_is_never_overwritten(self, script: str) -> None:
        """Also what makes the pass idempotent."""
        assert "unless builtin_ids.include?(j.user_id)" in script
        assert "stats['already_real'] += 1" in script

    def test_uses_update_columns_so_it_does_not_journal_itself(self, script: str) -> None:
        assert "j.update_columns(user_id: wp.author_id)" in script
        assert "j.save" not in script

    def test_work_packages_without_history_are_collected_and_passed(
        self,
        component: WpJournalHistoryMigration,
    ) -> None:
        builder = MagicMock()
        builder._build_rails_ops_for_issue.return_value = []
        component._get_builder = MagicMock(return_value=builder)  # type: ignore[method-assign]
        component._merge_batch_issues = MagicMock(return_value={"EF-38": MagicMock()})  # type: ignore[method-assign]
        component.op_client.execute_script_with_data.return_value = {
            "status": "success",
            "data": {"reattributed": 1, "already_real": 0},
        }

        with (
            patch("src.application.components.wp_journal_history_migration.config") as cfg,
            patch.object(WpJournalHistoryMigration, "_rails_script", return_value="RUBY"),
        ):
            cfg.mappings.get_mapping.return_value = {
                "10126": {"jira_key": "EF-38", "openproject_id": 1574},
            }
            result = component.run()

        payload = component.op_client.execute_script_with_data.call_args[0][1]
        assert payload == [{"work_package_id": 1574}]
        assert result.details["v1_reattributed"] == 1
        assert result.details["skipped"] == {"no_jira_history": 1}

    def test_no_rails_call_when_every_work_package_had_history(
        self,
        component: WpJournalHistoryMigration,
    ) -> None:
        component._reattribute_lone_creation_journals([], MagicMock())

        component.op_client.execute_script_with_data.assert_not_called()


class TestV1PayloadIsNotStranded:
    """The third leak, found by the sweep counting 391 more rows than expected."""

    def test_the_replaced_payload_row_is_deleted(self) -> None:
        assert "stale_data_id = v1_journal.data_id" in _TEMPLATE
        assert "Journal::WorkPackageJournal.where(id: stale_data_id).delete_all" in _TEMPLATE

    def test_it_is_captured_before_the_replacement(self) -> None:
        """Reading ``data_id`` after the assignment yields the new row's id."""
        assert _TEMPLATE.index("stale_data_id = v1_journal.data_id") < _TEMPLATE.index(
            "v1_journal.data = Journal::WorkPackageJournal.new(sanitized_state)",
        )

    def test_it_is_deleted_only_when_it_actually_changed(self) -> None:
        """A journal that had no payload, or kept the same row, must be left alone."""
        assert "if stale_data_id && stale_data_id != v1_journal.data_id" in _TEMPLATE
