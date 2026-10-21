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
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from clients.models import ListeTarif
from core.utils import normaliser_texte as normaliser
from core.utils import parser_booleen, parser_decimal, parser_entier

from .models import Article, Conditionnement, Tarif, TypeArticle, categorie_article


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
CHAMPS_TEXTE = ["libarticle", "unite_base", "code_barre", "groupe"]
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


# --------------------------------------------------------------------------- #
#  Import Excel des tarifs (prix d'un article existant, pour une liste de
#  tarifs et un conditionnement existants). A la difference des tarifs
#  embarques dans l'import catalogue ci-dessus, la liste de tarifs et
#  l'article doivent deja exister : ce n'est pas le role de cet import de
#  les creer a la volee.
# --------------------------------------------------------------------------- #
COLONNES_MODELE_TARIFS = ["Liste de tarifs", "Article (code ou libellé)", "Conditionnement", "Prix"]
EXEMPLES_MODELE_TARIFS = [("Gros", "ART00001", "Casier 24", "15000")]


def generer_modele_tarifs():
    from core.utils import generer_modele_xlsx

    return generer_modele_xlsx(COLONNES_MODELE_TARIFS, exemples=EXEMPLES_MODELE_TARIFS, feuille="Tarifs")


def _valeur_tarif(row, *cles):
    for cle in cles:
        if row.get(cle):
            return str(row[cle]).strip()
    return ""


def analyser_tarifs(lignes):
    """Compare chaque ligne du fichier aux tarifs existants, sans rien
    ecrire. L'article et la liste de tarifs doivent deja exister."""
    listes = {normaliser(l.nomtarif): l for l in ListeTarif.objects.all()}
    articles_par_code = {normaliser(a.code): a for a in Article.objects.all()}
    articles_par_libelle = {}
    for a in Article.objects.all():
        articles_par_libelle.setdefault(normaliser(a.libarticle), a)
    conditionnements = {normaliser(c.nom): c for c in Conditionnement.objects.all()}
    existants = {
        (t.liste_tarif_id, t.article_id, t.conditionnement_id): t
        for t in Tarif.objects.select_related("liste_tarif", "article", "conditionnement")
    }

    resultats = []
    stats = {"creations": 0, "mises_a_jour": 0, "inchanges": 0, "erreurs": 0}
    vus_dans_fichier = set()

    for index, row in enumerate(lignes, start=1):
        nom_liste = _valeur_tarif(row, "LISTE DE TARIFS", "LISTE TARIF")
        ref_article = _valeur_tarif(row, "ARTICLE (CODE OU LIBELLE)", "ARTICLE", "CODE")
        nom_cond = _valeur_tarif(row, "CONDITIONNEMENT")
        brut_prix = _valeur_tarif(row, "PRIX")

        ligne = {
            "numero": index, "liste_tarif": nom_liste, "article": ref_article,
            "conditionnement": nom_cond, "prix": brut_prix,
            "liste": None, "article_obj": None, "cond_obj": None, "montant": None,
            "erreurs": [], "action": "erreur",
        }

        if not (nom_liste and ref_article and nom_cond and brut_prix):
            ligne["erreurs"].append("Liste de tarifs, article, conditionnement et prix sont tous obligatoires.")
            stats["erreurs"] += 1
            resultats.append(ligne)
            continue

        liste = listes.get(normaliser(nom_liste))
        if liste is None:
            ligne["erreurs"].append(f"Liste de tarifs inexistante : « {nom_liste} ».")
        ligne["liste"] = liste

        article = articles_par_code.get(normaliser(ref_article)) or articles_par_libelle.get(normaliser(ref_article))
        if article is None:
            ligne["erreurs"].append(f"Article inconnu : « {ref_article} ».")
        ligne["article_obj"] = article

        cond = conditionnements.get(normaliser(nom_cond))
        if cond is None:
            ligne["erreurs"].append(f"Conditionnement inconnu : « {nom_cond} ».")
        ligne["cond_obj"] = cond

        montant = parser_decimal(brut_prix, defaut=None)
        if montant is None or montant <= 0:
            ligne["erreurs"].append(f"Montant invalide : « {brut_prix} ».")
        ligne["montant"] = montant

        if not ligne["erreurs"] and article and cond:
            plancher = article.prix_vente_minimum(cond)
            if plancher and montant < plancher:
                ligne["erreurs"].append(
                    f"Le prix ({montant}) ne peut être inférieur au prix d'achat ({plancher}) pour {cond}."
                )

        if not ligne["erreurs"]:
            cle = (liste.pk, article.pk, cond.pk)
            if cle in vus_dans_fichier:
                ligne["erreurs"].append("Doublon dans le fichier (même liste, article et conditionnement).")
            else:
                vus_dans_fichier.add(cle)

        if ligne["erreurs"]:
            ligne["action"] = "erreur"
            stats["erreurs"] += 1
        else:
            existant = existants.get((liste.pk, article.pk, cond.pk))
            if existant is None:
                ligne["action"] = "creation"
                stats["creations"] += 1
            elif existant.prix_unitaire != montant:
                ligne["action"] = "maj"
                stats["mises_a_jour"] += 1
            else:
                ligne["action"] = "inchange"
                stats["inchanges"] += 1

        resultats.append(ligne)

    return resultats, stats


@transaction.atomic
def appliquer_tarifs_import(lignes, creer_nouveaux=True, mettre_a_jour=True):
    resultats, _ = analyser_tarifs(lignes)
    compteurs = {"crees": 0, "modifies": 0, "ignores": 0, "erreurs": 0}

    for ligne in resultats:
        if ligne["action"] == "erreur":
            compteurs["erreurs"] += 1
            continue
        if ligne["action"] == "creation" and not creer_nouveaux:
            compteurs["ignores"] += 1
            continue
        if ligne["action"] in ("maj", "inchange") and not mettre_a_jour:
            compteurs["ignores"] += 1
            continue

        tarif = Tarif.objects.filter(
            liste_tarif=ligne["liste"], article=ligne["article_obj"], conditionnement=ligne["cond_obj"],
        ).first()
        cree = tarif is None
        tarif = tarif or Tarif(
            liste_tarif=ligne["liste"], article=ligne["article_obj"], conditionnement=ligne["cond_obj"],
        )
        tarif.prix_unitaire = ligne["montant"]
        try:
            tarif.full_clean()
        except ValidationError:
            compteurs["erreurs"] += 1
            continue
        tarif.save()

        if cree:
            compteurs["crees"] += 1
        elif ligne["action"] == "maj":
            compteurs["modifies"] += 1

    return compteurs
