"""Migrate Jira sprints into OpenProject's native Sprint objects.

Since OpenProject 17.3 a sprint is a first-class record rather than a
Backlogs-era alias for ``Version``, so this component replaces the
sprint half of :class:`~src.application.components.agile_board_migration.AgileBoardMigration`
(which keeps the board → saved-query half). The legacy Version path is
still reachable through ``J2O_SPRINT_STRATEGY`` for pre-17.3 targets.

This component only *creates* sprints. Attaching them to work packages
belongs to :class:`~src.application.components.sprint_epic_migration.SprintEpicMigration`,
which runs after the work packages exist — the two were fused before,
and because the registry sequenced them ahead of
``work_packages_skeleton`` the assignment half silently no-opped on
every cold run.

Two Jira/OpenProject impedance mismatches are resolved here rather than
at the Rails boundary, so that what happened is visible in the run
summary instead of surfacing as row-level validation errors:

* **The same sprint arrives more than once.** ``GET /board/{id}/sprint``
  answers for every board whose filter reaches a sprint, so a sprint
  shared by two boards is reported twice. The Jira sprint id is its
  identity; the board is not part of it. See ``_dedupe_sprints``.
* **Jira allows several active sprints where OpenProject allows one per
  project.** See ``_resolve_single_active``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.infrastructure.openproject.openproject_sprint_service import (
    STATUS_ACTIVE,
    STATUS_IN_PLANNING,
    map_jira_state,
    to_date,
)
from src.models import ComponentResult

#: ``native`` uses OpenProject's Sprint model; ``version`` keeps the legacy
#: sprint-as-Version behaviour; ``both`` writes each sprint twice (useful
#: while comparing the two representations side by side).
SPRINT_STRATEGY_NATIVE = "native"
SPRINT_STRATEGY_VERSION = "version"
SPRINT_STRATEGY_BOTH = "both"


def sprint_strategy() -> str:
    """Return the configured sprint strategy, defaulting to ``native``."""
    raw = str(config.migration_config.get("sprint_strategy", SPRINT_STRATEGY_NATIVE) or "").lower()
    if raw in (SPRINT_STRATEGY_NATIVE, SPRINT_STRATEGY_VERSION, SPRINT_STRATEGY_BOTH):
        return raw
    return SPRINT_STRATEGY_NATIVE


@register_entity_types("native_sprints")
class SprintMigration(BaseMigration):
    """Create OpenProject native sprints from Jira sprints."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.project_mapping = config.mappings.get_mapping("project") or {}
        self.sprint_mapping = config.mappings.get_mapping("sprint") or {}

    # ------------------------------------------------------------------ #
    # BaseMigration overrides                                            #
    # ------------------------------------------------------------------ #

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Opt out of change detection.

        Sprints are discovered per board and deduplicated across boards,
        so the generic ``ChangeDetector`` — which keys entities by
        ``id``/``key``/``name`` off a flat pre-fetch — cannot track them
        across runs without re-implementing the fetch. Raising follows the
        project's convention for components that always re-apply
        (``ResolutionMigration``, ``AffectsVersionsMigration``);
        ``ensure_project_sprint`` is idempotent, so re-running is cheap and
        safe.

        Args:
            entity_type: Type of entities

        Raises:
            ValueError: Always, as this migration does not support change detection

        """
        msg = f"{type(self).__name__} does not support change detection for entity type: {entity_type}"
        raise ValueError(msg)

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _dedupe_sprints(sprints: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Collapse the same Jira sprint arriving from several boards.

        ``GET /board/{id}/sprint`` returns a sprint for every board whose
        filter reaches it, so a sprint shared by two boards — on this
        instance 'Desarrollo' and 'Copia de Desarrollo' both report the
        active ``Sprint v0.0.262`` — arrives twice.

        Without this, ``_resolve_single_active`` sees a phantom conflict
        and demotes a sprint against itself; and since both copies resolve
        to the same ``(project_id, name)`` row, which status survives would
        come down to write order.
        """
        by_id: dict[str, dict[str, Any]] = {}
        duplicates = 0

        for sprint in sprints:
            sprint_id = str(sprint.get("id") or "")
            if not sprint_id:
                continue

            seen = by_id.get(sprint_id)
            if seen is None:
                by_id[sprint_id] = sprint
                continue

            duplicates += 1
            # First board wins. The same sprint reachable from boards in
            # *different* projects would be genuinely ambiguous, so say so
            # rather than pick in silence. Not observed on this instance.
            if seen.get("project_key") != sprint.get("project_key"):
                config.logger.warning(
                    "Jira sprint %s is reachable from boards in different projects (%s vs %s); keeping %s",
                    sprint_id,
                    seen.get("project_key"),
                    sprint.get("project_key"),
                    seen.get("project_key"),
                )

        return list(by_id.values()), duplicates

    @staticmethod
    def _resolve_single_active(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Enforce OpenProject's one-active-sprint-per-project rule.

        ``Sprint`` validates uniqueness of an active status scoped to
        ``project_id`` (``only_one_active_sprint_allowed``). Jira has no
        such rule, and boards collapse into far fewer OpenProject projects
        than they occupy in Jira, so two boards' active sprints can land in
        one project.

        Keeps the most recently started active sprint per project and
        demotes the rest to ``in_planning``, returning what was demoted so
        the choice lands in the run summary instead of being buried.

        Run this *after* ``_dedupe_sprints`` — otherwise one sprint seen
        from two boards looks like a conflict with itself.
        """
        by_project: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for payload in payloads:
            if payload.get("status") == STATUS_ACTIVE:
                by_project[int(payload["project_id"])].append(payload)

        demoted: list[dict[str, Any]] = []
        for project_id, actives in by_project.items():
            if len(actives) < 2:
                continue
            # Latest start wins; the Jira sprint id breaks ties so the
            # outcome does not depend on board iteration order.
            actives.sort(
                key=lambda p: (str(p.get("start_date") or ""), int(p.get("jira_sprint_id") or 0)),
            )
            for loser in actives[:-1]:
                loser["status"] = STATUS_IN_PLANNING
                demoted.append(
                    {
                        "project_id": project_id,
                        "name": loser.get("name"),
                        "jira_sprint_id": loser.get("jira_sprint_id"),
                    },
                )
        return demoted

    def _fetch_sprints(self) -> list[dict[str, Any]]:
        """Fetch every sprint reachable from every Jira board."""
        try:
            boards = self.jira_client.get_boards()
        except Exception as exc:
            self.logger.exception("Failed to fetch Jira boards: %s", exc)
            return []

        sprint_payloads: list[dict[str, Any]] = []

        for board in boards:
            board_id = board.get("id")
            if board_id is None:
                continue

            # ``location`` is Cloud-only — absent on this Jira Server/DC —
            # so fall back to the dedicated board/project endpoint, the
            # same resolution order ``AgileBoardMigration`` uses.
            location = board.get("location") or {}
            project_key = location.get("key") or location.get("projectKey") or board.get("locationProjectKey")
            if not project_key:
                try:
                    board_projects = self.jira_client.get_board_projects(board_id)
                except Exception:
                    board_projects = []
                if board_projects:
                    project_key = board_projects[0].get("key")

            try:
                board_sprints = self.jira_client.get_board_sprints(board_id)
            except Exception:
                board_sprints = []

            for sprint in board_sprints:
                sprint_payloads.append(
                    {
                        "board_id": board_id,
                        "board_name": board.get("name"),
                        "project_key": project_key,
                        "id": sprint.get("id"),
                        "name": sprint.get("name"),
                        "goal": sprint.get("goal"),
                        "state": sprint.get("state"),
                        "startDate": sprint.get("startDate"),
                        "endDate": sprint.get("endDate"),
                    },
                )

        return sprint_payloads

    # ------------------------------------------------------------------ #
    # ETL                                                                #
    # ------------------------------------------------------------------ #

    def _extract(self) -> ComponentResult:
        """Fetch sprints from Jira and collapse cross-board duplicates."""
        try:
            raw = self._fetch_sprints()
        except Exception as exc:
            return ComponentResult(
                success=False,
                message=f"Failed to fetch Jira sprints: {exc}",
                error=str(exc),
            )

        sprints, duplicates = self._dedupe_sprints(raw)
        if duplicates:
            self.logger.info(
                "Collapsed %s duplicate sprint entries reported by more than one board (%s unique sprints)",
                duplicates,
                len(sprints),
            )

        return ComponentResult(
            success=True,
            data={"sprints": sprints},
            total_count=len(sprints),
            details={"duplicates_collapsed": duplicates},
        )

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Translate Jira sprints into native-sprint payloads."""
        if not extracted.success or not isinstance(extracted.data, dict):
            return ComponentResult(
                success=False,
                message="Sprint extraction failed",
                error=extracted.message or "extract phase returned no data",
            )

        sprints: list[dict[str, Any]] = extracted.data.get("sprints", [])
        payloads: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for sprint in sprints:
            project_key = sprint.get("project_key")
            project_entry = self.project_mapping.get(project_key) if project_key else None
            op_project_id = int(project_entry.get("openproject_id", 0)) if isinstance(project_entry, dict) else 0

            if op_project_id <= 0:
                skipped.append(
                    {
                        "reason": "missing_project_mapping",
                        "sprint_id": sprint.get("id"),
                        "sprint_name": sprint.get("name"),
                        "board_name": sprint.get("board_name"),
                        "project_key": project_key,
                    },
                )
                continue

            payloads.append(
                {
                    "project_id": op_project_id,
                    "jira_sprint_id": sprint.get("id"),
                    "name": sprint.get("name") or f"Sprint {sprint.get('id')}",
                    "goal": sprint.get("goal"),
                    "start_date": to_date(sprint.get("startDate")),
                    "finish_date": to_date(sprint.get("endDate")),
                    "status": map_jira_state(sprint.get("state")),
                },
            )

        demoted = self._resolve_single_active(payloads)
        for entry in demoted:
            self.logger.warning(
                "Project %s received more than one active Jira sprint; '%s' demoted to %s",
                entry["project_id"],
                entry["name"],
                STATUS_IN_PLANNING,
            )

        if skipped:
            self.logger.warning(
                "%s sprint(s) skipped: their board has no resolvable Jira project",
                len(skipped),
            )

        return ComponentResult(
            success=True,
            data={"sprints": payloads, "skipped": skipped, "demoted_active": demoted},
            total_count=len(payloads),
            details={
                "sprints": len(payloads),
                "skipped": len(skipped),
                "demoted_active": len(demoted),
            },
        )

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Create the sprints in OpenProject and persist the sprint mapping."""
        if not mapped.success or not isinstance(mapped.data, dict):
            return ComponentResult(
                success=False,
                message="Sprint mapping failed",
                error=mapped.message or "map phase returned no data",
            )

        strategy = sprint_strategy()
        if strategy == SPRINT_STRATEGY_VERSION:
            return ComponentResult(
                success=True,
                message=f"Native sprint creation skipped (J2O_SPRINT_STRATEGY={strategy})",
                details={"strategy": strategy, "skipped_by_strategy": True},
            )

        support = self.op_client.detect_native_sprint_support()
        if not support.get("supported"):
            # Not an error: a pre-17.3 target legitimately has no Sprint
            # model, and ``agile_boards`` still creates the Versions.
            self.logger.warning(
                "This OpenProject instance has no native Sprint model; leaving sprints to the Version path",
            )
            return ComponentResult(
                success=True,
                message="Native sprints unsupported on this instance; Version path retained",
                details={"strategy": strategy, "native_supported": False},
            )

        sprints: list[dict[str, Any]] = mapped.data.get("sprints", [])
        created = 0
        existing = 0
        errors = 0
        goals_written = 0
        goals_skipped = 0
        blocked: list[dict[str, Any]] = []
        mapping_updates: dict[str, Any] = {}

        for payload in sprints:
            jira_sprint_id = payload.get("jira_sprint_id")
            try:
                result = self.op_client.ensure_project_sprint(
                    payload["project_id"],
                    name=payload["name"],
                    start_date=payload.get("start_date"),
                    finish_date=payload.get("finish_date"),
                    status=payload.get("status"),
                    goal=payload.get("goal"),
                )
            except Exception as exc:
                errors += 1
                self.logger.exception("Failed to create sprint %s: %s", payload.get("name"), exc)
                continue

            if not result.get("success"):
                errors += 1
                if result.get("blocking_active"):
                    blocked.append(
                        {
                            "sprint": payload.get("name"),
                            "project_id": payload["project_id"],
                            "blocking_active": result.get("blocking_active"),
                        },
                    )
                self.logger.error(
                    "Sprint '%s' rejected by OpenProject: %s",
                    payload.get("name"),
                    result.get("error"),
                )
                continue

            if result.get("created"):
                created += 1
            else:
                existing += 1
            if result.get("goal_id"):
                goals_written += 1
            if result.get("goal_skipped"):
                goals_skipped += 1

            if jira_sprint_id:
                # Keep the existing entry (it may carry the legacy Version
                # id that ``SprintEpicMigration`` falls back to) and add the
                # native id alongside, rather than replacing it.
                existing_entry = self.sprint_mapping.get(str(jira_sprint_id))
                entry = dict(existing_entry) if isinstance(existing_entry, dict) else {}
                entry.update(
                    {
                        "openproject_sprint_id": result.get("id"),
                        "project_id": payload["project_id"],
                        "name": payload.get("name"),
                    },
                )
                mapping_updates[str(jira_sprint_id)] = entry
                sprint_name = payload.get("name")
                if sprint_name:
                    mapping_updates[sprint_name] = entry

        if mapping_updates:
            updated_mapping = dict(self.sprint_mapping)
            updated_mapping.update(mapping_updates)
            config.mappings.set_mapping("sprint", updated_mapping)
            self.sprint_mapping = updated_mapping

        if goals_skipped:
            self.logger.warning(
                "%s sprint goal(s) not written: this instance has no SprintGoal model",
                goals_skipped,
            )

        return ComponentResult(
            success=errors == 0,
            message="Native sprints migrated",
            success_count=created,
            failed_count=errors,
            details={
                "strategy": strategy,
                "native_supported": True,
                "sprints_created": created,
                "sprints_existing": existing,
                "goals_written": goals_written,
                "goals_skipped": goals_skipped,
                "errors": errors,
                "blocked_by_active_conflict": blocked,
                "skipped": len(mapped.data.get("skipped", [])),
                "demoted_active": len(mapped.data.get("demoted_active", [])),
            },
        )

    def run(self) -> ComponentResult:
        """Execute the native sprint migration pipeline."""
        self.logger.info("Starting native sprint migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error(
                "Sprint extraction failed: %s",
                extracted.message or extracted.error,
            )
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error(
                "Sprint mapping failed: %s",
                mapped.message or mapped.error,
            )
            return mapped

        result = self._load(mapped)
        if result.success:
            self.logger.info(
                "Native sprint migration complete (created=%s, existing=%s, goals=%s, skipped=%s)",
                result.details.get("sprints_created", 0),
                result.details.get("sprints_existing", 0),
                result.details.get("goals_written", 0),
                result.details.get("skipped", 0),
            )
        else:
            self.logger.error(
                "Native sprint migration encountered %s error(s)",
                result.details.get("errors", 0),
            )
        return result
