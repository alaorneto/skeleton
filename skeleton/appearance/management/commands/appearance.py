"""``manage.py appearance``: the console of the instance brand and of theme packages.

- ``show`` (#592) prints the product name, the footer, the active theme and which images exist
  (yes/no), never a storage key.
- ``reset-theme`` (#595) is the emergency exit: back to the core theme, without an actor, in a
  ``system_context`` with the reason ``appearance.reset_theme``.
- ``check-package`` and ``build-package`` (#634) are for theme authors: they read a directory or
  a zip, check it like the installation does (format, AA contrast, core compatibility) and build
  a deterministic zip. They never open the database.
"""

import os
import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

REASON = "appearance.reset_theme"


class Command(BaseCommand):
    help = "Marca da instância, saída de emergência do tema e pacotes de tema."

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)
        actions.add_parser("show", help="Mostra a marca e o tema ativo.")
        actions.add_parser("reset-theme", help="Volta ao tema do núcleo.")
        check = actions.add_parser("check-package", help="Confere um pacote de tema (diretório ou .zip).")
        check.add_argument("source")
        build = actions.add_parser("build-package", help="Confere um diretório e monta o .zip do pacote.")
        build.add_argument("source")
        build.add_argument("--output", required=True)
        build.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "reset-theme":
            return self._reset()
        if action == "check-package":
            return self._check_package(options["source"])
        if action == "build-package":
            return self._build_package(options["source"], options["output"], force=options["force"])
        return self._show()

    # ----------------------------------------------------------------------- brand

    def _show(self):
        from skeleton.appearance.services import current_brand
        from skeleton.appearance.snapshot import _load

        brand = current_brand()
        appearance = _load()  # Straight from the database, never the cache of this process.
        theme = "núcleo"
        if brand is not None and brand.active_theme_id:
            active = brand.active_theme
            if active is not None and active.source == "package":
                theme = f"pacote: {active.name} ({active.package_id} {active.package_version})"
            else:
                theme = f"personalizado: {active.name}" if active is not None else "núcleo"
        elif brand is not None and brand.builtin_theme:
            theme = f"pronto: {brand.builtin_theme}"
        origin = {"brand": "sim (enviado)", "theme": "sim (do tema)", "": "não"}

        def text(own, value, empty):
            if not value:
                return empty
            return value if own else f"{value} (do tema)"

        dark = origin[appearance.logo_source] if appearance.logo_dark_version else "não"
        lines = [
            f"Nome do produto: {text(brand and brand.product_name, appearance.product_name, 'Skeleton (padrão)')}",
            f"Rodapé: {text(brand and brand.footer_text, appearance.footer_text, '(vazio)')}",
            f"Tema ativo: {theme}",
            f"Logo: {origin[appearance.logo_source]}",
            f"Logo para o modo escuro: {dark}",
            f"Ícone: {origin[appearance.icon_source]}",
        ]
        self.stdout.write("\n".join(lines))

    def _reset(self):
        from skeleton.appearance.services import activate_theme, current_brand, revision_of
        from skeleton.appearance.snapshot import reset_appearance_cache
        from skeleton.tenancy.context import system_context

        with system_context(reason=REASON), transaction.atomic():
            activate_theme(actor=None, expected_revision=revision_of(current_brand()))
        reset_appearance_cache()
        self.stdout.write(self.style.SUCCESS("Tema do núcleo restaurado."))

    # ----------------------------------------------------------------------- packages (authors)

    def _read(self, source):
        """``(package, files)`` of a directory or a zip, checked like the installation does."""
        from django.core.exceptions import ValidationError

        from skeleton.appearance.package_format import (
            MAX_ARCHIVE_BYTES,
            ThemePackageError,
            archive_files,
            check_core_compatibility,
            directory_files,
            from_files,
        )
        from skeleton.appearance.package_messages import describe, package_error_message
        from skeleton.tenancy.branding import validate_brand_color
        from skeleton.versioning import core_version

        path = Path(source)
        try:
            if path.is_dir():
                files = directory_files(path)
            elif path.is_file():
                if path.stat().st_size > MAX_ARCHIVE_BYTES:
                    raise ThemePackageError("too_large")
                files = archive_files(path.read_bytes())
            else:
                raise CommandError(f"{source} não existe.")
            package = from_files(files)
            check_core_compatibility(package, core_version()["version"])
        except ThemePackageError as error:
            raise CommandError(f"ERRO {error.code}: {describe(error)}") from error
        if package.brand_color:
            try:
                validate_brand_color(package.brand_color)
            except ValidationError as error:
                message = package_error_message("brand_color")
                raise CommandError(f"ERRO brand_color: {message}") from error
        return package, files

    def _summary(self, package):
        kinds = {"logo": "logo", "logo_dark": "logo escuro", "icon": "ícone"}
        images = ", ".join(kinds[image.kind] for image in package.images) or "nenhuma"
        return "\n".join(
            [
                f"Pacote: {package.id} {package.version}",
                f"Nome: {package.name}",
                f"Cor: {package.brand_color or '(do núcleo)'} · cantos: {package.radius} · fonte: "
                f"{package.font_family or package.font}",
                f"Imagens: {images} · fontes: {len(package.fonts)} · CSS: {len(package.custom_css.encode())} bytes",
                f"Nome do produto: {package.product_name or '(não define)'}",
                f"Digest: {package.content_digest}",
            ]
        )

    def _check_package(self, source):
        package, _files = self._read(source)
        self.stdout.write(self._summary(package))
        self.stdout.write(self.style.SUCCESS("Pacote válido."))

    def _build_package(self, source, output, *, force):
        from skeleton.appearance.package_build import build_theme_zip, package_files
        from skeleton.appearance.package_format import read_theme_archive

        root, target = Path(source), Path(output)
        if not root.is_dir():
            raise CommandError("build-package recebe um diretório.")
        if target.resolve().is_relative_to(root.resolve()):
            raise CommandError("O arquivo de saída não pode ficar dentro do diretório do pacote.")
        if target.exists() and not force:
            raise CommandError(f"{output} já existe; use --force para substituir.")
        package, files = self._read(root)
        data = build_theme_zip(package_files(package, files))
        if read_theme_archive(data).content_digest != package.content_digest:  # Defence against a build bug.
            raise CommandError("O zip montado não confere com o diretório.")
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".theme-", suffix=".zip")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        self.stdout.write(f"{target} · {len(data)} bytes · {package.content_digest}")
        self.stdout.write(self.style.SUCCESS("Pacote montado."))
