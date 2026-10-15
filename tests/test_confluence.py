from __future__ import annotations

import json
import os
import unittest
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.confluence import (
    ConfluenceAuthError,
    ConfluenceClient,
    ConfluenceError,
    PageImages,
    _attachment_download_url,
    extract_tree,
    format_confluence_time,
    main,
    parse_args,
    render_page,
)

WIKI = "https://acme.atlassian.net/wiki"


def _adf(text: str) -> dict:
    return {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _page(page_id: str, title: str, parent: str | None, text: str, author: str = "acc-ada") -> dict:
    page = {
        "id": page_id,
        "status": "current",
        "title": title,
        "parentId": parent,
        "authorId": author,
        "version": {
            "number": 3,
            "authorId": author,
            "createdAt": "2021-01-17T12:34:00.000Z",
        },
        "body": {"atlas_doc_format": {"value": json.dumps(_adf(text))}},
        "_links": {"webui": f"/spaces/TEAM/pages/{page_id}/{title}"},
    }
    return page


def _comment(
    comment_id: str,
    text: str,
    author: str,
    created: str,
    resolution: str = "",
    selection: str = "",
) -> dict:
    comment = {
        "id": comment_id,
        "version": {"number": 1, "authorId": author, "createdAt": created},
        "body": {"atlas_doc_format": {"value": _adf(text)}},
    }
    if resolution:
        comment["resolutionStatus"] = resolution
    if selection:
        comment["properties"] = {"inlineOriginalSelection": selection}
    return comment


def _attachment(attachment_id: str, title: str, mime: str, file_id: str) -> dict:
    return {
        "id": attachment_id,
        "title": title,
        "mediaType": mime,
        "fileId": file_id,
        "pageId": "2",
        "downloadLink": "https://evil.example/steal",
    }


def _media(alt: str, file_id: str) -> dict:
    return {"type": "media", "attrs": {"type": "file", "id": file_id, "alt": alt}}


def _search_comment(
    comment_id: str,
    text: str,
    author: str,
    name: str,
    created: str,
    parent: str = "",
    location: str = "footer",
    resolution: str = "",
    selection: str = "",
) -> dict:
    ancestors = [{"id": parent, "type": "comment"}] if parent else []
    extensions: dict = {"location": location}
    if resolution:
        extensions["resolution"] = {"status": resolution}
    if selection:
        extensions["inlineProperties"] = {"originalSelection": selection}
    return {
        "id": comment_id,
        "type": "comment",
        "status": "current",
        "body": {"atlas_doc_format": {"value": _adf(text)}},
        "version": {
            "number": 1,
            "when": created,
            "by": {"accountId": author, "displayName": name},
        },
        "history": {
            "createdDate": created,
            "createdBy": {"accountId": author, "displayName": name},
        },
        "ancestors": ancestors,
        "extensions": extensions,
    }


def _empty(_url: str) -> tuple[int, dict]:
    return 200, {"results": []}


class RenderTests(unittest.TestCase):
    def test_page_markdown_includes_comments_replies_and_inline_selection(self):
        footer = [
            _comment("10", "Hello", "acc-mia", "2021-01-17T12:34:00.000+0000"),
        ]
        footer[0]["replies"] = [
            _comment("11", "A reply", "acc-ada", "2021-01-18T09:00:00.000+0000"),
        ]
        footer[0]["replies"][0]["replies"] = [
            _comment("12", "Nested", "acc-ada", "2021-01-18T10:00:00.000+0000"),
        ]
        inline = [
            _comment(
                "20",
                "Please fix",
                "acc-mia",
                "2021-01-17T13:00:00.000Z",
                "open",
                "Root body",
            ),
            {
                "id": "21",
                "resolutionStatus": "resolved",
                "version": {
                    "authorId": "acc-ada",
                    "createdAt": "2021-01-19T00:00:00.000Z",
                },
                "properties": {
                    "results": [{"key": "inlineOriginalSelection", "value": "old text"}],
                },
                "body": {"atlas_doc_format": {"value": _adf("Done")}},
            },
        ]
        page = _page("1", "Runbook", "9", "Root body")
        text = render_page(
            page,
            footer,
            inline,
            {"acc-ada": "Ada Lovelace", "acc-mia": "Mia Krystof"},
            WIKI,
            {"9": "Operations"},
        )
        self.assertIn("# Runbook", text)
        self.assertIn(f"- **URL:** {WIKI}/spaces/TEAM/pages/1/Runbook", text)
        self.assertIn("- **Parent:** [Operations](9.md)", text)
        self.assertIn("- **Updated:** 2021-01-17 12:34 +0000", text)
        self.assertIn("- **Author:** Ada Lovelace", text)
        self.assertIn("Root body", text)
        self.assertIn("### Mia Krystof — 2021-01-17 12:34 +0000", text)
        self.assertIn("Hello", text)
        self.assertIn("> **Ada Lovelace** — 2021-01-18 09:00 +0000", text)
        self.assertIn("A reply", text)
        self.assertIn("> > **Ada Lovelace** — 2021-01-18 10:00 +0000", text)
        self.assertIn("### Mia Krystof — 2021-01-17 13:00 +0000 (open)", text)
        self.assertIn("> Root body", text)
        self.assertIn("Please fix", text)
        self.assertIn("> old text", text)
        self.assertIn("### Ada Lovelace — 2021-01-19 00:00 +0000 (resolved)", text)
        self.assertIn("Done", text)
        self.assertLess(text.index("## Comments"), text.index("## Inline comments"))
        self.assertLess(text.index("Hello"), text.index("A reply"))

    def test_empty_page_uses_the_id_and_placeholder_sections(self):
        text = render_page(
            {"id": "5", "title": "", "body": {"atlas_doc_format": {"value": ""}}},
            [],
            [],
            {},
            WIKI,
        )
        self.assertIn("# 5", text)
        self.assertIn(f"{WIKI}/pages/viewpage.action?pageId=5", text)
        self.assertIn("- **Author:** Unknown", text)
        self.assertIn("_No body._", text)
        self.assertIn("_No comments._", text)
        self.assertIn("_No inline comments._", text)
        self.assertNotIn("**Parent:**", text)
        self.assertNotIn("**Updated:**", text)

    def test_missing_document_body_raises(self):
        with self.assertRaises(ConfluenceError) as caught:
            render_page({"id": "5", "title": "", "body": {}}, [], [], {}, WIKI)
        self.assertIn("atlas_doc_format", str(caught.exception))

    def test_updated_by_is_shown_when_the_editor_differs(self):
        page = _page("6", "Child", None, "Body", "acc-ada")
        page["version"]["authorId"] = "acc-mia"
        text = render_page(
            page,
            [],
            [],
            {"acc-ada": "Ada Lovelace", "acc-mia": "Mia Krystof"},
            WIKI,
        )
        self.assertIn("- **Author:** Ada Lovelace", text)
        self.assertIn("- **Updated by:** Mia Krystof", text)

    def test_account_id_is_escaped_when_rendered(self):
        page = _page("6", "Child", None, "Body", "acc_ada")
        text = render_page(page, [], [], {}, WIKI)
        self.assertIn(r"- **Author:** acc\_ada", text)

    def test_edited_comment_keeps_the_original_time(self):
        comment = _comment("10", "Hello", "acc-mia", "2021-02-01T00:00:00.000Z")
        comment["version"]["number"] = 2
        comment["createdAt"] = "2021-01-17T12:34:00.000Z"
        comment["createdBy"] = "acc-ada"
        comment["authorName"] = "Ada Lovelace"
        comment["version"]["displayName"] = "Mia Krystof"
        text = render_page(
            _page("1", "Runbook", None, "Body"),
            [comment],
            [],
            {},
            WIKI,
        )
        self.assertIn("### Ada Lovelace — 2021-01-17 12:34 +0000", text)
        self.assertIn("_Updated 2021-02-01 00:00 +0000 by Mia Krystof._", text)
        self.assertNotIn("### Mia Krystof — 2021-02-01", text)

    def test_folder_parent_is_omitted(self):
        page = _page("6", "Child", "1", "Body")
        page["parentType"] = "folder"
        text = render_page(page, [], [], {}, WIKI, {"1": "Notes"})
        self.assertNotIn("**Parent:**", text)

    def test_parent_outside_the_export_links_to_confluence(self):
        page = _page("6", "Child", "1", "Body")
        text = render_page(page, [], [], {}, WIKI, {})
        self.assertIn(f"- **Parent:** [1]({WIKI}/pages/viewpage.action?pageId=1)", text)

    def test_format_confluence_time(self):
        self.assertEqual(format_confluence_time("2021-01-17T12:34:00.000+0000"), "2021-01-17 12:34 +0000")
        self.assertEqual(format_confluence_time("2021-01-17T12:34:00.000Z"), "2021-01-17 12:34 +0000")
        self.assertEqual(format_confluence_time(""), "")


class ImageTests(unittest.TestCase):
    def test_file_id_saves_an_image_and_reuses_the_download(self):
        calls: list[str] = []

        def download(attachment: dict) -> bytes:
            calls.append(str(attachment["id"]))
            return b"png"

        with TemporaryDirectory() as directory:
            images = PageImages(
                [_attachment("50", "shot.png", "image/png", "file-50")],
                download,
                Path(directory),
                "2",
                "2",
            )
            self.assertEqual(images(_media("Diagram", "file-50")), "![Diagram](2/shot.png)")
            inline = {"type": "mediaInline", "attrs": {"type": "file", "id": "file-50", "alt": "Inline"}}
            self.assertEqual(images(inline), "![Inline](2/shot.png)")
            self.assertEqual(images(_media("Other", "file-50")), "![Other](2/shot.png)")
            self.assertEqual((Path(directory) / "shot.png").read_bytes(), b"png")
            self.assertEqual(calls, ["50"])

    def test_duplicate_filenames_are_kept_distinct(self):
        def download(attachment: dict) -> bytes:
            return attachment["id"].encode()

        with TemporaryDirectory() as directory:
            images = PageImages(
                [
                    _attachment("50", "shot.png", "image/png", "file-50"),
                    _attachment("51", "shot.png", "image/png", "file-51"),
                ],
                download,
                Path(directory),
                "2",
                "2",
            )
            self.assertEqual(images(_media("Left", "file-50")), "![Left](2/shot.png)")
            self.assertEqual(images(_media("Right", "file-51")), "![Right](2/shot-51.png)")

    def test_pdf_and_failed_download_are_not_written(self):
        def download(_attachment: dict) -> bytes:
            raise ConfluenceError(404, "attachment shot.png")

        with TemporaryDirectory() as directory:
            images = PageImages(
                [
                    _attachment("50", "shot.png", "image/png", "file-50"),
                    _attachment("60", "notes.pdf", "application/pdf", "file-60"),
                ],
                download,
                Path(directory),
                "2",
                "2",
            )
            self.assertIsNone(images(_media("Diagram", "file-50")))
            self.assertIsNone(images(_media("notes.pdf", "file-60")))
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertTrue(any("shot.png" in failure for failure in images.failures))
            external = {
                "type": "media",
                "attrs": {"type": "external", "url": "https://cdn.example/a.png", "alt": "Remote"},
            }
            self.assertEqual(images(external), "![Remote](https://cdn.example/a.png)")

    def test_auth_failure_while_downloading_an_image_stops_the_export(self):
        def download(_attachment: dict) -> bytes:
            raise ConfluenceAuthError(401, "check JIRA_EMAIL and JIRA_API_TOKEN")

        images = PageImages(
            [_attachment("50", "shot.png", "image/png", "file-50")],
            download,
            Path("unused"),
            "2",
            "2",
        )
        with self.assertRaises(ConfluenceAuthError):
            images(_media("Diagram", "file-50"))


class ExtractTests(unittest.TestCase):
    def test_exports_descendant_pages_comments_and_images(self):
        names = {"acc-ada": "Ada Lovelace", "acc-mia": "Mia Krystof"}
        posted: list[str] = []
        downloads: list[str] = []

        def transport(url: str) -> tuple[int, dict | None]:
            parsed = urllib.parse.urlparse(url)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if path.endswith("/descendants"):
                if "cursor" not in query:
                    return 200, {
                        "results": [
                            {"id": "9", "type": "whiteboard", "status": "current", "title": "Board"},
                        ],
                        "_links": {"next": "/wiki/api/v2/pages/1/descendants?cursor=c2"},
                    }
                return 200, {
                    "results": [
                        {"id": "2", "type": "page", "status": "current", "title": "Child"},
                        {"id": "8", "type": "page", "status": "archived", "title": "Old"},
                    ]
                }
            if path.endswith("/content/search"):
                cql = query.get("cql", [""])[0]
                self.assertNotIn("status", cql)
                if "container = 1" in cql:
                    return 200, {
                        "results": [
                            _search_comment(
                                "10",
                                "Hello",
                                "acc-mia",
                                "Mia Krystof",
                                "2021-01-17T12:34:00.000Z",
                            ),
                            _search_comment(
                                "11",
                                "A reply",
                                "acc-ada",
                                "Ada Lovelace",
                                "2021-01-18T09:00:00.000Z",
                                parent="10",
                            ),
                            _search_comment(
                                "20",
                                "Please fix",
                                "acc-mia",
                                "Mia Krystof",
                                "2021-01-17T13:00:00.000Z",
                                location="inline",
                                resolution="open",
                                selection="Root body",
                            ),
                        ]
                    }
                return 200, {"results": []}
            if path.endswith("/pages/2/attachments"):
                return 200, {"results": [_attachment("50", "shot.png", "image/png", "file-50")]}
            if path.endswith(("/attachments", "/footer-comments", "/inline-comments")):
                return 200, {"results": []}
            if path.endswith("/pages/1"):
                self.assertEqual(query["body-format"], ["atlas_doc_format"])
                return 200, _page("1", "Root", None, "Root body")
            if path.endswith("/pages/2"):
                page = _page("2", "Child", "1", "Child body")
                page["body"]["atlas_doc_format"]["value"] = {
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Child body"}]},
                        {
                            "type": "mediaSingle",
                            "content": [_media("Diagram", "file-50")],
                        },
                    ],
                }
                return 200, page
            raise AssertionError(url)

        def post(url: str, payload: dict) -> tuple[int, dict]:
            self.assertTrue(url.endswith("/wiki/api/v2/users-bulk"))
            posted.extend(payload["accountIds"])
            return 200, {
                "results": [
                    {"accountId": account_id, "displayName": names[account_id]}
                    for account_id in payload["accountIds"]
                    if account_id in names
                ]
            }

        def download(url: str) -> tuple[int, bytes]:
            downloads.append(url)
            return 200, b"png-bytes"

        with TemporaryDirectory() as directory:
            result = extract_tree(
                ConfluenceClient("https://acme.atlassian.net", transport, download, post),
                "1",
                Path(directory),
            )
            self.assertEqual([path.name for path in result.wrote], ["1.md", "2.md"])
            self.assertEqual(result.missing, [])
            self.assertEqual(result.failed, [])
            self.assertEqual(result.skipped, ["9", "8"])
            root = result.wrote[0].read_text(encoding="utf-8")
            child = result.wrote[1].read_text(encoding="utf-8")
            self.assertIn("Root body", root)
            self.assertIn("### Mia Krystof — 2021-01-17 12:34 +0000", root)
            self.assertIn("Hello", root)
            self.assertIn("> **Ada Lovelace** — 2021-01-18 09:00 +0000", root)
            self.assertIn("A reply", root)
            self.assertIn("> Root body", root)
            self.assertIn("(open)", root)
            self.assertIn("Please fix", root)
            self.assertIn("- **Parent:** [Root](1.md)", child)
            self.assertIn("![Diagram](2/shot.png)", child)
            self.assertEqual((Path(directory) / "2" / "shot.png").read_bytes(), b"png-bytes")
            self.assertEqual(
                downloads,
                [f"{WIKI}/rest/api/content/2/child/attachment/50/download"],
            )
            self.assertIn("acc-mia", posted)
            self.assertIn("acc-ada", posted)

    def test_missing_root_does_not_walk_the_tree(self):
        calls: list[str] = []

        def transport(url: str) -> tuple[int, dict | None]:
            calls.append(url)
            return 404, None

        with TemporaryDirectory() as directory:
            result = extract_tree(
                ConfluenceClient("https://acme.atlassian.net", transport),
                "1",
                Path(directory),
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.missing, ["1"])
        self.assertEqual(result.wrote, [])

    def test_descendant_failure_still_writes_the_root(self):
        def transport(url: str) -> tuple[int, dict | None]:
            path = urllib.parse.urlparse(url).path
            if path.endswith("/descendants"):
                return 200, {"results": [{"id": "2", "type": "page", "status": "current"}]}
            if path.endswith("/pages/2"):
                return 500, {"message": "nope"}
            if path.endswith("/pages/1"):
                return 200, _page("1", "Root", None, "Root body")
            return _empty(url)

        with TemporaryDirectory() as directory:
            result = extract_tree(
                ConfluenceClient("https://acme.atlassian.net", transport),
                "1",
                Path(directory),
            )
        self.assertEqual([path.name for path in result.wrote], ["1.md"])
        self.assertEqual(result.failed, [("2", "HTTP 500: nope")])

    def test_descendant_list_error_still_writes_the_root(self):
        def transport(url: str) -> tuple[int, dict | None]:
            path = urllib.parse.urlparse(url).path
            if path.endswith("/descendants"):
                return 500, {"message": "nope"}
            if path.endswith("/pages/1"):
                return 200, _page("1", "Root", None, "Root body")
            return _empty(url)

        with TemporaryDirectory() as directory:
            result = extract_tree(
                ConfluenceClient("https://acme.atlassian.net", transport),
                "1",
                Path(directory),
            )
        self.assertEqual([path.name for path in result.wrote], ["1.md"])
        self.assertEqual(result.failed, [("1", "descendants: HTTP 500: nope")])

    def test_invalid_body_fails_that_page(self):
        def transport(url: str) -> tuple[int, dict | None]:
            path = urllib.parse.urlparse(url).path
            if path.endswith("/descendants"):
                return 200, {"results": []}
            if path.endswith("/pages/1"):
                page = _page("1", "Root", None, "Root body")
                page["body"] = {"atlas_doc_format": {"value": "not-json"}}
                return 200, page
            return _empty(url)

        with TemporaryDirectory() as directory:
            result = extract_tree(
                ConfluenceClient("https://acme.atlassian.net", transport),
                "1",
                Path(directory),
            )
        self.assertEqual(result.wrote, [])
        self.assertIn("not valid JSON", result.failed[0][1])

    def test_comment_failure_still_writes_the_page(self):
        def transport(url: str) -> tuple[int, dict | None]:
            path = urllib.parse.urlparse(url).path
            if path.endswith("/descendants"):
                return 200, {"results": []}
            if path.endswith("/content/search"):
                return 500, {"message": "comments down"}
            if path.endswith("/pages/1"):
                return 200, _page("1", "Root", None, "Root body")
            return _empty(url)

        with TemporaryDirectory() as directory:
            result = extract_tree(
                ConfluenceClient("https://acme.atlassian.net", transport),
                "1",
                Path(directory),
            )
            text = result.wrote[0].read_text(encoding="utf-8")
        self.assertIn("Root body", text)
        self.assertIn("_Could not load comments: HTTP 500: comments down._", text)
        self.assertNotIn("_No comments._", text)
        self.assertEqual(result.failed, [("1", "comments: HTTP 500: comments down")])

    def test_image_download_failure_still_writes_the_page(self):
        def transport(url: str) -> tuple[int, dict | None]:
            path = urllib.parse.urlparse(url).path
            if path.endswith("/descendants"):
                return 200, {"results": []}
            if path.endswith("/attachments"):
                return 200, {"results": [_attachment("50", "shot.png", "image/png", "file-50")]}
            if path.endswith("/pages/1"):
                page = _page("1", "Root", None, "Root body")
                page["body"]["atlas_doc_format"]["value"] = {
                    "type": "doc",
                    "content": [{"type": "mediaSingle", "content": [_media("Diagram", "file-50")]}],
                }
                return 200, page
            return _empty(url)

        def download(url: str) -> tuple[int, bytes]:
            return 404, b""

        with TemporaryDirectory() as directory:
            result = extract_tree(
                ConfluenceClient("https://acme.atlassian.net", transport, download),
                "1",
                Path(directory),
            )
            text = result.wrote[0].read_text(encoding="utf-8")
        self.assertIn("# Root", text)
        self.assertEqual(result.failed, [("1", "image shot.png: HTTP 404: attachment shot.png")])

    def test_failed_parent_is_not_linked_as_a_local_file(self):
        def transport(url: str) -> tuple[int, dict | None]:
            path = urllib.parse.urlparse(url).path
            if path.endswith("/descendants"):
                return 200, {"results": [{"id": "2", "type": "page", "status": "current"}]}
            if path.endswith("/pages/1"):
                page = _page("1", "Root", None, "Root body")
                page["body"] = {"atlas_doc_format": {"value": "not-json"}}
                return 200, page
            if path.endswith("/pages/2"):
                return 200, _page("2", "Child", "1", "Child body")
            return _empty(url)

        with TemporaryDirectory() as directory:
            result = extract_tree(
                ConfluenceClient("https://acme.atlassian.net", transport),
                "1",
                Path(directory),
            )
            child = result.wrote[0].read_text(encoding="utf-8")
        self.assertEqual([path.name for path in result.wrote], ["2.md"])
        self.assertIn(f"- **Parent:** [1]({WIKI}/pages/viewpage.action?pageId=1)", child)
        self.assertNotIn("(1.md)", child)

    def test_include_expands_by_content_id(self):
        def transport(url: str) -> tuple[int, dict | None]:
            path = urllib.parse.urlparse(url).path
            if path.endswith("/descendants"):
                return 200, {"results": []}
            if path.endswith("/pages/1"):
                page = _page("1", "Root", None, "Root body")
                page["body"]["atlas_doc_format"]["value"] = {
                    "type": "doc",
                    "content": [
                        {
                            "type": "extension",
                            "attrs": {
                                "extensionKey": "excerpt-include",
                                "parameters": {"macroParams": {"contentId": {"value": "3"}}},
                            },
                        }
                    ],
                }
                return 200, page
            if path.endswith("/pages/3"):
                return 200, _page("3", "Shared", None, "Included text")
            return _empty(url)

        with TemporaryDirectory() as directory:
            result = extract_tree(
                ConfluenceClient("https://acme.atlassian.net", transport),
                "1",
                Path(directory),
            )
            text = result.wrote[0].read_text(encoding="utf-8")
        self.assertIn("Included text", text)
        self.assertEqual(result.failed, [])

    def test_missing_include_is_a_failure(self):
        def transport(url: str) -> tuple[int, dict | None]:
            path = urllib.parse.urlparse(url).path
            if path.endswith("/descendants"):
                return 200, {"results": []}
            if path.endswith("/pages/1"):
                page = _page("1", "Root", None, "Root body")
                page["body"]["atlas_doc_format"]["value"] = {
                    "type": "doc",
                    "content": [
                        {
                            "type": "extension",
                            "attrs": {
                                "extensionKey": "include",
                                "parameters": {
                                    "macroParams": {
                                        "": {"value": "Missing"},
                                        "space": {"value": "TEAM"},
                                    }
                                },
                            },
                        }
                    ],
                }
                return 200, page
            if path.endswith("/content/search"):
                cql = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("cql", [""])[0]
                if "type = page" in cql:
                    return 200, {"results": []}
            return _empty(url)

        with TemporaryDirectory() as directory:
            result = extract_tree(
                ConfluenceClient("https://acme.atlassian.net", transport),
                "1",
                Path(directory),
            )
            text = result.wrote[0].read_text(encoding="utf-8")
        self.assertIn("**include: Missing, TEAM**", text)
        self.assertTrue(result.failed)
        self.assertIn("include Missing", result.failed[0][1])

    def test_authentication_failure_stops_the_export(self):
        client = ConfluenceClient("https://acme.atlassian.net", lambda url: (401, {"message": "no"}))
        with TemporaryDirectory() as directory, self.assertRaises(ConfluenceAuthError):
            extract_tree(client, "1", Path(directory))


class ClientTests(unittest.TestCase):
    def test_base_url_gains_a_wiki_prefix_once(self):
        seen: list[str] = []

        def transport(url: str) -> tuple[int, dict | None]:
            seen.append(url)
            return 200, {"id": "42", "title": "T"}

        expected = f"{WIKI}/api/v2/pages/42?body-format=atlas_doc_format"
        ConfluenceClient("https://acme.atlassian.net", transport).get_page("42")
        ConfluenceClient("https://acme.atlassian.net/wiki/", transport).get_page("42")
        self.assertEqual(seen, [expected, expected])

    def test_http_base_url_is_rejected(self):
        with self.assertRaises(ConfluenceError):
            ConfluenceClient("http://acme.atlassian.net", lambda url: (200, {}))

    def test_reply_cycle_stops(self):
        def transport(url: str) -> tuple[int, dict | None]:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            self.assertNotIn("status", query["cql"][0])
            return 200, {
                "results": [
                    _search_comment("10", "Root", "acc-ada", "Ada", "2021-01-17T12:34:00.000Z"),
                    _search_comment(
                        "11",
                        "Reply",
                        "acc-ada",
                        "Ada",
                        "2021-01-18T09:00:00.000Z",
                        parent="10",
                    ),
                    {
                        "id": "13",
                        "status": "trashed",
                        "type": "comment",
                        "extensions": {"location": "footer"},
                        "body": {"atlas_doc_format": {"value": _adf("Gone")}},
                    },
                    {
                        "id": "12",
                        "type": "comment",
                        "extensions": {"location": "footer"},
                        "ancestors": [{"id": "12", "type": "comment"}],
                        "body": {"atlas_doc_format": {"value": _adf("Self")}},
                        "version": {
                            "number": 1,
                            "when": "2021-01-18T10:00:00.000Z",
                            "by": {"accountId": "acc-ada", "displayName": "Ada"},
                        },
                        "history": {"createdDate": "2021-01-18T10:00:00.000Z"},
                    },
                ]
            }

        footer, inline = ConfluenceClient("https://acme.atlassian.net", transport).get_page_comments("1")
        self.assertEqual(inline, [])
        self.assertEqual([comment["id"] for comment in footer], ["10", "12"])
        self.assertEqual(footer[0]["replies"][0]["id"], "11")
        self.assertEqual(footer[0]["replies"][0]["replies"], [])
        self.assertEqual(footer[1]["replies"], [])

    def test_pagination_stays_on_the_confluence_host(self):
        def transport(url: str) -> tuple[int, dict | None]:
            return 200, {"results": [], "_links": {"next": "https://evil.example/next"}}

        client = ConfluenceClient("https://acme.atlassian.net", transport)
        with self.assertRaises(ConfluenceError) as caught:
            client.get_descendants("1")
        self.assertIn("Confluence host", str(caught.exception))

    def test_attachment_download_ignores_an_off_host_link(self):
        attachment = _attachment("50", "shot.png", "image/png", "file-50")
        self.assertEqual(
            _attachment_download_url(WIKI, "2", attachment),
            f"{WIKI}/rest/api/content/2/child/attachment/50/download",
        )

        def download(url: str) -> tuple[int, bytes]:
            self.assertNotIn("evil.example", url)
            return 404, b""

        client = ConfluenceClient("https://acme.atlassian.net", lambda url: (200, {}), download)
        with self.assertRaises(ConfluenceError):
            client.download_attachment("2", attachment)

    def test_v1_search_next_joins_the_wiki_root(self):
        seen: list[str] = []

        def transport(url: str) -> tuple[int, dict | None]:
            seen.append(url)
            if "start=2" in url:
                return 200, {"results": []}
            return 200, {"results": [], "_links": {"next": "/rest/api/content/search?start=2"}}

        ConfluenceClient("https://acme.atlassian.net", transport).get_page_comments("1")
        self.assertEqual(seen[1], f"{WIKI}/rest/api/content/search?start=2")


class MainTests(unittest.TestCase):
    def test_page_is_required_and_numeric(self):
        args = parse_args(["--page", " 42 "])
        self.assertEqual(args.page, "42")
        self.assertEqual(args.out, "out/confluence")
        with self.assertRaises(SystemExit):
            parse_args(["--page", "PROJ"])

    def test_missing_env_returns_2(self):
        keys = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
        saved = {key: os.environ.pop(key, None) for key in keys}
        try:
            code = main(["--page", "1"])
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
        self.assertEqual(code, 2)

    def test_page_id_must_be_numeric_for_extract(self):
        client = ConfluenceClient("https://acme.atlassian.net", lambda url: (200, {}))
        with TemporaryDirectory() as directory, self.assertRaises(ConfluenceError):
            extract_tree(client, "abc", Path(directory))


if __name__ == "__main__":
    unittest.main()
