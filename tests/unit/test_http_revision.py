import pytest

from skeleton.api.errors import BoundaryError, parse_revision


@pytest.mark.parametrize("value", ['"1"', '"42"', '"999999"'])
def test_strong_single_revision(value):
    assert parse_revision(value) == int(value.strip('"'))


@pytest.mark.parametrize("value", ['1', '"0"', '"-1"', 'W/"1"', '*', '"1", "2"', '"01"', '', '"1-2"'])
def test_invalid_single_revision(value):
    with pytest.raises(BoundaryError) as caught:
        parse_revision(value)
    assert caught.value.status_code == 400
    assert caught.value.detail["code"] == "invalid_revision"


def test_missing_revision_requires_precondition():
    with pytest.raises(BoundaryError) as caught:
        parse_revision(None)
    assert caught.value.status_code == 428
    assert caught.value.detail["code"] == "revision_required"


def test_profile_revision_is_composite():
    assert parse_revision('"2-3"', composite=True) == (2, 3)
    with pytest.raises(BoundaryError):
        parse_revision('"2"', composite=True)


def test_uniqueness_validation_is_conflict_not_bad_input():
    from django.core.exceptions import ValidationError
    from skeleton.api.errors import validation_conflict

    assert validation_conflict(ValidationError({"code": ValidationError("Duplicate", code="unique")}))
    assert not validation_conflict(ValidationError({"title": "Required"}))
