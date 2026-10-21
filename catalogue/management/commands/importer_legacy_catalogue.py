"""Reprise de donnees depuis l'ancien systeme (fichiers Excel exportes).

Importe, dans l'ordre impose (chaque table doit exister avant d'etre
referencee par la suivante) :
  1. stock_fournisseur.xlsx      -> stock.Fournisseur
  2. catalogue_typearticle.xlsx  -> catalogue.TypeArticle
  3. catalogue_categorie_article.xlsx -> catalogue.categorie_article
  4. catalogue_article.xlsx      -> catalogue.Article

Chaque fichier est importe dans sa propre transaction (tout ou rien par
fichier). Le TYPEARTICLE et le CONDITIONNEMENT de catalogue_article sont du
texte libre et heterogene dans l'ancien systeme : ils sont normalises vers
des valeurs canoniques connues, et toute ligne dont une valeur ne peut pas
etre rapprochee avec confiance est signalee dans le rapport final (l'article
est tout de meme cree, avec le champ concerne laisse vide, car les trois
autres tables ne tolerent aucune erreur de ce type).

Usage :
    python manage.py importer_legacy_catalogue --dossier "<dossier contenant les 4 fichiers>"
"""

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalogue.models import Article, Conditionnement, TypeArticle, categorie_article
from stock.models import Fournisseur

FICHIERS = {
    "fournisseur": "stock_fournisseur.xlsx",
    "typearticle": "catalogue_typearticle.xlsx",
    "categorie": "catalogue_categorie_article.xlsx",
    "article": "catalogue_article.xlsx",
}

# --------------------------------------------------------------------------- #
#  Normalisation TYPEARTICLE (texte libre et heterogene sur chaque article)
# --------------------------------------------------------------------------- #
TYPE_CANONIQUES = ["BTS", "PET", "CAN", "CRT", "PQT", "BRQ", "CASIER VIDE", "SCH", "AUTRE"]
TYPE_SYNONYMES = {
    "BTS": "BTS", "BOUTEILLE": "BTS", "BQ": "BTS", "BOU": "BTS",
    "PET": "PET",
    "CAN": "CAN",
    "CRT": "CRT",
    "PQT": "PQT",
    "BRQ": "BRQ",
    "CASIER VIDE": "CASIER VIDE",
    "SCH": "SCH",
    "AUTRE": "AUTRE", "AUTRES": "AUTRE",
}

# --------------------------------------------------------------------------- #
#  Normalisation CONDITIONNEMENT (ex. "CRT X 12", "PKS X 6", "BTS")
# --------------------------------------------------------------------------- #
PREFIXE_CONDITIONNEMENT = {
    "CRT": "Carton", "PKS": "Pack", "PK": "Pack", "CAN": "Canette",
    "CSR": "Caisse", "BTS": "Bouteille", "AUTRES": "Autres", "AUTRE": "Autres",
}
RE_CONDITIONNEMENT = re.compile(r"^([A-Z]+)\s*X\s*(\d+)\s*[A-Z]?\s*$")


def normaliser(valeur):
    valeur = (valeur or "")
    if not isinstance(valeur, str):
        valeur = str(valeur)
    valeur = valeur.strip()
    valeur = unicodedata.normalize("NFD", valeur).encode("ascii", "ignore").decode("ascii")
    return " ".join(valeur.upper().split())


def parser_decimal(valeur, defaut=Decimal("0"), decimales=2):
    """Parse un montant et l'arrondit a `decimales` decimales : certaines
    valeurs du fichier source ont plus de decimales que la colonne MySQL
    cible n'en accepte (decimal_places), ce que le mode strict rejette au
    lieu de tronquer silencieusement."""
    if valeur in (None, ""):
        return defaut
    if isinstance(valeur, (int, float, Decimal)):
        try:
            brut = Decimal(str(valeur))
        except InvalidOperation:
            return defaut
    else:
        texte = str(valeur).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
        try:
            brut = Decimal(texte)
        except InvalidOperation:
            return defaut
    quantum = Decimal(1).scaleb(-decimales) if decimales else Decimal(1)
    return brut.quantize(quantum)


def parser_entier(valeur, defaut=0):
    dec = parser_decimal(valeur, defaut=None)
    if dec is None:
        return defaut
    try:
        return int(dec)
    except (InvalidOperation, ValueError):
        return defaut


def lire_feuille(chemin):
    from openpyxl import load_workbook

    classeur = load_workbook(chemin, data_only=True, read_only=True)
    feuille = classeur.active
    lignes = list(feuille.iter_rows(values_only=True))
    classeur.close()
    if not lignes:
        return [], []
    entetes = [normaliser(h) for h in lignes[0]]
    donnees = []
    for row in lignes[1:]:
        if row and any(v is not None and str(v).strip() for v in row):
            donnees.append(dict(zip(entetes, row)))
    return entetes, donnees


def resoudre_conditionnement_legacy(brut, cache):
    """Normalise une valeur de conditionnement en texte libre de l'ancien
    systeme (ex. « CRT X 12 ») en objet `Conditionnement`, en le creant si
    besoin. `cache` : dict partage entre les appels (cle normalisee -> objet
    ou None) pour eviter de relancer la resolution/creation a chaque ligne.
    Retourne (conditionnement_ou_None, avertissement_ou_None)."""
    if not brut:
        return None, None
    cle = normaliser(brut)
    if cle in cache:
        return cache[cle], None
    if cle == "0":
        cache[cle] = None
        return None, brut
    m = RE_CONDITIONNEMENT.match(cle)
    if m:
        prefixe, nombre = m.group(1), int(m.group(2))
        avertissement = None
    elif cle in PREFIXE_CONDITIONNEMENT:
        prefixe, nombre = cle, 1
        avertissement = None
    else:
        cache[cle] = None
        return None, brut

    nom = PREFIXE_CONDITIONNEMENT.get(prefixe)
    if not nom:
        nom = prefixe.title()
        avertissement = f"prefixe de conditionnement non reconnu : « {prefixe} » (cree tel quel)"
    cond, _ = Conditionnement.objects.get_or_create(nom=nom, nombre_unites=nombre)
    cache[cle] = cond
    return cond, avertissement


class Command(BaseCommand):
    help = "Importe la reprise de donnees de l'ancien systeme (fournisseurs, types, categories, articles)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dossier", required=True,
            help="Dossier contenant les 4 fichiers Excel (noms attendus : "
                 "stock_fournisseur.xlsx, catalogue_typearticle.xlsx, "
                 "catalogue_categorie_article.xlsx, catalogue_article.xlsx).",
        )
        parser.add_argument(
            "--seulement", choices=list(FICHIERS), default=None,
            help="N'importer qu'une seule etape (fournisseur, typearticle, categorie, article).",
        )

    def handle(self, *args, **options):
        dossier = Path(options["dossier"])
        if not dossier.is_dir():
            raise CommandError(f"Dossier introuvable : {dossier}")

        etapes = [options["seulement"]] if options["seulement"] else list(FICHIERS)

        for etape in etapes:
            chemin = dossier / FICHIERS[etape]
            if not chemin.exists():
                raise CommandError(f"Fichier introuvable : {chemin}")
            methode = getattr(self, f"_importer_{etape}")
            self.stdout.write(self.style.MIGRATE_HEADING(f"--- {FICHIERS[etape]} ---"))
            methode(chemin)

    # --------------------------------------------------------------------- #
    #  1. Fournisseurs
    # --------------------------------------------------------------------- #
    @transaction.atomic
    def _importer_fournisseur(self, chemin):
        _, lignes = lire_feuille(chemin)
        crees, maj = 0, 0
        for row in lignes:
            ancien_id = parser_entier(row.get("IDFOURNISSEURS"), defaut=None)
            nom = str(row.get("NOMFOURNISSEUR") or "").strip()
            if ancien_id is None or not nom:
                continue
            fournisseur, cree = Fournisseur.objects.update_or_create(
                ancien_id=ancien_id,
                defaults={
                    "nom_fournisseur": nom,
                    "adressefour": str(row.get("ADRESSEFOUR") or "").strip(),
                    "numerofour": str(row.get("NUMFOURNISSEUR") or "").strip(),
                    "typefour": str(row.get("TYPE_FOURNISSEURS") or "").strip(),
                    "telephonefourn": str(row.get("TEL") or "").strip(),
                    "fax": str(row.get("FAX") or "").strip(),
                    "emailfour": str(row.get("EMAIL") or "").strip(),
                    "mobile": str(row.get("MOBILE") or "").strip(),
                },
            )
            crees += cree
            maj += not cree
        self.stdout.write(self.style.SUCCESS(f"Fournisseurs : {crees} cree(s), {maj} mis a jour."))

    # --------------------------------------------------------------------- #
    #  2. Types d'article
    # --------------------------------------------------------------------- #
    @transaction.atomic
    def _importer_typearticle(self, chemin):
        _, lignes = lire_feuille(chemin)
        crees, maj = 0, 0
        for row in lignes:
            ancien_id = parser_entier(row.get("ID"), defaut=None)
            nom = str(row.get("TYPEARTICLE") or "").strip()
            if ancien_id is None or not nom:
                continue
            obj, cree = TypeArticle.objects.update_or_create(
                ancien_id=ancien_id, defaults={"nom": nom},
            )
            crees += cree
            maj += not cree
        self.stdout.write(self.style.SUCCESS(f"Types d'article : {crees} cree(s), {maj} mis a jour."))

    # --------------------------------------------------------------------- #
    #  3. Categories d'article
    # --------------------------------------------------------------------- #
    @transaction.atomic
    def _importer_categorie(self, chemin):
        _, lignes = lire_feuille(chemin)
        crees, maj = 0, 0
        for row in lignes:
            ancien_id = parser_entier(row.get("IDCATEGORIE"), defaut=None)
            libelle = str(row.get("LIBACATEG") or "").strip()
            if ancien_id is None or not libelle:
                continue
            obj, cree = categorie_article.objects.update_or_create(
                ancien_id=ancien_id, defaults={"categories_articles": libelle},
            )
            crees += cree
            maj += not cree
        self.stdout.write(self.style.SUCCESS(f"Categories d'article : {crees} cree(s), {maj} mis a jour."))

    # --------------------------------------------------------------------- #
    #  4. Articles
    # --------------------------------------------------------------------- #
    def _resoudre_type(self, brut):
        if not brut:
            return None, None
        cle = normaliser(brut)
        # "P E T" (lettres espacees) doit aussi matcher "PET".
        canon = TYPE_SYNONYMES.get(cle) or TYPE_SYNONYMES.get(cle.replace(" ", ""))
        if not canon:
            return None, brut
        return TypeArticle.objects.filter(nom=canon).first(), None

    def _resoudre_conditionnement(self, brut, cache):
        return resoudre_conditionnement_legacy(brut, cache)

    @transaction.atomic
    def _importer_article(self, chemin):
        _, lignes = lire_feuille(chemin)
        fournisseurs = {f.ancien_id: f for f in Fournisseur.objects.exclude(ancien_id=None)}
        categories = {c.ancien_id: c for c in categorie_article.objects.exclude(ancien_id=None)}

        crees, maj = 0, 0
        types_non_reconnus, conditionnements_non_reconnus, articles_sans_fournisseur = set(), set(), 0
        cache_conditionnement = {}

        for row in lignes:
            ancien_id = parser_entier(row.get("IDARTICLE"), defaut=None)
            libelle = str(row.get("LIBARTICLE") or "").strip()
            if ancien_id is None or not libelle:
                continue

            type_obj, type_brut_non_reconnu = self._resoudre_type(row.get("TYPEARTICLE"))
            if type_brut_non_reconnu:
                types_non_reconnus.add(type_brut_non_reconnu)

            id_categorie = parser_entier(row.get("IDCATEGORIE"), defaut=None)
            categorie_obj = categories.get(id_categorie)

            id_fournisseur = parser_entier(row.get("IDFOURNISSEURS"), defaut=None)
            fournisseur_obj = fournisseurs.get(id_fournisseur)
            if id_fournisseur is not None and fournisseur_obj is None:
                articles_sans_fournisseur += 1

            etat_brut = normaliser(row.get("ETATPRODUIT"))
            etat = {
                "EN ETAT": Article.EtatProduit.EN_ETAT,
                "A L'ARRET": Article.EtatProduit.A_L_ARRET,
                "EN RUPTURE": Article.EtatProduit.EN_RUPTURE,
            }.get(etat_brut, Article.EtatProduit.EN_ETAT)

            # Le conditionnement doit etre resolu AVANT l'article : PRIXACHATLIQUIDE/
            # PRIXACHATEMBALLAGE sont des montants par conditionnement (carton/pack)
            # dans l'ancien systeme, alors que le modele actuel les stocke par unite
            # de base (coherent avec la saisie manuelle et le plancher de vente) ;
            # il faut donc les diviser par le nombre d'unites du conditionnement.
            cond_obj, cond_brut_non_reconnu = self._resoudre_conditionnement(
                row.get("CONDITIONNEMENT"), cache_conditionnement
            )
            if cond_brut_non_reconnu:
                conditionnements_non_reconnus.add(cond_brut_non_reconnu)
            diviseur = Decimal(cond_obj.nombre_unites if cond_obj and cond_obj.nombre_unites else 1)

            article, cree = Article.objects.update_or_create(
                ancien_id=ancien_id,
                defaults={
                    "libarticle": libelle,
                    "reference": str(row.get("REFERENCE") or "").strip(),
                    "categorie_articles": categorie_obj,
                    "type_article": type_obj,
                    "prix_achat_liquide": (parser_decimal(row.get("PRIXACHATLIQUIDE")) / diviseur).quantize(Decimal("0.01")),
                    "prix_achat_emballage": (parser_decimal(row.get("PRIXACHATEMBALLAGE")) / diviseur).quantize(Decimal("0.01")),
                    "seuil_alerte": parser_entier(row.get("SEUIL")),
                    "code_barre": str(row.get("CODEBARRES") or "").strip(),
                    "prix_bouteille": parser_decimal(row.get("PRIXBOUTEILLE")),
                    "prix_ttc": parser_decimal(row.get("PRIXTTC")),
                    "consignation": parser_decimal(row.get("CONSIGNATION"), decimales=0),
                    "fournisseur": fournisseur_obj,
                    "groupe": str(row.get("LIBELLEGROUPE") or "").strip(),
                    "etat_produit": etat,
                },
            )
            crees += cree
            maj += not cree

            if cond_obj:
                article.conditionnements.add(cond_obj)

        self.stdout.write(self.style.SUCCESS(f"Articles : {crees} cree(s), {maj} mis a jour."))
        if types_non_reconnus:
            self.stdout.write(self.style.WARNING(
                f"Type d'article non reconnu sur {len(types_non_reconnus)} valeur(s) distincte(s), "
                f"laisse vide : {sorted(types_non_reconnus)}"
            ))
        if conditionnements_non_reconnus:
            self.stdout.write(self.style.WARNING(
                f"Conditionnement non reconnu sur {len(conditionnements_non_reconnus)} valeur(s) distincte(s), "
                f"laisse vide : {sorted(conditionnements_non_reconnus)}"
            ))
        if articles_sans_fournisseur:
            self.stdout.write(self.style.WARNING(
                f"{articles_sans_fournisseur} article(s) reference(nt) un IDFOURNISSEURS "
                f"absent de la table fournisseur (fournisseur laisse vide)."
            ))
