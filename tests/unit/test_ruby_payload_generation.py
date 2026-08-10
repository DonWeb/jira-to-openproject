r"""Regression tests: JSON must reach Ruby as data, never as source.

Two failure modes, both observed in this project:

**JSON ``null`` is not Ruby.** Inlining ``json.dumps`` output as Ruby source
looks correct — JSON object syntax is a valid Ruby hash literal and
``true``/``false`` spell the same — right up until a ``None`` appears. On
2026-08-06 a checklist-type Jira custom field carrying ``"status": null``
turned a 543 KB batch of 137 work packages into
``NameError: undefined local variable or method 'null'``. The script died
before writing its result file, so the Python side polled a container path for
595 seconds — 38% of that migration run — then retried and succeeded in two.

**JSON escaping is not Ruby escaping.** ``json.dumps`` yields a *double*-quoted
string, and Ruby evaluates ``#{...}`` inside those. JSON has no such construct,
so an interpolation passes through untouched. ``openproject_issue_priority_service``
was hardened against this and recorded why; the sweep that followed the 08-06
run found three siblings still carrying the original pattern.

The safe shapes are: a heredoc plus ``JSON.parse`` for structures, and
``escape_ruby_single_quoted`` for scalars.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.infrastructure.openproject.openproject_custom_field_service import (
    OpenProjectCustomFieldService,
)
from src.infrastructure.openproject.openproject_work_package_service import (
    OpenProjectWorkPackageService,
)


def _client() -> MagicMock:
    client = MagicMock()
    client.logger = MagicMock()
    client.execute_json_query = MagicMock(return_value={"updated": 0, "failed": 0, "results": []})
    return client


def _ruby_code_only(script: str) -> str:
    """Strip heredoc bodies, leaving the parts Ruby actually evaluates.

    A ``null`` inside the JSON payload is data and harmless; one in the
    surrounding source is a bare identifier and fatal. Only the latter matters.
    """
    # The opening tag is followed by the rest of its line (``JSON.parse(<<'TAG')``
    # closes the call there), then the body, then the terminator on its own line.
    return re.sub(r"<<'(\w+)'[^\n]*\n.*?\n\1", "<<HEREDOC_BODY_REMOVED", script, flags=re.DOTALL)


# ── JSON null must never land in Ruby source ─────────────────────────────────


def test_batch_update_survives_none_in_the_payload() -> None:
    """A ``None`` anywhere in an update must not emit a bare ``null`` token.

    Exactly the payload shape that broke the 2026-08-06 run: a custom field
    whose value is a nested structure containing ``None``.
    """
    service = OpenProjectWorkPackageService(_client())

    service.batch_update_work_packages(
        [
            {
                "id": 1437,
                "customField54": [{"name": "check", "checked": False, "status": None}],
                "subject": None,
            },
        ],
    )

    script = service._client.execute_json_query.call_args[0][0]
    assert "JSON.parse(<<'J2O_UPDATES')" in script, "payload must cross as data, not as source"
    assert "null" not in _ruby_code_only(script), "a bare null in Ruby source raises NameError"


def test_batch_update_reads_string_keys() -> None:
    """``JSON.parse`` yields string keys, so the Ruby must not index by symbol.

    Getting this wrong is silent: ``update[:id]`` on a string-keyed hash is
    ``nil``, and ``WorkPackage.find(nil)`` raises for every row.
    """
    service = OpenProjectWorkPackageService(_client())
    service.batch_update_work_packages([{"id": 1, "subject": "x"}])

    script = service._client.execute_json_query.call_args[0][0]
    assert "update['id']" in script
    assert "update[:id]" not in script
    assert "key == 'id'" in script


def test_batch_update_reports_attributes_it_could_not_apply() -> None:
    """Skipping an unknown setter must be visible, not silently counted as updated.

    ``wp.respond_to?`` guards the assignment; without reporting, a row whose
    attributes all bounced still increments ``updated``.
    """
    service = OpenProjectWorkPackageService(_client())
    service.batch_update_work_packages([{"id": 1, "nope": 2}])

    script = service._client.execute_json_query.call_args[0][0]
    assert "unapplied" in script


def test_batch_query_survives_none_among_the_values() -> None:
    """``_build_safe_batch_query`` interpolated its value list as Ruby source."""
    client = OpenProjectClient.__new__(OpenProjectClient)

    query = client._build_safe_batch_query("WorkPackage", "id", [1, None, 3])

    assert "JSON.parse" in query
    assert "null" not in _ruby_code_only(query)


def test_batch_query_still_rejects_injected_field_names() -> None:
    """The existing field-name guard must survive the refactor."""
    client = OpenProjectClient.__new__(OpenProjectClient)

    with pytest.raises(ValueError, match="Illegal field name"):
        client._build_safe_batch_query("WorkPackage", "id); system('x'); (", [1])


# ── Ruby string escaping ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hostile",
    [
        '#{system("rm -rf /")}',
        "#{`whoami`}",
        "#{User.first.destroy}",
    ],
)
def test_remove_custom_field_does_not_interpolate_a_crafted_name(hostile: str) -> None:
    """Custom-field names come from Jira and must not be executable.

    ``json.dumps(name)`` produced a double-quoted Ruby literal, where
    ``#{...}`` runs. Single-quoted literals never interpolate.
    """
    client = _client()
    client.execute_json_query = MagicMock(return_value={"removed": 0})
    service = OpenProjectCustomFieldService(client)

    service.remove_custom_field(hostile)

    ruby = client.execute_json_query.call_args[0][0]
    assert f'"{hostile}"' not in ruby, "a double-quoted literal would let Ruby interpolate this"
    assert "CustomField.where(name: '" in ruby


def test_remove_custom_field_escapes_quotes_in_the_name() -> None:
    """A quote in the name must not break out of the single-quoted literal."""
    client = _client()
    client.execute_json_query = MagicMock(return_value={"removed": 0})
    service = OpenProjectCustomFieldService(client)

    service.remove_custom_field("O'Brien's field")

    ruby = client.execute_json_query.call_args[0][0]
    assert r"O\'Brien\'s field" in ruby
