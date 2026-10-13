"""Playwright's synchronous API owns an event loop; ORM stays in a sync thread."""
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from django.db import connections

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sso-orm')


def db_call(function):
    def invoke():
        try:
            # The fixture replaces its per-test DB; this worker reuses a thread.
            connections.close_all()
            connections['default'].settings_dict = connections.databases['default']
            return function()
        finally:
            connections.close_all()
    return _executor.submit(invoke).result()


def database_thread(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return db_call(lambda: function(*args, **kwargs))
    return wrapped
