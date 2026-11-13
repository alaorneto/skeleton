"""SemVer, version files, changelog, signals and the idempotent release (#211)."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

import release  # noqa: E402
from release import Context, ReleaseError  # noqa: E402
from semver import Version, VersionError, bump, effective_level, max_level  # noqa: E402
from workflow_config import load  # noqa: E402

REPO = "owner/project"
CONFIG = (
    (SKILL / "assets" / "dev-workflow.toml")
    .read_text(encoding="utf-8")
    .replace("[versioning]\nenabled = false", "[versioning]\nenabled = true")
    .replace(
        "files = []",
        'files = [{ path = "pyproject.toml", regex = \'^version = "(?P<version>[^"]+)"\', replace = \'version = "{version}"\' },'
        ' { path = "docs/version.txt", search = "versão {version}.", replace = "versão {version}." }]',
    )
    .replace(
        "floors = []",
        'floors = [{ path = "floor.py", regex = \'MINIMUM = "(?P<version>[^"]+)"\', replace = \'MINIMUM = "{version}"\' }]',
    )
)


class SemverTest(unittest.TestCase):
    def test_parse_and_precedence(self):
        ordered = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ]
        versions = [Version.parse(text) for text in ordered]
        self.assertEqual(sorted(reversed(versions)), versions)
        self.assertEqual(Version.parse("1.0.0+build.5"), Version.parse("1.0.0"))
        self.assertEqual(str(Version.parse("2.3.4-rc.1+sha.abc")), "2.3.4-rc.1+sha.abc")
        for invalid in ("1.0", "01.0.0", "1.0.0-01", "v1.0.0", "1.0.0-"):
            with self.subTest(invalid=invalid), self.assertRaises(VersionError):
                Version.parse(invalid)

    def test_next_number_and_zero_rule(self):
        current = Version.parse("0.5.3")
        self.assertEqual(str(bump(current, "patch")), "0.5.4")
        self.assertEqual(str(bump(current, "minor")), "0.6.0")
        self.assertEqual(effective_level(current, "major"), "minor")
        self.assertEqual(effective_level(current, "major", explicit=True), "major")
        self.assertEqual(str(bump(current, "major")), "1.0.0")
        self.assertEqual(effective_level(Version.parse("1.2.3"), "major"), "major")
        self.assertEqual(max_level("patch", "minor", "patch"), "minor")
        self.assertEqual(max_level(), "none")

    def test_prerelease_counts_up_and_releases_the_same_target(self):
        rc1 = bump(Version.parse("0.5.0"), "minor", prerelease="rc")
        self.assertEqual(str(rc1), "0.6.0-rc.1")
        self.assertEqual(str(bump(rc1, "minor", prerelease="rc")), "0.6.0-rc.2")
        self.assertEqual(str(bump(rc1, "patch")), "0.6.0")
        self.assertEqual(str(bump(Version.parse("1.0.0-rc.1"), "major")), "1.0.0")


class Repository:
    """A scratch repository with a bare origin, a fake gh and the release configuration."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name).resolve()
        self.origin = base / "origin.git"
        self.root = base / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        self.git_cmd("remote", "add", "origin", str(self.origin))
        (self.root / ".github").mkdir()
        (self.root / ".github" / "dev-workflow.toml").write_text(CONFIG, encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0.5.0"\n', encoding="utf-8")
        (self.root / "docs" / "version.txt").write_text("Esta é a versão 0.5.0.\n", encoding="utf-8")
        (self.root / "floor.py").write_text('MINIMUM = "0.4.0"\n', encoding="utf-8")
        self.commit("chore: start\n\nRefs #1", add=True)
        self.git_cmd("push", "-q", "origin", "main")
        self.gh = FakeGh()
        self.context = Context(
            load(self.root),
            self.root,
            REPO,
            release._process("git", self.root),
            self.gh,
            lambda: datetime(2026, 11, 13, tzinfo=UTC),
        )

    def git_cmd(self, *args):
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
        }
        import os

        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True, env={**os.environ, **env}
        ).stdout.strip()

    def commit(self, message, *, add=False, change=None):
        if change:
            path, text = change
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(text, encoding="utf-8")
            add = True
        if add:
            self.git_cmd("add", "-A")
        self.git_cmd("commit", "-q", "--allow-empty", "-m", message)
        # The release runs with the user's identity configured in the repository.
        self.git_cmd("config", "user.name", "t")
        self.git_cmd("config", "user.email", "t@example.test")
        return self.git_cmd("rev-parse", "HEAD")

    def merge(self, number, title, *, labels=(), closes=(), issue_labels=None, body_extra="", change=None):
        sha = self.commit(f"{title} (#{number})", change=change or (f"src/{number}.txt", title))
        body = "\n".join(f"Closes #{issue}" for issue in closes) + ("\n" + body_extra if body_extra else "")
        self.gh.pulls[number] = {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": n} for n in labels],
            "user": {"login": "author"},
            "merged_at": "2026-11-13T00:00:00Z",
            "merge_commit_sha": sha,
        }
        for issue in closes:
            self.gh.issues[issue] = {
                "number": issue,
                "title": f"Issue {issue}",
                "labels": [{"name": n} for n in (issue_labels or {}).get(issue, ["bug"])],
            }
        return sha

    def file(self, path):
        return (self.root / path).read_text(encoding="utf-8")

    def tags(self):
        return self.git_cmd("tag", "--list").split()

    def close(self):
        self.directory.cleanup()


class FakeGh:
    def __init__(self):
        self.pulls, self.issues, self.comments = {}, {}, {}
        self.releases, self.created_prs, self.milestones, self.permissions = {}, [], [], {}
        self.fail_release_once = False
        self.calls = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        ok = lambda data="": subprocess.CompletedProcess(
            args, 0, data if isinstance(data, str) else json.dumps(data), ""
        )  # noqa: E731
        missing = subprocess.CompletedProcess(args, 1, "", "not found")
        if args[:2] == ["release", "view"]:
            return ok() if args[2] in self.releases else missing
        if args[:2] == ["release", "create"]:
            if self.fail_release_once:
                self.fail_release_once = False
                return subprocess.CompletedProcess(args, 1, "", "rede indisponível")
            notes = Path(args[args.index("--notes-file") + 1]).read_text(encoding="utf-8")
            self.releases[args[2]] = {"notes": notes, "prerelease": "--prerelease" in args}
            return ok()
        if args[:2] == ["pr", "create"]:
            self.created_prs.append(args)
            return ok("https://github.com/owner/project/pull/99")
        if args[0] != "api":
            raise AssertionError(args)
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        path = next(arg for arg in args[1:] if arg.startswith("repos/"))
        parts = path.split("?")[0].split("/")
        if parts[3:4] == ["pulls"]:
            number = int(parts[4])
            return ok(self.pulls[number]) if number in self.pulls else missing
        if parts[3:4] == ["collaborators"]:
            return ok({"permission": self.permissions.get(parts[4], "read")})
        if parts[3:4] == ["milestones"]:
            if method == "POST":
                milestone = {"number": len(self.milestones) + 1, "title": args[-1].split("=", 1)[1]}
                self.milestones.append(milestone)
                return ok(milestone)
            return ok(self.milestones)
        if parts[3:4] == ["issues"]:
            number = int(parts[4])
            if parts[5:6] == ["comments"]:
                if method == "POST":
                    body = args[-1].split("=", 1)[1]
                    self.comments.setdefault(number, []).append(
                        {"body": body, "user": {"login": "github-actions[bot]"}}
                    )
                    return ok({})
                return ok(self.comments.get(number, []))
            if method == "PATCH":
                return ok({})
            if number in self.issues:
                return ok(self.issues[number])
            if number in self.pulls:
                return ok({"number": number, "pull_request": {}})
            return missing
        raise AssertionError(args)


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repository()
        self.addCleanup(self.repo.close)
        release.baseline(self.repo.context, "0.5.0")

    def run_release(self, trigger, **options):
        with redirect_stdout(io.StringIO()):
            return release.run(self.repo.context, trigger=trigger, **options)

    def test_accumulated_pull_requests_enter_the_next_release(self):
        self.repo.merge(2, "fix: corrige a busca", closes=(10,))
        self.assertEqual(self.run_release(2), [])  # Without the label it accumulates.
        self.assertEqual(self.repo.tags(), ["v0.5.0"])
        self.repo.merge(3, "feat: nova busca", labels=("release",), closes=(11,), issue_labels={11: ["feature"]})
        done = self.run_release(3)
        self.assertIn("commit chore(release): v0.6.0", done)
        self.assertIn("v0.6.0", self.repo.tags())
        self.assertIn('version = "0.6.0"', self.repo.file("pyproject.toml"))
        self.assertIn("versão 0.6.0.", self.repo.file("docs/version.txt"))
        changelog = self.repo.file("CHANGELOG.md")
        self.assertIn("## [0.6.0](https://github.com/owner/project/compare/v0.5.0...v0.6.0) — 2026-11-13", changelog)
        self.assertIn("### Correções", changelog)
        self.assertIn("### Funcionalidades", changelog)
        self.assertIn("[#2](https://github.com/owner/project/pull/2)", changelog)
        self.assertIn("v0.6.0", self.repo.gh.releases)
        self.assertEqual(release.check_files(self.repo.context.config, self.repo.root), [])
        remote_tags = subprocess.run(
            ["git", "ls-remote", "--tags", str(self.repo.origin)], capture_output=True, text=True
        ).stdout
        self.assertIn("refs/tags/v0.6.0", remote_tags)
        for number in (2, 3, 10, 11):
            markers = [c for c in self.repo.gh.comments.get(number, []) if "dev-workflow:released v0.6.0" in c["body"]]
            self.assertEqual(len(markers), 1, number)

    def test_levels_patch_minor_major_and_the_highest_wins(self):
        cases = [
            (["fix: a"], {}, "0.5.1"),
            (["fix: a", "feat: b"], {}, "0.6.0"),
            (["feat!: quebra"], {}, "0.6.0"),  # 0.x: a break raises the minor.
        ]
        for subjects, _, expected in cases:
            with self.subTest(expected=expected):
                repo = Repository()
                self.addCleanup(repo.close)
                release.baseline(repo.context, "0.5.0")
                for index, subject in enumerate(subjects, 2):
                    repo.merge(index, subject, labels=("release",) if index == len(subjects) + 1 else ())
                self.assertEqual(str(release.plan(repo.context, trigger=len(subjects) + 1).next), expected)

    def test_label_override_wins_and_its_reason_is_in_the_notes(self):
        self.repo.merge(2, "fix: a", labels=("release", "semver:major"), body_extra="Motivo: primeira versão estável")
        self.run_release(2)
        self.assertIn("v1.0.0", self.repo.tags())
        self.assertIn("primeira versão estável", self.repo.gh.releases["v1.0.0"]["notes"])

    def test_override_requires_a_reason_and_a_single_label(self):
        self.repo.merge(2, "fix: a", labels=("release", "semver:minor"))
        with self.assertRaises(ReleaseError):
            release.plan(self.repo.context, trigger=2)
        self.repo.gh.pulls[2]["labels"] += [{"name": "semver:patch"}]
        self.repo.gh.pulls[2]["body"] = "Motivo: x"
        with self.assertRaises(ReleaseError):
            release.plan(self.repo.context, trigger=2)

    def test_rerun_after_success_does_nothing(self):
        self.repo.merge(2, "fix: a", labels=("release",))
        self.run_release(2)
        head, comments = self.repo.git_cmd("rev-parse", "HEAD"), json.dumps(self.repo.gh.comments)
        self.assertEqual(self.run_release(2), [])
        self.assertEqual(self.repo.git_cmd("rev-parse", "HEAD"), head)
        self.assertEqual(json.dumps(self.repo.gh.comments), comments)
        self.assertEqual(sorted(self.repo.gh.releases), ["v0.5.1"])

    def test_rerun_after_a_partial_failure_finishes(self):
        self.repo.merge(2, "fix: a", labels=("release",))
        self.repo.gh.fail_release_once = True
        with self.assertRaises(ReleaseError):
            self.run_release(2)
        self.assertIn("v0.5.1", self.repo.tags())
        self.assertNotIn("v0.5.1", self.repo.gh.releases)
        self.assertEqual(self.run_release(2), ["release v0.5.1"])
        self.assertIn("v0.5.1", self.repo.gh.releases)
        self.assertEqual(len([c for c in self.repo.gh.comments[2] if "released v0.5.1" in c["body"]]), 1)

    def test_existing_tag_is_an_error(self):
        self.repo.merge(2, "fix: a", labels=("release",))
        self.repo.git_cmd("tag", "-a", "v0.5.1", "HEAD~1", "-m", "manual")
        with self.assertRaises(ReleaseError):
            self.run_release(2)

    def test_untrusted_analysis_is_ignored_and_trusted_one_raises(self):
        self.repo.merge(2, "fix: a", labels=("release",))
        marker = "<!-- dev-workflow:semver-analysis level=minor -->"
        self.repo.gh.comments[2] = [{"body": marker, "user": {"login": "stranger"}}]
        self.assertEqual(str(release.plan(self.repo.context, trigger=2).next), "0.5.1")
        self.repo.gh.permissions["maintainer"] = "write"
        self.repo.gh.comments[2].append({"body": marker, "user": {"login": "maintainer"}})
        self.assertEqual(str(release.plan(self.repo.context, trigger=2).next), "0.6.0")

    def test_floor_and_public_surface_signals(self):
        config = self.repo.context.config
        self.repo.merge(2, "fix: a", labels=("release",), change=("floor.py", 'MINIMUM = "0.6.0"\n'))
        self.assertEqual(str(release.plan(self.repo.context, trigger=2).next), "0.6.0")
        self.assertTrue(release.check_files(config, self.repo.root))  # The floor is above 0.5.0.

    def test_check_reports_a_file_out_of_sync(self):
        self.assertEqual(release.check_files(self.repo.context.config, self.repo.root), [])
        (self.repo.root / "pyproject.toml").write_text('version = "0.4.9"\n', encoding="utf-8")
        problems = release.check_files(self.repo.context.config, self.repo.root)
        self.assertTrue(any("pyproject.toml" in problem for problem in problems))

    def test_release_pr_mode_and_prerelease(self):
        self.repo.merge(2, "feat: a", labels=("release",))
        done = self.run_release(2, prerelease="rc")
        self.assertIn("v0.6.0-rc.1", self.repo.tags())
        self.assertTrue(self.repo.gh.releases["v0.6.0-rc.1"]["prerelease"])
        self.assertIn("commit chore(release): v0.6.0-rc.1", done)
        repo = Repository()
        self.addCleanup(repo.close)
        config = load(repo.root)
        repo.context.config = replace(config, versioning=replace(config.versioning, mode="pr"))
        release.baseline(repo.context, "0.5.0")
        repo.merge(2, "fix: a", labels=("release",))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(release.run(repo.context, trigger=2), ["PR de release v0.5.1"])
        self.assertEqual(repo.git_cmd("rev-parse", "--abbrev-ref", "HEAD"), "release/v0.5.1")
        self.assertNotIn("v0.5.1", repo.tags())
        self.assertEqual(len(repo.gh.created_prs), 1)

    def test_preview_says_accumulated_or_the_number_and_updates_one_comment(self):
        self.repo.merge(2, "feat: a")
        text = release.preview_text(self.repo.context, 2)
        self.assertIn("fica acumulado; com ele, sairia **v0.6.0** (minor)", text)
        self.repo.gh.pulls[2]["labels"] = [{"name": "release"}]
        self.assertIn("o merge publica **v0.6.0**", release.preview_text(self.repo.context, 2))
        comments = self.repo.gh.comments
        self.assertEqual(release.upsert_preview(self.repo.context, 2, text), "created")
        comments[2][0]["id"] = 77
        patched = []
        original = self.repo.gh.__call__

        def gh(args):
            if "PATCH" in args:
                patched.append(args)
                return subprocess.CompletedProcess(args, 0, "{}", "")
            return original(args)

        self.repo.context.gh = gh
        self.assertEqual(release.upsert_preview(self.repo.context, 2, text), "updated")
        self.assertTrue(patched and "issues/comments/77" in " ".join(patched[0]))
        self.assertEqual(len(comments[2]), 1)

    def test_without_the_release_label_nothing_is_versioned(self):
        self.repo.merge(2, "feat: a")
        self.assertEqual(self.run_release(2), [])
        self.assertEqual(self.repo.tags(), ["v0.5.0"])
        self.assertNotIn("CHANGELOG.md", [path.name for path in self.repo.root.iterdir()])


if __name__ == "__main__":
    unittest.main()
