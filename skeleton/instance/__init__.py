"""Instance configuration kept in a dedicated SQLite file, never application data (#143).

The settings read this store before Django exists, so nothing here imports Django models
or uses ``DATABASES``: the store is the standard library ``sqlite3``, the catalog of keys
lives in code (``schema.py``), secrets are Fernet tokens whose master key sits in a separate
file (``crypto.py``), and ``resolve.py`` layers environment > store > default.
"""
