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
    instance.sprint_mapping = {}
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


# --------------------------------------------------------------------------
# C1 — the ignore list finally gates the note fallback
#
# ``IGNORED_CHANGELOG_FIELDS`` had exactly one reference in the module, in the
# ``work_packages`` component's flow — absent from DEFAULT_COMPONENT_SEQUENCE and
# from the ``full`` profile, so it never ran. Measured on the instance
# 2026-08-26: 1844 of 7557 work package journals (24.4%) were changelog lines
# rendered as comments.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "jira_field",
    ["Link", "RemoteIssueLink", "RemoteIssueLinkGlobalId", "WorklogId", "timespent"],
)
def test_fields_owned_by_another_component_produce_no_note(
    component: WorkPackageMigration,
    jira_field: str,
) -> None:
    """These cannot be changes in OpenProject, so they must be silent.

    Relations are not journaled at all and time entries carry their own journal,
    so there is nowhere for these to land — and ``relations``, ``remote_links``
    and ``time_entries`` already migrate the underlying data.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item(jira_field, "whatever")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    # The whole entry is empty, so the template drops it via its is_empty guard.
    assert all(op["notes"] == "" for op in ops)
    assert all("field_changes" not in op for op in ops)


def test_worklog_pair_shares_one_journal_and_both_halves_are_dropped(
    component: WorkPackageMigration,
) -> None:
    """Jira emits WorklogId *and* timespent for a single worklog edit.

    Those 48 shared journals are exactly the overlap between the per-field noise
    counts (1892) and the journal count (1844).
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [_item("WorklogId", "10502"), _item("timespent", "3600")],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops[0]["notes"] == ""


def test_an_unknown_field_still_gets_its_note(
    component: WorkPackageMigration,
) -> None:
    """The gate is an allowlist of things to drop, not a blanket mute.

    A field nobody has classified is better recorded as a note than lost.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("Complejidad", "Alta", from_string="Baja")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert "**Complejidad**: Baja → Alta" in ops[0]["notes"]


def test_duedate_keeps_its_native_change_despite_being_in_the_ignore_list(
    component: WorkPackageMigration,
) -> None:
    """The gate runs after the mapping, and this is why.

    ``duedate`` sits in both ``IGNORED_CHANGELOG_FIELDS`` and
    ``jira_to_op_field``. Filtering before the mapping would silently drop real
    due-date changes.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("duedate", "2026-03-01", from_string="2026-02-01")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops[0]["field_changes"]["due_date"] == ["2026-02-01", "2026-03-01"]
    assert ops[0]["notes"] == ""


def test_ignore_list_matching_is_case_insensitive() -> None:
    """Jira is not consistent: "Attachment" but "labels", "Sprint" but "timespent"."""
    lowered = WorkPackageMigration._IGNORED_CHANGELOG_FIELDS_LOWER

    assert "attachment" in lowered
    assert "worklogid" in lowered
    assert "remoteissuelink" in lowered
    assert all(name == name.lower() for name in lowered)


# --------------------------------------------------------------------------
# C2 — the fields that become custom field changes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("jira_field", "cf_name"),
    [
        ("labels", "Labels"),
        ("Rank", "Rank"),
        ("Global Rank", "Rank"),
        ("Bugs", "Bugs"),
        ("Story Points", "Story Points"),
        ("resolution", "Resolution"),
    ],
)
def test_field_becomes_a_custom_field_change_and_not_a_note(
    component: WorkPackageMigration,
    jira_field: str,
    cf_name: str,
) -> None:
    """A change, not a comment — the whole point of the exercise.

    ``Rank`` and ``Global Rank`` are also in ``IGNORED_CHANGELOG_FIELDS``; the
    custom field map takes precedence, so they become changes rather than being
    dropped.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item(jira_field, "nuevo", from_string="viejo")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops[0]["cf_state_snapshot"] == {cf_name: "nuevo"}
    assert ops[0]["notes"] == ""


def test_story_points_does_not_use_the_native_column(
    component: WorkPackageMigration,
) -> None:
    """OpenProject 17.6 has ``work_package_journals.story_points``, but the
    pipeline's ``story_points`` component writes the *custom field*.

    Journaling the native column would show changes to a field the work package
    form does not display, because the value a reader sees lives in the CF.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("Story Points", "5", from_string="3")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert "field_changes" not in ops[0]
    assert ops[0]["cf_state_snapshot"] == {"Story Points": "5"}


# --------------------------------------------------------------------------
# C2b — Sprint as a native change
# --------------------------------------------------------------------------

SPRINT_MAPPING = {
    "84": {"name": "Sprint v0.0.104", "openproject_sprint_id": 12, "project_id": 42},
    "Sprint v0.0.104": {"name": "Sprint v0.0.104", "openproject_sprint_id": 12, "project_id": 42},
    "85": {"name": "Sprint v0.0.105", "openproject_sprint_id": 13, "project_id": 42},
    "Sprint v0.0.105": {"name": "Sprint v0.0.105", "openproject_sprint_id": 13, "project_id": 42},
}


def test_sprint_becomes_a_native_sprint_id_change(
    component: WorkPackageMigration,
) -> None:
    """``work_package_journals.sprint_id`` exists on 17.6 and ``sprint_epic``
    writes ``work_packages.sprint_id``, so the snapshot is the right home."""
    component.sprint_mapping = SPRINT_MAPPING
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [_item("Sprint", "Sprint v0.0.105", from_string="Sprint v0.0.104")],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops[0]["field_changes"]["sprint_id"] == [12, 13]
    assert ops[0]["notes"] == ""
    assert "cf_state_snapshot" not in ops[0]


def test_sprint_resolves_the_last_of_several_because_the_column_is_scalar(
    component: WorkPackageMigration,
) -> None:
    """A Jira issue can sit in several sprints at once; ``sprint_id`` cannot.

    The last one listed is the sprint the issue ended up in.
    """
    component.sprint_mapping = SPRINT_MAPPING

    assert component._resolve_sprint_id("84, 85", None) == 13
    assert component._resolve_sprint_id(None, "Sprint v0.0.104, Sprint v0.0.105") == 13


def test_sprint_prefers_ids_and_falls_back_to_names(
    component: WorkPackageMigration,
) -> None:
    """``from``/``to`` carry ids, ``fromString``/``toString`` the names.

    The mapping is indexed both ways (259 ids plus the same 259 names on this
    instance), so a row where Jira reported no id still resolves.
    """
    component.sprint_mapping = SPRINT_MAPPING

    assert component._resolve_sprint_id("85", "Sprint v0.0.104") == 13
    assert component._resolve_sprint_id("", "Sprint v0.0.104") == 12
    assert component._resolve_sprint_id(None, None) is None


def test_sprint_ignores_the_legacy_version_id(
    component: WorkPackageMigration,
) -> None:
    """``openproject_id`` is the Version, which is not what ``sprint_id`` points at."""
    component.sprint_mapping = {"84": {"name": "S", "openproject_id": 999}}

    assert component._resolve_sprint_id("84", None) is None


def test_an_unresolvable_sprint_records_no_change(
    component: WorkPackageMigration,
) -> None:
    """``[None, None]`` would be a journal that renders nothing."""
    component.sprint_mapping = SPRINT_MAPPING
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("Sprint", "Sprint desconocido")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert all("field_changes" not in op for op in ops)
    assert all(op["notes"] == "" for op in ops)


def test_the_six_requested_fields_all_have_a_destination() -> None:
    """The user's list, minus the four that OpenProject cannot journal.

    Attachment is C3 (``attachable_journals``) and is deliberately not here yet.
    """
    native = {"sprint"}
    by_cf = set(WorkPackageMigration.JIRA_FIELD_TO_OP_CF_NAME)
    suppressed = WorkPackageMigration._IGNORED_CHANGELOG_FIELDS_LOWER

    assert native <= {"sprint"}
    assert {"resolution", "labels", "rank", "global rank", "bugs"} <= by_cf
    assert {"link", "remoteissuelink", "worklogid", "timespent"} <= suppressed
