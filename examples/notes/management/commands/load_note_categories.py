"""Operator command: load the global category catalog of the example app (#576).

The catalog is a global table of the app (``SystemRecord``, scope ``global``): it is written only
here, inside ``command_context``, the public system context of an app's management command. The
writes are audited with ``source=system:command:example_notes:load_note_categories``.
"""

from django.core.management.base import BaseCommand, CommandError

from skeleton.tenancy.context import command_context

from ...models import NoteCategory

REASON = "example_notes:load_note_categories"


def parse_entry(value):
    code, separator, title = value.partition("=")
    code, title = code.strip(), title.strip()
    if not separator or not code or not title:
        raise CommandError(f"Use CODE=Title, not {value!r}.")
    return code, title


class Command(BaseCommand):
    help = "Create or retitle categories of the global catalog: CODE=Title ..."

    def add_arguments(self, parser):
        parser.add_argument("entries", nargs="+", metavar="CODE=Title")

    def handle(self, *args, **options):
        entries = dict(parse_entry(value) for value in options["entries"])
        created = updated = 0
        with command_context(reason=REASON):
            for code, title in sorted(entries.items()):
                category = NoteCategory.objects.filter(code=code).first()
                if category is None:
                    NoteCategory(code=code, title=title).save()
                    created += 1
                elif category.title != title:
                    category.title = title
                    category.save(update_fields=["title"])
                    updated += 1
        self.stdout.write(f"{created} created, {updated} updated.")
