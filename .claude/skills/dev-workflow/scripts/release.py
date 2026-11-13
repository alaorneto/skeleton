"""Semantic versions, changelog, tag and GitHub Release after a merge (skill dev-workflow, #211).

Only the standard library, ``git`` and ``gh``. The single source of the version is the last
version tag reachable from HEAD; the files of ``versioning.files`` are projections written by
this tool. Pull requests without the ``release`` label accumulate; the next one with it releases
everything since the tag. State is derived from tags, the release commit and the GitHub Release,
so ``run`` is idempotent: after success it does nothing, after a partial failure it finishes.

Subcommands:

- ``status``: current version, commits and pull requests accumulated since the tag.
- ``plan [--pr N] [--prerelease ID] [--json]``: level, signals and the next version.
- ``next [--pr N] [--prerelease ID]``: only the next number.
- ``check``: the declared files carry the current version; no floor is above it.
- ``bump VERSION``: writes VERSION into the declared files (no commit).
- ``run [--pr N] [--prerelease ID] [--dry-run]``: the release itself.
- ``baseline VERSION [REF]``: the base tag (an owner step, once).
- ``record-analysis --pr N --level L --reason TEXT``: the agent's analysis, as a PR comment.
- ``preview --pr N``: the version this pull request would release with the label (#212).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from semver import LEVELS, Version, VersionError, bump, effective_level, max_level  # noqa: E402
from workflow_config import Config, ConfigError, VersionFile, load  # noqa: E402

RELEASE_SUBJECT = re.compile(r"^chore\(release\): (?P<tag>\S+)$")
CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?(?P<breaking>!)?: .+")
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE: ", re.MULTILINE)
SQUASH_PR = re.compile(r"\(#(?P<number>\d+)\)\s*$")
MERGE_PR = re.compile(r"^Merge pull request #(?P<number>\d+)")
ANALYSIS_MARKER = re.compile(r"<!-- dev-workflow:semver-analysis level=(?P<level>none|patch|minor|major) -->")
RELEASED_MARKER = "<!-- dev-workflow:released {tag} -->"
PREVIEW_MARKER = "<!-- dev-workflow:version-preview -->"
REASON = re.compile(r"(?im)^\s*(?:[-*]\s*)?(?:motivo|reason)\s*:\s*(?P<reason>\S.*)$")
TRUSTED_PERMISSIONS = {"admin", "maintain", "write"}


class ReleaseError(RuntimeError):
    pass


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _process(command: str, root: Path) -> Runner:
    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [command, *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=False
            )
        except OSError as exc:
            return subprocess.CompletedProcess([command, *args], 127, "", str(exc))

    return run


@dataclass
class Context:
    """Everything a release reads or writes, injectable for tests."""

    config: Config
    root: Path
    repository: str
    git: Runner
    gh: Runner
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def run_git(self, *args: str) -> str:
        completed = self.git(list(args))
        if completed.returncode != 0:
            raise ReleaseError(f"git {' '.join(args)}: {completed.stderr.strip() or completed.stdout.strip()}")
        return completed.stdout.strip()

    def run_gh(self, *args: str) -> str:
        completed = self.gh(list(args))
        if completed.returncode != 0:
            raise ReleaseError(f"gh {' '.join(args[:3])}: {completed.stderr.strip() or completed.stdout.strip()}")
        return completed.stdout

    def gh_json(self, *args: str) -> object:
        text = self.run_gh(*args)
        return json.loads(text) if text.strip() else None


# --- version files -----------------------------------------------------------------------------


def _read(root: Path, item: VersionFile) -> str:
    path = root / item.path
    if not path.is_file():
        raise ReleaseError(f"{item.path}: arquivo de versão inexistente.")
    return path.read_text(encoding="utf-8")


def file_versions(root: Path, item: VersionFile) -> list[str]:
    """Every version the entry finds in its file (regex group ``version`` or the search pattern)."""
    text = _read(root, item)
    if item.regex is not None:
        return [match.group("version") for match in re.finditer(item.regex, text, re.MULTILINE)]
    before, _, after = item.search.partition("{version}")  # type: ignore[union-attr]
    version = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+?)?(?:\+[0-9A-Za-z.-]+?)?"
    pattern = re.escape(before) + rf"(?P<version>{version})" + re.escape(after)
    return [match.group("version") for match in re.finditer(pattern, text)]


def placeholders(version: str) -> dict[str, object]:
    """``{version}`` and the derived parts a range needs, such as ``<{major}.{next_minor}.0``."""
    parsed = Version.parse(version)
    return {
        "version": version,
        "major": parsed.major,
        "minor": parsed.minor,
        "patch": parsed.patch,
        "next_minor": parsed.minor + 1,
        "next_major": parsed.major + 1,
    }


def write_version(root: Path, item: VersionFile, current: str, new: str) -> bool:
    """Rewrite the entry from current to new; False when the file already carries new."""
    text = _read(root, item)
    replacement = item.replace.format(**placeholders(new))
    if item.regex is not None:
        compiled = re.compile(item.regex, re.MULTILINE)
        matches = list(compiled.finditer(text))
        if not matches:
            raise ReleaseError(f"{item.path}: o padrão de versão não foi encontrado.")
        if all(match.group("version") == new for match in matches):
            return False
        if any(match.group("version") != current for match in matches):
            raise ReleaseError(f"{item.path}: a versão no arquivo diverge de {current}.")
        updated = compiled.sub(lambda match: replacement, text)
    else:
        old = item.search.format(**placeholders(current))  # type: ignore[union-attr]
        if old not in text:
            if item.search.format(**placeholders(new)) in text:  # type: ignore[union-attr]
                return False
            raise ReleaseError(f"{item.path}: {old!r} não foi encontrado.")
        updated = text.replace(old, replacement)
    (root / item.path).write_text(updated, encoding="utf-8", newline="")
    return True


def check_files(config: Config, root: Path, version: str | None = None) -> list[str]:
    """Problems of the projections and floors against the current (or given) version."""
    problems: list[str] = []
    if version is None:
        try:
            version = str(declared_version(Context(config, root, "", _process("git", root), _process("gh", root))))
        except (ReleaseError, VersionError) as exc:
            return [str(exc)]
    published = Version.parse(version)
    for item in config.versioning.files:
        try:
            found = file_versions(root, item)
        except ReleaseError as exc:
            problems.append(str(exc))
            continue
        if not found:
            problems.append(f"{item.path}: o padrão de versão não foi encontrado.")
        elif any(value != version for value in found):
            problems.append(f"{item.path}: {', '.join(sorted(set(found)))} diverge de {version}.")
    for item in config.versioning.floors:
        try:
            values = file_versions(root, item)
        except ReleaseError as exc:
            problems.append(str(exc))
            continue
        for value in values:
            try:
                floor = Version.parse(value)
            except VersionError:
                problems.append(f"{item.path}: piso {value!r} não é SemVer.")
                continue
            if floor > published:
                problems.append(f"{item.path}: piso {value} acima da versão publicada {version}.")
    return problems


# --- accumulation ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str = ""


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    author: str
    merge_sha: str | None = None


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Signal:
    level: str
    source: str
    detail: str


@dataclass
class Plan:
    current: Version
    tag: str
    level: str
    next: Version
    signals: list[Signal]
    override: tuple[str, str] | None  # (level, reason)
    commits: list[Commit]
    pulls: list[PullRequest]
    issues: dict[int, Issue]
    trigger: PullRequest | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "current": str(self.current),
            "level": self.level,
            "next": str(self.next),
            "override": {"level": self.override[0], "reason": self.override[1]} if self.override else None,
            "signals": [signal.__dict__ for signal in self.signals],
            "pulls": [pull.number for pull in self.pulls],
            "issues": sorted(self.issues),
            "commits": len(self.commits),
        }


def last_tag(context: Context) -> str | None:
    prefix = context.config.versioning.tag_prefix
    completed = context.git(["describe", "--tags", "--abbrev=0", "--match", f"{prefix}[0-9]*"])
    return completed.stdout.strip() if completed.returncode == 0 and completed.stdout.strip() else None


def current_version(context: Context) -> Version:
    tag = last_tag(context)
    if tag is None:
        raise ReleaseError("nenhuma tag de versão alcançável: o responsável cria a de base com `release.py baseline`.")
    return Version.parse(tag[len(context.config.versioning.tag_prefix) :])


def declared_version(context: Context) -> Version:
    """The last reachable tag or, before the base tag exists, the version of the first declared file."""
    if last_tag(context) is not None:
        return current_version(context)
    files = context.config.versioning.files
    found = file_versions(context.root, files[0]) if files else []
    if not found:
        raise ReleaseError("sem tag de base e sem versão no primeiro arquivo de versioning.files.")
    return Version.parse(found[0])


def commits_since(context: Context, tag: str) -> list[Commit]:
    raw = context.run_git("log", "--format=%H%x1f%s%x1f%b%x1e", f"{tag}..HEAD")
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        sha, subject, body = (record.split("\x1f") + ["", ""])[:3]
        commits.append(Commit(sha.strip(), subject.strip(), body.strip()))
    return commits


def pull_numbers(commits: Iterable[Commit]) -> list[int]:
    numbers: list[int] = []
    for commit in commits:
        match = SQUASH_PR.search(commit.subject) or MERGE_PR.match(commit.subject)
        if match:
            numbers.append(int(match.group("number")))
    return list(dict.fromkeys(numbers))


def fetch_pull(context: Context, number: int) -> PullRequest | None:
    completed = context.gh(["api", f"repos/{context.repository}/pulls/{number}"])
    if completed.returncode != 0:
        return None
    data = json.loads(completed.stdout)
    return PullRequest(
        number=int(data["number"]),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        labels=tuple(str(label["name"]) for label in data.get("labels", [])),
        author=str((data.get("user") or {}).get("login") or ""),
        merge_sha=data.get("merge_commit_sha") if data.get("merged_at") or data.get("merged") else None,
    )


def fetch_issue(context: Context, number: int) -> Issue | None:
    completed = context.gh(["api", f"repos/{context.repository}/issues/{number}"])
    if completed.returncode != 0:
        return None
    data = json.loads(completed.stdout)
    if "pull_request" in data:
        return None
    return Issue(
        int(data["number"]), str(data.get("title") or ""), tuple(str(label["name"]) for label in data.get("labels", []))
    )


def _referenced(text: str, repository: str) -> list[int]:
    from github_issues import referenced_issues

    return referenced_issues(text, repository)


def trusted(context: Context, login: str) -> bool:
    if login in context.config.versioning.analysis_authors:
        return True
    completed = context.gh(["api", f"repos/{context.repository}/collaborators/{login}/permission"])
    if completed.returncode != 0:
        return False
    return str(json.loads(completed.stdout).get("permission")) in TRUSTED_PERMISSIONS


def analysis_levels(context: Context, pull: PullRequest) -> list[Signal]:
    """Analyses recorded on the PR by trusted authors; anyone else's comment is ignored."""
    completed = context.gh(["api", f"repos/{context.repository}/issues/{pull.number}/comments", "--paginate"])
    if completed.returncode != 0:
        return []
    signals = []
    for comment in json.loads(completed.stdout or "[]"):
        match = ANALYSIS_MARKER.search(str(comment.get("body") or ""))
        author = str((comment.get("user") or {}).get("login") or "")
        if match and trusted(context, author):
            signals.append(Signal(match.group("level"), "análise", f"#{pull.number} por {author}"))
    return signals


def floor_signals(context: Context, current: Version) -> list[Signal]:
    signals = []
    for item in context.config.versioning.floors:
        for value in file_versions(context.root, item):
            try:
                floor = Version.parse(value)
            except VersionError:
                continue
            if floor.core > current.core:
                level = "major" if floor.major > current.major else "minor" if floor.minor > current.minor else "patch"
                signals.append(Signal(level, "piso", f"{item.path} exige {value}"))
    return signals


def surface_signals(context: Context, tag: str) -> list[Signal]:
    globs = context.config.versioning.public_surfaces
    if not globs:
        return []
    changed = context.run_git("diff", "--name-only", f"{tag}..HEAD").splitlines()
    touched = sorted({path for path in changed for glob in globs if fnmatch.fnmatch(path, glob)})
    return [Signal("minor", "superfície pública", ", ".join(touched[:5]))] if touched else []


def override_of(context: Context, pull: PullRequest | None) -> tuple[str, str] | None:
    if pull is None:
        return None
    prefix = context.config.versioning.override_prefix
    levels = [label[len(prefix) :] for label in pull.labels if label.startswith(prefix)]
    if not levels:
        return None
    if len(levels) > 1 or levels[0] not in LEVELS[1:]:
        raise ReleaseError(f"#{pull.number}: use no máximo um label {prefix}patch|minor|major.")
    reason = REASON.search(pull.body)
    if reason is None:
        raise ReleaseError(f"#{pull.number}: o label {prefix}{levels[0]} exige 'Motivo: ...' no corpo do PR.")
    return levels[0], reason.group("reason").strip()


def plan(context: Context, *, trigger: int | None = None, prerelease: str | None = None) -> Plan:
    tag = last_tag(context)
    if tag is None:
        raise ReleaseError("nenhuma tag de versão alcançável: o responsável cria a de base com `release.py baseline`.")
    current = Version.parse(tag[len(context.config.versioning.tag_prefix) :])
    commits = commits_since(context, tag)
    numbers = pull_numbers(commits)
    if trigger is not None and trigger not in numbers:
        numbers.append(trigger)
    pulls = [pull for number in numbers if (pull := fetch_pull(context, number)) is not None]
    trigger_pull = next((pull for pull in pulls if pull.number == trigger), None)
    issues: dict[int, Issue] = {}
    for pull in pulls:
        for number in _referenced(pull.body, context.repository):
            if number not in issues and (issue := fetch_issue(context, number)) is not None:
                issues[number] = issue
    signals: list[Signal] = []
    for commit in commits:
        if RELEASE_SUBJECT.match(commit.subject):
            continue
        match = CONVENTIONAL.match(commit.subject)
        if match and (match.group("breaking") or BREAKING_FOOTER.search(commit.body)):
            signals.append(Signal("major", "commit", commit.subject))
        elif match and (kind := context.config.type_for_commit(match.group("type"))) is not None:
            signals.append(Signal(kind.level, "commit", commit.subject))
    for issue in issues.values():
        for kind in context.config.types:
            if kind.name in issue.labels:
                signals.append(Signal(kind.level, "issue", f"#{issue.number} {kind.name}"))
    for pull in pulls:
        if context.config.versioning.breaking_label in pull.labels:
            signals.append(Signal("major", "label", f"#{pull.number} {context.config.versioning.breaking_label}"))
        signals.extend(analysis_levels(context, pull))
    signals.extend(surface_signals(context, tag))
    signals.extend(floor_signals(context, current))
    override = override_of(context, trigger_pull)
    computed = max_level("patch", *(signal.level for signal in signals))
    chosen = override[0] if override else computed
    level = effective_level(current, chosen, explicit=bool(override and override[0] == "major"))
    return Plan(
        current=current,
        tag=tag,
        level=level,
        next=bump(current, level, prerelease=prerelease),
        signals=signals,
        override=override,
        commits=commits,
        pulls=pulls,
        issues=issues,
        trigger=trigger_pull,
    )


# --- changelog and notes -----------------------------------------------------------------------


def _type_of(context: Context, pull: PullRequest, issues: dict[int, Issue]) -> str:
    for number in _referenced(pull.body, context.repository):
        issue = issues.get(number)
        if issue:
            for kind in context.config.types:
                if kind.name in issue.labels:
                    return kind.name
    match = CONVENTIONAL.match(pull.title)
    kind = context.config.type_for_commit(match.group("type")) if match else None
    return kind.name if kind else context.config.types[-1].name


def release_notes(context: Context, release: Plan, tag: str) -> str:
    base = f"https://github.com/{context.repository}"
    lines = [
        f"## [{tag[len(context.config.versioning.tag_prefix) :]}]({base}/compare/{release.tag}...{tag}) — {context.now():%Y-%m-%d}",
        "",
    ]
    if release.override:
        lines += [f"Nível {release.override[0]} definido por label: {release.override[1]}", ""]
    groups: dict[str, list[str]] = {}
    for pull in release.pulls:
        if pull.title.startswith("chore(release):"):
            continue
        references = [
            f"[#{n}]({base}/issues/{n})" for n in _referenced(pull.body, context.repository) if n in release.issues
        ]
        entry = f"- {pull.title} ([#{pull.number}]({base}/pull/{pull.number}))"
        if references:
            entry += " — " + ", ".join(references)
        groups.setdefault(_type_of(context, pull, release.issues), []).append(entry)
    if not groups:
        groups[context.config.types[-1].name] = [f"- {commit.subject}" for commit in release.commits[:50]] or [
            "- Sem mudanças listadas."
        ]
    for kind in context.config.types:
        if kind.name in groups:
            lines += [f"### {kind.heading}", "", *groups[kind.name], ""]
    return "\n".join(lines).rstrip() + "\n"


def update_changelog(root: Path, path: str, section: str, version: str) -> bool:
    target = root / path
    text = target.read_text(encoding="utf-8") if target.exists() else "# Changelog\n"
    if f"## [{version}]" in text:
        return False
    head, separator, rest = text.partition("\n## ")
    if separator:
        updated = head.rstrip() + "\n\n" + section + "\n## " + rest
    else:
        updated = text.rstrip() + "\n\n" + section
    target.write_text(updated, encoding="utf-8", newline="")
    return True


# --- marking -----------------------------------------------------------------------------------


def _comments(context: Context, number: int) -> list[dict[str, object]]:
    completed = context.gh(["api", f"repos/{context.repository}/issues/{number}/comments", "--paginate"])
    return json.loads(completed.stdout or "[]") if completed.returncode == 0 else []


def _milestone(context: Context, tag: str) -> int | None:
    milestones = context.gh_json("api", f"repos/{context.repository}/milestones?state=all&per_page=100") or []
    for milestone in milestones:  # type: ignore[union-attr]
        if milestone.get("title") == tag:
            return int(milestone["number"])
    created = context.gh_json("api", "-X", "POST", f"repos/{context.repository}/milestones", "-f", f"title={tag}")
    return int(created["number"]) if isinstance(created, dict) else None  # type: ignore[index]


def mark_included(context: Context, release: Plan, tag: str) -> None:
    """One comment per PR and issue, and the version milestone, never twice."""
    marker = RELEASED_MARKER.format(tag=tag)
    url = f"https://github.com/{context.repository}/releases/tag/{tag}"
    milestone = _milestone(context, tag)
    for number in [pull.number for pull in release.pulls] + sorted(release.issues):
        if not any(marker in str(comment.get("body") or "") for comment in _comments(context, number)):
            context.run_gh(
                "api",
                "-X",
                "POST",
                f"repos/{context.repository}/issues/{number}/comments",
                "-f",
                f"body={marker}\nIncluído em [{tag}]({url}).",
            )
        if milestone is not None:
            context.gh(
                ["api", "-X", "PATCH", f"repos/{context.repository}/issues/{number}", "-F", f"milestone={milestone}"]
            )


# --- run ---------------------------------------------------------------------------------------


def _tag_target(context: Context, tag: str) -> str | None:
    completed = context.git(["rev-list", "-n", "1", tag])
    return completed.stdout.strip() if completed.returncode == 0 else None


def _release_exists(context: Context, tag: str) -> bool:
    return context.gh(["release", "view", tag, "--repo", context.repository]).returncode == 0


def _publish(context: Context, release: Plan, tag: str, *, dry_run: bool, branch: str) -> list[str]:
    """Tag HEAD (the release commit), push, create the GitHub Release and mark what it includes."""
    done: list[str] = []
    head = context.run_git("rev-parse", "HEAD")
    target = _tag_target(context, tag)
    if target is not None and target != head:
        raise ReleaseError(f"a tag {tag} já existe em outro commit ({target[:12]}).")
    if dry_run:
        return [f"[dry-run] tag {tag}, release e marcação"]
    if target is None:
        context.run_git("tag", "-a", tag, "-m", f"Release {tag}")
        done.append(f"tag {tag}")
    context.run_git("push", "origin", f"HEAD:refs/heads/{branch}", f"refs/tags/{tag}")
    if not _release_exists(context, tag):
        notes = release_notes(context, release, tag)
        handle, path = tempfile.mkstemp(suffix=".md")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(notes)
            arguments = [
                "release",
                "create",
                tag,
                "--repo",
                context.repository,
                "--title",
                tag,
                "--notes-file",
                path,
                "--verify-tag",
            ]
            if Version.parse(tag[len(context.config.versioning.tag_prefix) :]).is_prerelease:
                arguments.append("--prerelease")
            context.run_gh(*arguments)
        finally:
            os.unlink(path)
        done.append(f"release {tag}")
    mark_included(context, release, tag)
    return done


def run(context: Context, *, trigger: int | None, prerelease: str | None = None, dry_run: bool = False) -> list[str]:
    """The release after a merge; returns what was done ([] when there was nothing to do)."""
    versioning = context.config.versioning
    if not versioning.enabled:
        return ["versionamento desligado (versioning.enabled = false)"]
    branch = context.run_git("rev-parse", "--abbrev-ref", "HEAD")
    head = context.git(["log", "-1", "--format=%s"]).stdout.strip()
    release_commit = RELEASE_SUBJECT.match(head)
    if release_commit:
        # The release commit is on the branch already (rerun, or the merged release PR).
        tag = release_commit.group("tag")
        previous = context.run_git(
            "describe", "--tags", "--abbrev=0", "--match", f"{versioning.tag_prefix}[0-9]*", "HEAD~1"
        )
        released = plan_between(context, previous, trigger=trigger, tag=tag)
        return _publish(context, released, tag, dry_run=dry_run, branch=branch)
    if trigger is None:
        return []
    pull = fetch_pull(context, trigger)
    if pull is None or versioning.release_label not in pull.labels:
        return []  # Without the label the PR accumulates for the next release.
    tag_now = last_tag(context)
    if (
        pull.merge_sha
        and tag_now
        and context.git(["merge-base", "--is-ancestor", pull.merge_sha, tag_now]).returncode == 0
    ):
        return []  # Already released.
    release = plan(context, trigger=trigger, prerelease=prerelease)
    tag = f"{versioning.tag_prefix}{release.next}"
    if _tag_target(context, tag) is not None:
        raise ReleaseError(f"a tag {tag} já existe: confira a última tag alcançável e o nível calculado.")
    if dry_run:
        return [f"[dry-run] {release.current} → {release.next} ({release.level})"]
    for item in versioning.files:
        write_version(context.root, item, str(release.current), str(release.next))
    update_changelog(context.root, versioning.changelog, release_notes(context, release, tag), str(release.next))
    paths = sorted({item.path for item in versioning.files} | {versioning.changelog})
    subject = f"chore(release): {tag}"
    trailers = "\n".join(f"Refs #{number}" for number in sorted(release.issues)) or f"Refs #{trigger}"
    if versioning.mode == "pr":
        release_branch = f"release/{tag}"
        context.run_git("switch", "-c", release_branch)
        context.run_git("add", "--", *paths)
        context.run_git("commit", "-m", f"{subject}\n\n{trailers}")
        context.run_git("push", "origin", f"HEAD:refs/heads/{release_branch}")
        context.run_gh(
            "pr",
            "create",
            "--repo",
            context.repository,
            "--base",
            branch,
            "--head",
            release_branch,
            "--title",
            subject,
            "--body",
            f"Release {tag} (gerado pelo skill dev-workflow).\n\n{trailers}",
        )
        return [f"PR de release {tag}"]
    context.run_git("add", "--", *paths)
    context.run_git("commit", "-m", f"{subject}\n\n{trailers}")
    return [f"commit {subject}", *_publish(context, release, tag, dry_run=False, branch=branch)]


def plan_between(context: Context, previous: str, *, trigger: int | None, tag: str) -> Plan:
    """The plan of an existing release commit: everything between the previous tag and HEAD~1."""
    current = Version.parse(previous[len(context.config.versioning.tag_prefix) :])
    commits = [commit for commit in commits_since(context, previous) if not RELEASE_SUBJECT.match(commit.subject)]
    numbers = pull_numbers(commits)
    if trigger is not None and trigger not in numbers:
        numbers.append(trigger)
    pulls = [pull for number in numbers if (pull := fetch_pull(context, number)) is not None]
    issues: dict[int, Issue] = {}
    for pull in pulls:
        for number in _referenced(pull.body, context.repository):
            if number not in issues and (issue := fetch_issue(context, number)) is not None:
                issues[number] = issue
    version = Version.parse(tag[len(context.config.versioning.tag_prefix) :])
    return Plan(current, previous, "patch", version, [], None, commits, pulls, issues)


# --- other commands ----------------------------------------------------------------------------


def baseline(context: Context, version: str, ref: str = "HEAD") -> str:
    tag = f"{context.config.versioning.tag_prefix}{Version.parse(version)}"
    if _tag_target(context, tag) is not None:
        raise ReleaseError(f"a tag {tag} já existe.")
    problems = check_files(context.config, context.root, version)
    if problems:
        raise ReleaseError("os arquivos não estão em " + version + ": " + "; ".join(problems))
    context.run_git("tag", "-a", tag, ref, "-m", f"Base de versionamento {tag}")
    return tag


def record_analysis(context: Context, number: int, level: str, reason: str) -> None:
    if level not in LEVELS:
        raise ReleaseError(f"nível desconhecido: {level}")
    body = f"<!-- dev-workflow:semver-analysis level={level} -->\nAnálise de versão: **{level}**.\n\n{reason.strip()}"
    context.run_gh("api", "-X", "POST", f"repos/{context.repository}/issues/{number}/comments", "-f", f"body={body}")


def preview_text(context: Context, number: int) -> str:
    pull = fetch_pull(context, number)
    labels = pull.labels if pull else ()
    release = plan(context, trigger=number)
    tag = f"{context.config.versioning.tag_prefix}{release.next}"
    marked = context.config.versioning.release_label in labels
    state = (
        f"Com o label `{context.config.versioning.release_label}`, o merge publica **{tag}** ({release.level})."
        if marked
        else f"Sem o label `{context.config.versioning.release_label}`, este PR fica acumulado; com ele, sairia **{tag}** ({release.level})."
    )
    signals = "\n".join(f"- {signal.level}: {signal.source} — {signal.detail}" for signal in release.signals[:20])
    override = f"\n\nSobreposição: {release.override[0]} — {release.override[1]}" if release.override else ""
    return f"{PREVIEW_MARKER}\n### Versão\n\nAtual: {release.current}. {state}{override}\n\n{signals or '- sem sinais além do patch mínimo'}\n"


def upsert_preview(context: Context, number: int, text: str) -> str:
    """One preview comment per PR, updated in place; returns ``created`` or ``updated``."""
    for comment in _comments(context, number):
        author = str((comment.get("user") or {}).get("login") or "")  # type: ignore[union-attr]
        if PREVIEW_MARKER in str(comment.get("body") or "") and author in context.config.versioning.analysis_authors:
            context.run_gh(
                "api",
                "-X",
                "PATCH",
                f"repos/{context.repository}/issues/comments/{comment['id']}",
                "-f",
                f"body={text}",
            )
            return "updated"
    context.run_gh("api", "-X", "POST", f"repos/{context.repository}/issues/{number}/comments", "-f", f"body={text}")
    return "created"


def _context(config: Config) -> Context:
    root = config.root
    repository = os.environ.get("GITHUB_REPOSITORY") or ""
    if not repository:
        completed = _process("gh", root)(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
        repository = completed.stdout.strip()
    return Context(config, root, repository, _process("git", root), _process("gh", root))


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    for name in ("plan", "next", "run"):
        sub = commands.add_parser(name)
        sub.add_argument(
            "--pr",
            type=int,
            default=int(os.environ["PR_NUMBER"]) if os.environ.get("PR_NUMBER", "").isdigit() else None,
        )
        sub.add_argument("--prerelease")
        if name == "plan":
            sub.add_argument("--json", action="store_true")
        if name == "run":
            sub.add_argument("--dry-run", action="store_true")
    commands.add_parser("check")
    sub = commands.add_parser("bump")
    sub.add_argument("version")
    sub = commands.add_parser("baseline")
    sub.add_argument("version")
    sub.add_argument("ref", nargs="?", default="HEAD")
    sub = commands.add_parser("record-analysis")
    sub.add_argument("--pr", type=int, required=True)
    sub.add_argument("--level", required=True, choices=LEVELS)
    sub.add_argument("--reason", required=True)
    sub = commands.add_parser("preview")
    sub.add_argument("--pr", type=int, required=True)
    sub.add_argument("--comment", action="store_true", help="cria ou atualiza o comentário único de prévia no PR")
    options = parser.parse_args(argv)
    try:
        config = load()
        if not config.versioning.enabled and options.command != "status":
            print("versionamento desligado: versioning.enabled = false em .github/dev-workflow.toml")
            return 0
        context = _context(config)
        if options.command == "status":
            tag = last_tag(context)
            if tag is None:
                print("sem tag de versão alcançável (release.py baseline)")
                return 0
            commits = commits_since(context, tag)
            print(
                f"versão {tag[len(config.versioning.tag_prefix) :]}: {len(commits)} commits e PRs {pull_numbers(commits)} acumulados"
            )
            return 0
        if options.command == "check":
            problems = check_files(config, config.root)
            for problem in problems:
                print(f"divergência: {problem}")
            if not problems:
                print("arquivos de versão coerentes")
            return 1 if problems else 0
        if options.command == "bump":
            current = str(declared_version(context))
            new = str(Version.parse(options.version))
            for item in config.versioning.files:
                write_version(config.root, item, current, new)
            print(f"{current} → {new} em {len(config.versioning.files)} arquivos")
            return 0
        if options.command in ("plan", "next"):
            release = plan(context, trigger=options.pr, prerelease=options.prerelease)
            if options.command == "next":
                print(release.next)
            elif options.json:
                print(json.dumps(release.as_dict(), ensure_ascii=False, indent=2))
            else:
                print(f"{release.current} → {release.next} ({release.level})")
                for signal in release.signals:
                    print(f"  {signal.level}: {signal.source} — {signal.detail}")
            return 0
        if options.command == "run":
            for line in run(context, trigger=options.pr, prerelease=options.prerelease, dry_run=options.dry_run):
                print(line)
            return 0
        if options.command == "baseline":
            print(baseline(context, options.version, options.ref))
            return 0
        if options.command == "record-analysis":
            record_analysis(context, options.pr, options.level, options.reason)
            return 0
        if last_tag(context) is None:
            # Before the owner's base tag there is no version to preview; not an error of the PR.
            print("sem tag de versão alcançável: a prévia começa depois de `release.py baseline`.")
            return 0
        text = preview_text(context, options.pr)
        print(text)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(text)
        if options.comment:
            print(f"comentário de prévia {upsert_preview(context, options.pr, text)}")
        return 0
    except (ReleaseError, ConfigError, VersionError) as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
