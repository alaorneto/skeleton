"""Activation and availability are closed by default in the models too, not only in the services (#283)."""

from skeleton.system.models import AppAvailability, AppInstallation


def test_installation_is_disabled_by_default():
    assert AppInstallation._meta.get_field("enabled").default is False
    assert AppInstallation(app_label="example_notes").enabled is False


def test_availability_is_selected_by_default():
    assert AppAvailability._meta.get_field("mode").default == AppAvailability.Mode.SELECTED
    assert AppAvailability(app_label="example_notes").mode == AppAvailability.Mode.SELECTED
