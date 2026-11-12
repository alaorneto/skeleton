"""Full-text index of code and title, and the picker index of the title (#180, #181)."""

from django.db import migrations

from skeleton.common.migration_operations import InstallSearchIndex


class Migration(migrations.Migration):
    dependencies = [("example_notes", "0001_initial"), ("sk_system", "0008_search_function")]

    operations = [InstallSearchIndex("example_notes", "note", fields=["code", "title"], label="title")]
