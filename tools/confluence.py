"""Extract a Confluence Cloud page tree to markdown files."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from tools.adf import adf_to_markdown

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
_RESOLUTION = ("open", "resolved", "reopened", "dangling")


class ConfluenceError(Exception):
    def __init__(self, status: int, detail: str = ""):
        self.status = status
        self.detail = detail
        if detail and not status:
            message = detail
        elif detail:
            message = f"HTTP {status}: {detail[:180]}"
        elif status:
            message = f"HTTP {status}"
        else:
            message = "request failed"
        super().__init__(message)


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

    def get_footer_comments(self, page_id: str) -> list[dict]:
        url = self._v2(f"pages/{_quote(page_id)}/footer-comments", _comment_query())
        return self._with_replies(
            self._paginate(url, f"comments for {page_id}"),
            "footer-comments",
        )

    def get_inline_comments(self, page_id: str) -> list[dict]:
        url = self._v2(f"pages/{_quote(page_id)}/inline-comments", _inline_query())
        return self._with_replies(
            self._paginate(url, f"inline comments for {page_id}"),
            "inline-comments",
        )

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
                    name = _single_line(str(user.get("displayName") or ""))
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

    def _with_replies(self, comments: list[dict], kind: str) -> list[dict]:
        seen: set[str] = set()
        for comment in comments:
            comment_id = str(comment.get("id") or "")
            comment["replies"] = self._replies(comment_id, kind, seen, 1)
        return comments

    def _replies(self, comment_id: str, kind: str, seen: set[str], depth: int) -> list[dict]:
        if not comment_id or comment_id in seen:
            return []
        if depth > 20:
            raise ConfluenceError(0, f"comment {comment_id} is nested too deeply")
        seen.add(comment_id)
        url = self._v2(f"{kind}/{_quote(comment_id)}/children", _reply_query())
        replies = self._paginate(url, f"replies for {comment_id}")
        for reply in replies:
            reply["replies"] = self._replies(str(reply.get("id") or ""), kind, seen, depth + 1)
        return replies

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


class _Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


class _CaptureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            fp.read()
        finally:
            fp.close()
        raise _Redirect(newurl)

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302


def _request_with_retries(
    opener,
    url: str,
    auth: str,
    timeout: float,
    accept: str,
    attempts: int,
    data: bytes | None = None,
):
    delay = 1.0
    last_error: ConfluenceError | None = None
    for attempt in range(attempts):
        try:
            return _read(opener, url, auth, timeout, accept, data)
        except urllib.error.HTTPError as error:
            payload = error.read()
            if error.code == 429 and attempt + 1 < attempts:
                time.sleep(_retry_seconds(error.headers.get("Retry-After"), delay))
                delay = min(delay * 2, 30)
                continue
            return error.code, payload
        except urllib.error.URLError as error:
            last_error = ConfluenceError(0, str(error.reason))
            if attempt + 1 < attempts:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
        except ConfluenceError as error:
            last_error = error
            break
    raise last_error or ConfluenceError(0, "request failed")


def urllib_transport(email: str, token: str, timeout: float = 30.0, attempts: int = 5) -> Transport:
    auth = _basic_auth(email, token)
    opener = urllib.request.build_opener(_CaptureRedirect)

    def transport(url: str) -> tuple[int, dict | None]:
        status, body = _request_with_retries(
            opener, url, auth, timeout, "application/json", attempts
        )
        return status, _parse_json(body)

    return transport


def urllib_post(email: str, token: str, timeout: float = 30.0, attempts: int = 5) -> Post:
    auth = _basic_auth(email, token)
    opener = urllib.request.build_opener(_CaptureRedirect)

    def post(url: str, payload: dict) -> tuple[int, dict | None]:
        data = json.dumps(payload).encode("utf-8")
        status, body = _request_with_retries(
            opener, url, auth, timeout, "application/json", attempts, data
        )
        return status, _parse_json(body)

    return post


def urllib_download(email: str, token: str, timeout: float = 60.0, attempts: int = 5) -> Download:
    auth = _basic_auth(email, token)
    opener = urllib.request.build_opener(_CaptureRedirect)

    def download(url: str) -> tuple[int, bytes]:
        return _request_with_retries(opener, url, auth, timeout, "*/*", attempts)

    return download


def _read(
    opener,
    url: str,
    auth: str,
    timeout: float,
    accept: str,
    data: bytes | None = None,
) -> tuple[int, bytes]:
    current = url
    send_auth = True
    for _hop in range(5):
        headers = {"Accept": accept, "User-Agent": "supreme-octo-doodle"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if send_auth:
            headers["Authorization"] = f"Basic {auth}"
        request = urllib.request.Request(current, data=data, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except _Redirect as redirect:
            if not redirect.url.startswith("https://"):
                raise ConfluenceError(0, "redirect was not https") from redirect
            if data is not None and not _same_host(current, redirect.url):
                raise ConfluenceError(0, "redirect left the Confluence host") from redirect
            send_auth = _same_host(current, redirect.url)
            current = redirect.url
    raise ConfluenceError(0, "redirect did not finish")


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
        result.failed.append((page_id, str(error)))
        print(f"failed {page_id}: {error}", file=sys.stderr)
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

    titles = {_page_id(page): str(page.get("title") or "") for page in pages}
    for page in pages:
        current_id = _page_id(page)
        try:
            _write_page(client, page, titles, out_dir, result)
        except ConfluenceAuthError:
            raise
        except ConfluenceError as error:
            result.failed.append((current_id, str(error)))
            print(f"failed {current_id}: {error}", file=sys.stderr)
    _summarize(result)
    return result


def _write_page(
    client: ConfluenceClient,
    page: dict,
    titles: dict[str, str],
    out_dir: Path,
    result: ExtractResult,
) -> None:
    page_id = _page_id(page)
    footer = client.get_footer_comments(page_id)
    inline = client.get_inline_comments(page_id)
    attachments = client.get_attachments(page_id)
    names = client.display_names(_account_ids(page, [footer, inline]))
    path = out_dir / f"{page_id}.md"
    images = PageImages(
        attachments=attachments,
        download=lambda attachment: client.download_attachment(page_id, attachment),
        directory=out_dir / page_id,
        folder_name=page_id,
        page_id=page_id,
    )
    path.write_text(
        render_page(page, footer, inline, names, client.wiki_root, titles, images),
        encoding="utf-8",
    )
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
    body = adf_to_markdown(_adf_value(page.get("body"), f"page {page_id}"), images).strip()
    footer_block = _render_thread(footer, names, images, 3) or "_No comments._"
    inline_block = _render_thread(inline, names, images, 3) or "_No inline comments._"
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


def _render_thread(
    comments: list,
    names: dict[str, str],
    images: Callable[[dict], str | None] | None,
    level: int,
) -> str:
    blocks: list[str] = []
    for comment in comments:
        if isinstance(comment, dict):
            blocks.append(_render_comment(comment, names, images, level))
    return "\n\n".join(block for block in blocks if block)


def _render_comment(
    comment: dict,
    names: dict[str, str],
    images: Callable[[dict], str | None] | None,
    level: int,
) -> str:
    author = _author_name(comment, names)
    created = _version_time(comment)
    resolution = _single_line(str(comment.get("resolutionStatus") or ""))
    heading = f"{'#' * min(max(level, 3), 6)} {author}"
    if created:
        heading = f"{heading} — {created}"
    if resolution:
        heading = f"{heading} ({resolution})"
    parts = [heading]
    selection = _selection(comment)
    if selection:
        parts.append(_blockquote(selection))
    comment_id = str(comment.get("id") or "")
    body = adf_to_markdown(_adf_value(comment.get("body"), f"comment {comment_id}"), images).strip()
    if body:
        parts.append(body)
    replies = _render_thread(comment.get("replies") or [], names, images, level + 1)
    if replies:
        parts.append(replies)
    return "\n\n".join(parts)


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

    def __call__(self, node: dict) -> str | None:
        if node.get("type") != "media":
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

    def _save(self, attachment: dict) -> str | None:
        attachment_id = str(attachment.get("id"))
        saved = self._saved.get(attachment_id)
        if saved:
            return saved
        filename = str(attachment.get("title") or attachment_id)
        try:
            data = self._download(attachment)
        except ConfluenceError as error:
            print(f"failed image {self._page_id} {filename}: {error}", file=sys.stderr)
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
    ids = [str(account_id) for account_id in (version.get("authorId"), page.get("authorId")) if account_id]
    for thread in threads:
        ids.extend(_comment_account_ids(thread))
    return ids


def _comment_account_ids(comments: list) -> list[str]:
    ids: list[str] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        version = comment.get("version") if isinstance(comment.get("version"), dict) else {}
        if version.get("authorId"):
            ids.append(str(version["authorId"]))
        replies = comment.get("replies") or []
        if isinstance(replies, list):
            ids.extend(_comment_account_ids(replies))
    return ids


def _page_author(page: dict, names: dict[str, str]) -> str:
    version = page.get("version") if isinstance(page.get("version"), dict) else {}
    account_id = str(version.get("authorId") or page.get("authorId") or "")
    if not account_id:
        return "Unknown"
    return names.get(account_id) or account_id


def _author_name(comment: dict, names: dict[str, str]) -> str:
    version = comment.get("version") if isinstance(comment.get("version"), dict) else {}
    account_id = str(version.get("authorId") or "")
    if not account_id:
        return "Unknown"
    return names.get(account_id) or account_id


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


def _comment_query() -> list[tuple[str, str]]:
    return [
        ("body-format", "atlas_doc_format"),
        ("status", "current"),
        ("sort", "created-date"),
        ("limit", "250"),
    ]


def _inline_query() -> list[tuple[str, str]]:
    query = _comment_query()
    query.extend(("resolution-status", status) for status in _RESOLUTION)
    return query


def _reply_query() -> list[tuple[str, str]]:
    return [
        ("body-format", "atlas_doc_format"),
        ("sort", "created-date"),
        ("limit", "250"),
    ]


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


def _basic_auth(email: str, token: str) -> str:
    return base64.b64encode(f"{email}:{token}".encode()).decode("ascii")


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


def _parse_json(raw: bytes) -> dict | None:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _retry_seconds(header: str | None, fallback: float) -> float:
    if header and header.strip().isdigit():
        return min(float(header.strip()), 60.0)
    return fallback


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
        urllib_transport(email, token),
        urllib_download(email, token),
        urllib_post(email, token),
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
