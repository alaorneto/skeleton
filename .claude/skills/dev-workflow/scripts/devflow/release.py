"""Versionamento semântico automatizado (``release.py <subcomando>``).

- ``status`` / ``plan [--format md|json]``: o que se acumulou desde a última tag e a próxima versão.
- ``next [--level L]``: só o próximo número.
- ``check``: os arquivos de versão declarados estão coerentes com a versão corrente?
- ``bump --to X [--dry-run]``: escreve uma versão nos arquivos declarados (uso manual).
- ``preview --pr N --base-ref R --head-ref H [--comment | --check]``: prévia de versão de um PR
  aberto; ``--check`` falha quando o pedido de versão do PR não pode ser publicado.
- ``record-analysis --pr N --file F``: grava no PR a análise de versão feita por um agente.
- ``baseline [--ref R] [--push]``: cria a tag de base quando o projeto ainda não tem tag.
- ``run [--force] [--level L --reason "..."] [--prerelease canal] [--dry-run]``: publica o que
  estiver pendente (``--prerelease`` já implica ``--force``). É a máquina de estados que o workflow de
  release executa; rodar de novo depois de concluir não faz nada, e rodar depois de uma falha conclui
  o que faltou.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from . import i18n
from .config import Config, ConfigError, load
from .gh import CommandError, Git, GitHub, current_repository, is_trusted_author, utf8_streams
from .plan import (
    ANALYSIS_MARK,
    Collector,
    Commit,
    Links,
    Override,
    Plan,
    Problem,
    Repo,
    build_plan,
    plan_json,
    render_changelog_body,
    render_justification,
    validate_analysis,
)
from .semver import LEVELS, Version, highest, next_version
from .text import MERGE_PR, REVERTS

PREVIEW_MARK = "<!-- dev-workflow:preview -->"
RELEASED_MARK = "<!-- dev-workflow:released {tag} -->"
RELEASE_PR_MARK = "<!-- dev-workflow:release-pr -->"
# Título de seção de versão no changelog; `version` é o número inteiro, com o sufixo de pré-release.
VERSION_HEADING = re.compile(r"^## \[?v?(?P<version>\d+\.\d+\.\d+[^\]\s(]*)", re.MULTILINE)
REMOTE = "origin"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
MAX_ROUNDS = 4
# Limites do GitHub, em caracteres: corpo de PR, issue e comentário; corpo de uma Release.
BODY_LIMIT = 65_536
RELEASE_BODY_LIMIT = 125_000
# Folga para o que o GitHub conta diferente de ``len`` (caracteres fora do BMP contam dois).
NOTES_MARGIN = 2_048


class ReleaseError(RuntimeError):
    """Erro que interrompe a release com mensagem para quem opera."""


def _error(cfg: Config, text: object) -> str:
    """``ERRO: texto`` no idioma do projeto (fora do GitHub Actions)."""
    return cfg.t("release.error", text=text)


def _warning(cfg: Config, text: object) -> str:
    """``aviso: texto`` no idioma do projeto (fora do GitHub Actions)."""
    return cfg.t("release.warning", text=text)


# O que o git escreve quando o servidor recusa o push por regra, não porque a branch andou.
PUSH_REFUSED = re.compile(
    r"protected branch|GH006|GH013|permission|denied|\b403\b|not allowed|rule violations", re.IGNORECASE
)


def push_refused(stderr: str) -> bool:
    return bool(PUSH_REFUSED.search(stderr or ""))


def refused_push_message(branch: str, stderr: str) -> str:
    detail = " ".join((stderr or "").split())[:500]
    return i18n.t("release.push.refused", branch=branch, detail=detail)


def read_analysis_file(path: str) -> Any:
    """O JSON de uma análise de versão, com erro legível para arquivo ausente, ilegível ou inválido."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ReleaseError(i18n.t("release.analysis.missing", path=path)) from None
    except UnicodeDecodeError:
        raise ReleaseError(i18n.t("release.analysis.encoding", path=path)) from None
    except OSError as exc:
        raise ReleaseError(i18n.t("release.analysis.unreadable", path=path, error=exc.strerror or exc)) from None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ReleaseError(i18n.t("release.analysis.invalid", path=path, error=exc)) from None


# --- arquivos de versão ------------------------------------------------------------------------


def channel_of(version: Version) -> str:
    """O canal de uma pré-release (``beta`` em ``1.3.0-beta.1``); vazio numa versão final."""
    return version.pre[0] if version.is_prerelease else ""


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
        # Vazios numa versão final: para arquivos que guardam o sufixo de pré-release à parte.
        values[f"{name}_prerelease"] = ".".join(version.pre)
        values[f"{name}_channel"] = channel_of(version)
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
                i18n.t("release.files.count", path=entry["path"], expected=expected, needle=needle, found=found)
            )
        return text.replace(needle, replacement)
    pattern = re.compile(expand(entry["regex"], values, escape=True), re.MULTILINE)
    found = len(pattern.findall(text))
    if found != expected:
        regex = entry["regex"]
        raise ReleaseError(
            i18n.t("release.files.count_regex", path=entry["path"], expected=expected, regex=regex, found=found)
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
            except UnicodeDecodeError:
                raise ReleaseError(cfg.t("release.files.encoding", path=entry["path"])) from None
            except OSError as exc:
                raise ReleaseError(cfg.t("release.files.unreadable", path=entry["path"], error=exc)) from exc
            contents[entry["path"]] = (original, original)
        original, text = contents[entry["path"]]
        contents[entry["path"]] = (original, rewrite(text, entry, current, new))
    changed = [name for name, (original, text) in contents.items() if original != text]
    if write:
        for name in changed:
            (base / name).write_bytes(contents[name][1].encode("utf-8"))
    return changed


def prepared_release(session: Session, tree: Path, current: Version) -> tuple[str, Version] | None:
    """``(tag, versão)`` quando ``tree`` é o PR de release: a ponta é ``chore(release): <tag>`` de uma
    versão acima da corrente e ainda sem tag. Os arquivos dele já trazem essa versão, não a corrente."""
    cfg, tree_git = session.cfg, Git(tree)
    if not tree_git.ok("rev-parse", "HEAD"):
        return None
    match = cfg.release_subject_pattern().match(tree_git("log", "-1", "--format=%s", "HEAD"))
    tag = match.group("tag") if match else ""
    version = Version.try_parse(tag[len(cfg.tag_prefix) :]) if tag.startswith(cfg.tag_prefix) else None
    if version is None or version.key() <= current.key() or tag in session.repo.version_tags():
        return None
    return tag, version


def check_version_files(cfg: Config, current: Version, *, root: Path | None = None) -> list[str]:
    """Divergências entre os arquivos e a versão corrente: reescrever para ela não pode mudar nada."""
    try:
        drifted = apply_version_files(cfg, current, current, write=False, root=root)
    except ReleaseError as exc:
        return [str(exc)]
    return [cfg.t("release.files.drift", path=name, version=current) for name in drifted]


def tree_latest(cfg: Config, tree: Path) -> tuple[str, Version] | None:
    """A versão corrente da linha de ``tree`` (``Repo.current``), quando ela é um checkout com histórico.

    O PR é conferido contra a versão de que partiu, não contra a da base: depois de uma release,
    um PR mais antigo ainda traz a versão anterior nos arquivos sem ter tocado neles. Só o git da
    árvore é consultado; nada dela é executado. Sem histórico (checkout raso, pasta comum) não há
    resposta, e quem chama usa a versão da base.
    """
    tree_git = Git(tree)
    if not tree_git.ok("rev-parse", "--verify", "--quiet", "HEAD^{commit}"):
        return None
    return Repo(cfg, tree_git).current("HEAD")


# --- changelog ---------------------------------------------------------------------------------


def writes_changelog(plan: Plan, cfg: Config) -> bool:
    """Esta versão escreve o arquivo de changelog? Uma pré-release só com `changelog.prereleases`."""
    return bool(cfg.changelog["path"]) and (not plan.prerelease or bool(cfg.changelog["prereleases"]))


def changelog_heading(plan: Plan, cfg: Config, links: Links) -> str:
    """Título da seção. O link compara com a versão anterior: a última final, ou, numa pré-release,
    a tag publicada antes dela."""
    version, tag = str(plan.version), plan.tag(cfg)
    date = datetime.datetime.now(datetime.UTC).date().isoformat()
    since = (plan.previous_tag if plan.prerelease else None) or plan.base_tag
    if links.web and since:
        return f"## [{version}]({links.web}/compare/{since}...{tag}) — {date}"
    if links.web:
        return f"## [{version}]({links.web}/releases/tag/{tag}) — {date}"
    return f"## {version} — {date}"


def cycle_versions(plan: Plan, cfg: Config) -> list[Version]:
    """As pré-releases do ciclo em andamento, que a versão do plano substitui no changelog."""
    prefix = cfg.tag_prefix
    versions = (Version.try_parse(tag[len(prefix) :]) for tag in plan.cycle if tag.startswith(prefix))
    return [version for version in versions if version is not None]


def drop_sections(text: str, versions: Iterable[Version]) -> str:
    """Tira de ``text`` as seções das versões dadas.

    A versão é lida do título de cada seção e comparada inteira, nunca por prefixo de texto:
    ``1.3.0-beta.1`` não se confunde com ``1.3.0``, ``1.3.0-beta.10`` nem ``1.30.0-beta.1``.
    """
    gone = set(versions)
    matches = list(VERSION_HEADING.finditer(text)) if gone else []
    kept, removed = [text[: matches[0].start()]] if matches else [text], False
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if Version.try_parse(match.group("version")) in gone:
            removed = True
        else:
            kept.append(text[match.start() : end])
    return "".join(kept).rstrip("\n") + "\n" if removed else text


def update_changelog(
    existing: str | None, version: str, heading: str, body: str, cfg: Config, *, superseded: Iterable[Version] = ()
) -> str:
    """Insere a seção da versão no topo; se ela já existe, substitui (reexecução idempotente).

    ``superseded`` são as pré-releases do ciclo que esta versão cobre: as seções delas saem, porque
    a seção nova traz a lista inteira desde a última versão final.
    """
    block = f"{heading}\n\n{body.strip()}\n"
    if existing:
        existing = drop_sections(existing, (old for old in superseded if str(old) != version))
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


def truncate_lines(text: str, limit: int, note: str) -> str:
    """Corta ``text`` num fim de linha para caber em ``limit`` com ``note`` no fim.

    ``note`` pode citar ``{omitted}``: quantos itens de lista (``- ``) ficaram de fora.
    """
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    budget = max(limit - len(note.format(omitted=len(lines))) - 2, 0)
    kept, size = 0, 0
    while kept < len(lines) and size + len(lines[kept]) + 1 <= budget:
        size += len(lines[kept]) + 1
        kept += 1
    omitted = sum(1 for line in lines[kept:] if line.lstrip().startswith("- "))
    head = "\n".join(lines[:kept]).rstrip()
    return (head + "\n\n" if head else "") + note.format(omitted=omitted)


def fit_notes(
    changelog: str,
    justification: str,
    compact: str,
    extra: str,
    limit: int | None,
    *,
    full_list: str,
    language: str | None = None,
) -> str:
    """Notas que cabem em ``limit`` caracteres (o GitHub recusa corpo maior; ``None`` não limita).

    Primeiro a tabela por mudança sai (``compact`` diz onde ela continua inteira); se ainda não
    couber, o changelog é cortado em linha inteira, com a indicação de ``full_list``. ``language``
    é o idioma dos títulos e avisos; sem ele, vale o idioma ativo.
    """
    language = language or i18n.active()
    why = i18n.translate(language, "release.notes.why") + "\n\n"
    full = "\n\n".join(part for part in (changelog, why + justification, extra) if part) + "\n"
    if limit is None or len(full) <= limit:
        return full
    tail = "\n\n" + why + compact + "\n"
    # `{omitted}` fica no texto: `truncate_lines` o preenche com o que de fato ficou de fora.
    note = i18n.translate(language, "release.notes.more", full_list=full_list)
    body = truncate_lines(changelog, max(limit - len(tail), 0), note)
    return truncate_lines(body + tail, limit, i18n.translate(language, "release.notes.cut"))


def notes_changelog(plan: Plan, cfg: Config, links: Links) -> str:
    """A lista de mudanças das notas.

    Numa pré-release que continua um ciclo, quem testa quer saber primeiro o que mudou desde a
    pré-release anterior: as novidades abrem as notas e o acumulado desde a última final vem depois.
    """
    full = render_changelog_body(plan, cfg, links, hide=False)
    fresh = [change for change in plan.changes if change.fresh]
    if not (plan.prerelease and plan.cycle and plan.previous_tag) or len(fresh) == len(plan.changes):
        return full
    if fresh:
        news = render_changelog_body(plan, cfg, links, hide=False, changes=fresh)
    else:
        news = cfg.t("notes.news.none", previous=plan.previous_tag, channel=plan.prerelease)
    base = plan.base_tag or cfg.t("release.beginning")
    return "\n\n".join(
        (
            "## " + cfg.t("notes.news.title", previous=plan.previous_tag),
            news,
            "## " + cfg.t("notes.accumulated.title", base=base),
            full,
        )
    )


def release_notes(
    plan: Plan, cfg: Config, links: Links, extra: str = "", *, limit: int | None = None, ref: str = ""
) -> str:
    """Notas da versão: changelog e o porquê do número, dentro de ``limit`` quando dado.

    ``ref`` é a tag ou branch em que o ``CHANGELOG.md`` completo pode ser lido.
    """
    changelog = notes_changelog(plan, cfg, links)
    path = cfg.changelog["path"] if writes_changelog(plan, cfg) else ""
    if path and links.web and ref:
        full_list = cfg.t("release.notes.full.link", path=path, url=f"{links.web}/blob/{ref}/{path}")
    elif path:
        full_list = cfg.t("release.notes.full.file", path=path)
    else:  # sem arquivo, ou pré-release que não o escreve: a lista inteira está na anotação da tag
        full_list = cfg.t("release.notes.full.tag")
    compact = render_justification(plan, cfg, links, table_note=cfg.t("release.notes.table", count=len(plan.changes)))
    justification = render_justification(plan, cfg, links)
    return fit_notes(changelog, justification, compact, extra, limit, full_list=full_list, language=cfg.language)


# --- contexto de execução ----------------------------------------------------------------------


class Session:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.git = Git(cfg.root)
        self.repo = Repo(cfg, self.git)
        self.github = GitHub(current_repository(self.git))
        self.links = Links(self.github.web)
        self.published: list[tuple[str, Version]] = []  # o que esta execução publicou

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


def version_outputs(version: Version, tag: str) -> dict[str, str]:
    """O que os jobs seguintes recebem sobre a versão: número, tag, se é pré-release e de qual canal."""
    return {
        "version": str(version),
        "tag": tag,
        "prerelease": "true" if version.is_prerelease else "false",
        "channel": channel_of(version),
    }


# --- preparar e publicar -----------------------------------------------------------------------


def prepare(session: Session, plan: Plan) -> str:
    """Escreve os arquivos e o changelog e cria o commit de release. Devolve o SHA."""
    cfg, git = session.cfg, session.git
    assert plan.version is not None
    tag = str(plan.tag(cfg))
    paths = apply_version_files(cfg, plan.files_version, plan.version)
    changelog = cfg.changelog["path"]
    if writes_changelog(plan, cfg):
        target = cfg.root / changelog
        existing = target.read_bytes().decode("utf-8") if target.is_file() else None
        body = render_changelog_body(plan, cfg, session.links)
        heading = changelog_heading(plan, cfg, session.links)
        # Uma seção por versão publicada: a lista inteira entra aqui, e as pré-releases do ciclo saem.
        updated = update_changelog(
            existing, str(plan.version), heading, body, cfg, superseded=cycle_versions(plan, cfg)
        )
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


def release_commit_refusal(session: Session, collector: Collector, commit: Commit, revision: str) -> str | None:
    """Por que um commit com assunto de release não é da automação; ``None`` quando é.

    Só dois caminhos trazem um commit de release legítimo: o push direto (o commit está no primeiro
    pai e nenhum PR o trouxe) e o merge do PR de release deste repositório. O assunto sozinho não
    prova nada, porque qualquer PR pode conter um commit com esse texto.
    """
    cfg, git, github = session.cfg, session.git, session.github
    mainline = session.repo.log(revision, first_parent=True)
    carrier = next(
        (c for c in mainline if c.sha == commit.sha or git.ok("merge-base", "--is-ancestor", commit.sha, c.sha)),
        None,
    )
    if carrier is None:
        return None  # fora do primeiro pai deste intervalo: `find_prepared` já não o publica
    branch = cfg.release["pr_branch"]
    if not github.enabled:  # sem a API vale o que o git mostra: o assunto do merge
        merge = MERGE_PR.match(carrier.subject)
        owner = (github.repository or "").split("/", 1)[0].lower()
        ours = merge is not None and merge.group("branch") == branch
        if carrier.sha == commit.sha or (
            ours and merge is not None and (not owner or str(merge.group("owner")).lower() == owner)
        ):
            return None
        return cfg.t("release.forged.merge", sha=carrier.sha[:7], branch=branch)
    number = collector.pr_number(carrier)
    if number is None:
        if carrier.sha == commit.sha:
            return None
        return cfg.t("release.forged.not_pr", sha=carrier.sha[:7])
    if cfg.is_release_pull(collector.pull(number)):
        return None
    return cfg.t("release.forged.other_pr", number=number, branch=branch)


def find_prepared(session: Session, base_tag: str | None, head: str = "HEAD") -> tuple[str, Version, str] | None:
    """Commit de release ainda sem tag: ``(tag, versão, commit a marcar)``.

    Cobre a retomada depois de uma falha entre o push e a tag, e o modo por PR, em que o commit
    chega à branch por merge, squash ou rebase. Um commit de release revertido não conta, nem um
    que não veio da automação (veja ``release_commit_refusal``).
    """
    cfg, repo = session.cfg, session.repo
    marker = cfg.release_subject_pattern()
    collector = Collector(cfg, repo, session.github)
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
        refusal = release_commit_refusal(session, collector, commit, f"{base_tag}..{head}" if base_tag else head)
        if refusal:
            text = cfg.t("release.forged.ignored", sha=commit.sha[:7], tag=tag, reason=refusal)
            print(f"::warning title=Release::{text}" if os.environ.get("GITHUB_ACTIONS") else _warning(cfg, text))
            continue
        for candidate in repo.log(f"{base_tag}..{head}" if base_tag else head, first_parent=True):
            if candidate.sha == commit.sha or session.git.ok("merge-base", "--is-ancestor", commit.sha, candidate.sha):
                # A tag vai no commit de release, não no merge que o trouxe: o que entrou na branch
                # depois de ele ser preparado fica depois da tag e sai na versão seguinte.
                return tag, version, commit.sha
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
            targets[change.number] = f"{mark}\n{cfg.t('release.marked.pr', tag=tag, url=url)}\n"
        for issue in change.issues:
            if issue.closing:
                targets.setdefault(issue.number, f"{mark}\n{cfg.t('release.marked.issue', tag=tag, url=url)}\n")
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
            print(_warning(cfg, cfg.t("release.mark.failed", number=number, error=exc)))
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
            extra = cfg.t("release.notes.commit_record") + "\n\n" + bodies[-1]
    notes = release_notes(plan, cfg, session.links, extra)  # tag anotada: sem limite, com tudo
    if tag not in session.repo.version_tags():
        path = _write_temp(f"{tag}\n\n{notes}")
        try:
            git(*session.identity(), "tag", "-a", tag, target, "--cleanup=verbatim", "-F", path)
        finally:
            os.unlink(path)
        print(cfg.t("release.tag.created", tag=tag, sha=target[:7]))
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
                    "body": release_notes(
                        plan, cfg, session.links, extra, limit=RELEASE_BODY_LIMIT - NOTES_MARGIN, ref=tag
                    ),
                    "prerelease": version.is_prerelease,
                    "make_latest": "true" if latest else "false",
                }
            )
            print(cfg.t("release.github.created", tag=tag))
        if not version.is_prerelease:
            mark_included(session, plan, tag)
        workflow = cfg.release["dispatch_workflow"]
        if workflow:
            try:
                github.dispatch_workflow(workflow, tag, {"version": str(version), "tag": tag})
                print(cfg.t("release.dispatch.done", workflow=workflow, tag=tag))
            except CommandError as exc:
                print(_warning(cfg, cfg.t("release.dispatch.failed", workflow=workflow, error=exc)))
    session.published.append((tag, version))
    _output(released="true", **version_outputs(version, tag))


def open_release_pr(session: Session, plan: Plan) -> None:
    """Modo por PR: publica a branch de release e abre ou atualiza o PR."""
    cfg, git, github = session.cfg, session.git, session.github
    tag, branch = str(plan.tag(cfg)), cfg.release["pr_branch"]
    git("push", "--force", REMOTE, f"HEAD:refs/heads/{branch}")
    title = cfg.release_subject(tag)
    header = f"{RELEASE_PR_MARK}\n{cfg.t('release.pr.header', tag=tag)}\n\n"
    limit = BODY_LIMIT - NOTES_MARGIN - len(header)
    body = header + release_notes(plan, cfg, session.links, limit=limit, ref=branch)
    existing = github.open_pulls_from(branch) if github.enabled else []
    if not github.enabled:  # sem a API não há PR: dizer o que de fato aconteceu e como concluir
        print(cfg.t("release.pr.offline", branch=branch, tag=tag, base=plan.branch, title=title))
    elif existing:
        github.update_pull(int(existing[0]["number"]), {"title": title, "body": body})
        print(cfg.t("release.pr.updated", number=existing[0]["number"], tag=tag))
    else:
        created = github.create_pull({"title": title, "head": branch, "base": plan.branch, "body": body})
        print(cfg.t("release.pr.opened", number=(created or {}).get("number", "?"), tag=tag))
    git("reset", "--hard", "HEAD~1")
    assert plan.version is not None
    if not session.published:  # uma versão publicada nesta execução é a que os jobs seguintes recebem
        _output(released="false", **version_outputs(plan.version, tag))


def reconcile_latest(session: Session) -> None:
    """Uma tag de release sem GitHub Release (falha no meio) é concluída aqui."""
    cfg, github = session.cfg, session.github
    latest = session.repo.current("HEAD")
    if latest is None or not github.enabled:
        return
    tag, version = latest
    target = session.git("rev-list", "-n", "1", tag)
    subject = session.git("log", "-1", "--format=%s", target)
    inner = session.repo.log(f"{target}~1..{target}") if session.git.ok("rev-parse", f"{target}~1") else []
    is_release = any(cfg.release_subject_pattern().match(s) for s in [subject, *(c.subject for c in inner)])
    if is_release and github.release_by_tag(tag) is None:
        print(cfg.t("release.reconcile", tag=tag))
        publish(session, tag, version, target)


def run(session: Session, args: argparse.Namespace) -> int:
    cfg, git = session.cfg, session.git
    if not cfg.versioning_enabled:
        print(cfg.t("release.disabled"))
        return 0
    branch = session.branch()
    if not cfg.is_release_branch(branch):
        print(cfg.t("release.branch.not_release", branch=branch))
        return 0
    if branch == cfg.release["pr_branch"]:
        # O nome casa com `release/*`, e um push feito com RELEASE_TOKEN dispara o workflow nela.
        print(cfg.t("release.branch.release_pr", branch=branch))
        _output(released="false")
        return 0
    args.prerelease = (args.prerelease or "").strip().lower()
    channels = cfg.prerelease_channels
    if args.prerelease and args.prerelease not in channels:
        if not channels:
            raise ReleaseError(cfg.t("release.channel.none"))
        raise ReleaseError(cfg.t("release.channel.unknown", channel=args.prerelease, accepted=", ".join(channels)))
    # Pedir um canal é pedir a publicação: sem isto o disparo manual não fazia nada, em silêncio.
    args.force = args.force or bool(args.prerelease)
    override = None
    if args.level:
        reason = args.reason or cfg.t("release.override.no_reason")
        override = Override(args.level, reason, cfg.t("release.override.manual"))
    analyses = []
    if args.analysis_file:
        analyses.append(validate_analysis(read_analysis_file(args.analysis_file)))
    if session.has_remote:
        git("fetch", "--tags", REMOTE, check=False)
    for _ in range(MAX_ROUNDS):
        reconcile_latest(session)
        base = session.repo.current("HEAD")
        prepared = find_prepared(session, base[0] if base else None)
        if prepared and not args.dry_run:
            tag, version, target = prepared
            print(cfg.t("release.prepared.publishing", tag=tag))
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
        if session.published and args.force and not any(change.fresh for change in plan.changes):
            # Retomada de uma execução manual que tinha parado no meio (commit de release sem tag,
            # tag sem GitHub Release): a versão que ela pedia acabou de sair, e nada entrou depois.
            tag, version = session.published[-1]
            if channel_of(version) == args.prerelease:
                print(cfg.t("release.manual.answered", tag=tag))
                return 0
        if not plan.requested:
            print(nothing_to_release(plan, cfg))
            if not session.published:  # sem apagar o `released=true` do que esta execução publicou
                _output(released="false")
            return 0
        print(report(plan, cfg, session.links))
        if plan.problems or plan.version is None:
            for problem in plan.problems:
                print(f"::error title=Release::{problem}" if os.environ.get("GITHUB_ACTIONS") else _error(cfg, problem))
            return 1
        if args.dry_run:
            return 0
        drift = check_version_files(cfg, plan.files_version)
        if drift:
            for problem in drift:
                print(_error(cfg, problem))
            return 1
        sha = prepare(session, plan)
        if cfg.release["mode"] == "pull-request":
            open_release_pr(session, plan)
            return 0
        if session.has_remote:
            pushed = git.raw("push", REMOTE, f"HEAD:refs/heads/{branch}")
            if pushed.returncode != 0 and push_refused(pushed.stderr):
                raise ReleaseError(refused_push_message(branch, pushed.stderr))
            if pushed.returncode != 0:  # a branch andou: refaz o plano sobre o novo estado
                print(cfg.t("release.push.moved"))
                git("fetch", "--tags", REMOTE, branch)
                git("reset", "--hard", f"{REMOTE}/{branch}")
                continue
        publish(session, str(plan.tag(cfg)), plan.version, sha, plan)
        return 0
    print(_error(cfg, cfg.t("release.unstable")))
    return 1


# --- relatórios --------------------------------------------------------------------------------


def version_label_list(cfg: Config) -> str:
    """Os labels que pedem versão, como texto: o de release e um por canal."""
    names = [cfg.label("release"), *(cfg.channel_label(channel) for channel in cfg.prerelease_channels)]
    return ", ".join(f"`{name}`" for name in names)


def nothing_to_release(plan: Plan, cfg: Config) -> str:
    """Por que esta execução não publica: nenhum PR novo pediu versão."""
    base = plan.base_tag or cfg.t("release.beginning")
    if not plan.cycle:
        return cfg.t("release.nothing", count=len(plan.changes), base=base, labels=version_label_list(cfg))
    return cfg.t(
        "release.nothing.cycle",
        count=len(plan.changes),
        base=base,
        fresh=sum(1 for change in plan.changes if change.fresh),
        previous=plan.previous_tag,
        labels=version_label_list(cfg),
    )


def report(plan: Plan, cfg: Config, links: Links, *, pr: int | None = None, table_note: str | None = None) -> str:
    """O plano em texto. Com ``pr``, é a prévia de um PR aberto, e o texto segue o que o plano
    resolveu (final, pré-release de um canal ou nada), não só os labels do próprio PR."""
    lines: list[str] = []
    tag = plan.tag(cfg)
    release = cfg.label("release")
    base = plan.base_tag or cfg.t("release.beginning")
    if pr is not None:
        current = next((change for change in plan.changes if change.number == pr), None)
        others = len(plan.changes) - 1
        asks = bool(current and cfg.version_labels(current.labels))
        waiting = [change for change in plan.changes if change.key in plan.requested_by and change.number != pr]
        if asks and plan.prerelease:
            lines.append(cfg.t("preview.prerelease.title", tag=tag, level=plan.level))
            lines.append(
                cfg.t("preview.prerelease.body", others=others, base=base, channel=plan.prerelease, release=release)
            )
            if plan.cycle:
                fresh = sum(1 for change in plan.changes if change.fresh) - 1
                lines.append(cfg.t("preview.prerelease.news", previous=plan.previous_tag, fresh=fresh))
        elif asks:
            lines.append(cfg.t("release.preview.final.title", tag=tag, level=plan.level))
            lines.append(cfg.t("release.preview.final.body", others=others, base=base))
            if plan.cycle:
                cycle = ", ".join(plan.cycle)
                lines.append(cfg.t("preview.final.closes_cycle", cycle=cycle, base=base))
        elif waiting:
            asked = ", ".join(f"{change.key} (`{cfg.version_labels(change.labels)[0]}`)" for change in waiting)
            lines.append(cfg.t("preview.pending.title", tag=tag, level=plan.level))
            lines.append(cfg.t("preview.pending.body", asked=asked, tag=tag))
        else:
            lines.append(cfg.t("release.preview.none.title"))
            lines.append(cfg.t("release.preview.none.body", release=release, others=others, tag=tag, level=plan.level))
            channels = ", ".join(f"`{cfg.channel_label(channel)}`" for channel in cfg.prerelease_channels)
            if channels:
                lines.append(cfg.t("preview.none.channels", labels=channels))
    elif plan.version is None:
        lines.append(cfg.t("release.report.nothing", base=base))
    else:
        state = cfg.t("release.report.next.will") if plan.requested else cfg.t("release.report.next.would")
        lines.append(cfg.t("release.report.next.title", tag=tag, level=plan.level, state=state))
        if plan.requested_by:
            lines.append(cfg.t("release.report.requested_by", changes=", ".join(plan.requested_by)))
    if plan.cycle:
        lines.append(cfg.t("report.cycle", cycle=", ".join(plan.cycle), current=plan.previous_tag))
    lines += ["", render_justification(plan, cfg, links, table_note=table_note)]
    if plan.problems:
        lines += ["", cfg.t("release.report.problems"), *(f"- {problem}" for problem in plan.problems)]
    return "\n".join(lines).strip() + "\n"


def _problem_key(problem: str) -> str:
    """A chave de um problema do plano; o próprio texto quando ele não traz uma."""
    return problem.key if isinstance(problem, Problem) else problem


def check_request(session: Session, args: argparse.Namespace, plan: Plan, collector: Collector) -> int:
    """``preview --check``: reprova o PR quando o pedido de versão **dele** não pode ser publicado.

    Sem label de versão o PR não pede nada, e não há o que reprovar. Com label, o plano é refeito
    sem o PR, pedindo o mesmo tipo de versão: um problema que já existe sem ele (um piso que o
    acumulado não alcança, por exemplo) não é dele, e só os que aparecem com ele contam. Os
    problemas são comparados pela chave (``Problem.key``), porque o texto cita a versão alvo, que
    muda com o PR.

    A análise de versão que falta por causa do PR também não reprova: ela se resolve com um
    comentário no PR, que não dispara o check de novo, e o PR ficaria vermelho depois de
    resolvido. O aviso aparece, e a release para até a análise ser registrada.
    """
    cfg = session.cfg
    current = next((change for change in plan.changes if change.number == args.pr), None)
    labels = cfg.version_labels(current.labels) if current else []
    if not labels:
        print(cfg.t("check.request.none", pr=args.pr))
        return 0
    before = build_plan(
        cfg,
        session.repo,
        session.github,
        head=args.base_ref,
        forced=True,
        prerelease=plan.prerelease,
        collector=collector,
    )
    known = {_problem_key(problem) for problem in before.problems}
    inherited = [problem for problem in plan.problems if _problem_key(problem) in known]
    brought = [problem for problem in plan.problems if _problem_key(problem) not in known]
    waiting = [problem for problem in brought if _problem_key(problem).startswith("analysis:")]
    own = [problem for problem in brought if problem not in waiting]
    actions = bool(os.environ.get("GITHUB_ACTIONS"))
    for problem in own:
        print(f"::error title=Release::{problem}" if actions else _error(cfg, problem))
    for problem in [*inherited, *waiting]:
        print(f"::warning title=Release::{problem}" if actions else _warning(cfg, problem))
    if own:
        print(cfg.t("check.request.refused", pr=args.pr, label=labels[0], count=len(own)))
        return 1
    if waiting:
        print(cfg.t("check.request.analysis", pr=args.pr, label=labels[0]))
    if inherited:
        print(cfg.t("check.request.inherited", pr=args.pr, label=labels[0], count=len(inherited)))
    if not (waiting or inherited):
        print(cfg.t("check.request.ok", pr=args.pr, label=labels[0], tag=plan.tag(cfg)))
    return 0


def preview(session: Session, args: argparse.Namespace) -> int:
    cfg = session.cfg
    if session.repo.current(args.base_ref) is None:
        # Antes da tag de base não há versão de referência: não é erro do PR, e o histórico inteiro
        # não é o que o PR publicaria.
        print(cfg.t("release.preview.no_tag"))
        return 0
    collector = Collector(cfg, session.repo, session.github)
    pending = (args.pr, args.head_ref)
    plan = build_plan(cfg, session.repo, session.github, head=args.base_ref, pending=pending, collector=collector)
    if args.check:
        return check_request(session, args, plan, collector)
    text = report(plan, cfg, session.links, pr=args.pr)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write(text)
    if args.comment and session.github.enabled:
        footer = "\n" + cfg.t("release.preview.footer") + "\n"
        if len(PREVIEW_MARK) + len(text) + len(footer) + 1 > BODY_LIMIT - NOTES_MARGIN:
            note = cfg.t("release.preview.table_note", count=len(plan.changes))
            compact = report(plan, cfg, session.links, pr=args.pr, table_note=note)
            text = truncate_lines(compact, BODY_LIMIT - NOTES_MARGIN - len(PREVIEW_MARK) - len(footer) - 1, note)
        body = f"{PREVIEW_MARK}\n{text}{footer}"
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
            print(_warning(cfg, cfg.t("release.preview.comment_failed", error=exc)))
    return 0


def analysis_comment(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, indent=1).replace("--", "-\\u002d")
    lines = [
        f"<!-- {ANALYSIS_MARK}",
        payload,
        "-->",
        i18n.t("release.analysis.comment.title"),
        i18n.t(
            "release.analysis.comment.range",
            base=data["base"] or i18n.t("release.analysis.comment.start"),
            head=data["head"][:7] or "HEAD",
            level=data["level"],
        ),
        "",
        i18n.t("release.analysis.comment.columns"),
        "|---|---|---|",
    ]
    for entry in data["changes"]:
        key = f"#{entry['pr']}" if "pr" in entry else entry["commit"][:7]
        shown = (" ".join(item.replace("`", "'").split()) for item in entry["evidence"][:6])
        evidence = "<br>".join(f"`{item}`" for item in shown)
        summary = " ".join(entry["summary"].replace("|", "/").split()) + (f"<br>{evidence}" if evidence else "")
        lines.append(f"| {key} | {entry['level']}{' ⚠' if entry['breaking'] else ''} | {summary} |")
    if data["notes"]:
        lines += ["", data["notes"]]
    lines += ["", i18n.t("release.analysis.comment.footer")]
    return "\n".join(lines) + "\n"


def record_analysis(session: Session, args: argparse.Namespace) -> int:
    cfg = session.cfg
    try:
        data = validate_analysis(read_analysis_file(args.file))
    except (OSError, ValueError, ReleaseError) as exc:
        print(cfg.t("release.analysis.refused", error=exc), file=sys.stderr)
        return 1
    if not data["head"] and session.github.enabled:
        # Sem `head`, a análise não diria o que viu e não contaria: vale a ponta atual do PR.
        head = str(((session.github.pull(args.pr) or {}).get("head") or {}).get("sha") or "")
        if head:
            data["head"] = head
            print(cfg.t("release.analysis.head_filled", sha=head[:7]))
    if not data["head"]:
        print(cfg.t("release.analysis.no_head"), file=sys.stderr)
    if args.dry_run or not session.github.enabled:
        print(analysis_comment(data))
        return 0
    session.github.add_comment(args.pr, analysis_comment(data))
    print(cfg.t("release.analysis.recorded", pr=args.pr, level=data["level"], count=len(data["changes"])))
    return 0


def baseline(session: Session, args: argparse.Namespace) -> int:
    cfg, git = session.cfg, session.git
    if session.repo.version_tags():
        print(cfg.t("release.baseline.exists"))
        return 0
    version = Version.parse(str(cfg.versioning["initial_version"]))
    drift = check_version_files(cfg, version)
    if drift:
        for problem in drift:
            print(_error(cfg, problem))
        return 1
    tag = f"{cfg.tag_prefix}{version}"
    message = f"{tag}\n\n{cfg.t('release.baseline.message', version=version)}\n"
    git(*session.identity(), "tag", "-a", tag, args.ref, "-m", message)
    print(cfg.t("release.baseline.created", tag=tag, sha=git("rev-parse", "--short", args.ref)))
    if args.push and session.has_remote:
        git("push", REMOTE, f"refs/tags/{tag}")
        print(cfg.t("release.baseline.pushed"))
    return 0


# --- linha de comando --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release.py", description=i18n.t("release.cli.description"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "plan"):
        command = sub.add_parser(name)
        command.add_argument("--format", choices=("md", "json"), default="md")
        command.add_argument("--ref", default="HEAD")
        command.add_argument("--pr", type=int, help=i18n.t("release.cli.pr"))
        command.add_argument("--base-ref", help=i18n.t("release.cli.base_ref"))
        command.add_argument("--head-ref", default="HEAD", help=i18n.t("release.cli.head_ref"))
    command = sub.add_parser("next")
    command.add_argument("--level", choices=LEVELS[1:])
    command = sub.add_parser("check")
    command.add_argument("--tree", default="", help=i18n.t("release.cli.tree"))
    command = sub.add_parser("bump")
    command.add_argument("--to", required=True)
    command.add_argument("--dry-run", action="store_true")
    command = sub.add_parser("preview")
    command.add_argument("--pr", type=int, required=True)
    command.add_argument("--base-ref", required=True)
    command.add_argument("--head-ref", default="HEAD")
    command.add_argument("--comment", action="store_true")
    command.add_argument("--check", action="store_true", help=i18n.t("release.cli.check"))
    command = sub.add_parser("record-analysis")
    command.add_argument("--pr", type=int, required=True)
    command.add_argument("--file", required=True)
    command.add_argument("--dry-run", action="store_true")
    command = sub.add_parser("baseline")
    command.add_argument("--ref", default="HEAD")
    command.add_argument("--push", action="store_true")
    command = sub.add_parser("run")
    command.add_argument("--force", action="store_true", help=i18n.t("release.cli.force"))
    command.add_argument("--level", choices=("", *LEVELS[1:]), default="")
    command.add_argument("--reason", default="")
    command.add_argument("--prerelease", default="", help=i18n.t("release.cli.prerelease"))
    command.add_argument("--analysis-file", default="")
    command.add_argument("--dry-run", action="store_true")
    return parser


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def main(argv: Sequence[str] | None = None) -> int:
    utf8_streams()
    # A configuração vem antes dos argumentos para a ajuda sair no idioma do projeto; um erro nela
    # só é dito depois, como antes, para `--help` e argumentos inválidos continuarem respondendo.
    try:
        loaded: Config | ConfigError | ValueError = load()
    except (ConfigError, ValueError) as exc:
        loaded = exc
    args = _parser().parse_args(argv)
    try:
        if not isinstance(loaded, Config):
            raise loaded
        cfg = loaded
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
                base = session.repo.line("HEAD").final or Version.parse(str(cfg.versioning["initial_version"]))
                print(next_version(base, args.level, cfg.versioning["zero_major"]))
            else:
                plan = build_plan(cfg, session.repo, session.github)
                print(plan.version or plan.base_version)
            return 0
        if args.command == "check":
            # Repositório sem commit ainda (recém-adotado): vale a versão inicial da configuração.
            latest = session.repo.current("HEAD") if session.git.ok("rev-parse", "HEAD") else None
            current = latest[1] if latest else Version.parse(str(cfg.versioning["initial_version"]))
            tree = Path(args.tree).resolve() if args.tree else None
            if tree is not None and not tree.is_dir():
                print(_error(cfg, cfg.t("release.check.no_tree", tree=args.tree)))
                return 1
            where = ""
            own = tree_latest(cfg, tree) if tree is not None else None
            if own:  # a versão de que o PR partiu; a base pode já ter publicado outra
                latest, current, where = own, own[1], cfg.t("release.check.pr_tip")
            prepared = prepared_release(session, tree, current) if tree is not None else None
            problems = check_version_files(cfg, prepared[1] if prepared else current, root=tree)
            for problem in problems:
                print(_error(cfg, problem))
            count = len(cfg.versioning["files"])
            if not problems and prepared:
                print(cfg.t("release.check.prepared", tag=prepared[0], count=count, version=prepared[1]))
            elif not problems:
                origin = f"tag {latest[0]}{where}" if latest else cfg.t("release.check.initial")
                print(cfg.t("release.check.ok", count=count, version=current, origin=origin))
            return 1 if problems else 0
        if args.command == "bump":
            latest = session.repo.current("HEAD")
            current = latest[1] if latest else Version.parse(str(cfg.versioning["initial_version"]))
            changed = apply_version_files(cfg, current, Version.parse(args.to), write=not args.dry_run)
            print("\n".join(changed) or cfg.t("release.bump.none"))
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
            raise ReleaseError(cfg.t("release.level.invalid", level=args.level))
        return run(session, args)
    except (ConfigError, ReleaseError, CommandError, ValueError) as exc:
        print(i18n.t("release.error", text=exc), file=sys.stderr)
        return 1
