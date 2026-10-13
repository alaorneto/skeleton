"""Real WSGI/browser fixtures, without pytest-django's flush-based live_server."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socketserver
from threading import Thread
from urllib.request import urlopen
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import pytest
from django.core.wsgi import get_wsgi_application
from django.db import connections
from django.test import override_settings


@dataclass
class SSOFixture:
    origin: str
    providers: dict
    organizations: dict
    administrator: object


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        # Callback queries contain OAuth codes/state: never emit request URLs.
        pass


class ThreadedServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True


@pytest.fixture
def real_sso(skeleton_db, monkeypatch):
    from django.core.cache import cache
    from skeleton.accounts.providers import load_provider_config
    from skeleton.accounts.services import create_identity, create_organization
    from skeleton.tenancy.context import system_context
    providers_path = os.environ.get('SSO_PROVIDER_CONFIG_PATH')
    environment_path = os.environ.get('SSO_FIXTURE_ENV_PATH')
    if not providers_path or not environment_path:
        raise pytest.UsageError('Run tools/prepare_sso_fixtures.py; set SSO_PROVIDER_CONFIG_PATH and SSO_FIXTURE_ENV_PATH to its private JSON outputs.')
    for key,value in json.loads(Path(environment_path).read_text()).items():
        if not key.startswith('SKELETON_FIXTURE_') or not isinstance(value,str):
            raise pytest.UsageError('Unexpected SSO fixture environment entry.')
        monkeypatch.setenv(key,value)
    document = json.loads(Path(providers_path).read_text())
    if {p['provider_id'] for p in document['providers']} != {'oidc-a','oidc-b','saml-a','saml-b'}:
        raise pytest.UsageError('SSO fixtures must contain both real OIDC and SAML realm bindings.')
    for realm in ('skeleton-a','skeleton-b'):
        with urlopen(f'http://127.0.0.1:8080/realms/{realm}/.well-known/openid-configuration',timeout=10) as response:
            discovery = json.load(response)
            assert discovery['issuer'] == f'http://127.0.0.1:8080/realms/{realm}'
    configuration = load_provider_config(providers_path,debug=True)
    cache.clear()
    with override_settings(DEBUG=True, ALLOWED_HOSTS=['127.0.0.1'],
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False,
        SKELETON_REQUIRE_ADMIN_MFA=False, SKELETON_PROVIDER_CONFIGURATION=configuration,
        SOCIALACCOUNT_PROVIDERS=configuration.settings):
        with system_context(reason='sso-fixture:identity'):
            admin = create_identity(email='sso-owner@example.test',password='Fixture-owner-password-2026!')
        organizations = {suffix:create_organization(owner=admin,slug=f'sso-{suffix}',name=f'SSO {suffix.upper()}') for suffix in ('a','b')}
        try:
            server = make_server('127.0.0.1',8000,get_wsgi_application(),server_class=ThreadedServer,handler_class=QuietHandler)
        except OSError as exc:
            raise pytest.UsageError('Real SSO fixture requires free loopback port 8000 matching Keycloak callbacks.') from exc
        thread = Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            yield SSOFixture('http://127.0.0.1:8000',document,organizations,admin)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            connections.close_all()


@pytest.fixture
def browser(real_sso):
    # Application/database setup completes before Playwright starts its loop.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    try:
        yield context.new_page()
    finally:
        context.close()
