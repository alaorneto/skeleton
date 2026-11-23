"""Portuguese sentences of the theme package errors (#634), shared by the console and the pages.

One sentence per code of ``package_format.ERROR_CODES``, saying what to fix. ``%(path)s``,
``%(line)s`` and ``%(item)s`` come from the error; ``detail`` (technical text of the zip guards)
is never part of a message.
"""

from __future__ import annotations

from typing import Any

from django.utils.translation import gettext_lazy

__all__ = ["PACKAGE_ERROR_MESSAGES", "package_error_message"]

PACKAGE_ERROR_MESSAGES = {
    "archive": gettext_lazy(
        "O arquivo não é um zip válido de pacote de tema (links, criptografia, nomes repetidos ou caminhos inseguros)."
    ),
    "too_large": gettext_lazy("O pacote passa do tamanho máximo (4 MiB o zip, 6 MiB descompactado)."),
    "too_many_files": gettext_lazy("O pacote tem mais de 40 arquivos."),
    "unexpected_file": gettext_lazy("O arquivo %(path)s não é declarado no theme.json; tire-o do pacote ou declare-o."),
    "missing_manifest": gettext_lazy(
        "Falta o theme.json na raiz do pacote. Compacte o conteúdo da pasta, não a pasta."
    ),
    "manifest_json": gettext_lazy("O theme.json não é um objeto JSON válido (ou repete uma chave)."),
    "manifest_field": gettext_lazy("O theme.json não tem o campo %(item)s, ou ele tem o tipo errado."),
    "unknown_field": gettext_lazy("O theme.json tem o campo desconhecido %(item)s."),
    "format": gettext_lazy('Use "format": 1 no theme.json.'),
    "id": gettext_lazy(
        "O id precisa ter só letras minúsculas, números, ponto, hífen ou sublinhado (ex.: org.example.theme.aurora)."
    ),
    "version": gettext_lazy("A versão precisa ser semântica (ex.: 1.0.0)."),
    "name": gettext_lazy("O nome do tema é obrigatório, com até 80 caracteres numa linha."),
    "text_field": gettext_lazy("O campo %(item)s é longo demais ou tem quebra de linha."),
    "publisher": gettext_lazy("O publicador precisa seguir a mesma regra do id."),
    "compatibility": gettext_lazy(
        "Informe em compatibility.skeleton uma faixa de versões do núcleo (ex.: >=0.3.0,<0.4.0)."
    ),
    "brand_color": gettext_lazy("A cor da marca precisa ser #RRGGBB com contraste AA (4,5:1) com o texto branco."),
    "radius": gettext_lazy("Os cantos precisam ser sharp, default ou round."),
    "font": gettext_lazy("A fonte precisa ser default, system ou serif."),
    "font_family": gettext_lazy("tokens.font_family precisa ser a família de uma das fontes declaradas em fonts."),
    "image_path": gettext_lazy(
        "A imagem %(path)s não existe, não está em images/ ou tem extensão não aceita (PNG, JPEG, WebP ou SVG; ícone só PNG)."
    ),
    "image_type": gettext_lazy("O conteúdo de %(path)s não é PNG, JPEG nem WebP."),
    "image_size": gettext_lazy("A imagem %(path)s passa do limite (logo 512 KB, SVG e ícone 256 KB)."),
    "svg": gettext_lazy(
        "O SVG %(path)s usa algo não permitido (%(item)s). Exporte-o como SVG simples, sem scripts, estilos ou referências externas."
    ),
    "icon": gettext_lazy("O ícone %(path)s precisa ser um PNG quadrado de 512 × 512 pixels."),
    "logo_dark_without_logo": gettext_lazy("O logo para o modo escuro só vale junto com o logo."),
    "too_many_fonts": gettext_lazy("O pacote pode ter até 8 fontes."),
    "font_family_name": gettext_lazy(
        "O nome da família de fonte é inválido ou reservado (letras, números, espaço e hífen; nunca um nome genérico do CSS, como serif ou sans-serif)."
    ),
    "font_file": gettext_lazy("A fonte %(path)s precisa ser um .woff2 em fonts/, até 512 KB, declarado uma vez só."),
    "font_weight": gettext_lazy('O peso de fonte precisa ser 100 a 900 em centenas, ou uma faixa como "100 900".'),
    "font_style": gettext_lazy("O estilo de fonte precisa ser normal ou italic."),
    "css_missing": gettext_lazy("O CSS %(path)s declarado no theme.json não está no pacote."),
    "css": gettext_lazy("%(path)s, linha %(line)s: o CSS não pode usar isto (%(item)s)."),
    "incompatible_core": gettext_lazy("Este pacote não é compatível com a versão do núcleo instalada."),
    "same_version_changed": gettext_lazy("Já existe esta versão do pacote com outro conteúdo; aumente a versão."),
    "downgrade": gettext_lazy("Já está instalada uma versão mais nova deste pacote."),
    "name_taken": gettext_lazy("Já existe um tema com este nome."),
    "too_many_themes": gettext_lazy("O catálogo já tem o máximo de 50 temas."),
    "id_belongs_to_app": gettext_lazy(
        "Este id pertence a um tema da app %(item)s; um tema de app é atualizado com a própria app."
    ),
}


def package_error_message(code: str, *, path: str = "", line: int = 0, item: str = "") -> str:
    """The sentence of ``code`` with its values; an unknown code gets the generic archive sentence."""
    if code == "css":
        from .css_validation import CODES, CssProblem, css_problem_message

        problem = CssProblem(item, line) if item in CODES else CssProblem("forbidden", line, item)
        return f"{path}: {css_problem_message(problem)}"
    template: Any = PACKAGE_ERROR_MESSAGES.get(code, PACKAGE_ERROR_MESSAGES["archive"])
    return str(template) % {"path": path, "line": line, "item": item}


def describe(error: Any) -> str:
    """The sentence of a ``ThemePackageError``."""
    return package_error_message(error.code, path=error.path, line=error.line, item=error.item)
