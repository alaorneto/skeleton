"""A scenario with several roles per organization, as an app in its own repository would build it (#572).

This module imports only ``skeleton.testing.*``: ``test_people_kit.py`` scans it with the
import boundary analysis (E019/W019) and expects no violation. Keep it that way.
"""

from types import SimpleNamespace

from skeleton.testing.people import add_member, make_group, make_organization, make_person, make_role

TEAMS = ("sales", "support", "finance")


def build_teams(slug: str) -> dict[str, SimpleNamespace]:
    """Organizations A and B, each with an owner, three people with a team role and one group per team."""
    sides = {}
    for side in ("a", "b"):
        owner = make_person(email=f"owner-{side}@{slug}.example.test")
        organization = make_organization(owner=owner, slug=f"{slug}-{side}", name=f"Teams {side.upper()}")
        people = {"owner": owner}
        groups = {}
        for team in TEAMS:
            make_role(organization=organization, code=team, name=team.title())
            person = make_person(email=f"{team}-{side}@{slug}.example.test", first_name=team.title())
            add_member(organization=organization, user=person)
            add_member(organization=organization, user=person, role_code=team)
            people[team] = person
            # The finance group also holds the owner; support is created by the fixture, not by the owner.
            members = [person, owner] if team == "finance" else [person]
            admin = None if team == "support" else owner
            groups[team] = make_group(
                organization=organization, admin=admin, name=f"{team.title()} team", members=members
            )
        sides[side] = SimpleNamespace(organization=organization, people=people, groups=groups)
    return sides
