"""Names of people for the screens: one IdentityService lookup per page (#475).

The screens keep the identifiers they already had (audit actor, comment author, task
assignee) and show the display name of those who are active members of the current
organization; anyone else stays an identifier ("Pessoa indisponível").
"""

from __future__ import annotations

from collections.abc import Iterable

LIMIT = 200


def people_labels(user_ids: Iterable, *, actor) -> dict[str, str]:
    """{str(sysid): display name} for the members the actor may see; missing ids are omitted."""
    from skeleton.service_api.errors import ContextRequired, Forbidden, InvalidInput
    from skeleton.service_api.identity import IdentityService

    identifiers = list(dict.fromkeys(str(item) for item in user_ids if item))[:LIMIT]
    if not identifiers:
        return {}
    try:
        found = IdentityService().people(actor=actor, user_ids=tuple(identifiers))
    except (ContextRequired, Forbidden, InvalidInput):
        return {}
    return {str(person.sysid): person.display_name for person in found}
