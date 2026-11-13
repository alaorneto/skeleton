"""Versionamento semântico automatizado (``release.py <subcomando>``).

- ``status`` / ``plan [--format md|json]``: o que se acumulou desde a última tag e a próxima versão.
- ``next [--level L]``: só o próximo número.
- ``check``: os arquivos de versão declarados estão coerentes com a versão corrente?
- ``bump --to X [--dry-run]``: escreve uma versão nos arquivos declarados (uso manual).
- ``preview --pr N --base-ref R --head-ref H [--comment]``: prévia de versão de um PR aberto.
- ``record-analysis --pr N --file F``: grava no PR a análise de versão feita por um agente.
- ``baseline [--ref R] [--push]``: cria a tag de base quando o projeto ainda não tem tag.
- ``run [--force] [--level L --reason "..."] [--prerelease canal] [--dry-run]``: publica o que
  estiver pendente. É a máquina de estados que o workflow de release executa; rodar de novo
  depois de concluir não faz nada, e rodar depois de uma falha conclui o que faltou.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import Config, ConfigError, load
from .gh import CommandError, Git, GitHub, current_repository, is_trusted_author, utf8_streams
from .plan import (
    ANALYSIS_MARK,
    Links,
    Override,
    Plan,
    Repo,
    build_plan,
    plan_json,
    render_changelog_body,
    render_justification,
    validate_analysis,
)
from .semver import LEVELS, Version, highest, next_version
from .text import REVERTS

PREVIEW_MARK = "<!-- dev-workflow:preview -->"
RELEASED_MARK = "<!-- dev-workflow:released {tag} -->"
RELEASE_PR_MARK = "<!-- dev-workflow:release-pr -->"
VERSION_HEADING = re.compile(r"^## \[?v?\d+\.\d+\.\d+", re.MULTILINE)
REMOTE = "origin"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
MAX_ROUNDS = 4


class ReleaseError(RuntimeError):
    """Erro que interrompe a release com mensagem para quem opera."""


# --- arquivos de versão ------------------------------------------------------------------------


def tokens(current: Version, new: Version) -> dict[str, str]:
    values: dict[str, str] = {"date": datetime.datetime.now(datetime.UTC).date().isoformat()}
    for name, version in (("current", current), ("new", new)):
        values[name] = str(version)
        values[f"{name}_pep440"] = version.pep440()
        values[f"{name}_core"] = version.core
        values[f"{name}_major"] = str(version.major)
        values[f"{name}_minor"] = str(version.minor)
        values[f"{name}_patch"] = str(version.patch)
        values[f"{name}_next_minor"] = f"{version.major}.{version.minor + 1}.0"
        values[f"{name}_next_major"] = f"{version.major + 1}.0.0"
    return values


def expand(template: str, values: dict[str, str], *, escape: bool = False) -> str:
    for name, value in values.items():
        template = template.replace("{" + name + "}", re.escape(value) if escape else value)
    return template


def rewrite(text: str, entry: dict[str, Any], current: Version, new: Version) -> str:
    """Aplica uma entrada de ``versioning.files`` ao conteúdo; erro se as ocorrências não batem."""
    values, expected = tokens(current, new), int(entry.get("count", 1))
    if entry.get("search"):
        needle = expand(entry["search"], values)
        replacement = expand(entry.get("replace", entry["search"].replace("{current", "{new")), values)
        found = text.count(needle)
        if found != expected:
            raise ReleaseError(
                f"{entry['path']}: esperava {expected} ocorrência(s) de `{needle}`, encontrei {found}. "
                "O arquivo divergiu da versão corrente ou o padrão em `versioning.files` está errado."
            )
        return text.replace(needle, replacement)
    pattern = re.compile(expand(entry["regex"], values, escape=True), re.MULTILINE)
    found = len(pattern.findall(text))
    if found != expected:
        raise ReleaseError(
            f"{entry['path']}: esperava {expected} ocorrência(s) de /{entry['regex']}/, encontrei {found}."
        )
    replacement = expand(entry["replace"], values)
    return pattern.sub(lambda _match: replacement, text)


def apply_version_files(
    cfg: Config, current: Version, new: Version, *, write: bool = True, root: Path | None = None
) -> list[str]:
    """Escreve ``new`` em todos os arquivos declarados. Devolve os caminhos que mudaram.

    ``root`` lê os arquivos de outra árvore (o checkout do PR) com a configuração desta.
    """
    base = root or cfg.root
    contents: dict[str, tuple[str, str]] = {}
    for entry in cfg.versioning["files"]:
        path = base / entry["path"]
        if entry["path"] not in contents:
            try:
                original = path.read_bytes().decode("utf-8")
            except OSError as exc:
                raise ReleaseError(f"{entry['path']}: não consegui ler ({exc}).") from exc
            contents[entry["path"]] = (original, original)
        original, text = contents[entry["path"]]
        contents[entry["path"]] = (original, rewrite(text, entry, current, new))
    changed = [name for name, (original, text) in contents.items() if original != text]
    if write:
        for name in changed:
            (base / name).write_bytes(contents[name][1].encode("utf-8"))
    return changed


def check_version_files(cfg: Config, current: Version, *, root: Path | None = None) -> list[str]:
    """Divergências entre os arquivos e a versão corrente: reescrever para ela não pode mudar nada."""
    try:
        drifted = apply_version_files(cfg, current, current, write=False, root=root)
    except ReleaseError as exc:
        return [str(exc)]
    return [f"{name}: o conteúdo não corresponde à versão {current}." for name in drifted]


# --- changelog ---------------------------------------------------------------------------------


def changelog_heading(plan: Plan, cfg: Config, links: Links) -> str:
    version, tag = str(plan.version), plan.tag(cfg)
    date = datetime.datetime.now(datetime.UTC).date().isoformat()
    if links.web and plan.base_tag:
        return f"## [{version}]({links.web}/compare/{plan.base_tag}...{tag}) — {date}"
    if links.web:
        return f"## [{version}]({links.web}/releases/tag/{tag}) — {date}"
    return f"## {version} — {date}"


def update_changelog(existing: str | None, version: str, heading: str, body: str, cfg: Config) -> str:
    """Insere a seção da versão no topo; se ela já existe, substitui (reexecução idempotente)."""
    block = f"{heading}\n\n{body.strip()}\n"
    if not existing or not existing.strip():
        return f"{cfg.changelog['title']}\n\n{cfg.changelog['intro']}\n\n{block}"
    own = re.compile(rf"^## \[?v?{re.escape(version)}\]?(?=[\s(]|$)", re.MULTILINE)
    match = own.search(existing)
    if match:
        following = VERSION_HEADING.search(existing, match.end())
        tail = existing[following.start() :] if following else ""
        return existing[: match.start()] + block + ("\n" + tail if tail else "")
    first = VERSION_HEADING.search(existing)
    if first:
        return existing[: first.start()] + block + "\n" + existing[first.start() :]
    return existing.rstrip("\n") + "\n\n" + block


def release_notes(plan: Plan, cfg: Config, links: Links, extra: str = "") -> str:
    parts = [
        render_changelog_body(plan, cfg, links, hide=False),
        "### Por que este número\n\n" + render_justification(plan, cfg, links),
    ]
    if extra:
        parts.append(extra)
    return "\n\n".join(parts) + "\n"


# --- contexto de execução ----------------------------------------------------------------------


class Session:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.git = Git(cfg.root)
        self.repo = Repo(cfg, self.git)
        self.github = GitHub(current_repository(self.git))
        self.links = Links(self.github.web)

    @property
    def has_remote(self) -> bool:
        return self.git.ok("remote", "get-url", REMOTE)

    def branch(self) -> str:
        return os.environ.get("RELEASE_BRANCH") or self.git("symbolic-ref", "--short", "HEAD", check=False)

    def identity(self) -> list[str]:
        """Opções ``-c`` para commits e tags quando o git não tem autor configurado (CI)."""
        if self.git("config", "user.name", check=False) and self.git("config", "user.email", check=False):
            return []
        return ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}"]


def _write_temp(text: str) -> str:
    handle, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
        file.write(text)
    return path


def _output(**values: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as file:
            file.writelines(f"{key}={value}\n" for key, value in values.items())


# --- preparar e publicar -----------------------------------------------------------------------


def prepare(session: Session, plan: Plan) -> str:
    """Escreve os arquivos e o changelog e cria o commit de release. Devolve o SHA."""
    cfg, git = session.cfg, session.git
    assert plan.version is not None
    tag = str(plan.tag(cfg))
    paths = apply_version_files(cfg, plan.files_version, plan.version)
    changelog = cfg.changelog["path"]
    if changelog and (not plan.prerelease or cfg.changelog["prereleases"]):
        target = cfg.root / changelog
        existing = target.read_bytes().decode("utf-8") if target.is_file() else None
        body = render_changelog_body(plan, cfg, session.links)
        updated = update_changelog(existing, str(plan.version), changelog_heading(plan, cfg, session.links), body, cfg)
        target.write_bytes(updated.encode("utf-8"))
        paths.append(changelog)
    if paths:
        git("add", "--", *paths)
    plain = Links("")
    message = f"{cfg.release_subject(tag)}\n\n{render_justification(plan, cfg, plain)}\n"
    path = _write_temp(message)
    try:
        git(*session.identity(), "commit", "--allow-empty", "--cleanup=verbatim", "-F", path)
    finally:
        os.unlink(path)
    return git("rev-parse", "HEAD")


def find_prepared(session: Session, base_tag: str | None, head: str = "HEAD") -> tuple[str, Version, str] | None:
    """Commit de release ainda sem tag: ``(tag, versão, commit a marcar)``.

    Cobre a retomada depois de uma falha entre o push e a tag, e o modo por PR, em que o commit
    chega à branch por merge, squash ou rebase. Um commit de release revertido não conta.
    """
    cfg, repo = session.cfg, session.repo
    marker = cfg.release_subject_pattern()
    commits = repo.log(f"{base_tag}..{head}" if base_tag else head)
    reverted = {sha for commit in commits for sha in REVERTS.findall(commit.body)}
    existing = repo.version_tags()
    base_version = existing.get(base_tag or "")
    for commit in reversed(commits):
        match = marker.match(commit.subject)
        if not match or any(commit.sha.startswith(sha) for sha in reverted):
            continue
        tag = match.group("tag")
        version = Version.try_parse(tag[len(cfg.tag_prefix) :]) if tag.startswith(cfg.tag_prefix) else None
        if version is None or tag in existing:
            continue
        if base_version is not None and version.key() <= base_version.key():
            continue
        for candidate in repo.log(f"{base_tag}..{head}" if base_tag else head, first_parent=True):
            if candidate.sha == commit.sha or session.git.ok("merge-base", "--is-ancestor", commit.sha, candidate.sha):
                return tag, version, candidate.sha
    return None


def _ensure_milestone(github: GitHub, title: str) -> int | None:
    for milestone in github.milestones():
        if milestone.get("title") == title:
            return int(milestone["number"])
    created = github.create_milestone(title)
    return int(created["number"]) if created and "number" in created else None


def mark_included(session: Session, plan: Plan, tag: str) -> None:
    """Marca PRs e issues incluídos na versão: comentário único e milestone com o nome da tag."""
    cfg, github = session.cfg, session.github
    if not github.enabled or not cfg.release["mark"]:
        return
    mark = RELEASED_MARK.format(tag=tag)
    url = f"{github.web}/releases/tag/{tag}"
    targets: dict[int, str] = {}
    for change in plan.changes:
        if change.number is not None:
            targets[change.number] = f"{mark}\n🚀 Este PR foi publicado na versão [{tag}]({url}).\n"
        for issue in change.issues:
            if issue.closing:
                targets.setdefault(issue.number, f"{mark}\n🚀 Entregue na versão [{tag}]({url}).\n")
    milestone = _ensure_milestone(github, tag) if "milestone" in cfg.release["mark"] else None
    trusted = cfg.versioning["trusted_bots"]
    for number, body in targets.items():
        try:
            if "comment" in cfg.release["mark"]:
                comments = github.comments(number)
                if not any(mark in str(c.get("body", "")) and is_trusted_author(c, trusted) for c in comments):
                    github.add_comment(number, body)
            if milestone is not None and not (github.issue(number) or {}).get("milestone"):
                github.set_milestone(number, milestone)
        except CommandError as exc:
            print(f"aviso: não consegui marcar #{number}: {exc}")
    if milestone is not None:
        github.update_milestone(milestone, {"state": "closed"})


def publish(session: Session, tag: str, version: Version, target: str, plan: Plan | None = None) -> None:
    """Tag anotada, GitHub Release e marcações. Cada passo confere antes se já foi feito."""
    cfg, git, github = session.cfg, session.git, session.github
    extra = ""
    if plan is None:  # retomada: refaz o plano do intervalo que esta tag cobre
        plan = build_plan(cfg, session.repo, github, head=target, fixed_version=version, exclude_tag=tag)
        release_commit = session.repo.log(f"{target}~1..{target}" if git.ok("rev-parse", f"{target}~1") else target)
        bodies = [c.body for c in release_commit if cfg.release_subject_pattern().match(c.subject) and c.body]
        if bodies:
            extra = "### Registro do commit de release\n\n" + bodies[-1]
    notes = release_notes(plan, cfg, session.links, extra)
    if tag not in session.repo.version_tags():
        path = _write_temp(f"{tag}\n\n{notes}")
        try:
            git(*session.identity(), "tag", "-a", tag, target, "--cleanup=verbatim", "-F", path)
        finally:
            os.unlink(path)
        print(f"Tag {tag} criada em {target[:7]}.")
    if session.has_remote:
        git("push", REMOTE, f"refs/tags/{tag}")
    if github.enabled:
        if github.release_by_tag(tag) is None:
            finals = [v for v in session.repo.version_tags().values() if not v.is_prerelease]
            top = highest(finals)
            latest = not version.is_prerelease and (top is None or version.key() >= top.key())
            github.create_release(
                {
                    "tag_name": tag,
                    "name": tag,
                    "body": notes,
                    "prerelease": version.is_prerelease,
                    "make_latest": "true" if latest else "false",
                }
            )
            print(f"GitHub Release {tag} criada.")
        if not version.is_prerelease:
            mark_included(session, plan, tag)
        workflow = cfg.release["dispatch_workflow"]
        if workflow:
            try:
                github.dispatch_workflow(workflow, tag, {"version": str(version), "tag": tag})
                print(f"Workflow {workflow} disparado para {tag}.")
            except CommandError as exc:
                print(f"aviso: não consegui disparar {workflow}: {exc}")
    _output(released="true", version=str(version), tag=tag)


def open_release_pr(session: Session, plan: Plan) -> None:
    """Modo por PR: publica a branch de release e abre ou atualiza o PR."""
    cfg, git, github = session.cfg, session.git, session.github
    tag, branch = str(plan.tag(cfg)), cfg.release["pr_branch"]
    git("push", "--force", REMOTE, f"HEAD:refs/heads/{branch}")
    title = cfg.release_subject(tag)
    body = (
        f"{RELEASE_PR_MARK}\nPR de release gerado pela automação. Ao ser mesclado, a tag `{tag}` e a GitHub Release "
        "são criadas. Não edite: ele é refeito a cada push na branch de destino.\n\n"
        + release_notes(plan, cfg, session.links)
    )
    existing = github.open_pulls_from(branch)
    if existing:
        github.update_pull(int(existing[0]["number"]), {"title": title, "body": body})
        print(f"PR de release #{existing[0]['number']} atualizado para {tag}.")
    else:
        created = github.create_pull({"title": title, "head": branch, "base": plan.branch, "body": body})
        print(f"PR de release #{(created or {}).get('number', '?')} aberto para {tag}.")
    git("reset", "--hard", "HEAD~1")
    _output(released="false", version=str(plan.version), tag=tag)


def reconcile_latest(session: Session) -> None:
    """Uma tag de release sem GitHub Release (falha no meio) é concluída aqui."""
    cfg, github = session.cfg, session.github
    latest = session.repo.latest("HEAD", final_only=False)
    if latest is None or not github.enabled:
        return
    tag, version = latest
    target = session.git("rev-list", "-n", "1", tag)
    subject = session.git("log", "-1", "--format=%s", target)
    inner = session.repo.log(f"{target}~1..{target}") if session.git.ok("rev-parse", f"{target}~1") else []
    is_release = any(cfg.release_subject_pattern().match(s) for s in [subject, *(c.subject for c in inner)])
    if is_release and github.release_by_tag(tag) is None:
        print(f"A tag {tag} não tem GitHub Release; concluindo a publicação.")
        publish(session, tag, version, target)


def run(session: Session, args: argparse.Namespace) -> int:
    cfg, git = session.cfg, session.git
    if not cfg.versioning_enabled:
        print("Versionamento desligado em dev-workflow.toml (`versioning.enabled = false`).")
        return 0
    branch = session.branch()
    if not cfg.is_release_branch(branch):
        print(f"A branch `{branch}` não publica versões (veja `repository.release_branches`).")
        return 0
    override = Override(args.level, args.reason or "(motivo não informado)", "execução manual") if args.level else None
    analyses = []
    if args.analysis_file:
        analyses.append(validate_analysis(json.loads(Path(args.analysis_file).read_text(encoding="utf-8"))))
    if session.has_remote:
        git("fetch", "--tags", REMOTE, check=False)
    for _ in range(MAX_ROUNDS):
        reconcile_latest(session)
        base = session.repo.latest("HEAD", final_only=False)
        prepared = find_prepared(session, base[0] if base else None)
        if prepared and not args.dry_run:
            tag, version, target = prepared
            print(f"Commit de release de {tag} sem tag; publicando.")
            publish(session, tag, version, target)
            continue
        plan = build_plan(
            cfg,
            session.repo,
            session.github,
            branch=branch,
            forced=args.force,
            override=override,
            prerelease=args.prerelease or None,
            extra_analyses=analyses,
        )
        if not plan.requested:
            pending = len(plan.changes)
            print(
                f"Nada a versionar: {pending} mudança(s) acumulada(s) desde {plan.base_tag or 'o início'}, "
                f"nenhuma em PR marcado com `{cfg.label('release')}`."
            )
            _output(released="false")
            return 0
        print(report(plan, cfg, session.links))
        if plan.problems or plan.version is None:
            for problem in plan.problems:
                print(f"::error title=Release::{problem}" if os.environ.get("GITHUB_ACTIONS") else f"ERRO: {problem}")
            return 1
        if args.dry_run:
            return 0
        drift = check_version_files(cfg, plan.files_version)
        if drift:
            for problem in drift:
                print(f"ERRO: {problem}")
            return 1
        sha = prepare(session, plan)
        if cfg.release["mode"] == "pull-request":
            open_release_pr(session, plan)
            return 0
        if session.has_remote:
            pushed = git.raw("push", REMOTE, f"HEAD:refs/heads/{branch}")
            if pushed.returncode != 0:  # a branch andou: refaz o plano sobre o novo estado
                print("A branch remota avançou durante a release; recalculando.")
                git("fetch", "--tags", REMOTE, branch)
                git("reset", "--hard", f"{REMOTE}/{branch}")
                continue
        publish(session, str(plan.tag(cfg)), plan.version, sha, plan)
        return 0
    print("ERRO: a release não estabilizou; rode de novo e confira o estado da branch.")
    return 1


# --- relatórios --------------------------------------------------------------------------------


def report(plan: Plan, cfg: Config, links: Links, *, pr: int | None = None) -> str:
    lines: list[str] = []
    tag = plan.tag(cfg)
    release = cfg.label("release")
    if pr is not None:
        current = next((change for change in plan.changes if change.number == pr), None)
        others = len(plan.changes) - 1
        labels = current.labels if current else set()
        if release in labels:
            lines.append(f"### Prévia de versão: ao mesclar, este PR publica **{tag}** ({plan.level})")
            lines.append(
                f"Entram nesta versão este PR e {others} mudança(s) já acumulada(s) desde {plan.base_tag or 'o início'}."
            )
        else:
            lines.append("### Prévia de versão: este PR não publica versão")
            lines.append(
                f"Sem o label `{release}`, ele fica acumulado com {others} mudança(s) pendente(s) e entra na próxima "
                f"versão. Com `{release}`, o merge publicaria **{tag}** ({plan.level})."
            )
    elif plan.version is None:
        lines.append(f"Nada acumulado desde {plan.base_tag or 'o início'}.")
    else:
        state = "será publicada" if plan.requested else "seria publicada (nenhum PR pediu versão ainda)"
        lines.append(f"### Próxima versão: **{tag}** ({plan.level}) — {state}")
        if plan.requested_by:
            lines.append(f"Versão pedida por: {', '.join(plan.requested_by)}.")
    lines += ["", render_justification(plan, cfg, links)]
    if plan.problems:
        lines += ["", "**Problemas que impedem a release**", *(f"- {problem}" for problem in plan.problems)]
    return "\n".join(lines).strip() + "\n"


def preview(session: Session, args: argparse.Namespace) -> int:
    cfg = session.cfg
    if session.repo.latest(args.base_ref, final_only=False) is None:
        # Antes da tag de base não há versão de referência: não é erro do PR, e o histórico inteiro
        # não é o que o PR publicaria.
        print("Sem tag de versão alcançável: a prévia começa depois de `release.py baseline`.")
        return 0
    plan = build_plan(cfg, session.repo, session.github, head=args.base_ref, pending=(args.pr, args.head_ref))
    text = report(plan, cfg, session.links, pr=args.pr)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write(text)
    if args.comment and session.github.enabled:
        body = f"{PREVIEW_MARK}\n{text}\n<sub>Atualizado a cada push e a cada mudança de label.</sub>\n"
        try:
            trusted = cfg.versioning["trusted_bots"]
            mine = [
                c
                for c in session.github.comments(args.pr)
                if PREVIEW_MARK in str(c.get("body", "")) and is_trusted_author(c, trusted)
            ]
            if mine:
                session.github.update_comment(int(mine[-1]["id"]), body)
            else:
                session.github.add_comment(args.pr, body)
        except CommandError as exc:  # PR de fork: token só de leitura
            print(f"aviso: não consegui comentar a prévia no PR: {exc}")
    return 0


def analysis_comment(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, indent=1).replace("--", "-\\u002d")
    lines = [
        f"<!-- {ANALYSIS_MARK}",
        payload,
        "-->",
        "### Análise de versão",
        f"Intervalo `{data['base'] or 'início'}..{data['head'][:7] or 'HEAD'}` · nível proposto: **{data['level']}**",
        "",
        "| Mudança | Nível | Motivo |",
        "|---|---|---|",
    ]
    for entry in data["changes"]:
        key = f"#{entry['pr']}" if "pr" in entry else entry["commit"][:7]
        evidence = "<br>".join(f"`{item}`" for item in entry["evidence"][:6])
        summary = entry["summary"].replace("|", "/") + (f"<br>{evidence}" if evidence else "")
        lines.append(f"| {key} | {entry['level']}{' ⚠' if entry['breaking'] else ''} | {summary} |")
    if data["notes"]:
        lines += ["", data["notes"]]
    lines += ["", "<sub>A análise só eleva o nível calculado pelos sinais determinísticos; nunca o reduz.</sub>"]
    return "\n".join(lines) + "\n"


def record_analysis(session: Session, args: argparse.Namespace) -> int:
    try:
        data = validate_analysis(json.loads(Path(args.file).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"Análise inválida: {exc}", file=sys.stderr)
        return 1
    if args.dry_run or not session.github.enabled:
        print(analysis_comment(data))
        return 0
    session.github.add_comment(args.pr, analysis_comment(data))
    print(f"Análise gravada no PR #{args.pr}: nível {data['level']}, {len(data['changes'])} mudança(s).")
    return 0


def baseline(session: Session, args: argparse.Namespace) -> int:
    cfg, git = session.cfg, session.git
    if session.repo.version_tags():
        print("O repositório já tem tags de versão; a tag de base não é necessária.")
        return 0
    version = Version.parse(str(cfg.versioning["initial_version"]))
    drift = check_version_files(cfg, version)
    if drift:
        for problem in drift:
            print(f"ERRO: {problem}")
        return 1
    tag = f"{cfg.tag_prefix}{version}"
    message = f"{tag}\n\nLinha de base do versionamento automático: o que veio antes desta tag é a versão {version}.\n"
    git(*session.identity(), "tag", "-a", tag, args.ref, "-m", message)
    print(f"Tag de base {tag} criada em {git('rev-parse', '--short', args.ref)}.")
    if args.push and session.has_remote:
        git("push", REMOTE, f"refs/tags/{tag}")
        print("Tag enviada ao remoto.")
    return 0


# --- linha de comando --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release.py", description="Versionamento semântico do projeto.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "plan"):
        command = sub.add_parser(name)
        command.add_argument("--format", choices=("md", "json"), default="md")
        command.add_argument("--ref", default="HEAD")
        command.add_argument("--pr", type=int, help="inclui um PR aberto como se estivesse mesclado")
        command.add_argument("--base-ref", help="com --pr: a branch de destino (padrão: origin/<branch padrão>)")
        command.add_argument("--head-ref", default="HEAD", help="com --pr: a ponta do PR")
    command = sub.add_parser("next")
    command.add_argument("--level", choices=LEVELS[1:])
    command = sub.add_parser("check")
    command.add_argument(
        "--tree", default="", help="confere os arquivos de outra árvore (checkout do PR), com a configuração desta"
    )
    command = sub.add_parser("bump")
    command.add_argument("--to", required=True)
    command.add_argument("--dry-run", action="store_true")
    command = sub.add_parser("preview")
    command.add_argument("--pr", type=int, required=True)
    command.add_argument("--base-ref", required=True)
    command.add_argument("--head-ref", default="HEAD")
    command.add_argument("--comment", action="store_true")
    command = sub.add_parser("record-analysis")
    command.add_argument("--pr", type=int, required=True)
    command.add_argument("--file", required=True)
    command.add_argument("--dry-run", action="store_true")
    command = sub.add_parser("baseline")
    command.add_argument("--ref", default="HEAD")
    command.add_argument("--push", action="store_true")
    command = sub.add_parser("run")
    command.add_argument("--force", action="store_true", help="publica o acumulado mesmo sem PR marcado")
    command.add_argument("--level", choices=("", *LEVELS[1:]), default="")
    command.add_argument("--reason", default="")
    command.add_argument("--prerelease", default="", help="canal de pré-release, por exemplo rc")
    command.add_argument("--analysis-file", default="")
    command.add_argument("--dry-run", action="store_true")
    return parser


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def main(argv: Sequence[str] | None = None) -> int:
    utf8_streams()
    args = _parser().parse_args(argv)
    try:
        cfg = load()
        session = Session(cfg)
        if args.command in ("status", "plan"):
            pending = None
            head = args.ref
            if args.pr:
                head = args.base_ref or f"{REMOTE}/{cfg.default_branch}"
                pending = (args.pr, args.head_ref)
            plan = build_plan(cfg, session.repo, session.github, head=head, branch=session.branch(), pending=pending)
            if args.format == "json":
                print(json.dumps(plan_json(plan, cfg), ensure_ascii=False, indent=2))
            else:
                print(report(plan, cfg, session.links, pr=args.pr))
            return 0
        if args.command == "next":
            if args.level:
                latest = session.repo.latest("HEAD", final_only=True)
                base = latest[1] if latest else Version.parse(str(cfg.versioning["initial_version"]))
                print(next_version(base, args.level, cfg.versioning["zero_major"]))
            else:
                plan = build_plan(cfg, session.repo, session.github)
                print(plan.version or plan.base_version)
            return 0
        if args.command == "check":
            latest = session.repo.latest("HEAD", final_only=False)
            current = latest[1] if latest else Version.parse(str(cfg.versioning["initial_version"]))
            tree = Path(args.tree).resolve() if args.tree else None
            if tree is not None and not tree.is_dir():
                print(f"ERRO: --tree {args.tree} não é um diretório.")
                return 1
            problems = check_version_files(cfg, current, root=tree)
            for problem in problems:
                print(f"ERRO: {problem}")
            if not problems:
                origin = f"tag {latest[0]}" if latest else "versioning.initial_version (ainda não há tag)"
                print(
                    f"{len(cfg.versioning['files'])} entrada(s) de arquivo coerentes com a versão {current} ({origin})."
                )
            return 1 if problems else 0
        if args.command == "bump":
            latest = session.repo.latest("HEAD", final_only=False)
            current = latest[1] if latest else Version.parse(str(cfg.versioning["initial_version"]))
            changed = apply_version_files(cfg, current, Version.parse(args.to), write=not args.dry_run)
            print("\n".join(changed) or "Nenhum arquivo mudou.")
            return 0
        if args.command == "preview":
            return preview(session, args)
        if args.command == "record-analysis":
            return record_analysis(session, args)
        if args.command == "baseline":
            return baseline(session, args)
        args.force = args.force or _truthy("RELEASE_FORCE")
        args.dry_run = args.dry_run or _truthy("RELEASE_DRY_RUN")
        args.level = args.level or os.environ.get("RELEASE_LEVEL", "")
        args.reason = args.reason or os.environ.get("RELEASE_REASON", "")
        args.prerelease = args.prerelease or os.environ.get("RELEASE_PRERELEASE", "")
        if args.level not in ("", *LEVELS[1:]):
            raise ReleaseError(f"Nível inválido: {args.level}.")
        return run(session, args)
    except (ConfigError, ReleaseError, CommandError, ValueError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
