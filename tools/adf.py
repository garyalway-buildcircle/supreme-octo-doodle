"""Convert Atlassian Document Format to Markdown."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

BLOCK_TYPES = {
    "doc",
    "paragraph",
    "heading",
    "bulletList",
    "orderedList",
    "listItem",
    "codeBlock",
    "blockquote",
    "rule",
    "panel",
    "table",
    "tableRow",
    "tableHeader",
    "tableCell",
    "media",
    "mediaSingle",
    "mediaGroup",
    "expand",
    "nestedExpand",
    "blockCard",
}

_PANEL_LABELS = {
    "info": "Info",
    "note": "Note",
    "warning": "Warning",
    "error": "Error",
    "success": "Success",
}


MediaMarkdown = Callable[[dict], Optional[str]]
_media_markdown: ContextVar[MediaMarkdown | None] = ContextVar("media_markdown", default=None)


def adf_to_markdown(node: object, media: MediaMarkdown | None = None) -> str:
    token = _media_markdown.set(media)
    try:
        if node is None:
            return ""
        if isinstance(node, str):
            text = node.strip()
            return f"{text}\n" if text else ""
        if not isinstance(node, dict):
            return ""
        rendered = _render(node).strip()
        return f"{rendered}\n" if rendered else ""
    finally:
        _media_markdown.reset(token)


def _render(node: dict) -> str:
    renderer = RENDERERS.get(str(node.get("type")))
    if renderer:
        return renderer(node)
    content = _children(node)
    if not content:
        return ""
    if any(child.get("type") in BLOCK_TYPES for child in content):
        return _join_blocks(_render(child) for child in content)
    return "".join(_render_inline(child) for child in content)


def _children(node: dict) -> list[dict]:
    content = node.get("content") or []
    if not isinstance(content, list):
        return []
    return [child for child in content if isinstance(child, dict)]


def _join_blocks(parts: Iterable[str]) -> str:
    blocks = [part.strip("\n") for part in parts if part and part.strip("\n")]
    return "\n\n".join(blocks)


def _doc(node: dict) -> str:
    return _join_blocks(_render(child) for child in _children(node))


def _paragraph(node: dict) -> str:
    return _inlines(_children(node))


def _heading(node: dict) -> str:
    try:
        level = int((node.get("attrs") or {}).get("level") or 1)
    except (TypeError, ValueError):
        level = 1
    level = min(max(level, 1), 6)
    text = _inlines(_children(node)).replace("\n", " ").strip()
    return f"{'#' * level} {text}".rstrip()


def _code_block(node: dict) -> str:
    language = str((node.get("attrs") or {}).get("language") or "")
    text = _plain_text(node)
    run = max((len(match) for match in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, run + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def _blockquote(node: dict) -> str:
    return _quote(_join_blocks(_render(child) for child in _children(node)))


def _panel(node: dict) -> str:
    kind = str((node.get("attrs") or {}).get("panelType") or "info")
    label = _PANEL_LABELS.get(kind, kind.replace("_", " ").title() or "Info")
    body = _join_blocks(_render(child) for child in _children(node))
    inner = f"**{label}**\n\n{body}" if body else f"**{label}**"
    return _quote(inner)


def _rule(_node: dict) -> str:
    return "---"


def _list(node: dict) -> str:
    ordered = node.get("type") == "orderedList"
    start = 1
    if ordered:
        try:
            start = int((node.get("attrs") or {}).get("order") or 1)
        except (TypeError, ValueError):
            start = 1
    lines: list[str] = []
    for offset, item in enumerate(_children(node)):
        marker = f"{start + offset}." if ordered else "-"
        pad = " " * (len(marker) + 1)
        blocks = [
            _render(child).strip("\n")
            for child in _children(item)
        ]
        blocks = [block for block in blocks if block]
        if not blocks:
            lines.append(marker)
            continue
        first, *rest = blocks
        if "\n" in first:
            lines.append(marker)
            lines.extend(_indent(first, pad))
        else:
            lines.append(f"{marker} {first}")
        for block in rest:
            if not _is_list_block(block):
                lines.append("")
            lines.extend(_indent(block, pad))
    return "\n".join(lines)


def _table(node: dict) -> str:
    rows: list[list[str]] = []
    for row in _children(node):
        cells = [_cell_text(cell) for cell in _children(row)]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]

    def format_row(row: list[str]) -> str:
        return "| " + " | ".join(row) + " |"

    lines = [
        format_row(padded[0]),
        "| " + " | ".join("---" for _ in padded[0]) + " |",
    ]
    lines.extend(format_row(row) for row in padded[1:])
    return "\n".join(lines)


def _cell_text(node: dict) -> str:
    parts: list[str] = []
    for child in _children(node):
        if child.get("type") == "paragraph":
            parts.append(_inlines(_children(child)))
        else:
            parts.append(_render(child).replace("\n", " "))
    text = " ".join(" ".join(parts).split())
    return text.replace("|", "\\|")


def _media(node: dict) -> str:
    renderer = _media_markdown.get()
    if renderer is not None:
        rendered = renderer(node)
        if rendered:
            return rendered
    attrs = node.get("attrs") or {}
    name = " ".join(str(attrs.get("alt") or attrs.get("id") or "attachment").split())
    return f"[{_escape(name) or 'attachment'}]"


def _expand(node: dict) -> str:
    title = str((node.get("attrs") or {}).get("title") or "Details")
    body = _join_blocks(_render(child) for child in _children(node))
    if body:
        return f"**{_escape(title)}**\n\n{body}"
    return f"**{_escape(title)}**"


def _block_card(node: dict) -> str:
    url = str((node.get("attrs") or {}).get("url") or "")
    return _link(url, url) if url else ""


def _quote(text: str) -> str:
    if not text:
        return ""
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


def _indent(text: str, prefix: str) -> list[str]:
    return [prefix + line if line else "" for line in text.split("\n")]


def _is_list_block(text: str) -> bool:
    first = text.split("\n", 1)[0]
    return first.startswith("- ") or bool(re.match(r"\d+\. ", first))


def _inlines(nodes: list[dict]) -> str:
    return "".join(_render_inline(node) for node in nodes)


def _render_inline(node: dict) -> str:
    kind = node.get("type")
    if kind == "text":
        return _apply_marks(str(node.get("text") or ""), _marks(node))
    if kind == "hardBreak":
        return "  \n"
    if kind == "mention":
        return _escape(str((node.get("attrs") or {}).get("text") or ""))
    if kind == "emoji":
        attrs = node.get("attrs") or {}
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if kind == "inlineCard":
        url = str((node.get("attrs") or {}).get("url") or "")
        return _link(url, url) if url else ""
    if kind == "status":
        label = str((node.get("attrs") or {}).get("text") or "")
        return f"[{_escape(label)}]" if label else ""
    if kind == "date":
        return _format_adf_date((node.get("attrs") or {}).get("timestamp"))
    return _render(node)


def _marks(node: dict) -> list[dict]:
    marks = node.get("marks") or []
    if not isinstance(marks, list):
        return []
    return [mark for mark in marks if isinstance(mark, dict)]


def _apply_marks(raw: str, marks: list[dict]) -> str:
    kinds = {mark.get("type") for mark in marks}
    text = _code_span(raw) if "code" in kinds else _escape(raw)
    if "strike" in kinds:
        text = f"~~{text}~~"
    if "em" in kinds:
        text = f"*{text}*"
    if "strong" in kinds:
        text = f"**{text}**"
    link = next((mark for mark in marks if mark.get("type") == "link"), None)
    if link:
        href = str((link.get("attrs") or {}).get("href") or "")
        if href:
            text = _link(text, href)
    return text


def _plain_text(node: dict) -> str:
    if node.get("type") == "text":
        return str(node.get("text") or "")
    if node.get("type") == "hardBreak":
        return "\n"
    return "".join(_plain_text(child) for child in _children(node))


def _escape(text: str) -> str:
    text = text.replace("\\", "\\\\")
    for char in ("*", "_", "`", "[", "]", "<"):
        text = text.replace(char, f"\\{char}")
    return text


def _code_span(text: str) -> str:
    if "`" not in text:
        return f"`{text}`"
    run = max(len(match) for match in re.findall(r"`+", text))
    fence = "`" * (run + 1)
    return f"{fence} {text} {fence}"


def _link(text: str, href: str) -> str:
    safe = href.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"[{text}]({safe})"


def _format_adf_date(timestamp: object) -> str:
    if timestamp is None or timestamp == "":
        return ""
    try:
        millis = int(str(timestamp))
    except ValueError:
        return _escape(str(timestamp))
    moment = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d")


RENDERERS = {
    "doc": _doc,
    "paragraph": _paragraph,
    "heading": _heading,
    "codeBlock": _code_block,
    "blockquote": _blockquote,
    "panel": _panel,
    "rule": _rule,
    "bulletList": _list,
    "orderedList": _list,
    "table": _table,
    "media": _media,
    "mediaSingle": _doc,
    "mediaGroup": _doc,
    "expand": _expand,
    "nestedExpand": _expand,
    "blockCard": _block_card,
}
