"""Extract Jira Cloud issues in a key range to markdown files."""

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
Download = Callable[[str], Tuple[int, bytes]]
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
_IMG_TAG = re.compile(r"<img\b([^>]*)>", re.IGNORECASE)
_HTML_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_ATTACHMENT_CONTENT_ID = re.compile(r"/attachment/content/(\d+)")

_PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,31}$")
_TIME = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2})"
    r".*?(?P<offset>Z|[+-]\d{2}:?\d{2})?$"
)


class JiraError(Exception):
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


class JiraAuthError(JiraError):
    pass


@dataclass
class ExtractResult:
    wrote: list[Path] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


class JiraClient:
    def __init__(self, base_url: str, transport: Transport, download: Download | None = None):
        if not base_url.startswith("https://"):
            raise JiraError(0, "JIRA_BASE_URL must start with https://")
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._download = download

    def get_issue(self, key: str) -> dict | None:
        query = urllib.parse.urlencode(
            {
                "fields": "summary,description,status,assignee,issuelinks,attachment",
                "expand": "renderedFields",
                "updateHistory": "false",
            }
        )
        url = f"{self.base_url}/rest/api/3/issue/{urllib.parse.quote(key)}?{query}"
        return self._get(url)

    def highest_issue_number(self, project: str) -> int | None:
        query = urllib.parse.urlencode(
            {
                "jql": f"project = {project} ORDER BY key DESC",
                "maxResults": 1,
                "fields": "key",
            }
        )
        page = self._get(f"{self.base_url}/rest/api/3/search/jql?{query}")
        if page is None:
            raise JiraError(404, f"project {project} was not found")
        issues = page.get("issues") or []
        if not isinstance(issues, list) or not issues:
            return None
        key = str((issues[0] or {}).get("key") or "")
        prefix = f"{project}-"
        number = key[len(prefix):] if key.startswith(prefix) else ""
        if not number.isdigit():
            raise JiraError(0, f"unexpected issue key {key}")
        return int(number)

    def get_comments(self, key: str) -> list[dict]:
        comments: list[dict] = []
        start = 0
        while True:
            query = urllib.parse.urlencode(
                {
                    "startAt": start,
                    "maxResults": 100,
                    "orderBy": "created",
                    "expand": "renderedBody",
                }
            )
            url = (
                f"{self.base_url}/rest/api/3/issue/"
                f"{urllib.parse.quote(key)}/comment?{query}"
            )
            page = self._get(url)
            if page is None:
                raise JiraError(404, f"comments for {key} were not found")
            batch = page.get("comments") or []
            if not isinstance(batch, list):
                raise JiraError(0, f"unexpected comment payload for {key}")
            comments.extend(item for item in batch if isinstance(item, dict))
            total = page.get("total")
            if not isinstance(total, int):
                total = len(comments)
            start += len(batch)
            if not batch or start >= total:
                break
        return comments

    def _get(self, url: str) -> dict | None:
        status, payload = self._transport(url)
        if status == 404:
            return None
        if status == 401:
            raise JiraAuthError(status, "check JIRA_EMAIL and JIRA_API_TOKEN")
        if status != 200 or not isinstance(payload, dict):
            detail = ""
            if isinstance(payload, dict):
                messages = payload.get("errorMessages") or payload.get("message") or ""
                detail = "; ".join(messages) if isinstance(messages, list) else str(messages)
            raise JiraError(status, detail)
        return payload

    def download_attachment(self, attachment: dict) -> bytes:
        if self._download is None:
            raise JiraError(0, "attachment download is not configured")
        url = _attachment_content_url(self.base_url, attachment)
        status, body = self._download(url)
        if status == 401:
            raise JiraAuthError(status, "check JIRA_EMAIL and JIRA_API_TOKEN")
        filename = attachment.get("filename") or attachment.get("id")
        if status != 200:
            raise JiraError(status, f"attachment {filename}")
        if not body:
            raise JiraError(status, f"attachment {filename} was empty")
        return body


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


def _request_with_retries(opener, url: str, auth: str, timeout: float, accept: str, attempts: int):
    delay = 1.0
    last_error: JiraError | None = None
    for attempt in range(attempts):
        try:
            return _read_download(opener, url, auth, timeout, accept)
        except urllib.error.HTTPError as error:
            payload = error.read()
            if error.code == 429 and attempt + 1 < attempts:
                time.sleep(_retry_seconds(error.headers.get("Retry-After"), delay))
                delay = min(delay * 2, 30)
                continue
            return error.code, payload
        except urllib.error.URLError as error:
            last_error = JiraError(0, str(error.reason))
            if attempt + 1 < attempts:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
        except JiraError as error:
            last_error = error
            break
    raise last_error or JiraError(0, "request failed")


def urllib_transport(email: str, token: str, timeout: float = 30.0, attempts: int = 5) -> Transport:
    auth = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
    opener = urllib.request.build_opener(_CaptureRedirect)

    def transport(url: str) -> tuple[int, dict | None]:
        status, body = _request_with_retries(opener, url, auth, timeout, "application/json", attempts)
        return status, _parse_json(body)

    return transport


def urllib_download(email: str, token: str, timeout: float = 60.0, attempts: int = 5) -> Download:
    auth = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
    opener = urllib.request.build_opener(_CaptureRedirect)

    def download(url: str) -> tuple[int, bytes]:
        return _request_with_retries(opener, url, auth, timeout, "*/*", attempts)

    return download


def _read_download(opener, url: str, auth: str, timeout: float, accept: str) -> tuple[int, bytes]:
    current = url
    send_auth = True
    for _hop in range(5):
        headers = {"Accept": accept, "User-Agent": "supreme-octo-doodle"}
        if send_auth:
            headers["Authorization"] = f"Basic {auth}"
        request = urllib.request.Request(current, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except _Redirect as redirect:
            if not redirect.url.startswith("https://"):
                raise JiraError(0, "redirect was not https") from redirect
            send_auth = _same_host(current, redirect.url)
            current = redirect.url
    raise JiraError(0, "redirect did not finish")


def extract_range(
    client: JiraClient,
    project: str,
    start: int,
    end: int,
    out_dir: Path,
) -> ExtractResult:
    result = ExtractResult()
    out_dir.mkdir(parents=True, exist_ok=True)
    total = end - start + 1
    print(f"fetching {project}-{start}..{project}-{end} ({total})", file=sys.stderr)
    for number in range(start, end + 1):
        requested = f"{project}-{number}"
        try:
            issue = client.get_issue(requested)
            if issue is None:
                result.missing.append(requested)
                print(f"missing {requested}", file=sys.stderr)
                continue
            key = str(issue.get("key") or requested)
            comments = client.get_comments(key)
        except JiraAuthError:
            raise
        except JiraError as error:
            result.failed.append((requested, str(error)))
            print(f"failed {requested}: {error}", file=sys.stderr)
            continue
        path = out_dir / _markdown_name(key)
        fields = issue.get("fields") or {}
        rendered_fields = issue.get("renderedFields") or {}
        images = IssueImages(
            attachments=fields.get("attachment"),
            download=client.download_attachment,
            directory=out_dir / path.stem,
            folder_name=path.stem,
            issue_key=key,
            description=fields.get("description"),
            comments=comments,
            rendered_description=str(rendered_fields.get("description") or ""),
            rendered_comments=[
                str(comment.get("renderedBody") or "")
                for comment in comments
                if isinstance(comment, dict)
            ],
        )
        path.write_text(render_issue(issue, comments, client.base_url, images), encoding="utf-8")
        result.wrote.append(path)
        if key != requested:
            print(f"wrote {path} ({requested} returned as {key})", file=sys.stderr)
        else:
            print(f"wrote {path}", file=sys.stderr)
    print(
        f"wrote {len(result.wrote)}, missing {len(result.missing)}, failed {len(result.failed)}",
        file=sys.stderr,
    )
    return result


def render_issue(
    issue: dict,
    comments: list[dict],
    base_url: str,
    images: Callable[[dict], str | None] | None = None,
) -> str:
    fields = issue.get("fields") or {}
    key = str(issue.get("key") or "")
    summary = _single_line(str(fields.get("summary") or ""))
    status = _single_line(str((fields.get("status") or {}).get("name") or "Unknown"))
    assignee = _assignee_name(fields.get("assignee"))
    url = f"{base_url}/browse/{key}"
    description = adf_to_markdown(fields.get("description"), images).strip() or "_No description._"
    links = _render_links(fields.get("issuelinks") or [], base_url)
    comment_block = _render_comments(comments, images)
    title = f"# {key}: {summary}" if summary else f"# {key}"
    return (
        f"{title}\n\n"
        f"- **Status:** {status}\n"
        f"- **Assignee:** {assignee}\n"
        f"- **URL:** {url}\n\n"
        f"## Description\n\n"
        f"{description}\n\n"
        f"## Links\n\n"
        f"{links}\n\n"
        f"## Comments\n\n"
        f"{comment_block}\n"
    )


def _render_links(links: list, base_url: str) -> str:
    rows: list[str] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") or {}
        if "outwardIssue" in link:
            phrase = _single_line(str(link_type.get("outward") or link_type.get("name") or "links"))
            other = link.get("outwardIssue") or {}
        elif "inwardIssue" in link:
            phrase = _single_line(str(link_type.get("inward") or link_type.get("name") or "links"))
            other = link.get("inwardIssue") or {}
        else:
            continue
        key = str(other.get("key") or "")
        other_fields = other.get("fields") or {}
        summary = _single_line(str(other_fields.get("summary") or ""))
        other_status = _single_line(str((other_fields.get("status") or {}).get("name") or ""))
        if key:
            line = f"**{phrase}** [{key}]({base_url}/browse/{key})"
        else:
            line = f"**{phrase}**"
        if summary and other_status:
            line = f"{line}: {summary} ({other_status})"
        elif summary:
            line = f"{line}: {summary}"
        elif other_status:
            line = f"{line} ({other_status})"
        rows.append(f"- {line}")
    if not rows:
        return "_No issue links._"
    return "\n".join(sorted(rows))


def _render_comments(comments: list[dict], images: Callable[[dict], str | None] | None = None) -> str:
    blocks: list[str] = []
    for comment in comments:
        author = _single_line(str((comment.get("author") or {}).get("displayName") or "Unknown"))
        created = format_jira_time(str(comment.get("created") or ""))
        updated = format_jira_time(str(comment.get("updated") or ""))
        visibility = comment.get("visibility") or {}
        audience = _single_line(str(visibility.get("value") or visibility.get("identifier") or ""))
        heading = f"### {author}"
        if created:
            heading = f"{heading} — {created}"
        if audience:
            heading = f"{heading} ({audience})"
        body = adf_to_markdown(comment.get("body"), images).strip()
        parts = [heading]
        if updated and updated != created:
            parts.append(f"_Updated {updated}._")
        if body:
            parts.append(body)
        blocks.append("\n\n".join(parts))
    if not blocks:
        return "_No comments._"
    return "\n\n".join(blocks)


def format_jira_time(value: str) -> str:
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


class _RenderedImage:
    def __init__(self, attachment_id: str, media_id: str):
        self.attachment_id = attachment_id
        self.media_id = media_id
        self.used = False


class IssueImages:
    def __init__(
        self,
        attachments: object,
        download: Callable[[dict], bytes],
        directory: Path,
        folder_name: str,
        issue_key: str,
        description: object = None,
        comments: list | None = None,
        rendered_description: str = "",
        rendered_comments: list | None = None,
    ):
        self._download = download
        self._directory = directory
        self._folder_name = folder_name
        self._issue_key = issue_key
        self._images = [item for item in attachments or [] if isinstance(item, dict) and _is_image(item)]
        self._by_id = {str(item.get("id")): item for item in self._images if item.get("id") is not None}
        self._by_media: dict[str, dict] = {}
        self._by_name: dict[str, list[dict]] = {}
        for item in self._images:
            for field_name in ("mediaApiFileId", "mediaId"):
                media_id = item.get(field_name)
                if media_id:
                    self._by_media[str(media_id)] = item
            name = _filename_key(str(item.get("filename") or ""))
            if name:
                self._by_name.setdefault(name, []).append(item)
        self._by_node: dict[int, dict] = {}
        self._saved: dict[str, str] = {}
        self._used_names: set[str] = set()
        self._warned: set[int] = set()
        self._bind_nodes(description, comments or [], rendered_description, rendered_comments or [])

    def __call__(self, node: dict) -> str | None:
        if not _is_embedded_image(node):
            return None
        attachment = self._match(node)
        if attachment is None:
            self._warn_unmatched(node)
            return None
        relative = self._save(attachment)
        if not relative:
            return None
        attrs = node.get("attrs") or {}
        alt = str(attrs.get("alt") or attachment.get("filename") or "image")
        label = _single_line(alt) or "image"
        return f"![{label}]({relative})"

    def _bind_nodes(self, description: object, comments: list, rendered_description: str, rendered_comments: list) -> None:
        nodes = [node for node in _iter_media(description) if _is_embedded_image(node)]
        for comment in comments:
            if isinstance(comment, dict):
                nodes.extend(node for node in _iter_media(comment.get("body")) if _is_embedded_image(node))
        rendered: list[_RenderedImage] = []
        rendered.extend(_rendered_images(rendered_description))
        for html in rendered_comments:
            rendered.extend(_rendered_images(str(html or "")))
        unbound: list[dict] = []
        for node in nodes:
            media_id = str((node.get("attrs") or {}).get("id") or "")
            image = _unused_rendered(rendered, media_id) if media_id else None
            attachment = self._attachment_for_image(image) if image is not None else None
            if attachment is None and media_id:
                attachment = self._by_media.get(media_id) or self._by_id.get(media_id)
                if attachment is not None:
                    image = _unused_rendered_attachment(rendered, str(attachment.get("id")))
            if attachment is None:
                unbound.append(node)
                continue
            self._by_node[id(node)] = attachment
            if image is not None:
                image.used = True
        unused = [image for image in rendered if not image.used and self._attachment_for_image(image) is not None]
        if unbound and len(unbound) == len(unused):
            for node, image in zip(unbound, unused):
                attachment = self._attachment_for_image(image)
                if attachment is None:
                    continue
                self._by_node[id(node)] = attachment
                image.used = True
            unbound = [node for node in unbound if id(node) not in self._by_node]
        for node in unbound:
            attachment = self._unique_named_attachment(node)
            if attachment is not None:
                self._by_node[id(node)] = attachment

    def _attachment_for_image(self, image: _RenderedImage) -> dict | None:
        attachment = self._by_id.get(image.attachment_id)
        if attachment is not None:
            return attachment
        if image.media_id:
            return self._by_media.get(image.media_id)
        return None

    def _match(self, node: dict) -> dict | None:
        bound = self._by_node.get(id(node))
        if bound is not None:
            return bound
        media_id = str((node.get("attrs") or {}).get("id") or "")
        if media_id and media_id in self._by_media:
            return self._by_media[media_id]
        if media_id and media_id in self._by_id:
            return self._by_id[media_id]
        return self._unique_named_attachment(node)

    def _unique_named_attachment(self, node: dict) -> dict | None:
        attrs = node.get("attrs") or {}
        name = _filename_key(str(attrs.get("alt") or attrs.get("filename") or ""))
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
        print(f"failed image {self._issue_key} {name}: no matching attachment", file=sys.stderr)

    def _save(self, attachment: dict) -> str | None:
        attachment_id = str(attachment.get("id"))
        saved = self._saved.get(attachment_id)
        if saved:
            return saved
        filename = str(attachment.get("filename") or attachment_id)
        try:
            data = self._download(attachment)
        except JiraError as error:
            print(f"failed image {self._issue_key} {filename}: {error}", file=sys.stderr)
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


def _iter_media(node: object):
    if not isinstance(node, dict):
        return
    if node.get("type") == "media":
        yield node
    for child in node.get("content") or []:
        yield from _iter_media(child)


def _is_embedded_image(node: dict) -> bool:
    if node.get("type") != "media":
        return False
    attrs = node.get("attrs") or {}
    if str(attrs.get("type") or "") == "external":
        return False
    name = str(attrs.get("alt") or attrs.get("filename") or "")
    suffix = Path(name).suffix.lower()
    if suffix in _NON_IMAGE_SUFFIXES:
        return False
    return suffix in _IMAGE_SUFFIXES or not suffix


def _rendered_images(html: str) -> list[_RenderedImage]:
    images: list[_RenderedImage] = []
    for match in _IMG_TAG.finditer(html or ""):
        attrs: dict[str, str] = {}
        for found in _HTML_ATTR.finditer(match.group(1)):
            value = found.group(2) if found.group(2) is not None else found.group(3)
            attrs[found.group(1).lower()] = value or ""
        src = (attrs.get("src") or "").replace("&amp;", "&")
        content = _ATTACHMENT_CONTENT_ID.search(src)
        if not content:
            continue
        media_id = (
            attrs.get("data-media-services-id")
            or attrs.get("data-media-id")
            or attrs.get("data-file-id")
            or ""
        )
        images.append(_RenderedImage(content.group(1), media_id))
    return images


def _unused_rendered(rendered: list[_RenderedImage], media_id: str) -> _RenderedImage | None:
    for image in rendered:
        if not image.used and image.media_id == media_id:
            return image
    return None


def _unused_rendered_attachment(rendered: list[_RenderedImage], attachment_id: str) -> _RenderedImage | None:
    for image in rendered:
        if not image.used and image.attachment_id == attachment_id:
            return image
    return None


def _is_image(attachment: dict) -> bool:
    mime = str(attachment.get("mimeType") or "").split(";", 1)[0].strip().lower()
    if mime.startswith("image/"):
        return True
    return Path(str(attachment.get("filename") or "")).suffix.lower() in _IMAGE_SUFFIXES


def _filename_key(name: str) -> str:
    return " ".join(Path(name).name.split())


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name).strip("._")
    return cleaned


def _same_host(left: str, right: str) -> bool:
    return urllib.parse.urlsplit(left).netloc.lower() == urllib.parse.urlsplit(right).netloc.lower()


def _attachment_content_url(base_url: str, attachment: dict) -> str:
    content = str(attachment.get("content") or "")
    if content.startswith("https://") and _same_host(base_url, content):
        parts = urllib.parse.urlsplit(content)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        query["redirect"] = "false"
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), ""))
    attachment_id = urllib.parse.quote(str(attachment.get("id") or ""), safe="")
    return f"{base_url}/rest/api/3/attachment/content/{attachment_id}?redirect=false"


def _assignee_name(assignee: object) -> str:
    if not isinstance(assignee, dict):
        return "Unassigned"
    name = _single_line(str(assignee.get("displayName") or ""))
    return name or "Unassigned"


def _markdown_name(key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key).strip("._")
    return f"{safe or 'issue'}.md"


def _single_line(value: str) -> str:
    text = " ".join(value.split())
    text = text.replace("\\", "\\\\")
    for char in ("*", "_", "`", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text


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


def build_client_from_env() -> JiraClient:
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
        raise JiraError(0, "set " + ", ".join(missing))
    return JiraClient(base_url, urllib_transport(email, token), urllib_download(email, token))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract Jira Cloud issues to markdown files.")
    parser.add_argument("--project", required=True, help="Project key, for example PROJ")
    parser.add_argument("--start", required=True, type=int, help="First issue number, inclusive")
    parser.add_argument("--end", type=int, help="Last issue number, inclusive. Defaults to the highest existing issue.")
    parser.add_argument("--out", default="out/jira", help="Directory for markdown files")
    args = parser.parse_args(argv)
    args.project = args.project.strip().upper()
    if not _PROJECT_KEY.match(args.project):
        parser.error("PROJECT must be a Jira key such as PROJ")
    if args.start < 1:
        parser.error("START must be 1 or greater")
    if args.end is not None and args.end < args.start:
        parser.error("END must be greater than or equal to START")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        client = build_client_from_env()
        end = args.end
        if end is None:
            end = client.highest_issue_number(args.project)
            if end is None or end < args.start:
                print(f"no issues at or above {args.project}-{args.start}", file=sys.stderr)
                return 0
            print(f"last issue is {args.project}-{end}", file=sys.stderr)
        result = extract_range(client, args.project, args.start, end, Path(args.out))
    except JiraAuthError as error:
        print(f"authentication failed: {error}", file=sys.stderr)
        return 1
    except JiraError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
