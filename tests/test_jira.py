from __future__ import annotations

import os
import unittest
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.jira import (
    IssueImages,
    JiraAuthError,
    JiraClient,
    JiraError,
    extract_range,
    format_jira_time,
    main,
    render_issue,
)


def _issue(key: str) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": "Login redirect",
            "status": {"name": "In Progress"},
            "assignee": {"displayName": "Ada Lovelace"},
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Sends people home."}],
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


def _comment(comment_id: str, body: str, created: str, updated: str | None = None) -> dict:
    return {
        "id": comment_id,
        "author": {"displayName": "Mia Krystof"},
        "created": created,
        "updated": updated or created,
        "visibility": {"type": "role", "value": "Administrators"},
        "body": {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}],
        },
    }


class RenderTests(unittest.TestCase):
    def test_issue_markdown_includes_links_and_comments(self):
        comments = [
            _comment("1", "First note", "2021-01-17T12:34:00.000+0000"),
            _comment(
                "2",
                "Edited note",
                "2021-01-18T09:00:00.000+0000",
                "2021-01-18T23:45:00.000+0000",
            ),
        ]
        text = render_issue(_issue("PROJ-101"), comments, "https://acme.atlassian.net")
        self.assertIn("# PROJ-101: Login redirect", text)
        self.assertIn("- **Status:** In Progress", text)
        self.assertIn("- **Assignee:** Ada Lovelace", text)
        self.assertIn("- **URL:** https://acme.atlassian.net/browse/PROJ-101", text)
        self.assertIn("Sends people home.", text)
        self.assertIn(
            "- **blocks** [PROJ-102](https://acme.atlassian.net/browse/PROJ-102): Add tests (To Do)",
            text,
        )
        self.assertIn(
            "- **is blocked by** [PROJ-100](https://acme.atlassian.net/browse/PROJ-100): Session store (Done)",
            text,
        )
        self.assertLess(
            text.index("**blocks**"),
            text.index("**is blocked by**"),
        )
        self.assertIn("### Mia Krystof — 2021-01-17 12:34 +0000 (Administrators)", text)
        self.assertIn("_Updated 2021-01-18 23:45 +0000._", text)
        self.assertNotIn("_Updated 2021-01-17", text)

    def test_missing_assignee_is_unassigned(self):
        issue = _issue("PROJ-101")
        issue["fields"]["assignee"] = None
        text = render_issue(issue, [], "https://acme.atlassian.net")
        self.assertIn("- **Assignee:** Unassigned", text)

    def test_format_jira_time(self):
        self.assertEqual(format_jira_time("2021-01-17T12:34:00.000+0000"), "2021-01-17 12:34 +0000")
        self.assertEqual(format_jira_time("2021-01-17T12:34:00.000Z"), "2021-01-17 12:34 +0000")


class ExtractTests(unittest.TestCase):
    def test_writes_one_file_per_existing_issue_and_pages_comments(self):
        comments = {
            "0": [_comment("1", "First", "2021-01-17T12:34:00.000+0000")],
            "1": [_comment("2", "Second", "2021-01-18T09:00:00.000+0000")],
        }

        def transport(url: str) -> tuple[int, dict | None]:
            parsed = urllib.parse.urlparse(url)
            if parsed.path.endswith("/comment"):
                start = dict(urllib.parse.parse_qsl(parsed.query))["startAt"]
                batch = comments[start]
                return 200, {"startAt": int(start), "maxResults": 1, "total": 2, "comments": batch}
            if parsed.path.endswith("/PROJ-2"):
                return 404, None
            if parsed.path.endswith("/PROJ-1"):
                self.assertIn("updateHistory=false", parsed.query)
                self.assertIn("assignee", parsed.query)
                self.assertIn("attachment", parsed.query)
                return 200, _issue("PROJ-1")
            raise AssertionError(url)

        with TemporaryDirectory() as directory:
            result = extract_range(
                JiraClient("https://acme.atlassian.net", transport),
                "PROJ",
                1,
                2,
                Path(directory),
            )
            self.assertEqual([path.name for path in result.wrote], ["PROJ-1.md"])
            self.assertEqual(result.missing, ["PROJ-2"])
            text = result.wrote[0].read_text(encoding="utf-8")
            self.assertLess(text.index("First"), text.index("Second"))

    def test_auth_failure_stops_the_range(self):
        def transport(url: str) -> tuple[int, dict | None]:
            if "PROJ-1" in url and "/comment" not in url:
                return 200, _issue("PROJ-1")
            if "/comment" in url:
                return 200, {"comments": [], "total": 0, "startAt": 0, "maxResults": 100}
            return 401, {"message": "client must be authenticated"}

        with TemporaryDirectory() as directory:
            with self.assertRaises(JiraAuthError):
                extract_range(
                    JiraClient("https://acme.atlassian.net", transport),
                    "PROJ",
                    1,
                    3,
                    Path(directory),
                )
            self.assertTrue((Path(directory) / "PROJ-1.md").exists())
            self.assertFalse((Path(directory) / "PROJ-3.md").exists())

    def test_http_failure_on_one_issue_continues(self):
        def transport(url: str) -> tuple[int, dict | None]:
            if "PROJ-1" in url:
                return 500, {"errorMessages": ["boom"]}
            if "/comment" in url:
                return 200, {"comments": [], "total": 0, "startAt": 0, "maxResults": 100}
            return 200, _issue("PROJ-2")

        with TemporaryDirectory() as directory:
            result = extract_range(
                JiraClient("https://acme.atlassian.net", transport),
                "PROJ",
                1,
                2,
                Path(directory),
            )
            self.assertEqual(result.failed, [("PROJ-1", "HTTP 500: boom")])
            self.assertEqual([path.name for path in result.wrote], ["PROJ-2.md"])


def _media(alt: str, media_id: str) -> dict:
    return {"type": "media", "attrs": {"id": media_id, "type": "file", "alt": alt}}


def _attachment(attachment_id: str, filename: str, mime_type: str) -> dict:
    return {
        "id": attachment_id,
        "filename": filename,
        "mimeType": mime_type,
        "content": f"https://acme.atlassian.net/rest/api/3/attachment/content/{attachment_id}",
    }


class ImageTests(unittest.TestCase):
    def test_saves_referenced_images_and_skips_other_files(self):
        issue = _issue("PROJ-1")
        issue["fields"]["description"] = {
            "type": "doc",
            "content": [
                _media("shot.png", "media-shot"),
                _media("shot.png", "media-shot"),
                _media("notes.pdf", "media-pdf"),
            ],
        }
        issue["fields"]["attachment"] = [
            _attachment("10", "shot.png", "image/png"),
            _attachment("11", "notes.pdf", "application/pdf"),
            _attachment("12", "comment.png", "image/png"),
        ]
        comment = _comment("1", "See the image", "2021-01-17T12:34:00.000+0000")
        comment["body"] = {"type": "doc", "content": [_media("comment.png", "media-comment")]}
        seen: list[str] = []

        def transport(url: str) -> tuple[int, dict | None]:
            parsed = urllib.parse.urlparse(url)
            if parsed.path.endswith("/comment"):
                return 200, {"startAt": 0, "maxResults": 100, "total": 1, "comments": [comment]}
            return 200, issue

        def download(url: str) -> tuple[int, bytes]:
            seen.append(url)
            if "/content/10?" in url:
                return 200, b"\x89PNG"
            if "/content/12?" in url:
                return 200, b"GIF89a"
            raise AssertionError(url)

        with TemporaryDirectory() as directory:
            result = extract_range(
                JiraClient("https://acme.atlassian.net", transport, download),
                "PROJ",
                1,
                1,
                Path(directory),
            )
            text = result.wrote[0].read_text(encoding="utf-8")
            folder = Path(directory) / "PROJ-1"
            self.assertEqual((folder / "shot.png").read_bytes(), b"\x89PNG")
            self.assertEqual((folder / "comment.png").read_bytes(), b"GIF89a")
            self.assertFalse((folder / "notes.pdf").exists())
            self.assertEqual(seen, [
                "https://acme.atlassian.net/rest/api/3/attachment/content/10?redirect=false",
                "https://acme.atlassian.net/rest/api/3/attachment/content/12?redirect=false",
            ])
            self.assertEqual(text.count("![shot.png](PROJ-1/shot.png)"), 2)
            self.assertIn("[notes.pdf]", text)
            self.assertIn("![comment.png](PROJ-1/comment.png)", text)

    def test_failed_image_download_keeps_the_ticket(self):
        issue = _issue("PROJ-1")
        issue["fields"]["description"] = {"type": "doc", "content": [_media("shot.png", "media-shot")]}
        issue["fields"]["attachment"] = [_attachment("10", "shot.png", "image/png")]

        def transport(url: str) -> tuple[int, dict | None]:
            if urllib.parse.urlparse(url).path.endswith("/comment"):
                return 200, {"startAt": 0, "maxResults": 100, "total": 0, "comments": []}
            return 200, issue

        def download(url: str) -> tuple[int, bytes]:
            return 500, b""

        with TemporaryDirectory() as directory:
            result = extract_range(
                JiraClient("https://acme.atlassian.net", transport, download),
                "PROJ",
                1,
                1,
                Path(directory),
            )
            self.assertEqual(result.failed, [])
            text = result.wrote[0].read_text(encoding="utf-8")
            self.assertIn("[shot.png]", text)
            self.assertFalse((Path(directory) / "PROJ-1").exists())

    def test_auth_failure_on_image_stops_the_range(self):
        issue = _issue("PROJ-1")
        issue["fields"]["description"] = {"type": "doc", "content": [_media("shot.png", "media-shot")]}
        issue["fields"]["attachment"] = [_attachment("10", "shot.png", "image/png")]
        requested: list[str] = []

        def transport(url: str) -> tuple[int, dict | None]:
            parsed = urllib.parse.urlparse(url)
            requested.append(parsed.path)
            if parsed.path.endswith("/comment"):
                return 200, {"startAt": 0, "maxResults": 100, "total": 0, "comments": []}
            return 200, issue

        def download(url: str) -> tuple[int, bytes]:
            return 401, b""

        with TemporaryDirectory() as directory:
            with self.assertRaises(JiraAuthError):
                extract_range(
                    JiraClient("https://acme.atlassian.net", transport, download),
                    "PROJ",
                    1,
                    2,
                    Path(directory),
                )
            self.assertFalse((Path(directory) / "PROJ-1.md").exists())
            self.assertFalse(any(path.endswith("/PROJ-2") for path in requested))

    def test_same_filename_is_reused_for_one_attachment(self):
        downloaded: list[str] = []

        def download(attachment: dict) -> bytes:
            downloaded.append(str(attachment["id"]))
            return b"png"

        with TemporaryDirectory() as directory:
            images = IssueImages(
                attachments=[_attachment("10", "shot.png", "image/png")],
                download=download,
                directory=Path(directory) / "PROJ-1",
                folder_name="PROJ-1",
                issue_key="PROJ-1",
            )
            first = images(_media("shot.png", "one"))
            second = images(_media("shot.png", "two"))
            self.assertEqual(first, "![shot.png](PROJ-1/shot.png)")
            self.assertEqual(second, first)
            self.assertEqual(downloaded, ["10"])
            self.assertIsNone(images(_media("notes.pdf", "pdf")))

    def test_download_error_is_reported_as_jira_error(self):
        client = JiraClient(
            "https://acme.atlassian.net",
            lambda url: (200, {}),
            lambda url: (404, b""),
        )
        with self.assertRaises(JiraError):
            client.download_attachment(_attachment("10", "shot.png", "image/png"))


class MainTests(unittest.TestCase):
    def test_missing_env_returns_2(self):
        keys = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
        saved = {key: os.environ.pop(key, None) for key in keys}
        try:
            code = main(["--project", "PROJ", "--start", "1", "--end", "1"])
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
