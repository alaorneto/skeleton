"""The core as OIDC provider for native clients (#603), on top of ``allauth.idp.oidc``.

Nothing here implements OAuth or OIDC: allauth runs the protocol (authorization code, PKCE
verification, token issue and rotation, revocation, discovery, JWKS). This package adds the
policy of the core around it: the registry of native clients (``NativeClient``), PKCE ``S256``
only, loopback redirects only, the two grants of a native client, the canonical issuer, the MFA
gate of the authorization and the binding of every token to the person's ``auth_version``.
"""
