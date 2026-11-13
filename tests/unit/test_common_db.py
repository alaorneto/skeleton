"""bound_lock_waits validates its value before touching the database (#295)."""

import pytest

from skeleton.common.db import bound_lock_waits


@pytest.mark.parametrize("value", [0, -1, 600_001, True, "5s", 1.5])
def test_bound_lock_waits_refuses_invalid_values(value):
    with pytest.raises(ValueError):
        bound_lock_waits(milliseconds=value)
