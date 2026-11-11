"""Quota form of the platform page: MiB to bytes and the three modes (#61)."""

from skeleton.web.platform.forms import StorageQuotaForm


def test_limited_quota_converts_mebibytes_to_bytes():
    form = StorageQuotaForm({"mode": "limited", "quota_mib": "5"})
    assert form.is_valid() and form.quota_bytes() == 5 * 1024 * 1024


def test_unlimited_and_default_have_no_limit_and_limited_needs_one():
    unlimited = StorageQuotaForm({"mode": "unlimited", "quota_mib": "9"})
    assert unlimited.is_valid() and unlimited.quota_bytes() is None
    assert StorageQuotaForm({"mode": "default"}).is_valid()
    assert not StorageQuotaForm({"mode": "limited"}).is_valid()
    assert not StorageQuotaForm({"mode": "limited", "quota_mib": "-1"}).is_valid()
    assert not StorageQuotaForm({"mode": "billing"}).is_valid()
