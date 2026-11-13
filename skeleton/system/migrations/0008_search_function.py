"""``skeleton.immutable_unaccent`` for the full-text and picker indexes (#180).

Frozen here: lower-case and remove the accents of Latin letters with ``translate``, so the
search needs no extension. ``skeleton.system.search`` uses the same function name.
"""

from django.db import migrations

FUNCTION = "CREATE OR REPLACE FUNCTION skeleton.immutable_unaccent(value text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$ SELECT translate(lower(value), 'áàâãäåéèêëíìîïóòôõöúùûüçñýÿÁÀÂÃÄÅÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑÝ', 'aaaaaaeeeeiiiiooooouuuucnyyAAAAAAEEEEIIIIOOOOOUUUUCNY') $$"


class Migration(migrations.Migration):
    dependencies = [("sk_system", "0007_bundled_components")]

    operations = [
        migrations.RunSQL(FUNCTION, reverse_sql="DROP FUNCTION IF EXISTS skeleton.immutable_unaccent(text)"),
    ]
