"""Ajoute Taxe.nature (TBA / TIGB / TVA / EGALISATION / AUTRE) pour piloter la
cascade de calcul, et classe automatiquement les taxes existantes d'apres leur
nom (et le drapeau sur_boissons_alcoolisees pour la TBA).
"""

import unicodedata

from django.db import migrations, models


def _norm(txt):
    return (
        unicodedata.normalize("NFD", txt or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def classer_natures(apps, schema_editor):
    Taxe = apps.get_model("stock", "Taxe")
    for taxe in Taxe.objects.all():
        n = _norm(taxe.type_taxe)
        compact = n.replace(".", "").replace(" ", "")
        if taxe.sur_boissons_alcoolisees or "tba" in compact:
            nature = "TBA"
        elif "tigb" in compact:
            nature = "TIGB"
        elif "egalisation" in n:
            nature = "EGALISATION"
        elif "tva" in compact:
            nature = "TVA"
        else:
            nature = "AUTRE"
        taxe.nature = nature
        # Coherence : la TBA porte sur les boissons alcoolisees.
        taxe.sur_boissons_alcoolisees = nature == "TBA"
        taxe.save(update_fields=["nature", "sur_boissons_alcoolisees"])


def inverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("stock", "0007_taxe_sur_boissons_alcoolisees"),
    ]

    operations = [
        migrations.AddField(
            model_name="taxe",
            name="nature",
            field=models.CharField(
                choices=[
                    ("TBA", "TBA (boissons alcoolisées)"),
                    ("TIGB", "TIGB"),
                    ("TVA", "TVA"),
                    ("EGALISATION", "Taxe d'égalisation"),
                    ("AUTRE", "Autre"),
                ],
                default="AUTRE",
                help_text=(
                    "Détermine l'assiette et l'ordre de calcul dans la cascade : "
                    "TBA (HT alcoolisé) → TIGB (HT − TBA) → TVA (HT + TBA + TIGB). "
                    "« Taxe d'égalisation » est affichée mais non calculée."
                ),
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="taxe",
            name="sur_boissons_alcoolisees",
            field=models.BooleanField(
                default=False,
                help_text="Renseigné automatiquement pour la TBA.",
                verbose_name="s'applique aux boissons alcoolisées uniquement",
            ),
        ),
        migrations.RunPython(classer_natures, inverse),
    ]
