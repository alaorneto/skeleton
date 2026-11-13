"""Canonical forms of SQL text, to compare installed definitions with the expected ones."""

import re


def _canonical(expression):
    """Compare Boolean expression trees, retaining AND/OR grouping semantics."""
    if expression is None:
        return None
    value = re.sub(r"::(?:pg_catalog\.)?text\b", "", expression.lower())
    tokens = re.findall(r"'(?:''|[^'])*'|::|[a-z_][a-z_0-9.]*|[(),=]|[^\s]", value.replace('"', ""))

    def tree(items):
        while items and items[0] == "(" and items[-1] == ")":
            depth = 0
            enclosed = True
            for index, token in enumerate(items):
                depth += (token == "(") - (token == ")")  # noqa: S105 - SQL tokenizer variable, not a secret
                if depth == 0 and index != len(items) - 1:
                    enclosed = False
                    break
            if not enclosed:
                break
            items = items[1:-1]
        for operator in ("or", "and"):
            depth, start, pieces = 0, 0, []
            for index, token in enumerate(items):
                depth += (token == "(") - (token == ")")  # noqa: S105 - SQL tokenizer variable, not a secret
                if token == operator and depth == 0:
                    pieces.append(tree(items[start:index]))
                    start = index + 1
            if pieces:
                pieces.append(tree(items[start:]))
                return (operator, tuple(pieces))
        return tuple(token for token in items if token not in ("(", ")"))

    return tree(tokens)


def _sql_tokens(sql):
    return tuple(
        token
        for token in re.findall(r"'(?:''|[^'])*'|[a-zA-Z_][a-zA-Z_0-9]*|[^\s]", sql.replace('"', ""))
        if token != ";"  # noqa: S105 - SQL tokenizer variable, not a secret
    )


def _trigger_tokens(sql):
    # PostgreSQL prints event order canonically, independent of the creation SQL.
    value = re.sub(
        r"\b(BEFORE|AFTER|INSTEAD OF)\s+(.+?)\s+ON\b",
        lambda match: match[1] + " " + " OR ".join(sorted(match[2].split(" OR "))) + " ON",
        sql,
    )
    return _sql_tokens(value)
