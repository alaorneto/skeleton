"""``manage.py instance status|show|set|unset|import-env|export-template`` (#143).

Secrets are written encrypted, read back only by the settings, and never printed: ``show``
masks them and every subcommand reports keys and origins, not values.
"""

import os
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from skeleton.instance import crypto
from skeleton.instance.resolve import instance_paths, missing_for_setup, resolve_instance_settings
from skeleton.instance.schema import BOOTSTRAP_ENV, CATALOG, spec_for
from skeleton.instance.store import InstanceStore

MASK = "****** (definido)"


class Command(BaseCommand):
    help = "Inspect and change the instance configuration stored in its own SQLite (secrets encrypted)."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)
        actions.add_parser("status", help="Paths, schema version and keys still missing for the setup.")
        actions.add_parser("show", help="Every catalog key with its effective origin; secrets masked.")
        put = actions.add_parser("set", help="Store a catalog key (validated; secrets encrypted).")
        put.add_argument("key", help="Catalog key or its environment variable name.")
        put.add_argument("value", nargs="?", help="Omit with --stdin to keep the value out of the shell history.")
        put.add_argument("--stdin", action="store_true", help="Read the value from standard input.")
        unset = actions.add_parser("unset", help="Remove a stored key (the default or the environment applies).")
        unset.add_argument("key")
        imported = actions.add_parser("import-env", help="Store the catalog keys present in the current environment.")
        imported.add_argument("--keep-existing", action="store_true", help="Do not replace keys already stored.")
        actions.add_parser("export-template", help="Print a commented .env template with no values.")

    def handle(self, *args, **options):
        database, key = instance_paths(settings.BASE_DIR)
        store = InstanceStore(database)
        handler = getattr(self, "_" + options["action"].replace("-", "_"))
        try:
            handler(store, key, options)
        except (crypto.InstanceKeyError, KeyError, ValueError) as exc:
            raise CommandError(str(exc.args[0] if exc.args else exc)) from None

    # ----------------------------------------------------------------- subcommands

    def _status(self, store, key, options):
        self.stdout.write(f"SQLite da instância: {store.path} ({'existe' if store.exists() else 'ausente'})")
        self.stdout.write(f"Chave mestra: {key} ({'existe' if key.is_file() else 'ausente'})")
        self.stdout.write(f"Versão do schema: {store.schema_version() or '-'}")
        self.stdout.write(f"Chaves gravadas: {len(store.settings())} de {len(CATALOG)}")
        missing = missing_for_setup(settings.BASE_DIR)
        self.stdout.write("Faltam para o setup: " + (", ".join(missing) if missing else "nenhuma"))

    def _show(self, store, key, options):
        for item in resolve_instance_settings(settings.BASE_DIR):
            if item.origin == "default":
                shown = "(padrão)"
            elif item.secret:
                shown = MASK
            else:
                shown = item.value
            self.stdout.write(f"{item.key} [{item.env}] = {shown}  <{item.origin}>")

    def _write(self, store, key_path, spec, raw, *, source):
        value = spec.clean(raw)
        if spec.secret:
            value = crypto.encrypt(crypto.load_key(key_path, create=True), value)
        store.put(spec.key, value, secret=spec.secret, source=source)

    def _set(self, store, key, options):
        spec = spec_for(options["key"])
        if options["stdin"] == (options["value"] is not None):
            raise CommandError("Informe o valor como argumento ou com --stdin, não os dois.")
        raw = sys.stdin.readline().rstrip("\r\n") if options["stdin"] else options["value"]
        self._write(store, key, spec, raw, source="cli:set")
        overridden = " (o ambiente ainda vence: " + spec.env + ")" if spec.env in os.environ else ""
        self.stdout.write(self.style.SUCCESS(f"{spec.key} gravada{overridden}."))

    def _unset(self, store, key, options):
        spec = spec_for(options["key"])
        removed = store.delete(spec.key, source="cli:unset")
        self.stdout.write(f"{spec.key} {'removida' if removed else 'não estava gravada'}.")

    def _import_env(self, store, key, options):
        stored = store.settings()
        imported = []
        for spec in CATALOG.values():
            if spec.env not in os.environ or (options["keep_existing"] and spec.key in stored):
                continue
            try:
                self._write(store, key, spec, os.environ[spec.env], source="cli:import-env")
            except ValueError as exc:
                raise CommandError(f"{spec.env}: {exc}") from None
            imported.append(spec.key)
        self.stdout.write(self.style.SUCCESS("Importadas: " + (", ".join(imported) if imported else "nenhuma") + "."))

    def _export_template(self, store, key, options):
        self.stdout.write("# Variáveis de bootstrap (somente no ambiente):")
        for name in BOOTSTRAP_ENV:
            self.stdout.write(f"# {name}=")
        self.stdout.write("")
        self.stdout.write("# Sobrescritas opcionais; sem elas vale o SQLite da instância (`manage.py instance show`).")
        for spec in CATALOG.values():
            marker = " (segredo)" if spec.secret else ""
            self.stdout.write(f"# {spec.description}{marker}")
            self.stdout.write(f"# {spec.env}=")
