"""Plano de versão: o que se acumulou desde a última tag e qual é o próximo número.

A fonte da verdade é o git: a versão corrente é a maior tag de versão alcançável, e tudo que
está entre ela e o HEAD ainda não foi publicado. Nada depende de marcação mutável no GitHub
para saber o que já saiu; labels e comentários só informam *como* versionar.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .config import BREAKING_SECTION, FALLBACK_SECTION, Config
from .gh import Git, GitHub, is_trusted_author, label_names
from .semver import LEVELS, Version, highest, level_index, max_level, next_prerelease, next_version
from .text import (
    MERGE_PR,
    SQUASH_SUFFIX,
    breaking_notes,
    marked_value,
    parse_conventional,
    referenced_issues,
    section,
)

ANALYSIS_MARK = "dev-workflow:analysis"
ANALYSIS_BLOCK = re.compile(rf"<!-- {ANALYSIS_MARK}\s*\n(?P<json>.*?)\n-->", re.DOTALL)
_FIELD, _RECORD = chr(0), chr(1)  # nunca espaço em branco: strip() não pode comê-los


@dataclass
class Commit:
    sha: str
    parents: list[str]
    subject: str
    body: str = ""

    @property
    def message(self) -> str:
        return f"{self.subject}\n\n{self.body}".strip()


@dataclass
class Signal:
    level: str
    source: str  # issue | commit | titulo | label | corpo | superficie | agente | piso
    detail: str
    breaking: bool = False


@dataclass
class IssueInfo:
    number: int
    title: str
    type: str | None
    closing: bool


@dataclass
class SurfaceHit:
    surface: str
    path: str
    event: str  # add | modify | remove
    action: str  # nível ou "review"


@dataclass
class Change:
    kind: str  # "pr" ou "commit"
    number: int | None
    sha: str
    title: str
    author: str = ""
    labels: set[str] = field(default_factory=set)
    body: str = ""
    commits: list[Commit] = field(default_factory=list)
    issues: list[IssueInfo] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    surfaces: list[SurfaceHit] = field(default_factory=list)
    analyzed: bool = False
    pending: bool = False  # PR ainda aberto, incluído numa prévia

    @property
    def key(self) -> str:
        return f"#{self.number}" if self.number is not None else self.sha[:7]

    @property
    def level(self) -> str:
        return max_level(signal.level for signal in self.signals)

    @property
    def breaking(self) -> bool:
        return any(signal.breaking for signal in self.signals)

    @property
    def needs_review(self) -> bool:
        return any(hit.action == "review" for hit in self.surfaces)


@dataclass
class Override:
    level: str
    reason: str
    source: str


@dataclass
class Plan:
    branch: str
    head: str
    base_tag: str | None
    base_version: Version
    files_version: Version
    changes: list[Change]
    requested_by: list[str]
    forced: bool
    natural: str
    level: str
    override: Override | None
    version: Version | None
    prerelease: str | None = None
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def requested(self) -> bool:
        return self.forced or bool(self.requested_by)

    def tag(self, cfg: Config) -> str | None:
        return f"{cfg.tag_prefix}{self.version}" if self.version else None


class Repo:
    """Leituras do git usadas pelo plano."""

    def __init__(self, cfg: Config, git: Git | None = None):
        self.cfg = cfg
        self.git = git or Git(cfg.root)

    def version_tags(self, merged_into: str | None = None) -> dict[str, Version]:
        prefix = self.cfg.tag_prefix
        args = ["tag", "--list", f"{prefix}*"] + (["--merged", merged_into] if merged_into else [])
        tags: dict[str, Version] = {}
        for tag in self.git(*args).splitlines():
            version = Version.try_parse(tag.strip()[len(prefix) :])
            if version is not None:
                tags[tag.strip()] = version
        return tags

    def latest(self, ref: str, *, final_only: bool) -> tuple[str, Version] | None:
        tags = {tag: v for tag, v in self.version_tags(ref).items() if not (final_only and v.is_prerelease)}
        if not tags:
            return None
        tag = max(tags, key=lambda name: tags[name].key())
        return tag, tags[tag]

    def log(self, revision: str, *, first_parent: bool = False, no_merges: bool = False) -> list[Commit]:
        args = ["log", "--reverse", "--format=%H%x00%P%x00%s%x00%b%x01"]
        args += ["--first-parent"] if first_parent else []
        args += ["--no-merges"] if no_merges else []
        commits = []
        for record in self.git(*args, revision).split(_RECORD):
            parts = record.strip("\n").split(_FIELD)
            if len(parts) == 4 and parts[0]:
                commits.append(Commit(parts[0], parts[1].split(), parts[2], parts[3].strip()))
        return commits

    def name_status(self, old: str | None, new: str) -> list[tuple[str, str]]:
        """``[(evento, caminho)]`` com evento em add/modify/remove; rename vira remove + add."""
        if old is None:
            output = self.git("show", "--name-status", "--format=", "-M", new)
        else:
            output = self.git("diff", "--name-status", "-M", old, new)
        events: list[tuple[str, str]] = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0][:1]
            if status == "R" and len(parts) == 3:
                events += [("remove", parts[1]), ("add", parts[2])]
            elif status in ("A", "C"):
                events.append(("add", parts[-1]))
            elif status == "D":
                events.append(("remove", parts[1]))
            else:
                events.append(("modify", parts[1]))
        return events

    def resolve(self, ref: str) -> str:
        return self.git("rev-parse", "--verify", f"{ref}^{{commit}}")

    def tracked_files(self) -> list[str]:
        return self.git("ls-files").splitlines()


def _glob_match(path: str, patterns: list[str]) -> bool:
    """``**`` cruza diretórios; ``dir/**`` cobre tudo abaixo de ``dir``."""
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, pattern.replace("**/", "")):
            return True
        if pattern.endswith("/**") and path.startswith(pattern[:-2]):
            return True
    return False


def surface_hits(events: list[tuple[str, str]], cfg: Config) -> list[SurfaceHit]:
    hits = []
    for event, path in events:
        for surface in cfg.versioning["public_surfaces"]:
            if _glob_match(path, surface["globs"]):
                default = "major" if event == "remove" else "review"
                action = surface.get(f"on_{event}", default)
                hits.append(SurfaceHit(surface.get("name", surface["globs"][0]), path, event, action))
                break
    return hits


class Collector:
    """Monta as mudanças de um intervalo, consultando o GitHub quando há acesso."""

    def __init__(self, cfg: Config, repo: Repo, github: GitHub):
        self.cfg, self.repo, self.github = cfg, repo, github
        self._pulls: dict[int, dict[str, Any] | None] = {}
        self._issues: dict[int, dict[str, Any] | None] = {}

    def pull(self, number: int) -> dict[str, Any] | None:
        if number not in self._pulls:
            self._pulls[number] = self.github.pull(number) if self.github.enabled else None
        return self._pulls[number]

    def issue(self, number: int) -> dict[str, Any] | None:
        if number not in self._issues:
            self._issues[number] = self.github.issue(number) if self.github.enabled else None
        return self._issues[number]

    def pr_number(self, commit: Commit) -> int | None:
        """O PR que trouxe este commit do primeiro pai: merge, squash ou rebase."""
        merge = MERGE_PR.match(commit.subject)
        if merge:
            return int(merge.group("number"))
        squash = SQUASH_SUFFIX.search(commit.subject)
        if squash:
            number = int(squash.group("number"))
            if not self.github.enabled or (self.pull(number) or {}).get("merged"):
                return number  # sem API confia no sufixo; com API, `(#N)` de uma issue não vira PR
        if self.github.enabled:
            merged = [pull for pull in self.github.commit_pulls(commit.sha) if pull.get("merged_at")]
            if merged:
                number = int(merged[0]["number"])
                self._pulls.setdefault(number, self.github.pull(number))
                return number
        return None

    def _issues_for(self, change: Change) -> list[IssueInfo]:
        repository = self.github.repository
        texts = [change.body, *(commit.message for commit in change.commits)]
        closing = {n for text in texts for n in referenced_issues(text, repository, closing_only=True)}
        infos: list[IssueInfo] = []
        for number in dict.fromkeys(n for text in texts for n in referenced_issues(text, repository)):
            if number == change.number:
                continue
            data = self.issue(number)
            if data is None:
                if not self.github.enabled:  # sem API: mantém a referência, sem tipo
                    infos.append(IssueInfo(number, "", None, number in closing))
                continue
            labels = label_names(data)
            if "pull_request" in data or self.cfg.label("epic") in labels:
                continue
            types = sorted(labels & set(self.cfg.type_labels))
            kind = types[0] if len(types) == 1 else None
            infos.append(IssueInfo(number, str(data.get("title", "")), kind, number in closing))
        return infos

    def _signals_for(self, change: Change) -> list[Signal]:
        cfg = self.cfg
        signals: list[Signal] = []
        for issue in change.issues:
            if issue.type:
                signals.append(
                    Signal(cfg.type_level(issue.type), "issue", f"#{issue.number} tem o label `{issue.type}`")
                )
        subjects = [(commit.subject, commit.message, commit.sha[:7]) for commit in change.commits]
        if change.kind == "pr":
            subjects.append((change.title, "", "título do PR"))
        for subject, message, where in subjects:
            conventional = parse_conventional(subject)
            if conventional:
                level = cfg.data["commits"]["levels"].get(conventional.type, "none")
                if level != "none":
                    signals.append(Signal(level, "commit", f"`{conventional.type}` em {where}"))
                if conventional.breaking:
                    signals.append(Signal("major", "commit", f"`!` em {where}: {conventional.description}", True))
            for note in breaking_notes(message):
                signals.append(Signal("major", "commit", f"BREAKING CHANGE em {where}: {note}", True))
        if cfg.label("breaking") in change.labels:
            signals.append(Signal("major", "label", f"label `{cfg.label('breaking')}`", True))
        for note in breaking_notes(change.body):
            signals.append(Signal("major", "corpo", f"BREAKING CHANGE no corpo do PR: {note}", True))
        for hit in change.surfaces:
            if hit.action in LEVELS and hit.action != "none":
                verb = {"add": "acrescentado", "modify": "alterado", "remove": "removido"}[hit.event]
                signals.append(
                    Signal(hit.action, "superficie", f"`{hit.path}` {verb} em «{hit.surface}»", hit.action == "major")
                )
        return signals

    def _pr_change(self, number: int, sha: str, fallback_title: str) -> Change:
        data = self.pull(number) or {}
        return Change(
            kind="pr",
            number=number,
            sha=sha,
            title=str(data.get("title") or fallback_title),
            author=str((data.get("user") or {}).get("login", "")),
            labels=label_names(data),
            body=str(data.get("body") or ""),
            pending=bool(data) and not data.get("merged", bool(data.get("merged_at"))),
        )

    def collect(self, base: str | None, head: str) -> list[Change]:
        marker = self.cfg.release_subject_pattern()
        changes: dict[str, Change] = {}
        for commit in self.repo.log(f"{base}..{head}" if base else head, first_parent=True):
            reverted = commit.subject[8:-1] if commit.subject.startswith('Revert "') else ""
            if marker.match(commit.subject) or (reverted and marker.match(reverted)):
                continue
            number = self.pr_number(commit)
            parent = commit.parents[0] if commit.parents else None
            members = self.repo.log(f"{parent}..{commit.sha}", no_merges=True) if len(commit.parents) > 1 else [commit]
            members = [c for c in members if not marker.match(c.subject)]
            if number is not None:
                change = changes.setdefault(f"#{number}", self._pr_change(number, commit.sha, commit.subject))
                change.sha = commit.sha
            else:
                change = changes.setdefault(commit.sha, Change("commit", None, commit.sha, commit.subject))
            change.commits += members
            change.surfaces += surface_hits(self.repo.name_status(parent, commit.sha), self.cfg)
        return self.finish(list(changes.values()))

    def pending_pr(self, number: int, base_ref: str, head_ref: str) -> Change:
        """O PR ainda aberto, como se já estivesse mesclado em ``base_ref``."""
        change = self._pr_change(number, self.repo.resolve(head_ref), f"PR #{number}")
        change.pending = True
        change.commits = self.repo.log(f"{base_ref}..{head_ref}", no_merges=True)
        merge_base = self.repo.git("merge-base", base_ref, head_ref)
        change.surfaces = surface_hits(self.repo.name_status(merge_base, head_ref), self.cfg)
        return self.finish([change])[0]

    def finish(self, changes: list[Change]) -> list[Change]:
        for change in changes:
            change.issues = self._issues_for(change)
            change.signals = self._signals_for(change)
        return changes

    # --- análise por agente ----------------------------------------------------------------

    def recorded_analyses(self, changes: list[Change]) -> list[dict[str, Any]]:
        """Análises gravadas em comentários de PR por quem pode escrever no repositório."""
        if not self.github.enabled:
            return []
        found: list[tuple[str, dict[str, Any]]] = []
        trusted = self.cfg.versioning["trusted_bots"]
        for change in changes:
            if change.number is None:
                continue
            for comment in self.github.comments(change.number):
                if not is_trusted_author(comment, trusted):
                    continue
                for block in ANALYSIS_BLOCK.finditer(str(comment.get("body") or "")):
                    try:
                        found.append((str(comment.get("created_at", "")), validate_analysis(json.loads(block["json"]))))
                    except ValueError:
                        continue
        return [data for _, data in sorted(found, key=lambda item: item[0])]


def validate_analysis(data: Any) -> dict[str, Any]:
    """Confere o formato da análise; devolve-a com ``level`` igual ao maior nível das mudanças."""
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("changes"), list):
        raise ValueError('A análise precisa de `"schema": 1` e de uma lista `changes`.')
    entries = []
    for index, entry in enumerate(data["changes"]):
        where = f"changes[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} deve ser um objeto.")
        pr, commit = entry.get("pr"), entry.get("commit")
        if isinstance(pr, bool) or not (isinstance(pr, int) or (isinstance(commit, str) and len(commit) >= 7)):
            raise ValueError(f"{where} precisa de `pr` (número) ou `commit` (SHA com 7 caracteres ou mais).")
        if entry.get("level") not in LEVELS:
            raise ValueError(f"{where}.level deve ser um de {', '.join(LEVELS)}.")
        if not isinstance(entry.get("summary"), str) or not entry["summary"].strip():
            raise ValueError(f"{where}.summary é obrigatório: uma frase com o motivo do nível.")
        evidence = entry.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise ValueError(f"{where}.evidence deve ser uma lista de textos.")
        item: dict[str, Any] = {
            "level": entry["level"],
            "summary": entry["summary"].strip(),
            "breaking": bool(entry.get("breaking", entry["level"] == "major")),
            "evidence": evidence,
        }
        item.update({"pr": pr} if isinstance(pr, int) else {"commit": commit})
        entries.append(item)
    return {
        "schema": 1,
        "base": str(data.get("base", "")),
        "head": str(data.get("head", "")),
        "level": max_level(entry["level"] for entry in entries),
        "changes": entries,
        "notes": str(data.get("notes", "")),
    }


def apply_analyses(changes: list[Change], analyses: list[dict[str, Any]]) -> None:
    """Acrescenta a cada mudança o veredito da análise mais recente que a cobre.

    A análise só acrescenta sinais; como o nível é sempre o maior, ela eleva e nunca reduz.
    """
    for change in changes:
        verdict = None
        for analysis in analyses:  # em ordem cronológica: a última que cita a mudança vence
            for entry in analysis["changes"]:
                by_pr = change.number is not None and entry.get("pr") == change.number
                by_sha = "commit" in entry and any(c.sha.startswith(entry["commit"]) for c in [*change.commits, change])
                if by_pr or (change.number is None and by_sha):
                    verdict = entry
        if verdict is None:
            continue
        change.signals = [signal for signal in change.signals if signal.source != "agente"]
        change.signals.append(Signal(verdict["level"], "agente", verdict["summary"], bool(verdict["breaking"])))
        change.analyzed = True


def floor_requirement(cfg: Config, repo: Repo) -> tuple[Version, str] | None:
    """Maior versão que o código declara como piso (``since=``, mínimos de compatibilidade)."""
    best: tuple[Version, str] | None = None
    rules = cfg.versioning["floors"]
    if not rules:
        return None
    files = repo.tracked_files()
    for rule in rules:
        pattern = re.compile(rule["regex"], re.MULTILINE)
        for path in files:
            if not _glob_match(path, rule["globs"]):
                continue
            try:
                content = (cfg.root / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for match in pattern.finditer(content):
                version = Version.try_parse(match.group(1))
                if version is not None and (best is None or version.key() > best[0].key()):
                    best = (version, path)
    return best


def release_override(changes: list[Change], cfg: Config) -> Override | None:
    """Sobreposição pedida no PR de release: label ``semver:*`` com o motivo na seção de versão."""
    prefix, chosen = cfg.label("override_prefix"), None
    for change in changes:
        if cfg.label("release") not in change.labels:
            continue
        levels = [label[len(prefix) :] for label in change.labels if label.startswith(prefix)]
        levels = [level for level in levels if level in ("major", "minor", "patch")]
        if len(levels) == 1:
            marker = str(cfg.pr["override_reason_marker"])
            reason = marked_value(section(change.body, cfg.pr["version_section"]), marker)
            chosen = Override(
                levels[0], reason or "(motivo não informado)", f"label `{prefix}{levels[0]}` em {change.key}"
            )
    return chosen


def build_plan(
    cfg: Config,
    repo: Repo,
    github: GitHub,
    *,
    head: str = "HEAD",
    branch: str = "",
    forced: bool = False,
    override: Override | None = None,
    prerelease: str | None = None,
    pending: tuple[int, str] | None = None,
    extra_analyses: list[dict[str, Any]] | None = None,
    fixed_version: Version | None = None,
    exclude_tag: str | None = None,
) -> Plan:
    """Calcula o plano para ``head``. ``pending=(PR, head_ref)`` simula o merge de um PR aberto."""
    tags = repo.version_tags(head)
    if exclude_tag:
        tags.pop(exclude_tag, None)
    finals = {tag: version for tag, version in tags.items() if not version.is_prerelease}
    base_tag = max(finals, key=lambda name: finals[name].key()) if finals else None
    initial = Version.parse(str(cfg.versioning["initial_version"]))
    base_version = finals[base_tag] if base_tag else initial
    files_version = highest(tags.values()) or initial

    collector = Collector(cfg, repo, github)
    changes = collector.collect(base_tag, head)
    if pending is not None:
        changes.append(collector.pending_pr(pending[0], head, pending[1]))
    apply_analyses(changes, [*collector.recorded_analyses(changes), *(extra_analyses or [])])

    plan = Plan(
        branch=branch,
        head=repo.resolve(pending[1] if pending else head),
        base_tag=base_tag,
        base_version=base_version,
        files_version=files_version,
        changes=changes,
        requested_by=[c.key for c in changes if c.kind == "pr" and cfg.label("release") in c.labels],
        forced=forced,
        natural=max_level(change.level for change in changes),
        level="none",
        override=override or release_override(changes, cfg),
        version=None,
        prerelease=prerelease,
    )
    zero_major = cfg.versioning["zero_major"]
    plan.level = plan.natural
    if plan.level == "none" and changes:
        plan.level = "patch"
        plan.notes.append("Nenhum sinal pede incremento; uma versão publicada sobe ao menos o patch.")
    if fixed_version is not None:
        plan.version = fixed_version
        return plan
    if not changes:
        if forced:
            plan.problems.append("Nada se acumulou desde a última versão; não há o que publicar.")
        return plan
    if plan.override:
        plan.level = plan.override.level
        target = base_version.final.bump(plan.level)  # a sobreposição é literal, inclusive para chegar a 1.0.0
    else:
        target = next_version(base_version, plan.level if plan.level != "none" else "patch", zero_major)
        if base_version.major == 0 and plan.level == "major" and zero_major == "breaking-is-minor":
            plan.notes.append(
                "Versão 0.x: a quebra de compatibilidade sobe o minor. Para declarar a API estável e ir a 1.0.0, "
                f"use o label `{cfg.override_label('major')}` no PR de release."
            )
    floor = floor_requirement(cfg, repo)
    if floor and floor[0].key() > target.key():
        version, path = floor
        raised = None
        if not plan.override:
            for level in LEVELS[level_index(plan.level) :]:
                if level != "none" and next_version(base_version, level, zero_major).key() >= version.key():
                    raised = level
                    break
        if raised:
            plan.notes.append(f"`{path}` declara o piso {version}: o nível sobe de {plan.level} para {raised}.")
            plan.level, target = raised, next_version(base_version, raised, zero_major)
        else:
            plan.problems.append(
                f"`{path}` declara o piso {version}, maior que a versão {target} que seria publicada. "
                "Corrija o piso ou classifique a mudança de modo que a versão o alcance."
            )
    if prerelease:
        target = next_prerelease(target, prerelease, repo.version_tags().values())
    plan.version = target
    if f"{cfg.tag_prefix}{target}" in repo.version_tags():
        plan.problems.append(
            f"A tag {cfg.tag_prefix}{target} já existe. Numa branch de manutenção só cabem correções; "
            "confira a branch e o nível calculado."
        )
    if cfg.versioning["require_analysis"] and not plan.override:
        uncovered = [change.key for change in changes if change.needs_review and not change.analyzed]
        if uncovered:
            plan.problems.append(
                "Mudanças em superfícies públicas sem análise registrada: "
                + ", ".join(uncovered)
                + ". Registre a análise (`release.py record-analysis`) ou sobreponha o nível com motivo."
            )
    return plan


# --- changelog e textos ------------------------------------------------------------------------


def _display_title(title: str) -> str:
    conventional = parse_conventional(title)
    if not conventional:
        return title
    scope = f"**{conventional.scope}:** " if conventional.scope else ""
    return f"{scope}{conventional.description}"


class Links:
    def __init__(self, web: str):
        self.web = web

    def issue(self, number: int) -> str:
        return f"[#{number}]({self.web}/issues/{number})" if self.web else f"#{number}"

    def pull(self, number: int) -> str:
        return f"[#{number}]({self.web}/pull/{number})" if self.web else f"#{number}"

    def commit(self, sha: str) -> str:
        return f"[{sha[:7]}]({self.web}/commit/{sha})" if self.web else sha[:7]

    def change(self, change: Change) -> str:
        return f"PR {self.pull(change.number)}" if change.number is not None else self.commit(change.sha)


def changelog_entries(plan: Plan, cfg: Config, links: Links) -> dict[str, list[str]]:
    """``{id da seção: [linhas]}``: uma linha por issue entregue; sem issue, uma linha por PR ou commit."""
    entries: dict[str, list[str]] = {}
    delivered: dict[int, tuple[str, str, list[str]]] = {}
    for change in plan.changes:
        reference = links.change(change)
        closing = [issue for issue in change.issues if issue.closing and issue.type]
        for issue in closing:
            slot = delivered.setdefault(issue.number, (cfg.type_section(issue.type or ""), issue.title, []))
            slot[2].append(reference)
        if not closing:
            typed = [issue for issue in change.issues if issue.type]
            conventional = parse_conventional(change.title)
            prefix_type = cfg.type_for_prefix(conventional.type) if conventional else None
            if typed:
                section_id = cfg.type_section(typed[0].type or "")
                suffix = " — parte de " + ", ".join(links.issue(issue.number) for issue in typed)
            else:
                section_id = cfg.type_section(prefix_type) if prefix_type else FALLBACK_SECTION
                suffix = ""
            entries.setdefault(section_id, []).append(f"- {_display_title(change.title)} ({reference}){suffix}")
        if change.breaking:
            details = "; ".join(dict.fromkeys(signal.detail for signal in change.signals if signal.breaking))
            entries.setdefault(BREAKING_SECTION, []).append(
                f"- {_display_title(change.title)} ({reference}) — {details}"
            )
    issue_lines: dict[str, list[str]] = {}
    for number, (section_id, title, references) in delivered.items():
        line = f"- {title or f'Issue #{number}'} ({links.issue(number)}) — {', '.join(dict.fromkeys(references))}"
        issue_lines.setdefault(section_id, []).append(line)
    for section_id, lines in issue_lines.items():  # issues entregues antes das entradas sem issue
        entries[section_id] = lines + entries.get(section_id, [])
    return entries


def render_changelog_body(plan: Plan, cfg: Config, links: Links, *, hide: bool = True) -> str:
    entries = changelog_entries(plan, cfg, links)
    hidden = set(cfg.changelog["hide"]) if hide else set()
    blocks = []
    for item in cfg.changelog["sections"]:
        lines = entries.get(item["id"])
        if lines and item["id"] not in hidden:
            blocks.append(f"### {item['title']}\n\n" + "\n".join(lines))
    return "\n\n".join(blocks) or "Sem mudanças visíveis para quem usa o projeto."


def render_justification(plan: Plan, cfg: Config, links: Links) -> str:
    """Por que este número: sinais de cada mudança, sobreposição e situação da análise por agente."""
    lines = []
    origin = f"{plan.base_tag}" if plan.base_tag else f"{plan.base_version} (versão inicial, sem tag)"
    if plan.version:
        lines.append(f"**{origin} → {cfg.tag_prefix}{plan.version}** — nível **{plan.level}**.")
    if plan.override:
        lines.append(
            f"Nível sobreposto para **{plan.override.level}** ({plan.override.source}); o cálculo dava "
            f"**{plan.natural}**. Motivo: {plan.override.reason}"
        )
    lines += plan.notes
    if plan.changes:
        lines += ["", "| Mudança | Nível | Sinais |", "|---|---|---|"]
        for change in plan.changes:
            signals = "; ".join(
                f"{s.detail} → {s.level}" for s in change.signals if s.level != "none" or s.source == "agente"
            )
            review = sorted({hit.path for hit in change.surfaces if hit.action == "review"})
            if review and not change.analyzed:
                shown = ", ".join(f"`{path}`" for path in review[:5]) + (" …" if len(review) > 5 else "")
                signals += ("; " if signals else "") + f"superfície pública tocada sem análise: {shown}"
            lines.append(
                f"| {links.change(change)} {change.title.replace('|', '/')} | {change.level} | {signals or '—'} |"
            )
    analyzed = sum(1 for change in plan.changes if change.analyzed)
    lines.append("")
    if analyzed == len(plan.changes) and plan.changes:
        lines.append("Análise por agente: cobre todas as mudanças.")
    elif analyzed:
        lines.append(
            f"Análise por agente: cobre {analyzed} de {len(plan.changes)} mudanças; as demais só têm sinais determinísticos."
        )
    else:
        lines.append("Análise por agente: ausente; o nível vem só dos sinais determinísticos.")
    return "\n".join(lines).strip()


def plan_json(plan: Plan, cfg: Config) -> dict[str, Any]:
    return {
        "branch": plan.branch,
        "head": plan.head,
        "base_tag": plan.base_tag,
        "base_version": str(plan.base_version),
        "requested": plan.requested,
        "requested_by": plan.requested_by,
        "natural_level": plan.natural,
        "level": plan.level,
        "override": plan.override.__dict__ if plan.override else None,
        "next_version": str(plan.version) if plan.version else None,
        "next_tag": plan.tag(cfg),
        "notes": plan.notes,
        "problems": plan.problems,
        "changes": [
            {
                "key": change.key,
                "kind": change.kind,
                "pr": change.number,
                "sha": change.sha,
                "title": change.title,
                "labels": sorted(change.labels),
                "level": change.level,
                "breaking": change.breaking,
                "analyzed": change.analyzed,
                "pending": change.pending,
                "issues": [issue.__dict__ for issue in change.issues],
                "signals": [signal.__dict__ for signal in change.signals],
                "commits": [{"sha": commit.sha, "subject": commit.subject} for commit in change.commits],
                "public_surfaces": [hit.__dict__ for hit in change.surfaces],
            }
            for change in plan.changes
        ],
    }
