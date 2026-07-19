from django.db import migrations


class Migration(migrations.Migration):
    """Migration 0008 renamed categorie_article.nom to categories_articles in
    Django's model state, but the column was never actually renamed at the
    SQL level on this database (same drift pattern as 0010, fixed in 0011).
    """

    dependencies = [
        ("catalogue", "0011_fix_categorie_articles_column"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "ALTER TABLE catalogue_categorie_article "
                "RENAME COLUMN nom TO categories_articles;",
            ],
            reverse_sql=[
                "ALTER TABLE catalogue_categorie_article "
                "RENAME COLUMN categories_articles TO nom;",
            ],
            state_operations=[],
        ),
    ]
