"""Importe un inventaire physique depuis un fichier CSV.

Format attendu (en-tete inclus) :
    code_article;quantite_physique

Exemple :
    python manage.py importer_inventaire comptage.csv --date 2026-07-06 --periode MOIS
"""

import csv

from django.core.management.base import BaseCommand, CommandError

from catalogue.models import Article
from stock.models import Inventaire, LigneInventaire


class Command(BaseCommand):
    help = "Importe un inventaire physique depuis un CSV (code_article;quantite)."

    def add_arguments(self, parser):
        parser.add_argument("fichier", help="Chemin du fichier CSV.")
        parser.add_argument("--date", required=True, help="Date de l'inventaire (AAAA-MM-JJ).")
        parser.add_argument("--periode", default="MOIS")
        parser.add_argument("--separateur", default=";")
        parser.add_argument(
            "--valider",
            action="store_true",
            help="Valide immediatement l'inventaire (genere les ajustements).",
        )

    def handle(self, *args, **options):
        inventaire = Inventaire.objects.create(
            date=options["date"], periode=options["periode"]
        )
        importees, ignorees = 0, []
        with open(options["fichier"], newline="", encoding="utf-8-sig") as f:
            lecteur = csv.reader(f, delimiter=options["separateur"])
            next(lecteur, None)  # en-tete
            for numero, ligne in enumerate(lecteur, start=2):
                if not ligne or not ligne[0].strip():
                    continue
                code = ligne[0].strip()
                try:
                    quantite = int(ligne[1])
                except (IndexError, ValueError):
                    ignorees.append(f"ligne {numero}: quantite invalide")
                    continue
                article = Article.objects.filter(code=code).first()
                if not article:
                    ignorees.append(f"ligne {numero}: code inconnu '{code}'")
                    continue
                LigneInventaire.objects.update_or_create(
                    inventaire=inventaire,
                    article=article,
                    defaults={"quantite_physique": quantite},
                )
                importees += 1

        if importees == 0:
            inventaire.delete()
            raise CommandError("Aucune ligne importee. Inventaire annule.")

        self.stdout.write(
            self.style.SUCCESS(
                f"{inventaire.reference} cree : {importees} ligne(s) importee(s)."
            )
        )
        for msg in ignorees:
            self.stdout.write(self.style.WARNING(f"  ignore -> {msg}"))

        if options["valider"]:
            inventaire.valider()
            self.stdout.write(self.style.SUCCESS("Inventaire valide, ecarts ajustes."))
