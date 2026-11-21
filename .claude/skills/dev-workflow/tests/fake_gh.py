#!/usr/bin/env python3
"""Simula o subconjunto de ``gh api`` que o fluxo usa, sobre um arquivo JSON de estado.

Uso nos testes: ``DEV_WORKFLOW_GH='["python", ".../fake_gh.py"]'`` e ``FAKE_GH_STATE=<arquivo>``.
REST: ``gh api -X MÉTODO repos/<dono>/<repo>/<caminho> [--paginate --slurp] [--input -]``, com as
listas cortadas em páginas como a API faz (``per_page``, 30 por padrão e no máximo 100; ``page``).
Além dele: ``gh api graphql`` (só ``issueOrPullRequest``), ``gh label list|create``,
``gh issue list|view|edit|close`` e ``gh repo view``, sobre as mesmas chaves do estado.
"""

import json
import os
import re
import sys
from urllib.parse import parse_qs, urlsplit


def not_found():
    sys.stderr.write("gh: Not Found (HTTP 404)\n")
    raise SystemExit(1)


def as_issue(number, state):
    key = str(number)
    if key in state["issues"]:
        return state["issues"][key]
    if key in state["pulls"]:
        pull = state["pulls"][key]
        return {**pull, "pull_request": {}}
    not_found()


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def handle(method, path, query, body, state, repository=""):
    actor = state.get("actor", {"login": "github-actions[bot]", "association": "NONE"})
    # Limites do GitHub: o corpo de release vai até 125 000 caracteres; PR, issue e comentário, 65 536.
    limit = 125_000 if path == "releases" else 65_536
    if method in ("POST", "PATCH") and utf16_length(str((body or {}).get("body") or "")) > limit:
        sys.stderr.write(f"gh: Validation Failed: body is too long (maximum is {limit} characters) (HTTP 422)\n")
        return SystemExit(1)

    def new_id():
        state["next_id"] = state.get("next_id", 1000) + 1
        return state["next_id"]

    match = re.fullmatch(r"pulls/(\d+)", path)
    if match and method == "GET":
        return state["pulls"].get(match[1]) or not_found()
    if match and method == "PATCH":
        state["pulls"][match[1]].update(body)
        return state["pulls"][match[1]]
    match = re.fullmatch(r"pulls/(\d+)/commits", path)
    if match:
        return state["pulls"].get(match[1], {}).get("commit_list", [])
    if path == "pulls" and method == "GET":
        head = query.get("head", [""])[0].split(":")[-1]
        return [p for p in state["pulls"].values() if p["state"] == "open" and p["head"]["ref"] == head]
    if path == "pulls" and method == "POST":
        number = new_id()
        pull = {
            "number": number,
            "state": "open",
            "merged": False,
            "merged_at": None,
            "labels": [],
            "user": {"login": actor["login"]},
            "title": body["title"],
            "body": body.get("body", ""),
            "head": {"ref": body["head"], "repo": {"full_name": repository}},
            "base": {"ref": body["base"], "repo": {"full_name": repository}},
        }
        state["pulls"][str(number)] = pull
        return pull
    match = re.fullmatch(r"commits/([0-9a-f]+)/pulls", path)
    if match:
        return [
            p
            for p in state["pulls"].values()
            if p.get("merge_commit_sha") == match[1] or match[1] in p.get("commit_shas", [])
        ]
    match = re.fullmatch(r"issues/(\d+)", path)
    if match and method == "GET":
        return as_issue(match[1], state)
    if match and method == "PATCH":
        target = state["issues"].get(match[1]) or state["pulls"].get(match[1]) or not_found()
        if "milestone" in body:
            target["milestone"] = {"number": body["milestone"]}
        return target
    match = re.fullmatch(r"issues/(\d+)/comments", path)
    if match and method == "GET":
        as_issue(match[1], state)
        return state["comments"].get(match[1], [])
    if match and method == "POST":
        as_issue(match[1], state)
        comment = {
            "id": new_id(),
            "body": body["body"],
            "user": {"login": actor["login"]},
            "author_association": actor.get("association", "NONE"),
            "created_at": f"2030-01-01T00:00:{len(state.get('log', [])):02d}Z",
        }
        state["comments"].setdefault(match[1], []).append(comment)
        return comment
    match = re.fullmatch(r"issues/comments/(\d+)", path)
    if match and method == "PATCH":
        for comments in state["comments"].values():
            for comment in comments:
                if comment["id"] == int(match[1]):
                    comment["body"] = body["body"]
                    return comment
        not_found()
    if path == "milestones" and method == "GET":
        return state["milestones"]
    if path == "milestones" and method == "POST":
        milestone = {"number": len(state["milestones"]) + 1, "title": body["title"], "state": "open"}
        state["milestones"].append(milestone)
        return milestone
    match = re.fullmatch(r"milestones/(\d+)", path)
    if match and method == "PATCH":
        milestone = state["milestones"][int(match[1]) - 1]
        milestone.update(body)
        return milestone
    match = re.fullmatch(r"releases/tags/(.+)", path)
    if match:
        return state["releases"].get(match[1]) or not_found()
    if path == "releases" and method == "POST":
        if state.get("fail_release_once"):
            state["fail_release_once"] = False
            sys.stderr.write("gh: Server Error (HTTP 500)\n")
            return SystemExit(1)
        state["releases"][body["tag_name"]] = body
        return body
    match = re.fullmatch(r"actions/workflows/(.+)/dispatches", path)
    if match and method == "POST":
        state["dispatches"].append({"workflow": match[1], **body})
        return {}
    sys.stderr.write(f"fake_gh: rota não simulada: {method} {path}\n")
    raise SystemExit(2)


MAX_PER_PAGE = 100  # o teto do GitHub para ``per_page``
DEFAULT_PER_PAGE = 30


def pages_of(items, query):
    """Corta uma lista como a API REST: ``per_page`` (30 por padrão, até 100) a partir de ``page``."""
    per_page = min(max(int(query.get("per_page", [DEFAULT_PER_PAGE])[0]), 1), MAX_PER_PAGE)
    first = max(int(query.get("page", ["1"])[0]), 1)
    pages = [items[start : start + per_page] for start in range(0, len(items), per_page)] or [[]]
    return pages[first - 1 :] or [[]]


def paged_output(result, query, paginate, slurp, state, path):
    """O que o ``gh`` escreve: sem ``--paginate`` só a página pedida; com ele, todas as seguintes.

    O ``gh`` real segue o cabeçalho ``Link: rel="next"`` uma requisição por página; aqui cada página
    servida fica em ``state["pages"]``, para os testes conferirem que a leitura passou da primeira.
    """
    if not isinstance(result, list):
        return [result] if paginate else result
    pages = pages_of(result, query)
    if not paginate:
        return pages[0]
    for number, _ in enumerate(pages, start=int(query.get("page", ["1"])[0])):
        state.setdefault("pages", []).append(f"GET {path} page={number}")
    return pages if slurp else [item for page in pages for item in page]


# --- subcomandos além de ``gh api`` ------------------------------------------------------------


def _option(args, name, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def _issue_view(issue, fields):
    closed = issue.get("state") == "closed"
    reason = issue.get("state_reason")
    data = {
        "number": issue["number"],
        "title": issue.get("title", ""),
        "body": issue.get("body", ""),
        "state": "CLOSED" if closed else "OPEN",
        "stateReason": reason.upper().replace(" ", "_") if reason else None,
        "labels": [{"name": label["name"]} for label in issue.get("labels", [])],
    }
    return {key: data.get(key) for key in fields} if fields else data


def _repository_ref(state, number):
    return {"number": int(number), "repository": {"nameWithOwner": state.get("repository", "acme/demo")}}


def graphql(query, state):
    """``issueOrPullRequest`` em lote, como ``epics.fetch_links`` pede; número inexistente vira erro."""
    nodes, errors = {}, []
    for alias, number in re.findall(r"\b(i\d+): issueOrPullRequest\(number: (\d+)\)", query):
        issue = state["issues"].get(number)
        if issue is None and number in state["pulls"]:
            nodes[alias] = {"__typename": "PullRequest"}
            continue
        if issue is None:
            nodes[alias] = None
            errors.append({"type": "NOT_FOUND", "path": ["repository", alias]})
            continue
        view = _issue_view(issue, ())
        parent = issue.get("parent")
        nodes[alias] = {
            "__typename": "Issue",
            "number": issue["number"],
            "state": view["state"],
            "stateReason": view["stateReason"],
            "title": view["title"],
            "parent": _repository_ref(state, parent) if parent else None,
            "subIssues": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [_repository_ref(state, n) for n in issue.get("sub_issues", [])],
            },
            "blockedBy": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [_repository_ref(state, n) for n in issue.get("blocked_by", [])],
            },
        }
    return {"data": {"repository": nodes}, "errors": errors} if errors else {"data": {"repository": nodes}}


def cli(argv, state):
    """``gh label``, ``gh issue`` e ``gh repo view``: devolve ``(código, saída, erro)``."""
    state.setdefault("cli", []).append(" ".join(argv[:3]))
    fields = [field for field in (_option(argv, "--json") or "").split(",") if field]
    if argv[:2] == ["repo", "view"]:
        return 0, state.get("repository", "acme/demo") + "\n", ""
    if argv[:2] == ["label", "list"]:
        labels = [{"name": name, **spec} for name, spec in state.get("labels", {}).items()]
        limit = int(_option(argv, "--limit", "30"))
        return 0, json.dumps([{key: item.get(key) for key in fields} if fields else item for item in labels[:limit]]), ""
    if argv[:2] == ["label", "create"]:
        name = argv[2]
        labels = state.setdefault("labels", {})
        if name in state.get("fail_labels", []):
            return 1, "", f"HTTP 403: Resource not accessible by integration (label {name})\n"
        if name in labels and "--force" not in argv:
            return 1, "", f"label with name \"{name}\" already exists; use `--force` to update its color and description\n"
        labels[name] = {"color": _option(argv, "--color", ""), "description": _option(argv, "--description", "")}
        return 0, "", ""
    if argv[:1] == ["issue"] and len(argv) >= 2:
        action = argv[1]
        if action == "list":
            wanted_state = _option(argv, "--state", "open")
            label = _option(argv, "--label")
            limit = int(_option(argv, "--limit", "30"))
            found = [
                _issue_view(issue, fields)
                for issue in state["issues"].values()
                if (wanted_state == "all" or issue.get("state", "open") == wanted_state)
                and (label is None or label in {item["name"] for item in issue.get("labels", [])})
            ]
            return 0, json.dumps(found[:limit], ensure_ascii=False), ""
        number = argv[2].lstrip("#") if len(argv) > 2 else ""
        issue = state["issues"].get(number)
        if issue is None:
            return 1, "", f"GraphQL: Could not resolve to an issue or pull request with the number of {number}.\n"
        if action == "view":
            return 0, json.dumps(_issue_view(issue, fields), ensure_ascii=False), ""
        if action == "edit":
            body_file = _option(argv, "--body-file")
            if body_file:
                with open(body_file, encoding="utf-8", newline="") as file:
                    issue["body"] = file.read()
            for flag, key in (("--add-sub-issue", "sub_issues"), ("--add-blocked-by", "blocked_by")):
                ref = _option(argv, flag)
                if ref:
                    issue.setdefault(key, []).append(int(ref.lstrip("#")))
                    if key == "sub_issues" and ref.lstrip("#") in state["issues"]:
                        state["issues"][ref.lstrip("#")]["parent"] = int(number)
            return 0, "", ""
        if action == "close":
            issue["state"] = "closed"
            issue["state_reason"] = _option(argv, "--reason", "completed")
            comment = _option(argv, "--comment")
            if comment:
                state["comments"].setdefault(number, []).append(
                    {"id": 9000 + len(state["cli"]), "body": comment, "user": {"login": "github-actions[bot]"}}
                )
            return 0, "", ""
    return None


def main(argv):
    if argv[:1] != ["api"] or argv[1:2] == ["graphql"]:
        state_path = os.environ["FAKE_GH_STATE"]
        with open(state_path, encoding="utf-8") as file:
            state = json.load(file)
        if argv[:2] == ["api", "graphql"]:
            state.setdefault("cli", []).append("api graphql")
            payload = graphql(_option(argv, "-f", ""), state)
            outcome = (1 if "errors" in payload else 0, json.dumps(payload), "")
        else:
            outcome = cli(argv, state)
        if outcome is None:
            sys.stderr.write(f"fake_gh: comando não simulado: {argv}\n")
            return 2
        with open(state_path, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=1)
        code, out, err = outcome
        sys.stdout.write(out)
        sys.stderr.write(err)
        return code
    method, target, paginate, slurp, has_input = "GET", None, False, False, False
    rest = argv[1:]
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg == "-X":
            method = rest[index + 1]
            index += 1
        elif arg == "--paginate":
            paginate = True
        elif arg == "--slurp":
            slurp = True
        elif arg == "--input":
            has_input = True
            index += 1
        else:
            target = arg
        index += 1
    body = json.loads(sys.stdin.read()) if has_input else None
    parts = urlsplit(target)
    path = re.sub(r"^repos/[^/]+/[^/]+/?", "", parts.path)
    slug = re.match(r"^repos/([^/]+/[^/]+)", parts.path)
    state_path = os.environ["FAKE_GH_STATE"]
    with open(state_path, encoding="utf-8") as file:
        state = json.load(file)
    state.setdefault("log", []).append(f"{method} {path}")
    # Falhas programadas por rota: ``state["failures"]["GET pulls/10"] = [502, 503]`` faz as duas
    # próximas chamadas a essa rota falharem com esses status, como um GitHub instável.
    planned = state.get("failures", {}).get(f"{method} {path}")
    if planned:
        status = planned.pop(0)
        with open(state_path, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=1)
        sys.stderr.write(f"gh: Server Error (HTTP {status})\n")
        return 1
    query = parse_qs(parts.query)
    result = handle(method, path, query, body, state, slug[1] if slug else "")
    if not isinstance(result, SystemExit):
        result = paged_output(result, query, paginate, slurp, state, path)
    with open(state_path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=1)
    if isinstance(result, SystemExit):
        return result.code
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
