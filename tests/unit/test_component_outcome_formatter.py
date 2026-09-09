"""Per-component outcome log line must be informative.

Issue #260 reported a misleading pair of lines from the run summary::

    Component 'companies'     failed or had errors (0/0 items migrated, 0 failed), took 0.00 seconds
    Component 'admin_schemes' completed successfully (0/0 items migrated), took 0.00 seconds

Both produced ``0/0``. One is "failed", the other is "successfully", and the
underlying reason (a ``JiraApiError`` from the extractor) is invisible in
the line — you have to scroll back through the log to find it.

These tests pin a refactored ``_format_component_outcome`` helper:

    * 3 outcome levels: ``success`` / ``warning`` / ``error``.
    * The failure line includes the underlying cause (``result.error``
      → ``result.errors[0]`` → ``details['error']`` → ``result.message``)
      inline, so the user sees *why*.
    * Partial success (``success=True`` but with errors) emits a
      ``warning``-level line, not a fake "success".
"""

from __future__ import annotations

from src.migration import _first_error_message, _format_component_outcome
from src.models.component_results import ComponentResult


class TestFirstErrorMessage:
    def test_prefers_error_field(self) -> None:
        r = ComponentResult(
            success=False,
            error="X failed",
            errors=["different"],
            details={"error": "also different"},
            message="message",
        )
        assert _first_error_message(r) == "X failed"

    def test_falls_back_to_errors_list(self) -> None:
        r = ComponentResult(success=False, errors=["boom", "second"])
        assert _first_error_message(r) == "boom"

    def test_falls_back_to_details_error(self) -> None:
        r = ComponentResult(success=False, details={"error": "from details"})
        assert _first_error_message(r) == "from details"

    def test_falls_back_to_message(self) -> None:
        r = ComponentResult(success=False, message="something happened")
        assert _first_error_message(r) == "something happened"

    def test_returns_empty_string_when_nothing_to_say(self) -> None:
        r = ComponentResult(success=True)
        assert _first_error_message(r) == ""

    def test_skips_empty_first_errors_element_and_falls_through(self) -> None:
        """``errors=[""]`` is non-empty but the first entry is falsy. The
        previous version returned ``""`` short-circuiting the chain. Now we
        skip and fall through to ``details['error']`` / ``message``.
        """
        r = ComponentResult(
            success=False,
            errors=[""],
            details={"error": "real cause"},
        )
        assert _first_error_message(r) == "real cause"

    def test_handles_none_input(self) -> None:
        assert _first_error_message(None) == ""


class TestFormatComponentOutcome:
    def test_clean_success(self) -> None:
        r = ComponentResult(success=True, success_count=10, total_count=10)
        level, msg = _format_component_outcome("priorities", r, 1.23)
        assert level == "success"
        assert "completed successfully" in msg
        assert "10/10" in msg
        assert "1.23" in msg

    def test_partial_success_is_warning_not_success(self) -> None:
        r = ComponentResult(
            success=True,
            success_count=7,
            failed_count=3,
            total_count=10,
            errors=["TK-1 failed validation"],
        )
        level, msg = _format_component_outcome("priorities", r, 0.5)
        assert level == "warning"
        assert "7/10" in msg
        # The failed count is surfaced AND the first cause appears inline.
        assert "3 failed" in msg
        assert "TK-1 failed validation" in msg

    def test_failure_with_explicit_error_includes_cause(self) -> None:
        r = ComponentResult(
            success=False,
            errors=["JiraApiError: Failed to retrieve Tempo customers"],
            details={"status": "failed", "time": 0.0},
        )
        level, msg = _format_component_outcome("companies", r, 0.0)
        assert level == "error"
        assert "FAILED" in msg
        assert "JiraApiError" in msg
        assert "Failed to retrieve Tempo customers" in msg
        # No misleading "completed successfully"
        assert "completed successfully" not in msg

    def test_failure_with_only_message_falls_back(self) -> None:
        r = ComponentResult(
            success=False,
            message="Error during component execution: connection refused",
        )
        level, msg = _format_component_outcome("accounts", r, 2.0)
        assert level == "error"
        assert "connection refused" in msg

    def test_failure_with_details_error_falls_back(self) -> None:
        r = ComponentResult(
            success=False,
            details={"error": "permission denied"},
        )
        level, msg = _format_component_outcome("admin_schemes", r, 0.1)
        assert level == "error"
        assert "permission denied" in msg

    def test_failure_without_any_message_still_marks_failed(self) -> None:
        r = ComponentResult(success=False)
        level, msg = _format_component_outcome("ghost", r, 0.0)
        assert level == "error"
        assert "FAILED" in msg

    def test_elapsed_none_does_not_crash(self) -> None:
        """``details.get('time')`` can return ``None`` for components that
        didn't record duration; ``{None:.2f}`` would raise ``TypeError``.
        """
        r = ComponentResult(success=True, success_count=1, total_count=1)
        level, msg = _format_component_outcome("priorities", r, None)
        assert level == "success"
        assert "0.00 seconds" in msg

    def test_elapsed_zero_is_preserved(self) -> None:
        """A legitimate ``0`` must not be coerced via ``or 0.0`` from a
        falsy-but-valid value (e.g. ``0`` for instant-completion).
        """
        r = ComponentResult(success=True, success_count=1, total_count=1)
        level, msg = _format_component_outcome("priorities", r, 0)
        assert level == "success"
        assert "0.00 seconds" in msg

    def test_partial_success_with_message_fallback(self) -> None:
        """A ``success=True`` result that nonetheless flags an error status
        in ``details`` must emit a warning AND surface the cause when only
        ``message`` is set (no ``errors``/``error``/``details['error']``).
        """
        r = ComponentResult(
            success=True,
            success_count=5,
            failed_count=2,
            total_count=7,
            details={"status": "failed"},
            message="partial failure cause",
        )
        level, msg = _format_component_outcome("priorities", r, 0.5)
        assert level == "warning"
        assert "partial failure cause" in msg


class TestComponentElapsed:
    """The seconds the orchestrator reports for a component.

    This read ``details.get("time", 0)`` and nothing else. Exactly one
    component of forty sets that key, so every other one has always
    reported "took 0.00 seconds" — including a ``workflows`` run whose
    own log timestamps span six.
    """

    def test_falls_back_to_the_measured_wall_clock(self) -> None:
        from src.migration import _component_elapsed

        result = ComponentResult(success=True, details={"created": 234})
        assert _component_elapsed(result, 6.18) == 6.18

    def test_a_component_that_times_itself_still_wins(self) -> None:
        """``time_entries`` measures its own work, excluding its setup."""
        from src.migration import _component_elapsed

        result = ComponentResult(success=True, details={"time": 12.5})
        assert _component_elapsed(result, 30.0) == 12.5

    def test_a_reported_zero_is_kept(self) -> None:
        """A genuine 0.0 is a measurement, not a missing value."""
        from src.migration import _component_elapsed

        result = ComponentResult(success=True, details={"time": 0.0})
        assert _component_elapsed(result, 9.9) == 0.0

    def test_details_may_be_absent_entirely(self) -> None:
        from src.migration import _component_elapsed

        assert _component_elapsed(ComponentResult(success=True), 3.5) == 3.5

    def test_an_unparseable_report_falls_back(self) -> None:
        from src.migration import _component_elapsed

        result = ComponentResult(success=True, details={"time": "a while"})
        assert _component_elapsed(result, 4.25) == 4.25
