from crispy_forms.utils import render_crispy_form
from django import template
from django.utils.safestring import mark_safe

from skeleton.web.form_engine import build_crud_helper, build_crud_layout

__all__ = ["crud_detail_fields", "crud_form_fields"]
register = template.Library()


@register.simple_tag(takes_context=True)
def crud_form_fields(context, form):
    html = render_crispy_form(form, helper=build_crud_helper(form, context.get("sections")),
                             context=context.flatten())
    # Only fixed, autoescaped core templates produce this HTML.
    return mark_safe(html)


@register.simple_tag(takes_context=True)
def crud_detail_fields(context, sections):
    html = build_crud_layout(sections, mode="read").render(None, context, template_pack="skeleton_crud")
    return mark_safe(html)
