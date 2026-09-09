"""Workflow migration: aligns Jira workflows with OpenProject transitions.

An OpenProject ``Workflow`` row is ``(type, old status, new status,
role)``, and it is the only thing that lets a user move a work package:
with no row for a status, its dropdown offers nothing but the status it
is already in. Getting these wrong is therefore not a partial migration,
it is an unusable instance.

Where the transitions come from
-------------------------------
**Jira Server/DC does not expose a workflow's transition graph over
REST** — see ``JiraWorkflowService.get_observed_transitions`` for the
endpoints tried and what each returns. The graph is recovered instead
from the **issue changelogs**: every status change ever made is recorded
there, so the transitions an issue type permits are readable from what
its issues actually did.

That makes this a *sample*, and the limit is worth stating: a transition
Jira allows but nobody ever used leaves no trace and cannot be
recovered. Those gaps are reported (``unresolved_jira_statuses``,
``skipped``) rather than filled by inventing transitions — an invented
one is indistinguishable from a real one afterwards, and it would grant
moves the source system forbade.

Two defects this replaced, both of which reported success
---------------------------------------------------------
* The transition source was ``/rest/api/2/workflow/search``, a Jira
  **Cloud** endpoint that 404s on Server/DC. Every workflow came back
  with zero transitions, so nothing was mapped, nothing was skipped, and
  "0 planned, 0 skipped" read exactly like "this Jira has no
  transitions". The component stayed green across every run while the
  target instance had no workflow for any migrated status — 26 statuses
  holding 286 of its 289 work packages.
* Roles were selected **by name**, defaulting to ``["Project admin",
  "Project member"]``. OpenProject's builtin member role is called
  "Member", so ordinary members would have got no transitions even once
  the source was fixed. Selection is now by the ``edit_work_packages``
  permission — the property that actually decides whether a workflow row
  for a role means anything, and what OpenProject's own seeder keys on.

Both are now failures rather than green zeroes: a run that maps no
transition returns ``success=False`` and says so.

Phase 7d note
-------------
This migration is intentionally left unchanged in the typed-pipeline
sweep. It does not consume the ``work_package`` mapping (so there is no
``wp_map`` polymorphic ladder to retire here), and the structures it
*does* touch — ``status_mapping`` and ``issue_type_mapping`` — use their
own dict-of-dict shapes that are unrelated to ADR-002 phase 3/7. Phase 7
targets the ``wp_map`` ladder specifically; retyping the workflow status
maps is deferred.
"""

from __future__ import annotations

from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult


@register_entity_types("workflows")
class WorkflowMigration(BaseMigration):
    """Synchronise Jira workflow transitions with OpenProject workflow records."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    # ------------------------------------------------------------------ #
    # BaseMigration overrides                                            #
    # ------------------------------------------------------------------ #

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for change detection.

        WorkflowMigration aggregates issue types, workflow schemes,
        transitions, statuses, and OpenProject roles into a single wrapper
        payload (see ``_fetch_workflow_metadata``) rather than a list of
        independently identifiable entities, so the generic ``ChangeDetector``
        (which keys entities by ``id``/``key``/``name``) cannot track this
        data across runs — every run would otherwise see 0 current entities
        and skip the real migration permanently. This migration is therefore
        transformation-only from the change-detector's point of view; it
        always re-fetches and re-applies workflow transitions, and
        ``sync_workflow_transitions`` in ``_load`` makes that idempotent.

        Args:
            entity_type: Type of entities

        Raises:
            ValueError: Always, as this migration does not support change detection

        """
        msg = f"{type(self).__name__} does not support change detection for entity type: {entity_type}"
        raise ValueError(msg)

    def _fetch_workflow_metadata(self) -> list[dict[str, Any]]:
        """Fetch issue types, workflow schemes, transitions, statuses, and OP roles.

        Returns:
            List containing aggregated workflow metadata (schemes, transitions, statuses, roles)

        """
        # Fetch issue types (API call 1)
        try:
            issue_types = self.jira_client.get_issue_types()
        except Exception as exc:
            self.logger.exception("Failed to extract issue types: %s", exc)
            return []

        # Fetch workflow schemes (API call 2)
        try:
            schemes = self.jira_client.get_workflow_schemes()
        except Exception as exc:
            self.logger.exception("Failed to extract workflow schemes: %s", exc)
            return []

        # Fetch OpenProject roles (API call 3)
        try:
            roles = self.op_client.get_roles()
        except Exception as exc:
            self.logger.exception("Failed to extract OpenProject roles: %s", exc)
            roles = []

        issue_type_by_id = {
            str(item.get("id")): item.get("name") for item in issue_types if item.get("id") and item.get("name")
        }

        issue_type_to_workflow: dict[str, str] = {}
        workflow_names: set[str] = set()
        for scheme in schemes:
            mappings = scheme.get("issueTypeMappings") or scheme.get("mappings") or {}
            if isinstance(mappings, dict):
                for issue_type_id, workflow_name in mappings.items():
                    jira_name = issue_type_by_id.get(str(issue_type_id))
                    if jira_name and isinstance(workflow_name, str) and workflow_name:
                        issue_type_to_workflow[jira_name] = workflow_name
                        workflow_names.add(workflow_name)
            default_workflow = scheme.get("defaultWorkflow")
            if isinstance(default_workflow, str) and default_workflow and not issue_type_to_workflow:
                # Fallback: apply default workflow to every known issue type if no explicit mappings
                for name in issue_type_by_id.values():
                    if name not in issue_type_to_workflow:
                        issue_type_to_workflow[name] = default_workflow
                        workflow_names.add(default_workflow)

        if schemes and not issue_type_to_workflow:
            # Every scheme's ``issueTypeMappings``/``defaultWorkflow`` came up
            # empty or unusable, so no workflow to synchronise was found even
            # though schemes exist. Log the raw shape of each scheme so the
            # next real run shows exactly what this Jira instance returns
            # (e.g. ``issueTypeMappings`` as a list instead of a dict, or a
            # differently-named field) instead of guessing at a fix blind.
            self.logger.warning(
                "0 issue-type-to-workflow mappings resolved from %d workflow scheme(s); raw scheme shapes: %s",
                len(schemes),
                [
                    {
                        "keys": sorted(s.keys()) if isinstance(s, dict) else type(s).__name__,
                        "issueTypeMappings_type": type(s.get("issueTypeMappings")).__name__
                        if isinstance(s, dict)
                        else None,
                        "issueTypeMappings": s.get("issueTypeMappings") if isinstance(s, dict) else None,
                        "defaultWorkflow": s.get("defaultWorkflow") if isinstance(s, dict) else None,
                    }
                    for s in schemes
                ],
            )

        # Fetch transitions and statuses for each workflow (API calls 4 & 5 per workflow)
        workflow_transitions: dict[str, list[dict[str, Any]]] = {}
        workflow_statuses: dict[str, list[dict[str, Any]]] = {}
        for workflow_name in workflow_names:
            try:
                transitions = self.jira_client.get_workflow_transitions(workflow_name)
            except Exception:
                transitions = []
            workflow_transitions[workflow_name] = transitions

            try:
                statuses = self.jira_client.get_workflow_statuses(workflow_name)
            except Exception:
                statuses = []
            workflow_statuses[workflow_name] = statuses if isinstance(statuses, list) else []

        # The real transition source (API call 6+, one page per 100 issues).
        #
        # ``workflow_transitions`` above is empty on every Jira Server/DC
        # target: the endpoint behind it is Cloud-only. Rather than infer a
        # workflow's graph from an API that does not expose it, read what the
        # issues actually did — see ``get_observed_transitions``.
        observed_transitions: dict[str, list[dict[str, Any]]] = {}
        try:
            observed_transitions = self.jira_client.get_observed_transitions(
                self._migrated_project_keys(),
            )
        except Exception as exc:
            self.logger.exception("Failed to observe status transitions from issue changelogs: %s", exc)

        # Return aggregated data structure
        return [
            {
                "issue_type_to_workflow": issue_type_to_workflow,
                "workflow_transitions": workflow_transitions,
                "workflow_statuses": workflow_statuses,
                "observed_transitions": observed_transitions,
                "roles": roles,
            },
        ]

    def _workflow_role_ids(self, roles: list[dict[str, Any]]) -> list[int]:
        """Return the roles a workflow row should be written for.

        A workflow row is ``(type, old status, new status, role)``: a user
        may make a transition only when a row exists for a role they hold.
        So the roles that matter are exactly the ones that may edit work
        packages — which is what OpenProject's own seeder keys on, and it
        shows: on the target instance the seeded rows cover precisely
        ``Work package editor``, ``Member`` and ``Project admin``, the three
        roles holding ``edit_work_packages``, 586 rows each.

        Selecting by **name** — which this did, with the default
        ``["Project admin", "Project member"]`` — is the trap. The builtin
        member role is called "Member"; nothing has been called "Project
        member" for several major versions, and a renamed or localised role
        would miss too. That left ``role_ids = [Project admin]``: every
        ordinary member would have been unable to move a work package even
        once transitions existed. ``J2O_WORKFLOW_ROLES`` still overrides by
        name for an instance that wants a narrower set.

        The old fallback — "if no role matched, use every role" — is gone.
        It would have swept in ``Anonymous``, ``Non member`` and the global
        roles, granting transitions to roles that cannot edit a work package
        at all.
        """
        configured = config.migration_config.get("workflow_roles") or []
        if configured:
            selected = [role for role in roles if role.get("name") in configured]
            if not selected:
                self.logger.warning(
                    "J2O_WORKFLOW_ROLES names %s, none of which exist on this instance (roles: %s); "
                    "falling back to every role that may edit work packages",
                    ", ".join(str(name) for name in configured),
                    ", ".join(str(role.get("name")) for role in roles),
                )
        else:
            selected = []

        if not selected:
            selected = [role for role in roles if role.get("edit_work_packages")]

        if not selected:
            # An instance that reports no such role is either very old or the
            # probe lost the field. Say so instead of writing rows against a
            # guessed role id, which would look successful and change nothing.
            self.logger.error(
                "No OpenProject role reports the 'edit_work_packages' permission; "
                "no workflow transitions can be written. Roles seen: %s",
                ", ".join(str(role.get("name")) for role in roles) or "none",
            )
            return []

        role_ids = sorted({int(role["id"]) for role in selected if int(role.get("id", 0) or 0) > 0})
        self.logger.info(
            "Writing workflow transitions for role(s): %s",
            ", ".join(f"{role.get('name')} (#{role.get('id')})" for role in selected),
        )
        return role_ids

    def _migrated_project_keys(self) -> list[str]:
        """Return the Jira project keys this migration covers.

        Scoped to the ``project`` mapping rather than the whole instance:
        transitions for projects nobody migrated would add workflow rows for
        statuses no work package here can reach.
        """
        try:
            project_mapping = self.mappings.get_mapping("project") or {}
        except Exception:
            return []
        return [str(key) for key in project_mapping if str(key).strip()]

    def _extract(self) -> ComponentResult:
        """Gather workflow schemes, transitions, and OpenProject roles."""
        try:
            data_list = self._fetch_workflow_metadata()
            data = data_list[0] if data_list else {}
            return ComponentResult(
                success=True,
                data=data,
                total_count=len(data.get("workflow_transitions", {})),
            )
        except Exception as exc:
            return ComponentResult(
                success=False,
                message=f"Failed to extract workflow metadata: {exc}",
                error=str(exc),
            )

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Translate Jira workflows into OpenProject workflow transition payloads."""
        if not extracted.success or not isinstance(extracted.data, dict):
            return ComponentResult(
                success=False,
                message="Workflow extraction failed",
                error=extracted.message or "extract phase returned no data",
            )

        issue_type_to_workflow: dict[str, str] = extracted.data.get("issue_type_to_workflow", {})
        observed_transitions: dict[str, list[dict[str, Any]]] = extracted.data.get(
            "observed_transitions",
            {},
        )
        roles: list[dict[str, Any]] = extracted.data.get("roles", [])

        status_mapping = self.mappings.get_mapping("status") or {}
        issue_type_mapping = self.mappings.get_mapping("issue_type") or {}

        status_by_id = {
            str(jira_id): entry
            for jira_id, entry in status_mapping.items()
            if isinstance(jira_id, str) and isinstance(entry, dict)
        }
        status_by_name = {
            str(entry.get("jira_name", "")).lower(): entry
            for entry in status_mapping.values()
            if isinstance(entry, dict) and entry.get("jira_name")
        }

        role_ids = self._workflow_role_ids(roles)

        dedup_transitions: dict[tuple[int, int, int], dict[str, Any]] = {}
        skipped: list[dict[str, Any]] = []
        unresolved_statuses: set[str] = set()
        collapsed = 0

        for issue_type_name, transitions in observed_transitions.items():
            mapping_entry = issue_type_mapping.get(issue_type_name)
            if not isinstance(mapping_entry, dict):
                skipped.append(
                    {
                        "reason": "missing_issue_type_mapping",
                        "issue_type": issue_type_name,
                        "transitions": len(transitions),
                    },
                )
                continue

            type_id = int(mapping_entry.get("openproject_id", 0) or 0)
            if type_id <= 0:
                skipped.append(
                    {
                        "reason": "invalid_openproject_type",
                        "issue_type": issue_type_name,
                        "transitions": len(transitions),
                    },
                )
                continue

            for transition in transitions:
                # ``status`` is keyed by the Jira status **id as a string**;
                # the name fallback covers a mapping written by an older run
                # that keyed on names.
                from_id = str(transition.get("from") or "")
                to_id = str(transition.get("to") or "")
                from_entry = status_by_id.get(from_id) or status_by_name.get(from_id.lower())
                to_entry = status_by_id.get(to_id) or status_by_name.get(to_id.lower())

                missing = [
                    jira_id
                    for jira_id, entry in ((from_id, from_entry), (to_id, to_entry))
                    if not isinstance(entry, dict)
                ]
                if missing:
                    skipped.append(
                        {
                            "reason": "missing_status_mapping",
                            "issue_type": issue_type_name,
                            "status_ids": missing,
                        },
                    )
                    unresolved_statuses.update(missing)
                    continue

                op_from = int(from_entry.get("openproject_id", 0) or 0)
                op_to = int(to_entry.get("openproject_id", 0) or 0)
                if op_from <= 0 or op_to <= 0:
                    continue
                if op_from == op_to:
                    # Two Jira statuses that collapsed onto one OpenProject
                    # status. A self-transition is not a move and OpenProject
                    # has no row shape for it.
                    collapsed += 1
                    continue

                key = (type_id, op_from, op_to)
                existing = dedup_transitions.get(key)
                if existing:
                    # Two Jira issue types can map onto one OpenProject type
                    # (Improvement and New Feature both become Feature here).
                    # Keep the one row and add up what each contributed.
                    existing["observed_count"] += int(transition.get("count", 0) or 0)
                    continue

                dedup_transitions[key] = {
                    "type_id": type_id,
                    "from_status_id": op_from,
                    "to_status_id": op_to,
                    "jira_issue_type": issue_type_name,
                    "jira_workflow": issue_type_to_workflow.get(issue_type_name),
                    "observed_count": int(transition.get("count", 0) or 0),
                }

        observed_total = sum(len(entries) for entries in observed_transitions.values())

        if unresolved_statuses:
            self.logger.warning(
                "%s Jira status(es) referenced by an observed transition are absent from the status "
                "mapping, so those transitions were not created: %s",
                len(unresolved_statuses),
                ", ".join(sorted(unresolved_statuses)),
            )
        if collapsed:
            self.logger.info(
                "%s transition(s) had the same OpenProject status on both ends (two Jira statuses "
                "mapped onto one) and were dropped",
                collapsed,
            )

        # A component that migrates nothing must not report success.
        #
        # This is the failure that hid the whole defect: the transition source
        # was a Cloud-only endpoint that 404s on Server, so every workflow came
        # back with zero transitions, nothing was mapped, nothing was skipped —
        # and "0 planned, 0 skipped, success" is indistinguishable from "this
        # Jira has no transitions". It stayed green through every run while the
        # target instance had no usable workflow at all.
        mapped = {
            "transitions": list(dedup_transitions.values()),
            "role_ids": role_ids,
            "skipped": skipped,
        }
        details = {
            "transitions_planned": len(dedup_transitions),
            "transitions_observed": observed_total,
            "issue_types_observed": len(observed_transitions),
            "skipped": len(skipped),
            "unresolved_jira_statuses": sorted(unresolved_statuses),
            "collapsed_self_transitions": collapsed,
            "role_ids": role_ids,
        }

        if observed_total == 0:
            return ComponentResult(
                success=False,
                data=mapped,
                message=(
                    "No status transitions could be read from Jira. Nothing was migrated, so no "
                    "work package will be movable between statuses in OpenProject."
                ),
                error="no transitions observed",
                total_count=0,
                details=details,
            )

        if not dedup_transitions:
            return ComponentResult(
                success=False,
                data=mapped,
                message=(
                    f"{observed_total} transition(s) were read from Jira but none could be mapped "
                    f"to OpenProject; check the status and issue_type mappings"
                ),
                error="no transitions mappable",
                total_count=0,
                details=details,
            )

        if not role_ids:
            return ComponentResult(
                success=False,
                data=mapped,
                message="No role can hold the workflow transitions, so writing them would change nothing",
                error="no eligible roles",
                total_count=len(dedup_transitions),
                details=details,
            )

        return ComponentResult(
            success=True,
            data=mapped,
            total_count=len(dedup_transitions),
            details=details,
        )

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Create workflow entries in OpenProject."""
        if not mapped.success or not isinstance(mapped.data, dict):
            return ComponentResult(
                success=False,
                message="Workflow mapping failed",
                error=mapped.message or "map phase returned no data",
            )

        transitions: list[dict[str, Any]] = mapped.data.get("transitions", [])
        role_ids: list[int] = mapped.data.get("role_ids", [])

        if not transitions or not role_ids:
            # Reached only by a direct call — ``_map`` now fails on both of
            # these — but it must not report success either way. "0 to
            # synchronise" was the message this component produced on every
            # run while the target had no usable workflow at all.
            return ComponentResult(
                success=False,
                message="Nothing to synchronise: no transitions, or no role to hold them",
                error="empty transition set",
                details={"created": 0, "existing": 0, "transitions": len(transitions), "role_ids": role_ids},
            )

        summary = self.op_client.sync_workflow_transitions(transitions, role_ids)
        created = int(summary.get("created", 0))
        existing = int(summary.get("existing", 0))
        errors = int(summary.get("errors", 0))

        success = errors == 0
        return ComponentResult(
            success=success,
            message="Workflow transitions synchronised",
            success_count=created,
            failed_count=errors,
            details={
                "created": created,
                "existing": existing,
                "errors": errors,
                "skipped": len(mapped.data.get("skipped", [])),
                "roles": role_ids,
                "transitions": len(transitions),
            },
        )

    def run(self) -> ComponentResult:
        """Execute the workflow migration pipeline (extract → map → load)."""
        self.logger.info("Starting workflow transition migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error(
                "Workflow extraction failed: %s",
                extracted.message or extracted.error,
            )
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error(
                "Workflow mapping failed: %s",
                mapped.message or mapped.error,
            )
            return mapped

        result = self._load(mapped)
        if result.success:
            self.logger.info(
                "Workflow migration completed (created=%s, existing=%s)",
                result.details.get("created", 0),
                result.details.get("existing", 0),
            )
        else:
            self.logger.error(
                "Workflow migration failed: created=%s existing=%s errors=%s",
                result.details.get("created", 0),
                result.details.get("existing", 0),
                result.details.get("errors", 0),
            )
        return result
