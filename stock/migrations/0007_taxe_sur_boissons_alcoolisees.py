"""Ajoute Taxe.sur_boissons_alcoolisees (assiette « boissons alcoolisees »,
type TBA) et coche automatiquement les taxes deja nommees TBA.
"""

import unicodedata

from django.db import migrations, models


def _normaliser(txt):
    return (
        unicodedata.normalize("NFD", txt or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def cocher_tba_existantes(apps, schema_editor):
    Taxe = apps.get_model("stock", "Taxe")
    for taxe in Taxe.objects.all():
        norm = _normaliser(taxe.type_taxe)
        compact = norm.replace(".", "").replace(" ", "")
        est_alcool = (
            "alco" in norm and "non alco" not in norm and "sans alco" not in norm
        )
        if "tba" in compact or est_alcool:
            taxe.sur_boissons_alcoolisees = True
            taxe.save(update_fields=["sur_boissons_alcoolisees"])


def inverse(apps, schema_editor):
    # Rien a defaire : le champ est supprime par la migration inverse du schema.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("stock", "0006_fournisseur_taxes_and_fk"),
    ]

    operations = [
        migrations.AddField(
            model_name="taxe",
            name="sur_boissons_alcoolisees",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Ex. TBA : la taxe ne porte que sur le montant HT des articles "
                    "de catégorie « boisson alcoolisée » (et non sur tout l'approvisionnement)."
                ),
                verbose_name="s'applique aux boissons alcoolisées uniquement",
            ),
        ),
        migrations.RunPython(cocher_tba_existantes, inverse),
    ]
