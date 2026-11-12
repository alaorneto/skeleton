"""Instalação e conferência do fluxo num projeto (``bootstrap.py <subcomando>``).

- ``init [--default-branch B] [--initial-version V] [--force] [--no-labels] [--dry-run]``: gera a
  configuração, os workflows, os modelos de issue e de PR e instala o hook ``commit-msg``. Se o
  skill estiver fora do repositório, copia-o para ``.claude/skills/dev-workflow`` (o CI precisa
  dos scripts versionados). Arquivos que já existem só são trocados com ``--force``.
- ``install-hook``: instala o hook ``commit-msg`` em ``.git/hooks`` (projetos sem pre-commit).
- ``doctor``: confere a instalação e lista o que falta, inclusive os passos de quem é dono.
"""

from __future__ import annotations

import argparse
import json
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
        "DEFAULT_BRANCH": branch,
        "RELEASE_LABEL": cfg.label("release") if cfg else "release",
        "REPOSITORY_URL": f"https://github.com/{repository}",
    }


def planned_files(values: dict[str, str], *, selftest: bool) -> dict[str, str]:
    """``{caminho no projeto: conteúdo}`` dos modelos, sem a configuração."""
    files: dict[str, str] = {}
    for name in (*WORKFLOWS, *(OPTIONAL_WORKFLOWS if selftest else ())):
        files[f".github/workflows/{name}"] = render((ASSETS / "workflows" / name).read_text(encoding="utf-8"), values)
    for template in sorted((ASSETS / "ISSUE_TEMPLATE").glob("*.yml")):
        files[f".github/ISSUE_TEMPLATE/{template.name}"] = render(template.read_text(encoding="utf-8"), values)
    files[".github/pull_request_template.md"] = (ASSETS / "pull_request_template.md").read_text(encoding="utf-8")
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
            shutil.copytree(
                SKILL,
                destination,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*-workspace"),
            )
        actions.append(f"skill copiado para {VENDOR_PATH} (os workflows do CI chamam os scripts de lá)")
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
        report.ok(f"skill versionado em {skill_dir}")

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
    command = sub.add_parser("install-hook")
    command.add_argument("--force", action="store_true")
    sub.add_parser("doctor")
    args = parser.parse_args(argv)
    if args.command == "init":
        return init(args)
    if args.command == "install-hook":
        root = find_root()
        skill_dir = skill_dir_in(root)
        if skill_dir is None:
            print("ERRO: o skill está fora deste repositório; rode `bootstrap.py init` primeiro.", file=sys.stderr)
            return 1
        print(install_hook(root, skill_dir, force=args.force))
        return 0
    return doctor(args)
