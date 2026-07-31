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

#: Stop the load loop after this many consecutive failures.
#:
#: Every failed ``ensure_project_sprint`` costs a Rails round-trip, and a
#: failure that repeats is almost always systemic (wrong schema, wedged
#: console) rather than per-sprint. Grinding through 259 sprints to learn the
#: same thing 259 times is how a one-line mismatch turned into hours.
MAX_CONSECUTIVE_FAILURES = 5

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

            # The copy resolved through the sprint's own origin board wins;
            # the others are just boards whose filter happens to reach it.
            if sprint.get("project_from_origin_board") and not seen.get("project_from_origin_board"):
                by_id[sprint_id] = sprint
                seen, sprint = sprint, seen

            if seen.get("project_key") == sprint.get("project_key"):
                continue

            # Same sprint, two projects, and no origin board settled it —
            # genuinely ambiguous, so say so instead of picking in silence.
            config.logger.warning(
                "Jira sprint %s is reachable from boards in different projects (%s via '%s' vs %s via '%s'); "
                "keeping %s (origin board %s)",
                sprint_id,
                seen.get("project_key"),
                seen.get("board_name"),
                sprint.get("project_key"),
                sprint.get("board_name"),
                seen.get("project_key"),
                "resolved" if seen.get("project_from_origin_board") else "unresolved",
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

    def _board_project_key(self, board: dict[str, Any]) -> str | None:
        """Resolve a board's Jira project key.

        ``location`` is Cloud-only — absent on this Jira Server/DC — so fall
        back to the dedicated board/project endpoint, the same resolution
        order ``AgileBoardMigration`` uses.
        """
        location = board.get("location") or {}
        project_key = location.get("key") or location.get("projectKey") or board.get("locationProjectKey")
        if project_key:
            return str(project_key)

        board_id = board.get("id")
        try:
            board_projects = self.jira_client.get_board_projects(board_id)
        except Exception:
            return None
        if board_projects:
            key = board_projects[0].get("key")
            return str(key) if key else None
        return None

    def _fetch_sprints(self) -> list[dict[str, Any]]:
        """Fetch every sprint reachable from every Jira board.

        A sprint's project comes from its **origin board** (``originBoardId``),
        not from whichever board happened to surface it first. A board's
        sprint listing includes any sprint its filter reaches, so a sprint can
        be reported by boards belonging to different projects — five sprints
        here are visible from both an ES board and an EF board, and those are
        two different OpenProject projects. Taking the first board would file
        them under whichever one the iteration happened to hit.
        """
        try:
            boards = self.jira_client.get_boards()
        except Exception as exc:
            self.logger.exception("Failed to fetch Jira boards: %s", exc)
            return []

        project_key_by_board: dict[int, str | None] = {}
        raw: list[dict[str, Any]] = []

        for board in boards:
            board_id = board.get("id")
            if board_id is None:
                continue

            project_key_by_board[int(board_id)] = self._board_project_key(board)

            try:
                board_sprints = self.jira_client.get_board_sprints(board_id)
            except Exception:
                board_sprints = []

            for sprint in board_sprints:
                raw.append(
                    {
                        "board_id": int(board_id),
                        "board_name": board.get("name"),
                        "origin_board_id": sprint.get("originBoardId"),
                        "id": sprint.get("id"),
                        "name": sprint.get("name"),
                        "goal": sprint.get("goal"),
                        "state": sprint.get("state"),
                        "startDate": sprint.get("startDate"),
                        "endDate": sprint.get("endDate"),
                    },
                )

        for entry in raw:
            origin_id = entry.get("origin_board_id")
            origin_key = project_key_by_board.get(int(origin_id)) if origin_id is not None else None
            if origin_key:
                entry["project_key"] = origin_key
                entry["project_from_origin_board"] = True
            else:
                # Either Jira did not report an origin board, or it points at
                # a board outside this instance's visible set. Fall back to
                # the reporting board and let ``_dedupe_sprints`` flag any
                # cross-project disagreement.
                entry["project_key"] = project_key_by_board.get(entry["board_id"])
                entry["project_from_origin_board"] = False

        return raw

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

        # Say out loud which instance and which schema this is about to write
        # to. Two runs failed against an instance whose ``sprints`` table had
        # no ``finish_date`` while the logs said nothing about either the
        # version or the columns, so diagnosing it meant digging through
        # archived tmux captures for a Rails backtrace.
        self.logger.info(
            "OpenProject %s | native sprints: %s | sprint columns: %s | sprint_goals: %s | work_packages.sprint_id: %s",
            support.get("op_version") or "unknown",
            support.get("supported"),
            ", ".join(support.get("columns") or []) or "none",
            support.get("goals"),
            support.get("wp_fk"),
        )

        if not support.get("supported"):
            # Not an error: a pre-17.3 target legitimately has no Sprint
            # model, and ``agile_boards`` still creates the Versions.
            self.logger.warning(
                "This OpenProject instance has no native Sprint model; leaving sprints to the Version path",
            )
            return ComponentResult(
                success=True,
                message="Native sprints unsupported on this instance; Version path retained",
                details={
                    "strategy": strategy,
                    "native_supported": False,
                    "op_version": support.get("op_version"),
                },
            )

        # Check the schema once, before touching 259 sprints. The Sprint model
        # is not stable across OpenProject releases — 17.4.0 has the model but
        # no ``finish_date`` — and finding that out one row at a time costs a
        # Rails round-trip each. The observed column list goes into the error
        # so the message itself is the schema report for that release.
        missing = list(support.get("missing_required") or [])
        if missing:
            message = (
                f"OpenProject {support.get('op_version') or 'unknown'} has a Sprint model but is missing "
                f"required column(s): {', '.join(missing)}. Columns present: "
                f"{', '.join(support.get('columns') or []) or 'none'}. "
                f"Set J2O_SPRINT_STRATEGY=version to migrate sprints as Versions on this instance."
            )
            self.logger.error(message)
            return ComponentResult(
                success=False,
                message="Native sprint schema is incompatible with this OpenProject release",
                error=message,
                details={
                    "strategy": strategy,
                    "native_supported": True,
                    "op_version": support.get("op_version"),
                    "missing_required": missing,
                    "columns": support.get("columns"),
                },
            )

        sprints: list[dict[str, Any]] = mapped.data.get("sprints", [])
        created = 0
        existing = 0
        errors = 0
        goals_written = 0
        goals_skipped = 0
        blocked: list[dict[str, Any]] = []
        mapping_updates: dict[str, Any] = {}
        consecutive_failures = 0
        aborted_after: int | None = None
        dropped_columns: set[str] = set()

        for index, payload in enumerate(sprints):
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
                consecutive_failures += 1
                self.logger.exception("Failed to create sprint %s: %s", payload.get("name"), exc)
                result = {"success": False, "error": str(exc)}
            else:
                if result.get("success"):
                    consecutive_failures = 0
                else:
                    errors += 1
                    consecutive_failures += 1
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

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Repeating failures are systemic, not per-sprint. Stop rather
                # than pay a Rails round-trip apiece to confirm it 250 more
                # times.
                aborted_after = index + 1
                self.logger.error(
                    "Stopping after %s consecutive failures at sprint %s/%s; last error: %s",
                    consecutive_failures,
                    aborted_after,
                    len(sprints),
                    result.get("error"),
                )
                break

            if not result.get("success"):
                continue

            for column in result.get("dropped_columns") or []:
                dropped_columns.add(str(column))

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

        if dropped_columns:
            self.logger.warning(
                "Column(s) not present on this instance's Sprint model, left unset: %s",
                ", ".join(sorted(dropped_columns)),
            )

        message = "Native sprints migrated"
        if aborted_after is not None:
            message = (
                f"Native sprint migration stopped after {MAX_CONSECUTIVE_FAILURES} consecutive failures "
                f"({aborted_after} of {len(sprints)} sprints attempted)"
            )

        return ComponentResult(
            success=errors == 0,
            message=message,
            success_count=created,
            failed_count=errors,
            details={
                "strategy": strategy,
                "native_supported": True,
                "op_version": support.get("op_version"),
                "sprints_created": created,
                "sprints_existing": existing,
                "sprints_attempted": aborted_after if aborted_after is not None else len(sprints),
                "sprints_total": len(sprints),
                "aborted_after_consecutive_failures": aborted_after,
                "goals_written": goals_written,
                "goals_skipped": goals_skipped,
                "dropped_columns": sorted(dropped_columns),
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
