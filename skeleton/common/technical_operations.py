from django.db.migrations.operations.base import Operation


class GrantTechnicalTablePrivileges(Operation):
    """Frozen per-table framework privileges; no blanket future-table grants."""
    reversible = True
    reduces_to_sql = True

    def __init__(self, tables):
        allowed = {"SELECT", "INSERT", "UPDATE", "DELETE"}
        self.tables = {table: list(privileges) for table, privileges in tables.items()}
        if any(not permissions or not set(permissions) <= allowed for permissions in self.tables.values()):
            raise ValueError("Technical privileges must be explicit DML actions.")

    def deconstruct(self):
        return self.__class__.__name__, [], {"tables": self.tables}

    def state_forwards(self, app_label, state):
        pass

    def _apply(self, editor, grant):
        if editor.connection.vendor != "postgresql":
            return
        q = editor.quote_name
        with editor.connection.cursor() as cursor:
            for table, privileges in self.tables.items():
                cursor.execute("SELECT to_regclass(%s)", ["skeleton." + table])
                if cursor.fetchone()[0] is None:
                    raise ValueError(f"Missing pinned technical table: {table}")
                editor.execute(f"REVOKE ALL ON TABLE skeleton.{q(table)} FROM PUBLIC, skeleton_runtime")
                if grant:
                    editor.execute(f"GRANT {','.join(privileges)} ON TABLE skeleton.{q(table)} TO skeleton_runtime")
                cursor.execute("SELECT a.attname,pg_get_serial_sequence(%s,a.attname) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='skeleton' AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped", ["skeleton." + table, table])
                for column, sequence in cursor.fetchall():
                    if sequence:
                        editor.execute(f"REVOKE ALL ON SEQUENCE {sequence} FROM PUBLIC, skeleton_runtime")
                        if grant and "INSERT" in privileges:
                            editor.execute(f"GRANT USAGE,SELECT ON SEQUENCE {sequence} TO skeleton_runtime")

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self._apply(schema_editor, True)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self._apply(schema_editor, False)

    def describe(self):
        return "Install explicit framework identity/session privileges for runtime"
