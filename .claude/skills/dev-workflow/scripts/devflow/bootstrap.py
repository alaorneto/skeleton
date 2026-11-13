"""Instalação e conferência do fluxo num projeto (``bootstrap.py <subcomando>``).

- ``init [--default-branch B] [--initial-version V] [--force] [--no-labels] [--dry-run]``: gera a
  configuração, os workflows, os modelos de issue e de PR e instala o hook ``commit-msg``. Se o
  skill estiver fora do repositório, copia-o para ``.claude/skills/dev-workflow`` (o CI precisa
  dos scripts versionados). Arquivos que já existem só são trocados com ``--force``.
- ``update [--force] [--dry-run]``: rodado a partir da versão nova do skill (fora do projeto),
  troca a cópia em ``.claude/skills/dev-workflow`` e regenera os workflows e modelos que não foram
  personalizados. A configuração do projeto nunca é tocada.
- ``install-hook``: instala o hook ``commit-msg`` em ``.git/hooks`` (projetos sem pre-commit).
- ``doctor``: confere a instalação e lista o que falta, inclusive os passos de quem é dono.
- ``version``: a versão do skill.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import CONFIG_CANDIDATES, Config, ConfigError, find_root, load
from .gh import Git, GitHub, current_repository, offline, run_gh, utf8_streams
from .plan import Repo
from .release import check_version_files
from .semver import Version
from .text import sections

SKILL = Path(__file__).resolve().parents[2]
ASSETS = SKILL / "assets"
VENDOR_PATH = ".claude/skills/dev-workflow"
# O que é do repositório do skill, não do skill: não vai para a cópia dentro de um projeto.
COPY_IGNORE = ("__pycache__", "*.pyc", "*-workspace", ".git", ".github", ".pr-head")
WORKFLOWS = ("pr-check.yml", "pr-merged.yml", "release.yml", "epic-checklist.yml", "semver-analysis.yml")
OPTIONAL_WORKFLOWS = ("dev-workflow-selftest.yml",)

# (arquivo, expressão que captura a versão, marcador usado no padrão de busca)
DETECTORS: tuple[tuple[str, str, str], ...] = (
    ("pyproject.toml", r'^version\s*=\s*"(?P<v>[^"]+)"', "{current_pep440}"),
    ("package.json", r'^\s*"version"\s*:\s*"(?P<v>[^"]+)"', "{current}"),
    ("Cargo.toml", r'^version\s*=\s*"(?P<v>[^"]+)"', "{current}"),
    ("composer.json", r'^\s*"version"\s*:\s*"(?P<v>[^"]+)"', "{current}"),
    ("pubspec.yaml", r"^version:\s*(?P<v>\S+)", "{current}"),
    ("mix.exs", r'version:\s*"(?P<v>[^"]+)"', "{current}"),
    ("build.gradle", r"^version\s*=?\s*'(?P<v>[^']+)'", "{current}"),
    ("build.gradle.kts", r'^version\s*=\s*"(?P<v>[^"]+)"', "{current}"),
    ("Directory.Build.props", r"<Version>(?P<v>[^<]+)</Version>", "{current}"),
    ("VERSION", r"^(?P<v>\d+\.\d+\.\d+[^\s]*)\s*$", "{current}"),
    ("version.txt", r"^(?P<v>\d+\.\d+\.\d+[^\s]*)\s*$", "{current}"),
)


def skill_version(skill: Path = SKILL) -> str:
    """Versão do skill (arquivo ``VERSION``); ``"?"`` numa cópia antiga, sem o arquivo."""
    try:
        return (skill / "VERSION").read_text(encoding="utf-8").strip() or "?"
    except OSError:
        return "?"


def is_skill(path: Path) -> bool:
    """O diretório tem este skill (e não outro com o mesmo caminho)?"""
    skill_md = path / "SKILL.md"
    if not skill_md.is_file() or not (path / "scripts" / "bootstrap.py").is_file():
        return False
    head = skill_md.read_text(encoding="utf-8", errors="replace")[:400]
    return re.search(r"^name:\s*dev-workflow\s*$", head, re.MULTILINE) is not None


def _skill_files(base: Path) -> set[str]:
    """Arquivos do skill em ``base``, relativos a ela, sem o que é só do repositório do skill."""
    ignore = shutil.ignore_patterns(*COPY_IGNORE)
    found: set[str] = set()
    for directory, dirs, names in os.walk(base):
        skipped = ignore(directory, [*dirs, *names])
        dirs[:] = [name for name in dirs if name not in skipped]
        here = Path(directory)
        found |= {(here / name).relative_to(base).as_posix() for name in names if name not in skipped}
    return found


def copy_skill(source: Path, destination: Path) -> tuple[list[str], list[str]]:
    """Espelha ``source`` em ``destination``: copia tudo e remove o que saiu. Devolve (copiados, removidos)."""
    wanted = _skill_files(source)
    removed = sorted(_skill_files(destination) - wanted) if destination.exists() else []
    for name in removed:
        (destination / name).unlink()
    for name in sorted(wanted):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    for directory, _dirs, _names in sorted(os.walk(destination), key=lambda item: -len(Path(item[0]).parts)):
        if Path(directory) != destination and not any(Path(directory).iterdir()):
            Path(directory).rmdir()
    return sorted(wanted), removed


def _normalize(version: str) -> Version | None:
    """Aceita SemVer e a forma PEP 440 das pré-releases (``1.2.0rc1``)."""
    direct = Version.try_parse(version)
    if direct:
        return direct
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(a|b|rc)(\d+)", version)
    names = {"a": "alpha", "b": "beta", "rc": "rc"}
    return Version.try_parse(f"{match[1]}-{names[match[2]]}.{match[3]}") if match else None


def detect_version_files(root: Path) -> tuple[list[dict[str, str]], Version | None]:
    """Arquivos de versão reconhecidos na raiz e a versão que eles declaram."""
    entries: list[dict[str, str]] = []
    found: Version | None = None
    for name, pattern, token in DETECTORS:
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(pattern, text, re.MULTILINE)
        version = _normalize(match.group("v")) if match else None
        if not match or version is None:
            continue
        literal = match.group(0).strip()
        entry = {"path": name, "search": literal.replace(match.group("v"), token)}
        occurrences = text.count(literal)
        if occurrences != 1:
            entry["note"] = f"ATENÇÃO: `{literal}` aparece {occurrences} vezes; ajuste `search` ou use `count`."
        entries.append(entry)
        found = found or version
    return entries, found


def _toml_string(value: str) -> str:
    return f"'{value}'" if "'" not in value else json.dumps(value, ensure_ascii=False)


def render_version_files(entries: list[dict[str, str]]) -> str:
    if not entries:
        return (
            "# Nenhum arquivo de versão reconhecido: a versão vive só nas tags. Para escrevê-la em arquivos:\n"
            '# [[versioning.files]]\n# path = "caminho/do/arquivo"\n# search = \'version = "{current}"\'\n'
        )
    blocks = []
    for entry in entries:
        note = f"# {entry['note']}\n" if entry.get("note") else ""
        blocks.append(
            f'{note}[[versioning.files]]\npath = "{entry["path"]}"\nsearch = {_toml_string(entry["search"])}\n'
        )
    return "\n".join(blocks)


def render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(f"__{key}__", value)
    return template


def default_branch(git: Git) -> str:
    head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if head.startswith("origin/"):
        return head[len("origin/") :]
    return git("symbolic-ref", "--short", "HEAD", check=False) or "main"


def skill_dir_in(root: Path) -> str | None:
    """Caminho do skill relativo à raiz do repositório, ou ``None`` se estiver fora dela."""
    try:
        return SKILL.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def template_values(root: Path, skill_dir: str, branch: str, cfg: Config | None, git: Git) -> dict[str, str]:
    repository = current_repository(git) or "OWNER/REPO"
    return {
        "SKILL_DIR": skill_dir,
        "SKILL_GLOB": "**" if skill_dir == "." else f"{skill_dir}/**",
        "SKILL_PATH": "" if skill_dir == "." else f"{skill_dir}/",
        "DEFAULT_BRANCH": branch,
        "RELEASE_LABEL": cfg.label("release") if cfg else "release",
        "REPOSITORY_URL": f"https://github.com/{repository}",
    }


def planned_files(values: dict[str, str], *, selftest: bool, assets: Path = ASSETS) -> dict[str, str]:
    """``{caminho no projeto: conteúdo}`` dos modelos, sem a configuração."""
    files: dict[str, str] = {}
    for name in (*WORKFLOWS, *(OPTIONAL_WORKFLOWS if selftest else ())):
        template = assets / "workflows" / name
        if template.is_file():
            files[f".github/workflows/{name}"] = render(template.read_text(encoding="utf-8"), values)
    for template in sorted((assets / "ISSUE_TEMPLATE").glob("*.yml")):
        files[f".github/ISSUE_TEMPLATE/{template.name}"] = render(template.read_text(encoding="utf-8"), values)
    pull_request = assets / "pull_request_template.md"
    if pull_request.is_file():
        files[".github/pull_request_template.md"] = pull_request.read_text(encoding="utf-8")
    return files


def hook_path(git: Git) -> Path:
    path = Path(git("rev-parse", "--git-path", "hooks/commit-msg"))
    return path if path.is_absolute() else (git.root or Path.cwd()) / path


def install_hook(root: Path, skill_dir: str, *, force: bool = False) -> str:
    git = Git(root)
    target = hook_path(git)
    content = render((ASSETS / "hooks" / "commit-msg").read_text(encoding="utf-8"), {"SKILL_DIR": skill_dir})
    if target.exists() and not force:
        existing = target.read_text(encoding="utf-8", errors="replace")
        if "github_issues.py" in existing or "pre-commit" in existing:
            return f"hook commit-msg já instalado em {target}"
        return (
            f"ATENÇÃO: {target} já existe e é de outra origem; use --force para trocar ou chame o script a partir dele"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8"))
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return f"hook commit-msg instalado em {target}"


def init(args: argparse.Namespace) -> int:
    root = find_root()
    git = Git(root)
    if not (root / ".git").exists():
        print("ERRO: rode dentro de um repositório git (`git init` primeiro).", file=sys.stderr)
        return 1
    skill_dir = skill_dir_in(root)
    actions: list[str] = []
    if skill_dir is None:
        destination = root / VENDOR_PATH
        if destination.exists() and not args.force:
            print(f"ERRO: {VENDOR_PATH} já existe; rode o bootstrap de lá ou use --force.", file=sys.stderr)
            return 1
        if not args.dry_run:
            copy_skill(SKILL, destination)
        actions.append(
            f"skill dev-workflow {skill_version()} copiado para {VENDOR_PATH} "
            "(os workflows do CI chamam os scripts de lá)"
        )
        skill_dir = VENDOR_PATH
    branch = args.default_branch or default_branch(git)
    entries, detected = detect_version_files(root)
    initial = args.initial_version or (str(detected) if detected else "0.1.0")
    if Version.try_parse(initial) is None:
        print(f"ERRO: '{initial}' não é uma versão SemVer.", file=sys.stderr)
        return 1
    values = template_values(root, skill_dir, branch, None, git)
    files = planned_files(values, selftest=args.selftest)
    existing_config = next((name for name in CONFIG_CANDIDATES if (root / name).is_file()), None)
    if existing_config is None or args.force:
        config_values = {**values, "INITIAL_VERSION": initial, "VERSION_FILES": render_version_files(entries)}
        files = {
            CONFIG_CANDIDATES[0]: render(
                (ASSETS / "dev-workflow.toml")
                .read_text(encoding="utf-8")
                .replace("# __VERSION_FILES__", "__VERSION_FILES__"),
                config_values,
            ),
            **files,
        }
    else:
        actions.append(f"{existing_config} mantido (já existe)")
    for name, content in files.items():
        target = root / name
        if target.exists() and not args.force:
            same = target.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n") == content
            actions.append(
                f"{name} {'já está igual ao modelo' if same else 'mantido (já existe e difere do modelo; --force troca)'}"
            )
            continue
        if not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        actions.append(f"{name} criado")
    if (root / ".pre-commit-config.yaml").is_file():
        actions.append(
            f"projeto usa pre-commit: acrescente o trecho de {skill_dir}/assets/pre-commit-hook.yaml e rode `pre-commit install`"
        )
    elif not args.dry_run:
        actions.append(install_hook(root, skill_dir, force=args.force))
    if not args.no_labels and not args.dry_run and not offline():
        from .issues import sync_labels  # noqa: PLC0415

        try:
            ok = sync_labels(load(root)) == 0
        except (ConfigError, OSError) as exc:
            ok = False
            actions.append(f"labels: {exc}")
        actions.append(
            "labels criados no GitHub"
            if ok
            else "labels NÃO criados: rode `github_issues.py sync-labels` com o gh autenticado"
        )
    prefix = "[dry-run] " if args.dry_run else ""
    for action in actions:
        print(f"{prefix}{action}")
    print(
        "\nPróximos passos:\n"
        f"  1. Revise .github/dev-workflow.toml (arquivos de versão, superfícies públicas) e rode\n"
        f"     python3 {skill_dir}/scripts/bootstrap.py doctor\n"
        "  2. Commite os arquivos gerados e mescle na branch padrão: workflows só valem a partir dela.\n"
        f"  3. Crie a tag de base: python3 {skill_dir}/scripts/release.py baseline --push\n"
        f"  4. Passos de quem é dono (segredos, permissões, proteção de branch): {skill_dir}/references/installation.md"
    )
    return 0


# --- update ------------------------------------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return None


def _refuse(message: str) -> int:
    print(f"ERRO: {message}", file=sys.stderr)
    return 1


def update(args: argparse.Namespace) -> int:
    """Troca a cópia do skill no projeto pela versão que está rodando este comando."""
    root = find_root()
    destination = root / VENDOR_PATH
    if skill_dir_in(root) is not None:
        return _refuse(
            "este é o skill que já está no projeto. Rode o update a partir da versão nova (por exemplo o\n"
            "clone do repositório do skill): python3 <skill-novo>/scripts/bootstrap.py update"
        )
    if not is_skill(destination):
        return _refuse(f"{VENDOR_PATH} não tem o skill dev-workflow; para instalar, use `bootstrap.py init`.")
    old, new = skill_version(destination), skill_version()
    old_version, new_version = Version.try_parse(old), Version.try_parse(new)
    if new_version is None:
        return _refuse(f"a versão do skill de origem ({new}) não é SemVer; confira o arquivo VERSION.")
    if old_version is not None and new_version.key() < old_version.key() and not args.force:
        return _refuse(f"a cópia instalada ({old}) é mais nova que a de origem ({new}); use --force para voltar.")
    try:
        cfg = load(root)
    except ConfigError as exc:
        return _refuse(str(exc))

    # Arquivo igual ao modelo antigo não foi personalizado: recebe o modelo novo. Arquivo diferente
    # dos dois é do projeto e só é trocado com --force. Opcional ausente continua ausente.
    values = template_values(root, VENDOR_PATH, cfg.default_branch, cfg, Git(root))
    before = planned_files(values, selftest=True, assets=destination / "assets")
    after = planned_files(values, selftest=True)
    optional = {f".github/workflows/{name}" for name in (*OPTIONAL_WORKFLOWS, "semver-analysis.yml")}
    regenerate: dict[str, str] = {}
    customized: list[str] = []
    for name, content in after.items():
        current = _read(root / name)
        if current == content or (current is None and name in optional):
            continue
        if current is None or current == before.get(name) or args.force:
            regenerate[name] = content
        else:
            customized.append(name)

    prefix = "[dry-run] " if args.dry_run else ""
    if args.dry_run:
        print(f"{prefix}skill dev-workflow {old} → {new} em {VENDOR_PATH}")
    else:
        copied, removed = copy_skill(SKILL, destination)
        print(
            f"skill dev-workflow {old} → {new} em {VENDOR_PATH}: {len(copied)} arquivo(s), {len(removed)} removido(s)"
        )
        for name in removed:
            print(f"  removido {VENDOR_PATH}/{name}")
    for name, content in regenerate.items():
        if not args.dry_run:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        print(f"{prefix}{name} atualizado pelo modelo novo")
    for name in customized:
        print(
            f"{prefix}ATENÇÃO: {name} foi personalizado e difere do modelo novo; compare e ajuste à mão "
            "(ou rode de novo com --force para trocar)"
        )
    print(f"{prefix}{CONFIG_CANDIDATES[0]} mantido (o update nunca troca a configuração)")
    print(
        "\nPróximos passos:\n"
        f"  1. python3 {VENDOR_PATH}/scripts/bootstrap.py doctor\n"
        "  2. Revise o diff e commite numa branch da issue da atualização; workflows novos valem depois\n"
        "     do merge na branch padrão."
    )
    return 0


# --- doctor ------------------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.errors = 0
        self.pending = 0

    def ok(self, message: str) -> None:
        print(f"  ok        {message}")

    def todo(self, message: str) -> None:
        self.pending += 1
        print(f"  PENDENTE  {message}")

    def error(self, message: str) -> None:
        self.errors += 1
        print(f"  ERRO      {message}")


def doctor(_args: argparse.Namespace) -> int:
    root = find_root()
    git = Git(root)
    report = Report()
    print(f"dev-workflow em {root}")
    try:
        cfg = load(root)
    except ConfigError as exc:
        report.error(str(exc))
        return 1
    if cfg.path is None:
        report.error("sem .github/dev-workflow.toml: rode `bootstrap.py init`.")
    else:
        report.ok(f"configuração válida: {cfg.path.relative_to(root).as_posix()}")
    skill_dir = skill_dir_in(root)
    if skill_dir is None:
        report.error(
            f"o skill está fora do repositório; o CI precisa dele versionado (bootstrap.py init copia para {VENDOR_PATH})."
        )
        skill_dir = VENDOR_PATH
    else:
        report.ok(f"skill dev-workflow {skill_version()} versionado em {skill_dir}")

    values = template_values(root, skill_dir, cfg.default_branch, cfg, git)
    for name, content in planned_files(values, selftest=False).items():
        target = root / name
        optional = name.endswith("semver-analysis.yml")
        if not target.is_file():
            (report.ok if optional else report.todo)(
                f"{name} ausente" + (" (opcional)" if optional else ": rode `bootstrap.py init`")
            )
        elif ".github/workflows/" in name and target.read_text(encoding="utf-8").replace("\r\n", "\n") != content:
            report.todo(f"{name} difere do modelo do skill (personalizado ou desatualizado)")
        else:
            report.ok(name)
    template = root / ".github/pull_request_template.md"
    if template.is_file():
        present = sections(template.read_text(encoding="utf-8"))
        missing = [name for name in cfg.pr["required_sections"] if name.casefold() not in present]
        if missing:
            report.error(f"o modelo de PR não tem as seções exigidas em pr.required_sections: {', '.join(missing)}")

    hook = hook_path(git)
    if hook.is_file():
        report.ok("hook commit-msg instalado neste checkout")
    else:
        report.todo("hook commit-msg ausente: `pre-commit install` ou `bootstrap.py install-hook`")

    repo = Repo(cfg, git)
    if cfg.versioning_enabled:
        latest = repo.latest("HEAD", final_only=False) if git.ok("rev-parse", "HEAD") else None
        current = latest[1] if latest else Version.parse(str(cfg.versioning["initial_version"]))
        problems = check_version_files(cfg, current)
        for problem in problems:
            report.error(problem)
        if not problems:
            report.ok(f"{len(cfg.versioning['files'])} entrada(s) de arquivo de versão coerentes com {current}")
        if latest is None:
            report.todo(
                f"sem tag de versão: crie a linha de base com `release.py baseline --push` ({cfg.tag_prefix}{current})"
            )
        else:
            report.ok(f"última tag de versão: {latest[0]}")

    repository = current_repository(git)
    github = GitHub(repository)
    if not github.enabled:
        report.todo("sem acesso ao GitHub (gh ou GITHUB_REPOSITORY): labels e permissões não conferidos")
    else:
        completed = run_gh(["label", "list", "--repo", str(repository), "--limit", "300", "--json", "name"])
        if completed.returncode != 0:
            report.todo(f"não consegui listar labels: {completed.stderr.strip() or 'gh indisponível'}")
        else:
            existing = {item["name"] for item in json.loads(completed.stdout or "[]")}
            missing = [name for name in cfg.all_labels() if name not in existing]
            if missing:
                report.todo(f"labels ausentes: {', '.join(missing)} (rode `github_issues.py sync-labels`)")
            else:
                report.ok(f"{len(cfg.all_labels())} labels do fluxo existem no GitHub")
        permissions = github.api("actions/permissions/workflow") if cfg.release["mode"] == "pull-request" else None
        if permissions and not permissions.get("can_approve_pull_request_reviews"):
            report.todo(
                'modo pull-request: habilite "Allow GitHub Actions to create and approve pull requests" nas configurações do Actions'
            )
    print(
        "\nPassos de quem é dono, que este comando não consegue conferir: check obrigatório na proteção de branch, "
        f"segredos opcionais (RELEASE_TOKEN, ANTHROPIC_API_KEY). Veja {skill_dir}/references/installation.md."
    )
    print(f"\n{report.errors} erro(s), {report.pending} pendência(s).")
    return 1 if report.errors else 0


def main(argv: Sequence[str] | None = None) -> int:
    utf8_streams()
    parser = argparse.ArgumentParser(prog="bootstrap.py", description="Instala e confere o skill dev-workflow.")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("init")
    command.add_argument("--default-branch", default="")
    command.add_argument("--initial-version", default="")
    command.add_argument("--force", action="store_true", help="troca arquivos que já existem")
    command.add_argument("--no-labels", action="store_true")
    command.add_argument("--selftest", action="store_true", help="instala também o workflow de autoteste do skill")
    command.add_argument("--dry-run", action="store_true")
    command = sub.add_parser("update", help="troca a cópia do skill no projeto por esta versão")
    command.add_argument(
        "--force", action="store_true", help="volta a uma versão anterior e troca modelos personalizados"
    )
    command.add_argument("--dry-run", action="store_true")
    command = sub.add_parser("install-hook")
    command.add_argument("--force", action="store_true")
    sub.add_parser("doctor")
    sub.add_parser("version", help="mostra a versão do skill")
    args = parser.parse_args(argv)
    if args.command == "init":
        return init(args)
    if args.command == "update":
        return update(args)
    if args.command == "version":
        print(skill_version())
        return 0
    if args.command == "install-hook":
        root = find_root()
        skill_dir = skill_dir_in(root)
        if skill_dir is None:
            print("ERRO: o skill está fora deste repositório; rode `bootstrap.py init` primeiro.", file=sys.stderr)
            return 1
        print(install_hook(root, skill_dir, force=args.force))
        return 0
    return doctor(args)
