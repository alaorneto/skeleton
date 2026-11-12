"""The core documentation cites only files, anchors, commands, services and packages that exist (#339).

Fast and without database: it reads the Markdown of the repository and introspects the service
classes. Code examples of apps are checked by test_docs_app_snippets.py and the check identifiers
by test_check_reference.py; this file covers the rest.
"""

import importlib
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIPPED_DIRECTORIES = {".git", ".venv", "node_modules", "build", "dist", "var", "worktrees", "__pycache__"}
PATH_PREFIXES = (
    "skeleton/",
    "tests/",
    "tools/",
    "docs/",
    "config/",
    "deploy/",
    "examples/",
    "deployment_agent/",
    ".github/",
    ".claude/",
)
# Paths cited in backticks that are not paths of this repository, each with its reason.
PATH_EXCEPTIONS = {
    "docs/spec.md": "file of an app's documentation_root, not of the core",
    "docs/adr.md": "file of an app's documentation_root, not of the core",
    "tests/test_service_api.py": "test module of an app created by skeleton_startapp",
    "skeleton/python/django": "a label of the Compose image, not a path",
    "tests/integration/test_x.py": "placeholder for the file being worked on (docs/testing.md table)",
}
# Documents that cite the paths and commands of their time on purpose: historical evidence.
HISTORICAL_DOCUMENTS = {"docs/testing-evidence.md", "docs/versioning-packages-govbr-plan.md"}
# Top-level docs that the reading map leaves out on purpose.
UNCITED_HISTORY = {"versioning-packages-govbr-plan.md": "original plan of versioning/packages/gov.br, kept as history"}
DJANGO_COMMANDS = {
    "check",
    "migrate",
    "makemigrations",
    "runserver",
    "shell",
    "collectstatic",
    "createsuperuser",
    "showmigrations",
    "compilemessages",
    "makemessages",
    "changepassword",
}
FENCE = re.compile(r"^\s*(```|~~~)")


def documents():
    found = [ROOT / "README.md", ROOT / "AGENTS.md"]
    for base in (ROOT / "docs", ROOT / ".claude" / "skills"):
        found += [path for path in base.rglob("*.md") if not SKIPPED_DIRECTORIES & set(path.parts)]
    return sorted(found)


def relative(path):
    return path.relative_to(ROOT).as_posix()


def prose_lines(path):
    """(line number, text) of the lines outside fenced code blocks."""
    inside = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            yield number, line


def slug(title):
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title).replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path):
    seen, result = {}, set()
    for _, line in prose_lines(path):
        match = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        base = slug(match.group(2))
        count = seen.get(base, 0)
        seen[base] = count + 1
        result.add(base if count == 0 else f"{base}-{count}")
    return result


def links(path):
    for number, line in prose_lines(path):
        line = re.sub(r"`[^`]*`", "", line)
        for match in re.finditer(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", line):
            yield number, match.group(1)


def test_relative_links_and_anchors_resolve():
    problems, cache = [], {}
    for document in documents():
        for number, target in links(document):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                continue  # http:, https:, mailto: are not checked (no network).
            file_part, _, anchor = target.partition("#")
            resolved = (document.parent / file_part).resolve() if file_part else document
            if not resolved.exists():
                problems.append(f"{relative(document)}:{number} -> missing file {target}")
                continue
            if anchor and resolved.suffix == ".md":
                if resolved not in cache:
                    cache[resolved] = anchors(resolved)
                if anchor not in cache[resolved]:
                    problems.append(f"{relative(document)}:{number} -> missing anchor {target}")
    assert problems == []


def _template(path):
    """A template name such as skeleton/web/pagination.html, resolved under a templates/ directory."""
    return path.endswith((".html", ".txt")) and any(
        (base / path).exists() for base in (ROOT / "skeleton").glob("*/templates")
    )


def test_cited_repository_paths_exist():
    problems = []
    for document in documents():
        if relative(document) in HISTORICAL_DOCUMENTS:
            continue
        for number, line in prose_lines(document):
            for span in re.findall(r"`([^`]+)`", line):
                if not span.startswith(PATH_PREFIXES) or re.search(r"[<>*…\s{}$]", span):
                    continue
                path = re.sub(r":\d+(-\d+)?$", "", span).rstrip("/.,;")
                path = path.split("#", 1)[0].split("::", 1)[0]
                if path in PATH_EXCEPTIONS or (ROOT / path).exists() or _template(path):
                    continue
                problems.append(f"{relative(document)}:{number} -> {span}")
    assert problems == []


def _service_classes():
    from skeleton.service_api.base import ServiceAPI

    classes = {}
    for module_path in sorted((ROOT / "skeleton" / "service_api").glob("*.py")):
        module = importlib.import_module(f"skeleton.service_api.{module_path.stem}")
        for name, value in inspect.getmembers(module, inspect.isclass):
            if issubclass(value, ServiceAPI) and value is not ServiceAPI and value.__module__ == module.__name__:
                classes[name] = set(value.service_methods())
    return classes


def _catalog_rows():
    text = (ROOT / "docs" / "service-api.md").read_text(encoding="utf-8")
    section = text.split("## Catálogo do núcleo", 1)[1].split("\n### ", 1)[0]  # The summary table only.
    rows = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].startswith("`"):
            rows.append((set(re.findall(r"`(\w+)`", cells[0])), set(re.findall(r"`(\w+)`", cells[2]))))
    return rows


def test_core_service_catalog_matches_code():
    classes, rows = _service_classes(), _catalog_rows()
    problems = [f"{name} has no row in the catalog" for name in classes if not any(name in row[0] for row in rows)]
    for names, methods in rows:
        unknown = names - set(classes)
        if unknown:
            problems.append(f"catalog lists unknown services {sorted(unknown)}")
            continue
        expected = set().union(*(classes[name] for name in names))
        if methods != expected:
            problems.append(f"{sorted(names)}: catalog {sorted(methods)} != code {sorted(expected)}")
    assert problems == []


def test_agents_and_architecture_list_every_core_package():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    boundaries = agents.split("## Fronteiras de código", 1)[1].split("\n## ", 1)[0]
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    problems = []
    for package in sorted(path.parent.name for path in (ROOT / "skeleton").glob("*/__init__.py")):
        for name, text in (("AGENTS.md", boundaries), ("docs/architecture.md", architecture)):
            if f"`skeleton/{package}`" not in text:
                problems.append(f"{name} misses skeleton/{package}")
    for apps_file in sorted((ROOT / "skeleton").glob("*/apps.py")):
        for label in re.findall(r'label = "(sk_\w+)"', apps_file.read_text(encoding="utf-8")):
            if f"`{label}`" not in architecture:
                problems.append(f"docs/architecture.md misses the label {label}")
    assert problems == []


def _commands():
    names = set()
    for base in (ROOT / "skeleton", ROOT / "examples", ROOT / "applications", ROOT / "config"):
        for path in base.rglob("management/commands/*.py"):
            if not SKIPPED_DIRECTORIES & set(path.parts) and path.stem != "__init__":
                names.add(path.stem)
    return names


def test_documented_management_commands_exist():
    known = _commands() | DJANGO_COMMANDS
    problems = []
    for document in documents():
        if relative(document) in HISTORICAL_DOCUMENTS:
            continue
        for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
            for command in re.findall(r"manage\.py\s+([a-z_]+)", line):
                if command not in known:
                    problems.append(f"{relative(document)}:{number} -> manage.py {command}")
    assert problems == []


def test_reading_order_documents_exist():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    order = agents.split("## Ordem de leitura", 1)[1].split("\n## ", 1)[0]
    problems = [f"AGENTS.md reading order cites missing {path}" for path in re.findall(r"`(docs/[^`]+\.md)`", order)]
    problems = [problem for problem in problems if not (ROOT / problem.rsplit(" ", 1)[1]).exists()]
    cited = (ROOT / "README.md").read_text(encoding="utf-8") + agents
    for path in sorted((ROOT / "docs").glob("*.md")):
        name = path.name
        if name in UNCITED_HISTORY:
            continue
        if f"docs/{name}" not in cited and f"({name})" not in cited:
            problems.append(f"docs/{name} is cited neither in README.md nor in AGENTS.md")
    assert problems == []
