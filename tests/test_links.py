from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.jira import render_issue
from tools.links import main, rewrite_dump


class RewriteTests(unittest.TestCase):
    def test_exported_targets_become_local_and_everything_else_stays(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            jira = root / "jira"
            confluence = root / "confluence"
            jira.mkdir()
            confluence.mkdir()
            (jira / "PROJ-1.md").write_text(_PROJ_1, encoding="utf-8")
            (jira / "PROJ-2.md").write_text(_PROJ_2, encoding="utf-8")
            (confluence / "42.md").write_text(_PAGE_42, encoding="utf-8")
            (confluence / "7.md").write_text(_PAGE_7, encoding="utf-8")
            (jira / "PROJ-1").mkdir()
            (jira / "PROJ-1" / "notes.md").write_text(
                "[PROJ-2](https://acme.atlassian.net/browse/PROJ-2)\n",
                encoding="utf-8",
            )

            result = rewrite_dump(jira, confluence)

            self.assertEqual((jira / "PROJ-1.md").read_text(encoding="utf-8"), _PROJ_1_LINKED)
            self.assertEqual((jira / "PROJ-2.md").read_text(encoding="utf-8"), _PROJ_2_LINKED)
            self.assertEqual((confluence / "42.md").read_text(encoding="utf-8"), _PAGE_42_LINKED)
            self.assertEqual((confluence / "7.md").read_text(encoding="utf-8"), _PAGE_7_LINKED)
            self.assertEqual(
                (jira / "PROJ-1" / "notes.md").read_text(encoding="utf-8"),
                "[PROJ-2](https://acme.atlassian.net/browse/PROJ-2)\n",
            )
            self.assertEqual(result.links, 13)
            self.assertEqual(len(result.files), 4)

            again = rewrite_dump(jira, confluence)

            self.assertEqual(again.links, 0)
            self.assertEqual(again.files, [])
            self.assertEqual((jira / "PROJ-1.md").read_text(encoding="utf-8"), _PROJ_1_LINKED)

    def test_rendered_issue_links_point_at_exported_files(self):
        issue = {
            "key": "PROJ-101",
            "fields": {
                "summary": "Login redirect",
                "status": {"name": "In Progress"},
                "assignee": {"displayName": "Ada Lovelace"},
                "description": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "See "},
                                {
                                    "type": "inlineCard",
                                    "attrs": {
                                        "url": "https://acme.atlassian.net/wiki/spaces/TEAM/pages/42/Notes"
                                    },
                                },
                            ],
                        }
                    ],
                },
                "issuelinks": [
                    {
                        "type": {"name": "Blocks", "outward": "blocks", "inward": "is blocked by"},
                        "outwardIssue": {
                            "key": "PROJ-102",
                            "fields": {"summary": "Add tests", "status": {"name": "To Do"}},
                        },
                    },
                    {
                        "type": {"name": "Blocks", "outward": "blocks", "inward": "is blocked by"},
                        "inwardIssue": {
                            "key": "PROJ-100",
                            "fields": {"summary": "Session store", "status": {"name": "Done"}},
                        },
                    },
                ],
            },
        }
        text = render_issue(issue, [], "https://acme.atlassian.net")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            jira = root / "jira"
            confluence = root / "confluence"
            jira.mkdir()
            confluence.mkdir()
            (jira / "PROJ-101.md").write_text(text, encoding="utf-8")
            (jira / "PROJ-102.md").write_text(
                "- **URL:** https://acme.atlassian.net/browse/PROJ-102\n",
                encoding="utf-8",
            )
            (confluence / "42.md").write_text(
                "- **URL:** https://acme.atlassian.net/wiki/spaces/TEAM/pages/42/Notes\n",
                encoding="utf-8",
            )
            rewrite_dump(jira, confluence)
            rewritten = (jira / "PROJ-101.md").read_text(encoding="utf-8")
        self.assertIn("- **URL:** https://acme.atlassian.net/browse/PROJ-101", rewritten)
        self.assertIn("[PROJ-102](PROJ-102.md)", rewritten)
        self.assertIn("[PROJ-100](https://acme.atlassian.net/browse/PROJ-100)", rewritten)
        self.assertIn(
            "[https://acme.atlassian.net/wiki/spaces/TEAM/pages/42/Notes](../confluence/42.md)",
            rewritten,
        )

    def test_indented_code_fence_is_left_alone(self):
        text = """\
- **URL:** https://acme.atlassian.net/browse/PROJ-1

-
  ```
  [PROJ-2](https://acme.atlassian.net/browse/PROJ-2)
  ```

[PROJ-2](https://acme.atlassian.net/browse/PROJ-2)
"""
        expected = """\
- **URL:** https://acme.atlassian.net/browse/PROJ-1

-
  ```
  [PROJ-2](https://acme.atlassian.net/browse/PROJ-2)
  ```

[PROJ-2](PROJ-2.md)
"""
        with TemporaryDirectory() as tmp:
            jira = Path(tmp) / "jira"
            jira.mkdir()
            (jira / "PROJ-1.md").write_text(text, encoding="utf-8")
            (jira / "PROJ-2.md").write_text(
                "- **URL:** https://acme.atlassian.net/browse/PROJ-2\n",
                encoding="utf-8",
            )
            rewrite_dump(jira, Path(tmp) / "confluence")
            self.assertEqual((jira / "PROJ-1.md").read_text(encoding="utf-8"), expected)

    def test_links_without_a_known_site_stay_remote(self):
        with TemporaryDirectory() as tmp:
            jira = Path(tmp) / "jira"
            jira.mkdir()
            original = "[PROJ-2](https://acme.atlassian.net/browse/PROJ-2)\n"
            (jira / "PROJ-1.md").write_text(original, encoding="utf-8")
            (jira / "PROJ-2.md").write_text("# PROJ-2\n", encoding="utf-8")
            result = rewrite_dump(jira, Path(tmp) / "confluence")
            self.assertEqual(result.links, 0)
            self.assertEqual((jira / "PROJ-1.md").read_text(encoding="utf-8"), original)

    def test_missing_directories_rewrite_nothing(self):
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            result = rewrite_dump(missing, missing / "other")
        self.assertEqual(result.links, 0)
        self.assertEqual(result.files, [])

    def test_main_rejects_a_file_path(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "file.md"
            path.write_text("x", encoding="utf-8")
            code = main(["--jira", str(path), "--confluence", str(Path(tmp) / "missing")])
        self.assertEqual(code, 2)


_PROJ_1 = """\
# PROJ-1

- **Status:** Open
- **Assignee:** Ada
- **URL:** https://acme.atlassian.net/browse/PROJ-1

See [the notes](https://acme.atlassian.net/wiki/spaces/TEAM/pages/42/Notes#Section%20one) and [PROJ-2](https://acme.atlassian.net/browse/PROJ-2?focusedCommentId=9).

[PROJ-9](https://acme.atlassian.net/browse/PROJ-9) stays.

[Other](https://other.example/browse/PROJ-2) stays.

![shot](PROJ-1/screen.png)

![remote](https://acme.atlassian.net/browse/PROJ-2)

`[PROJ-2](https://acme.atlassian.net/browse/PROJ-2)`

> ```
> [PROJ-2](https://acme.atlassian.net/browse/PROJ-2)
> ```

> See [PROJ-2](https://acme.atlassian.net/browse/PROJ-2).

Board [card](https://acme.atlassian.net/jira/software/projects/PROJ/issues/PROJ-2).

[selected](https://acme.atlassian.net/jira/software/projects/PROJ/boards/1?selectedIssue=PROJ-2)

[a\\]b](https://acme.atlassian.net/browse/PROJ-2)

[angled](<https://acme.atlassian.net/browse/PROJ-2>)

[draft](https://acme.atlassian.net/wiki/spaces/TEAM/pages/edit-v2/42?draftShareId=abc)
"""

_PROJ_1_LINKED = """\
# PROJ-1

- **Status:** Open
- **Assignee:** Ada
- **URL:** https://acme.atlassian.net/browse/PROJ-1

See [the notes](../confluence/42.md#Section%20one) and [PROJ-2](PROJ-2.md).

[PROJ-9](https://acme.atlassian.net/browse/PROJ-9) stays.

[Other](https://other.example/browse/PROJ-2) stays.

![shot](PROJ-1/screen.png)

![remote](https://acme.atlassian.net/browse/PROJ-2)

`[PROJ-2](https://acme.atlassian.net/browse/PROJ-2)`

> ```
> [PROJ-2](https://acme.atlassian.net/browse/PROJ-2)
> ```

> See [PROJ-2](PROJ-2.md).

Board [card](PROJ-2.md).

[selected](PROJ-2.md)

[a\\]b](PROJ-2.md)

[angled](PROJ-2.md)

[draft](../confluence/42.md)
"""

_PROJ_2 = """\
# PROJ-2

- **URL:** https://acme.atlassian.net/browse/PROJ-2

[page](https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=42)
"""

_PROJ_2_LINKED = """\
# PROJ-2

- **URL:** https://acme.atlassian.net/browse/PROJ-2

[page](../confluence/42.md)
"""

_PAGE_42 = """\
# Notes

- **URL:** https://acme.atlassian.net/wiki/spaces/TEAM/pages/42/Notes
- **Parent:** [Home](7.md)

[PROJ-1](https://acme.atlassian.net/browse/PROJ-1)

[section](https://acme.atlassian.net/wiki/spaces/TEAM/pages/42/Notes#Licensing)

[missing](https://acme.atlassian.net/wiki/spaces/TEAM/pages/99/Nope)

[home draft](https://acme.atlassian.net/wiki/spaces/TEAM/pages/edit-v2/7)

[other draft](https://acme.atlassian.net/wiki/spaces/TEAM/pages/edit-v2/99)
"""

_PAGE_42_LINKED = """\
# Notes

- **URL:** https://acme.atlassian.net/wiki/spaces/TEAM/pages/42/Notes
- **Parent:** [Home](7.md)

[PROJ-1](../jira/PROJ-1.md)

[section](42.md#Licensing)

[missing](https://acme.atlassian.net/wiki/spaces/TEAM/pages/99/Nope)

[home draft](7.md)

[other draft](https://acme.atlassian.net/wiki/spaces/TEAM/pages/edit-v2/99)
"""

_PAGE_7 = """\
# Home

- **URL:** https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=7

- **Parent:** [42](https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=42)
"""

_PAGE_7_LINKED = """\
# Home

- **URL:** https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=7

- **Parent:** [42](42.md)
"""


if __name__ == "__main__":
    unittest.main()
