"""Reprise de donnees depuis l'ancien systeme (lot 2 : tarifs par liste).

tarifs.xlsx -> catalogue.Tarif. A executer apres les imports
`importer_legacy_catalogue` (articles) et `importer_legacy_clients`
(listes de tarifs).

L'ancien systeme ne distingue pas le tarif par conditionnement : il stocke,
pour chaque couple (article, liste de tarifs), un prix "detail"
(PRIXVENTELIQUIDE + PRIXEMBALLAGE, pour le conditionnement normal de
l'article, ex. casier/carton) et/ou un prix "bouteille" (PRIXBOUTEILLE,
pour la vente a l'unite). Chaque prix non nul produit donc une ligne Tarif
distincte :
  - prix detail  -> Tarif(liste, article, <conditionnement normal de l'article>)
  - prix bouteille -> Tarif(liste, article, <conditionnement Bouteille (1)>),
    cree/ajoute a l'article si besoin.

Usage :
    python manage.py importer_legacy_tarifs --dossier "<dossier>"
"""

from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalogue.management.commands.importer_legacy_catalogue import (
    lire_feuille,
    parser_decimal,
    parser_entier,
)
from catalogue.models import Article, Conditionnement, Tarif
from clients.models import ListeTarif


class Command(BaseCommand):
    help = "Importe les tarifs par liste (tarifs.xlsx) de l'ancien systeme."

    def add_arguments(self, parser):
        parser.add_argument("--dossier", required=True)

    def handle(self, *args, **options):
        dossier = Path(options["dossier"])
        chemin = dossier / "tarifs.xlsx"
        if not chemin.exists():
            raise CommandError(f"Fichier introuvable : {chemin}")

        self.stdout.write(self.style.MIGRATE_HEADING("--- tarifs.xlsx ---"))
        self._importer(chemin)

    @transaction.atomic
    def _importer(self, chemin):
        _, lignes = lire_feuille(chemin)

        listes = {l.ancien_id: l for l in ListeTarif.objects.exclude(ancien_id=None)}
        articles = {
            a.ancien_id: a
            for a in Article.objects.exclude(ancien_id=None).prefetch_related("conditionnements")
        }
        cond_bouteille, _ = Conditionnement.objects.get_or_create(nom="Bouteille", nombre_unites=1)

        crees, maj, vides, sous_plancher = 0, 0, 0, 0
        liste_inconnue, article_inconnu, sans_conditionnement = set(), set(), set()

        for row in lignes:
            id_liste = parser_entier(row.get("IDNOMLISTE"), defaut=None)
            id_article = parser_entier(row.get("IDARTICLE"), defaut=None)
            if id_liste is None or id_article is None:
                continue

            liste_tarif = listes.get(id_liste)
            if liste_tarif is None:
                liste_inconnue.add(id_liste)
                continue

            article = articles.get(id_article)
            if article is None:
                article_inconnu.add(id_article)
                continue

            # Tarif.prix_unitaire a 0 decimale (contrairement aux montants de l'article).
            prix_detail = parser_decimal(row.get("PRIXVENTELIQUIDE"), decimales=0) \
                + parser_decimal(row.get("PRIXEMBALLAGE"), decimales=0)
            prix_bouteille = parser_decimal(row.get("PRIXBOUTEILLE"), decimales=0)

            if prix_detail <= 0 and prix_bouteille <= 0:
                vides += 1
                continue

            if prix_detail > 0:
                cond_normal = next(
                    (c for c in article.conditionnements.all() if c.pk != cond_bouteille.pk), None
                )
                if cond_normal is None:
                    sans_conditionnement.add(id_article)
                else:
                    try:
                        _, cree = Tarif.objects.update_or_create(
                            liste_tarif=liste_tarif, article=article, conditionnement=cond_normal,
                            defaults={"prix_unitaire": prix_detail},
                        )
                        crees += cree
                        maj += not cree
                    except ValidationError:
                        sous_plancher += 1

            if prix_bouteille > 0:
                if not article.conditionnements.filter(pk=cond_bouteille.pk).exists():
                    article.conditionnements.add(cond_bouteille)
                try:
                    _, cree = Tarif.objects.update_or_create(
                        liste_tarif=liste_tarif, article=article, conditionnement=cond_bouteille,
                        defaults={"prix_unitaire": prix_bouteille},
                    )
                    crees += cree
                    maj += not cree
                except ValidationError:
                    sous_plancher += 1

        self.stdout.write(self.style.SUCCESS(f"Tarifs : {crees} cree(s), {maj} mis a jour, {vides} ligne(s) sans prix ignoree(s)."))
        if sous_plancher:
            self.stdout.write(self.style.WARNING(
                f"{sous_plancher} tarif(s) rejete(s) : prix de vente inferieur au prix d'achat de l'article."
            ))
        if liste_inconnue:
            self.stdout.write(self.style.WARNING(
                f"{len(liste_inconnue)} IDNOMLISTE absent(s) de la table des listes : {sorted(liste_inconnue)[:30]}"
            ))
        if article_inconnu:
            self.stdout.write(self.style.WARNING(
                f"{len(article_inconnu)} IDARTICLE absent(s) de la table des articles : {sorted(article_inconnu)[:30]}"
            ))
        if sans_conditionnement:
            self.stdout.write(self.style.WARNING(
                f"{len(sans_conditionnement)} article(s) sans conditionnement normal connu : "
                f"le tarif « detail » (liquide+emballage) n'a pas pu etre enregistre pour eux "
                f"(IDARTICLE : {sorted(sans_conditionnement)[:30]})."
            ))
