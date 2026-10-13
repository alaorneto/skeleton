"""Exercise rendered Compose commands and production settings, without a Docker daemon."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from threading import Thread
import time
from wsgiref.simple_server import make_server, WSGIRequestHandler

import pytest
from django.test import RequestFactory, override_settings

ROOT = Path(__file__).resolve().parents[2]
PYTHON = str(ROOT / '.venv/bin/python')


@pytest.fixture
def composition():
    env = dict(os.environ, POSTGRES_PASSWORD='disposable-pg-password', REDIS_PASSWORD='disposable-redis-password',
               DJANGO_SECRET_KEY='disposable-settings-secret', DJANGO_ALLOWED_HOSTS='production.example.test',
               DATABASE_URL='postgres://skeleton_runtime:fixture@127.0.0.1:5433/disposable',
               CACHE_URL='rediscache://:fixture@127.0.0.1:6380/0', EMAIL_URL='smtp://localhost:2525',
               DEFAULT_FROM_EMAIL='fixture@example.test', MFA_ENCRYPTION_KEYS='dV6nwLEp9c3A8-yiLskMRFbJaUMoXXJ-_SNkrulBj8I=',
               KEYCLOAK_ADMIN_PASSWORD='fixture-only', DJANGO_TRUST_PROXY='true',
               DJANGO_CSRF_TRUSTED_ORIGINS='https://frontend.example.test', DJANGO_HEALTH_HOST='production.example.test')
    result = subprocess.run(['docker', 'compose', 'config', '--format', 'json'], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    return json.loads(result.stdout), env


def test_rd07_rendered_redis_health_authenticates_real_server(composition, tmp_path):
    config, env = composition
    server = shutil.which('redis-server') or str(ROOT / '.private/runtime/redis/redis-8.8.3/src/redis-server')
    cli = shutil.which('redis-cli') or str(ROOT / '.private/runtime/redis/redis-8.8.3/src/redis-cli')
    socket_path = str(tmp_path / 'redis.sock')
    process = subprocess.Popen([server, '--port', '0', '--unixsocket', socket_path, '--save', '', '--appendonly', 'no',
                                '--requirepass', env['REDIS_PASSWORD']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not Path(socket_path).exists():
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail('Redis fixture failed to start.')
            time.sleep(.01)
        command = config['services']['redis']['healthcheck']['test'][1].replace('$$', '$')
        command = command.replace('redis-cli', f'{cli} -s {socket_path}')
        result = subprocess.run(['/bin/sh', '-c', command], env=env, capture_output=True, text=True)
        assert result.returncode == 0, 'Rendered Redis healthcheck could not authenticate.'
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_rd08_compose_forwarding_enables_https_and_trusted_csrf_origin(composition):
    config, env = composition
    env.update(config['services']['web']['environment'])
    for key in ('DJANGO_TRUST_PROXY', 'DJANGO_CSRF_TRUSTED_ORIGINS'):
        if key not in config['services']['web']['environment']:
            env.pop(key, None)
    code = """
import django; django.setup()
from django.http import HttpResponse
from django.test import RequestFactory
from django.middleware.security import SecurityMiddleware
from django.middleware.csrf import CsrfViewMiddleware, get_token
r=RequestFactory().get('/profile/', HTTP_HOST='production.example.test',HTTP_X_FORWARDED_PROTO='https')
assert SecurityMiddleware(lambda r:HttpResponse('ok'))(r).status_code==200
r=RequestFactory().post('/profile/', HTTP_HOST='production.example.test',HTTP_X_FORWARDED_PROTO='https',HTTP_ORIGIN='https://frontend.example.test')
token=get_token(r);r.COOKIES['csrftoken']=r.META['CSRF_COOKIE'];r.META['HTTP_X_CSRFTOKEN']=token
assert CsrfViewMiddleware(lambda r:HttpResponse('ok')).process_view(r, lambda r:HttpResponse('ok'), (), {}) is None
"""
    result = subprocess.run([PYTHON, '-c', code], env=env, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_rd09_rendered_web_health_uses_permitted_production_host(composition):
    from django.core.wsgi import get_wsgi_application
    config, env = composition
    class Quiet(WSGIRequestHandler):
        def log_message(self, *args):
            pass
    with override_settings(ALLOWED_HOSTS=['production.example.test']):
        server = make_server('127.0.0.1', 8000, get_wsgi_application(), handler_class=Quiet)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            command = config['services']['web']['healthcheck']['test'][1:]
            assert command[0] == 'python'
            result = subprocess.run([PYTHON, *command[1:]], env=env, capture_output=True, text=True)
            assert result.returncode == 0, 'Rendered web healthcheck did not use an allowed Host.'
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_sbr09_proxy_configuration_keeps_client_ip_buckets_distinct_and_default_safe():
    import environ
    from skeleton.api.proxy import proxy_settings
    from allauth.core.internal.httpkit import get_client_ip
    env = environ.Env()
    configured = proxy_settings(env, values={'DJANGO_TRUST_PROXY':'true', 'DJANGO_TRUSTED_PROXY_COUNT':'1'})
    with override_settings(**configured):
        a = RequestFactory().get('/',REMOTE_ADDR='192.0.2.10',HTTP_X_FORWARDED_FOR='198.51.100.1')
        b = RequestFactory().get('/',REMOTE_ADDR='192.0.2.10',HTTP_X_FORWARDED_FOR='198.51.100.2')
        assert get_client_ip(a) == '198.51.100.1'
        assert get_client_ip(b) == '198.51.100.2'
    with override_settings(**proxy_settings(env, values={})):
        assert get_client_ip(a) == '192.0.2.10'
    from django.core.exceptions import ImproperlyConfigured
    with pytest.raises(ImproperlyConfigured):
        proxy_settings(env, values={'DJANGO_TRUSTED_PROXY_COUNT':'1'})
