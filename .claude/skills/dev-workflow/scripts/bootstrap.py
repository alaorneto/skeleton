"""Install and check the dev-workflow skill in a repository (only the standard library, git and gh).

- ``init [--no-labels] [--hook auto|git|pre-commit|none]``: writes ``.github/dev-workflow.toml``, the
  pull request and issue templates and the workflows, installs the ``commit-msg`` hook and creates
  the labels. Never overwrites a file that already exists: it reports it and keeps it.
- ``doctor``: checks the installation; ``falta`` items fail, ``pendente`` items (labels without
  ``gh``, owner steps) are listed without failing.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflow_config import CONFIG_PATH, ConfigError, load, repository_root  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSETS = SKILL_ROOT / "assets"
SKILL_PLACEHOLDER = "{{SKILL_DIR}}"
HOOK_MARKER = "dev-workflow commit-msg"
PRE_COMMIT_SNIPPET = """  - repo: local
    hooks:
      - id: issue-reference
        name: referência a issue do GitHub
        entry: python {skill}/scripts/github_issues.py commit-msg
        language: python
        stages: [commit-msg]
"""


@dataclass(frozen=True)
class Finding:
    state: str  # "ok", "falta" or "pendente"
    item: str
    detail: str = ""

    def line(self) -> str:
        return f"[{self.state}] {self.item}" + (f": {self.detail}" if self.detail else "")


def skill_dir(root: Path) -> str:
    """The skill directory relative to the repository root, with forward slashes (for workflows)."""
    try:
        return SKILL_ROOT.relative_to(root.resolve()).as_posix()
    except ValueError:
        return ".claude/skills/dev-workflow"  # The skill is copied there by init.


def _render(text: str, root: Path) -> str:
    return text.replace(SKILL_PLACEHOLDER, skill_dir(root))


def _copy(source: Path, target: Path, root: Path, report: list[Finding]) -> None:
    relative = target.relative_to(root).as_posix()
    if target.exists():
        report.append(Finding("ok", relative, "já existe; mantido"))
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render(source.read_text(encoding="utf-8"), root), encoding="utf-8", newline="\n")
    report.append(Finding("ok", relative, "criado"))


def _assets(root: Path) -> list[tuple[Path, Path]]:
    pairs = [
        (ASSETS / "dev-workflow.toml", root / CONFIG_PATH),
        (ASSETS / "pull_request_template.md", root / ".github" / "pull_request_template.md"),
    ]
    for folder, target in (("ISSUE_TEMPLATE", ".github/ISSUE_TEMPLATE"), ("workflows", ".github/workflows")):
        for source in sorted((ASSETS / folder).glob("*")):
            if source.is_file():
                pairs.append((source, root / target / source.name))
    return pairs


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def hook_path(root: Path) -> Path:
    return (root / _git(root, "rev-parse", "--git-path", "hooks/commit-msg")).resolve()


def hook_script(root: Path) -> str:
    return (ASSETS / "hooks" / "commit-msg").read_text(encoding="utf-8").replace(SKILL_PLACEHOLDER, skill_dir(root))


def install_hook(root: Path, mode: str, report: list[Finding]) -> None:
    uses_pre_commit = (root / ".pre-commit-config.yaml").is_file()
    if mode == "none":
        report.append(Finding("pendente", "hook commit-msg", "instalação pulada (--hook none)"))
        return
    if mode == "pre-commit" or (mode == "auto" and uses_pre_commit):
        text = (root / ".pre-commit-config.yaml").read_text(encoding="utf-8") if uses_pre_commit else ""
        if "github_issues.py commit-msg" in text:
            report.append(Finding("ok", "hook commit-msg", "configurado no pre-commit"))
        else:
            report.append(
                Finding(
                    "pendente",
                    "hook commit-msg",
                    "acrescente ao .pre-commit-config.yaml e rode `pre-commit install --hook-type commit-msg`:\n"
                    + PRE_COMMIT_SNIPPET.format(skill=skill_dir(root)),
                )
            )
        return
    target = hook_path(root)
    if target.exists() and HOOK_MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        report.append(Finding("falta", "hook commit-msg", f"{target} já existe e não é deste skill; mantido"))
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(hook_script(root), encoding="utf-8", newline="\n")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    report.append(Finding("ok", "hook commit-msg", f"instalado em {target}"))


GhRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run_gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", check=False)
    except OSError as exc:
        return subprocess.CompletedProcess(list(args), 127, "", str(exc))


def init(root: Path, *, labels: bool = True, hook: str = "auto", gh: GhRunner = _run_gh) -> list[Finding]:
    report: list[Finding] = []
    for source, target in _assets(root):
        _copy(source, target, root, report)
    try:
        config = load(root)
    except ConfigError as exc:
        report.append(Finding("falta", CONFIG_PATH.as_posix(), str(exc)))
        return report
    install_hook(root, hook, report)
    if labels:
        from github_issues import sync_labels

        if gh(["auth", "status"]).returncode != 0:
            report.append(
                Finding("pendente", "labels", "gh indisponível ou sem login; rode `github_issues.py sync-labels`")
            )
        elif sync_labels(config, gh):
            report.append(Finding("falta", "labels", "falha ao criar algum label (veja acima)"))
        else:
            report.append(Finding("ok", "labels", "criados ou atualizados"))
    return report


def doctor(root: Path, *, gh: GhRunner = _run_gh) -> list[Finding]:
    report: list[Finding] = []
    try:
        config = load(root)
        report.append(Finding("ok", CONFIG_PATH.as_posix()))
    except ConfigError as exc:
        return [Finding("falta", CONFIG_PATH.as_posix(), str(exc))]
    for source, target in _assets(root):
        if target == root / CONFIG_PATH:
            continue
        relative = target.relative_to(root).as_posix()
        if not target.is_file():
            report.append(Finding("falta", relative, "rode bootstrap.py init"))
        elif source.parent.name == "workflows" and skill_dir(root) not in target.read_text(encoding="utf-8"):
            report.append(Finding("falta", relative, f"não chama os scripts de {skill_dir(root)}"))
        else:
            report.append(Finding("ok", relative))
    pre_commit = root / ".pre-commit-config.yaml"
    if pre_commit.is_file() and "github_issues.py commit-msg" in pre_commit.read_text(encoding="utf-8"):
        installed = hook_path(root).exists()
        report.append(
            Finding(
                "ok" if installed else "falta",
                "hook commit-msg",
                "pre-commit" if installed else "rode `pre-commit install`",
            )
        )
    else:
        target = hook_path(root)
        ok = target.exists() and HOOK_MARKER in target.read_text(encoding="utf-8", errors="replace")
        report.append(Finding("ok" if ok else "falta", "hook commit-msg", "" if ok else "rode bootstrap.py init"))
    completed = gh(["label", "list", "--limit", "200", "--json", "name", "-q", ".[].name"])
    if completed.returncode != 0:
        report.append(Finding("pendente", "labels", "gh indisponível ou sem login"))
    else:
        existing = set(completed.stdout.split())
        missing = [label.name for label in (*config.types, *config.extra_labels) if label.name not in existing]
        report.append(
            Finding("falta", "labels", "faltam " + ", ".join(missing)) if missing else Finding("ok", "labels")
        )
    if config.versioning.enabled:
        from release import check_files

        problems = check_files(config, root)
        report.append(Finding("falta", "versão", "; ".join(problems)) if problems else Finding("ok", "versão"))
        tags = subprocess.run(
            ["git", "tag", "--list", f"{config.versioning.tag_prefix}*"], cwd=root, capture_output=True, text=True
        ).stdout.split()
        if not tags:
            report.append(Finding("pendente", "tag de base", "passo do responsável: release.py baseline VERSÃO"))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    root = repository_root(Path(os.getcwd()))
    if args[:1] == ["init"]:
        options = args[1:]
        hook = "auto"
        if "--hook" in options:
            index = options.index("--hook")
            if index + 1 >= len(options) or options[index + 1] not in ("auto", "git", "pre-commit", "none"):
                print("uso: bootstrap.py init [--no-labels] [--hook auto|git|pre-commit|none]", file=sys.stderr)
                return 2
            hook = options[index + 1]
            del options[index : index + 2]
        if set(options) - {"--no-labels"}:
            print("uso: bootstrap.py init [--no-labels] [--hook auto|git|pre-commit|none]", file=sys.stderr)
            return 2
        report = init(root, labels="--no-labels" not in options, hook=hook)
    elif args == ["doctor"]:
        report = doctor(root)
    else:
        print("uso: bootstrap.py init [--no-labels] [--hook ...] | doctor", file=sys.stderr)
        return 2
    for finding in report:
        print(finding.line())
    return 1 if any(finding.state == "falta" for finding in report) else 0


if __name__ == "__main__":
    raise SystemExit(main())
