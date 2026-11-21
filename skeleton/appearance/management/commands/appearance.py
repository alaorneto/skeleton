"""``manage.py appearance show`` (#592): the console of the instance brand.

``show`` prints the product name, the footer, the active theme and which images exist (yes/no),
never a storage key.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Mostra a marca da instância ou volta ao tema do núcleo."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("show",))

    def handle(self, *args, **options):
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
