"""Convertit Approvisionnement.fournisseur (texte) en cle etrangere vers
Fournisseur, en migrant les donnees existantes, et ajoute le lien
Fournisseur.taxes (taxes applicables par fournisseur).
"""

import django.db.models.deletion
from django.db import migrations, models


def peupler_fournisseur_fk(apps, schema_editor):
    """Cree/relie un Fournisseur pour chaque valeur texte existante."""
    Approvisionnement = apps.get_model("stock", "Approvisionnement")
    Fournisseur = apps.get_model("stock", "Fournisseur")

    cache = {}
    for appro in Approvisionnement.objects.all():
        nom = (appro.fournisseur or "").strip() or "(Non renseigné)"
        four = cache.get(nom)
        if four is None:
            four, _ = Fournisseur.objects.get_or_create(nom_fournisseur=nom)
            cache[nom] = four
        appro.fournisseur_ref = four
        appro.save(update_fields=["fournisseur_ref"])


def inverse_peupler(apps, schema_editor):
    """Retour arriere : recopie le nom du fournisseur dans le champ texte."""
    Approvisionnement = apps.get_model("stock", "Approvisionnement")
    for appro in Approvisionnement.objects.all():
        if appro.fournisseur_ref_id:
            appro.fournisseur = appro.fournisseur_ref.nom_fournisseur
            appro.save(update_fields=["fournisseur"])


class Migration(migrations.Migration):

    dependencies = [
        ("stock", "0005_fournisseur"),
    ]

    operations = [
        # 1. Taxes applicables par fournisseur.
        migrations.AddField(
            model_name="fournisseur",
            name="taxes",
            field=models.ManyToManyField(
                blank=True,
                help_text="Taxes (TVA, etc.) applicables aux approvisionnements de ce fournisseur.",
                related_name="fournisseurs",
                to="stock.taxe",
            ),
        ),
        # 2. FK temporaire (nullable) vers Fournisseur.
        migrations.AddField(
            model_name="approvisionnement",
            name="fournisseur_ref",
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="approvisionnements",
                to="stock.fournisseur",
            ),
        ),
        # 3. Migration des donnees texte -> FK.
        migrations.RunPython(peupler_fournisseur_fk, inverse_peupler),
        # 4. Suppression de l'ancien champ texte.
        migrations.RemoveField(
            model_name="approvisionnement",
            name="fournisseur",
        ),
        # 5. Renommage fournisseur_ref -> fournisseur.
        migrations.RenameField(
            model_name="approvisionnement",
            old_name="fournisseur_ref",
            new_name="fournisseur",
        ),
        # 6. Champ obligatoire (etat final du modele).
        migrations.AlterField(
            model_name="approvisionnement",
            name="fournisseur",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="approvisionnements",
                to="stock.fournisseur",
            ),
        ),
    ]
