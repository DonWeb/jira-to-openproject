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
it neither sorts nor sums, while the native column is an integer and every value
in this Jira is whole.

Note on dict access patterns kept here
--------------------------------------
:class:`JiraIssueFields` does not model per-tenant custom fields, so the boundary
parse stays as direct ``getattr`` on the raw fields object — same rationale as
the ``customfields_generic_migration`` carry-over from phase 7b. The work-package
mapping ladder, on the other hand, is normalised through
:class:`WorkPackageMappingEntry.from_legacy`.
"""

from __future__ import annotations

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
        field this instance has is a *text* one, so it neither sorts nor sums,
        while the native column is an integer and every value in this Jira is
        whole (1, 2, 3, 5, 8, 10, 13, 20, 40, 100).

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
        skipped_fractional = 0
        for jira_key, text in text_by_key.items():
            if text is None or text == "0":
                continue
            entry = entries_by_jira_key.get(jira_key)
            if entry is None:
                continue
            # Jira hands these over as floats ("13.0"); the column is an integer.
            # A fractional value would be truncated, so it is reported instead —
            # none exist on this instance, but silence would be the wrong default.
            try:
                value = float(text)
            except (TypeError, ValueError):
                continue
            if value != int(value):
                logger.warning(
                    "Story Points for %s is %s, which the integer column cannot"
                    " hold without loss — skipped",
                    jira_key,
                    value,
                )
                skipped_fractional += 1
                continue
            updates.append({"id": int(entry.openproject_id), "story_points": int(value)})

        if not updates:
            return ComponentResult(success=True, updated=0, failed=skipped_fractional)

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
            "Story Points: %d work packages updated, %d failed, %d skipped (fractional)",
            updated,
            failed,
            skipped_fractional,
        )
        return ComponentResult(
            success=failed == 0,
            updated=updated,
            failed=failed + skipped_fractional,
        )

    def run(self) -> ComponentResult:
        """Run Story points migration."""
        return self._run_etl_pipeline("Story points")
