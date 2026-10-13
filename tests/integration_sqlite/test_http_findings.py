import io
import json
import logging
from uuid import UUID, uuid4

import pytest
from django.db import connection, OperationalError
from django.test import Client, override_settings

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.logging import SecurityJSONFormatter

pytestmark = pytest.mark.usefixtures('skeleton_db')
PASSWORD = 'Http-findings-password!72'


def logged_client():
    actor, organization = bootstrap_identity(email='http-findings@example.test', organization='http-findings', name='HTTP', password=PASSWORD)
    client = Client(raise_request_exception=False)
    assert client.post('/accounts/login/', {'login': actor.email, 'password': PASSWORD}).status_code == 302
    return client, actor, organization


def test_rd02_clickjacking_header_on_own_html_pages():
    client, actor, organization = logged_client()
    assert Client().get('/accounts/login/').get('X-Frame-Options') == 'DENY'
    for path in ['/profile/', f'/o/{organization.pk}/', f'/o/{organization.pk}/apps/example_notes/configuration/']:
        response = client.get(path)
        assert response.status_code == 200
        assert response.get('X-Frame-Options') == 'DENY'


@pytest.mark.parametrize('path,status', [('/health/live/', 200), ('/health/ready/', 503)])
def test_sbr07_exact_health_probes_survive_database_unavailability(monkeypatch, path, status):
    connection.close()
    def offline():
        raise OperationalError('Database offline secret-sentinel')
    monkeypatch.setattr(connection, 'ensure_connection', offline)
    client = Client(raise_request_exception=False)
    client.cookies['sessionid'] = 'stale-session-that-must-not-touch-db'
    response = client.get(path)
    assert response.status_code == status
    assert response.json()['status'] == ('ok' if status == 200 else 'unavailable')
    assert 'secret-sentinel' not in response.content.decode()
    assert client.get('/api/v1/me/').status_code >= 400
    assert client.get('/health/live/business/').status_code != 200


def test_rd10_oversized_json_returns_413_without_partial_profile_write():
    client, actor, organization = logged_client()
    before = client.get('/api/v1/me/')
    with override_settings(SKELETON_JSON_MAX_BYTES=128):
        response = client.patch('/api/v1/me/', json.dumps({'first_name': 'x' * 200}), content_type='application/json', HTTP_IF_MATCH=before['ETag'])
        assert response.status_code == 413
        assert response.json()['code'] == 'request_too_large'
        assert client.get('/api/v1/me/').json() == before.json()
        valid = client.patch('/api/v1/me/', '{"first_name":"Valid"}', content_type='application/json', HTTP_IF_MATCH=before['ETag'])
        assert valid.status_code == 200 and valid.json()['first_name'] == 'Valid'


def test_rd10_json_stream_without_length_is_bounded():
    from skeleton.api.parsers import BoundedJSONParser
    from skeleton.api.errors import BoundaryError
    stream = io.BytesIO(b'{"value":"' + b'x' * 10000 + b'"}')
    with override_settings(SKELETON_JSON_MAX_BYTES=128), pytest.raises(BoundaryError) as caught:
        BoundedJSONParser().parse(stream)
    assert caught.value.status_code == 413
    assert stream.tell() == 129


def test_sbr15_django_status_exception_class_and_target_are_safe():
    target = uuid4()
    try:
        raise OSError('private-password-token-link-sentinel')
    except OSError:
        import sys
        record = logging.LogRecord('django.request', logging.ERROR, __file__, 1, 'secret-message-sentinel', (), sys.exc_info())
    record.status_code = 503
    record.target_sysid = str(target)
    payload = json.loads(SecurityJSONFormatter().format(record))
    assert payload['status'] == 503
    assert payload['error_class'] == 'OSError'
    assert payload['target_sysid'] == str(target)
    assert 'sentinel' not in json.dumps(payload)


@pytest.mark.parametrize('status,outcome', [(400,'validation_error'), (403,'access_denied'), (409,'conflict'), (500,'server_error')])
def test_sbr16_completion_classifies_actual_result_once(status, outcome, caplog):
    from skeleton.common.http_logging import RequestCompletionMiddleware
    from django.http import HttpResponse
    from django.test import RequestFactory
    request = RequestFactory().get('/not-a-secret-url')
    response = RequestCompletionMiddleware(lambda request: HttpResponse(status=status))(request)
    completed = [record for record in caplog.records if getattr(record, 'event', None) == 'http.request']
    assert len(completed) == 1
    payload = json.loads(SecurityJSONFormatter().format(completed[0]))
    assert payload['status'] == status and payload['outcome'] == outcome
    assert UUID(payload['request_id']) == UUID(response['X-Request-ID'])
    assert not any(getattr(record, 'event', None) == 'access.denied' for record in caplog.records)
