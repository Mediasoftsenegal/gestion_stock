"""Import Excel des listes de tarifs (noms de listes, pas les prix eux-memes
: voir `catalogue.services` pour l'import des tarifs par article).

Meme principe que l'import du catalogue (catalogue.services) : aperçu avant
ecriture, transaction tout-ou-rien, rapport final.
"""

from django.db import transaction

from core.utils import generer_modele_xlsx, normaliser_texte, parser_booleen

from .models import ListeTarif

COLONNES_MODELE = ["Nom", "Description", "Active"]
EXEMPLES_MODELE = [
    ("Gros", "Clients grossistes", "Oui"),
    ("Detail", "Clients au detail", "Oui"),
]

LIBELLES = {"nom": "Nom", "description": "Description", "active": "Active"}


def generer_modele():
    return generer_modele_xlsx(COLONNES_MODELE, exemples=EXEMPLES_MODELE, feuille="Listes de tarifs")


def _valeur(row, *cles):
    for cle in cles:
        if row.get(cle):
            return str(row[cle]).strip()
    return ""


def analyser(lignes):
    """Compare chaque ligne du fichier aux listes de tarifs existantes, sans
    rien ecrire. Retourne (resultats, statistiques)."""
    existantes = {normaliser_texte(l.nomtarif): l for l in ListeTarif.objects.all()}
    vus_dans_fichier = {}

    resultats = []
    stats = {"creations": 0, "mises_a_jour": 0, "inchanges": 0, "erreurs": 0}

    for index, row in enumerate(lignes, start=1):
        nom = _valeur(row, "NOM", "NOM TARIF", "NOM DE LA LISTE", "LISTE DE TARIFS", "LISTE TARIF")
        description = _valeur(row, "DESCRIPTION")
        brut_active = _valeur(row, "ACTIVE", "ACTIF")

        ligne = {
            "numero": index, "nom": nom, "description": description,
            "active": parser_booleen(brut_active, defaut=True) if brut_active else True,
            "liste": None, "erreurs": [], "action": "erreur",
        }

        if not nom:
            ligne["erreurs"].append("Nom manquant : impossible de créer la liste de tarifs.")
            stats["erreurs"] += 1
            resultats.append(ligne)
            continue

        cle = normaliser_texte(nom)
        if cle in vus_dans_fichier:
            ligne["erreurs"].append(f"Doublon dans le fichier (déjà en ligne {vus_dans_fichier[cle]}).")
            stats["erreurs"] += 1
            resultats.append(ligne)
            continue
        vus_dans_fichier[cle] = index

        existante = existantes.get(cle)
        ligne["liste"] = existante
        if existante is None:
            ligne["action"] = "creation"
            stats["creations"] += 1
        elif (existante.description != description and description) or existante.active != ligne["active"]:
            ligne["action"] = "maj"
            stats["mises_a_jour"] += 1
        else:
            ligne["action"] = "inchange"
            stats["inchanges"] += 1

        resultats.append(ligne)

    return resultats, stats


@transaction.atomic
def appliquer(lignes, creer_nouveaux=True, mettre_a_jour=True):
    resultats, _ = analyser(lignes)
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

        liste = ligne["liste"] or ListeTarif(nomtarif=ligne["nom"])
        cree = liste.pk is None
        liste.nomtarif = ligne["nom"]
        if ligne["description"]:
            liste.description = ligne["description"]
        liste.active = ligne["active"]
        liste.full_clean()
        liste.save()

        if cree:
            compteurs["crees"] += 1
        elif ligne["action"] == "maj":
            compteurs["modifies"] += 1

    return compteurs
