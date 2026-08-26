"""``cf_state_snapshot`` — the payload that turns a changelog entry into a change.

Two defects met here, both of which meant a custom field change reached
OpenProject as nothing at all:

* The Ruby template resolved the custom fields it journals by the names
  ``J2O Jira Workflow`` / ``J2O Jira Resolution`` / ``J2O Affects Version``.
  Those are created by ``WorkPackageMigration._ensure_j2o_custom_fields`` — the
  ``work_packages`` component, which is in neither ``DEFAULT_COMPONENT_SEQUENCE``
  nor the ``full`` profile. Probed against the live instance on 2026-08-26:
  ``j2o_legacy_cfs: {}``. ``j2o_cf_ids`` came out empty, so the entire
  ``customizable_journals`` block never ran and ``customizable_rows`` held only
  what other components had written.

* Python keyed the snapshot ``{"workflow": …, "resolution": …}`` and treated
  ``customfield_10500`` as the workflow scheme. On this Jira that id is **Bugs**
  (the Okapya checklist plugin) — it came from upstream's instance — so every
  Bugs value was filed as a workflow change before being dropped anyway.

The snapshot is now keyed by OpenProject custom field *name* and accumulates
across entries, so each journal carries the full state as of that moment and the
template can emit a row only where a value actually changed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.application.components.work_package_migration import WorkPackageMigration

JIRA_KEY = "ES-76"


@pytest.fixture
def component() -> WorkPackageMigration:
    with patch.object(WorkPackageMigration, "__init__", lambda self, **_: None):
        instance = WorkPackageMigration()  # type: ignore[call-arg]
    instance.logger = MagicMock()
    instance.user_mapping = {}
    instance.status_mapping = {}
    instance.issue_type_mapping = {}
    instance.markdown_converter = None
    instance.enhanced_audit_trail_migrator = MagicMock()
    instance.enhanced_audit_trail_migrator.extract_comments_from_issue.return_value = []
    return instance


def _issue() -> MagicMock:
    issue = MagicMock()
    issue.key = JIRA_KEY
    return issue


def _changelog(*entries: tuple[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    return [
        {
            "id": str(1000 + index),
            "created": created,
            "author": {"name": "melina.rosell"},
            "items": items,
        }
        for index, (created, items) in enumerate(entries)
    ]


def _item(field: str, to_string: str, *, from_string: str = "", field_id: str = "") -> dict[str, object]:
    return {
        "field": field,
        "fieldId": field_id,
        "from": None,
        "fromString": from_string,
        "to": None,
        "toString": to_string,
    }


def test_snapshot_is_keyed_by_custom_field_name(
    component: WorkPackageMigration,
) -> None:
    """Names, not ids: ids are not stable across installs.

    The template resolves them per batch, so a name is the only thing Python can
    send that survives a different OpenProject instance.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("resolution", "Fixed")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops[0]["cf_state_snapshot"] == {"Resolution": "Fixed"}
    assert "resolution" not in ops[0]["cf_state_snapshot"]


def test_snapshot_accumulates_across_entries(
    component: WorkPackageMigration,
) -> None:
    """Each journal ships the full state, which is what makes the diff work.

    The template compares a journal's snapshot against the previous one to
    decide whether to write a row. A snapshot holding only the field that
    changed in *this* entry would read as "every other field was cleared".
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("resolution", "Fixed")]),
        ("2026-02-04T10:00:00.000-0300", [_item("status", "Closed")]),
        ("2026-02-05T10:00:00.000-0300", [_item("resolution", "Won't Do", from_string="Fixed")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops[0]["cf_state_snapshot"] == {"Resolution": "Fixed"}
    # Carried, not dropped, even though this entry changed a different field.
    assert ops[1]["cf_state_snapshot"] == {"Resolution": "Fixed"}
    assert ops[2]["cf_state_snapshot"] == {"Resolution": "Won't Do"}


def test_a_custom_field_change_does_not_also_emit_a_note(
    component: WorkPackageMigration,
) -> None:
    """Otherwise the journal renders as a comment *and* a change.

    Producing both is what kept resolution looking like a comment in the
    activity tab even where the snapshot was correct.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("resolution", "Fixed")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops[0]["notes"] == ""
    assert "**resolution**" not in ops[0]["notes"]


def test_customfield_10500_is_not_treated_as_the_workflow_scheme(
    component: WorkPackageMigration,
) -> None:
    """On this Jira ``customfield_10500`` is Bugs, not Workflow.

    The id came from upstream's instance. While it stood, every Bugs value was
    filed as a workflow change.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [_item("Bugs", '{"items":[{"name":"revisar"}]}', field_id="customfield_10500")],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    snapshot = ops[0].get("cf_state_snapshot") or {}
    assert "Workflow" not in snapshot
    assert "workflow" not in snapshot


def test_no_tracked_custom_field_means_no_snapshot_key(
    component: WorkPackageMigration,
) -> None:
    """An op with nothing to snapshot must not carry an empty dict.

    The template branches on ``cf_snapshot.is_a?(Hash)``, so an empty hash costs
    a pointless pass over every op.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("status", "Closed")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert "cf_state_snapshot" not in ops[0]


def test_every_mapped_name_is_a_custom_field_the_pipeline_creates() -> None:
    """A name with no custom field behind it is lost history, not a no-op.

    Verified against the live instance's inventory (probe of 2026-08-26,
    ``cf_missing: []``): these are the names the ``custom_fields`` /
    ``resolutions`` / ``labels`` components actually produce.
    """
    known_cf_names = {
        "Resolution",
        "Labels",
        "Rank",
        "Sprint",
        "Story Points",
        "Bugs",
        "Flagged",
        "Security Level",
        "Affects Versions",
        "Votes",
    }

    assert set(WorkPackageMigration.JIRA_FIELD_TO_OP_CF_NAME.values()) <= known_cf_names


def test_map_is_keyed_lowercase_because_lookup_lowercases() -> None:
    """``jira_field.lower()`` is the lookup key, so an uppercase entry is dead."""
    for jira_field in WorkPackageMigration.JIRA_FIELD_TO_OP_CF_NAME:
        assert jira_field == jira_field.lower()
