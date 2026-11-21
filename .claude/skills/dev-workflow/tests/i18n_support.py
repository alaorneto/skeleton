"""Apoio dos testes de idioma: catraca de texto fixo nos módulos migrados e configuração em inglês.

Cada módulo migrado para o catálogo (``devflow/locales``) ganha um teste de catraca: nenhum texto
com acento fica fora de docstring e de comentário, porque texto com acento é texto em português que
escapou do catálogo. As marcas ``<!-- dev-workflow:… -->`` e os códigos do JSON do plano são ASCII.
"""

import ast
from pathlib import Path

import support

DEVFLOW = support.SCRIPTS / "devflow"


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def accented_literals(module: str, allowed: str = "") -> list[str]:
    """``linha: texto`` de cada literal de texto com caractere fora do ASCII, fora de docstring.

    ``allowed`` lista caracteres que podem aparecer (por exemplo ``"⚠…→"``), por serem símbolos e não
    palavras.
    """
    path = DEVFLOW / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstrings(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            extra = {char for char in node.value if ord(char) > 127 and char not in allowed}
            if extra:
                found.append(f"{module}:{node.lineno}: {node.value[:70]!r}")
    return found


def english_config(config: str = support.BASE_CONFIG) -> str:
    """A configuração do sandbox com ``language = "en"`` (a chave fica antes das tabelas)."""
    return config.replace("schema = 1\n", 'schema = 1\nlanguage = "en"\n', 1)


PR_BODY_EN = """\
## Issues
{issues}

## Summary
- {summary}

## Tests run
- `make test` — 12 passed

## Documentation
- Not applicable — no change in usage

## Version
- {version}
"""


def module_path(module: str) -> Path:
    return DEVFLOW / module
