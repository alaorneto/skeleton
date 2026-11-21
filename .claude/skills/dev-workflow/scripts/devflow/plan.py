"""Plano de versão: o que se acumulou desde a última tag e qual é o próximo número.

A fonte da verdade é o git: a versão corrente vem das tags de versão alcançáveis (``Repo.line``),
e tudo que está entre a última final e o HEAD ainda não saiu numa versão final. Nada depende de
marcação mutável no GitHub para saber o que já saiu; labels e comentários só informam *como*
versionar.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from . import i18n
from .config import BREAKING_SECTION, FALLBACK_SECTION, Config
from .gh import Git, GitHub, is_trusted_author, label_names
from .semver import (
    LEVELS,
    Version,
    cycle_floor,
    level_between,
    level_index,
    max_level,
    next_prerelease,
    next_version,
    not_surpassed,
)
from .text import (
    MERGE_PR,
    REVERTS,
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
    fresh: bool = True  # novo: ainda não saiu em nenhuma pré-release do ciclo em andamento
    head_sha: str = ""  # último commit do PR (``head.sha`` da API), que a análise precisa ter visto

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


class Problem(str):
    """Um problema do plano: o texto para quem opera e uma chave que não muda com o alvo.

    O texto de um mesmo problema cita a versão que seria publicada, que muda quando um PR entra
    na conta. Quem compara dois planos (o check do pedido de versão de um PR) usa a chave.
    """

    key: str

    def __new__(cls, key: str, text: str) -> Problem:
        problem = super().__new__(cls, text)
        problem.key = key
        return problem


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
    previous_tag: str | None = None  # a tag da versão corrente: a pré-release mais recente do ciclo ou a final
    cycle: list[str] = field(default_factory=list)  # tags de pré-release do ciclo em andamento
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def requested(self) -> bool:
        return self.forced or bool(self.requested_by)

    def tag(self, cfg: Config) -> str | None:
        return f"{cfg.tag_prefix}{self.version}" if self.version else None


@dataclass
class Line:
    """Onde a linha de um ref está: a última final, o ciclo de pré-releases e a versão corrente.

    O **ciclo** são as tags de pré-release alcançáveis do ref, não alcançáveis da última final e
    acima dela. O alcance no grafo cobre o repositório que já regrediu: uma pré-release maior que
    ficou *atrás* de uma final não volta a contar, e nada é forçado a alcançá-la. O número cobre a
    linha de manutenção mesclada de volta: as pré-releases dela ficam alcançáveis de uma branch que
    já publicou uma versão maior, e não são o ciclo desta.
    A **versão corrente** é a que os arquivos de versão carregam: a pré-release mais recente do
    ciclo ou, sem ciclo, a última final.
    """

    tags: dict[str, Version]  # todas as tags de versão alcançáveis
    final_tag: str | None
    cycle: dict[str, Version]  # em ordem de precedência
    current_tag: str | None

    @property
    def final(self) -> Version | None:
        return self.tags[self.final_tag] if self.final_tag else None

    @property
    def current(self) -> Version | None:
        return self.tags[self.current_tag] if self.current_tag else None

    @property
    def floor(self) -> tuple[str, Version] | None:
        """``(tag, núcleo)`` do maior alvo que o ciclo já publicou em pré-release."""
        core = cycle_floor(self.cycle.values())
        if core is None:
            return None
        return [tag for tag, version in self.cycle.items() if version.final == core][-1], core


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

    def line(self, ref: str, *, exclude: str | None = None) -> Line:
        """A situação da linha de ``ref``. ``exclude`` ignora uma tag (a que está sendo publicada)."""
        tags = self.version_tags(ref)
        if exclude:
            tags.pop(exclude, None)
        finals = {tag: version for tag, version in tags.items() if not version.is_prerelease}
        final_tag = max(finals, key=lambda name: finals[name].key()) if finals else None
        behind = self.version_tags(final_tag) if final_tag else {}
        last = finals[final_tag].key() if final_tag else None
        # Abaixo da última final não há ciclo desta linha: é o de uma linha de manutenção mesclada aqui
        # (`1.2.1-rc.1` numa branch que já publicou `1.3.0`), tenha ela publicado a final dela ou não.
        ahead = {
            tag: version
            for tag, version in tags.items()
            if version.is_prerelease and tag not in behind and (last is None or version.key() > last)
        }
        cycle = dict(sorted(ahead.items(), key=lambda item: item[1].key()))
        current_tag = final_tag
        if cycle:
            # A mais recente pelo grafo, não a de maior número: é a que os arquivos de versão trazem.
            # Com duas pontas que não se alcançam (ciclos de linhas diferentes mesclados), a maior delas.
            pattern = f"{self.cfg.tag_prefix}*"
            tips = self.git("merge-base", "--independent", *(f"{tag}^{{commit}}" for tag in cycle)).split()
            here: set[str] = set()
            for tip in tips:
                here.update(self.git("tag", "--list", pattern, "--points-at", tip).split())
            current_tag = [tag for tag in cycle if tag in here][-1]
        return Line(tags, final_tag, cycle, current_tag)

    def current(self, ref: str) -> tuple[str, Version] | None:
        """``(tag, versão)`` corrente da linha de ``ref``; ``None`` enquanto não há tag de versão."""
        line = self.line(ref)
        return (line.current_tag, line.tags[line.current_tag]) if line.current_tag else None

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


@functools.cache
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Glob de caminho como expressão regular.

    ``*``, ``?`` e ``[...]`` ficam dentro de um nível de diretório; ``**/`` vale zero ou mais
    diretórios e ``**`` em outra posição vale qualquer coisa. ``fnmatch`` não serve aqui: nele o
    ``*`` também casa ``/``, e ``scripts/*.py`` cobriria os subdiretórios.
    """
    parts: list[str] = []
    index, size = 0, len(pattern)
    while index < size:
        char = pattern[index]
        if pattern.startswith("**/", index) and (index == 0 or pattern[index - 1] == "/"):
            parts.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif char == "*":
            parts.append("[^/]*")
            index += 1
        elif char == "?":
            parts.append("[^/]")
            index += 1
        elif char == "[" and "]" in pattern[index + 2 :]:
            end = pattern.index("]", index + 2)
            body = pattern[index + 1 : end]
            negated = body[:1] in ("!", "^")
            body = (body[1:] if negated else body).replace("\\", "\\\\").replace("[", "\\[")
            parts.append(f"(?!/)[{'^' if negated else ''}{body}]")
            index = end + 1
        else:
            parts.append(re.escape(char))
            index += 1
    return re.compile("".join(parts))


def _glob_match(path: str, patterns: list[str]) -> bool:
    """``**`` cruza diretórios; ``*`` não. ``dir/**`` cobre tudo abaixo de ``dir``."""
    return any(_glob_regex(pattern).fullmatch(path) for pattern in patterns)


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
        # Notas para o plano: issues citadas sem exatamente um label de tipo.
        self.notes: list[str] = []

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
            if kind is None:
                found = ", ".join(f"`{name}`" for name in types) or self.cfg.t("plan.none")
                note = self.cfg.t("plan.note.issue_type", number=number, change=change.key, found=found)
                if note not in self.notes:
                    self.notes.append(note)
            infos.append(IssueInfo(number, str(data.get("title", "")), kind, number in closing))
        return infos

    def _signals_for(self, change: Change) -> list[Signal]:
        cfg = self.cfg
        signals: list[Signal] = []
        for issue in change.issues:
            if issue.type:
                detail = cfg.t("plan.signal.issue", number=issue.number, label=issue.type)
                signals.append(Signal(cfg.type_level(issue.type), "issue", detail))
        subjects = [(commit.subject, commit.message, commit.sha[:7]) for commit in change.commits]
        if change.kind == "pr":
            subjects.append((change.title, "", cfg.t("plan.signal.pr_title")))
        for subject, message, where in subjects:
            conventional = parse_conventional(subject)
            if conventional:
                level = cfg.data["commits"]["levels"].get(conventional.type, "none")
                if level != "none":
                    detail = cfg.t("plan.signal.commit", type=conventional.type, where=where)
                    signals.append(Signal(level, "commit", detail))
                if conventional.breaking:
                    detail = cfg.t("plan.signal.bang", where=where, description=conventional.description)
                    signals.append(Signal("major", "commit", detail, True))
            for note in breaking_notes(message):
                detail = cfg.t("plan.signal.breaking_commit", where=where, note=note)
                signals.append(Signal("major", "commit", detail, True))
        if cfg.label("breaking") in change.labels:
            signals.append(Signal("major", "label", cfg.t("plan.signal.label", label=cfg.label("breaking")), True))
        for note in breaking_notes(change.body):
            signals.append(Signal("major", "corpo", cfg.t("plan.signal.breaking_body", note=note), True))
        for hit in change.surfaces:
            if hit.action in LEVELS and hit.action != "none":
                details = {
                    "add": cfg.t("plan.signal.surface_added", path=hit.path, surface=hit.surface),
                    "modify": cfg.t("plan.signal.surface_modified", path=hit.path, surface=hit.surface),
                    "remove": cfg.t("plan.signal.surface_removed", path=hit.path, surface=hit.surface),
                }
                signals.append(Signal(hit.action, "superficie", details[hit.event], hit.action == "major"))
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
            head_sha=str((data.get("head") or {}).get("sha") or ""),
        )

    def collect(self, base: str | None, head: str, cycle: Sequence[str] = ()) -> list[Change]:
        """As mudanças de ``base..head``. ``cycle`` são as tags de pré-release do ciclo em andamento:
        o que elas já alcançam continua na lista (a final cobre tudo), mas deixa de ser novidade."""
        marker = self.cfg.release_subject_pattern()
        changes: dict[str, Change] = {}
        unreleased: set[str] | None = None
        if cycle:
            behind = [*([base] if base else []), *cycle]
            unreleased = set(self.repo.git("log", "--first-parent", "--format=%H", head, "--not", *behind).split())
        for commit in self.repo.log(f"{base}..{head}" if base else head, first_parent=True):
            reverted = commit.subject[8:-1] if commit.subject.startswith('Revert "') else ""
            if marker.match(commit.subject) or (reverted and marker.match(reverted)):
                continue
            number = self.pr_number(commit)
            parent = commit.parents[0] if commit.parents else None
            members = self.repo.log(f"{parent}..{commit.sha}", no_merges=True) if len(commit.parents) > 1 else [commit]
            carried = len(members)
            members = [c for c in members if not marker.match(c.subject)]
            if len(commit.parents) > 1 and carried and not members:
                continue  # merge do PR de release: só traz o commit de release, que não é mudança
            if number is not None:
                change = changes.setdefault(f"#{number}", self._pr_change(number, commit.sha, commit.subject))
                change.sha = commit.sha
            else:
                change = changes.setdefault(commit.sha, Change("commit", None, commit.sha, commit.subject))
            change.commits += members
            change.surfaces += surface_hits(self.repo.name_status(parent, commit.sha), self.cfg)
        for change in changes.values():
            change.fresh = unreleased is None or change.sha in unreleased
        return self.finish(self._drop_reverted(list(changes.values())))

    def _drop_reverted(self, changes: list[Change]) -> list[Change]:
        """Tira o par mudança + revert quando os dois estão pendentes: nada dele chega a quem usa.

        Um revert é reconhecido pela linha ``This reverts commit <sha>`` que o git e o botão do
        GitHub escrevem. Ele anula a mudança cujo commit na branch (merge ou squash) ou cujos commits
        todos ele reverte. Revert de algo já publicado não tem o original no intervalo e continua
        contando; o mesmo quando só um dos dois já saiu numa pré-release do ciclo.
        """
        dropped: set[int] = set()
        for index, revert in enumerate(changes):
            commits = revert.commits or []
            if not commits or not all(commit.subject.startswith('Revert "') for commit in commits):
                continue
            targets = [sha for commit in commits for sha in REVERTS.findall(commit.body)]
            if not targets:
                continue
            for other_index, original in enumerate(changes):
                if other_index == index or other_index in dropped or original.fresh != revert.fresh:
                    continue
                own = [original.sha, *(commit.sha for commit in original.commits)]
                # Todo sha revertido é desta mudança, e o revert a desfaz inteira: o commit dela na
                # branch (merge ou squash) ou cada um dos commits que ela trouxe.
                belongs = all(any(sha.startswith(target) for sha in own) for target in targets)
                whole = any(original.sha.startswith(target) for target in targets) or (
                    bool(original.commits)
                    and all(any(commit.sha.startswith(target) for target in targets) for commit in original.commits)
                )
                if belongs and whole:
                    dropped.update((index, other_index))
                    self.notes.append(
                        self.cfg.t("plan.note.reverted", revert=revert.key, original=original.key, title=original.title)
                    )
                    break
        return [change for index, change in enumerate(changes) if index not in dropped]

    def pending_pr(self, number: int, base_ref: str, head_ref: str) -> Change:
        """O PR ainda aberto, como se já estivesse mesclado em ``base_ref``."""
        change = self._pr_change(number, self.repo.resolve(head_ref), f"PR #{number}")
        change.pending = True
        change.head_sha = change.head_sha or change.sha
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
        raise ValueError(i18n.t("plan.analysis.schema"))
    entries = []
    for index, entry in enumerate(data["changes"]):
        where = f"changes[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(i18n.t("plan.analysis.object", where=where))
        pr, commit = entry.get("pr"), entry.get("commit")
        if isinstance(pr, bool) or not (isinstance(pr, int) or (isinstance(commit, str) and len(commit) >= 7)):
            raise ValueError(i18n.t("plan.analysis.reference", where=where))
        if entry.get("level") not in LEVELS:
            raise ValueError(i18n.t("plan.analysis.level", where=where, accepted=", ".join(LEVELS)))
        if not isinstance(entry.get("summary"), str) or not entry["summary"].strip():
            raise ValueError(i18n.t("plan.analysis.summary", where=where))
        evidence = entry.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise ValueError(i18n.t("plan.analysis.evidence", where=where))
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


def _same_sha(one: str, other: str) -> bool:
    shortest = min(len(one), len(other))
    return shortest >= 7 and one[:shortest].lower() == other[:shortest].lower()


def analysis_saw(analysis: dict[str, Any], change: Change, repo: Repo | None) -> bool:
    """A análise viu o último commit da mudança?

    Vale quando o ``head`` analisado é o último commit do PR ou o contém, ou quando contém o commit
    que a mudança deixou na branch (análise feita depois do merge, no PR de release). Análise sem
    ``head`` não prova o que viu e não conta.
    """
    head = str(analysis.get("head") or "").strip()
    if not head:
        return False
    last = change.head_sha or (change.sha if change.pending else "")
    if last and _same_sha(head, last):
        return True
    if repo is None or not repo.git.ok("cat-file", "-e", f"{head}^{{commit}}"):
        return False
    candidates = [last] if last else []
    if not change.pending:
        candidates.append(change.sha)
    return any(
        candidate and repo.git.ok("cat-file", "-e", f"{candidate}^{{commit}}")
        and repo.git.ok("merge-base", "--is-ancestor", candidate, head)
        for candidate in candidates
    )


def apply_analyses(changes: list[Change], analyses: list[dict[str, Any]], repo: Repo | None = None) -> list[str]:
    """Acrescenta a cada mudança o veredito da análise mais recente que a cobre.

    A análise só acrescenta sinais; como o nível é sempre o maior, ela eleva e nunca reduz. Um
    veredito por PR só vale se a análise viu o último commit do PR (``analysis_saw``); senão a
    mudança fica sem análise e a nota devolvida diz por quê.
    """
    notes: list[str] = []
    for change in changes:
        verdict = None
        stale = False
        for analysis in analyses:  # em ordem cronológica: a última que cita a mudança vence
            for entry in analysis["changes"]:
                by_pr = change.number is not None and entry.get("pr") == change.number
                shas = [*(commit.sha for commit in change.commits), change.sha]
                by_sha = "commit" in entry and any(sha.startswith(entry["commit"]) for sha in shas)
                if by_pr and not analysis_saw(analysis, change, repo):
                    stale = True
                    continue
                if by_pr or (change.number is None and by_sha):
                    verdict, stale = entry, False
        if verdict is None:
            if stale:
                notes.append(i18n.t("plan.note.stale_analysis", change=change.key))
            continue
        change.signals = [signal for signal in change.signals if signal.source != "agente"]
        change.signals.append(Signal(verdict["level"], "agente", verdict["summary"], bool(verdict["breaking"])))
        change.analyzed = True
    return notes


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
    """Sobreposição pedida no PR que pede a versão: label ``semver:*`` com o motivo na seção de versão.

    Vale enquanto o PR é novo, como o pedido dele: depois que uma pré-release o cobriu, a
    sobreposição não é reaplicada às versões seguintes (o piso do ciclo impede que o alvo desça).
    """
    prefix, chosen = cfg.label("override_prefix"), None
    for change in changes:
        if not change.fresh or not cfg.version_labels(change.labels):
            continue
        levels = [label[len(prefix) :] for label in change.labels if label.startswith(prefix)]
        levels = [level for level in levels if level in ("major", "minor", "patch")]
        if len(levels) == 1:
            marker = str(cfg.pr["override_reason_marker"])
            reason = marked_value(section(change.body, cfg.pr["version_section"]), marker)
            source = cfg.t("plan.override.source", label=f"{prefix}{levels[0]}", change=change.key)
            chosen = Override(levels[0], reason or cfg.t("plan.override.no_reason"), source)
    return chosen


def resolve_request(
    changes: list[Change], cfg: Config, *, forced: bool, manual: str | None
) -> tuple[list[str], str | None, list[str]]:
    """Quem pediu versão, o canal que sai (``None`` é a versão final) e as notas da decisão.

    Um pedido é o label de versão de um PR **novo**: ``release`` pede a final e ``release:<canal>``
    a pré-release do canal. Depois que uma pré-release cobriu o PR, o pedido dele já foi atendido.
    Vence o pedido mais maduro (final, depois os canais do mais ao menos maduro); a execução manual
    (``--prerelease`` ou ``--force``) vence os labels naquela execução.
    """
    release, order = cfg.label("release"), cfg.prerelease_channels
    notes: list[str] = []
    asked: list[tuple[Change, str | None]] = []
    for change in changes:
        if change.kind != "pr" or not change.fresh:
            continue
        for label in cfg.unknown_channel_labels(change.labels):
            accepted = ", ".join(f"`{cfg.channel_label(channel)}`" for channel in order) or "—"
            notes.append(cfg.t("plan.request.unknown_label", label=label, change=change.key, accepted=accepted))
        channels = cfg.requested_channels(change.labels)
        if release in change.labels:
            asked.append((change, None))
        elif channels:
            asked.append((change, channels[-1]))

    def shown(items: list[tuple[Change, str | None]]) -> str:
        return ", ".join(f"{change.key} (`{cfg.channel_label(ch) if ch else release}`)" for change, ch in items)

    if manual:
        channel: str | None = manual
    elif forced or not asked or any(ch is None for _, ch in asked):
        channel = None
    else:
        channel = max((ch for _, ch in asked if ch), key=order.index)
    others = [(change, ch) for change, ch in asked if ch != channel]
    winners = [(change, ch) for change, ch in asked if ch == channel]
    if others and manual:
        notes.append(cfg.t("plan.request.manual_prerelease", channel=manual, asked=shown(others)))
    elif others and forced:
        notes.append(cfg.t("plan.request.manual_final", asked=shown(others)))
    elif others:
        notes.append(cfg.t("plan.request.most_mature", winner=shown(winners), others=shown(others)))
    return [change.key for change, _ in asked], channel, notes


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
    collector: Collector | None = None,
) -> Plan:
    """Calcula o plano para ``head``. ``pending=(PR, head_ref)`` simula o merge de um PR aberto.

    ``prerelease`` é o canal pedido pela execução manual; sem ele, o tipo da versão (final ou
    pré-release de um canal) vem dos labels dos PRs novos (``resolve_request``).
    """
    line = repo.line(head, exclude=exclude_tag)
    base_tag = line.final_tag
    initial = Version.parse(str(cfg.versioning["initial_version"]))
    base_version = line.final or initial
    files_version = line.current or initial

    collector = collector or Collector(cfg, repo, github)
    changes = collector.collect(base_tag, head, list(line.cycle))
    if pending is not None:
        changes.append(collector.pending_pr(pending[0], head, pending[1]))
    analysis_notes = apply_analyses(changes, [*collector.recorded_analyses(changes), *(extra_analyses or [])], repo)
    requested_by, channel, request_notes = resolve_request(changes, cfg, forced=forced, manual=prerelease)

    plan = Plan(
        branch=branch,
        head=repo.resolve(pending[1] if pending else head),
        base_tag=base_tag,
        base_version=base_version,
        files_version=files_version,
        changes=changes,
        requested_by=requested_by,
        forced=forced,
        natural=max_level(change.level for change in changes),
        level="none",
        override=override or release_override(changes, cfg),
        version=None,
        prerelease=channel,
        previous_tag=line.current_tag,
        cycle=list(line.cycle),
    )
    zero_major = cfg.versioning["zero_major"]
    plan.level = plan.natural
    if plan.level == "none" and changes:
        plan.level = "patch"
        plan.notes.append(cfg.t("plan.note.at_least_patch"))
    if fixed_version is not None:
        plan.version = fixed_version
        plan.prerelease = fixed_version.pre[0] if fixed_version.is_prerelease else None
        return plan
    if not changes:
        if forced:
            text = cfg.t("plan.problem.nothing")
            plan.problems.append(Problem("nothing", text))
        return plan
    plan.notes += request_notes
    plan.notes += collector.notes
    plan.notes += analysis_notes
    if plan.override:
        plan.level = plan.override.level
        target = base_version.final.bump(plan.level)  # a sobreposição é literal, inclusive para chegar a 1.0.0
    else:
        target = next_version(base_version, plan.level if plan.level != "none" else "patch", zero_major)
        if base_version.major == 0 and plan.level == "major" and zero_major == "breaking-is-minor":
            plan.notes.append(cfg.t("plan.note.zero_major", label=cfg.override_label("major")))
    started = line.floor
    if started and started[1].key() > target.key():
        # O ciclo já publicou pré-releases de um alvo maior: descer faria a versão regredir.
        tag, core = started
        if plan.override:
            text = cfg.t("plan.cycle.override_below", target=target, floor=core, tag=tag)
            plan.problems.append(Problem(f"override-below-cycle:{core}", text))
        else:
            plan.notes.append(cfg.t("plan.cycle.floor", target=target, floor=core, tag=tag))
            plan.level, target = level_between(base_version, core), core
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
            plan.notes.append(cfg.t("plan.note.floor_raised", path=path, version=version, old=plan.level, new=raised))
            plan.level, target = raised, next_version(base_version, raised, zero_major)
        else:
            text = cfg.t("plan.problem.floor", path=path, version=version, target=target)
            plan.problems.append(Problem(f"floor:{path}@{version}", text))
    if started and target.key() > started[1].key():
        plan.notes.append(cfg.t("plan.cycle.target_changed", old=started[1], new=target, tag=started[0]))
    everything = repo.version_tags()
    if channel:
        final, allowed = target, cfg.prerelease_channels
        target = next_prerelease(final, channel, everything.values(), allowed)
        ahead = not_surpassed(target, line.cycle.values())
        if ahead is not None and not prerelease and ahead.pre[0] in allowed:
            # Pedido por label: em vez de regredir, a pré-release sai no canal em que o ciclo já está.
            plan.notes.append(
                cfg.t(
                    "plan.request.promoted",
                    label=cfg.channel_label(channel),
                    current=ahead.pre[0],
                    core=final,
                    tag=f"{cfg.tag_prefix}{ahead}",
                )
            )
            channel = plan.prerelease = ahead.pre[0]
            target = next_prerelease(final, channel, everything.values(), allowed)
        elif ahead is not None:
            text = cfg.t(
                "plan.channel.regressed",
                channel=channel,
                current=ahead.pre[0],
                core=final,
                tag=f"{cfg.tag_prefix}{ahead}",
            )
            plan.problems.append(Problem(f"channel-regressed:{channel}", text))
        current = line.current
        same = current is not None and current.final == final and current.pre[:1] == (channel,)
        if prerelease and same and not any(change.fresh for change in changes):
            # A promoção a um canal mais maduro não precisa de mudança nova; repetir o canal, sim.
            text = cfg.t("plan.prerelease.nothing_new", tag=line.current_tag, channel=channel)
            plan.problems.append(Problem("nothing-new", text))
    plan.version = target
    elsewhere = sorted(
        (tag for tag, version in everything.items() if version.final == target.final and tag not in line.tags),
        key=lambda tag: everything[tag].key(),
    )
    if f"{cfg.tag_prefix}{target}" in everything:
        text = cfg.t("plan.problem.tag_exists", tag=f"{cfg.tag_prefix}{target}")
        plan.problems.append(Problem("tag-exists", text))
    elif elsewhere:
        # Outra linha já usa este núcleo (o ciclo da branch principal, por exemplo): não se divide um número.
        text = cfg.t("plan.core.taken", core=target.core, tags=", ".join(elsewhere))
        plan.problems.append(Problem("core-taken", text))
    if cfg.versioning["require_analysis"] and not plan.override:
        uncovered = [change.key for change in changes if change.needs_review and not change.analyzed]
        if uncovered:
            text = cfg.t("plan.problem.analysis_missing", changes=", ".join(uncovered))
            plan.problems.append(Problem("analysis:" + ",".join(uncovered), text))
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


def changelog_entries(changes: Sequence[Change], cfg: Config, links: Links) -> dict[str, list[str]]:
    """``{id da seção: [linhas]}``: uma linha por issue entregue; sem issue, uma linha por PR ou commit."""
    entries: dict[str, list[str]] = {}
    delivered: dict[int, tuple[str, str, list[str]]] = {}
    for change in changes:
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
                suffix = " " + cfg.t("plan.changelog.part_of", issues=", ".join(links.issue(i.number) for i in typed))
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


def render_changelog_body(
    plan: Plan, cfg: Config, links: Links, *, hide: bool = True, changes: Sequence[Change] | None = None
) -> str:
    """As seções do changelog. ``changes`` restringe a lista: é como as notas mostram só as novidades."""
    entries = changelog_entries(plan.changes if changes is None else changes, cfg, links)
    hidden = set(cfg.changelog["hide"]) if hide else set()
    blocks = []
    for item in cfg.changelog["sections"]:
        lines = entries.get(item["id"])
        if lines and item["id"] not in hidden:
            blocks.append(f"### {item['title']}\n\n" + "\n".join(lines))
    return "\n\n".join(blocks) or cfg.t("plan.changelog.empty")


def render_justification(plan: Plan, cfg: Config, links: Links, *, table_note: str | None = None) -> str:
    """Por que este número: sinais de cada mudança, sobreposição e situação da análise por agente.

    Com ``table_note``, a tabela por mudança dá lugar a essa frase: é a forma compacta, para textos
    que o GitHub limita em tamanho.
    """
    lines = []
    origin = f"{plan.base_tag}" if plan.base_tag else cfg.t("plan.origin.initial", version=plan.base_version)
    if plan.version:
        target = f"{cfg.tag_prefix}{plan.version}"
        lines.append(cfg.t("plan.justification.headline", origin=origin, target=target, level=plan.level))
    if plan.override:
        override = plan.override
        lines.append(
            cfg.t(
                "plan.justification.override",
                level=override.level,
                source=override.source,
                natural=plan.natural,
                reason=override.reason,
            )
        )
    lines += plan.notes
    if plan.changes and table_note is not None:
        lines += ["", table_note]
    elif plan.changes:
        lines += ["", cfg.t("plan.justification.table_header"), "|---|---|---|"]
        for change in plan.changes:
            signals = "; ".join(
                f"{s.detail} → {s.level}" for s in change.signals if s.level != "none" or s.source == "agente"
            )
            review = sorted({hit.path for hit in change.surfaces if hit.action == "review"})
            if review and not change.analyzed:
                shown = ", ".join(f"`{path}`" for path in review[:5]) + (" …" if len(review) > 5 else "")
                signals += ("; " if signals else "") + cfg.t("plan.justification.unanalyzed", paths=shown)
            lines.append(
                f"| {links.change(change)} {change.title.replace('|', '/')} | {change.level} | {signals or '—'} |"
            )
    analyzed = sum(1 for change in plan.changes if change.analyzed)
    lines.append("")
    if analyzed == len(plan.changes) and plan.changes:
        lines.append(cfg.t("plan.justification.analysis_all"))
    elif analyzed:
        lines.append(cfg.t("plan.justification.analysis_some", analyzed=analyzed, total=len(plan.changes)))
    else:
        lines.append(cfg.t("plan.justification.analysis_none"))
    return "\n".join(lines).strip()


def plan_json(plan: Plan, cfg: Config) -> dict[str, Any]:
    return {
        "branch": plan.branch,
        "head": plan.head,
        "base_tag": plan.base_tag,
        "base_version": str(plan.base_version),
        "previous_tag": plan.previous_tag,
        "cycle": plan.cycle,
        "type": "prerelease" if plan.prerelease else "final",
        "channel": plan.prerelease,
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
                "fresh": change.fresh,
                "issues": [issue.__dict__ for issue in change.issues],
                "signals": [signal.__dict__ for signal in change.signals],
                "commits": [{"sha": commit.sha, "subject": commit.subject} for commit in change.commits],
                "public_surfaces": [hit.__dict__ for hit in change.surfaces],
            }
            for change in plan.changes
        ],
    }
