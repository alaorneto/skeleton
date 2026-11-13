"""``manage.py instance status|show|set|unset|import-env|export-template|rotate-key`` (#143, #342).

Secrets are written encrypted, read back only by the settings, and never printed: ``show``
masks them and every subcommand reports keys and origins, not values.
"""

import os
import sys

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from skeleton.instance import crypto
from skeleton.instance.resolve import instance_paths, missing_for_setup, resolve_instance_settings
from skeleton.instance.schema import BOOTSTRAP_ENV, CATALOG, spec_for
from skeleton.instance.store import InstanceStore
from skeleton.instance.writer import write_setting

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
        unset.add_argument(
            "--force", action="store_true", help="Remove even a key the instance needs to start (recovery only)."
        )
        imported = actions.add_parser("import-env", help="Store the catalog keys present in the current environment.")
        imported.add_argument("--keep-existing", action="store_true", help="Do not replace keys already stored.")
        actions.add_parser("export-template", help="Print a commented .env template with no values.")
        rotate = actions.add_parser("rotate-key", help="Generate a new master key and re-encrypt every stored secret.")
        rotate.add_argument("--drop-old", action="store_true", help="Remove the previous keys from the key file.")

    def handle(self, *args, **options):
        database, key = instance_paths(settings.BASE_DIR)
        store = InstanceStore(database)
        handler = getattr(self, "_" + options["action"].replace("-", "_"))
        try:
            handler(store, key, options)
        except (crypto.InstanceKeyError, InvalidToken, KeyError, ValueError) as exc:
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

    def _set(self, store, key, options):
        spec = spec_for(options["key"])
        if options["stdin"] == (options["value"] is not None):
            raise CommandError("Informe o valor como argumento ou com --stdin, não os dois.")
        raw = sys.stdin.readline().rstrip("\r\n") if options["stdin"] else options["value"]
        write_setting(settings.BASE_DIR, spec.key, raw, source="cli:set")
        overridden = " (o ambiente ainda vence: " + spec.env + ")" if spec.env in os.environ else ""
        self.stdout.write(self.style.SUCCESS(f"{spec.key} gravada{overridden}."))

    def _unset(self, store, key, options):
        from skeleton.instance.writer import REQUIRED_KEY_MESSAGE, can_clear

        spec = spec_for(options["key"])
        if not options["force"] and not can_clear(spec):
            raise CommandError(REQUIRED_KEY_MESSAGE + " Use --force para remover assim mesmo.")
        removed = store.delete(spec.key, source="cli:unset")
        self.stdout.write(f"{spec.key} {'removida' if removed else 'não estava gravada'}.")

    def _import_env(self, store, key, options):
        stored = store.settings()
        imported = []
        for spec in CATALOG.values():
            if spec.env not in os.environ or (options["keep_existing"] and spec.key in stored):
                continue
            try:
                write_setting(settings.BASE_DIR, spec.key, os.environ[spec.env], source="cli:import-env")
            except ValueError as exc:
                raise CommandError(f"{spec.env}: {exc}") from None
            imported.append(spec.key)
        self.stdout.write(self.style.SUCCESS("Importadas: " + (", ".join(imported) if imported else "nenhuma") + "."))

    def _rotate_key(self, store, key, options):
        """Two steps (#342): add a new first key and re-encrypt; later, ``--drop-old`` forgets the others."""
        if not key.is_file():
            raise CommandError("Não há chave mestra para rotacionar.")
        old = crypto.read_keys(key)
        if options["drop_old"]:
            # Every secret must already be under the first key; checked before the file changes.
            current = MultiFernet([Fernet(old[0])])
            for value, secret in store.settings().values():
                if secret:
                    try:
                        current.decrypt(value.encode())
                    except InvalidToken:
                        raise CommandError(
                            "Ainda há segredos cifrados com uma chave antiga; rode rotate-key sem --drop-old primeiro."
                        ) from None
            crypto.write_keys(key, old[:1])
            self.stdout.write(self.style.SUCCESS("Chaves antigas removidas do arquivo."))
            return
        new = Fernet.generate_key()
        crypto.write_keys(key, [new, *old])  # 1) The file knows both keys: every reader keeps working.
        keyring = MultiFernet([Fernet(item) for item in (new, *old)])
        count = store.reencrypt_secrets(
            lambda token: keyring.rotate(token.encode()).decode(), source="cli:rotate-key"
        )  # 2) Every secret under the new key, in one SQLite transaction.
        self.stdout.write(self.style.SUCCESS(f"Chave mestra rotacionada; {count} segredos recifrados."))
        self.stdout.write(
            "Faça backup do novo arquivo de chave, reinicie a instância e depois rode: instance rotate-key --drop-old"
        )

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
