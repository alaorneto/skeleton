"""Metrics an app adds to the management figures of the instance (#710).

``register_instance_metric("<app_label>", collector)`` in ``AppConfig.ready()``: the collector
receives an organization id, runs in that organization's read-only context during the hourly
refresh and returns ``{"<name>": int}``; the values are summed over the organizations as the
daily metrics ``<app_label>.<name>``. Never personal data: only counts.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID


def register_instance_metric(code_prefix: str, collector: Callable[[UUID], dict[str, int]]) -> None:
    from skeleton.insights.collect import register_instance_metric as register

    register(code_prefix, collector)
