"""Attachment references in migrated comments must use the OpenProject API URL.

Reported on ES-4218 / work package 1552: the activity tab linked
``https://<host>/76_renewable_free_end.html``, which 404s, while the Files tab
showed the same attachment working at
``/api/v3/attachments/413/content``. The attachment itself had migrated fine —
``attachment_mapping.json`` held ``ES-4218 → {76_renewable_free_end.html: 413}``
all along.

The cause was the *call*, not the conversion. ``MarkdownConverter.convert``
takes ``jira_key`` and needs it to scope the lookup, because the mapping is keyed
issue → filename → id. Without it ``_convert_attachments`` resolves nothing and
falls back to ``[file](file)`` — a relative link the browser resolves against the
instance root.

This was a regression introduced with ``wp_journal_history``: comments used to be
created by ``work_packages_content``, which passes ``jira_key``, and moved to the
journal rebuild, which did not.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.application.components.work_package_migration import WorkPackageMigration
from src.utils.markdown_converter import MarkdownConverter

# The real case from the report.
JIRA_KEY = "ES-4218"
FILENAME = "76_renewable_free_end.html"
ATTACHMENT_ID = 413
API_URL = f"/api/v3/attachments/{ATTACHMENT_ID}/content"

MAPPING = {JIRA_KEY: {FILENAME: ATTACHMENT_ID}}


class TestConverterNeedsTheIssueKey:
    """Why the argument matters, in both directions."""

    @pytest.fixture
    def converter(self) -> MarkdownConverter:
        return MarkdownConverter(attachment_mapping=MAPPING)

    @pytest.mark.parametrize(
        "markup",
        [
            f"Ver el reporte [^{FILENAME}] adjunto",
            f"Ver [el reporte|{FILENAME}] adjunto",
            f"Captura: !{FILENAME}!",
        ],
    )
    def test_resolves_to_the_api_url_with_the_key(
        self,
        converter: MarkdownConverter,
        markup: str,
    ) -> None:
        assert API_URL in converter.convert(markup, jira_key=JIRA_KEY)

    @pytest.mark.parametrize(
        "markup",
        [
            f"Ver el reporte [^{FILENAME}] adjunto",
            f"Ver [el reporte|{FILENAME}] adjunto",
            f"Captura: !{FILENAME}!",
        ],
    )
    def test_without_the_key_it_emits_the_broken_relative_link(
        self,
        converter: MarkdownConverter,
        markup: str,
    ) -> None:
        """Pinning the failure mode, so the fallback stays recognisable.

        ``[file](file)`` is what produced ``https://<host>/76_renewable_free_end.html``
        in the activity tab.
        """
        converted = converter.convert(markup)

        assert API_URL not in converted
        assert f"({FILENAME})" in converted

    def test_an_unmapped_issue_key_does_not_borrow_another_issue_s_attachment(
        self,
        converter: MarkdownConverter,
    ) -> None:
        """The lookup is scoped per issue, and must stay that way.

        Two Jira issues can attach files with the same name; resolving by
        filename alone would cross-link them.
        """
        converted = converter.convert(f"[^{FILENAME}]", jira_key="ES-9999")

        assert API_URL not in converted


class TestJournalRebuildPassesTheKey:
    """The wiring that was missing — the live path for comments."""

    @pytest.fixture
    def component(self) -> WorkPackageMigration:
        with patch.object(WorkPackageMigration, "__init__", lambda self, **_: None):
            instance = WorkPackageMigration()  # type: ignore[call-arg]
        instance.logger = MagicMock()
        instance.user_mapping = {}
        instance.status_mapping = {}
        instance.issue_type_mapping = {}
        instance.enhanced_audit_trail_migrator = MagicMock()
        instance.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = []
        return instance

    def _issue(self) -> MagicMock:
        issue = MagicMock()
        issue.key = JIRA_KEY
        return issue

    def test_comment_notes_carry_the_api_url_end_to_end(
        self,
        component: WorkPackageMigration,
    ) -> None:
        """A real converter, so this covers the wiring and the conversion together."""
        component.enhanced_audit_trail_migrator.extract_comments_from_issue.return_value = [
            {
                "id": "10001",
                "created": "2026-02-03T17:03:16.000-0300",
                "author": {"name": "melina.rosell"},
                "body": f"Ver el reporte [^{FILENAME}] adjunto",
            },
        ]
        component.markdown_converter = MarkdownConverter(attachment_mapping=MAPPING)

        ops = component._build_rails_ops_for_issue(self._issue(), {"id": 1552})

        # ops[0] is the synthetic creation journal (v1); the comment is the
        # entry after it. A comment is not the creation of the issue, so it no
        # longer gets folded into v1.
        assert len(ops) == 2
        assert ops[0]["version"] == 1
        assert ops[0]["notes"] == ""
        assert API_URL in ops[1]["notes"]
        assert f"({FILENAME})" not in ops[1]["notes"]

    def test_the_key_is_forwarded_to_the_converter(
        self,
        component: WorkPackageMigration,
    ) -> None:
        """Explicit on the contract, so the argument cannot be dropped again."""
        component.enhanced_audit_trail_migrator.extract_comments_from_issue.return_value = [
            {
                "id": "10001",
                "created": "2026-02-03T17:03:16.000-0300",
                "author": {"name": "melina.rosell"},
                "body": "cualquier cosa",
            },
        ]
        component.markdown_converter = MagicMock()
        component.markdown_converter.convert.return_value = "convertido"

        component._build_rails_ops_for_issue(self._issue(), {"id": 1552})

        assert component.markdown_converter.convert.call_args.kwargs["jira_key"] == JIRA_KEY


class TestEveryCallSiteSuppliesTheKey:
    """No conversion in this module may run without the issue context.

    Six of the nine call sites omitted it. Attachment resolution is the reason it
    matters most, but the same argument also scopes work-package cross-links.
    """

    def test_no_bare_convert_calls_remain(self) -> None:
        source = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "application"
            / "components"
            / "work_package_migration.py"
        ).read_text(encoding="utf-8")

        offenders = []
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "markdown_converter.convert(" not in line:
                continue
            # The argument may sit on this line or on one of the next few, for
            # the calls that wrap.
            window = "\n".join(lines[i : i + 4])
            if "jira_key" not in window:
                offenders.append(f"{i + 1}: {line.strip()}")

        assert not offenders, "convert() called without jira_key at:\n" + "\n".join(offenders)


class TestReportedCaseData:
    """The mapping held the answer before any code changed."""

    def test_attachment_mapping_resolves_the_reported_file(self) -> None:
        path = Path(__file__).resolve().parent.parent.parent / "var" / "data" / "attachment_mapping.json"
        if not path.exists():
            pytest.skip("attachment_mapping.json is run output, absent on a clean checkout")

        mapping = json.loads(path.read_text(encoding="utf-8"))

        assert mapping.get(JIRA_KEY, {}).get(FILENAME) == ATTACHMENT_ID
