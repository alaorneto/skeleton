"""``manage.py appearance show`` and ``reset-theme`` (#592, #595): the console of the instance brand.

``show`` prints the product name, the footer, the active theme and which images exist (yes/no),
never a storage key. ``reset-theme`` is the emergency exit: it goes back to the core theme,
without an actor, in a ``system_context`` with the reason ``appearance.reset_theme``.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

REASON = "appearance.reset_theme"


class Command(BaseCommand):
    help = "Mostra a marca da instância ou volta ao tema do núcleo."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("show", "reset-theme"))

    def handle(self, *args, **options):
        if options["action"] == "reset-theme":
            return self._reset()
        return self._show()

    def _show(self):
        from skeleton.appearance.services import current_brand

        brand = current_brand()
        theme = "núcleo"
        if brand is not None and brand.active_theme_id:
            active = brand.active_theme
            theme = f"personalizado: {active.name}" if active is not None else "núcleo"
        elif brand is not None and brand.builtin_theme:
            theme = f"pronto: {brand.builtin_theme}"
        yes = {True: "sim", False: "não"}
        lines = [
            f"Nome do produto: {(brand.product_name if brand else '') or 'Skeleton (padrão)'}",
            f"Rodapé: {(brand.footer_text if brand else '') or '(vazio)'}",
            f"Tema ativo: {theme}",
            f"Logo: {yes[bool(brand and brand.logo_key)]}",
            f"Logo para o modo escuro: {yes[bool(brand and brand.logo_dark_key)]}",
            f"Ícone: {yes[bool(brand and brand.icon_key)]}",
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
