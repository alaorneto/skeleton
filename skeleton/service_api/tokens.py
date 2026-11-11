"""Feed tokens for apps (#76): subscriptions that calendar clients fetch without a session.

A feed token is an API token of ``kind=feed`` whose scopes are named feed scopes registered in
code (``register_token_scope(..., feed=True)``). Calendar clients cannot send headers, so the
token travels in the URL path; it authorizes only the feed views that check its scope, never
``/api/v1/``. ``FeedTokenService.issue`` returns the text once; the person lists and revokes
feed tokens at ``/profile/tokens/``. :func:`authenticate_feed` verifies a token for one scope,
with the same rate limits as Bearer tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied, ValidationError

from .base import ServiceAPI, service_method
from .errors import Forbidden, InvalidInput


@dataclass(frozen=True, slots=True)
class IssuedFeedToken:
    sysid: UUID
    prefix: str
    scope: str
    expires_at: datetime
    text: str


class FeedTokenService(ServiceAPI):
    """Issue feed tokens of the actor for a named feed scope."""

    @service_method(read_only=False, since="0.5.0", tenant=False)
    def issue(self, *, actor: AbstractBaseUser, name: str, scope: str, expires_at: datetime) -> IssuedFeedToken:
        """Issue a feed token of the actor for ``scope``; the text is returned only here.

        Autorização: a própria pessoa autenticada (contas de serviço não têm feed); política de MFA.
        Erros: InvalidInput para escopo que não seja de feed, nome ou validade inválidos;
        Forbidden sem MFA exigida pela política ou quando a requisição vem de um token.
        Efeitos: grava o token (só o digest do segredo) na transação do chamador.
        """
        from skeleton.integrations.tokens.services import issue_personal_token

        try:
            issued = issue_personal_token(actor=actor, name=name, scopes=[scope], expires_at=expires_at, feed=True)
        except ValidationError as exc:
            raise InvalidInput(exc.message_dict if hasattr(exc, "message_dict") else {"scope": exc.messages}) from None
        except PermissionDenied:
            raise Forbidden() from None
        token = issued.token
        return IssuedFeedToken(token.pk, token.prefix, scope, token.expires_at, issued.text)


def authenticate_feed(request: Any, text: str, *, scope: str) -> Any | None:
    """The user of a live feed token carrying ``scope``, or ``None`` (rate limited per token and IP).

    Unknown, expired, revoked or wrong-scope tokens, and tokens of an inactive user or with a
    changed ``auth_version``, all return ``None``: the caller answers 404. Over the limits the
    caller gets ``None`` too, so the feed never tells a guess apart from a valid token.
    """
    from django.conf import settings

    from skeleton.common.ratelimit import hit, peek
    from skeleton.integrations.tokens.middleware import WINDOW_S, client_ip
    from skeleton.integrations.tokens.services import verify_token

    failures = f"token-failure:{client_ip(request)}"
    limit = int(getattr(settings, "SKELETON_API_TOKEN_FAILURES_PER_MINUTE", 20))
    if peek(failures, window_s=WINDOW_S) >= limit:
        return None
    token = verify_token(text)
    if token is None or token.kind != "feed" or scope not in token.scopes:
        hit(failures, limit=0, window_s=WINDOW_S)
        return None
    rate = int(getattr(settings, "SKELETON_API_TOKEN_RATE_PER_MINUTE", 600))
    if not hit(f"token:{token.pk}", limit=rate, window_s=WINDOW_S).allowed:
        return None
    from skeleton.integrations.tokens.activity import touch

    touch(token)
    return token.user
