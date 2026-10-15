from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.git import (
    Checkout,
    GitError,
    RepoMeta,
    clone_command,
    clone_environment,
    clone_repository,
    extract_repositories,
    main,
    parse_args,
    parse_github_url,
    read_repo_list,
    summarize,
)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _meta() -> RepoMeta:
    return RepoMeta(
        url="https://github.com/acme/widgets",
        owner="acme",
        name="widgets",
        branch="main",
        commit="abc",
        committed_at="2024-01-02T03:04:05+00:00",
        subject="Add widgets",
    )


def _checkout() -> Checkout:
    return Checkout(
        commit="a" * 40,
        branch="main",
        committed_at="2024-01-02T03:04:05+00:00",
        subject="Add widgets",
    )


class UrlTests(unittest.TestCase):
    def test_https_ssh_and_tree_urls_normalize(self):
        expected = "https://github.com/acme/widgets"
        for raw in (
            "https://github.com/acme/widgets",
            "https://github.com/acme/widgets.git",
            "https://github.com/acme/widgets.git/",
            "https://github.com/acme/widgets/",
            "https://github.com/acme/widgets/tree/main/src",
            "git@github.com:acme/widgets.git",
            "ssh://git@github.com/acme/widgets.git",
        ):
            repository = parse_github_url(raw)
            self.assertEqual(repository.page_url, expected)
            self.assertEqual(repository.clone_url, expected + ".git")
            self.assertEqual((repository.owner, repository.name), ("acme", "widgets"))

    def test_rejects_other_hosts_and_credentials(self):
        with self.assertRaises(GitError):
            parse_github_url("https://gitlab.com/acme/widgets")
        with self.assertRaisesRegex(GitError, "credentials"):
            parse_github_url("https://user:secret@github.com/acme/widgets")
        with self.assertRaises(GitError):
            parse_github_url("https://github.com/acme")


class RepoListTests(unittest.TestCase):
    def test_skips_blanks_and_comments(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "repos.txt"
            path.write_text(
                "# comment\n\nhttps://github.com/acme/widgets\n  \n",
                encoding="utf-8",
            )
            self.assertEqual(read_repo_list(path), ["https://github.com/acme/widgets"])

    def test_empty_file_is_an_error(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "repos.txt"
            path.write_text("# nothing\n\n", encoding="utf-8")
            with self.assertRaises(GitError):
                read_repo_list(path)


class SummaryTests(unittest.TestCase):
    def test_summary_is_facts_without_secret_values(self):
        readme = "# Hello\n\nBody\n"
        app = 'print("hi")\n'
        terraform = 'resource "aws_iam_role" "exec" {}\n'
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "README.md", readme)
            _write(root, "src/app.py", app)
            _write(root, "node_modules/pkg/index.js", "console.log(1)\n")
            _write(root, "infra/main.tf", terraform)
            _write(root, "infra/terraform.tfvars", 'password = "super-secret-tfvars"\n')
            text = summarize(
                root,
                RepoMeta(
                    url="https://github.com/acme/widgets",
                    owner="acme",
                    name="widgets",
                    branch="main",
                    commit="a" * 40,
                    committed_at="2024-01-02T03:04:05+00:00",
                    subject="Add *widgets*",
                ),
            )
        self.assertEqual(
            text,
            f"""\
# acme/widgets

- **URL:** https://github.com/acme/widgets
- **Default branch:** main
- **Commit:** {"a" * 40}
- **Commit date:** 2024-01-02T03:04:05+00:00
- **Subject:** Add \\*widgets\\*

## README

# Hello

Body

## Languages

- HCL: 1 file, {len(terraform.encode())} bytes
- Markdown: 1 file, {len(readme.encode())} bytes
- Python: 1 file, {len(app.encode())} bytes

## Layout

```
infra/
  main.tf
  terraform.tfvars
src/
  app.py
README.md
```

## Project files

_No project files._

## Terraform

### infra

- **Files:** `infra/main.tf`

_No containers recognized. Resource types: aws_iam_role._

## CDK

_No CDK apps._
""",
        )
        self.assertNotIn("super-secret", text)
        self.assertNotIn("console.log", text)

    def test_project_files_are_compact_facts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root,
                "package.json",
                """\
{
  "name": "widgets",
  "description": "Billing export",
  "dependencies": {"express": "1.0.0"},
  "devDependencies": {"typescript": "5.0.0"}
}
""",
            )
            _write(root, "Dockerfile", "FROM python:3.12-slim\nFROM golang:1.22 AS build\n")
            _write(root, "Makefile", "test:\n\tpython -m unittest\n\ngit:\n\ttrue\n")
            _write(root, ".github/workflows/ci.yml", "name: ci\n")
            text = summarize(
                root,
                RepoMeta(
                    url="https://github.com/acme/widgets",
                    owner="acme",
                    name="widgets",
                    branch="main",
                    commit="abc",
                    committed_at="2024-01-02T03:04:05+00:00",
                    subject="Add widgets",
                ),
            )
        self.assertIn(
            "- `package.json`: name `widgets`, Billing export, dependencies `express`, `typescript`",
            text,
        )
        self.assertIn("- `Dockerfile`: FROM `python:3.12-slim`, `golang:1.22`", text)
        self.assertIn("- `Makefile`: targets `test`, `git`", text)
        self.assertIn("- `.github/workflows/ci.yml`", text)
        self.assertNotIn("1.0.0", text)

    def test_long_readme_is_truncated(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("a" * 80_001, encoding="utf-8")
            text = summarize(root, _meta())
        self.assertIn("_README truncated._", text)
        self.assertNotIn("a" * 80_001, text)


class ExtractTests(unittest.TestCase):
    def test_writes_one_file_per_repository_and_hides_the_token(self):
        seen: list[tuple[str, str | None]] = []

        def clone(url: str, dest: Path, token: str | None) -> Checkout:
            seen.append((url, token))
            dest.mkdir()
            (dest / "README.md").write_text("# Widgets\n", encoding="utf-8")
            return _checkout()

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = extract_repositories(
                [
                    "https://github.com/acme/widgets",
                    "https://github.com/acme/widgets.git",
                    "https://gitlab.com/acme/other",
                    "https://user:super-secret@github.com/acme/private",
                ],
                out,
                "super-secret",
                clone,
            )
            text = (out / "acme-widgets.md").read_text(encoding="utf-8")

        self.assertEqual(seen, [("https://github.com/acme/widgets.git", "super-secret")])
        self.assertEqual([path.name for path in result.wrote], ["acme-widgets.md"])
        self.assertEqual(len(result.failed), 2)
        self.assertIn("not a GitHub repository URL", result.failed[0][1])
        self.assertNotIn("super-secret", result.failed[1][0])
        self.assertIn("credentials", result.failed[1][1])
        self.assertIn("- **URL:** https://github.com/acme/widgets\n", text)
        self.assertNotIn("super-secret", text)
        self.assertNotIn(".git", text.splitlines()[2])

    def test_clone_failure_is_recorded(self):
        def clone(url: str, dest: Path, token: str | None) -> Checkout:
            raise GitError("authentication failed")

        with TemporaryDirectory() as tmp:
            result = extract_repositories(
                ["https://github.com/acme/widgets"],
                Path(tmp),
                None,
                clone,
            )
        self.assertEqual(result.wrote, [])
        self.assertEqual(
            result.failed,
            [("https://github.com/acme/widgets", "authentication failed")],
        )


class CloneTests(unittest.TestCase):
    def test_command_and_askpass_do_not_embed_the_token(self):
        command = clone_command("https://github.com/acme/widgets.git", Path("repo"))
        self.assertNotIn("super-secret", command)
        self.assertEqual(command[0:3], ["git", "-c", "credential.helper="])
        self.assertIn("--depth", command)
        with clone_environment("super-secret") as env:
            script = Path(env["GIT_ASKPASS"]).read_text(encoding="utf-8")
            self.assertEqual(env["GIT_TOKEN"], "super-secret")
            self.assertNotIn("super-secret", script)
            self.assertNotIn("super-secret", " ".join(command))

    def test_shallow_clone_reads_head(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            (src / "README.md").write_text("# Hello\n", encoding="utf-8")
            _git(src, "init", "-b", "main")
            _git(src, "add", "README.md")
            _git(
                src,
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "-m",
                "Add readme",
            )
            checkout = clone_repository(src.as_uri(), root / "dest", None)
        self.assertEqual(checkout.branch, "main")
        self.assertEqual(checkout.subject, "Add readme")
        self.assertEqual(len(checkout.commit), 40)


class MainTests(unittest.TestCase):
    def test_requires_one_of_url_or_repos(self):
        with self.assertRaises(SystemExit):
            parse_args([])
        with self.assertRaises(SystemExit):
            parse_args(["--url", "https://github.com/acme/widgets", "--repos", "repos.txt"])

    def test_main_writes_from_a_repos_file(self):
        def clone(url: str, dest: Path, token: str | None) -> Checkout:
            dest.mkdir()
            (dest / "README.md").write_text("# Widgets\n", encoding="utf-8")
            return _checkout()

        previous = os.environ.get("GIT_TOKEN")
        os.environ["GIT_TOKEN"] = "super-secret"
        try:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                repos = root / "repos.txt"
                repos.write_text(
                    "# comment\n\nhttps://github.com/acme/widgets\n",
                    encoding="utf-8",
                )
                code = main(["--repos", os.fspath(repos), "--out", os.fspath(root / "out")], clone)
                text = (root / "out" / "acme-widgets.md").read_text(encoding="utf-8")
        finally:
            if previous is None:
                os.environ.pop("GIT_TOKEN", None)
            else:
                os.environ["GIT_TOKEN"] = previous
        self.assertEqual(code, 0)
        self.assertIn("# Widgets", text)
        self.assertNotIn("super-secret", text)

    def test_missing_repos_file_exits_with_status_2(self):
        self.assertEqual(main(["--repos", "missing-repos.txt"]), 2)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
