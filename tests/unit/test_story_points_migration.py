import pytest

from src.application.components.story_points_migration import STORY_POINTS_CF_NAME, StoryPointsMigration


class DummyFields:
    def __init__(self, sp=None, cf=None):
        self.storyPoints = sp
        self.customfield_10016 = cf


class DummyIssue:
    def __init__(self, key: str, sp=None, cf=None):
        self.key = key
        self.fields = DummyFields(sp=sp, cf=cf)


class DummyJira:
    def __init__(self) -> None:
        self.issues = {
            "PRJ-1": DummyIssue("PRJ-1", sp=3),
            "PRJ-2": DummyIssue("PRJ-2", sp=None, cf=5.5),
            "PRJ-3": DummyIssue("PRJ-3", sp=None, cf=None),
        }

    def batch_get_issues(self, keys):
        return {k: self.issues.get(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.updates: list[dict] = []

    def batch_update_work_packages(self, updates):
        self.updates.extend(updates)
        return {"updated": len(updates), "failed": 0}

    def get_custom_field_by_name(self, name: str):
        assert name == STORY_POINTS_CF_NAME
        raise Exception("not found")

    def execute_query(self, script: str):
        self.queries.append(script)
        if "cf.id" in script:
            return 801
        return True

    def ensure_wp_custom_field_id(self, name: str, field_format: str = "text") -> int:
        return 801

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
                    "PRJ-1": {"openproject_id": 11001},
                    "PRJ-2": {"openproject_id": 11002},
                    "PRJ-3": {"openproject_id": 11003},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_story_points_migration_writes_the_native_column():
    """Reverses the original design, deliberately.

    This used to write a WorkPackage custom field, and asserted only the count.
    The custom field on this instance is a *text* one, so it neither sorts nor
    sums; OpenProject 17.6 has a real integer ``story_points`` column and that is
    where the value goes as of 2026-09-01.
    """
    op = DummyOp()
    mig = StoryPointsMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    ld = mig._load(mig._map(mig._extract()))

    assert ld.success is True
    # PRJ-1 has 3; PRJ-2's 5.5 is fractional and the column is an integer.
    assert op.updates == [{"id": 11001, "story_points": 3}]
    assert ld.updated == 1


def test_fractional_story_points_are_reported_not_truncated():
    """Rounding a value away silently would be the wrong default.

    None of this Jira's 81 values are fractional, but the column is an integer
    and the component should say so rather than quietly store 5 for 5.5.
    """
    op = DummyOp()
    mig = StoryPointsMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    ld = mig._load(mig._map(mig._extract()))

    assert all(u["id"] != 11002 for u in op.updates)
    assert ld.failed == 1


def test_the_tenants_real_custom_field_is_tried_first(monkeypatch: pytest.MonkeyPatch):
    """The whole point of the fix: this Jira numbers the field customfield_10106,
    which none of the guessed ids nor the attribute-name scan can find."""
    import src.config as cfg

    class Fields:
        customfield_10106 = 8

    class Issue:
        key = "PRJ-9"
        fields = Fields()

    class Jira:
        def batch_get_issues(self, keys):
            return {"PRJ-9": Issue()}

    class Mappings:
        def get_mapping(self, name: str):
            if name == "work_package":
                return {"PRJ-9": {"openproject_id": 11009}}
            if name == "custom_field":
                return {"customfield_10106": {"jira_name": "Story Points"}}
            return {}

    monkeypatch.setattr(cfg, "mappings", Mappings(), raising=False)
    op = DummyOp()
    mig = StoryPointsMigration(jira_client=Jira(), op_client=op)  # type: ignore[arg-type]

    mig._load(mig._map(mig._extract()))

    assert op.updates == [{"id": 11009, "story_points": 8}]
