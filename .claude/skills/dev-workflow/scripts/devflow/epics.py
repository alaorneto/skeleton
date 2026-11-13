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

from .gh import run_gh
from .text import ANY_ISSUE, ITEM_ISSUE

SECTION_HEADING = re.compile(r"^#{2,3}\s+(?P<title>.+?)\s*$")
CHECKBOX_ITEM = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+(?P<rest>.*)$")


@dataclass(frozen=True)
class EpicSettings:
    """O que muda de projeto para projeto na leitura de um epic."""

    items_section: str = "itens"
    depends_marker: str = "depende de"
    epic_label: str = "epic"

    @property
    def depends_on(self) -> re.Pattern[str]:
        return re.compile(rf"\b{re.escape(self.depends_marker)}\b", re.IGNORECASE)


SETTINGS = EpicSettings()


def configure(*, items_section: str, depends_marker: str, epic_label: str) -> None:
    global SETTINGS
    SETTINGS = EpicSettings(items_section.strip().lower(), depends_marker, epic_label)


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
            return f"#{self.ref} vira sub-issue de #{self.target}"
        return f"#{self.target} fica bloqueada por #{self.ref}"


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
            in_section = heading.group("title").strip().lower() == SETTINGS.items_section
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
            plan.problems.append(f"item de outro repositório ignorado: {rest[:80]}")
            continue
        number = int(issue.group("number"))
        if number in seen:
            plan.problems.append(f"#{number} aparece mais de uma vez na lista")
            continue
        seen.add(number)
        blocked_by: list[int] = []
        depends = SETTINGS.depends_on.search(rest)
        if depends:
            for ref in ANY_ISSUE.finditer(rest[depends.end() :]):
                if not _same_repo(ref.group("repo"), repository):
                    plan.problems.append(f"#{number}: dependência de outro repositório ignorada ({ref.group(0)})")
                    continue
                dep = int(ref.group("number"))
                if dep == number:
                    plan.problems.append(f"#{number} depende de si mesma")
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
        return [], [*problems, f"#{epic} não existe ou não é uma issue"]
    skipped = set(skip)
    listed = {item.number for item in plan.items}
    for item in plan.items:
        number = item.number
        if number == epic:
            problems.append(f"#{epic} lista a si mesmo como item")
            continue
        if number in skipped:
            continue
        child = links.get(number)
        if child is None:
            problems.append(f"#{number} não existe ou não é uma issue")
            continue
        if child.parent is None:
            operations.append(Operation("sub-issue", epic, number))
        elif child.parent != epic:
            problems.append(f"#{number} já é sub-issue de {_ref(child.parent)}; parent não alterado")
        for dep in item.blocked_by:
            if links.get(dep) is None:
                problems.append(f"#{number} depende de #{dep}, que não existe ou não é uma issue")
            elif dep not in child.blocked_by:
                operations.append(Operation("blocked-by", number, dep))
    for native in epic_links.sub_issues:
        if native not in listed:
            problems.append(f"{_ref(native)} é sub-issue de #{epic} mas não está na seção Itens")
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
            in_section = heading.group("title").strip().lower() == SETTINGS.items_section
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
        raise RuntimeError(
            f"#{data['number']} tem mais de 100 sub-issues ou bloqueios; a leitura está incompleta "
            "e nenhuma ação automática é segura."
        )
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
            raise RuntimeError(f"gh api graphql falhou: {completed.stderr.strip()}") from exc
        data = payload.get("data") or {}
        repo_data = data.get("repository")
        if not isinstance(repo_data, dict):
            raise RuntimeError(f"repositório {repository} inacessível: {completed.stderr.strip()}")
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
        for number, checked in changes:
            verb = "marcar" if checked else "desmarcar"
            print(f"  {'[dry-run] ' if dry_run else ''}{verb} #{number} na seção Itens")
        if dry_run:
            return False
        error = _write_body(epic, new_body, repository, gh)
        if error:
            print(f"  ERRO ao atualizar a seção Itens de #{epic}: {error}")
            return True
        if attempt + 1 < CHECKLIST_ATTEMPTS:
            body = _fetch_epics([epic], repository, gh)[epic][0]
            links = fetch_links([item.number for item in parse_epic_items(body, repository).items], repository, gh)
    print(f"  ERRO: a seção Itens de #{epic} não estabilizou após {CHECKLIST_ATTEMPTS} tentativas")
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
        print(f"#{issue} não é sub-issue de um epic deste repositório; nada a fazer.")
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
    lines = [
        f"- #{child.number} {child.title} — {'dispensada' if child.state_reason == 'NOT_PLANNED' else 'concluída'}"
        for child in children
    ]
    comment = (
        f"Fechado automaticamente pelo fluxo de epics: as {len(children)} sub-issues estão fechadas.\n\n"
        + "\n".join(lines)
        + "\n\nSe algum critério de conclusão do epic ainda faltar, reabra o epic e registre o que falta."
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
            print(f"#{current} não é sub-issue de um epic deste repositório; nada a fechar.")
            return 0
        open_, labels = _issue_state(parent, repository, gh)
        if SETTINGS.epic_label not in labels:
            print(f"#{parent} não tem o label epic; não é fechado automaticamente.")
            return 0
        if not open_:
            print(f"#{parent} já está fechado.")
            return 0
        epic = fetch_links([parent], repository, gh).get(parent)
        children = epic.sub_issues if epic else ()
        if not children:
            print(f"#{parent} não tem sub-issues nativas; nada a fechar.")
            return 0
        foreign = [str(child) for child in children if not isinstance(child, int)]
        if foreign:
            print(f"#{parent} tem sub-issues de outro repositório ({', '.join(foreign)}); feche-o à mão.")
            return 0
        states = fetch_links([child for child in children if isinstance(child, int)], repository, gh)
        pending = [f"#{number}" for number, state in sorted(states.items()) if state is None or not state.closed]
        if pending:
            print(f"#{parent} continua aberto: {', '.join(pending)} ainda aberta(s).")
            return 0
        reason, comment = epic_summary([state for _, state in sorted(states.items()) if state is not None])
        if dry_run:
            print(f"[dry-run] fecharia #{parent} como {reason}:\n{comment}")
            return 0
        if not _issue_state(parent, repository, gh)[0]:  # outra filha fechou ao mesmo tempo
            print(f"#{parent} já foi fechado por outra execução.")
            return 0
        completed = gh(["issue", "close", str(parent), "--repo", repository, "--reason", reason, "--comment", comment])
        if completed.returncode != 0:
            print(f"ERRO ao fechar #{parent}: {completed.stderr.strip() or completed.stdout.strip()}")
            return 1
        print(f"#{parent} fechado como {reason}.")
        grandparent = epic.parent if epic else None
        if isinstance(grandparent, int) and sync_checklist([grandparent], repository, dry_run=False, gh=gh):
            return 1
        current = parent
    print(f"ERRO: a cadeia de epics acima de #{issue} passa de {CLOSE_EPIC_DEPTH} níveis.")
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
            problems.insert(0, f"#{epic} não tem o label epic")
        problems.extend(
            f"#{number} está em mais de um epic ({', '.join(f'#{e}' for e in owners)}); vincule à mão"
            for number, owners in shared.items()
            if epic in owners
        )
        print(f"#{epic}: {len(plan.items)} itens, {len(operations)} vínculos a criar", end="")
        print(f", {plan.candidates} candidatos sem issue" if plan.candidates else "")
        for operation in operations:
            if dry_run:
                print(f"  [dry-run] {operation.describe()}")
                continue
            error = _apply(operation, repository, gh)
            if error:
                failed = True
                print(f"  ERRO {operation.describe()}: {error}")
            else:
                print(f"  ok {operation.describe()}")
        body = epics[epic][0]
        failed |= sync_epic_checklist(epic, body, repository, dry_run=dry_run, gh=gh, links=links)
        for problem in problems:
            print(f"  divergência: {problem}")
        failed = failed or bool(problems)
    return 1 if failed else 0
