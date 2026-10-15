"""Summarize GitHub repositories as markdown files."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from tools.c4 import SKIP_DIR_NAMES, Diagram, cdk_diagrams, terraform_diagrams

_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_README_NAMES = ("readme.md", "readme.markdown", "readme.rst", "readme.txt", "readme")
_README_LIMIT = 80_000
_LANGUAGE_LIMIT = 2_000_000
_TEXT_LIMIT = 200_000
_PROJECT_NAMES = {
    "Cargo.toml",
    "Dockerfile",
    "Gemfile",
    "LICENSE",
    "LICENSE.md",
    "Makefile",
    "build.gradle",
    "build.gradle.kts",
    "compose.yaml",
    "compose.yml",
    "docker-compose.yml",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
}
_SKIP_FILE_NAMES = {
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "yarn.lock",
}
_LANGUAGES = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".go": "Go",
    ".h": "C",
    ".hcl": "HCL",
    ".hpp": "C++",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".json": "JSON",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".md": "Markdown",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scss": "SCSS",
    ".sh": "Shell",
    ".sql": "SQL",
    ".swift": "Swift",
    ".tf": "HCL",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".yaml": "YAML",
    ".yml": "YAML",
}
_ASKPASS = """#!/bin/sh
case "$1" in
  [Uu]sername*) printf '%s\\n' 'x-access-token' ;;
  *) printf '%s\\n' "$GIT_TOKEN" ;;
esac
"""


class GitError(Exception):
    pass


@dataclass(frozen=True)
class Repository:
    clone_url: str
    page_url: str
    owner: str
    name: str


@dataclass(frozen=True)
class Checkout:
    commit: str
    branch: str
    committed_at: str
    subject: str


@dataclass(frozen=True)
class RepoMeta:
    url: str
    owner: str
    name: str
    branch: str
    commit: str
    committed_at: str
    subject: str


@dataclass
class ExtractResult:
    wrote: list[Path]
    failed: list[tuple[str, str]]


Clone = Callable[[str, Path, Optional[str]], Checkout]


def parse_github_url(value: str) -> Repository:
    raw = value.strip()
    if not raw:
        raise GitError("not a GitHub repository URL")
    for pattern in (r"^git@github\.com:(.+)$", r"^ssh://git@github\.com/(.+)$"):
        matched = re.match(pattern, raw)
        if matched:
            return _repository_from_path(matched.group(1))
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host != "github.com":
        raise GitError("not a GitHub repository URL")
    if parsed.username or parsed.password:
        raise GitError("repository URL must not include credentials")
    return _repository_from_path(parsed.path)


def read_repo_list(path: Path) -> list[str]:
    if not path.is_file():
        raise GitError(f"{path} is not a file")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise GitError(f"{path} is not utf-8") from error
    except OSError as error:
        raise GitError(str(error)) from error
    urls: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    if not urls:
        raise GitError(f"{path} has no repository URLs")
    return urls


def clone_command(url: str, dest: Path) -> list[str]:
    return [
        "git",
        "-c",
        "credential.helper=",
        "clone",
        "--depth",
        "1",
        "--quiet",
        url,
        os.fspath(dest),
    ]


@contextmanager
def clone_environment(token: str | None) -> Iterator[dict[str, str]]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    with TemporaryDirectory() as tmp:
        config = Path(tmp) / "gitconfig"
        config.write_text("", encoding="utf-8")
        env["GIT_CONFIG_GLOBAL"] = str(config)
        env["GIT_CONFIG_SYSTEM"] = str(config)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        if token:
            script = Path(tmp) / "askpass"
            script.write_text(_ASKPASS, encoding="utf-8")
            script.chmod(0o700)
            env["GIT_ASKPASS"] = str(script)
            env["GIT_TOKEN"] = token
        yield env


def clone_repository(url: str, dest: Path, token: str | None) -> Checkout:
    command = clone_command(url, dest)
    try:
        with clone_environment(token) as env:
            proc = subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
    except subprocess.TimeoutExpired as error:
        raise GitError("git clone timed out") from error
    except FileNotFoundError as error:
        raise GitError("git is not installed") from error
    if proc.returncode != 0:
        detail = _redact(proc.stderr or proc.stdout, token)
        raise GitError(detail or "git clone failed")
    return _read_checkout(dest)


def summarize(root: Path, meta: RepoMeta) -> str:
    text, _diagrams = _summary(root, meta)
    return text


def _summary(root: Path, meta: RepoMeta) -> tuple[str, list[tuple[str, str]]]:
    layout = _layout(root)
    layout_block = layout if layout == "_Empty._" else f"```\n{layout}\n```"
    terraform = terraform_diagrams(root)
    cdk = cdk_diagrams(root)
    stem = f"{meta.owner}-{meta.name}"
    terraform_links, terraform_files = _diagram_links(stem, "terraform", terraform)
    cdk_links, cdk_files = _diagram_links(stem, "cdk", cdk)
    parts = [
        f"# {meta.owner}/{meta.name}",
        "",
        f"- **URL:** {meta.url}",
        f"- **Default branch:** {meta.branch}",
        f"- **Commit:** {meta.commit}",
        f"- **Commit date:** {meta.committed_at}",
        f"- **Subject:** {_inline(meta.subject)}",
        "",
        "## README",
        "",
        _readme(root),
        "",
        "## Languages",
        "",
        _languages(root),
        "",
        "## Layout",
        "",
        layout_block,
        "",
        "## Project files",
        "",
        _project_files(root),
        "",
        _diagram_section("Terraform", terraform, "_No Terraform roots._", terraform_links),
        "",
        _diagram_section("CDK", cdk, "_No CDK apps._", cdk_links),
        "",
    ]
    return "\n".join(parts), terraform_files + cdk_files


def extract_repositories(
    urls: list[str],
    out_dir: Path,
    token: str | None,
    clone: Clone = clone_repository,
) -> ExtractResult:
    result = ExtractResult(wrote=[], failed=[])
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for raw in urls:
        label = _safe_label(raw)
        try:
            repository = parse_github_url(raw)
        except GitError as error:
            _fail(result, label, error, token)
            continue
        if repository.page_url in seen:
            print(f"skip duplicate {repository.page_url}", file=sys.stderr)
            continue
        seen.add(repository.page_url)
        try:
            with TemporaryDirectory() as tmp:
                dest = Path(tmp) / "repo"
                checkout = clone(repository.clone_url, dest, token)
                text, diagrams = _summary(
                    dest,
                    RepoMeta(
                        url=repository.page_url,
                        owner=repository.owner,
                        name=repository.name,
                        branch=checkout.branch,
                        commit=checkout.commit,
                        committed_at=checkout.committed_at,
                        subject=checkout.subject,
                    ),
                )
            path = out_dir / f"{repository.owner}-{repository.name}.md"
            for relative, body in diagrams:
                diagram_path = out_dir / relative
                diagram_path.parent.mkdir(parents=True, exist_ok=True)
                diagram_path.write_text(body, encoding="utf-8")
            path.write_text(text, encoding="utf-8")
        except (GitError, OSError) as error:
            _fail(result, repository.page_url, error, token)
            continue
        result.wrote.append(path)
        print(f"wrote {path}", file=sys.stderr)
    print(
        f"wrote {len(result.wrote)}, failed {len(result.failed)}",
        file=sys.stderr,
    )
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize GitHub repositories as markdown files.")
    parser.add_argument("--url", help="GitHub repository URL")
    parser.add_argument("--repos", help="Text file of GitHub repository URLs, one per line")
    parser.add_argument("--out", default="out/git", help="Directory for markdown files")
    args = parser.parse_args(argv)
    if bool(args.url) == bool(args.repos):
        parser.error("pass --url or --repos")
    return args


def main(argv: list[str] | None = None, clone: Clone = clone_repository) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        urls = [args.url] if args.url else read_repo_list(Path(args.repos))
        token = os.environ.get("GIT_TOKEN", "").strip() or None
        result = extract_repositories(urls, Path(args.out), token, clone)
    except (GitError, OSError) as error:
        print(_redact(str(error), os.environ.get("GIT_TOKEN", "").strip() or None), file=sys.stderr)
        return 2
    return 1 if result.failed else 0


def _repository_from_path(path: str) -> Repository:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        raise GitError("not a GitHub repository URL")
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    if not _valid_component(owner) or not _valid_component(name):
        raise GitError("not a GitHub repository URL")
    return Repository(
        clone_url=f"https://github.com/{owner}/{name}.git",
        page_url=f"https://github.com/{owner}/{name}",
        owner=owner,
        name=name,
    )


def _valid_component(value: str) -> bool:
    if not _COMPONENT.match(value):
        return False
    return not value.endswith(".") and ".." not in value


def _read_checkout(dest: Path) -> Checkout:
    commit = _git(dest, "rev-parse", "HEAD")
    branch = _git(dest, "rev-parse", "--abbrev-ref", "HEAD")
    meta = _git(dest, "log", "-1", "--format=%cI%x00%s")
    committed_at, separator, subject = meta.partition("\x00")
    if not separator:
        subject = ""
    return Checkout(
        commit=commit,
        branch=branch,
        committed_at=committed_at,
        subject=subject,
    )


def _git(dest: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", os.fspath(dest), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise GitError("git is not installed") from error
    if proc.returncode != 0:
        raise GitError(_redact(proc.stderr, None) or "git failed")
    return proc.stdout.strip()


def _fail(result: ExtractResult, label: str, error: BaseException, token: str | None) -> None:
    message = _redact(str(error), token)
    result.failed.append((label, message))
    print(f"failed {label}: {message}", file=sys.stderr)


def _safe_label(value: str) -> str:
    raw = value.strip()
    parsed = urllib.parse.urlsplit(raw)
    if not parsed.username and not parsed.password:
        return raw
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit(parsed._replace(netloc=host))


def _redact(text: str, token: str | None) -> str:
    if token:
        text = text.replace(token, "***")
    return " ".join(text.split())


def _diagram_section(
    heading: str,
    diagrams: list[Diagram],
    empty: str,
    links: dict[int, tuple[str, str]],
) -> str:
    if not diagrams:
        return f"## {heading}\n\n{empty}"
    parts = [f"## {heading}"]
    parts.extend(
        _one_diagram(diagram, links.get(index)) for index, diagram in enumerate(diagrams)
    )
    return "\n\n".join(parts)


def _one_diagram(diagram: Diagram, link: tuple[str, str] | None) -> str:
    lines = [f"### {diagram.title}", ""]
    if diagram.detail:
        lines.append(f"- **App:** {_inline(diagram.detail)}")
    if diagram.files:
        shown = diagram.files[:30]
        files = ", ".join(f"`{name}`" for name in shown)
        if len(diagram.files) > 30:
            files += ", ..."
        lines.append(f"- **Files:** {files}")
    if link:
        filename, relative = link
        lines.append(f"- **Diagram:** [{filename}]({relative})")
    if not diagram.mermaid:
        if len(lines) > 2:
            lines.append("")
        lines.append(diagram.note)
    return "\n".join(lines)


def _diagram_links(
    stem: str,
    kind: str,
    diagrams: list[Diagram],
) -> tuple[dict[int, tuple[str, str]], list[tuple[str, str]]]:
    links: dict[int, tuple[str, str]] = {}
    files: list[tuple[str, str]] = []
    used: set[str] = set()
    for index, diagram in enumerate(diagrams):
        if not diagram.mermaid:
            continue
        filename = _unique_diagram_name(kind, _diagram_slug(diagram.title), used)
        relative = f"{stem}/{filename}"
        links[index] = (filename, relative)
        body = diagram.mermaid if diagram.mermaid.endswith("\n") else f"{diagram.mermaid}\n"
        files.append((relative, body))
    return links, files


def _diagram_slug(title: str) -> str:
    slug = title.replace("\\", "/").strip("/")
    if slug in {"", "."}:
        return "root"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug.replace("/", "-")).strip(".-")
    return slug or "root"


def _unique_diagram_name(kind: str, slug: str, used: set[str]) -> str:
    filename = f"{kind}-{slug}.mmd"
    number = 2
    while filename in used:
        filename = f"{kind}-{slug}-{number}.mmd"
        number += 1
    used.add(filename)
    return filename


def _readme(root: Path) -> str:
    path = _find_readme(root)
    if path is None:
        return "_No README._"
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            text = handle.read(_README_LIMIT + 1)
    except OSError:
        return "_No README._"
    truncated = len(text) > _README_LIMIT
    text = text[:_README_LIMIT].strip("\n")
    if not text.strip():
        return "_No README._"
    if truncated:
        text = text.rstrip() + "\n\n_README truncated._"
    return text


def _find_readme(root: Path) -> Path | None:
    found: dict[str, Path] = {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    for path in entries:
        if path.is_file() and not path.is_symlink():
            found[path.name.lower()] = path
    for name in _README_NAMES:
        if name in found:
            return found[name]
    return None


def _languages(root: Path) -> str:
    totals: dict[str, tuple[int, int]] = {}
    for path in _walk_files(root):
        if path.name in _SKIP_FILE_NAMES:
            continue
        language = _LANGUAGES.get(path.suffix.lower())
        if language is None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > _LANGUAGE_LIMIT:
            continue
        files, total = totals.get(language, (0, 0))
        totals[language] = (files + 1, total + size)
    if not totals:
        return "_No recognized source files._"
    lines = []
    ranked = sorted(totals.items(), key=lambda item: (-item[1][1], item[0]))
    for language, (files, total) in ranked:
        lines.append(f"- {language}: {_noun(files, 'file')}, {_noun(total, 'byte')}")
    return "\n".join(lines)


def _layout(root: Path) -> str:
    lines: list[str] = []

    def add(directory: Path, depth: int) -> None:
        if len(lines) >= 200:
            return
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        visible = [
            entry
            for entry in entries
            if entry.name not in SKIP_DIR_NAMES and not entry.is_symlink()
        ]
        visible.sort(key=lambda entry: (entry.is_file(), entry.name.lower()))
        for entry in visible:
            if len(lines) >= 200:
                if lines[-1] != "...":
                    lines.append("...")
                return
            indent = "  " * depth
            if entry.is_dir():
                lines.append(f"{indent}{entry.name}/")
                if depth < 1:
                    add(entry, depth + 1)
            else:
                lines.append(f"{indent}{entry.name}")

    add(root, 0)
    if not lines:
        return "_Empty._"
    return "\n".join(lines)


def _project_files(root: Path) -> str:
    found = [path for path in _walk_files(root) if _is_project_file(root, path)]
    found.sort(key=lambda path: path.relative_to(root).as_posix())
    if not found:
        return "_No project files._"
    lines = [_project_fact(root, path) for path in found[:40]]
    if len(found) > 40:
        lines.append("_Project file list truncated._")
    return "\n".join(lines)


def _is_project_file(root: Path, path: Path) -> bool:
    if path.name in _PROJECT_NAMES:
        return True
    relative = path.relative_to(root)
    return (
        len(relative.parts) == 3
        and relative.parts[0] == ".github"
        and relative.parts[1] == "workflows"
        and path.suffix in {".yml", ".yaml"}
    )


def _project_fact(root: Path, path: Path) -> str:
    relative = path.relative_to(root).as_posix()
    text = _read_limited(path, _TEXT_LIMIT)
    detail = _project_detail(path.name, text) if text is not None else ""
    if detail:
        return f"- `{relative}`: {detail}"
    return f"- `{relative}`"


def _project_detail(name: str, text: str) -> str:
    if name == "package.json":
        return _package_json_detail(text)
    if name == "pyproject.toml":
        return _pyproject_detail(text)
    if name == "requirements.txt":
        return _requirements_detail(text)
    if name == "go.mod":
        return _go_mod_detail(text)
    if name == "Cargo.toml":
        return _toml_name(text)
    if name == "Gemfile":
        return _gemfile_detail(text)
    if name == "pom.xml":
        return _pom_detail(text)
    if name == "Dockerfile":
        return _docker_detail(text)
    if name == "Makefile":
        return _makefile_detail(text)
    if name in {"LICENSE", "LICENSE.md"}:
        return _first_line(text)
    return ""


def _package_json_detail(text: str) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    bits: list[str] = []
    name = data.get("name")
    if isinstance(name, str) and name.strip():
        bits.append(f"name {_code(name)}")
    description = data.get("description")
    if isinstance(description, str) and description.strip():
        bits.append(_inline(description)[:200])
    deps: list[str] = []
    for key in ("dependencies", "devDependencies"):
        value = data.get(key)
        if isinstance(value, dict):
            deps.extend(str(item) for item in value)
    unique = sorted(set(deps))
    if unique:
        shown = ", ".join(_code(item) for item in unique[:40])
        if len(unique) > 40:
            shown += ", ..."
        bits.append(f"dependencies {shown}")
    return ", ".join(bits)


def _pyproject_detail(text: str) -> str:
    match = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    section = match.group(1) if match else ""
    bits: list[str] = []
    name = re.search(r"(?m)^name\s*=\s*['\"]([^'\"]+)['\"]", section)
    if name:
        bits.append(f"name {_code(name.group(1))}")
    deps_block = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)\]", section)
    if deps_block:
        raw_deps = re.findall(r"['\"]([^'\"]+)['\"]", deps_block.group(1))
        names = []
        for dep in raw_deps:
            names.append(re.split(r"[<>=\[]", dep, maxsplit=1)[0].strip())
        names = [item for item in names if item]
        if names:
            shown = ", ".join(_code(item) for item in names[:40])
            bits.append(f"dependencies {shown}")
    return ", ".join(bits)


def _requirements_detail(text: str) -> str:
    names: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=\[]", line, maxsplit=1)[0].strip()
        if name:
            names.append(name)
    if not names:
        return ""
    return "dependencies " + ", ".join(_code(name) for name in names[:40])


def _go_mod_detail(text: str) -> str:
    module = re.search(r"(?m)^module\s+(\S+)", text)
    requires = re.findall(r"(?m)^[ \t]+([^\s]+)\s+v\d", text)
    requires += re.findall(r"(?m)^require\s+([^\s]+)\s+v\d", text)
    bits: list[str] = []
    if module:
        bits.append(f"module {_code(module.group(1))}")
    unique = sorted(set(requires))
    if unique:
        shown = ", ".join(_code(name) for name in unique[:40])
        bits.append(f"requires {shown}")
    return ", ".join(bits)


def _toml_name(text: str) -> str:
    match = re.search(r'(?ms)^\[package\]\s*(.*?)(?=^\[|\Z)', text)
    section = match.group(1) if match else text
    name = re.search(r'(?m)^name\s*=\s*"([^"]+)"', section)
    if name is None:
        return ""
    return f"name {_code(name.group(1))}"


def _gemfile_detail(text: str) -> str:
    gems = re.findall(r"""(?m)^\s*gem\s+['"]([^'"]+)['"]""", text)
    if not gems:
        return ""
    return "gems " + ", ".join(_code(name) for name in gems[:40])


def _pom_detail(text: str) -> str:
    match = re.search(r"<artifactId>([^<]+)</artifactId>", text)
    if match is None:
        return ""
    return f"artifact {_code(match.group(1).strip())}"


def _docker_detail(text: str) -> str:
    images = re.findall(r"(?mi)^FROM\s+(\S+)", text)
    if not images:
        return ""
    return "FROM " + ", ".join(_code(image) for image in images[:10])


def _makefile_detail(text: str) -> str:
    targets: list[str] = []
    seen: set[str] = set()
    for target in re.findall(r"(?m)^([A-Za-z][A-Za-z0-9_-]*):", text):
        if target in seen:
            continue
        seen.add(target)
        targets.append(target)
    if not targets:
        return ""
    shown = ", ".join(_code(target) for target in targets[:30])
    if len(targets) > 30:
        shown += ", ..."
    return f"targets {shown}"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            return _inline(cleaned)[:120]
    return ""


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIR_NAMES)
        current = Path(dirpath)
        if current.is_symlink():
            dirnames[:] = []
            continue
        for name in filenames:
            path = current / name
            if path.is_symlink() or not path.is_file():
                continue
            files.append(path)
    return files


def _read_limited(path: Path, limit: int) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _noun(count: int, singular: str) -> str:
    word = singular if count == 1 else f"{singular}s"
    return f"{count} {word}"


def _inline(value: str) -> str:
    text = " ".join(value.split())
    text = text.replace("\\", "\\\\")
    for char in ("*", "_", "`", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text


def _code(value: str) -> str:
    cleaned = " ".join(value.split()).replace("`", "'")
    return f"`{cleaned}`"


if __name__ == "__main__":
    raise SystemExit(main())
