"""Rewrite exported markdown links so they point at local files."""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

_JIRA_FILE = re.compile(r"^[A-Z][A-Z0-9]{1,31}-\d+\.md$")
_PAGE_FILE = re.compile(r"^\d+\.md$")
_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,31}-\d+$")
_URL_LINE = re.compile(r"^- \*\*URL:\*\* (https://\S+)", re.MULTILINE)
_PAGE_PATH = re.compile(r"/pages/(\d+)(?=/|$)")
_ISSUE_PATH = re.compile(
    r"/(?:browse|issues)/([A-Z][A-Z0-9]{1,31}-\d+)(?=/|$)",
    re.IGNORECASE,
)
_FENCE_OPEN = re.compile(r"^( *)(`{3,}|~{3,})")


def _noun(count: int, singular: str) -> str:
    word = singular if count == 1 else f"{singular}s"
    return f"{count} {word}"


@dataclass
class LinkResult:
    files: list[Path] = field(default_factory=list)
    links: int = 0


def rewrite_dump(jira_dir: Path, confluence_dir: Path) -> LinkResult:
    jira_files = _markdown_files(jira_dir, _JIRA_FILE)
    page_files = _markdown_files(confluence_dir, _PAGE_FILE)
    jira_index = {path.stem: path for path in jira_files}
    page_index = {path.stem: path for path in page_files}
    documents = jira_files + page_files
    texts = {path: path.read_text(encoding="utf-8") for path in documents}
    hosts: set[str] = set()
    for text in texts.values():
        hosts.update(_hosts(text))
    result = LinkResult()
    for path in documents:
        rewritten, count = _rewrite_markdown(
            texts[path],
            lambda dest, source=path: _resolve(
                dest, source, hosts, jira_index, page_index
            ),
        )
        if count:
            path.write_text(rewritten, encoding="utf-8")
            result.files.append(path)
            result.links += count
            print(f"rewrote {_noun(count, 'link')} in {path}", file=sys.stderr)
    print(
        f"rewrote {_noun(result.links, 'link')} in {_noun(len(result.files), 'file')}",
        file=sys.stderr,
    )
    return result


def _markdown_files(directory: Path, pattern: re.Pattern[str]) -> list[Path]:
    if not directory.is_dir():
        return []
    paths = [
        path
        for path in directory.iterdir()
        if path.is_file() and pattern.match(path.name)
    ]
    return sorted(paths)


def _hosts(text: str) -> set[str]:
    found: set[str] = set()
    for match in _URL_LINE.finditer(text):
        host = urllib.parse.urlsplit(match.group(1)).hostname
        if host:
            found.add(host.lower())
    return found


def _resolve(
    dest: str,
    source: Path,
    hosts: set[str],
    jira_index: dict[str, Path],
    page_index: dict[str, Path],
) -> str | None:
    parts = urllib.parse.urlsplit(dest.strip())
    target = _local_target(parts, hosts, jira_index, page_index)
    if target is None:
        return None
    relative = Path(os.path.relpath(target, source.parent)).as_posix()
    if parts.fragment:
        return f"{relative}#{parts.fragment}"
    return relative


def _local_target(
    parts: urllib.parse.SplitResult,
    hosts: set[str],
    jira_index: dict[str, Path],
    page_index: dict[str, Path],
) -> Path | None:
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.hostname.lower() not in hosts:
        return None
    page_id = _page_id(parts)
    if page_id:
        return page_index.get(page_id)
    key = _issue_key(parts)
    if key:
        return jira_index.get(key)
    return None


def _page_id(parts: urllib.parse.SplitResult) -> str | None:
    match = _PAGE_PATH.search(parts.path)
    if match:
        return match.group(1)
    for value in urllib.parse.parse_qs(parts.query).get("pageId", []):
        if value.isdigit():
            return value
    return None


def _issue_key(parts: urllib.parse.SplitResult) -> str | None:
    match = _ISSUE_PATH.search(parts.path)
    if match:
        return match.group(1).upper()
    for value in urllib.parse.parse_qs(parts.query).get("selectedIssue", []):
        key = value.upper()
        if _ISSUE_KEY.match(key):
            return key
    return None


def _rewrite_markdown(text: str, resolve) -> tuple[str, int]:
    count = 0
    pieces: list[str] = []
    for kind, chunk in _segments(text):
        if kind == "code":
            pieces.append(chunk)
            continue
        rewritten, found = _rewrite_prose(chunk, resolve)
        count += found
        pieces.append(rewritten)
    return "".join(pieces), count


def _segments(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines(keepends=True)
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    kind = "prose"
    fence_char = ""
    fence_len = 0
    for line in lines:
        content = _unquote(line.rstrip("\n"))
        if kind == "prose":
            opened = _open_fence(content)
            if opened:
                if buf:
                    segments.append(("prose", "".join(buf)))
                    buf = []
                fence_char, fence_len = opened
                kind = "code"
            buf.append(line)
            continue
        buf.append(line)
        if _close_fence(content, fence_char, fence_len):
            segments.append(("code", "".join(buf)))
            buf = []
            kind = "prose"
            fence_char = ""
            fence_len = 0
    if buf:
        segments.append((kind, "".join(buf)))
    return segments


def _unquote(line: str) -> str:
    while line.startswith(">"):
        line = line[1:]
        if line.startswith(" "):
            line = line[1:]
    return line


def _open_fence(content: str) -> tuple[str, int] | None:
    match = _FENCE_OPEN.match(content)
    if not match:
        return None
    mark = match.group(2)
    return mark[0], len(mark)


def _close_fence(content: str, char: str, length: int) -> bool:
    match = re.match(r"^ *(" + re.escape(char) + r"{3,})[ \t]*$", content)
    return bool(match) and len(match.group(1)) >= length


def _rewrite_prose(text: str, resolve) -> tuple[str, int]:
    pieces: list[str] = []
    count = 0
    index = 0
    while index < len(text):
        if text[index] == "`":
            run = _backtick_run(text, index)
            end = _code_span_end(text, index, run)
            if end is None:
                pieces.append(text[index : index + run])
                index += run
                continue
            pieces.append(text[index:end])
            index = end
            continue
        if text.startswith("![", index):
            parsed = _parse_link(text, index + 1)
            if parsed:
                _, _, end = parsed
                pieces.append(text[index:end])
                index = end
                continue
        if text[index] == "[":
            parsed = _parse_link(text, index)
            if parsed:
                label, dest, end = parsed
                replacement = resolve(dest)
                if replacement and replacement != dest:
                    pieces.append(f"[{label}]({_destination(replacement)})")
                    count += 1
                else:
                    pieces.append(text[index:end])
                index = end
                continue
        pieces.append(text[index])
        index += 1
    return "".join(pieces), count


def _backtick_run(text: str, start: int) -> int:
    length = 1
    while start + length < len(text) and text[start + length] == "`":
        length += 1
    return length


def _code_span_end(text: str, start: int, length: int) -> int | None:
    marker = "`" * length
    search = start + length
    while True:
        found = text.find(marker, search)
        if found == -1:
            return None
        end = found + length
        if end < len(text) and text[end] == "`":
            while end < len(text) and text[end] == "`":
                end += 1
            search = end
            continue
        return end


def _parse_link(text: str, start: int) -> tuple[str, str, int] | None:
    if start >= len(text) or text[start] != "[":
        return None
    index = start + 1
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            index += 2
            continue
        if text[index] == "\n":
            return None
        if text[index] == "]":
            break
        index += 1
    else:
        return None
    label = text[start + 1 : index]
    index += 1
    if index >= len(text) or text[index] != "(":
        return None
    index += 1
    if index < len(text) and text[index] == "<":
        close = text.find(">", index + 1)
        if close == -1 or "\n" in text[index + 1 : close]:
            return None
        dest = text[index + 1 : close]
        index = close + 1
    else:
        dest_start = index
        while index < len(text) and text[index] not in ")\n \t":
            index += 1
        if index >= len(text) or text[index] != ")":
            return None
        dest = text[dest_start:index]
    if index >= len(text) or text[index] != ")" or not dest:
        return None
    return label, dest, index + 1


def _destination(href: str) -> str:
    return href.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rewrite links between exported Jira and Confluence markdown files."
    )
    parser.add_argument(
        "--jira",
        default="out/jira",
        help="Directory of Jira markdown files",
    )
    parser.add_argument(
        "--confluence",
        default="out/confluence",
        help="Directory of Confluence markdown files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    jira_dir = Path(args.jira)
    confluence_dir = Path(args.confluence)
    for path in (jira_dir, confluence_dir):
        if path.exists() and not path.is_dir():
            print(f"{path} is not a directory", file=sys.stderr)
            return 2
    try:
        rewrite_dump(jira_dir, confluence_dir)
    except OSError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
