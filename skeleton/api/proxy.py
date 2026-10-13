import re
from django.core.exceptions import ImproperlyConfigured


def proxy_settings(env, *, values=None):
    def value(name, default):
        return values.get(name, default) if values is not None else env(name, default=default)
    trusted = env.parse_value(value('DJANGO_TRUST_PROXY', 'false'), bool)
    try:
        count = int(value('DJANGO_TRUSTED_PROXY_COUNT', '0'))
    except (TypeError, ValueError):
        raise ImproperlyConfigured('Trusted proxy count must be a nonnegative integer.') from None
    header = value('DJANGO_TRUSTED_CLIENT_IP_HEADER', '') or None
    if count < 0 or (count and header) or ((count or header) and not trusted):
        raise ImproperlyConfigured('Client IP trust requires an explicit trusted proxy topology.')
    if header and (not isinstance(header, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,63}', header)):
        raise ImproperlyConfigured('Trusted client IP header must be an HTTP header name.')
    return {'ALLAUTH_TRUSTED_PROXY_COUNT': count, 'ALLAUTH_TRUSTED_CLIENT_IP_HEADER': header}
