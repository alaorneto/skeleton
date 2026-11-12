"""Internal errors of the flow engine; ``FlowService`` translates them to the public service errors."""

from __future__ import annotations


class FlowConflict(Exception):
    """The command conflicts with the current state (``code`` is stable and safe to show)."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail or "A operação conflita com o estado atual do fluxo."
        super().__init__(self.detail)


class FlowUnavailable(Exception):
    """The app that owns the flow is disabled, unavailable or gone in this organization."""


class FlowFailure(Exception):
    """The definition cannot be executed as declared (missing name, no route, runaway loop).

    A command of a person is refused and changes nothing; a job marks the instance ``failed``.
    """

    def __init__(self, code: str, step_key: str = "") -> None:
        self.code = code
        self.step_key = step_key
        super().__init__(f"{code} at {step_key}" if step_key else code)
