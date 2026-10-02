"""A component that reports its work as ``updated`` must not summarise as 0/0.

``_extract_counts`` feeds the per-component line the orchestrator prints. It
read ``success_count``/``failed_count``/``total_count`` and a handful of
component-specific shapes, but never ``updated`` — which is a first-class field
on ``ComponentResult`` and the one most components actually fill in.

On the 2026-09-30 run that silenced eight components at once. ``customfields_generic``
applied 24492 updates over 88 minutes and reported "0/0 items migrated";
``sprint_epic`` did 11918 and reported the same. The numbers were in the results
JSON the whole time — only the summary could not see them.
"""

from __future__ import annotations

import pytest

from src.migration import _extract_counts, _format_component_outcome
from src.models.component_results import ComponentResult


def test_model_updated_becomes_the_success_count() -> None:
    """The exact shape ``customfields_generic`` returns."""
    result = ComponentResult(success=True, updated=24492)

    assert _extract_counts(result) == (24492, 0, 24492)


def test_details_updated_is_read_too() -> None:
    """``wp_timestamp_restore`` puts the same number one level down."""
    result = ComponentResult(success=True, details={"updated": 6782})

    assert _extract_counts(result) == (6782, 0, 6782)


def test_failed_is_picked_up_alongside() -> None:
    """Otherwise a run that updated nothing and failed on everything reads 0/0."""
    result = ComponentResult(success=False, updated=0, failed=5)

    assert _extract_counts(result) == (0, 5, 5)


def test_partial_work_shows_both_halves() -> None:
    result = ComponentResult(success=True, updated=10, failed=5)

    assert _extract_counts(result) == (10, 5, 15)


def test_an_explicit_summary_still_wins() -> None:
    """A component that sets both keeps the one it chose.

    ``relations`` counts skipped items in its total, so deriving from
    ``updated`` would shrink a total it set deliberately.
    """
    result = ComponentResult(
        success=True,
        updated=999,
        details={"success_count": 741, "failed_count": 0, "total_count": 1706},
    )

    assert _extract_counts(result) == (741, 0, 1706)


def test_the_total_is_never_smaller_than_its_parts() -> None:
    """A line like ``11918/0 items migrated`` reads as broken.

    It also always means the two halves came from different sources.
    """
    result = ComponentResult(success=True, updated=11918, total_count=0)

    _success, _failed, total = _extract_counts(result)

    assert total == 11918


def test_skipped_items_keep_the_total_above_the_sum() -> None:
    """The floor must not flatten a total that legitimately exceeds the parts."""
    result = ComponentResult(
        success=True,
        details={"success_count": 0, "failed_count": 0, "total_count": 8711},
    )

    assert _extract_counts(result) == (0, 0, 8711)


@pytest.mark.parametrize(
    ("updated", "expected"),
    [(24492, "24492/24492 items migrated"), (0, "0/0 items migrated")],
)
def test_the_rendered_line_carries_the_number(updated: int, expected: str) -> None:
    """End to end: this is the string the operator actually reads."""
    result = ComponentResult(success=True, updated=updated)

    _level, message = _format_component_outcome("customfields_generic", result, 5296.9)

    assert expected in message
