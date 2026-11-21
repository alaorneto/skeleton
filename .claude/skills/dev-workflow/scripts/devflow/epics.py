"""Epics: sub-issues e "blocked by" nativos, caixas da seção de itens e fechamento automático.

O vínculo oficial entre um epic e suas filhas é o relacionamento nativo de sub-issue do GitHub;
o texto da seção de itens do epic é o resumo legível que estas funções reconciliam com ele.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from . import i18n
from .config import Config
from .gh import run_gh
from .text import ANY_ISSUE, ITEM_ISSUE, equivalent_titles, normalize_title, same_title

SECTION_HEADING = re.compile(r"^#{2,3}\s+(?P<title>.+?)\s*$")
CHECKBOX_ITEM = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+(?P<rest>.*)$")


@dataclass(frozen=True)
class EpicSettings:
    """O que muda de projeto para projeto na leitura de um epic.

    Sem padrões próprios: os valores vêm sempre da configuração (``configure``).
    """

    items_section: str  # já normalizado por ``normalize_title``
    depends_marker: str
    epic_label: str

    @property
    def depends_on(self) -> re.Pattern[str]:
        # O marcador do idioma do projeto e o equivalente nos outros: `depende de` e `depends on`.
        markers = [self.depends_marker, *equivalent_titles(self.depends_marker)]
        return re.compile(rf"\b(?:{'|'.join(re.escape(marker) for marker in markers)})\b", re.IGNORECASE)


def _settings(items_section: str, depends_marker: str, epic_label: str) -> EpicSettings:
    return EpicSettings(normalize_title(items_section), depends_marker, epic_label)


def configure(*, items_section: str, depends_marker: str, epic_label: str) -> None:
    global SETTINGS  # noqa: PLW0603
    SETTINGS = _settings(items_section, depends_marker, epic_label)


_DEFAULTS = Config()
# Até ``configure`` receber os valores do projeto, valem os padrões da configuração.
SETTINGS = _settings(_DEFAULTS.issues["items_section"], _DEFAULTS.issues["depends_marker"], _DEFAULTS.label("epic"))


@dataclass(frozen=True)
class EpicItem:
    number: int
    blocked_by: tuple[int, ...] = ()


@dataclass
class EpicPlan:
    """O que a seção "Itens" de um epic declara."""

    items: list[EpicItem] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    candidates: int = 0  # itens sem issue criada (ex.: ciclo 2); não são erro


@dataclass(frozen=True)
class IssueLinks:
    """Estado nativo de uma issue. Referências a outro repositório viram ``owner/repo#N``."""

    number: int
    parent: int | str | None = None
    sub_issues: tuple[int | str, ...] = ()
    blocked_by: tuple[int | str, ...] = ()
    closed: bool = False
    title: str = ""
    state_reason: str | None = None  # COMPLETED, NOT_PLANNED, REOPENED ou None


@dataclass(frozen=True)
class Operation:
    kind: str  # "sub-issue" (target=epic, ref=filha) ou "blocked-by" (target=filha, ref=bloqueadora)
    target: int
    ref: int

    def describe(self) -> str:
        if self.kind == "sub-issue":
            return i18n.t("epics.operation.sub_issue", ref=self.ref, target=self.target)
        return i18n.t("epics.operation.blocked_by", target=self.target, ref=self.ref)


def _same_repo(repo: str | None, repository: str) -> bool:
    return repo is None or repo.lower() == repository.lower()


def parse_epic_items(body: str, repository: str) -> EpicPlan:
    """Lê as linhas ``- [ ] #N título — depende de #M, #K`` da seção "Itens"."""
    plan = EpicPlan()
    in_section = False
    seen: set[int] = set()
    for line in body.splitlines():
        heading = SECTION_HEADING.match(line)
        if heading:
            in_section = same_title(heading.group("title"), SETTINGS.items_section)
            continue
        if not in_section:
            continue
        item = CHECKBOX_ITEM.match(line)
        if not item:
            continue
        rest = item.group("rest").strip()
        issue = ITEM_ISSUE.match(rest)
        if not issue:
            plan.candidates += 1
            continue
        if not _same_repo(issue.group("repo"), repository):
            plan.problems.append(i18n.t("epics.item.foreign", item=rest[:80]))
            continue
        number = int(issue.group("number"))
        if number in seen:
            plan.problems.append(i18n.t("epics.item.repeated", number=number))
            continue
        seen.add(number)
        blocked_by: list[int] = []
        depends = SETTINGS.depends_on.search(rest)
        if depends:
            for ref in ANY_ISSUE.finditer(rest[depends.end() :]):
                if not _same_repo(ref.group("repo"), repository):
                    plan.problems.append(i18n.t("epics.item.foreign_dependency", number=number, ref=ref.group(0)))
                    continue
                dep = int(ref.group("number"))
                if dep == number:
                    plan.problems.append(i18n.t("epics.item.self_dependency", number=number))
                elif dep not in blocked_by:
                    blocked_by.append(dep)
        plan.items.append(EpicItem(number, tuple(blocked_by)))
    return plan


def shared_items(plans: Mapping[int, EpicPlan]) -> dict[int, list[int]]:
    """Issues listadas em mais de um epic: ``{issue: [epics]}``. Uma issue tem no máximo um parent."""
    owners: dict[int, list[int]] = {}
    for epic, plan in sorted(plans.items()):
        for item in plan.items:
            owners.setdefault(item.number, []).append(epic)
    return {number: epics for number, epics in owners.items() if len(epics) > 1}


def _ref(value: int | str) -> str:
    return f"#{value}" if isinstance(value, int) else value


def plan_epic(
    epic: int,
    plan: EpicPlan,
    links: Mapping[int, IssueLinks | None],
    skip: Iterable[int] = (),
) -> tuple[list[Operation], list[str]]:
    """Operações que faltam para o GitHub refletir a lista, e divergências que exigem uma pessoa."""
    operations: list[Operation] = []
    problems = list(plan.problems)
    epic_links = links.get(epic)
    if epic_links is None:
        return [], [*problems, i18n.t("epics.not_issue", number=epic)]
    skipped = set(skip)
    listed = {item.number for item in plan.items}
    for item in plan.items:
        number = item.number
        if number == epic:
            problems.append(i18n.t("epics.lists_itself", epic=epic))
            continue
        if number in skipped:
            continue
        child = links.get(number)
        if child is None:
            problems.append(i18n.t("epics.not_issue", number=number))
            continue
        if child.parent is None:
            operations.append(Operation("sub-issue", epic, number))
        elif child.parent != epic:
            problems.append(i18n.t("epics.other_parent", number=number, parent=_ref(child.parent)))
        for dep in item.blocked_by:
            if links.get(dep) is None:
                problems.append(i18n.t("epics.missing_dependency", number=number, dep=dep))
            elif dep not in child.blocked_by:
                operations.append(Operation("blocked-by", number, dep))
    for native in epic_links.sub_issues:
        if native not in listed:
            problems.append(i18n.t("epics.unlisted", ref=_ref(native), epic=epic))
    return operations, problems


CHECKBOX_PARTS = re.compile(r"^(?P<head>\s*[-*]\s+\[)(?P<mark>[ xX])(?P<tail>\]\s+)(?P<rest>.*)$")


def reconcile_checklist(body: str, closed: Mapping[int, bool], repository: str) -> tuple[str, list[tuple[int, bool]]]:
    """Marca ``[x]`` nos itens fechados da seção "Itens" e ``[ ]`` nos abertos.

    Só toca a caixa das linhas cujo número está em ``closed``; o resto do corpo, inclusive as
    quebras de linha, fica idêntico. Devolve o corpo novo e as mudanças ``(issue, marcada)``.
    """
    out: list[str] = []
    changes: list[tuple[int, bool]] = []
    in_section = False
    for line in body.splitlines(keepends=True):
        text = line.rstrip("\r\n")
        ending = line[len(text) :]
        heading = SECTION_HEADING.match(text)
        if heading:
            in_section = same_title(heading.group("title"), SETTINGS.items_section)
        parts = CHECKBOX_PARTS.match(text) if in_section else None
        issue = ITEM_ISSUE.match(parts.group("rest").strip()) if parts else None
        if parts and issue and _same_repo(issue.group("repo"), repository):
            number = int(issue.group("number"))
            checked = parts.group("mark") != " "
            if number in closed and closed[number] != checked:
                mark = "x" if closed[number] else " "
                text = f"{parts.group('head')}{mark}{parts.group('tail')}{parts.group('rest')}"
                changes.append((number, closed[number]))
        out.append(text + ending)
    return "".join(out), changes


# Leitura em lote pelo GraphQL; escrita pelo `gh issue edit`, que valida os vínculos.

_LINK_FIELDS = (
    "__typename ... on Issue { number state stateReason title "
    "parent { number repository { nameWithOwner } } "
    "subIssues(first: 100) { pageInfo { hasNextPage } nodes { number repository { nameWithOwner } } } "
    "blockedBy(first: 100) { pageInfo { hasNextPage } nodes { number repository { nameWithOwner } } } }"
)
GRAPHQL_BATCH = 50

GhRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run_gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return run_gh(args)


def _node_ref(node: object, repository: str) -> int | str | None:
    if not isinstance(node, dict):
        return None
    repo = node.get("repository")
    name = repo.get("nameWithOwner") if isinstance(repo, dict) else None
    number = int(node["number"])
    return number if not name or _same_repo(str(name), repository) else f"{name}#{number}"


def _nodes(value: object, repository: str) -> tuple[int | str, ...]:
    nodes = value.get("nodes") if isinstance(value, dict) else None
    refs = (_node_ref(node, repository) for node in nodes or [])
    return tuple(ref for ref in refs if ref is not None)


def _truncated(value: object) -> bool:
    page = value.get("pageInfo") if isinstance(value, dict) else None
    return isinstance(page, dict) and bool(page.get("hasNextPage"))  # sem pageInfo: página única


def parse_links(data: Mapping[str, object] | None, repository: str) -> IssueLinks | None:
    """Converte um nó do GraphQL; ``None`` para inexistente ou pull request.

    Falha fechado se ``subIssues`` ou ``blockedBy`` tiver outra página: com a lista incompleta,
    nenhuma ação automática (fechar o epic, criar vínculos, marcar caixas) é segura.
    """
    if not data or data.get("__typename") != "Issue":
        return None
    if _truncated(data.get("subIssues")) or _truncated(data.get("blockedBy")):
        raise RuntimeError(i18n.t("epics.truncated", number=data["number"]))
    return IssueLinks(
        number=int(str(data["number"])),
        parent=_node_ref(data.get("parent"), repository),
        sub_issues=_nodes(data.get("subIssues"), repository),
        blocked_by=_nodes(data.get("blockedBy"), repository),
        closed=data.get("state") == "CLOSED",
        title=str(data.get("title") or ""),
        state_reason=str(data["stateReason"]) if data.get("stateReason") else None,
    )


def fetch_links(numbers: Iterable[int], repository: str, gh: GhRunner = _run_gh) -> dict[int, IssueLinks | None]:
    owner, name = repository.split("/", 1)
    wanted = sorted(set(numbers))
    result: dict[int, IssueLinks | None] = {}
    for start in range(0, len(wanted), GRAPHQL_BATCH):
        chunk = wanted[start : start + GRAPHQL_BATCH]
        aliases = " ".join(f"i{n}: issueOrPullRequest(number: {n}) {{ {_LINK_FIELDS} }}" for n in chunk)
        query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {aliases} }} }}'
        completed = gh(["api", "graphql", "-f", f"query={query}"])
        try:  # números inexistentes geram "errors" e saída != 0, mas os demais vêm em "data"
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(i18n.t("epics.graphql_failed", error=completed.stderr.strip())) from exc
        data = payload.get("data") or {}
        repo_data = data.get("repository")
        if not isinstance(repo_data, dict):
            raise RuntimeError(
                i18n.t("epics.repository_unreachable", repository=repository, error=completed.stderr.strip())
            )
        for n in chunk:
            result[n] = parse_links(repo_data.get(f"i{n}"), repository)
    return result


def _fetch_epics(selection: Sequence[int] | None, repository: str, gh: GhRunner) -> dict[int, tuple[str, list[str]]]:
    """``{epic: (body, labels)}`` para os epics pedidos ou, sem seleção, todos com o label ``epic``."""
    fields = "number,body,labels"
    if selection is None:
        listing = [
            "issue",
            "list",
            "--repo",
            repository,
            "--label",
            SETTINGS.epic_label,
            "--state",
            "all",
            "--limit",
            "500",
        ]
        completed = gh([*listing, "--json", fields])
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip())
        raw = json.loads(completed.stdout)
    else:
        raw = []
        for number in selection:
            completed = gh(["issue", "view", str(number), "--repo", repository, "--json", fields])
            if completed.returncode != 0:
                raise RuntimeError(f"#{number}: {completed.stderr.strip()}")
            raw.append(json.loads(completed.stdout))
    return {
        int(issue["number"]): (str(issue.get("body") or ""), [str(label["name"]) for label in issue.get("labels", [])])
        for issue in raw
    }


def _apply(operation: Operation, repository: str, gh: GhRunner) -> str | None:
    flag = "--add-sub-issue" if operation.kind == "sub-issue" else "--add-blocked-by"
    completed = gh(["issue", "edit", str(operation.target), "--repo", repository, flag, str(operation.ref)])
    return None if completed.returncode == 0 else (completed.stderr.strip() or completed.stdout.strip())


def _write_body(epic: int, body: str, repository: str, gh: GhRunner) -> str | None:
    handle, path = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as file:
            file.write(body)
        completed = gh(["issue", "edit", str(epic), "--repo", repository, "--body-file", path])
    finally:
        os.unlink(path)
    return None if completed.returncode == 0 else (completed.stderr.strip() or completed.stdout.strip())


CHECKLIST_ATTEMPTS = 5


def sync_epic_checklist(
    epic: int, body: str, repository: str, *, dry_run: bool, gh: GhRunner, links: Mapping[int, IssueLinks | None]
) -> bool:
    """Reconcilia as caixas de um epic com o estado das issues. Devolve ``True`` se falhou.

    Depois de cada escrita, relê o corpo e o estado e reconcilia de novo: se outra execução
    gravou um corpo antigo no meio-tempo, a última a escrever corrige o resultado.
    """
    for attempt in range(CHECKLIST_ATTEMPTS):
        plan = parse_epic_items(body, repository)
        closed = {item.number: state.closed for item in plan.items if (state := links.get(item.number)) is not None}
        new_body, changes = reconcile_checklist(body, closed, repository)
        if not changes:
            return False
        prefix = "[dry-run] " if dry_run else ""
        for number, checked in changes:
            if checked:
                print("  " + i18n.t("epics.checklist.check", prefix=prefix, number=number))
            else:
                print("  " + i18n.t("epics.checklist.uncheck", prefix=prefix, number=number))
        if dry_run:
            return False
        error = _write_body(epic, new_body, repository, gh)
        if error:
            print("  " + i18n.t("epics.checklist.write_failed", epic=epic, error=error))
            return True
        if attempt + 1 < CHECKLIST_ATTEMPTS:
            body = _fetch_epics([epic], repository, gh)[epic][0]
            links = fetch_links([item.number for item in parse_epic_items(body, repository).items], repository, gh)
    print("  " + i18n.t("epics.checklist.unstable", epic=epic, attempts=CHECKLIST_ATTEMPTS))
    return True


def sync_checklist(selection: Sequence[int] | None, repository: str, *, dry_run: bool, gh: GhRunner = _run_gh) -> int:
    """Só as caixas da seção "Itens"; não cria vínculos nem falha por divergências de vínculo."""
    epics = _fetch_epics(selection, repository, gh)
    wanted = {item.number for body, _ in epics.values() for item in parse_epic_items(body, repository).items}
    links = fetch_links(wanted, repository, gh)
    failed = False
    for epic in sorted(epics):
        print(f"#{epic}:")
        failed |= sync_epic_checklist(epic, epics[epic][0], repository, dry_run=dry_run, gh=gh, links=links)
    return 1 if failed else 0


def sync_parent_checklist(issue: int, repository: str, *, dry_run: bool, gh: GhRunner = _run_gh) -> int:
    """Usado pelo workflow ao fechar ou reabrir ``issue``: reconcilia o epic pai nativo, se houver."""
    links = fetch_links([issue], repository, gh).get(issue)
    parent = links.parent if links else None
    if not isinstance(parent, int):
        print(i18n.t("epics.checklist.no_parent", issue=issue))
        return 0
    return sync_checklist([parent], repository, dry_run=dry_run, gh=gh)


CLOSE_EPIC_DEPTH = 10  # epics dentro de epics; o limite só evita ciclo de parents


def _issue_state(number: int, repository: str, gh: GhRunner) -> tuple[bool, list[str]]:
    """``(aberta, labels)`` lidos na hora, logo antes de decidir fechar."""
    completed = gh(["issue", "view", str(number), "--repo", repository, "--json", "state,labels"])
    if completed.returncode != 0:
        raise RuntimeError(f"#{number}: {completed.stderr.strip()}")
    data = json.loads(completed.stdout)
    return data.get("state") == "OPEN", [str(label["name"]) for label in data.get("labels", [])]


def epic_summary(children: Sequence[IssueLinks]) -> tuple[str, str]:
    """``(motivo, comentário)`` do fechamento automático de um epic com todas as filhas fechadas."""
    reason = "completed" if any(child.state_reason != "NOT_PLANNED" for child in children) else "not planned"
    completed, not_planned = i18n.t("epics.summary.completed"), i18n.t("epics.summary.not_planned")
    lines = [
        i18n.t(
            "epics.summary.item",
            number=child.number,
            title=child.title,
            state=not_planned if child.state_reason == "NOT_PLANNED" else completed,
        )
        for child in children
    ]
    comment = (
        i18n.t("epics.summary.head", count=len(children))
        + "\n\n"
        + "\n".join(lines)
        + "\n\n"
        + i18n.t("epics.summary.foot")
    )
    return reason, comment


def close_completed_parents(issue: int, repository: str, *, dry_run: bool, gh: GhRunner = _run_gh) -> int:
    """Fecha o epic pai de ``issue`` quando todas as sub-issues nativas dele estão fechadas.

    Sobe a cadeia: um fechamento feito com o token do workflow não dispara outro evento
    ``issues: closed``, então o próprio run atualiza as caixas do epic de cima e o verifica.
    """
    current = issue
    for _ in range(CLOSE_EPIC_DEPTH):
        links = fetch_links([current], repository, gh).get(current)
        parent = links.parent if links else None
        if not isinstance(parent, int):
            print(i18n.t("epics.close.no_parent", issue=current))
            return 0
        open_, labels = _issue_state(parent, repository, gh)
        if SETTINGS.epic_label not in labels:
            print(i18n.t("epics.close.not_epic", parent=parent))
            return 0
        if not open_:
            print(i18n.t("epics.close.already_closed", parent=parent))
            return 0
        epic = fetch_links([parent], repository, gh).get(parent)
        children = epic.sub_issues if epic else ()
        if not children:
            print(i18n.t("epics.close.no_children", parent=parent))
            return 0
        foreign = [str(child) for child in children if not isinstance(child, int)]
        if foreign:
            print(i18n.t("epics.close.foreign", parent=parent, foreign=", ".join(foreign)))
            return 0
        states = fetch_links([child for child in children if isinstance(child, int)], repository, gh)
        pending = [f"#{number}" for number, state in sorted(states.items()) if state is None or not state.closed]
        if pending:
            print(i18n.t("epics.close.pending", parent=parent, pending=", ".join(pending)))
            return 0
        reason, comment = epic_summary([state for _, state in sorted(states.items()) if state is not None])
        if dry_run:
            print(i18n.t("epics.close.dry_run", parent=parent, reason=reason, comment=comment))
            return 0
        if not _issue_state(parent, repository, gh)[0]:  # outra filha fechou ao mesmo tempo
            print(i18n.t("epics.close.raced", parent=parent))
            return 0
        completed = gh(["issue", "close", str(parent), "--repo", repository, "--reason", reason, "--comment", comment])
        if completed.returncode != 0:
            error = completed.stderr.strip() or completed.stdout.strip()
            print(i18n.t("epics.close.failed", parent=parent, error=error))
            return 1
        print(i18n.t("epics.close.done", parent=parent, reason=reason))
        grandparent = epic.parent if epic else None
        if isinstance(grandparent, int) and sync_checklist([grandparent], repository, dry_run=False, gh=gh):
            return 1
        current = parent
    print(i18n.t("epics.close.too_deep", issue=issue, depth=CLOSE_EPIC_DEPTH))
    return 1


def sync_epic(selection: Sequence[int] | None, repository: str, *, dry_run: bool, gh: GhRunner = _run_gh) -> int:
    epics = _fetch_epics(selection, repository, gh)
    plans = {epic: parse_epic_items(body, repository) for epic, (body, _) in epics.items()}
    shared = shared_items(plans)
    wanted = set(epics)
    for plan in plans.values():
        for item in plan.items:
            wanted.add(item.number)
            wanted.update(item.blocked_by)
    links = fetch_links(wanted, repository, gh)

    failed = False
    for epic in sorted(epics):
        plan = plans[epic]
        operations, problems = plan_epic(epic, plan, links, skip=shared)
        if SETTINGS.epic_label not in epics[epic][1]:
            problems.insert(0, i18n.t("epics.sync.not_epic", epic=epic))
        problems.extend(
            i18n.t("epics.sync.shared", number=number, epics=", ".join(f"#{e}" for e in owners))
            for number, owners in shared.items()
            if epic in owners
        )
        print(i18n.t("epics.sync.summary", epic=epic, items=len(plan.items), links=len(operations)), end="")
        print(i18n.t("epics.sync.candidates", count=plan.candidates) if plan.candidates else "")
        for operation in operations:
            if dry_run:
                print(f"  [dry-run] {operation.describe()}")
                continue
            error = _apply(operation, repository, gh)
            if error:
                failed = True
                print("  " + i18n.t("epics.sync.failed", operation=operation.describe(), error=error))
            else:
                print(f"  ok {operation.describe()}")
        body = epics[epic][0]
        failed |= sync_epic_checklist(epic, body, repository, dry_run=dry_run, gh=gh, links=links)
        for problem in problems:
            print("  " + i18n.t("epics.sync.problem", problem=problem))
        failed = failed or bool(problems)
    return 1 if failed else 0
