"""Every key of the instance catalog has one section of /platform/instance/ (#779)."""

from skeleton.instance.schema import CATALOG, SETTING_GROUPS


def test_every_key_is_in_exactly_one_known_group():
    keys = [key for _, _, members in SETTING_GROUPS for key in members]
    assert sorted(keys) == sorted(CATALOG), "a key without a section, or a section with an unknown key"
    assert len(keys) == len(set(keys)), "a key in two sections"
    groups = {group for group, _, _ in SETTING_GROUPS}
    assert all(spec.group in groups for spec in CATALOG.values())


def test_groups_have_labels_and_members():
    assert all(group and label and members for group, label, members in SETTING_GROUPS)
