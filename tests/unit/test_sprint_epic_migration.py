import pytest

from src.application.components.sprint_epic_migration import SPRINT_CF_NAME, SprintEpicMigration


class DummyFields:
    def __init__(self, epic=None, sprint=None):
        self.customfield_10008 = epic  # Epic Link common
        self.customfield_10020 = sprint  # Sprint common


class DummyIssue:
    def __init__(self, key: str, epic=None, sprint=None):
        self.key = key
        self.fields = DummyFields(epic=epic, sprint=sprint)


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "EPIC-1": DummyIssue("EPIC-1", epic=None, sprint=[{"name": "Sprint A"}]),
            "PRJ-1": DummyIssue("PRJ-1", epic="EPIC-1", sprint=[{"name": "Sprint A"}, {"name": "Sprint B"}]),
            "PRJ-2": DummyIssue("PRJ-2", epic=None, sprint=None),
        }

    def batch_get_issues(self, keys):
        return {k: self.issues.get(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.queries: list[str] = []

    def batch_update_work_packages(self, updates):
        self.updates.extend(updates)
        return {"updated": len(updates), "failed": 0}

    def get_custom_field_by_name(self, name: str):
        assert name == SPRINT_CF_NAME
        raise Exception("not found")

    def execute_query(self, script: str):
        self.queries.append(script)
        if "cf.id" in script:
            return 901
        return True

    def execute_query_to_json_file(self, script: str):
        """Same behavior as execute_query but returns the result directly."""
        return self.execute_query(script)

    def ensure_wp_custom_field_id(self, name: str, field_format: str = "text") -> int:
        return 901

    def enable_custom_field_for_projects(
        self,
        cf_id: int,
        project_ids: set[int],
        cf_name: str | None = None,
    ) -> None:
        return None


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "EPIC-1": {"openproject_id": 12000},
                    "PRJ-1": {"openproject_id": 12001},
                    "PRJ-2": {"openproject_id": 12002},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_sprint_epic_migration_sets_parent_and_sprint_cf():
    mig = SprintEpicMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    # Expect: 1 parent link (PRJ-1 -> EPIC-1) + 2 sprint CF updates (EPIC-1, PRJ-1)
    # batch_update_work_packages updated=1, CF updates add 2 more -> updated==3
    assert ld.updated == 3


def _sprint_mapping_fixture(monkeypatch: pytest.MonkeyPatch, sprint_mapping: dict) -> None:
    """Re-point ``cfg.mappings`` with a sprint mapping alongside the WP one."""
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "EPIC-1": {"openproject_id": 12000},
                    "PRJ-1": {"openproject_id": 12001},
                    "PRJ-2": {"openproject_id": 12002},
                },
                "sprint": sprint_mapping,
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_native_sprint_id_is_preferred_over_the_legacy_version_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both ids are mapped, the work package gets sprint_id, not version_id.

    Both are scalar foreign keys on the work package, so only one can win;
    on OpenProject 17.3+ the native sprint is the real object and the
    Version is the legacy stand-in.
    """
    _sprint_mapping_fixture(
        monkeypatch,
        {"Sprint A": {"openproject_id": 800, "openproject_sprint_id": 901, "project_id": 11}},
    )
    op = DummyOp()
    mig = SprintEpicMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]

    result = mig._load(mig._map(mig._extract()))

    sprint_assignments = [u for u in op.updates if "sprint_id" in u]
    assert [u["sprint_id"] for u in sprint_assignments] == [901, 901]
    assert not [u for u in op.updates if "version_id" in u]
    assert result.details["sprint_assignments"] == 2
    assert result.details["version_assignments"] == 0


def test_version_id_is_used_when_no_native_sprint_is_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``openproject_sprint_id`` the legacy Version path still applies.

    This is the pre-17.3 target, and the reason writing the native id must
    not clobber the Version id in the sprint mapping.
    """
    _sprint_mapping_fixture(
        monkeypatch,
        {"Sprint A": {"openproject_id": 800, "project_id": 11}},
    )
    op = DummyOp()
    mig = SprintEpicMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]

    result = mig._load(mig._map(mig._extract()))

    assert [u["version_id"] for u in op.updates if "version_id" in u] == [800, 800]
    assert not [u for u in op.updates if "sprint_id" in u]
    assert result.details["sprint_assignments"] == 0
    assert result.details["version_assignments"] == 2


def test_coerce_sprint_names_extracts_name_from_greenhopper_tostring():
    """Classic Jira Server/DC sometimes serialises the Sprint field as a
    GreenHopper Java object's toString() instead of clean JSON — confirmed
    live via var/debug tmux captures, where the OpenProject custom field
    ended up literally set to this string. The real name lives in the
    embedded ``name=...`` segment.
    """
    raw = (
        "com.atlassian.greenhopper.service.sprint.Sprint@f4203e6[id=84,rapidViewId=4,"
        "state=CLOSED,name=Sprint v0.0.104,startDate=2020-04-07T08:37:18.068-03:00,"
        "endDate=2020-04-28T08:37:00.000-03:00,completeDate=2020-05-04T09:17:30.494-03:00,"
        "activatedDate=2020-04-07T08:37:18.068-03:00,sequence=84,goal=,synced=false,"
        "autoStartStop=false,incompleteIssuesDestinationId=<null>]"
    )
    assert SprintEpicMigration._coerce_sprint_names(raw) == ["Sprint v0.0.104"]
    assert SprintEpicMigration._coerce_sprint_names([raw]) == ["Sprint v0.0.104"]


def test_coerce_sprint_names_passes_through_clean_string_unchanged():
    assert SprintEpicMigration._coerce_sprint_names("Sprint 5") == ["Sprint 5"]


def test_the_last_sprint_wins_not_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jira lists every sprint an issue passed through, in join order.

    So the first is the oldest and the last is the one it ended in, which is what
    ``work_packages.sprint_id`` has to say. Taking the first assigned each issue
    to the sprint it *started* in.

    It stayed invisible until the journal chain began carrying ``sprint_id``:
    ``_build_rails_ops_for_issue._resolve_sprint_id`` walks the same list from the
    other end, and measured against the live instance on 2026-08-28 the two
    disagreed on 25 work packages — 23 of them off by exactly one sprint. Each
    would render a "Sprint changed" that never happened on the next native save.
    """
    _sprint_mapping_fixture(
        monkeypatch,
        {
            "Sprint A": {"openproject_id": 800, "openproject_sprint_id": 901, "project_id": 11},
            "Sprint B": {"openproject_id": 801, "openproject_sprint_id": 902, "project_id": 11},
        },
    )
    op = DummyOp()
    mig = SprintEpicMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]

    mig._load(mig._map(mig._extract()))

    by_wp = {u["id"]: u["sprint_id"] for u in op.updates if "sprint_id" in u}
    # EPIC-1 only ever sat in Sprint A.
    assert by_wp[12000] == 901
    # PRJ-1 went A -> B, so it belongs to B.
    assert by_wp[12001] == 902


def test_falls_back_to_an_earlier_sprint_when_the_last_is_unmapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sprint whose board never resolved to a project is not in the mapping.

    Walking backwards has to keep going rather than give up, or the issue loses
    its sprint entirely.
    """
    _sprint_mapping_fixture(
        monkeypatch,
        {"Sprint A": {"openproject_id": 800, "openproject_sprint_id": 901, "project_id": 11}},
    )
    op = DummyOp()
    mig = SprintEpicMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]

    mig._load(mig._map(mig._extract()))

    by_wp = {u["id"]: u["sprint_id"] for u in op.updates if "sprint_id" in u}
    assert by_wp[12001] == 901


def test_both_ends_of_the_pipeline_pick_the_same_sprint() -> None:
    """``sprint_epic`` and the journal rebuild must agree, or the newest journal
    disagrees with the work package it describes.

    They read the same comma-separated list from opposite code paths, so the
    direction has to match in both.
    """
    import inspect

    from src.application.components.sprint_epic_migration import SprintEpicMigration as SEM
    from src.application.components.work_package_migration import WorkPackageMigration

    assert "for candidate in reversed(sprint_names)" in inspect.getsource(SEM._load)
    assert "for candidate in reversed(candidates)" in inspect.getsource(
        WorkPackageMigration._resolve_sprint_id,
    )


class _OrderTrapJira:
    """ESUX-85's real sprint history: 74 -> 75 -> 105, across 2019-2020.

    The names are the trap. Alphabetically "Sprint v0.0.105" sorts *before*
    "Sprint v0.0.75" because "1" < "7", so any sort here turns the newest sprint
    into the oldest-looking one.
    """

    def __init__(self) -> None:
        self.issues = {
            "ESUX-85": DummyIssue(
                "ESUX-85",
                epic=None,
                sprint=[
                    {"name": "Sprint v0.0.74"},
                    {"name": "Sprint v0.0.75"},
                    {"name": "Sprint v0.0.105"},
                ],
            ),
        }

    def batch_get_issues(self, keys):
        return {k: self.issues.get(k) for k in keys}


def _order_trap_mappings(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {"ESUX-85": {"openproject_id": 1714}},
                "sprint": {
                    "Sprint v0.0.74": {"openproject_sprint_id": 15, "project_id": 42},
                    "Sprint v0.0.75": {"openproject_sprint_id": 16, "project_id": 42},
                    "Sprint v0.0.105": {"openproject_sprint_id": 40, "project_id": 42},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_sprint_names_keep_join_order_instead_of_being_sorted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sorting destroyed the only information the list carries.

    Jira returns an issue's sprints in the order it joined them, so the order
    *is* the chronology. ``sorted(set(...))`` replaced it with an alphabetical
    one that disagrees whenever the numeric part changes digit count.
    """
    _order_trap_mappings(monkeypatch)
    mig = SprintEpicMigration(jira_client=_OrderTrapJira(), op_client=DummyOp())  # type: ignore[arg-type]

    mapped = mig._map(mig._extract())

    assert mapped.data["sprint_text"]["ESUX-85"] == "Sprint v0.0.74, Sprint v0.0.75, Sprint v0.0.105"


def test_the_issue_lands_in_the_sprint_it_actually_finished_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ESUX-85 ended in Sprint v0.0.105 (2020), not v0.0.75 (2019).

    Measured on the live instance: with the names sorted, this work package was
    the last one whose newest journal still disagreed with the work package row.
    """
    _order_trap_mappings(monkeypatch)
    op = DummyOp()
    mig = SprintEpicMigration(jira_client=_OrderTrapJira(), op_client=op)  # type: ignore[arg-type]

    mig._load(mig._map(mig._extract()))

    assert [u["sprint_id"] for u in op.updates if "sprint_id" in u] == [40]


def test_duplicate_sprint_names_are_still_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """De-duplication was the point of the set; only the sorting had to go."""
    _order_trap_mappings(monkeypatch)
    mig = SprintEpicMigration(jira_client=_OrderTrapJira(), op_client=DummyOp())  # type: ignore[arg-type]
    mig.jira_client.issues["ESUX-85"].fields.customfield_10020 = [  # type: ignore[attr-defined]
        {"name": "Sprint v0.0.74"},
        {"name": "Sprint v0.0.75"},
        {"name": "Sprint v0.0.74"},
    ]

    mapped = mig._map(mig._extract())

    assert mapped.data["sprint_text"]["ESUX-85"] == "Sprint v0.0.74, Sprint v0.0.75"
