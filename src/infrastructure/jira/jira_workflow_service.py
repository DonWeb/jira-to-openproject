"""Jira workflow configuration queries.

Phase 3b of ADR-002 continues the jira_client.py decomposition. The
workflow-related methods (workflow scheme listing, transition lookup,
status lookup) move into a focused service.

The service is exposed on ``JiraClient`` as ``self.workflows`` and the
client keeps thin delegators so existing call sites continue to work
unchanged. Like ``JiraProjectService`` this is HTTP-only — calls go
through the ``jira`` SDK or ``JiraClient._make_request`` — so there is
no Ruby-script escaping to worry about.
"""

from __future__ import annotations

from typing import Any

from requests import exceptions

from src import config
from src.infrastructure.jira.jira_client import (
    HTTP_NOT_FOUND,
    JiraApiError,
    JiraClient,
    JiraConnectionError,
)


def _is_workflow_unfetchable(exc: BaseException) -> bool:
    """Return True if *exc* represents a workflow endpoint that cannot be fetched.

    Two cases are treated as "unfetchable" and suppressed at DEBUG level
    (instead of raising at ERROR) because they indicate a server-side
    configuration limitation rather than a real programming error:

    1. **HTTP 404** — the ``/rest/api/2/workflow/<name>`` endpoint simply does
       not exist on this Jira Server/DC version.  This is the original case
       that PR #240 addressed.

    2. **HTTP 400 with "Invalid URI" / "encoded slash"** — Apache Tomcat
       rejects percent-encoded slash characters (``%2F``) in URL path segments
       by default (``ALLOW_ENCODED_SLASH`` is ``false``).  A workflow name that
       contains a literal ``/`` (e.g. ``"NREDIT: Blog/CS Workflow"``) is
       URL-encoded as ``%2F`` by ``urllib.parse.quote``; Tomcat then returns
       HTTP 400 with an HTML body containing "Invalid URI" and a message about
       the encoded slash.  The URL is structurally unfetchable — no retry
       strategy can work around a server-side Tomcat configuration default.
       Treating it like a 404 (silent DEBUG, empty result) is correct.

    Implementation notes
    --------------------
    In production ``JiraClient._patch_jira_client`` wraps every exception
    (including ``JIRAError``) into ``JiraApiError`` so the outer exception is
    never a ``JIRAError`` directly.  The original ``JIRAError`` is stored as
    ``exc.__cause__``.  Walking the chain makes the check work for both the
    bare-``JIRAError`` path (unit-test stub, some alternate code paths) and
    the production-wrapping path.

    The *seen* set guards against pathological cycles where ``__cause__`` is
    set to the exception itself.
    """
    from jira.exceptions import JIRAError as _JIRAError

    # Tomcat's ``ALLOW_ENCODED_SLASH=false`` rejects ``%2F`` in URI paths with
    # an HTML page whose body contains both phrases. We require *both* markers
    # so that a generic "Invalid URI" response from a different cause is not
    # silently demoted to DEBUG.
    _ENCODED_SLASH_MARKERS = ("invalid uri", "encoded slash")

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        if isinstance(current, _JIRAError):
            if status == HTTP_NOT_FOUND:
                return True
            if status == 400:
                text = (getattr(current, "text", "") or "").lower()
                if all(marker in text for marker in _ENCODED_SLASH_MARKERS):
                    return True
        current = current.__cause__
    return False


class JiraWorkflowService:
    """Workflow-domain queries for ``JiraClient``."""

    def __init__(self, client: JiraClient) -> None:
        self._client = client
        # ``JiraClient`` uses the module-level ``logger`` from
        # ``src.infrastructure.jira.jira_client`` — pick that up so the service can
        # log through ``self._logger`` like the OpenProject services do.
        from src.infrastructure.jira.jira_client import logger

        self._logger = logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_workflow_schemes(self) -> list[dict[str, Any]]:
        """Return configured Jira workflow schemes with issue type mappings."""
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        url = f"{client.base_url}/rest/api/2/workflowscheme"
        self._logger.info("Fetching Jira workflow schemes")

        try:
            response = client.jira._session.get(url)
            response.raise_for_status()
            payload = response.json()
            values = payload.get("values") if isinstance(payload, dict) else None
            schemes = values if isinstance(values, list) else []
            self._logger.info("Retrieved %s workflow schemes", len(schemes))
            return schemes
        except exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 405:
                self._logger.warning(
                    "GET /rest/api/2/workflowscheme unsupported, falling back to per-project workflow inspection",
                )
                return self._get_workflow_schemes_per_project()
            error_msg = f"Failed to fetch workflow schemes: {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc
        except JiraApiError as exc:
            # ``JiraClient._handle_response`` raises ``JiraApiError`` with
            # messages like ``"HTTP Error 405: ..."`` (note the word
            # "Error"), but the patched session in some paths raises
            # ``"HTTP 405: ..."`` directly. Match both forms so the
            # per-project fallback fires regardless of which path
            # produced the exception. Pre-extraction code only checked
            # the second form, which silently never matched in
            # production.
            exc_text = str(exc)
            if "HTTP Error 405" in exc_text or "HTTP 405" in exc_text:
                self._logger.warning(
                    "Workflow scheme endpoint returned 405; using per-project fallback",
                )
                return self._get_workflow_schemes_per_project()
            raise
        except Exception as exc:
            error_msg = f"Failed to fetch workflow schemes: {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc

    def _get_workflow_schemes_per_project(self) -> list[dict[str, Any]]:
        """Fallback that assembles workflow schemes via project endpoints."""
        client = self._client
        project_keys: list[str] = []
        try:
            project_mapping = config.mappings.get_mapping("project") or {}
            project_keys = [str(key) for key in project_mapping]
        except Exception:
            project_keys = []

        if not project_keys:
            try:
                projects = client.get_projects()
                project_keys = [str(p.get("key")) for p in projects if p.get("key")]
            except Exception:
                project_keys = []

        schemes_by_id: dict[str, dict[str, Any]] = {}
        for key in project_keys:
            if not key:
                continue
            try:
                response = client._make_request(f"/rest/api/2/project/{key}/workflowscheme")
                if response.status_code == HTTP_NOT_FOUND:
                    continue
                response.raise_for_status()
                payload = response.json() or {}
            except Exception as exc:
                self._logger.debug("Failed to fetch workflow scheme for project %s: %s", key, exc)
                continue

            scheme = payload.get("workflowScheme") or payload
            if not isinstance(scheme, dict):
                continue

            scheme_id = str(scheme.get("id") or scheme.get("name") or key)
            existing = schemes_by_id.get(scheme_id)
            if existing:
                mappings = existing.setdefault("issueTypeMappings", {})
                if isinstance(mappings, dict):
                    new_mappings = scheme.get("issueTypeMappings") or {}
                    if isinstance(new_mappings, dict):
                        mappings.update(new_mappings)
                existing.setdefault("projects", set()).add(key)
            else:
                entry = dict(scheme)
                entry["projects"] = {key}
                schemes_by_id[scheme_id] = entry

        for entry in schemes_by_id.values():
            projects = entry.get("projects")
            if isinstance(projects, set):
                entry["projects"] = sorted(projects)

        self._logger.info(
            "Discovered %s workflow schemes via per-project fallback",
            len(schemes_by_id),
        )
        return list(schemes_by_id.values())

    def get_observed_transitions(
        self,
        project_keys: list[str],
        *,
        page_size: int = 100,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return the status transitions issues have actually made, per issue type.

        **Jira Server/DC does not expose a workflow's transition graph over
        REST.** Every candidate was tried against this instance (Jira
        Server 9.12.2) and none returns it:

        =========================================== ======================
        Endpoint                                    Result
        =========================================== ======================
        ``/rest/api/2/workflow/search``             404 (Cloud-only)
        ``/rest/api/2/workflow?workflowName=…``     200, but only ``steps``
                                                    as a count — no graph
        ``/rest/workflowDesigner/1.0/workflows``    404
        ``/rest/projectconfig/1/workflowscheme/…``  200, scheme only
        =========================================== ======================

        The changelog is the source that does exist. Every status change an
        issue ever made is recorded there as a ``status`` field item with
        ``from``/``to`` status ids, so the set of transitions Jira permits
        for an issue type is recoverable from what its issues actually did.
        On this instance 435 issues yield 134 distinct
        ``(issue type, from, to)`` triples across 8 issue types.

        The limitation is worth stating plainly, because it is the reason
        this is a *sample* rather than a specification: a transition that
        Jira allows but nobody ever used leaves no trace, so it cannot be
        recovered. Callers should report the resulting gaps rather than
        inventing transitions to fill them.

        Returns ``{issue_type_name: [{"from": id, "to": id, "count": n}]}``
        with ids as strings, matching the ``status`` mapping's key type.
        """
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        keys = [str(key).strip() for key in project_keys if str(key).strip()]
        if not keys:
            self._logger.warning("No project keys given; cannot observe workflow transitions")
            return {}

        jql = f"project in ({','.join(keys)}) ORDER BY key ASC"
        url = f"{client.base_url}/rest/api/2/search"
        self._logger.info("Reading status transitions from the changelog of %s project(s)", len(keys))

        # (issue type, from, to) → how many issues made that move. The count is
        # not used to decide anything; it goes in the run summary so a single
        # freak transition is distinguishable from the project's normal path.
        counts: dict[tuple[str, str, str], int] = {}
        start = 0
        issues_seen = 0

        while True:
            try:
                response = client.jira._session.get(
                    url,
                    params={
                        "jql": jql,
                        "startAt": start,
                        "maxResults": page_size,
                        "fields": "issuetype",
                        "expand": "changelog",
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                error_msg = f"Failed to read issue changelogs for workflow transitions: {exc!s}"
                self._logger.exception(error_msg)
                raise JiraApiError(error_msg) from exc

            issues = payload.get("issues") if isinstance(payload, dict) else None
            if not isinstance(issues, list):
                break

            for issue in issues:
                issues_seen += 1
                fields = issue.get("fields") or {}
                issue_type = (fields.get("issuetype") or {}).get("name")
                if not issue_type:
                    continue
                histories = (issue.get("changelog") or {}).get("histories") or []
                for history in histories:
                    for item in history.get("items") or []:
                        if item.get("field") != "status":
                            continue
                        from_id = item.get("from")
                        to_id = item.get("to")
                        # A creation entry has no ``from``; it is not a
                        # transition, it is where the issue started.
                        if not from_id or not to_id:
                            continue
                        key = (str(issue_type), str(from_id), str(to_id))
                        counts[key] = counts.get(key, 0) + 1

            total = payload.get("total", 0) if isinstance(payload, dict) else 0
            start += page_size
            if not issues or start >= int(total or 0):
                break

        observed: dict[str, list[dict[str, Any]]] = {}
        for (issue_type, from_id, to_id), count in counts.items():
            observed.setdefault(issue_type, []).append(
                {"from": from_id, "to": to_id, "count": count},
            )

        self._logger.info(
            "Observed %s distinct transition(s) across %s issue type(s) in %s issue(s)",
            len(counts),
            len(observed),
            issues_seen,
        )
        return observed

    def get_workflow_transitions(self, workflow_name: str) -> list[dict[str, Any]]:
        """Return transitions for a given Jira workflow name.

        .. deprecated::
           ``/rest/api/2/workflow/search`` is a Jira **Cloud** endpoint and
           404s on Server/DC, so on a Server target this always returned an
           empty list — which read as "this workflow has no transitions"
           and produced a migration that wrote nothing while reporting
           success. :meth:`get_observed_transitions` is the Server-capable
           replacement. This is kept for Cloud targets and for callers that
           still address a workflow by name.
        """
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        # ``/rest/api/2/workflow/<name>/transitions`` is not part of the public
        # Jira Server/DC REST API (confirmed live on this instance — see git
        # history: "fix(jira): treat 404 from per-workflow endpoints as empty,
        # not error"), so it consistently 404s and this migration always saw
        # 0 transitions. ``/rest/api/2/workflow/search`` is the documented
        # replacement that exposes the same data via ``expand=transitions``.
        url = f"{client.base_url}/rest/api/2/workflow/search"
        self._logger.debug("Fetching Jira workflow transitions for '%s'", workflow_name)

        try:
            response = client.jira._session.get(
                url,
                params={"workflowName": workflow_name, "expand": "transitions"},
            )
            # Defensive check for callers that return a response object
            # with status_code rather than raising.  The ``jira`` library's
            # ResilientSession raises JIRAError before reaching this point,
            # so this branch is a belt-and-suspenders guard only.
            if getattr(response, "status_code", None) == HTTP_NOT_FOUND:
                self._logger.debug(
                    "Workflow '%s' returned 404 for transitions; treating as empty",
                    workflow_name,
                )
                return []
            response.raise_for_status()
            payload = response.json()
            values = payload.get("values") if isinstance(payload, dict) else None
            if isinstance(values, list) and not values:
                # A 200 with zero matches means ``workflowName`` did not match
                # any workflow on the server — could be a permission filter
                # (some Jira admin endpoints silently drop results the token's
                # user can't see), a name mismatch, or an unsupported filter
                # param on this Jira version. Log at WARNING (not the usual
                # DEBUG) so the next live run shows which case it is instead
                # of silently looking identical to "workflow has 0 transitions".
                self._logger.warning(
                    "Workflow '%s' matched 0 entries via /workflow/search (workflowName filter); "
                    "raw payload keys=%s, total=%s",
                    workflow_name,
                    sorted(payload.keys()) if isinstance(payload, dict) else None,
                    payload.get("total") if isinstance(payload, dict) else None,
                )
                return []
            workflow = values[0] if isinstance(values, list) and values else None
            transitions = workflow.get("transitions") if isinstance(workflow, dict) else None
            if not isinstance(transitions, list):
                self._logger.warning(
                    "Unexpected workflow transitions payload for %s; workflow entry keys=%s",
                    workflow_name,
                    sorted(workflow.keys()) if isinstance(workflow, dict) else type(workflow).__name__,
                )
                return []
            self._logger.debug(
                "Workflow '%s' returned %s transitions",
                workflow_name,
                len(transitions),
            )
            return transitions
        except Exception as exc:
            # ``/workflow/search`` is documented and shouldn't 404 for a real
            # workflow name, but this stays as a safety net: a stale/renamed
            # workflow name would 404, and names containing "/" can still hit
            # the Tomcat encoded-slash 400 in the query-string encoding.
            #
            # In production JiraClient._patch_jira_client wraps every exception
            # into JiraApiError, so exc is never a JIRAError directly — the
            # original JIRAError lives in exc.__cause__.
            # _is_workflow_unfetchable walks the __cause__ chain and handles:
            #   • HTTP 404 — workflow name not found
            #   • HTTP 400 "Invalid URI" — Tomcat rejects %2F in URL path
            if _is_workflow_unfetchable(exc):
                self._logger.debug(
                    "Workflow '%s' transitions endpoint unfetchable (404 not-found or Tomcat encoded-slash 400); treating as empty",
                    workflow_name,
                )
                return []
            error_msg = f"Failed to fetch transitions for workflow '{workflow_name}': {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc

    def get_workflow_statuses(self, workflow_name: str) -> list[dict[str, Any]]:
        """Return statuses referenced by a workflow."""
        client = self._client
        if not client.jira:
            msg = "Jira client is not initialized"
            raise JiraConnectionError(msg)

        # Same rationale as ``get_workflow_transitions``: the per-name
        # ``/rest/api/2/workflow/<name>`` endpoint 404s on this Jira
        # Server/DC version; ``/rest/api/2/workflow/search`` is the
        # documented endpoint that also exposes ``statuses`` via ``expand``.
        url = f"{client.base_url}/rest/api/2/workflow/search"
        self._logger.debug("Fetching Jira workflow definition for '%s'", workflow_name)

        try:
            response = client.jira._session.get(
                url,
                params={"workflowName": workflow_name, "expand": "statuses"},
            )
            # Defensive check for callers that return a response object
            # with status_code rather than raising.  The ``jira`` library's
            # ResilientSession raises JIRAError before reaching this point,
            # so this branch is a belt-and-suspenders guard only.
            if getattr(response, "status_code", None) == HTTP_NOT_FOUND:
                self._logger.debug(
                    "Workflow '%s' returned 404 for definition; treating as empty",
                    workflow_name,
                )
                return []
            response.raise_for_status()
            payload = response.json()
            values = payload.get("values") if isinstance(payload, dict) else None
            workflow = values[0] if isinstance(values, list) and values else None
            if isinstance(workflow, dict):
                statuses = workflow.get("statuses")
                if isinstance(statuses, list):
                    return statuses
            self._logger.warning(
                "Unexpected workflow status payload for %s (type=%s)",
                workflow_name,
                type(workflow).__name__,
            )
            return []
        except Exception as exc:
            # ``/workflow/search`` is documented and shouldn't 404 for a real
            # workflow name — see ``get_workflow_transitions`` above for why
            # this safety net stays in place.
            #
            # In production JiraClient._patch_jira_client wraps every exception
            # into JiraApiError, so exc is never a JIRAError directly — the
            # original JIRAError lives in exc.__cause__.
            # _is_workflow_unfetchable walks the __cause__ chain and handles:
            #   • HTTP 404 — workflow name not found
            #   • HTTP 400 "Invalid URI" — Tomcat rejects %2F in URL path
            if _is_workflow_unfetchable(exc):
                self._logger.debug(
                    "Workflow '%s' definition endpoint unfetchable (404 not-found or Tomcat encoded-slash 400); treating as empty",
                    workflow_name,
                )
                return []
            error_msg = f"Failed to fetch workflow definition for '{workflow_name}': {exc!s}"
            self._logger.exception(error_msg)
            raise JiraApiError(error_msg) from exc
