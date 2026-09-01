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


def _real(ops: list[dict]) -> list[dict]:
    """The chain without the creation journal.

    ``ops[0]`` is always the synthetic creation entry now (see the N3 section
    below), so a test about a changelog entry or a comment means ``ops[1]``.
    Asserting that here keeps every one of those tests honest about the
    difference instead of silently shifting an index.
    """
    assert ops and ops[0]["version"] == 1, "the first operation must be the creation journal"
    return ops[1:]


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

    assert _real(ops)[0]["cf_state_snapshot"] == {"Resolution": "Fixed"}
    assert "resolution" not in _real(ops)[0]["cf_state_snapshot"]


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

    assert _real(ops)[0]["cf_state_snapshot"] == {"Resolution": "Fixed"}
    # Carried, not dropped, even though this entry changed a different field.
    assert _real(ops)[1]["cf_state_snapshot"] == {"Resolution": "Fixed"}
    assert _real(ops)[2]["cf_state_snapshot"] == {"Resolution": "Won't Do"}


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

    assert _real(ops)[0]["notes"] == ""
    assert "**resolution**" not in _real(ops)[0]["notes"]


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

    snapshot = _real(ops)[0].get("cf_state_snapshot") or {}
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

    assert "cf_state_snapshot" not in _real(ops)[0]


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
        "Bugs",
        "Flagged",
        "Security Level",
        "Affects Versions",
        "Votes",
        # Provenance field, already created by the pipeline (id 5). Receives the
        # Jira key when an issue is moved between projects.
        "J2O Origin Key",
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

    assert _real(ops)[0]["notes"] == ""


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

    assert "**Complejidad**: Baja → Alta" in _real(ops)[0]["notes"]


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

    assert _real(ops)[0]["field_changes"]["due_date"] == ["2026-02-01", "2026-03-01"]
    assert _real(ops)[0]["notes"] == ""


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

    assert _real(ops)[0]["cf_state_snapshot"] == {cf_name: "nuevo"}
    assert _real(ops)[0]["notes"] == ""


def test_story_points_uses_the_native_column(
    component: WorkPackageMigration,
) -> None:
    """Reverses an earlier decision, deliberately.

    This used to assert the opposite: the custom field, because that is where
    ``story_points`` wrote the value. The instance's "Story Points" custom field
    turned out to be a *text* one, so it neither sorts nor sums, while
    OpenProject 17.6 has a real integer ``story_points`` column. Decided on
    2026-09-01 to move both the component and the history there; all 81 values in
    this Jira are whole numbers, so the integer column loses nothing.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("Story Points", "5", from_string="3")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["story_points"] == [3, 5]
    assert "cf_state_snapshot" not in _real(ops)[0]


def test_fractional_story_points_are_not_silently_truncated(
    component: WorkPackageMigration,
) -> None:
    """The column is an integer. None of this Jira's values are fractional, but
    rounding one away without saying so would be the wrong default."""
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("Story Points", "2.5", from_string="3")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["story_points"] == [3, None]


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

    assert _real(ops)[0]["field_changes"]["sprint_id"] == [12, 13]
    assert _real(ops)[0]["notes"] == ""
    assert "cf_state_snapshot" not in _real(ops)[0]


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


# --------------------------------------------------------------------------
# C3 — Attachment becomes a real "File added" change
#
# OpenProject renders attachment changes from ``attachable_journals``, diffing a
# journal's rows against its predecessor's. That makes the snapshot an absolute
# set on every journal, not a delta — the opposite of the custom field rows.
# --------------------------------------------------------------------------

# {jira_key: {filename: op_attachment_id}} — the shape of attachment_mapping.json
ATTACHMENTS = {JIRA_KEY: {"informe.pdf": 501, "captura.png": 502, "extra.txt": 503}}


def _attachment_item(*, added: str = "", removed: str = "") -> dict[str, object]:
    """An Attachment changelog item: ``to*`` on an addition, ``from*`` on a removal."""
    return {
        "field": "Attachment",
        "fieldId": "",
        "from": None,
        "fromString": removed,
        "to": None,
        "toString": added,
    }


def test_attachment_addition_becomes_a_snapshot_not_a_note(
    component: WorkPackageMigration,
) -> None:
    component.attachment_mapping = {JIRA_KEY: {"informe.pdf": 501}}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_attachment_item(added="informe.pdf")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["attachment_snapshot"] == [501]
    assert _real(ops)[0]["notes"] == ""


def test_snapshot_is_the_full_set_at_each_journal_not_the_delta(
    component: WorkPackageMigration,
) -> None:
    """A journal carrying only what changed reads as "everything else removed"."""
    component.attachment_mapping = ATTACHMENTS
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_attachment_item(added="informe.pdf")]),
        ("2026-02-04T10:00:00.000-0300", [_attachment_item(added="captura.png")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    # extra.txt never appears as an addition, so it was there at creation.
    assert _real(ops)[0]["attachment_snapshot"] == [501, 503]
    assert _real(ops)[1]["attachment_snapshot"] == [501, 502, 503]


def test_baseline_is_what_was_attached_at_creation(
    component: WorkPackageMigration,
) -> None:
    """Jira's changelog only records post-creation changes.

    Anything migrated that never shows up as an addition was attached when the
    issue was created, and belongs in the first snapshot — otherwise the first
    diff invents a "File added" that never happened.
    """
    component.attachment_mapping = ATTACHMENTS
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_item("status", "Closed")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["attachment_snapshot"] == [501, 502, 503]


def test_the_last_journal_matches_the_work_package(
    component: WorkPackageMigration,
) -> None:
    """Otherwise the next native save renders a diff that never happened."""
    component.attachment_mapping = ATTACHMENTS
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_attachment_item(added="informe.pdf")]),
        ("2026-02-04T10:00:00.000-0300", [_attachment_item(added="captura.png")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[-1]["attachment_snapshot"] == sorted(ATTACHMENTS[JIRA_KEY].values())


def test_removal_drops_the_file_from_later_snapshots(
    component: WorkPackageMigration,
) -> None:
    component.attachment_mapping = {JIRA_KEY: {"informe.pdf": 501, "captura.png": 502}}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_item("status", "Closed")]),
        ("2026-02-04T10:00:00.000-0300", [_attachment_item(removed="captura.png")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["attachment_snapshot"] == [501, 502]
    assert _real(ops)[1]["attachment_snapshot"] == [501]


def test_an_unresolved_filename_is_handled_and_never_becomes_a_note(
    component: WorkPackageMigration,
) -> None:
    """A file Jira no longer has was never migrated, so there is nothing to snapshot.

    The entry still counts as handled — otherwise it falls through to the note
    fallback and the journal goes back to being a comment.
    """
    component.attachment_mapping = {JIRA_KEY: {"informe.pdf": 501}}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_attachment_item(added="borrado-en-jira.zip")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["notes"] == ""
    assert "**Attachment**" not in _real(ops)[0]["notes"]


def test_no_mapped_attachments_emits_no_snapshot_at_all(
    component: WorkPackageMigration,
) -> None:
    """Absent means "leave the rows alone"; an empty list would mean "all removed".

    The Ruby side keys its delete-then-rewrite of v1 on the snapshot being
    non-nil, so emitting an empty one would wipe attachment history the migration
    cannot rebuild.
    """
    component.attachment_mapping = {}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_attachment_item(added="informe.pdf")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert all("attachment_snapshot" not in op for op in ops)
    assert all(op["notes"] == "" for op in ops)


def test_snapshot_is_scoped_to_the_issue(
    component: WorkPackageMigration,
) -> None:
    """Two issues can attach files with the same name."""
    component.attachment_mapping = {"OTRO-1": {"informe.pdf": 999}}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_attachment_item(added="informe.pdf")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert all("attachment_snapshot" not in op for op in ops)


# --------------------------------------------------------------------------
# N2 — the attachment rows of deleted journals were being stranded
# --------------------------------------------------------------------------


def _template(name: str) -> str:
    """Read a Ruby template from ``src/ruby``.

    Resolved from the test's own path: ``src.ruby`` holds no ``__init__.py``, so
    it is a namespace package whose ``__file__`` is ``None``.
    """
    from pathlib import Path

    return (Path(__file__).resolve().parents[2] / "src" / "ruby" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "template",
    ["create_work_package_journals_batch.rb", "create_work_package_journals.rb"],
)
def test_deleting_v2_plus_journals_also_deletes_their_attachment_rows(template: str) -> None:
    """``delete_all`` skips ``dependent: :destroy``, so this has to be explicit.

    Measured on 2026-08-26 before the line existed: 1399 of 3156
    ``attachable_journals`` rows orphaned — 44.3%, growing on every re-run of a
    component that is supposed to be idempotent.
    """
    text = _template(template)

    assert "Journal::AttachableJournal.where(journal_id: v2_plus_ids).delete_all" in text


def test_batch_template_writes_absolute_attachment_sets() -> None:
    text = _template("create_work_package_journals_batch.rb")

    assert "INSERT INTO attachable_journals (journal_id, attachment_id, filename)" in text
    # filename is NOT NULL and comes from the Attachment rows, not the payload.
    assert "Attachment.where(id: requested_attachment_ids).pluck(:id, :filename)" in text


def test_batch_template_rewrites_v1_attachment_rows_only_when_it_has_a_snapshot() -> None:
    """v1 can hold attachment rows via OpenProject's journal aggregation.

    Wiping them when the migration resolved no attachments would destroy history
    it cannot rebuild.
    """
    text = _template("create_work_package_journals_batch.rb")

    assert "if v1_journal && !v1_attachment_snapshot.nil?" in text
    assert "Journal::AttachableJournal.where(journal_id: v1_journal.id).delete_all" in text


def test_orphan_cleanup_sweeps_attachable_journals() -> None:
    """The script covered the other two side tables but not this one."""
    from scripts.cleanup_orphan_journal_data import _ORPHAN_PREDICATES

    assert "attachable_journals" in _ORPHAN_PREDICATES
    predicate = _ORPHAN_PREDICATES["attachable_journals"]
    # NOT EXISTS, not NOT IN: a NOT IN against a nullable subquery matches nothing.
    assert "NOT EXISTS" in predicate
    assert "attachable_journals.journal_id" in predicate


def test_skip_test_compares_snapshots_instead_of_testing_emptiness() -> None:
    """An absolute snapshot is non-empty on every op, so emptiness is the wrong test.

    A work package that merely *has* attachments carries the full set on every
    single op. If the skip test only looked at ``notes`` and ``field_changes``,
    the 512 Link / RemoteIssueLink / WorklogId / timespent entries this rebuild
    is meant to drop would each keep a journal showing nothing at all.

    The guard's behaviour was checked by running the extracted loop under the
    local Ruby against five op sequences; this pins its shape so the comparison
    cannot quietly revert to an emptiness check.
    """
    text = _template("create_work_package_journals_batch.rb")

    assert "cf_unchanged = resolved_cf_snapshot == prev_written_cf_snapshot" in text
    assert "attachment_unchanged = attachment_snapshot == prev_written_attachment_snapshot" in text
    assert "cf_unchanged && attachment_unchanged" in text
    # Seeded nil so the first op always counts as a change...
    assert "prev_written_cf_snapshot = nil" in text
    # ...and only advanced past an op that actually became a journal.
    assert text.index("next if is_empty && op_idx != 0") < text.index(
        "prev_written_cf_snapshot = resolved_cf_snapshot",
    )


def test_snapshots_are_resolved_before_the_skip_test() -> None:
    """The comparison needs them, so their resolution has to come first."""
    text = _template("create_work_package_journals_batch.rb")

    assert text.index("raw_attachment_snapshot = op['attachment_snapshot']") < text.index(
        "next if is_empty && op_idx != 0",
    )


# --------------------------------------------------------------------------
# C4 — assignee and reporter resolve by username, not display name
#
# Both branches read fromString/toString, which are display names, while
# user_mapping.json is keyed by Jira user key (JIRAUSER10800) plus, on this
# instance, 14 login-style keys. Display names appear in neither, so every
# assignee change came out [None, None]: the Ruby side skips a nil, and because
# the field counted as mapped it did not even leave a note. The change vanished.
# --------------------------------------------------------------------------

# The shape of user_mapping.json: primary key is the Jira user key.
USERS = {
    "JIRAUSER10800": {
        "jira_name": "melina.rosell",
        "jira_display_name": "Melina Rosell",
        "jira_email": "melina.rosell@donweb.com",
        "openproject_id": 59,
    },
    "JIRAUSER10900": {
        "jira_name": "leonardo.perez",
        "jira_display_name": "Leonardo Perez",
        "jira_email": "leonardo.perez@donweb.com",
        "openproject_id": 56,
    },
}


def _augmented(mapping: dict) -> dict:
    """Run the real index augmentation, which is what makes lookups work."""
    with patch.object(WorkPackageMigration, "__init__", lambda self, **_: None):
        helper = WorkPackageMigration()  # type: ignore[call-arg]
    helper.logger = MagicMock()
    helper.user_mapping = dict(mapping)
    helper._augment_user_mapping_indices()
    return helper.user_mapping


def _user_item(field: str, *, from_user: str = "", to_user: str = "",
               from_display: str = "", to_display: str = "") -> dict[str, object]:
    """A user changelog item: from/to carry the username, *String the display name."""
    return {
        "field": field,
        "fieldId": "",
        "from": from_user or None,
        "fromString": from_display,
        "to": to_user or None,
        "toString": to_display,
    }


def test_assignee_resolves_from_the_username(
    component: WorkPackageMigration,
) -> None:
    component.user_mapping = _augmented(USERS)
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [
                _user_item(
                    "assignee",
                    from_user="melina.rosell",
                    to_user="leonardo.perez",
                    from_display="Melina Rosell",
                    to_display="Leonardo Perez",
                ),
            ],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["assigned_to_id"] == [59, 56]


def test_assignee_resolves_from_the_jira_user_key(
    component: WorkPackageMigration,
) -> None:
    """Some instances put the user key in from/to rather than the username."""
    component.user_mapping = _augmented(USERS)
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [_user_item("assignee", from_user="JIRAUSER10800", to_user="JIRAUSER10900")],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["assigned_to_id"] == [59, 56]


def test_assignee_falls_back_to_the_display_name(
    component: WorkPackageMigration,
) -> None:
    """The old behaviour still has to work where it is the only thing on offer."""
    component.user_mapping = _augmented(USERS)
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [_user_item("assignee", from_display="Melina Rosell", to_display="Leonardo Perez")],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["assigned_to_id"] == [59, 56]


def test_display_names_alone_resolve_nothing_without_augmentation(
    component: WorkPackageMigration,
) -> None:
    """This is the bug, pinned: the raw mapping has no display-name index.

    Without ``_augment_user_mapping_indices`` the lookup fails, which is exactly
    what happened while ``wp_journal_history`` assigned the mapping directly.
    """
    component.user_mapping = dict(USERS)  # NOT augmented
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [_user_item("assignee", from_display="Melina Rosell", to_display="Leonardo Perez")],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert all("field_changes" not in op for op in ops)


def test_a_reassignment_that_maps_to_the_same_user_records_nothing(
    component: WorkPackageMigration,
) -> None:
    """Two Jira accounts consolidated into one OpenProject user."""
    component.user_mapping = _augmented(
        {
            "JIRAUSER1": {"jira_name": "vieja.cuenta", "openproject_id": 59},
            "JIRAUSER2": {"jira_name": "nueva.cuenta", "openproject_id": 59},
        },
    )
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [_user_item("assignee", from_user="vieja.cuenta", to_user="nueva.cuenta")],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert all("field_changes" not in op for op in ops)


def test_reporter_shares_the_assignee_branch(
    component: WorkPackageMigration,
) -> None:
    component.user_mapping = _augmented(USERS)
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [_user_item("reporter", from_user="melina.rosell", to_user="leonardo.perez")],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["author_id"] == [59, 56]


# --------------------------------------------------------------------------
# C5 — the journal author, and the indices that make it resolvable
# --------------------------------------------------------------------------


def test_journal_author_resolves_by_jira_user_key(
    component: WorkPackageMigration,
) -> None:
    """The changelog author payload now carries ``key``, the mapping's primary index.

    While only ``name`` was probed and only ``name`` was extracted, an author
    mapped under their user key resolved to nobody and the journal was attributed
    to the work package's author instead.
    """
    component.user_mapping = _augmented(USERS)
    entries = _changelog(("2026-02-03T17:03:16.000-0300", [_item("status", "Closed")]))
    entries[0]["author"] = {"name": None, "key": "JIRAUSER10900", "displayName": None}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = entries

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["user_id"] == 56


def test_journal_author_unresolved_stays_zero(
    component: WorkPackageMigration,
) -> None:
    """0, not a hardcoded builtin id: the Ruby side has its own fallback chain."""
    component.user_mapping = _augmented(USERS)
    entries = _changelog(("2026-02-03T17:03:16.000-0300", [_item("status", "Closed")]))
    entries[0]["author"] = {"name": "nadie.conocido"}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = entries

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["user_id"] == 0


def test_extractors_carry_every_probe_key() -> None:
    """The probe order is only useful if the payload actually carries the fields."""
    import inspect

    from src.utils.enhanced_audit_trail_migrator import EnhancedAuditTrailMigrator

    for method in (
        EnhancedAuditTrailMigrator.extract_changelog_from_issue,
        EnhancedAuditTrailMigrator.extract_comments_from_issue,
    ):
        source = inspect.getsource(method)
        for probe_key in WorkPackageMigration._JOURNAL_AUTHOR_PROBE_KEYS:
            assert f'"{probe_key}"' in source, (method.__name__, probe_key)


def test_builder_augments_the_user_indices() -> None:
    """``wp_journal_history`` assigned the mapping and never built the indices."""
    from src.application.components import wp_journal_history_migration as mod

    with open(mod.__file__, encoding="utf-8") as handle:
        text = handle.read()

    assert "builder._augment_user_mapping_indices()" in text
    assert text.index("builder.user_mapping = ") < text.index("builder._augment_user_mapping_indices()")


# --------------------------------------------------------------------------
# N4 — a clear is not the same as an unresolvable value
#
# The template skipped every nil, which is right for "we could not resolve this"
# and wrong for "Jira emptied the field". So an unassignment, a removal from a
# sprint or a deleted due date left the old value standing and rendered nothing.
# --------------------------------------------------------------------------


def test_unassigning_is_reported_as_a_clear(
    component: WorkPackageMigration,
) -> None:
    component.user_mapping = _augmented(USERS)
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [_user_item("assignee", from_user="melina.rosell", from_display="Melina Rosell")],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["assigned_to_id"] == [59, None]
    assert _real(ops)[0]["field_clears"] == ["assigned_to_id"]


def test_an_unresolvable_new_value_is_not_a_clear(
    component: WorkPackageMigration,
) -> None:
    """Jira named a new assignee we cannot map. Keeping the old value beats nulling it."""
    component.user_mapping = _augmented(USERS)
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [
                _user_item(
                    "assignee",
                    from_user="melina.rosell",
                    to_user="usuario.borrado",
                    to_display="Usuario Borrado",
                ),
            ],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["assigned_to_id"] == [59, None]
    assert "field_clears" not in _real(ops)[0]


def test_removal_from_a_sprint_is_a_clear(
    component: WorkPackageMigration,
) -> None:
    component.sprint_mapping = SPRINT_MAPPING
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("Sprint", "", from_string="Sprint v0.0.104")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["sprint_id"] == [12, None]
    assert _real(ops)[0]["field_clears"] == ["sprint_id"]


def test_a_deleted_due_date_is_a_clear(
    component: WorkPackageMigration,
) -> None:
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_item("duedate", "", from_string="2026-02-01")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["due_date"] == ["2026-02-01", ""]
    assert _real(ops)[0]["field_clears"] == ["due_date"]


def test_not_null_columns_are_never_asked_to_clear() -> None:
    """Clearing one of these would fail the insert.

    Nothing puts the value back in the normal path — ``ensure_required_fields``
    only runs for the pre-built ``state_snapshot`` branch.
    """
    not_null = {"subject", "author_id", "priority_id", "status_id", "type_id", "project_id"}

    assert not (WorkPackageMigration._CLEARABLE_JOURNAL_FIELDS & not_null)


def test_clearing_the_reporter_is_not_offered(
    component: WorkPackageMigration,
) -> None:
    """``author_id`` is NOT NULL, so an emptied Jira reporter cannot be applied."""
    component.user_mapping = _augmented(USERS)
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_user_item("reporter", from_user="melina.rosell")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert "field_clears" not in _real(ops)[0]


@pytest.mark.parametrize(
    "template",
    ["create_work_package_journals_batch.rb", "create_work_package_journals.rb"],
)
def test_template_applies_a_clear_only_when_it_was_declared(template: str) -> None:
    """Both templates make the distinction; neither skips every nil any more."""
    text = _template(template)

    assert "clears.include?(field_sym)" in text
    assert "current_state[field_sym] = nil" in text
    # The blanket skips this replaced.
    assert "next if new_value.nil?\n" not in text


# --------------------------------------------------------------------------
# C9 — Component and Fix Version were writing Jira ids into OpenProject FKs
#
# Both fell through to the generic ID branch, which put ``from``/``to`` — Jira's
# own component and version ids — straight into ``category_id`` / ``version_id``.
# Those are foreign keys into OpenProject's own tables, so the journal pointed at
# whatever row happened to share that number, or at nothing.
#
# Python cannot resolve them: the lookup is scoped to a project. category_mapping.json
# has the right shape (project id -> name -> id) but nothing reads it, and versions
# builds its map in memory and never persists it. So names travel and Ruby resolves.
# --------------------------------------------------------------------------


def _named_item(field: str, *, from_id: str = "", to_id: str = "",
                from_name: str = "", to_name: str = "") -> dict[str, object]:
    """A component/version item: from/to carry Jira's ids, *String the names."""
    return {
        "field": field,
        "fieldId": "",
        "from": from_id or None,
        "fromString": from_name,
        "to": to_id or None,
        "toString": to_name,
    }


@pytest.mark.parametrize(
    ("jira_field", "op_field"),
    [
        ("Component", "category_id"),
        ("component", "category_id"),
        ("Fix Version", "version_id"),
        ("fixVersion", "version_id"),
    ],
)
def test_component_and_fix_version_travel_as_names(
    component: WorkPackageMigration,
    jira_field: str,
    op_field: str,
) -> None:
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [_named_item(jira_field, from_id="10021", to_id="10022",
                         from_name="Backend", to_name="Frontend")],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"][op_field] == ["Backend", "Frontend"]


@pytest.mark.parametrize(
    ("jira_field", "op_field"),
    [("Component", "category_id"), ("Fix Version", "version_id")],
)
def test_jira_ids_never_reach_the_foreign_key(
    component: WorkPackageMigration,
    jira_field: str,
    op_field: str,
) -> None:
    """The regression this fixes: 10021/10022 are Jira ids, not OpenProject ids."""
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T17:03:16.000-0300",
            [_named_item(jira_field, from_id="10021", to_id="10022",
                         from_name="Backend", to_name="Frontend")],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert "10021" not in _real(ops)[0]["field_changes"][op_field]
    assert "10022" not in _real(ops)[0]["field_changes"][op_field]


def test_an_item_with_no_names_records_nothing(
    component: WorkPackageMigration,
) -> None:
    """Ids alone are unusable, so there is nothing to send."""
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_named_item("Component", from_id="10021", to_id="10022")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert all("field_changes" not in op for op in ops)
    assert all(op["notes"] == "" for op in ops)


@pytest.mark.parametrize(
    ("jira_field", "op_field"),
    [("Component", "category_id"), ("Fix Version", "version_id")],
)
def test_removing_the_component_or_version_is_a_clear(
    component: WorkPackageMigration,
    jira_field: str,
    op_field: str,
) -> None:
    """Both columns are nullable, so N4 can represent the removal."""
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T17:03:16.000-0300", [_named_item(jira_field, from_name="Backend")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"][op_field] == ["Backend", ""]
    assert _real(ops)[0]["field_clears"] == [op_field]


@pytest.mark.parametrize(
    "template",
    ["create_work_package_journals_batch.rb", "create_work_package_journals.rb"],
)
def test_templates_resolve_the_names_scoped_to_the_project(template: str) -> None:
    """A category or version name is only unique within its project."""
    text = _template(template)

    assert "resolve_scoped_name" in text
    assert "WHERE project_id = #{project_id.to_i}" in text
    assert "table = field_sym == :category_id ? 'categories' : 'versions'" in text
    # Skipped, not written: a bogus foreign key is worse than a missing change.
    assert "next if resolved.nil?" in text or "unresolved_scoped_names += 1" in text


def test_batch_template_reports_the_names_it_could_not_resolve() -> None:
    """Silence is how the three J2O custom fields went unnoticed for a whole migration."""
    text = _template("create_work_package_journals_batch.rb")

    assert "unresolved_scoped_names += 1" in text
    assert "diagnostics['unresolved_scoped_names']" in text


@pytest.mark.parametrize(
    "template",
    ["create_work_package_journals_batch.rb", "create_work_package_journals.rb"],
)
def test_multivalue_names_fall_back_to_the_last_segment(template: str) -> None:
    """A Jira issue can hold several components or fix versions; the column cannot.

    Whole string first so a name containing a comma still resolves, then the last
    segment — same "last one wins" as ``sprint_id``, and for the same reason.
    """
    text = _template(template)

    assert "text.split(',').map(&:strip).reject(&:empty?).last" in text
    # Whole-string lookup comes first.
    assert text.index("found = by_name[text.downcase]") < text.index("text.split(',')")


# --------------------------------------------------------------------------
# N3 — the creation journal
#
# The Ruby template writes whatever operation comes first into the existing v1
# row. That used to be the issue's first comment or changelog entry, so v1 — the
# journal that represents creation — carried an event that happened *after* it:
# its notes, its author, and the state left behind by its changes.
#
# Three consequences, all fixed by giving v1 an operation of its own: the first
# change to every field was invisible (v1 already showed the post-change value),
# v1 was attributed to whoever touched the issue first rather than to its
# creator, and version 2 was never written, leaving a gap in the chain.
# --------------------------------------------------------------------------

STATUS_MAPPING = {
    "1": {"openproject_id": 7},   # Open
    "3": {"openproject_id": 8},   # In Progress
    "6": {"openproject_id": 9},   # Closed
}


def _status_item(from_id: str, to_id: str, from_name: str, to_name: str) -> dict[str, object]:
    return {
        "field": "status",
        "fieldId": "",
        "from": from_id,
        "fromString": from_name,
        "to": to_id,
        "toString": to_name,
    }


def _two_transitions(component: WorkPackageMigration) -> list[dict]:
    """Open -> In Progress -> Closed, on a work package now sitting in Closed."""
    component.status_mapping = STATUS_MAPPING
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_status_item("1", "3", "Open", "In Progress")]),
        ("2026-02-04T10:00:00.000-0300", [_status_item("3", "6", "In Progress", "Closed")]),
    )
    return component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})


def test_the_first_operation_is_the_creation_journal(
    component: WorkPackageMigration,
) -> None:
    ops = _two_transitions(component)

    assert ops[0]["version"] == 1
    assert ops[0]["notes"] == ""


def test_creation_journal_holds_the_state_jira_created_the_issue_with(
    component: WorkPackageMigration,
) -> None:
    """Reconstructed from the ``from`` of the first change to each field.

    The replay starts from the work package as it is *now* and only moves
    forward, so without this the value a field was created with exists nowhere.
    """
    ops = _two_transitions(component)

    assert ops[0]["field_changes"]["status_id"] == [None, 7]


def test_both_transitions_are_visible(
    component: WorkPackageMigration,
) -> None:
    """The point of the whole change: Open -> In Progress -> Closed is two
    changes, and the activity used to show one."""
    ops = _two_transitions(component)

    # Replay the way the template does: start from the work package's current
    # state (Closed) and apply each operation in turn.
    rendered = []
    state = 9
    for op in ops:
        new = (op.get("field_changes") or {}).get("status_id")
        if new and new[1] is not None:
            state = new[1]
        rendered.append(state)

    assert rendered == [7, 8, 9]


def test_versions_are_contiguous_with_no_gap_at_two(
    component: WorkPackageMigration,
) -> None:
    """Folding the first entry into v1 meant version 2 was never written."""
    ops = _two_transitions(component)

    assert [op["version"] for op in ops] == [1, 2, 3]


def test_creation_journal_defers_to_the_work_package_author(
    component: WorkPackageMigration,
) -> None:
    """``user_id: 0`` sends the template to its fallback chain, which starts at
    ``rec.author_id`` — the issue's creator.

    v1 used to be attributed to whoever made the first change, who is often
    somebody else entirely.
    """
    ops = _two_transitions(component)

    assert ops[0]["user_id"] == 0


def test_a_first_comment_no_longer_lands_on_the_creation_journal(
    component: WorkPackageMigration,
) -> None:
    """A comment is not the creation of the issue."""
    component.enhanced_audit_trail_migrator.extract_comments_from_issue.return_value = [
        {
            "id": "10001",
            "created": "2026-02-03T10:00:00.000-0300",
            "author": {"name": "melina.rosell"},
            "body": "primer comentario",
        },
    ]
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = []

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops[0]["version"] == 1
    assert ops[0]["notes"] == ""
    assert "primer comentario" in ops[1]["notes"]
    assert ops[1]["version"] == 2


def test_untouched_fields_stay_out_of_the_creation_state(
    component: WorkPackageMigration,
) -> None:
    """A field the changelog never mentions keeps the work package's own value.

    Guessing at it would be worse than leaving the template's base state alone.
    """
    ops = _two_transitions(component)

    assert set(ops[0]["field_changes"]) == {"status_id"}


def test_only_the_first_change_defines_the_creation_value(
    component: WorkPackageMigration,
) -> None:
    """A later change's ``from`` is not the creation value."""
    ops = _two_transitions(component)

    # 7 is the "from" of the first transition; 8 is the "from" of the second.
    assert ops[0]["field_changes"]["status_id"][1] == 7


def test_a_field_created_empty_is_declared_as_a_clear(
    component: WorkPackageMigration,
) -> None:
    """Added to a sprint later means it had none at creation.

    Without the declaration the template skips the nil and the creation journal
    inherits the work package's current sprint, so the first assignment renders
    as nothing.
    """
    component.sprint_mapping = SPRINT_MAPPING
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_item("Sprint", "Sprint v0.0.104")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops[0]["field_changes"]["sprint_id"] == [None, None]
    assert ops[0]["field_clears"] == ["sprint_id"]


def test_a_not_null_column_created_empty_is_never_declared_as_a_clear(
    component: WorkPackageMigration,
) -> None:
    """``status_id`` cannot be NULL, so an unresolvable creation value has to
    leave the template's base state standing."""
    component.status_mapping = {"3": {"openproject_id": 8}}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_status_item("", "3", "", "In Progress")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert "field_clears" not in ops[0]


def test_creation_journal_carries_the_custom_field_baseline(
    component: WorkPackageMigration,
) -> None:
    """So the first custom field change reads "X to Y" and not "set to Y"."""
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_item("resolution", "Fixed", from_string="Unresolved")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops[0]["cf_state_snapshot"] == {"Resolution": "Unresolved"}
    assert ops[1]["cf_state_snapshot"] == {"Resolution": "Fixed"}


def test_creation_journal_carries_the_attachments_present_at_creation(
    component: WorkPackageMigration,
) -> None:
    """The ones uploaded later must not already be on v1, or their upload
    renders as nothing."""
    component.attachment_mapping = ATTACHMENTS
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_attachment_item(added="informe.pdf")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    # captura.png and extra.txt never appear as additions, so they came with the issue.
    assert ops[0]["attachment_snapshot"] == [502, 503]
    assert ops[1]["attachment_snapshot"] == [501, 502, 503]


def test_an_issue_with_no_history_gets_no_operations_at_all(
    component: WorkPackageMigration,
) -> None:
    """Those work packages are handled by ``_reattribute_lone_creation_journals``.

    Emitting a lone creation operation would make the template rewrite v1 for
    every one of them to no purpose.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = []

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops == []


def test_the_template_numbers_the_journals_it_actually_keeps() -> None:
    """Python numbers one operation per Jira entry, but the skip test drops the
    ones that contribute nothing, and every drop left a hole in the chain.

    Measured after the rebuild on 2026-08-28: 158 work packages whose journal
    count did not match their highest version. Only Ruby knows which operations
    survived, so only Ruby can number them.
    """
    text = _template("create_work_package_journals_batch.rb")

    assert "version = base_version + bulk_journals.size + 1" in text
    # The payload's own number must not be read back.
    assert "pre_computed_version" not in text


# --------------------------------------------------------------------------
# Etapa 10 — los seis campos que seguian llegando como comentarios
#
# Medido en la instancia el 2026-09-01, acotado a los work packages migrados:
# Workflow 58, timeestimate 33, Key 27, project 27, issuetype 18, y Epic Link 0
# porque ya estaba suprimido. 99 journals distintos en total: mover un issue de
# proyecto emite Key y project en la misma entrada.
# --------------------------------------------------------------------------

TIPOS_POR_ID = {"10004": 7, "10005": 4}
TIPOS_POR_NOMBRE = {"Bug": {"openproject_id": 7}, "Task": {"openproject_id": 4}}
PROYECTOS = {"ES": {"openproject_id": 42}, "ESUX": {"openproject_id": 47}}


def test_issuetype_resolves_by_id_where_it_used_to_fall_through_to_a_note(
    component: WorkPackageMigration,
) -> None:
    """The mapping the code consulted is keyed by name; the changelog carries ids.

    ``issue_type_mapping`` holds 'Bug'/'Epic'/… while ``to``/``from`` are
    "10004"/"10005", so the lookup never matched, ``field_mapped`` stayed false,
    and the entry became a comment — one whose text showed the right names,
    proving the data was there and only the lookup was wrong. Same shape as the
    ``assignee`` defect.
    """
    component.issue_type_id_mapping = TIPOS_POR_ID
    component.issue_type_mapping = TIPOS_POR_NOMBRE
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T10:00:00.000-0300",
            [{"field": "issuetype", "fieldId": "", "from": "10005", "fromString": "Task",
              "to": "10004", "toString": "Bug"}],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["type_id"] == [4, 7]
    assert _real(ops)[0]["notes"] == ""


def test_issuetype_falls_back_to_the_name_mapping(
    component: WorkPackageMigration,
) -> None:
    """Ids first, names second — a fixture or instance may only have one."""
    component.issue_type_id_mapping = {}
    component.issue_type_mapping = TIPOS_POR_NOMBRE
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T10:00:00.000-0300",
            [{"field": "issuetype", "fieldId": "", "from": "10005", "fromString": "Task",
              "to": "10004", "toString": "Bug"}],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["type_id"] == [4, 7]


def test_an_unresolvable_issue_type_leaves_the_previous_one(
    component: WorkPackageMigration,
) -> None:
    """``type_id`` is NOT NULL, so it must never be cleared."""
    component.issue_type_id_mapping = {"10005": 4}
    component.issue_type_mapping = {}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T10:00:00.000-0300",
            [{"field": "issuetype", "fieldId": "", "from": "10005", "fromString": "Task",
              "to": "99999", "toString": "Desconocido"}],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert "field_changes" not in _real(ops)[0]
    assert "field_clears" not in _real(ops)[0]


def test_a_project_move_becomes_a_project_id_change(
    component: WorkPackageMigration,
) -> None:
    """``from``/``to`` carry Jira project ids; ``project_mapping`` is keyed by key."""
    component.project_mapping = PROYECTOS
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T10:00:00.000-0300",
            [{"field": "project", "fieldId": "", "from": "10000", "fromString": "ES",
              "to": "10100", "toString": "ESUX"}],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["project_id"] == [42, 47]
    assert _real(ops)[0]["notes"] == ""


def test_an_epic_link_becomes_a_parent_change(
    component: WorkPackageMigration,
) -> None:
    """Jira models the epic as a link, OpenProject as the parent.

    This one was not a comment before — it was in the ignore list and vanished
    entirely, which is why the probe counted 0 for it.
    """
    component.work_package_mapping = {"10126": {"jira_key": "EF-38", "openproject_id": 1583}}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_item("Epic Link", "EF-38")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["parent_id"] == [None, 1583]
    assert _real(ops)[0]["notes"] == ""


def test_time_estimates_are_converted_from_seconds_to_hours(
    component: WorkPackageMigration,
) -> None:
    """Jira reports seconds and OpenProject stores hours.

    Passing the raw value through the string branch would have written 7200
    *hours* into the column.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_item("timeestimate", "7200", from_string="3600")]),
        ("2026-02-04T10:00:00.000-0300", [_item("timeoriginalestimate", "5400")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["field_changes"]["remaining_hours"] == [1.0, 2.0]
    assert _real(ops)[1]["field_changes"]["estimated_hours"] == [None, 1.5]


def test_an_issue_rename_lands_in_the_provenance_field(
    component: WorkPackageMigration,
) -> None:
    """OpenProject has no key of its own; the Jira one already has a home."""
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_item("Key", "ESUX-85", from_string="ES-85")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert _real(ops)[0]["cf_state_snapshot"] == {"J2O Origin Key": "ESUX-85"}
    assert _real(ops)[0]["notes"] == ""


def test_workflow_is_suppressed_by_decision(
    component: WorkPackageMigration,
) -> None:
    """Jira's workflow *scheme* is an administration object with no OpenProject
    equivalent, and it was the single largest source of noise — 58 of 99
    journals. Suppressed by decision on 2026-09-01 rather than given a custom
    field of its own.
    """
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_item("Workflow", "Scrum", from_string="Kanban")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert all(op["notes"] == "" for op in ops)
    assert all("field_changes" not in op for op in ops)


@pytest.mark.parametrize(
    "jira_field",
    ["Workflow", "Key", "issuetype", "project", "timeestimate", "Epic Link"],
)
def test_none_of_the_six_produces_a_comment_with_the_mappings_wired(
    component: WorkPackageMigration,
    jira_field: str,
) -> None:
    """The headline of the stage, under the conditions the pipeline actually runs in."""
    component.issue_type_id_mapping = TIPOS_POR_ID
    component.issue_type_mapping = TIPOS_POR_NOMBRE
    component.project_mapping = PROYECTOS
    component.work_package_mapping = {"10126": {"jira_key": "EF-38", "openproject_id": 1583}}
    valores = {
        "issuetype": ("10004", "Bug", "10005", "Task"),
        "project": ("10100", "ESUX", "10000", "ES"),
        "Epic Link": ("", "EF-38", "", ""),
        "timeestimate": ("7200", "7200", "3600", "3600"),
    }.get(jira_field, ("", "algo", "", "otra cosa"))
    to_val, to_str, from_val, from_str = valores
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        (
            "2026-02-03T10:00:00.000-0300",
            [{"field": jira_field, "fieldId": "", "from": from_val or None,
              "fromString": from_str, "to": to_val or None, "toString": to_str}],
        ),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert all(op["notes"] == "" for op in ops), f"{jira_field} sigue generando un comentario"


@pytest.mark.parametrize("jira_field", ["issuetype", "project"])
def test_an_unresolvable_type_or_project_degrades_to_a_note_on_purpose(
    component: WorkPackageMigration,
    jira_field: str,
) -> None:
    """These two are the only ones that can still produce a comment, and only
    when their mapping cannot resolve the value.

    Both columns are NOT NULL, so an unresolvable value cannot be written and
    cannot be cleared either — the choice is a note or silence. A note is kept
    deliberately: silent loss is the failure mode this project has been bitten by
    four times over, and here the note still carries the names ("Task → Bug").
    With the mappings wired, which is how the pipeline runs, this never fires —
    the test above covers that case.
    """
    component.issue_type_id_mapping = {}
    component.issue_type_mapping = {}
    component.project_mapping = {}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_item(jira_field, "Bug", from_string="Task")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert f"**{jira_field}**: Task → Bug" in _real(ops)[0]["notes"]
