"""First-access setup (#146): configure a fresh instance in the browser, then restart normally.

The instance starts in setup mode (``config.settings.setup``) while a required catalog key
(``secret_key``, ``allowed_hosts``, ``database_url``) has no value in the environment or in
the instance SQLite. Setup mode has no database alias, keeps its session in a signed cookie
and routes only ``/setup/`` and the health probes. Access needs a single-use token printed
once in the log and written to a 0600 file.
"""
