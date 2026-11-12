"""Code the documentation presents as app code imports only public core surfaces (#260)."""

import ast
import re
from pathlib import Path

from skeleton.system.import_boundary import PUBLIC_CORE_MODULES, PUBLIC_CORE_PACKAGES

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = (
    "docs/sequences.md",
    "docs/events.md",
    "docs/jobs.md",
    "docs/notifications.md",
    "docs/service-api.md",
    "docs/extending.md",
    "docs/files.md",
    ".claude/skills/service-api/references/templates.md",
)
# (document, module): blocks that show core or operation code, not app code. Keep it short.
EXCEPTIONS: frozenset[tuple[str, str]] = frozenset()
BLOCK = re.compile(r"^```python\n(.*?)^```", re.DOTALL | re.MULTILINE)


def _public(module):
    return module in PUBLIC_CORE_MODULES or any(
        module == package or module.startswith(package + ".") for package in PUBLIC_CORE_PACKAGES
    )


def private_imports(markdown):
    """Core modules outside the public surfaces imported by the valid Python blocks of a document."""
    found = []
    for block in BLOCK.findall(markdown.replace("\r\n", "\n")):
        try:
            tree = ast.parse(block)
        except SyntaxError:
            continue  # Fragments with "..." are illustrations, not code.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                modules = [node.module]
                if node.module == "skeleton":
                    modules = [f"skeleton.{alias.name}" for alias in node.names]
            else:
                continue
            found += [module for module in modules if module.startswith("skeleton.") and not _public(module)]
    return found


def test_documented_app_snippets_import_only_public_core():
    problems = [
        (document, module)
        for document in DOCUMENTS
        for module in private_imports((ROOT / document).read_text(encoding="utf-8"))
        if (document, module) not in EXCEPTIONS
    ]
    assert problems == []


def test_snippet_scanner_reports_private_import():
    markdown = (
        "Texto.\n\n```python\nfrom skeleton.jobs.services import enqueue\nfrom skeleton.service_api import Page\n```\n"
    )
    assert private_imports(markdown) == ["skeleton.jobs.services"]
    assert private_imports("```python\nfrom skeleton.jobs.services import ...  # trecho\nx = (\n```\n") == []
