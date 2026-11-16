"""Minimal host project shipped in the wheel (#518).

An app developed in its own repository has no ``config`` package to run its suite or
``manage.py`` against: ``DJANGO_SETTINGS_MODULE=skeleton.testing.host.settings`` stands in for it.
Tests only; never a production entrypoint.
"""
