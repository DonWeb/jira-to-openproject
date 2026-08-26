"""Journal deletions must take their dependent rows with them.

An OpenProject journal keeps its payload in a side table: ``journals.data_id``
points at ``work_package_journals``, and ``customizable_journals.journal_id``
points back at the journal. ``delete_all`` issues one DELETE and skips callbacks
and ``dependent:`` associations, so deleting only from ``journals`` strands both.

Measured on this instance on 2026-08-20: 3908 orphaned ``work_package_journals``
rows accumulated across the June-August runs, most of them from
``cleanup_anonymous_comment_duplicates.py`` removing duplicate comment journals.
"""

from __future__ import annotations

import pytest

from scripts.cleanup_anonymous_comment_duplicates import _build_delete_script
from scripts.cleanup_orphan_journal_data import (
    _ORPHAN_PREDICATES,
    _build_parser,
    _build_script,
)


class TestDuplicateCleanupTakesDependentRows:
    """F10 — the source of the leak."""

    @pytest.fixture
    def script(self) -> str:
        return _build_delete_script([101, 102, 103])

    def test_data_rows_are_deleted_too(self, script: str) -> None:
        assert "Journal::WorkPackageJournal.where(id: data_ids).delete_all" in script

    def test_custom_value_rows_are_deleted_too(self, script: str) -> None:
        assert "Journal::CustomizableJournal.where(journal_id: ids).delete_all" in script

    def test_data_ids_are_read_before_the_journals_go(self, script: str) -> None:
        """Plucking after the DELETE would return an empty list, silently.

        The orphans would keep accumulating and the script would still report a
        healthy ``deleted`` count.
        """
        assert script.index("pluck(:data_id)") < script.index("Journal.where(id: ids).delete_all")

    def test_null_data_ids_are_excluded_from_the_pluck(self, script: str) -> None:
        """A journal with no payload contributes a NULL, which matches nothing."""
        assert "where.not(data_id: nil)" in script

    def test_only_work_package_payloads_are_targeted(self, script: str) -> None:
        """``data_id`` is unique per ``data_type``, not globally.

        Without the type filter an id belonging to some other journal payload
        table could be deleted from ``work_package_journals``.
        """
        assert "data_type: 'Journal::WorkPackageJournal'" in script

    def test_counts_are_reported_separately(self, script: str) -> None:
        assert "data_deleted:" in script
        assert "customizable_deleted:" in script


class TestOrphanCleanupScript:
    """F5 — removing what already leaked."""

    def test_dry_run_cannot_reach_the_delete(self) -> None:
        """The DELETE is present but gated; only ``--apply`` flips the gate."""
        dry = _build_script(apply=False)

        assert "do_apply = false" in dry
        assert "if do_apply && found > 0" in dry

    def test_apply_enables_it(self) -> None:
        assert "do_apply = true" in _build_script(apply=True)

    def test_both_orphan_tables_are_covered(self) -> None:
        script = _build_script(apply=True)

        for table in ("work_package_journals", "customizable_journals"):
            assert f"FROM {table} WHERE" in script

    def test_uses_not_exists_rather_than_not_in(self) -> None:
        """``NOT IN`` against a NULL-yielding subquery matches nothing at all.

        The predicate would evaluate to NULL for every row, so the script would
        cheerfully report zero orphans on an instance full of them.
        """
        script = _build_script(apply=True)

        assert script.count("NOT EXISTS") == 2
        assert "NOT IN" not in script

    def test_work_package_predicate_filters_on_data_type(self) -> None:
        assert "j.data_type = 'Journal::WorkPackageJournal'" in _ORPHAN_PREDICATES["work_package_journals"]

    def test_runs_in_a_single_transaction_and_rolls_back_on_error(self) -> None:
        """A partial clean would report counts that no longer describe the DB."""
        script = _build_script(apply=True)

        assert script.count("ActiveRecord::Base.transaction") == 1
        assert "raise ActiveRecord::Rollback if result['errors'].any?" in script

    def test_counts_are_taken_before_deleting(self) -> None:
        """``--apply`` must report what it found, not what survived."""
        script = _build_script(apply=True)

        assert script.index("result['found']") < script.index('conn.execute("DELETE')

    def test_predicates_use_a_non_interpolating_heredoc(self) -> None:
        """Ruby evaluates ``#{...}`` inside an interpolating heredoc."""
        assert "<<~'PRED'" in _build_script(apply=True)

    def test_defaults_to_dry_run(self) -> None:
        """Deleting has to be the thing you ask for, not the thing you get."""
        assert _build_parser().parse_args([]).apply is False

    def test_apply_flag_is_what_enables_deletion(self) -> None:
        assert _build_parser().parse_args(["--apply"]).apply is True

    def test_dry_run_and_apply_are_mutually_exclusive(self) -> None:
        """Passing both is a contradiction, and argparse should say so."""
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["--dry-run", "--apply"])
