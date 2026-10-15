"""Extract a Confluence Cloud page tree to markdown files."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from tools.adf import adf_to_markdown
from tools.atlassian_http import (
    TransportError,
    bytes_download,
    json_post,
    json_transport,
)

Transport = Callable[[str], Tuple[int, Optional[dict]]]
Post = Callable[[str, dict], Tuple[int, Optional[dict]]]
Download = Callable[[str], Tuple[int, bytes]]
_PAGE_ID = re.compile(r"^[0-9]+$")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
_NON_IMAGE_SUFFIXES = {
    ".pdf",
    ".zip",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".csv",
    ".json",
    ".xml",
}
_TIME = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2})"
    r".*?(?P<offset>Z|[+-]\d{2}:?\d{2})?$"
)

class ConfluenceError(TransportError):
    pass


class ConfluenceAuthError(ConfluenceError):
    pass


@dataclass
class ExtractResult:
    wrote: list[Path] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class ConfluenceClient:
    def __init__(
        self,
        base_url: str,
        transport: Transport,
        download: Download | None = None,
        post: Post | None = None,
    ):
        if not base_url.startswith("https://"):
            raise ConfluenceError(0, "JIRA_BASE_URL must start with https://")
        self.site_root, self.wiki_root = _roots(base_url.strip())
        self._transport = transport
        self._download = download
        self._post_transport = post

    def get_page(self, page_id: str) -> dict | None:
        url = self._v2(f"pages/{_quote(page_id)}", _body_query())
        return self._get(url)

    def get_descendants(self, page_id: str) -> list[dict]:
        url = self._v2(f"pages/{_quote(page_id)}/descendants", [("limit", "250")])
        return self._paginate(url, f"descendants of {page_id}")

    def get_page_comments(self, page_id: str) -> tuple[list[dict], list[dict]]:
        cql = (
            f"container = {page_id} AND type = comment AND status = current "
            "ORDER BY created ASC"
        )
        query = urllib.parse.urlencode(
            {
                "cql": cql,
                "limit": "100",
                "expand": (
                    "body.atlas_doc_format,version,history,ancestors,"
                    "extensions.inlineProperties,extensions.resolution"
                ),
            }
        )
        url = f"{self.wiki_root}/rest/api/content/search?{query}"
        return _comment_threads(self._paginate(url, f"comments for {page_id}"))

    def find_page(self, title: str, space: str = "") -> str | None:
        cql = f'type = page AND title = "{_cql_quote(title)}"'
        if space:
            cql += f' AND space = "{_cql_quote(space)}"'
        query = urllib.parse.urlencode({"cql": cql, "limit": "2"})
        page = self._get(f"{self.wiki_root}/rest/api/content/search?{query}")
        if page is None:
            raise ConfluenceError(404, f"page titled {title} was not found")
        results = page.get("results")
        if not isinstance(results, list):
            raise ConfluenceError(0, f"unexpected search payload for {title}")
        matches = [item for item in results if isinstance(item, dict)]
        if len(matches) != 1:
            return None
        page_id = str(matches[0].get("id") or "")
        if not _PAGE_ID.match(page_id):
            return None
        return page_id

    def get_attachments(self, page_id: str) -> list[dict]:
        url = self._v2(
            f"pages/{_quote(page_id)}/attachments",
            [("status", "current"), ("limit", "250")],
        )
        return self._paginate(url, f"attachments for {page_id}")

    def display_names(self, account_ids: list[str]) -> dict[str, str]:
        unique = list(dict.fromkeys(account_id for account_id in account_ids if account_id))
        if not unique or self._post_transport is None:
            return {}
        names: dict[str, str] = {}
        for start in range(0, len(unique), 100):
            page = self._post(self._v2("users-bulk"), {"accountIds": unique[start:start + 100]})
            seen_urls: set[str] = set()
            while True:
                batch = page.get("results")
                if not isinstance(batch, list):
                    raise ConfluenceError(0, "unexpected user lookup payload")
                for user in batch:
                    if not isinstance(user, dict) or not user.get("accountId"):
                        continue
                    name = " ".join(str(user.get("displayName") or "").split())
                    if name:
                        names[str(user["accountId"])] = name
                nxt = self._next_link(page)
                if not nxt or nxt in seen_urls:
                    break
                seen_urls.add(nxt)
                fetched = self._get(nxt)
                if fetched is None:
                    raise ConfluenceError(404, "user lookup was not found")
                page = fetched
        return names

    def download_attachment(self, page_id: str, attachment: dict) -> bytes:
        if self._download is None:
            raise ConfluenceError(0, "attachment download is not configured")
        url = _attachment_download_url(self.wiki_root, page_id, attachment)
        status, body = self._download(url)
        if status == 401:
            raise ConfluenceAuthError(status, "check JIRA_EMAIL and JIRA_API_TOKEN")
        filename = attachment.get("title") or attachment.get("id")
        if status != 200:
            raise ConfluenceError(status, f"attachment {filename}")
        if not body:
            raise ConfluenceError(status, f"attachment {filename} was empty")
        return body

    def _paginate(self, url: str, label: str) -> list[dict]:
        items: list[dict] = []
        seen: set[str] = set()
        while url:
            if url in seen:
                raise ConfluenceError(0, f"pagination repeated a page for {label}")
            seen.add(url)
            page = self._get(url)
            if page is None:
                raise ConfluenceError(404, f"{label} were not found")
            batch = page.get("results")
            if not isinstance(batch, list):
                raise ConfluenceError(0, f"unexpected payload for {label}")
            items.extend(item for item in batch if isinstance(item, dict))
            url = self._next_link(page) or ""
        return items

    def _next_link(self, page: dict) -> str | None:
        links = page.get("_links") if isinstance(page.get("_links"), dict) else {}
        nxt = links.get("next") if isinstance(links, dict) else None
        if not isinstance(nxt, str) or not nxt.strip():
            return None
        return self._absolute(nxt.strip())

    def _absolute(self, url: str) -> str:
        if url.startswith("//"):
            raise ConfluenceError(0, "unexpected next page link")
        if url.startswith("https://"):
            if not _same_host(self.site_root, url):
                raise ConfluenceError(0, "next page was not on the Confluence host")
            return url
        if url.startswith(("/rest/", "/download/")):
            return self.wiki_root + url
        if url.startswith("/"):
            return self.site_root + url
        raise ConfluenceError(0, "unexpected next page link")

    def _v2(self, path: str, query: list[tuple[str, str]] | None = None) -> str:
        url = f"{self.wiki_root}/api/v2/{path.lstrip('/')}"
        if query:
            return f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def _get(self, url: str) -> dict | None:
        status, payload = self._transport(url)
        return self._interpret(status, payload)

    def _post(self, url: str, body: dict) -> dict:
        if self._post_transport is None:
            raise ConfluenceError(0, "user lookup is not configured")
        status, payload = self._post_transport(url, body)
        page = self._interpret(status, payload)
        if page is None:
            raise ConfluenceError(404, "user lookup was not found")
        return page

    def _interpret(self, status: int, payload: dict | None) -> dict | None:
        if status == 404:
            return None
        if status == 401:
            raise ConfluenceAuthError(status, "check JIRA_EMAIL and JIRA_API_TOKEN")
        if status != 200 or not isinstance(payload, dict):
            detail = _error_detail(payload) if isinstance(payload, dict) else ""
            raise ConfluenceError(status, detail)
        return payload


def extract_tree(client: ConfluenceClient, page_id: str, out_dir: Path) -> ExtractResult:
    if not _PAGE_ID.match(page_id):
        raise ConfluenceError(0, "PAGE must be a Confluence page id")
    result = ExtractResult()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"fetching page {page_id} and its descendants", file=sys.stderr)
    try:
        root = client.get_page(page_id)
    except ConfluenceAuthError:
        raise
    except ConfluenceError as error:
        result.failed.append((page_id, str(error)))
        _summarize(result)
        return result
    if root is None:
        result.missing.append(page_id)
        print(f"missing {page_id}", file=sys.stderr)
        _summarize(result)
        return result

    pages = [root]
    seen = {_page_id(root)}
    try:
        descendants = client.get_descendants(page_id)
    except ConfluenceAuthError:
        raise
    except ConfluenceError as error:
        result.failed.append((page_id, f"descendants: {error}"))
        print(f"failed {page_id} descendants: {error}", file=sys.stderr)
        descendants = []

    for item in descendants:
        item_id = str(item.get("id") or "")
        if not item_id:
            result.failed.append(("?", "descendant was missing an id"))
            print("failed ?: descendant was missing an id", file=sys.stderr)
            continue
        if item_id in seen:
            continue
        if not _PAGE_ID.match(item_id):
            result.failed.append((item_id, "unexpected page id"))
            print(f"failed {item_id}: unexpected page id", file=sys.stderr)
            continue
        kind = str(item.get("type") or "").lower()
        status = str(item.get("status") or "current").lower()
        if kind != "page" or status != "current":
            result.skipped.append(item_id)
            label = kind if kind != "page" else status
            print(f"skipped {label or 'content'} {item_id}", file=sys.stderr)
            continue
        seen.add(item_id)
        try:
            page = client.get_page(item_id)
        except ConfluenceAuthError:
            raise
        except ConfluenceError as error:
            result.failed.append((item_id, str(error)))
            print(f"failed {item_id}: {error}", file=sys.stderr)
            continue
        if page is None:
            result.missing.append(item_id)
            print(f"missing {item_id}", file=sys.stderr)
            continue
        pages.append(page)

    ready: list[_Prepared] = []
    for page in pages:
        current_id = _page_id(page)
        try:
            ready.append(_prepare_page(client, page, out_dir, result))
        except ConfluenceAuthError:
            raise
        except ConfluenceError as error:
            result.failed.append((current_id, str(error)))
            print(f"failed {current_id}: {error}", file=sys.stderr)
    titles = {item.page_id: item.title for item in ready}
    for item in ready:
        _write_prepared(client, item, titles, out_dir, result)
    _summarize(result)
    return result


@dataclass
class _Prepared:
    page: dict
    page_id: str
    title: str
    footer: list[dict]
    inline: list[dict]
    attachments: list[dict]
    names: dict[str, str]
    notes: list[str]
    comment_error: str
    inline_error: str


def _prepare_page(
    client: ConfluenceClient,
    page: dict,
    out_dir: Path,
    result: ExtractResult,
) -> _Prepared:
    page_id = _page_id(page)
    _document_body(page.get("body"), f"page {page_id}")
    footer: list[dict] = []
    inline: list[dict] = []
    attachments: list[dict] = []
    names: dict[str, str] = {}
    notes: list[str] = []
    comment_error = ""
    inline_error = ""
    try:
        footer, inline = client.get_page_comments(page_id)
    except ConfluenceAuthError:
        raise
    except ConfluenceError as error:
        comment_error = str(error)
        inline_error = str(error)
        result.failed.append((page_id, f"comments: {error}"))
        print(f"failed {page_id} comments: {error}", file=sys.stderr)
    try:
        attachments = client.get_attachments(page_id)
    except ConfluenceAuthError:
        raise
    except ConfluenceError as error:
        notes.append(f"_Could not load attachments: {error}._")
        result.failed.append((page_id, f"attachments: {error}"))
        print(f"failed {page_id} attachments: {error}", file=sys.stderr)
    try:
        names = client.display_names(_account_ids(page, [footer, inline]))
    except ConfluenceAuthError:
        raise
    except ConfluenceError as error:
        notes.append(f"_Could not load account names: {error}._")
        result.failed.append((page_id, f"names: {error}"))
        print(f"failed {page_id} names: {error}", file=sys.stderr)
    return _Prepared(
        page=page,
        page_id=page_id,
        title=str(page.get("title") or ""),
        footer=footer,
        inline=inline,
        attachments=attachments,
        names=names,
        notes=notes,
        comment_error=comment_error,
        inline_error=inline_error,
    )


def _write_prepared(
    client: ConfluenceClient,
    item: _Prepared,
    titles: dict[str, str],
    out_dir: Path,
    result: ExtractResult,
) -> None:
    page_id = item.page_id
    path = out_dir / f"{page_id}.md"
    images = PageImages(
        attachments=item.attachments,
        download=lambda attachment: client.download_attachment(page_id, attachment),
        directory=out_dir / page_id,
        folder_name=page_id,
        page_id=page_id,
    )
    seen = {page_id}
    include_problems: list[str] = []

    def include(node: dict) -> str | None:
        return _expand_include(client, node, seen, images, include, include_problems)

    path.write_text(
        render_page(
            item.page,
            item.footer,
            item.inline,
            item.names,
            client.wiki_root,
            titles,
            images,
            include,
            item.notes,
            item.comment_error,
            item.inline_error,
        ),
        encoding="utf-8",
    )
    for failure in images.failures:
        result.failed.append((page_id, failure))
    for problem in include_problems:
        result.failed.append((page_id, problem))
        print(f"failed {page_id}: {problem}", file=sys.stderr)
    result.wrote.append(path)
    print(f"wrote {path}", file=sys.stderr)


def render_page(
    page: dict,
    footer: list[dict],
    inline: list[dict],
    names: dict[str, str],
    wiki_root: str,
    titles: dict[str, str] | None = None,
    images: Callable[[dict], str | None] | None = None,
    include: Callable[[dict], str | None] | None = None,
    notes: list[str] | None = None,
    comment_error: str = "",
    inline_error: str = "",
) -> str:
    page_id = _page_id(page)
    title_text = _single_line(str(page.get("title") or ""))
    title = f"# {title_text}" if title_text else f"# {page_id}"
    lines = [f"- **URL:** {_page_url(page, wiki_root, page_id)}"]
    parent = _parent_line(page, titles or {}, wiki_root)
    if parent:
        lines.append(parent)
    updated = _version_time(page)
    if updated:
        lines.append(f"- **Updated:** {updated}")
    lines.append(f"- **Author:** {_page_author(page, names)}")
    editor = _updated_by(page, names)
    if editor:
        lines.append(f"- **Updated by:** {editor}")
    lines.extend(notes or [])
    body = adf_to_markdown(
        _document_body(page.get("body"), f"page {page_id}"),
        images,
        include,
    ).strip()
    footer_block = _comment_section(footer, names, images, comment_error, "comments")
    inline_block = _comment_section(inline, names, images, inline_error, "inline comments")
    meta = "\n".join(lines)
    return (
        f"{title}\n\n"
        f"{meta}\n\n"
        f"{body or '_No body._'}\n\n"
        f"## Comments\n\n"
        f"{footer_block}\n\n"
        f"## Inline comments\n\n"
        f"{inline_block}\n"
    )


def _comment_section(
    comments: list[dict],
    names: dict[str, str],
    images: Callable[[dict], str | None] | None,
    error: str,
    label: str,
) -> str:
    if error:
        return f"_Could not load {label}: {error}._"
    return _render_thread(comments, names, images, 3) or f"_No {label}._"


def _render_thread(
    comments: list,
    names: dict[str, str],
    images: Callable[[dict], str | None] | None,
    level: int,
    seen: set[str] | None = None,
) -> str:
    blocks: list[str] = []
    visited = seen if seen is not None else set()
    for comment in comments:
        if isinstance(comment, dict):
            blocks.append(_render_comment(comment, names, images, level, visited))
    return "\n\n".join(block for block in blocks if block)


def _render_comment(
    comment: dict,
    names: dict[str, str],
    images: Callable[[dict], str | None] | None,
    level: int,
    seen: set[str],
) -> str:
    comment_id = str(comment.get("id") or "")
    if comment_id and comment_id in seen:
        return ""
    if comment_id:
        seen.add(comment_id)
    author, created, updated = _comment_times(comment, names)
    resolution = _single_line(str(comment.get("resolutionStatus") or ""))
    if level <= 3:
        heading = f"### {author}"
        if created:
            heading = f"{heading} — {created}"
        if resolution:
            heading = f"{heading} ({resolution})"
        parts = [heading]
    else:
        heading = f"**{author}**"
        if created:
            heading = f"{heading} — {created}"
        if resolution:
            heading = f"{heading} ({resolution})"
        parts = [heading]
    if updated:
        parts.append(updated)
    selection = _selection(comment)
    if selection:
        parts.append(_blockquote(selection))
    body = adf_to_markdown(_adf_value(comment.get("body"), f"comment {comment_id}"), images).strip()
    if body:
        parts.append(body)
    replies = _render_thread(comment.get("replies") or [], names, images, level + 1, seen)
    if replies:
        parts.append(replies)
    rendered = "\n\n".join(parts)
    if level > 3:
        rendered = "\n".join(f"> {line}" if line else ">" for line in rendered.splitlines())
    return rendered


def format_confluence_time(value: str) -> str:
    match = _TIME.match(value.strip())
    if not match:
        return ""
    offset = match.group("offset") or ""
    if offset == "Z":
        offset = "+0000"
    offset = offset.replace(":", "")
    if offset:
        return f"{match.group('date')} {match.group('time')} {offset}"
    return f"{match.group('date')} {match.group('time')}"


class PageImages:
    def __init__(
        self,
        attachments: object,
        download: Callable[[dict], bytes],
        directory: Path,
        folder_name: str,
        page_id: str,
    ):
        self._download = download
        self._directory = directory
        self._folder_name = folder_name
        self._page_id = page_id
        self._images = [item for item in attachments or [] if isinstance(item, dict) and _is_image(item)]
        self._by_file_id = {
            str(item.get("fileId")): item for item in self._images if item.get("fileId")
        }
        self._by_id = {
            str(item.get("id")): item for item in self._images if item.get("id") is not None
        }
        self._by_name: dict[str, list[dict]] = {}
        for item in self._images:
            name = _filename_key(str(item.get("title") or ""))
            if name:
                self._by_name.setdefault(name, []).append(item)
        self._saved: dict[str, str] = {}
        self._used_names: set[str] = set()
        self._warned: set[int] = set()
        self.failures: list[str] = []

    def add_attachments(self, attachments: object) -> None:
        for item in attachments or []:
            if not isinstance(item, dict) or not _is_image(item):
                continue
            self._images.append(item)
            file_id = str(item.get("fileId") or "")
            if file_id and file_id not in self._by_file_id:
                self._by_file_id[file_id] = item
            attachment_id = item.get("id")
            if attachment_id is not None and str(attachment_id) not in self._by_id:
                self._by_id[str(attachment_id)] = item
            name = _filename_key(str(item.get("title") or ""))
            if name:
                self._by_name.setdefault(name, []).append(item)

    def __call__(self, node: dict) -> str | None:
        if node.get("type") not in ("media", "mediaInline"):
            return None
        attrs = node.get("attrs") or {}
        if str(attrs.get("type") or "") == "external":
            return _external_image(attrs)
        attachment = self._match(node)
        if attachment is None:
            if _is_embedded_image(node):
                self._warn_unmatched(node)
            return None
        relative = self._save(attachment)
        if not relative:
            return None
        alt = str(attrs.get("alt") or attachment.get("title") or "image")
        label = _single_line(alt) or "image"
        return f"![{label}]({relative})"

    def _match(self, node: dict) -> dict | None:
        attrs = node.get("attrs") or {}
        media_id = str(attrs.get("id") or "")
        if media_id and media_id in self._by_file_id:
            return self._by_file_id[media_id]
        if media_id and media_id in self._by_id:
            return self._by_id[media_id]
        name = _filename_key(str(attrs.get("alt") or ""))
        matches = self._by_name.get(name) or []
        if len(matches) == 1:
            return matches[0]
        return None

    def _warn_unmatched(self, node: dict) -> None:
        if id(node) in self._warned:
            return
        self._warned.add(id(node))
        attrs = node.get("attrs") or {}
        name = attrs.get("alt") or attrs.get("id") or "image"
        print(f"failed image {self._page_id} {name}: no matching attachment", file=sys.stderr)
        self.failures.append(f"image {name}: no matching attachment")

    def _save(self, attachment: dict) -> str | None:
        attachment_id = str(attachment.get("id"))
        saved = self._saved.get(attachment_id)
        if saved:
            return saved
        filename = str(attachment.get("title") or attachment_id)
        try:
            data = self._download(attachment)
        except ConfluenceAuthError:
            raise
        except ConfluenceError as error:
            print(f"failed image {self._page_id} {filename}: {error}", file=sys.stderr)
            self.failures.append(f"image {filename}: {error}")
            return None
        safe_name = self._unique_filename(filename, attachment_id)
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / safe_name
        path.write_bytes(data)
        relative = f"{self._folder_name}/{safe_name}"
        self._saved[attachment_id] = relative
        print(f"saved {path}", file=sys.stderr)
        return relative

    def _unique_filename(self, raw_name: str, attachment_id: str) -> str:
        cleaned = _safe_filename(raw_name) or "image"
        if cleaned not in self._used_names:
            self._used_names.add(cleaned)
            return cleaned
        stem = Path(cleaned).stem or "image"
        suffix = Path(cleaned).suffix
        candidate = f"{stem}-{_safe_filename(attachment_id) or 'file'}{suffix}"
        if candidate not in self._used_names:
            self._used_names.add(candidate)
            return candidate
        number = 2
        while True:
            candidate = f"{stem}-{number}{suffix}"
            if candidate not in self._used_names:
                self._used_names.add(candidate)
                return candidate
            number += 1


def _external_image(attrs: dict) -> str | None:
    url = str(attrs.get("url") or "")
    if not url:
        return None
    alt = _single_line(str(attrs.get("alt") or "image")) or "image"
    safe = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"![{alt}]({safe})"


def _is_embedded_image(node: dict) -> bool:
    attrs = node.get("attrs") or {}
    name = str(attrs.get("alt") or "")
    suffix = Path(name).suffix.lower()
    if suffix in _NON_IMAGE_SUFFIXES:
        return False
    return suffix in _IMAGE_SUFFIXES or not suffix


def _is_image(attachment: dict) -> bool:
    mime = str(attachment.get("mediaType") or "").split(";", 1)[0].strip().lower()
    if mime.startswith("image/"):
        return True
    return Path(str(attachment.get("title") or "")).suffix.lower() in _IMAGE_SUFFIXES


def _attachment_download_url(wiki_root: str, page_id: str, attachment: dict) -> str:
    content_id = str(attachment.get("pageId") or page_id)
    attachment_id = str(attachment.get("id") or "")
    if not _PAGE_ID.match(content_id) or not attachment_id:
        raise ConfluenceError(0, "attachment is missing an id")
    return (
        f"{wiki_root}/rest/api/content/{_quote(content_id)}"
        f"/child/attachment/{_quote(attachment_id)}/download"
    )


def _account_ids(page: dict, threads: list[list[dict]]) -> list[str]:
    version = page.get("version") if isinstance(page.get("version"), dict) else {}
    ids = [str(account_id) for account_id in (page.get("authorId"), version.get("authorId")) if account_id]
    for thread in threads:
        ids.extend(_comment_account_ids(thread))
    return ids


def _comment_account_ids(comments: list) -> list[str]:
    ids: list[str] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        version = comment.get("version") if isinstance(comment.get("version"), dict) else {}
        for account_id in (comment.get("createdBy"), version.get("authorId")):
            if account_id:
                ids.append(str(account_id))
        replies = comment.get("replies") or []
        if isinstance(replies, list):
            ids.extend(_comment_account_ids(replies))
    return ids


def _page_author(page: dict, names: dict[str, str]) -> str:
    account_id = str(page.get("authorId") or "")
    if not account_id:
        version = page.get("version") if isinstance(page.get("version"), dict) else {}
        account_id = str(version.get("authorId") or "")
    if not account_id:
        return "Unknown"
    return _single_line(names.get(account_id) or account_id)


def _updated_by(page: dict, names: dict[str, str]) -> str:
    version = page.get("version") if isinstance(page.get("version"), dict) else {}
    editor = str(version.get("authorId") or "")
    creator = str(page.get("authorId") or "")
    if not editor or not creator or editor == creator:
        return ""
    return _single_line(names.get(editor) or editor)


def _comment_times(comment: dict, names: dict[str, str]) -> tuple[str, str, str]:
    version = comment.get("version") if isinstance(comment.get("version"), dict) else {}
    try:
        number = int(version.get("number") or 1)
    except (TypeError, ValueError):
        number = 1
    created_at = format_confluence_time(str(comment.get("createdAt") or ""))
    version_at = format_confluence_time(str(version.get("createdAt") or ""))
    creator_id = str(comment.get("createdBy") or "")
    editor_id = str(version.get("authorId") or "")
    if creator_id or created_at:
        author = _named(creator_id, str(comment.get("authorName") or ""), names)
        created = created_at or (version_at if number <= 1 else "")
    else:
        author = _named(editor_id, str(version.get("displayName") or ""), names)
        created = version_at
    updated = ""
    if number > 1 and version_at:
        editor = _named(editor_id, str(version.get("displayName") or ""), names)
        updated = f"_Updated {version_at} by {editor}._"
    return author, created, updated


def _named(account_id: str, display: str, names: dict[str, str]) -> str:
    if display:
        return _single_line(display)
    if account_id and account_id in names:
        return _single_line(names[account_id])
    if account_id:
        return _single_line(account_id)
    return "Unknown"


def _version_time(content: dict) -> str:
    version = content.get("version") if isinstance(content.get("version"), dict) else {}
    return format_confluence_time(str(version.get("createdAt") or ""))


def _page_url(page: dict, wiki_root: str, page_id: str) -> str:
    links = page.get("_links") if isinstance(page.get("_links"), dict) else {}
    webui = str(links.get("webui") or "") if isinstance(links, dict) else ""
    if webui.startswith("/") and not webui.startswith("//"):
        return f"{wiki_root}{webui}"
    return f"{wiki_root}/pages/viewpage.action?pageId={_quote(page_id)}"


def _parent_line(page: dict, titles: dict[str, str], wiki_root: str) -> str:
    parent_id = str(page.get("parentId") or "")
    parent_type = str(page.get("parentType") or "page").lower()
    if parent_type != "page" or not _PAGE_ID.match(parent_id):
        return ""
    title = titles.get(parent_id)
    if title is not None:
        label = _single_line(title) or parent_id
        return f"- **Parent:** [{label}]({parent_id}.md)"
    url = f"{wiki_root}/pages/viewpage.action?pageId={_quote(parent_id)}"
    return f"- **Parent:** [{parent_id}]({url})"


def _selection(comment: dict) -> str:
    props = comment.get("properties")
    if not isinstance(props, dict):
        return ""
    direct = props.get("inlineOriginalSelection")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    results = props.get("results")
    if not isinstance(results, list):
        return ""
    for item in results:
        if not isinstance(item, dict) or item.get("key") != "inlineOriginalSelection":
            continue
        value = item.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _blockquote(value: str) -> str:
    return "\n".join(f"> {_single_line(line)}" if line else ">" for line in value.splitlines())


def _document_body(body: object, label: str) -> object:
    if not isinstance(body, dict) or "atlas_doc_format" not in body:
        raise ConfluenceError(0, f"{label} body was missing atlas_doc_format")
    return _adf_value(body, label)


def _adf_value(body: object, label: str) -> object:
    if not isinstance(body, dict):
        return None
    atlas = body.get("atlas_doc_format")
    if isinstance(atlas, str):
        return _parse_adf(atlas, label) if atlas.strip() else None
    if not isinstance(atlas, dict):
        return None
    value = atlas.get("value")
    if isinstance(value, str):
        return _parse_adf(value, label) if value.strip() else None
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfluenceError(0, f"{label} body was not a document")
    return value


def _parse_adf(value: str, label: str) -> dict:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ConfluenceError(0, f"{label} body was not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ConfluenceError(0, f"{label} body was not a document")
    return parsed


def _body_query() -> list[tuple[str, str]]:
    return [("body-format", "atlas_doc_format")]


def _page_id(page: dict) -> str:
    page_id = str(page.get("id") or "")
    if not _PAGE_ID.match(page_id):
        raise ConfluenceError(0, f"unexpected page id {page_id}")
    return page_id


def _roots(base_url: str) -> tuple[str, str]:
    root = base_url.rstrip("/")
    if root.endswith("/wiki"):
        return root[: -len("/wiki")], root
    return root, root + "/wiki"


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _cql_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _comment_threads(flat: list[dict]) -> tuple[list[dict], list[dict]]:
    mapped: dict[str, dict] = {}
    order: list[str] = []
    for raw in flat:
        comment_id = str(raw.get("id") or "")
        if not comment_id or comment_id in mapped:
            continue
        mapped[comment_id] = _map_comment(raw)
        order.append(comment_id)
    footer: list[dict] = []
    inline: list[dict] = []
    parents = {
        comment_id: str(mapped[comment_id].get("_parent") or "")
        for comment_id in order
    }
    for comment_id in order:
        comment = mapped[comment_id]
        parent_id = parents[comment_id]
        parent = mapped.get(parent_id) if parent_id and parent_id != comment_id else None
        if parent is not None and _comment_link_cycles(comment_id, parent_id, parents):
            parent = None
        if parent is None:
            target = inline if comment.get("_location") == "inline" else footer
            target.append(comment)
        else:
            replies = parent.get("replies")
            if not isinstance(replies, list):
                replies = []
                parent["replies"] = replies
            replies.append(comment)
    for comment in mapped.values():
        comment.pop("_location", None)
        comment.pop("_parent", None)
    return footer, inline


def _comment_link_cycles(comment_id: str, parent_id: str, parents: dict[str, str]) -> bool:
    seen = {comment_id}
    current = parent_id
    while current:
        if current in seen:
            return True
        seen.add(current)
        current = parents.get(current, "")
    return False


def _map_comment(raw: dict) -> dict:
    extensions = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
    location = str(extensions.get("location") or "footer").lower()
    resolution = raw.get("resolutionStatus")
    resolved = extensions.get("resolution") if isinstance(extensions.get("resolution"), dict) else {}
    if not resolution:
        resolution = resolved.get("status") or ""
    inline_props = (
        extensions.get("inlineProperties")
        if isinstance(extensions.get("inlineProperties"), dict)
        else {}
    )
    selection = str(
        inline_props.get("originalSelection") or inline_props.get("inlineOriginalSelection") or ""
    )
    version = raw.get("version") if isinstance(raw.get("version"), dict) else {}
    by = version.get("by") if isinstance(version.get("by"), dict) else {}
    history = raw.get("history") if isinstance(raw.get("history"), dict) else {}
    created_by = history.get("createdBy") if isinstance(history.get("createdBy"), dict) else {}
    ancestors = raw.get("ancestors") if isinstance(raw.get("ancestors"), list) else []
    parent = ""
    for ancestor in reversed(ancestors):
        if isinstance(ancestor, dict) and str(ancestor.get("type") or "") == "comment":
            parent = str(ancestor.get("id") or "")
            break
    body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
    atlas = body.get("atlas_doc_format")
    mapped_body = {"atlas_doc_format": atlas} if atlas is not None else {}
    properties = {"inlineOriginalSelection": selection} if selection.strip() else {}
    return {
        "id": str(raw.get("id") or ""),
        "resolutionStatus": str(resolution or ""),
        "properties": properties,
        "body": mapped_body,
        "replies": [],
        "version": {
            "number": version.get("number") or 1,
            "authorId": str(by.get("accountId") or version.get("authorId") or ""),
            "createdAt": str(version.get("when") or version.get("createdAt") or ""),
            "displayName": str(by.get("displayName") or ""),
        },
        "createdAt": str(history.get("createdDate") or raw.get("createdAt") or ""),
        "createdBy": str(created_by.get("accountId") or raw.get("createdBy") or ""),
        "authorName": str(created_by.get("displayName") or raw.get("authorName") or ""),
        "_parent": parent,
        "_location": "inline" if location == "inline" else "footer",
    }


def _expand_include(
    client: ConfluenceClient,
    node: dict,
    seen: set[str],
    images: PageImages,
    include: Callable[[dict], str | None],
    problems: list[str],
) -> str | None:
    attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
    key = str(attrs.get("extensionKey") or "")
    if key not in ("include", "excerpt-include"):
        return None
    page_id = _include_page_id(attrs)
    title, space = _include_title_space(attrs)
    label = title or page_id or key
    if not page_id and title:
        try:
            page_id = client.find_page(title, space) or ""
        except ConfluenceAuthError:
            raise
        except ConfluenceError as error:
            problems.append(f"include {label}: {error}")
            return None
    if not page_id:
        problems.append(f"include {label}: page was not found")
        return None
    if page_id in seen:
        problems.append(f"include {label}: cycle")
        return None
    seen.add(page_id)
    try:
        page = client.get_page(page_id)
        if page is None:
            problems.append(f"include {label}: page was not found")
            return None
        try:
            attachments = client.get_attachments(page_id)
        except ConfluenceAuthError:
            raise
        except ConfluenceError as error:
            attachments = []
            problems.append(f"include {label} attachments: {error}")
        images.add_attachments(attachments)
        rendered = adf_to_markdown(
            _document_body(page.get("body"), f"page {page_id}"),
            images,
            include,
        ).strip()
        return rendered
    except ConfluenceAuthError:
        raise
    except ConfluenceError as error:
        problems.append(f"include {label}: {error}")
        return None
    finally:
        seen.discard(page_id)


def _include_page_id(attrs: dict) -> str:
    params = _macro_params(attrs)
    for key in ("contentId", "pageId", "content-id"):
        value = _param_value(params, key)
        if _PAGE_ID.match(value):
            return value
    return ""


def _include_title_space(attrs: dict) -> tuple[str, str]:
    params = _macro_params(attrs)
    return _param_value(params, "page", ""), _param_value(params, "space", "spaceKey")


def _macro_params(attrs: dict) -> dict:
    parameters = attrs.get("parameters") if isinstance(attrs.get("parameters"), dict) else {}
    macro = parameters.get("macroParams") if isinstance(parameters, dict) else {}
    return macro if isinstance(macro, dict) else {}


def _param_value(params: dict, *keys: str) -> str:
    for key in keys:
        item = params.get(key)
        value = item.get("value") if isinstance(item, dict) else item
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _filename_key(name: str) -> str:
    return " ".join(Path(name).name.split())


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name).strip("._")


def _same_host(left: str, right: str) -> bool:
    left_host = urllib.parse.urlsplit(left).netloc.lower()
    right_host = urllib.parse.urlsplit(right).netloc.lower()
    return left_host == right_host


def _single_line(value: str) -> str:
    text = " ".join(value.split())
    text = text.replace("\\", "\\\\")
    for char in ("*", "_", "`", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text


def _error_detail(payload: dict) -> str:
    errors = payload.get("errors")
    if isinstance(errors, list):
        parts = []
        for item in errors:
            if isinstance(item, dict):
                parts.append(str(item.get("detail") or item.get("title") or ""))
            elif item:
                parts.append(str(item))
        text = "; ".join(part for part in parts if part)
        if text:
            return text
    message = payload.get("message") or payload.get("title") or ""
    return str(message)


def _summarize(result: ExtractResult) -> None:
    print(
        f"wrote {len(result.wrote)}, missing {len(result.missing)}, "
        f"failed {len(result.failed)}, skipped {len(result.skipped)}",
        file=sys.stderr,
    )


def build_client_from_env() -> ConfluenceClient:
    base_url = os.environ.get("JIRA_BASE_URL", "").strip()
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    missing = [
        name
        for name, value in (
            ("JIRA_BASE_URL", base_url),
            ("JIRA_EMAIL", email),
            ("JIRA_API_TOKEN", token),
        )
        if not value
    ]
    if missing:
        raise ConfluenceError(0, "set " + ", ".join(missing))
    return ConfluenceClient(
        base_url,
        json_transport(email, token, ConfluenceError),
        bytes_download(email, token, ConfluenceError),
        json_post(email, token, ConfluenceError),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract a Confluence Cloud page tree to markdown.")
    parser.add_argument("--page", required=True, help="Root page id")
    parser.add_argument("--out", default="out/confluence", help="Directory for markdown files")
    args = parser.parse_args(argv)
    args.page = args.page.strip()
    if not _PAGE_ID.match(args.page):
        parser.error("PAGE must be a Confluence page id")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        client = build_client_from_env()
        result = extract_tree(client, args.page, Path(args.out))
    except ConfluenceAuthError as error:
        print(f"authentication failed: {error}", file=sys.stderr)
        return 1
    except ConfluenceError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 1 if result.failed or result.missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
