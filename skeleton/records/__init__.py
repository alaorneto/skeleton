"""Generic record gateway shared by the HTML and REST surfaces (#301).

Coercion by allowlist, create/update/delete/restore, the reference picker and the dedicated
access resources' input and output. No models and no app label: plain Python above
``access`` and ``system``, below ``web``, ``api``, ``files``, ``comments`` and ``transfer``.
"""
