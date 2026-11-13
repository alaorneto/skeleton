"""The diagram of a flow version on the instance page (#419), laid out on the server.

Steps go in columns by their distance from the start (the shortest path, so a loop back never
pushes a step further), stacked inside each column; transitions are straight lines. The
current steps and the ones already done are marked. The template renders the SVG with every
label escaped and no script, so the page's CSP stays as it is; a list of the steps follows it
for screen readers.
"""

from __future__ import annotations

from collections import deque
from typing import Any

WIDTH, HEIGHT, GAP_X, GAP_Y, MARGIN = 150, 48, 60, 28, 16


def layout(spec: Any, *, active: set[str], done: set[str]) -> dict[str, Any]:
    start = spec.start_step().key
    depth = {start: 0}
    queue = deque([start])
    while queue:
        key = queue.popleft()
        for item in spec.outgoing(key):
            if item.target not in depth:
                depth[item.target] = depth[key] + 1
                queue.append(item.target)
    for step in spec.steps:  # Unreachable steps (the check refuses them) go last, just in case.
        depth.setdefault(step.key, max(depth.values()) + 1)
    columns: dict[int, list[Any]] = {}
    for step in spec.steps:
        columns.setdefault(depth[step.key], []).append(step)
    nodes, where = [], {}
    for column, steps in sorted(columns.items()):
        for row, step in enumerate(steps):
            x, y = MARGIN + column * (WIDTH + GAP_X), MARGIN + row * (HEIGHT + GAP_Y)
            state = "active" if step.key in active else "done" if step.key in done else "idle"
            nodes.append(
                {
                    "key": step.key,
                    "label": step.title[:40],
                    "kind": step.kind,
                    "x": x,
                    "y": y,
                    "state": state,
                    "text_x": x + WIDTH // 2,
                    "text_y": y + HEIGHT // 2 + 4,
                }
            )
            where[step.key] = (x, y)
    edges = []
    for item in spec.transitions:
        (x1, y1), (x2, y2) = where[item.source], where[item.target]
        forward = x2 > x1
        edges.append(
            {
                "x1": x1 + (WIDTH if forward else 0),
                "y1": y1 + HEIGHT // 2,
                "x2": x2 if forward else x2 + WIDTH,
                "y2": y2 + HEIGHT // 2,
                "label": (item.on or item.label or item.key)[:30],
                "back": not forward,
            }
        )
    width = MARGIN * 2 + (max(columns) + 1) * (WIDTH + GAP_X) - GAP_X
    height = MARGIN * 2 + max(len(steps) for steps in columns.values()) * (HEIGHT + GAP_Y) - GAP_Y
    return {
        "nodes": nodes,
        "edges": edges,
        "width": width,
        "height": height,
        "node_width": WIDTH,
        "node_height": HEIGHT,
    }


def for_instance(instance_id: Any) -> dict[str, Any] | None:
    """The diagram of an instance the caller already read through ``FlowService``."""
    from .engine import specification_of
    from .models import FlowInstance, FlowStepRun

    instance = FlowInstance.objects.select_related("definition").filter(pk=instance_id).first()
    if instance is None:
        return None
    runs = FlowStepRun.objects.filter(instance=instance).values_list("step_key", "status")
    active = {key for key, status in runs if status in ("active", "waiting", "failed")}
    done = {key for key, status in runs if status == "completed"}
    return layout(specification_of(instance.definition), active=active, done=done)
