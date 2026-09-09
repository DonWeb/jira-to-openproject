"""Unit tests for WorkflowMigration.

An OpenProject ``Workflow`` row is what lets a user move a work package,
so every case here is about the difference between a migration that
writes usable rows and one that reports success having written none.
The numbers come from the live pair: a Jira Server 9.12.2 whose 435
issues yield 134 observed transitions, and an OpenProject 17.6.0 whose
member role is called "Member".
"""

from __future__ import annotations

import pytest

from src.application.components.workflow_migration import WorkflowMigration

# The live instance's roles, including the ones that must never receive
# workflow rows.
ROLES = [
    {"id": 11, "name": "Project admin", "type": "ProjectRole", "edit_work_packages": True},
    {"id": 9, "name": "Member", "type": "ProjectRole", "edit_work_packages": True},
    {"id": 1, "name": "Work package editor", "type": "WorkPackageRole", "edit_work_packages": True},
    {"id": 10, "name": "Reader", "type": "ProjectRole", "edit_work_packages": False},
    {"id": 7, "name": "Non member", "type": "ProjectRole", "edit_work_packages": False},
    {"id": 8, "name": "Anonymous", "type": "ProjectRole", "edit_work_packages": False},
    {"id": 6, "name": "Standard global role", "type": "GlobalRole", "edit_work_packages": False},
]


class DummyJira:
    def __init__(self, observed=None, issue_types=None, schemes=None) -> None:
        self._observed = observed if observed is not None else {}
        self._issue_types = issue_types or [{"id": "10004", "name": "Improvement"}]
        self._schemes = schemes or []
        self.observed_calls: list[list[str]] = []

    def get_issue_types(self):
        return self._issue_types

    def get_workflow_schemes(self):
        return self._schemes

    def get_workflow_transitions(self, workflow_name):
        # The Cloud-only endpoint behind this returns nothing on Server/DC.
        return []

    def get_workflow_statuses(self, workflow_name):
        return []

    def get_observed_transitions(self, project_keys, *, page_size=100):
        self.observed_calls.append(list(project_keys))
        return self._observed


class DummyOp:
    def __init__(self, *, roles=None, summary=None) -> None:
        self._roles = ROLES if roles is None else roles
        self._summary = summary or {"created": 0, "existing": 0, "errors": 0}
        self.synced: list[tuple[list[dict], list[int]]] = []

    def get_roles(self):
        return self._roles

    def sync_workflow_transitions(self, transitions, role_ids):
        self.synced.append((transitions, role_ids))
        return {**self._summary, "created": len(transitions) * len(role_ids)}


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"ES": {"openproject_id": 42}, "CE": {"openproject_id": 47}},
                "issue_type": {
                    "Improvement": {"openproject_id": 4, "openproject_name": "Feature"},
                    "New Feature": {"openproject_id": 4, "openproject_name": "Feature"},
                    "Bug": {"openproject_id": 7, "openproject_name": "Bug"},
                },
                "status": {
                    "10003": {"openproject_id": 18, "openproject_name": "To Do", "jira_name": "To Do"},
                    "10210": {
                        "openproject_id": 26,
                        "openproject_name": "Development in progress",
                        "jira_name": "Development in progress",
                    },
                    "10211": {"openproject_id": 19, "openproject_name": "Done", "jira_name": "Done"},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):
            self._m[name] = value

    dummy = DummyMappings()
    monkeypatch.setattr(cfg, "mappings", dummy, raising=False)
    return dummy


@pytest.fixture(autouse=True)
def _no_configured_roles(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    monkeypatch.setitem(cfg.migration_config, "workflow_roles", [])


def _run(jira, op):
    mig = WorkflowMigration(jira_client=jira, op_client=op)
    return mig, mig._map(mig._extract())


# --------------------------------------------------------------------- #
# the silent no-op                                                      #
# --------------------------------------------------------------------- #


def test_a_migration_that_maps_nothing_reports_failure(_mock_mappings) -> None:
    """"0 planned, 0 skipped, success" is what hid the defect for every run.

    The transition source was a Jira Cloud endpoint that 404s on
    Server/DC, so every workflow came back empty and the component stayed
    green while the target instance had no workflow for any migrated
    status — 26 statuses holding 286 of its 289 work packages.
    """
    _, mapped = _run(DummyJira(observed={}), DummyOp())

    assert mapped.success is False
    assert mapped.details["transitions_observed"] == 0
    assert mapped.details["transitions_planned"] == 0
    assert "Nothing was migrated" in mapped.message


def test_observed_but_unmappable_transitions_also_fail(_mock_mappings) -> None:
    """Reading transitions and mapping none is a mapping bug, not an empty Jira."""
    jira = DummyJira(observed={"Improvement": [{"from": "99999", "to": "99998", "count": 3}]})
    _, mapped = _run(jira, DummyOp())

    assert mapped.success is False
    assert mapped.details["transitions_observed"] == 1
    assert mapped.details["transitions_planned"] == 0
    assert mapped.details["unresolved_jira_statuses"] == ["99998", "99999"]


def test_load_refuses_an_empty_transition_set(_mock_mappings) -> None:
    """The same green-zero trap lived in the load phase too."""
    from src.models import ComponentResult

    mig = WorkflowMigration(jira_client=DummyJira(), op_client=DummyOp())
    result = mig._load(ComponentResult(success=True, data={"transitions": [], "role_ids": [9]}))

    assert result.success is False
    assert result.details["created"] == 0


# --------------------------------------------------------------------- #
# transitions from the changelog                                        #
# --------------------------------------------------------------------- #


def test_observed_transitions_become_workflow_rows(_mock_mappings) -> None:
    """The reported case: Feature, To Do → Development in progress.

    Seen 45 times in the live changelog; without it, work package 1453's
    status dropdown offered nothing but "To Do".
    """
    jira = DummyJira(
        observed={"Improvement": [{"from": "10003", "to": "10210", "count": 45}]},
    )
    op = DummyOp()
    _, mapped = _run(jira, op)

    assert mapped.success is True
    assert mapped.data["transitions"] == [
        {
            "type_id": 4,
            "from_status_id": 18,
            "to_status_id": 26,
            "jira_issue_type": "Improvement",
            "jira_workflow": None,
            "observed_count": 45,
        },
    ]


def test_two_jira_types_on_one_openproject_type_make_one_row(_mock_mappings) -> None:
    """'Improvement' and 'New Feature' both map to OpenProject's Feature.

    One workflow row, and the observed counts add up rather than the
    second silently replacing the first.
    """
    jira = DummyJira(
        observed={
            "Improvement": [{"from": "10003", "to": "10210", "count": 45}],
            "New Feature": [{"from": "10003", "to": "10210", "count": 3}],
        },
    )
    _, mapped = _run(jira, DummyOp())

    assert len(mapped.data["transitions"]) == 1
    assert mapped.data["transitions"][0]["observed_count"] == 48


def test_a_transition_that_collapses_onto_one_status_is_dropped(_mock_mappings) -> None:
    """Two Jira statuses can map to one OpenProject status.

    OpenProject has no row shape for a self-transition, and it would not
    mean anything if it did.
    """
    jira = DummyJira(
        observed={"Improvement": [{"from": "10003", "to": "10003", "count": 2}]},
    )
    _, mapped = _run(jira, DummyOp())

    assert mapped.success is False  # nothing else was mappable
    assert mapped.details["collapsed_self_transitions"] == 1


def _changelog_service(issues, total=None):
    """A JiraWorkflowService whose search endpoint returns *issues*."""
    from unittest.mock import MagicMock

    from src.infrastructure.jira.jira_workflow_service import JiraWorkflowService

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(
        return_value={"issues": issues, "total": len(issues) if total is None else total},
    )
    client = MagicMock()
    client.base_url = "https://jira.example"
    client.jira._session.get = MagicMock(return_value=response)
    service = JiraWorkflowService(client)
    service._logger = MagicMock()
    return service, client


def test_a_creation_entry_is_not_a_transition() -> None:
    """A changelog item with no ``from`` records where an issue started.

    Jira writes one on creation. Treating it as a transition would invent
    a move out of a status nothing was ever in.
    """
    issues = [
        {
            "fields": {"issuetype": {"name": "Improvement"}},
            "changelog": {
                "histories": [
                    {"items": [{"field": "status", "from": None, "to": "10003"}]},
                    {"items": [{"field": "status", "from": "10003", "to": "10210"}]},
                ],
            },
        },
    ]
    service, _ = _changelog_service(issues)

    assert service.get_observed_transitions(["ES"]) == {
        "Improvement": [{"from": "10003", "to": "10210", "count": 1}],
    }


def test_non_status_changelog_items_are_ignored() -> None:
    """A changelog carries every field change, not just status."""
    issues = [
        {
            "fields": {"issuetype": {"name": "Improvement"}},
            "changelog": {
                "histories": [
                    {"items": [{"field": "assignee", "from": "a", "to": "b"}]},
                    {"items": [{"field": "status", "from": "10003", "to": "10210"}]},
                ],
            },
        },
    ]
    service, _ = _changelog_service(issues)

    observed = service.get_observed_transitions(["ES"])
    assert observed == {"Improvement": [{"from": "10003", "to": "10210", "count": 1}]}


def test_the_same_move_across_issues_is_counted_once_with_a_tally() -> None:
    """The count is for the run summary, not for deciding anything."""
    issues = [
        {
            "fields": {"issuetype": {"name": "Improvement"}},
            "changelog": {"histories": [{"items": [{"field": "status", "from": "10003", "to": "10210"}]}]},
        }
        for _ in range(3)
    ]
    service, _ = _changelog_service(issues)

    assert service.get_observed_transitions(["ES"]) == {
        "Improvement": [{"from": "10003", "to": "10210", "count": 3}],
    }


def test_no_project_keys_reads_nothing() -> None:
    """An empty project mapping must not turn into an unscoped instance-wide scan."""
    service, client = _changelog_service([])

    assert service.get_observed_transitions([]) == {}
    assert client.jira._session.get.call_count == 0


def test_only_migrated_projects_are_scanned(_mock_mappings) -> None:
    """Transitions from projects nobody migrated would add unreachable rows."""
    jira = DummyJira(observed={"Improvement": [{"from": "10003", "to": "10210", "count": 1}]})
    _run(jira, DummyOp())

    assert jira.observed_calls == [["ES", "CE"]]


# --------------------------------------------------------------------- #
# role selection                                                        #
# --------------------------------------------------------------------- #


def test_roles_are_chosen_by_permission_not_by_name(_mock_mappings) -> None:
    """Selecting by name cost every member their transitions.

    The default was ``["Project admin", "Project member"]``; OpenProject's
    builtin member role is called "Member", so the match produced
    ``[Project admin]`` alone. The three roles that hold
    ``edit_work_packages`` are exactly the ones OpenProject's own seeder
    writes workflows for.
    """
    jira = DummyJira(observed={"Improvement": [{"from": "10003", "to": "10210", "count": 1}]})
    _, mapped = _run(jira, DummyOp())

    assert mapped.data["role_ids"] == [1, 9, 11]


def test_roles_that_cannot_edit_work_packages_are_excluded(_mock_mappings) -> None:
    """The old "if nothing matched, use every role" fallback swept these in."""
    jira = DummyJira(observed={"Improvement": [{"from": "10003", "to": "10210", "count": 1}]})
    _, mapped = _run(jira, DummyOp())

    excluded = {role["id"] for role in ROLES if not role["edit_work_packages"]}
    assert excluded.isdisjoint(mapped.data["role_ids"])


def test_an_explicit_role_list_still_wins(_mock_mappings, monkeypatch) -> None:
    """J2O_WORKFLOW_ROLES narrows the set for an instance that wants that."""
    import src.config as cfg

    monkeypatch.setitem(cfg.migration_config, "workflow_roles", ["Member"])
    jira = DummyJira(observed={"Improvement": [{"from": "10003", "to": "10210", "count": 1}]})
    _, mapped = _run(jira, DummyOp())

    assert mapped.data["role_ids"] == [9]


def test_a_configured_role_that_does_not_exist_falls_back_to_permission(
    _mock_mappings,
    monkeypatch,
) -> None:
    """A stale config name must not silently produce a migration with no roles."""
    import src.config as cfg

    monkeypatch.setitem(cfg.migration_config, "workflow_roles", ["Project member"])
    jira = DummyJira(observed={"Improvement": [{"from": "10003", "to": "10210", "count": 1}]})
    _, mapped = _run(jira, DummyOp())

    assert mapped.data["role_ids"] == [1, 9, 11]


def test_no_eligible_role_is_a_failure(_mock_mappings) -> None:
    """Rows nobody can use are worse than an honest failure."""
    roles = [{"id": 10, "name": "Reader", "type": "ProjectRole", "edit_work_packages": False}]
    jira = DummyJira(observed={"Improvement": [{"from": "10003", "to": "10210", "count": 1}]})
    _, mapped = _run(jira, DummyOp(roles=roles))

    assert mapped.success is False
    assert mapped.data["role_ids"] == []


# --------------------------------------------------------------------- #
# load                                                                  #
# --------------------------------------------------------------------- #


def test_every_eligible_role_gets_the_transition(_mock_mappings) -> None:
    """A workflow row is per role; one row for one role helps only that role."""
    jira = DummyJira(observed={"Improvement": [{"from": "10003", "to": "10210", "count": 45}]})
    op = DummyOp()
    mig = WorkflowMigration(jira_client=jira, op_client=op)
    result = mig.run()

    assert result.success is True
    transitions, role_ids = op.synced[0]
    assert len(transitions) == 1
    assert role_ids == [1, 9, 11]
    assert result.details["created"] == 3
