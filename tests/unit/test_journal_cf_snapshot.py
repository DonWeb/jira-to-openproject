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

    assert ops[0]["attachment_snapshot"] == [501]
    assert ops[0]["notes"] == ""


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
    assert ops[0]["attachment_snapshot"] == [501, 503]
    assert ops[1]["attachment_snapshot"] == [501, 502, 503]


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

    assert ops[0]["attachment_snapshot"] == [501, 502, 503]


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

    assert ops[-1]["attachment_snapshot"] == sorted(ATTACHMENTS[JIRA_KEY].values())


def test_removal_drops_the_file_from_later_snapshots(
    component: WorkPackageMigration,
) -> None:
    component.attachment_mapping = {JIRA_KEY: {"informe.pdf": 501, "captura.png": 502}}
    component.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = _changelog(
        ("2026-02-03T10:00:00.000-0300", [_item("status", "Closed")]),
        ("2026-02-04T10:00:00.000-0300", [_attachment_item(removed="captura.png")]),
    )

    ops = component._build_rails_ops_for_issue(_issue(), {"id": 1552, "jira_key": JIRA_KEY})

    assert ops[0]["attachment_snapshot"] == [501, 502]
    assert ops[1]["attachment_snapshot"] == [501]


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

    assert ops[0]["notes"] == ""
    assert "**Attachment**" not in ops[0]["notes"]


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
