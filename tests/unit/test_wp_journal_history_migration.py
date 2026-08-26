"""``wp_journal_history`` — the component that puts Jira's history in OpenProject.

The reconstruction logic already existed but nothing in the pipeline reached it:
``create_work_package_journals{,_batch}.rb`` are injected only by
``bulk_create_records``, which for work packages is called only from
:class:`WorkPackageMigration`, registered under the entity type
``work_packages`` — absent from both ``DEFAULT_COMPONENT_SEQUENCE`` and the
``full`` profile. Confirmed against the live instance: of 1773 journals on 520
migrated work packages, ``real_sin_notas`` was 0, i.e. every real-author journal
was a comment and not one was a changelog entry.

These tests pin the wiring and the ordering guarantees, which is where this
component can go wrong silently.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.application.components.registry import DEFAULT_COMPONENT_SEQUENCE
from src.application.components.wp_journal_history_migration import (
    WpJournalHistoryMigration,
)


@pytest.fixture
def component() -> WpJournalHistoryMigration:
    with patch.object(WpJournalHistoryMigration, "__init__", lambda self, **_: None):
        instance = WpJournalHistoryMigration()  # type: ignore[call-arg]
    instance.logger = MagicMock()
    instance.op_client = MagicMock()
    instance.jira_client = MagicMock()
    instance._builder = None
    return instance


# --------------------------------------------------------------------------
# Sequencing — the guarantees that make the rebuilt chain valid
# --------------------------------------------------------------------------


def test_runs_after_the_component_that_creates_comments() -> None:
    """It folds the comments into the chain, so they must already exist.

    Running before ``work_packages_content`` would rebuild a chain with no
    comments in it, and the comments appended afterwards would land out of
    chronological order.
    """
    sequence = list(DEFAULT_COMPONENT_SEQUENCE)

    assert sequence.index("wp_journal_history") > sequence.index("work_packages_content")


def test_runs_after_every_component_that_writes_work_packages() -> None:
    """A later ``wp.save!`` would append a journal onto the finished chain.

    That journal gets the current timestamp, so it would sit at the end of a
    chain whose other entries carry Jira dates — and it would be attributed to
    whoever the session runs as rather than to a Jira author.
    """
    wp_writers = [
        "wp_metadata_backfill",
        "sprint_epic",
        "versions",
        "components",
        "labels",
        "native_tags",
        "story_points",
        "estimates",
        "security_levels",
        "affects_versions",
        "customfields_generic",
        "inline_refs",
        "votes_reactions",
    ]
    sequence = list(DEFAULT_COMPONENT_SEQUENCE)
    position = sequence.index("wp_journal_history")

    for writer in wp_writers:
        assert sequence.index(writer) < position, f"{writer} must run before wp_journal_history"


def test_timestamp_restore_is_the_very_last_component() -> None:
    """Anything writing a work package after it re-introduces the drift."""
    assert DEFAULT_COMPONENT_SEQUENCE[-1] == "wp_timestamp_restore"


# --------------------------------------------------------------------------
# Payload shape — the contract the Ruby template reads
# --------------------------------------------------------------------------


def test_rails_script_is_the_batch_template() -> None:
    script = WpJournalHistoryMigration._rails_script()

    assert "input_data.each_with_index" in script
    assert "$j2o_start_marker" in script
    # It owns the chain: v2+ are deleted and rewritten, not appended to.
    assert "where('version > 1')" in script


def test_payload_uses_the_key_names_the_template_reads(
    component: WpJournalHistoryMigration,
) -> None:
    """The template reads ``wp_id``/``jira_key``/``rails_ops``.

    A mismatch here is invisible: the template would find no operations, skip
    every work package, and report ``created: 0`` with no error.
    """
    ops = [{"type": "journal", "notes": "hello", "version": 2}]
    builder = MagicMock()
    builder._build_rails_ops_for_issue.return_value = ops
    component._get_builder = MagicMock(return_value=builder)  # type: ignore[method-assign]
    component._merge_batch_issues = MagicMock(return_value={"EF-38": MagicMock()})  # type: ignore[method-assign]
    component.op_client.execute_script_with_data.return_value = {
        "status": "success",
        "data": [{"wp_id": 1574, "jira_key": "EF-38", "created": 1, "error": None}],
    }

    with (
        patch(
            "src.application.components.wp_journal_history_migration.config"
        ) as cfg,
        patch.object(WpJournalHistoryMigration, "_rails_script", return_value="RUBY"),
    ):
        cfg.mappings.get_mapping.return_value = {
            "10126": {"jira_key": "EF-38", "openproject_id": 1574},
        }
        result = component.run()

    payload = component.op_client.execute_script_with_data.call_args[0][1]
    assert payload == [{"wp_id": 1574, "jira_key": "EF-38", "rails_ops": ops}]
    assert result.success is True
    assert result.details["journals_created"] == 1
    assert result.details["wp_rebuilt"] == 1


def test_work_packages_without_jira_history_are_not_rebuilt(
    component: WpJournalHistoryMigration,
) -> None:
    """An issue with no comments and no changelog needs no rebuild.

    Sending an empty operation list would make the template delete v2+ for
    nothing. It does still get the v1 reattribution pass — the creation journal
    is the whole history, but its author is only correct once something fixes it,
    and the rebuild these work packages skip is what does that for the rest.
    """
    builder = MagicMock()
    builder._build_rails_ops_for_issue.return_value = []
    component._get_builder = MagicMock(return_value=builder)  # type: ignore[method-assign]
    component._merge_batch_issues = MagicMock(return_value={"EF-38": MagicMock()})  # type: ignore[method-assign]
    component.op_client.execute_script_with_data.return_value = {
        "status": "success",
        "data": {"reattributed": 1},
    }

    with (
        patch("src.application.components.wp_journal_history_migration.config") as cfg,
        patch.object(WpJournalHistoryMigration, "_rails_script", return_value="RUBY"),
    ):
        cfg.mappings.get_mapping.return_value = {
            "10126": {"jira_key": "EF-38", "openproject_id": 1574},
        }
        result = component.run()

    # Exactly one Rails call, and it is the reattribution pass — never the
    # rebuild template, whose payload key is ``rails_ops``.
    component.op_client.execute_script_with_data.assert_called_once()
    payload = component.op_client.execute_script_with_data.call_args[0][1]
    assert payload == [{"work_package_id": 1574}]
    assert result.details["skipped"] == {"no_jira_history": 1}
    assert result.details["v1_reattributed"] == 1


def test_per_work_package_errors_are_surfaced_not_swallowed(
    component: WpJournalHistoryMigration,
) -> None:
    """The template reports failures per row inside a successful envelope.

    Counting those rows as rebuilt is exactly the false-green pattern that made
    an earlier run report 435 updated while every comment failed.
    """
    builder = MagicMock()
    builder._build_rails_ops_for_issue.return_value = [{"type": "journal"}]
    component._get_builder = MagicMock(return_value=builder)  # type: ignore[method-assign]
    component._merge_batch_issues = MagicMock(return_value={"EF-38": MagicMock()})  # type: ignore[method-assign]
    component.op_client.execute_script_with_data.return_value = {
        "status": "success",
        "data": [
            {"wp_id": 1574, "jira_key": "EF-38", "created": 0, "error": "WP not found"},
        ],
    }

    with (
        patch("src.application.components.wp_journal_history_migration.config") as cfg,
        patch.object(WpJournalHistoryMigration, "_rails_script", return_value="RUBY"),
    ):
        cfg.mappings.get_mapping.return_value = {
            "10126": {"jira_key": "EF-38", "openproject_id": 1574},
        }
        result = component.run()

    assert result.success is False
    assert result.details["wp_failed"] == 1
    assert result.details["wp_rebuilt"] == 0
    assert "EF-38: WP not found" in result.details["wp_errors"]


def test_missing_work_package_mapping_fails_loudly(
    component: WpJournalHistoryMigration,
) -> None:
    with patch("src.application.components.wp_journal_history_migration.config") as cfg:
        cfg.mappings.get_mapping.return_value = {}
        result = component.run()

    assert result.success is False
    assert "missing_work_package_mapping" in result.errors


def test_does_not_support_change_detection(
    component: WpJournalHistoryMigration,
) -> None:
    """Opting out explicitly, the pattern the five 0/0 components needed.

    A component returning an aggregate wrapper ``ChangeDetector`` cannot read an
    id from reports ``baseline=0, current=0`` and skips its real work while
    reporting success.
    """
    with pytest.raises(ValueError, match="does not support idempotent workflow"):
        component._get_current_entities_for_type("wp_journal_history")


# --------------------------------------------------------------------------
# Comment provenance — so a rerun does not duplicate comments
# --------------------------------------------------------------------------


def test_rebuilt_comments_keep_the_provenance_marker() -> None:
    """``work_packages_content`` keys its idempotency on the marker.

    This component replaces the journals content created. Without carrying the
    marker across, the next ``work_packages_content`` run finds no evidence the
    comment was migrated and appends a duplicate.
    """
    from src.application.components import work_package_migration as wpm

    source = wpm.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    assert '_build_comment_with_marker(notes, entry_data.get("id"))' in text
