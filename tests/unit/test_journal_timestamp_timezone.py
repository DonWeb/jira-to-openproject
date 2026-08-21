"""Journal timestamps must be ordered and re-emitted on instants, not strings.

The 2026-08-20 run failed 211 of 435 work packages with a single Postgres error:

    PG::DataException: range lower bound must be less than or equal to
    range upper bound

Cause: the collision resolver formatted a timezone-aware value with
``strftime``, which drops the ``tzinfo`` and emits the *local* clock fields, then
appended a literal ``"+0000"``. Every timestamp this Jira instance returns
carries ``-0300`` (276 of 276 in the cached data), so a resolved collision landed
three hours *before* the entry it was supposed to follow and inverted the range.

The 25 tests written alongside the component all passed. Every one of them used
``+0000`` fixtures — the single offset for which the bug is invisible. These use
``-0300``, which is what the instance actually returns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from src.application.components.work_package_migration import (
    _normalize_instant_iso,
    _parse_jira_instant,
)

# The exact shape Jira Server/DC returns on this instance.
JIRA_TS = "2018-07-25T12:00:24.000-0300"


class TestParseJiraInstant:
    def test_offset_bearing_timestamp_converts_to_utc(self) -> None:
        """``12:00:24-0300`` is ``15:00:24`` UTC — not ``12:00:24`` UTC."""
        parsed = _parse_jira_instant(JIRA_TS)

        assert parsed == datetime(2018, 7, 25, 15, 0, 24, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2018-07-25T12:00:24.000-0300", datetime(2018, 7, 25, 15, 0, 24, tzinfo=UTC)),
            ("2018-07-25T12:00:24.000Z", datetime(2018, 7, 25, 12, 0, 24, tzinfo=UTC)),
            ("2018-07-25T12:00:24+00:00", datetime(2018, 7, 25, 12, 0, 24, tzinfo=UTC)),
            # Naive database format: read as UTC, which is what the pipeline
            # assumed before and keeps assuming.
            ("2018-07-25 12:00:24.000", datetime(2018, 7, 25, 12, 0, 24, tzinfo=UTC)),
        ],
    )
    def test_every_shape_the_pipeline_sees(self, raw: str, expected: datetime) -> None:
        assert _parse_jira_instant(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "not-a-date", "2018-13-45"])
    def test_unusable_input_returns_none_rather_than_a_wrong_instant(self, raw: object) -> None:
        assert _parse_jira_instant(raw) is None

    def test_datetime_input_is_converted_not_relabelled(self) -> None:
        aware = datetime(2018, 7, 25, 12, 0, 24, tzinfo=UTC) - timedelta(hours=3)
        assert _parse_jira_instant(aware) == datetime(2018, 7, 25, 9, 0, 24, tzinfo=UTC)


class TestNormalizeInstantIso:
    def test_emits_a_real_offset_never_a_pasted_label(self) -> None:
        """The regression in one assertion.

        The old code produced ``2018-07-25T12:00:24.000+0000`` for this input:
        the local clock fields with a UTC label glued on. The instant has to be
        preserved, so the hour must read 15.
        """
        assert _normalize_instant_iso(JIRA_TS) == "2018-07-25T15:00:24+00:00"

    def test_round_trips_through_parsing(self) -> None:
        iso = _normalize_instant_iso(JIRA_TS)
        assert iso is not None
        assert datetime.fromisoformat(iso) == _parse_jira_instant(JIRA_TS)

    def test_unparseable_returns_none_so_callers_can_decide(self) -> None:
        assert _normalize_instant_iso("nope") is None


class TestChainMonotonicity:
    """The property the failing work packages violated.

    Mirrors what ``_build_rails_ops_for_issue`` does between sorting the merged
    comment/changelog entries and handing ``validity_period`` bounds to Ruby.
    """

    @staticmethod
    def _resolve(raw_timestamps: list[str]) -> list[str]:
        entries = [{"timestamp": r, "instant": _parse_jira_instant(r)} for r in raw_timestamps]
        entries.sort(
            key=lambda e: (
                e["instant"] is None,
                e["instant"] or datetime.min.replace(tzinfo=UTC),
            ),
        )
        last: datetime | None = None
        for entry in entries:
            instant = entry["instant"]
            if instant is None:
                instant = last + timedelta(seconds=1) if last else None
            elif last is not None and instant <= last:
                instant = last + timedelta(seconds=1)
            entry["instant"] = instant
            entry["timestamp"] = instant.isoformat() if instant else ""
            if instant is not None:
                last = instant
        return [e["timestamp"] for e in entries]

    def test_colliding_offset_timestamps_move_forward_not_backward(self) -> None:
        """Two Jira events in the same second, the case that failed 211 WPs.

        The resolved second entry must land one second *after* the first, at
        ``15:00:25`` UTC — not at ``12:00:25``, three hours before it.
        """
        resolved = self._resolve([JIRA_TS, JIRA_TS])

        assert resolved == ["2018-07-25T15:00:24+00:00", "2018-07-25T15:00:25+00:00"]

    def test_resulting_chain_is_a_valid_set_of_ranges(self) -> None:
        """Every ``[lower, upper)`` must satisfy ``lower < upper``."""
        resolved = self._resolve(
            [
                "2018-07-25T12:00:24.000-0300",
                "2018-07-25T12:00:24.000-0300",
                "2018-07-25T12:00:24.000-0300",
                "2018-07-25T12:00:30.000-0300",
            ],
        )

        instants = [datetime.fromisoformat(ts) for ts in resolved]
        for lower, upper in pairwise(instants):
            assert lower < upper, f"{lower} is not strictly before {upper}"

    def test_ordering_is_by_instant_not_by_string(self) -> None:
        """Mixed offsets sort by the point in time they denote.

        ``09:00:00-0300`` (12:00 UTC) comes *after* ``11:00:00+00:00``, even
        though it sorts first as text.
        """
        resolved = self._resolve(
            ["2018-07-25T09:00:00.000-0300", "2018-07-25T11:00:00.000+00:00"],
        )

        assert resolved == ["2018-07-25T11:00:00+00:00", "2018-07-25T12:00:00+00:00"]

    def test_unparseable_entries_are_kept_and_parked_last(self) -> None:
        """Dropping them would lose a comment; letting them lead would break the chain."""
        resolved = self._resolve(["", JIRA_TS])

        assert resolved == ["2018-07-25T15:00:24+00:00", "2018-07-25T15:00:25+00:00"]


class TestBatchTemplateSafety:
    """Properties of ``create_work_package_journals_batch.rb`` itself.

    The Python fix above prevents the bad input; these guard the Ruby side so a
    future bad input degrades into correct data instead of into deleted journals.
    """

    @staticmethod
    def _template() -> str:
        from pathlib import Path

        path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "ruby"
            / "create_work_package_journals_batch.rb"
        )
        return path.read_text(encoding="utf-8")

    def test_each_work_package_is_wrapped_in_a_transaction(self) -> None:
        """Without it, a failed INSERT leaves the WP stripped of its journals.

        On 2026-08-20 that cost 211 work packages ~559 journals, comments
        included: the delete had already committed when the rescue ran.
        """
        template = self._template()

        assert "ActiveRecord::Base.transaction do" in template
        # The delete must be inside the transaction, not before it.
        assert template.index("ActiveRecord::Base.transaction do") < template.index(
            "v2_plus_journals.delete_all",
        )

    def test_a_rolled_back_work_package_reports_zero_created(self) -> None:
        assert "result['created'] = 0" in self._template()

    def test_ranges_are_built_from_a_normalised_timeline(self) -> None:
        """One monotonic pass over the chain, not a per-pair guard.

        The upper bound of journal N is the lower bound of journal N+1, so
        nudging a single pair in isolation converts an inverted range into an
        overlap and trips ``non_overlapping_journals_validity_periods`` instead.
        """
        template = self._template()

        assert "timeline[i] = timeline[i - 1] + 0.001 if timeline[i] <= timeline[i - 1]" in template
        assert "range_for = lambda" in template
        # Both the v1 update and the bulk insert read from that one lambda.
        assert template.count("range_for.call(") == 2

    def test_exclusion_constraint_is_deferred_for_the_rewrite(self) -> None:
        """Intermediate states of a chain rewrite are transiently inconsistent."""
        assert "SET CONSTRAINTS non_overlapping_journals_validity_periods DEFERRED" in self._template()
