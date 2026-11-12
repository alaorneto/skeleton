#!/usr/bin/env python3
"""Simula o subconjunto de ``gh api`` que o fluxo usa, sobre um arquivo JSON de estado.

Uso nos testes: ``DEV_WORKFLOW_GH='["python", ".../fake_gh.py"]'`` e ``FAKE_GH_STATE=<arquivo>``.
Só REST: ``gh api -X MÉTODO repos/<dono>/<repo>/<caminho> [--paginate --slurp] [--input -]``.
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


def handle(method, path, query, body, state):
    actor = state.get("actor", {"login": "github-actions[bot]", "association": "NONE"})

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
            "head": {"ref": body["head"]},
            "base": {"ref": body["base"]},
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
    match = re.fullmatch(r"issues/(\d+)/labels", path)
    if match and method == "POST":
        target = state["issues"].get(match[1]) or state["pulls"].get(match[1]) or not_found()
        names = {label["name"] for label in target["labels"]}
        target["labels"] += [{"name": name} for name in body["labels"] if name not in names]
        return target["labels"]
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


def main(argv):
    if argv[:1] != ["api"]:
        sys.stderr.write(f"fake_gh: comando não simulado: {argv}\n")
        return 2
    method, target, paginate, has_input = "GET", None, False, False
    rest = argv[1:]
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg == "-X":
            method = rest[index + 1]
            index += 1
        elif arg == "--paginate":
            paginate = True
        elif arg == "--input":
            has_input = True
            index += 1
        elif arg != "--slurp":
            target = arg
        index += 1
    body = json.loads(sys.stdin.read()) if has_input else None
    parts = urlsplit(target)
    path = re.sub(r"^repos/[^/]+/[^/]+/?", "", parts.path)
    state_path = os.environ["FAKE_GH_STATE"]
    with open(state_path, encoding="utf-8") as file:
        state = json.load(file)
    state.setdefault("log", []).append(f"{method} {path}")
    result = handle(method, path, parse_qs(parts.query), body, state)
    with open(state_path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=1)
    if isinstance(result, SystemExit):
        return result.code
    sys.stdout.write(json.dumps([result] if paginate else result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
