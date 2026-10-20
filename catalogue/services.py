"""Chargement d'une nouvelle liste d'articles (mise a niveau du catalogue).

Le fichier fourni par l'utilisateur (CSV/TXT ou Excel) est lu, ses colonnes
sont detectees automatiquement (et re-mappables a la main), puis chaque ligne
est comparee au catalogue existant AVANT toute ecriture : l'utilisateur voit
ce qui sera cree, ce qui sera modifie (champ par champ) et ce qui est
inchange, et confirme ensuite.

Les articles absents du fichier ne sont jamais touches : un import est une
mise a niveau, pas un remplacement.
"""

import csv
import io
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction

from clients.models import ListeTarif

from .models import Article, Conditionnement, Tarif, TypeArticle, categorie_article


# --------------------------------------------------------------------------- #
#  Utilitaires de lecture
# --------------------------------------------------------------------------- #
def normaliser(valeur):
    """Normalise un texte pour comparaison tolerante (accents/casse/espaces)."""
    valeur = (valeur or "").strip()
    valeur = unicodedata.normalize("NFD", valeur).encode("ascii", "ignore").decode("ascii")
    valeur = valeur.replace("_", " ").replace("-", " ").replace(".", " ")
    return " ".join(valeur.upper().split())


def parser_decimal(valeur, defaut=Decimal("0")):
    valeur = str(valeur or "").strip()
    if not valeur:
        return defaut
    valeur = valeur.replace("\xa0", "").replace(" ", "").replace(",", ".")
    valeur = valeur.replace("FCFA", "").replace("F", "") if valeur.upper().endswith("FCFA") else valeur
    try:
        return Decimal(valeur)
    except (InvalidOperation, ValueError):
        return defaut


def parser_entier(valeur, defaut=0):
    dec = parser_decimal(valeur, defaut=None)
    if dec is None:
        return defaut
    try:
        return int(dec.to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return defaut


def parser_booleen(valeur, defaut=True):
    texte = normaliser(valeur)
    if not texte:
        return defaut
    if texte in {"1", "OUI", "O", "TRUE", "VRAI", "ACTIF", "YES", "Y", "X"}:
        return True
    if texte in {"0", "NON", "N", "FALSE", "FAUX", "INACTIF", "NO"}:
        return False
    return defaut


# Colonnes reconnues : {champ interne: libelles acceptes dans le fichier}
CHAMPS_FICHIER = {
    "code": ["CODE", "CODE ARTICLE", "REFERENCE", "REF"],
    "libarticle": ["LIBARTICLE", "LIBELLE", "LIBELLE ARTICLE", "DESIGNATION", "ARTICLE", "NOM"],
    "categorie": ["CATEGORIE", "CATEGORIE ARTICLE", "CATEGORIES ARTICLES", "FAMILLE"],
    "type_article": ["TYPE", "TYPE ARTICLE"],
    "unite_base": ["UNITE BASE", "UNITE DE BASE", "UNITE"],
    "prix_achat": ["PRIX ACHAT", "PRIX D ACHAT", "PA"],
    "prix_bouteille": ["PRIX BOUTEILLE", "PRIX VENTE", "PRIX DETAIL", "PV"],
    "consignation": ["CONSIGNATION", "CONSIGNE"],
    "seuil_alerte": ["SEUIL ALERTE", "SEUIL", "STOCK MINI", "STOCK MINIMUM"],
    "code_barre": ["CODE BARRE", "EAN", "GENCOD"],
    "fournisseur": ["FOURNISSEUR", "SUPPLIER"],
    "groupe": ["GROUPE", "GROUP"],
    "actif": ["ACTIF", "ACTIVE"],
    "conditionnement": ["CONDITIONNEMENT", "EMBALLAGE", "COND"],
    "nombre_unites": ["NOMBRE UNITES", "NB UNITES", "UNITES PAR CONDITIONNEMENT", "CONTENANCE"],
    "prix_tarif": ["PRIX TARIF", "PRIX CONDITIONNEMENT", "PRIX CASIER", "PRIX VENTE CONDITIONNEMENT"],
    "liste_tarif": ["LISTE TARIF", "LISTE DE TARIFS", "TARIF"],
}

# Champs simples reportes tels quels sur l'article (nom du champ modele -> parseur)
CHAMPS_TEXTE = ["libarticle", "unite_base", "code_barre", "fournisseur", "groupe"]
CHAMPS_MONTANT = ["prix_achat", "prix_bouteille", "consignation"]

LIBELLES = {
    "code": "Code",
    "libarticle": "Libellé",
    "categorie": "Catégorie",
    "type_article": "Type",
    "unite_base": "Unité de base",
    "prix_achat": "Prix d'achat",
    "prix_bouteille": "Prix bouteille",
    "consignation": "Consignation",
    "seuil_alerte": "Seuil d'alerte",
    "code_barre": "Code-barre",
    "fournisseur": "Fournisseur",
    "groupe": "Groupe",
    "actif": "Actif",
    "conditionnement": "Conditionnement",
    "nombre_unites": "Unités par conditionnement",
    "prix_tarif": "Prix par conditionnement",
    "liste_tarif": "Liste de tarifs",
}


# Ordre de detection : du plus specifique au plus generique, pour eviter
# qu'une colonne « NB UNITES » soit prise pour « UNITE », ou « PRIX
# CONDITIONNEMENT » pour « PRIX ».
ORDRE_DETECTION = [
    "code_barre", "code", "libarticle", "categorie", "type_article",
    "nombre_unites", "prix_achat", "prix_bouteille", "prix_tarif",
    "liste_tarif", "conditionnement", "consignation", "seuil_alerte",
    "fournisseur", "groupe", "actif", "unite_base",
]


def detecter_colonne(entetes, candidats, exclues=()):
    """Retrouve l'en-tete du fichier correspondant a un champ (exact puis partiel)."""
    normalisees = {h: normaliser(h) for h in entetes if h not in exclues}
    for cand in candidats:
        for h, n in normalisees.items():
            if n == cand:
                return h
    for cand in candidats:
        for h, n in normalisees.items():
            if cand in n:
                return h
    return None


def detecter_mapping(entetes):
    """Associe chaque donnee attendue a une colonne du fichier.

    Une colonne deja affectee n'est pas reutilisee : les correspondances
    exactes sont traitees en premier, puis les correspondances partielles
    dans l'ordre de specificite.
    """
    mapping = {champ: None for champ in CHAMPS_FICHIER}
    utilisees = set()

    normalisees = {h: normaliser(h) for h in entetes}
    for champ in ORDRE_DETECTION:
        for cand in CHAMPS_FICHIER[champ]:
            trouve = next(
                (h for h, n in normalisees.items() if n == cand and h not in utilisees), None
            )
            if trouve:
                mapping[champ] = trouve
                utilisees.add(trouve)
                break

    for champ in ORDRE_DETECTION:
        if mapping[champ]:
            continue
        trouve = detecter_colonne(entetes, CHAMPS_FICHIER[champ], exclues=utilisees)
        if trouve:
            mapping[champ] = trouve
            utilisees.add(trouve)

    return mapping


def lire_fichier(fichier, separateur=""):
    """Lit un CSV/TXT ou un classeur Excel et retourne (entetes, lignes).

    `lignes` est une liste de dictionnaires {en-tete: valeur}.
    """
    nom = (getattr(fichier, "name", "") or "").lower()

    if nom.endswith((".xlsx", ".xlsm", ".xltx")):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover - depend de l'environnement
            raise ValueError(
                "La lecture des fichiers Excel necessite openpyxl "
                "(pip install openpyxl). Enregistrez le fichier en CSV a defaut."
            ) from exc
        classeur = load_workbook(fichier, data_only=True, read_only=True)
        feuille = classeur.active
        iterateur = feuille.iter_rows(values_only=True)
        entetes = []
        for brut in iterateur:
            if brut and any(c is not None and str(c).strip() for c in brut):
                entetes = [str(c).strip() if c is not None else "" for c in brut]
                break
        lignes = []
        for brut in iterateur:
            if not brut or not any(c is not None and str(c).strip() for c in brut):
                continue
            valeurs = ["" if c is None else str(c).strip() for c in brut]
            valeurs += [""] * (len(entetes) - len(valeurs))
            lignes.append(dict(zip(entetes, valeurs)))
        return [h for h in entetes if h], lignes

    brut = fichier.read()
    if isinstance(brut, bytes):
        try:
            contenu = brut.decode("utf-8-sig")
        except UnicodeDecodeError:
            contenu = brut.decode("latin-1")
    else:
        contenu = brut

    if not separateur:
        premiere = contenu.splitlines()[0] if contenu else ""
        separateur = max([";", ",", "\t", "|"], key=lambda s: premiere.count(s))

    lecteur = csv.DictReader(io.StringIO(contenu), delimiter=separateur)
    entetes = [h for h in (lecteur.fieldnames or []) if h]
    lignes = [
        {k: v for k, v in row.items() if k}
        for row in lecteur
        if any((v or "").strip() for v in row.values() if v is not None)
    ]
    return entetes, lignes


# --------------------------------------------------------------------------- #
#  Analyse (aperçu avant application)
# --------------------------------------------------------------------------- #
def _valeur(row, mapping, champ):
    colonne = mapping.get(champ)
    if not colonne:
        return ""
    return str(row.get(colonne) or "").strip()


def analyser(lignes, mapping):
    """Compare chaque ligne du fichier au catalogue, sans rien ecrire.

    Retourne (resultats, statistiques).
    """
    articles_par_code = {
        normaliser(a.code): a for a in Article.objects.select_related(
            "type_article", "categorie_articles"
        )
    }
    articles_par_libelle = {}
    for a in articles_par_code.values():
        articles_par_libelle.setdefault(normaliser(a.libarticle), a)

    resultats = []
    stats = {"creations": 0, "mises_a_jour": 0, "inchanges": 0, "erreurs": 0, "tarifs": 0}

    for index, row in enumerate(lignes, start=1):
        code = _valeur(row, mapping, "code")
        libelle = _valeur(row, mapping, "libarticle")

        if not code and not libelle:
            continue

        article = None
        if code:
            article = articles_par_code.get(normaliser(code))
        if article is None and libelle:
            article = articles_par_libelle.get(normaliser(libelle))

        ligne = {
            "numero": index,
            "code": code or (article.code if article else ""),
            "libelle": libelle or (article.libarticle if article else ""),
            "article": article,
            "modifications": [],
            "erreurs": [],
            "tarif": None,
            "action": "creation" if article is None else "inchange",
        }

        if article is None and not libelle:
            ligne["action"] = "erreur"
            ligne["erreurs"].append("Libellé manquant : impossible de créer l'article.")
            stats["erreurs"] += 1
            resultats.append(ligne)
            continue

        def comparer(champ, avant, apres):
            if apres in (None, ""):
                return
            if str(avant) == str(apres):
                return
            ligne["modifications"].append(
                {
                    "champ": LIBELLES.get(champ, champ),
                    "avant": "—" if avant in (None, "") else avant,
                    "apres": apres,
                }
            )

        for champ in CHAMPS_TEXTE:
            valeur = _valeur(row, mapping, champ)
            if valeur:
                comparer(champ, getattr(article, champ, "") if article else "", valeur)

        for champ in CHAMPS_MONTANT:
            brut = _valeur(row, mapping, champ)
            if brut:
                valeur = parser_decimal(brut, defaut=None)
                if valeur is None:
                    ligne["erreurs"].append(f"{LIBELLES[champ]} illisible : « {brut} ».")
                    continue
                avant = getattr(article, champ, None) if article else None
                if avant is None or Decimal(avant) != valeur:
                    comparer(champ, avant, valeur)

        brut_seuil = _valeur(row, mapping, "seuil_alerte")
        if brut_seuil:
            comparer("seuil_alerte", article.seuil_alerte if article else None,
                     parser_entier(brut_seuil))

        brut_actif = _valeur(row, mapping, "actif")
        if brut_actif:
            actif = parser_booleen(brut_actif)
            if article is None or article.actif != actif:
                comparer(
                    "actif",
                    "" if article is None else ("Oui" if article.actif else "Non"),
                    "Oui" if actif else "Non",
                )

        categorie = _valeur(row, mapping, "categorie")
        if categorie:
            avant = article.categorie_articles.categories_articles if article and article.categorie_articles else ""
            comparer("categorie", avant, categorie)

        type_article = _valeur(row, mapping, "type_article")
        if type_article:
            avant = article.type_article.nom if article and article.type_article else ""
            comparer("type_article", avant, type_article)

        # --- Tarif de vente par conditionnement (optionnel) ---
        nom_cond = _valeur(row, mapping, "conditionnement")
        nb_unites = _valeur(row, mapping, "nombre_unites")
        prix_tarif = _valeur(row, mapping, "prix_tarif")
        nom_liste = _valeur(row, mapping, "liste_tarif")
        if prix_tarif and (nom_cond or nb_unites):
            prix = parser_decimal(prix_tarif, defaut=None)
            if prix is None:
                ligne["erreurs"].append(f"Prix par conditionnement illisible : « {prix_tarif} ».")
            elif not nom_liste:
                ligne["erreurs"].append(
                    "Prix par conditionnement fourni sans liste de tarifs : "
                    "ajoutez une colonne « liste de tarifs »."
                )
            else:
                ligne["tarif"] = {
                    "conditionnement": nom_cond or f"Lot {parser_entier(nb_unites)}",
                    "nombre_unites": parser_entier(nb_unites, defaut=1) or 1,
                    "prix": prix,
                    "liste_tarif": nom_liste,
                }
                stats["tarifs"] += 1

        if ligne["erreurs"]:
            ligne["action"] = "erreur"
            stats["erreurs"] += 1
        elif article is None:
            ligne["action"] = "creation"
            stats["creations"] += 1
        elif ligne["modifications"] or ligne["tarif"]:
            ligne["action"] = "maj"
            stats["mises_a_jour"] += 1
        else:
            stats["inchanges"] += 1

        resultats.append(ligne)

    return resultats, stats


# --------------------------------------------------------------------------- #
#  Application (ecriture en base)
# --------------------------------------------------------------------------- #
@transaction.atomic
def appliquer(lignes, mapping, creer_nouveaux=True, mettre_a_jour=True, appliquer_tarifs=True):
    """Applique l'import. Les articles absents du fichier ne sont pas touches."""
    resultats, _ = analyser(lignes, mapping)

    compteurs = {"crees": 0, "modifies": 0, "ignores": 0, "tarifs": 0, "erreurs": 0}
    caches = {"categories": {}, "types": {}, "conditionnements": {}, "listes": {}}

    def categorie_obj(nom):
        cle = normaliser(nom)
        if cle not in caches["categories"]:
            obj = categorie_article.objects.filter(categories_articles__iexact=nom.strip()).first()
            if obj is None:
                obj = categorie_article.objects.create(categories_articles=nom.strip())
            caches["categories"][cle] = obj
        return caches["categories"][cle]

    def type_obj(nom):
        cle = normaliser(nom)
        if cle not in caches["types"]:
            obj, _ = TypeArticle.objects.get_or_create(nom=nom.strip())
            caches["types"][cle] = obj
        return caches["types"][cle]

    def conditionnement_obj(nom, nombre_unites):
        cle = (normaliser(nom), nombre_unites)
        if cle not in caches["conditionnements"]:
            obj = Conditionnement.objects.filter(
                nom__iexact=nom.strip(), nombre_unites=nombre_unites
            ).first()
            if obj is None:
                obj = Conditionnement.objects.create(nom=nom.strip(), nombre_unites=nombre_unites)
            caches["conditionnements"][cle] = obj
        return caches["conditionnements"][cle]

    def liste_tarif_obj(nom):
        cle = normaliser(nom)
        if cle not in caches["listes"]:
            obj = ListeTarif.objects.filter(nomtarif__iexact=nom.strip()).first()
            if obj is None:
                obj = ListeTarif.objects.create(nomtarif=nom.strip())
            caches["listes"][cle] = obj
        return caches["listes"][cle]

    for ligne, row in zip(resultats, lignes):
        if ligne["action"] == "erreur":
            compteurs["erreurs"] += 1
            continue
        if ligne["action"] == "creation" and not creer_nouveaux:
            compteurs["ignores"] += 1
            continue
        if ligne["action"] in ("maj", "inchange") and not mettre_a_jour:
            compteurs["ignores"] += 1
            continue

        article = ligne["article"]
        cree = False
        if article is None:
            article = Article(code=_valeur(row, mapping, "code") or "")
            cree = True

        for champ in CHAMPS_TEXTE:
            valeur = _valeur(row, mapping, champ)
            if valeur:
                setattr(article, champ, valeur)

        for champ in CHAMPS_MONTANT:
            brut = _valeur(row, mapping, champ)
            if brut:
                valeur = parser_decimal(brut, defaut=None)
                if valeur is not None:
                    setattr(article, champ, valeur)

        brut_seuil = _valeur(row, mapping, "seuil_alerte")
        if brut_seuil:
            article.seuil_alerte = parser_entier(brut_seuil)

        brut_actif = _valeur(row, mapping, "actif")
        if brut_actif:
            article.actif = parser_booleen(brut_actif)

        categorie = _valeur(row, mapping, "categorie")
        if categorie:
            article.categorie_articles = categorie_obj(categorie)

        type_article = _valeur(row, mapping, "type_article")
        if type_article:
            article.type_article = type_obj(type_article)

        article.save()
        if cree:
            compteurs["crees"] += 1
        elif ligne["modifications"]:
            compteurs["modifies"] += 1

        # Tarif de vente par conditionnement
        if appliquer_tarifs and ligne["tarif"]:
            info = ligne["tarif"]
            cond = conditionnement_obj(info["conditionnement"], info["nombre_unites"])
            article.conditionnements.add(cond)
            Tarif.objects.update_or_create(
                liste_tarif=liste_tarif_obj(info["liste_tarif"]),
                article=article,
                conditionnement=cond,
                defaults={"prix_unitaire": info["prix"]},
            )
            compteurs["tarifs"] += 1

    return compteurs
