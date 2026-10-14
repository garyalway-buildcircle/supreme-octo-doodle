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
                "updateHistory": "false",
            }
        )
        url = f"{self.base_url}/rest/api/3/issue/{urllib.parse.quote(key)}?{query}"
        return self._get(url)

    def get_comments(self, key: str) -> list[dict]:
        comments: list[dict] = []
        start = 0
        while True:
            query = urllib.parse.urlencode(
                {
                    "startAt": start,
                    "maxResults": 100,
                    "orderBy": "created",
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


def urllib_transport(email: str, token: str, timeout: float = 30.0, attempts: int = 5) -> Transport:
    auth = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
    headers = {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "User-Agent": "supreme-octo-doodle",
    }

    def transport(url: str) -> tuple[int, dict | None]:
        delay = 1.0
        last_error: JiraError | None = None
        for attempt in range(attempts):
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.status, _parse_json(response.read())
            except urllib.error.HTTPError as error:
                payload = _parse_json(error.read())
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
        raise last_error or JiraError(0, "request failed")

    return transport


def urllib_download(email: str, token: str, timeout: float = 60.0, attempts: int = 5) -> Download:
    auth = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
    opener = urllib.request.build_opener(_CaptureRedirect)

    def download(url: str) -> tuple[int, bytes]:
        delay = 1.0
        last_error: JiraError | None = None
        for attempt in range(attempts):
            try:
                return _read_download(opener, url, auth, timeout)
            except urllib.error.HTTPError as error:
                error.read()
                if error.code == 429 and attempt + 1 < attempts:
                    time.sleep(_retry_seconds(error.headers.get("Retry-After"), delay))
                    delay = min(delay * 2, 30)
                    continue
                return error.code, b""
            except urllib.error.URLError as error:
                last_error = JiraError(0, str(error.reason))
                if attempt + 1 < attempts:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
            except JiraError as error:
                last_error = error
                break
        raise last_error or JiraError(0, "attachment download failed")

    return download


def _read_download(opener, url: str, auth: str, timeout: float) -> tuple[int, bytes]:
    current = url
    send_auth = True
    for _hop in range(5):
        headers = {"Accept": "*/*", "User-Agent": "supreme-octo-doodle"}
        if send_auth:
            headers["Authorization"] = f"Basic {auth}"
        request = urllib.request.Request(current, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except _Redirect as redirect:
            if not redirect.url.startswith("https://"):
                raise JiraError(0, "attachment redirect was not https") from redirect
            send_auth = urllib.parse.urlsplit(redirect.url).netloc == urllib.parse.urlsplit(current).netloc
            current = redirect.url
    raise JiraError(0, "attachment redirect did not finish")


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
        images = IssueImages(
            attachments=(issue.get("fields") or {}).get("attachment"),
            download=client.download_attachment,
            directory=out_dir / path.stem,
            folder_name=path.stem,
            issue_key=key,
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


class IssueImages:
    def __init__(
        self,
        attachments: object,
        download: Callable[[dict], bytes],
        directory: Path,
        folder_name: str,
        issue_key: str,
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
        self._bound: dict[str, dict] = {}
        self._saved: dict[str, str] = {}
        self._used_names: set[str] = set()
        self._name_cursor: dict[str, int] = {}

    def __call__(self, node: dict) -> str | None:
        attrs = node.get("attrs") or {}
        if str(attrs.get("type") or "") == "external":
            return None
        attachment = self._match(attrs)
        if attachment is None:
            return None
        relative = self._save(attachment)
        if not relative:
            return None
        alt = str(attrs.get("alt") or attachment.get("filename") or "image")
        label = _single_line(alt) or "image"
        return f"![{label}]({relative})"

    def _match(self, attrs: dict) -> dict | None:
        media_id = str(attrs.get("id") or "")
        if media_id and media_id in self._bound:
            return self._bound[media_id]
        chosen = None
        if media_id and media_id in self._by_media:
            chosen = self._by_media[media_id]
        elif media_id and media_id in self._by_id:
            chosen = self._by_id[media_id]
        else:
            name = _filename_key(str(attrs.get("alt") or attrs.get("filename") or ""))
            queue = self._by_name.get(name) or []
            if queue:
                index = self._name_cursor.get(name, 0)
                if index >= len(queue):
                    chosen = queue[-1]
                else:
                    chosen = queue[index]
                    self._name_cursor[name] = index + 1
        if chosen is not None and media_id:
            self._bound[media_id] = chosen
        return chosen

    def _save(self, attachment: dict) -> str | None:
        attachment_id = str(attachment.get("id"))
        saved = self._saved.get(attachment_id)
        if saved:
            return saved
        filename = str(attachment.get("filename") or attachment_id)
        try:
            data = self._download(attachment)
        except JiraAuthError:
            raise
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


def _attachment_content_url(base_url: str, attachment: dict) -> str:
    content = str(attachment.get("content") or "")
    if content.startswith("https://"):
        parts = urllib.parse.urlsplit(content)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        query["redirect"] = "false"
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), ""))
    attachment_id = urllib.parse.quote(str(attachment.get("id") or ""))
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
    parser.add_argument("--end", required=True, type=int, help="Last issue number, inclusive")
    parser.add_argument("--out", default="out/jira", help="Directory for markdown files")
    args = parser.parse_args(argv)
    args.project = args.project.strip().upper()
    if not _PROJECT_KEY.match(args.project):
        parser.error("PROJECT must be a Jira key such as PROJ")
    if args.start < 1:
        parser.error("START must be 1 or greater")
    if args.end < args.start:
        parser.error("END must be greater than or equal to START")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        client = build_client_from_env()
        result = extract_range(client, args.project, args.start, args.end, Path(args.out))
    except JiraAuthError as error:
        print(f"authentication failed: {error}", file=sys.stderr)
        return 1
    except JiraError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
