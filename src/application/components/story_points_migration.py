"""Migrate Jira Story Points onto OpenProject's native ``story_points`` column.

Detection strategy, in order:

1. The tenant's real ``customfield_<id>``, resolved by display name through
   :meth:`BaseMigration.jira_custom_field_ids_by_name`.
2. ``fields.storyPoints`` / ``customfield_10016`` / ``story_points``.
3. A scan of ``fields`` attributes whose *name* contains both "story" and
   "point".

Only the first does not guess, and it is the only one that works here: this Jira
numbers the field ``customfield_10106``, which is not the Cloud sample id and
whose attribute name contains neither "story" nor "point", so steps 2 and 3 both
miss it. Every one of the 81 values was being dropped, with the component
reporting ``success=True, updated=0``.

The destination is the native column rather than a custom field (decision of
2026-09-01): the "Story Points" custom field on this instance is a *text* one, so
it neither sorts nor sums, while the native column is an integer.

Fractional values round **up** (decision of 2026-09-29): 0.25, 0.5 and 0.75 all
become 1, and 1.5 becomes 2. The production copy of this Jira has 24 of them, and
they used to be skipped outright — a value of 0.25 is an estimate that something
is small, and recording it as "small" beats recording nothing. Rounding up rather
than to nearest keeps every estimated issue non-zero, which is what the column is
read for. The rounding is lossy on purpose, so the count is logged and reported
in the component result.

Note on dict access patterns kept here
--------------------------------------
:class:`JiraIssueFields` does not model per-tenant custom fields, so the boundary
parse stays as direct ``getattr`` on the raw fields object — same rationale as
the ``customfields_generic_migration`` carry-over from phase 7b. The work-package
mapping ladder, on the other hand, is normalised through
:class:`WorkPackageMappingEntry.from_legacy`.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, WorkPackageMappingEntry

STORY_POINTS_CF_NAME = "Story Points"


@register_entity_types("story_points")
class StoryPointsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict]:
        """Get current entities for change detection.

        StoryPointsMigration is a transformation-only component that operates on
        already-migrated work packages. It doesn't fetch source data from Jira,
        so change detection is not supported.

        Args:
            entity_type: Type of entities

        Raises:
            ValueError: Always, as this migration is transformation-only

        """
        msg = f"{type(self).__name__} is transformation-only and does not support change detection for entity type: {entity_type}"
        raise ValueError(msg)

    @staticmethod
    def _coerce_number(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except Exception:
                return None
        return None

    @staticmethod
    def _extract_story_points_from_fields(fields: Any, resolved_attr: str | None = None) -> float | None:
        # The tenant's real ``customfield_<id>``, resolved by display name from
        # the mapping ``CustomFieldMigration`` populated. Tried first because it
        # is the only strategy that does not guess: on this instance the field is
        # ``customfield_10106``, which matches none of the fallbacks below — not
        # the Cloud sample id, and not the ``dir()`` scan either, since that
        # matches on the *attribute* name and "customfield_10106" contains
        # neither "story" nor "point". All 81 values were being dropped.
        if resolved_attr and hasattr(fields, resolved_attr):
            num = StoryPointsMigration._coerce_number(getattr(fields, resolved_attr, None))
            if num is not None:
                return num

        # Preferred explicit attributes
        for attr in ("storyPoints", "customfield_10016", "story_points"):
            if hasattr(fields, attr):
                num = StoryPointsMigration._coerce_number(getattr(fields, attr, None))
                if num is not None:
                    return num

        # Fallback: scan attributes for name containing both story and point
        try:
            for name in dir(fields):
                lname = name.lower()
                if "story" in lname and "point" in lname:
                    num = StoryPointsMigration._coerce_number(getattr(fields, name, None))
                    if num is not None:
                        return num
        except Exception:
            return None
        return None

    def _extract(self) -> ComponentResult:
        """Extract Jira story points per issue mapped to a WP."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        # Production wp_map is keyed by str(jira_id) (numeric) outer with
        # the human-readable ``jira_key`` stored inside. Prefer the inner
        # ``jira_key`` so we feed _merge_batch_issues the human-readable
        # form it expects; fall back to the outer key for legacy or test
        # fixtures that key by jira_key directly.
        keys: list[str] = []
        for outer_key, raw_entry in wp_map.items():
            inner_jira_key = raw_entry.get("jira_key") if isinstance(raw_entry, dict) else None
            keys.append(str(inner_jira_key or outer_key))
        if not keys:
            return ComponentResult(success=True, data={"sp": {}})

        issues = self._merge_batch_issues(keys)

        # Resolve this tenant's real Story Points custom field id once.
        resolved_attr = self.jira_custom_field_ids_by_name().get(STORY_POINTS_CF_NAME)
        if resolved_attr:
            logger.info("Story Points resolved to Jira field %s", resolved_attr)
        else:
            logger.warning(
                "No Jira custom field named %r in the custom_field mapping —"
                " falling back to guessed ids, which found nothing on this instance",
                STORY_POINTS_CF_NAME,
            )

        sp_by_key: dict[str, float] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                num = self._extract_story_points_from_fields(fields, resolved_attr) if fields else None
                if isinstance(num, (int, float)):
                    sp_by_key[k] = float(num)
            except Exception:
                continue
        return ComponentResult(success=True, data={"sp": sp_by_key})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        raw: dict[str, float] = data.get("sp", {}) if isinstance(data, dict) else {}
        # Normalize to strings suitable for CF
        norm: dict[str, str] = {k: (f"{v:g}") for k, v in raw.items()}
        return ComponentResult(success=True, data={"sp_text": norm})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Write the story points onto the work packages' native column.

        This used to write a WorkPackage custom field, one Rails round-trip per
        issue. OpenProject 17.6 has a real ``work_packages.story_points`` column,
        and by decision on 2026-09-01 that is where the value goes: the custom
        field this instance has is a *text* one, so it neither sorts nor sums.

        The column is an integer, and fractional values round up rather than
        being skipped (2026-09-29). Most of this Jira's values are already whole
        (1, 2, 3, 5, 8, 13, 20, 40, 100); the 24 that are not would otherwise
        reach OpenProject as nothing at all.

        Batching also replaces the per-work-package ``execute_query`` loop, and
        ``batch_update_work_packages`` reports back the attributes it could not
        apply rather than dropping them in silence.
        """
        wp_map = self.mappings.get_mapping("work_package") or {}
        data = mapped.data or {}
        text_by_key: dict[str, str] = data.get("sp_text", {}) if isinstance(data, dict) else {}

        # Build a fast jira_key → typed-entry lookup once. We walk
        # ``wp_map.items()`` and use the inner ``jira_key`` (production
        # layout: outer key is numeric jira_id, inner ``jira_key`` is the
        # human-readable form) so subsequent lookups work regardless of
        # which key shape the on-disk file uses.
        entries_by_jira_key: dict[str, WorkPackageMappingEntry] = {}
        for outer_key, raw_entry in wp_map.items():
            inner_jira_key = raw_entry.get("jira_key") if isinstance(raw_entry, dict) else None
            key_for_legacy = str(inner_jira_key or outer_key)
            try:
                entries_by_jira_key[key_for_legacy] = WorkPackageMappingEntry.from_legacy(key_for_legacy, raw_entry)
            except ValueError:
                continue

        updates: list[dict[str, Any]] = []
        rounded_up = 0
        for jira_key, text in text_by_key.items():
            if text is None or text == "0":
                continue
            entry = entries_by_jira_key.get(jira_key)
            if entry is None:
                continue
            # Jira hands these over as floats ("13.0"); the column is an integer.
            # Parsed as Decimal rather than float so the ceiling below acts on
            # the value as written: rounding *up* turns any float noise above a
            # whole number into a whole extra point.
            try:
                value = Decimal(text)
            except TypeError, InvalidOperation:
                continue
            points = math.ceil(value)
            if points != value:
                logger.debug("Story Points for %s rounded up: %s -> %d", jira_key, value, points)
                rounded_up += 1
            updates.append({"id": int(entry.openproject_id), "story_points": points})

        if rounded_up:
            # Lossy and deliberate, so it is said once at a level that shows up
            # in a normal run rather than only under --debug.
            logger.info("Story Points: %d fractional value(s) rounded up to the next integer", rounded_up)

        if not updates:
            return ComponentResult(success=True, updated=0, failed=0, details={"rounded_up": rounded_up})

        try:
            result = self.op_client.batch_update_work_packages(updates)
        except Exception:
            logger.exception("Failed to apply Story Points to %d work packages", len(updates))
            return ComponentResult(success=False, updated=0, failed=len(updates))

        updated = int(result.get("updated", 0)) if isinstance(result, dict) else 0
        failed = int(result.get("failed", 0)) if isinstance(result, dict) else len(updates)
        # ``batch_update_work_packages`` names the attributes it could not set.
        # An empty ``story_points`` setter would otherwise look like a clean run.
        unapplied = (result or {}).get("unapplied") if isinstance(result, dict) else None
        if unapplied:
            logger.warning("OpenProject did not apply: %s", unapplied)

        logger.info(
            "Story Points: %d work packages updated, %d failed, %d rounded up",
            updated,
            failed,
            rounded_up,
        )
        return ComponentResult(
            success=failed == 0,
            updated=updated,
            failed=failed,
            details={"rounded_up": rounded_up},
        )

    def run(self) -> ComponentResult:
        """Run Story points migration."""
        return self._run_etl_pipeline("Story points")
