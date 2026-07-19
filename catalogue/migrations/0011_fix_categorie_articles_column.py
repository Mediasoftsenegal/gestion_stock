from django.db import migrations


class Migration(migrations.Migration):
    """Migration 0010 changed article.categorie_articles to a ForeignKey in
    Django's model state, but on this database the column was never actually
    converted at the SQL level: it is still a `varchar(100) NOT NULL`
    containing leftover free-text values ('', 'NULL', 'VIN', ...) from before
    the categorie_article table existed, with no FK constraint.

    This migration brings the real schema in line with the model state:
    clear the invalid legacy values, convert the column to a nullable
    integer, and add the missing foreign key constraint.
    """

    dependencies = [
        ("catalogue", "0010_alter_article_categorie_articles"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "ALTER TABLE catalogue_article MODIFY categorie_articles_id varchar(100) NULL;",
                "UPDATE catalogue_article SET categorie_articles_id = NULL "
                "WHERE categorie_articles_id NOT REGEXP '^[0-9]+$';",
                "ALTER TABLE catalogue_article MODIFY categorie_articles_id bigint NULL;",
                "ALTER TABLE catalogue_article "
                "ADD CONSTRAINT catalogue_article_categorie_articles_id_fk "
                "FOREIGN KEY (categorie_articles_id) REFERENCES catalogue_categorie_article (id) "
                "ON DELETE SET NULL;",
            ],
            reverse_sql=[
                "ALTER TABLE catalogue_article DROP FOREIGN KEY catalogue_article_categorie_articles_id_fk;",
                "ALTER TABLE catalogue_article MODIFY categorie_articles_id varchar(100) NULL;",
            ],
            state_operations=[],
        ),
    ]
