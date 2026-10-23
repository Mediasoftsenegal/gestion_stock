"""Etat de suivi des achats par client sur une periode."""

import datetime as dt
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.utils import (
    bornes_periode,
    generer_modele_xlsx,
    normaliser_texte,
    parser_booleen,
    parser_decimal,
    parser_entier,
)

MOIS_LABELS = [
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]


def liste_tarif_boutique():
    """Liste de tarifs 'BOUTIQUE', utilisee pour les prix de la vente au
    detail (comptoir). Cree une liste vide si elle n'existe pas encore."""
    from clients.models import ListeTarif

    liste_tarif = ListeTarif.objects.filter(nomtarif__iexact="BOUTIQUE").first()
    if liste_tarif:
        return liste_tarif
    return ListeTarif.objects.create(
        nomtarif="BOUTIQUE",
        description="Prix de vente au detail (comptoir).",
    )


def client_comptoir():
    """Client pour les ventes au detail (comptoir) sans client B2B
    identifie : pointe uniquement sur le client reel « BOUTIQUE KH YALLA »
    (seul client/boutique concerne par la vente au detail), dont les prix
    proviennent de la liste de tarifs BOUTIQUE."""
    from clients.models import CategorieClient, Client

    client = Client.objects.filter(nomclient__iexact="BOUTIQUE KH YALLA").first()
    if client:
        return client

    # Filet de securite si ce client venait a disparaitre/etre renomme.
    categorie, _ = CategorieClient.objects.get_or_create(
        nom_categorie="VENTE COMPTOIR",
        defaults={"description": "Categorie technique pour les ventes au detail sans client identifie."},
    )
    liste_tarif = liste_tarif_boutique()
    client, _ = Client.objects.get_or_create(
        codeclient="COMPTOIR",
        defaults={
            "nomclient": "VENTE COMPTOIR",
            "categorie": categorie,
            "liste_tarif": liste_tarif,
            "avectva": False,
            "exempttva": True,
        },
    )
    if client.liste_tarif_id != liste_tarif.id:
        client.liste_tarif = liste_tarif
        client.save(update_fields=["liste_tarif", "modifie_le"])
    return client


def conditionnement_unite():
    """Conditionnement 'unite' (nombre_unites=1) utilise pour les ventes au
    detail : chaque article scanne compte pour une unite de base.

    Plusieurs conditionnements partagent nombre_unites=1 (Bouteille, Pack,
    Canette, Carton...) : « Bouteille » est de tres loin le plus utilise
    dans les tarifs BOUTIQUE, on le privilegie explicitement plutot que de
    prendre le premier conditionnement unitaire venu (id le plus bas),
    qui ne correspond pas forcement au tarif reellement saisi pour
    l'article."""
    from catalogue.models import Conditionnement

    cond = Conditionnement.objects.filter(nombre_unites=1, nom__iexact="Bouteille").first()
    if cond:
        return cond
    cond = Conditionnement.objects.filter(nombre_unites=1).order_by("id").first()
    if cond:
        return cond
    return Conditionnement.objects.create(nom="Unité", nombre_unites=1)


def suivi_client(client, periode="MOIS", reference=None):
    """Retourne le detail des factures d'un client sur une periode.

    `periode` : JOUR, SEMAINE, QUINZAINE, MOIS, TRIMESTRE, SEMESTRE, ANNEE.
    Structure retournee : dict avec les totaux et la liste des factures.
    """
    from .models import Facture

    debut, fin = bornes_periode(periode, reference)
    factures = (
        Facture.objects.filter(
            client=client, date__date__gte=debut, date__date__lte=fin
        )
        .prefetch_related("lignes", "paiements")
        .order_by("date")
    )

    detail = []
    total_facture = total_paye = total_non_paye = Decimal("0")
    for f in factures:
        montant = f.montant_total
        paye = f.montant_paye
        non_paye = Decimal("0") if f.annulee else (montant - paye)
        dates_paiement = sorted(p.date for p in f.paiements.all())
        detail.append(
            {
                "numero": f.numero,
                "date_facture": f.date,
                "montant": montant,
                "montant_paye": paye,
                "reliquat": non_paye,
                "statut_paiement": f.statut_paiement.label,
                "annulee": f.annulee,
                "dernier_paiement": dates_paiement[-1] if dates_paiement else None,
            }
        )
        if not f.annulee:
            total_facture += montant
            total_paye += paye
            total_non_paye += non_paye

    return {
        "client": client,
        "periode": periode,
        "debut": debut,
        "fin": fin,
        "total_facture": total_facture,
        "total_paye": total_paye,
        "total_non_paye": total_non_paye,
        "factures": detail,
    }


def releve_annuel_client(client, annee):
    """Etat de suivi annuel d'un client : ses factures regroupees mois par
    mois (janvier a decembre), avec le reliquat impaye d'un mois reporte
    ("report a nouveau") en premiere ligne du mois suivant.
    """
    from .models import Facture

    factures_par_mois = {m: [] for m in range(1, 13)}
    for f in (
        Facture.objects.filter(client=client, date__year=annee)
        .prefetch_related("paiements")
        .order_by("date")
    ):
        factures_par_mois[f.date.month].append(f)

    mois_data = []
    report_a_nouveau = Decimal("0")
    total_annee_facture = Decimal("0")
    total_annee_encaisse = Decimal("0")

    for m in range(1, 13):
        lignes = []
        if report_a_nouveau > 0:
            lignes.append({
                "report": True,
                "numero": "Report à nouveau",
                "pk": None,
                "date_facture": dt.date(annee, m, 1),
                "montant": report_a_nouveau,
                "montant_paye": Decimal("0"),
                "reliquat": report_a_nouveau,
                "annulee": False,
            })

        total_facture_mois = report_a_nouveau
        total_encaisse_mois = Decimal("0")
        total_reliquat_mois = report_a_nouveau

        for f in factures_par_mois[m]:
            montant = f.montant_total
            paye = f.montant_paye
            reliquat = Decimal("0") if f.annulee else (montant - paye)
            lignes.append({
                "report": False,
                "numero": f.numero,
                "pk": f.pk,
                "date_facture": f.date,
                "montant": montant,
                "montant_paye": paye,
                "reliquat": reliquat,
                "annulee": f.annulee,
            })
            if not f.annulee:
                total_facture_mois += montant
                total_encaisse_mois += paye
                total_reliquat_mois += reliquat

        mois_data.append({
            "numero": m,
            "nom": MOIS_LABELS[m - 1],
            "lignes": lignes,
            "report_a_nouveau": report_a_nouveau,
            "total_facture": total_facture_mois,
            "total_encaisse": total_encaisse_mois,
            "total_reliquat": total_reliquat_mois,
            "nb_factures": len(factures_par_mois[m]),
        })

        total_annee_facture += total_facture_mois - report_a_nouveau
        total_annee_encaisse += total_encaisse_mois
        report_a_nouveau = total_reliquat_mois if total_reliquat_mois > 0 else Decimal("0")

    return {
        "client": client,
        "annee": annee,
        "mois": mois_data,
        "total_annee_facture": total_annee_facture,
        "total_annee_encaisse": total_annee_encaisse,
        "reliquat_fin_annee": mois_data[-1]["total_reliquat"] if mois_data else Decimal("0"),
    }


# --------------------------------------------------------------------------- #
#  Import Excel des commandes (bons de commande). Une ligne de fichier =
#  une ligne de commande ; la colonne "Groupe" rassemble plusieurs lignes
#  dans une meme commande (vide = commande d'une seule ligne). Le prix
#  n'est jamais lu depuis le fichier : il est toujours recalcule depuis la
#  liste de tarifs du client (meme logique que la saisie manuelle, voir
#  `LigneCommande.save`). Si une colonne de verification du montant total
#  est fournie, l'ecart avec le montant recalcule est signale (sans
#  bloquer l'import).
# --------------------------------------------------------------------------- #
COLONNES_MODELE_COMMANDES = [
    "Groupe (regroupe les lignes de la meme commande)", "Client (code)", "Date (JJ/MM/AAAA)",
    "Article (code ou libellé)", "Conditionnement", "Quantité", "Remise unitaire",
    "Appliquer TVA (Oui/Non)", "Note", "Montant total attendu (vérification, optionnel)",
]
EXEMPLES_MODELE_COMMANDES = [
    ("CMD1", "CL-0001", "15/01/2026", "ART00001", "Casier 24", "10", "0", "Oui", "", ""),
    ("CMD1", "CL-0001", "15/01/2026", "ART00002", "Carton 12", "5", "0", "Oui", "", ""),
]


def generer_modele_commandes():
    return generer_modele_xlsx(COLONNES_MODELE_COMMANDES, exemples=EXEMPLES_MODELE_COMMANDES, feuille="Commandes")


def _valeur_commande(row, *cles):
    for cle in cles:
        if row.get(cle):
            return str(row[cle]).strip()
    return ""


def _parser_date(brut):
    """Accepte JJ/MM/AAAA, AAAA-MM-JJ, ou une date Excel deja convertie en
    texte par openpyxl (ex. '2026-01-15 00:00:00')."""
    brut = (brut or "").strip()
    if not brut:
        return timezone.localdate(), True
    brut_date = brut.split(" ")[0]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(brut_date, fmt).date(), True
        except ValueError:
            continue
    return None, False


def _grouper_lignes_commandes(lignes):
    """Regroupe les lignes du fichier par commande (colonne 'Groupe', ou une
    ligne par commande si elle est vide), en conservant l'ordre d'apparition."""
    groupes = {}
    ordre = []
    for index, row in enumerate(lignes, start=1):
        cle_brute = _valeur_commande(row, "GROUPE (REGROUPE LES LIGNES DE LA MEME COMMANDE)", "GROUPE", "REFERENCE")
        cle = cle_brute or f"__ligne_{index}"
        if cle not in groupes:
            groupes[cle] = []
            ordre.append(cle)
        groupes[cle].append((index, row))
    return [groupes[cle] for cle in ordre]


def analyser_commandes(lignes):
    """Compare chaque groupe de lignes (une future commande) a la base, sans
    rien ecrire. Retourne (resultats, statistiques) ; `resultats` est une
    liste de groupes, chacun avec ses lignes et ses erreurs."""
    from catalogue.models import Article, Conditionnement, get_prix
    from clients.models import Client

    from .models import _soumis_tva

    clients_par_code = {normaliser_texte(c.codeclient): c for c in Client.objects.select_related("liste_tarif")}
    articles_par_code = {normaliser_texte(a.code): a for a in Article.objects.all()}
    articles_par_libelle = {}
    for a in Article.objects.all():
        articles_par_libelle.setdefault(normaliser_texte(a.libarticle), a)
    conditionnements = {normaliser_texte(c.nom): c for c in Conditionnement.objects.all()}

    resultats = []
    stats = {"commandes": 0, "lignes": 0, "erreurs": 0, "ecarts": 0}

    for groupe in _grouper_lignes_commandes(lignes):
        premier_index = groupe[0][0]
        bloc = {
            "numero": premier_index, "lignes": [], "erreurs": [], "ecart": "",
            "client": None, "date": None, "appliquer_tva": True, "note": "",
        }

        code_client = next((_valeur_commande(r, "CLIENT (CODE)", "CLIENT", "CODE CLIENT") for _, r in groupe
                            if _valeur_commande(r, "CLIENT (CODE)", "CLIENT", "CODE CLIENT")), "")
        client = clients_par_code.get(normaliser_texte(code_client))
        if client is None:
            bloc["erreurs"].append(f"Client inconnu : « {code_client or '—'} ».")
        bloc["client"] = client
        bloc["code_client"] = code_client

        brut_date = next((_valeur_commande(r, "DATE (JJ/MM/AAAA)", "DATE") for _, r in groupe
                          if _valeur_commande(r, "DATE (JJ/MM/AAAA)", "DATE")), "")
        date, date_ok = _parser_date(brut_date)
        if not date_ok:
            bloc["erreurs"].append(f"Date invalide : « {brut_date} ».")
        bloc["date"] = date

        brut_tva = next((_valeur_commande(r, "APPLIQUER TVA (OUI/NON)", "APPLIQUER TVA") for _, r in groupe
                         if _valeur_commande(r, "APPLIQUER TVA (OUI/NON)", "APPLIQUER TVA")), "")
        bloc["appliquer_tva"] = parser_booleen(brut_tva, defaut=_soumis_tva(client)) if client else True
        bloc["note"] = next((_valeur_commande(r, "NOTE") for _, r in groupe if _valeur_commande(r, "NOTE")), "")
        brut_total_attendu = next((_valeur_commande(r, "MONTANT TOTAL ATTENDU (VERIFICATION, OPTIONNEL)", "MONTANT TOTAL ATTENDU")
                                   for _, r in groupe if _valeur_commande(r, "MONTANT TOTAL ATTENDU (VERIFICATION, OPTIONNEL)", "MONTANT TOTAL ATTENDU")), "")

        total_recalcule = Decimal("0")
        for index, row in groupe:
            ref_article = _valeur_commande(row, "ARTICLE (CODE OU LIBELLE)", "ARTICLE", "CODE")
            nom_cond = _valeur_commande(row, "CONDITIONNEMENT")
            brut_qte = _valeur_commande(row, "QUANTITE")
            brut_remise = _valeur_commande(row, "REMISE UNITAIRE")

            ligne = {
                "numero": index, "article": ref_article, "conditionnement": nom_cond,
                "quantite": brut_qte, "remise": brut_remise or "0", "erreurs": [],
                "article_obj": None, "cond_obj": None, "prix": None, "montant": None,
            }

            if not (ref_article and nom_cond and brut_qte):
                ligne["erreurs"].append("Article, conditionnement et quantité sont obligatoires.")
                bloc["lignes"].append(ligne)
                continue

            article = articles_par_code.get(normaliser_texte(ref_article)) or articles_par_libelle.get(normaliser_texte(ref_article))
            if article is None:
                ligne["erreurs"].append(f"Article inconnu : « {ref_article} ».")
            ligne["article_obj"] = article

            cond = conditionnements.get(normaliser_texte(nom_cond))
            if cond is None:
                ligne["erreurs"].append(f"Conditionnement inconnu : « {nom_cond} ».")
            ligne["cond_obj"] = cond

            quantite = parser_entier(brut_qte, defaut=None)
            if not quantite or quantite <= 0:
                ligne["erreurs"].append(f"Quantité invalide : « {brut_qte} ».")

            remise = parser_decimal(brut_remise, defaut=Decimal("0"))
            if remise < 0:
                ligne["erreurs"].append("La remise ne peut pas être négative.")

            if article and cond and client:
                prix = get_prix(client.liste_tarif, article, cond)
                if prix is None:
                    ligne["erreurs"].append(
                        f"Aucun tarif défini pour {article} / {cond} dans la liste « {client.liste_tarif} »."
                    )
                else:
                    ligne["prix"] = prix
                    if remise > prix:
                        ligne["erreurs"].append(f"La remise ({remise}) ne peut pas dépasser le prix ({prix}).")
                    else:
                        plancher = article.prix_vente_minimum(cond)
                        prix_net = prix - remise
                        if plancher and prix_net < plancher:
                            ligne["erreurs"].append(
                                f"Prix net ({prix_net}) inférieur au prix d'achat ({plancher}) pour {article}."
                            )
                        elif quantite:
                            ligne["montant"] = quantite * prix_net
                            total_recalcule += ligne["montant"]

            bloc["lignes"].append(ligne)

        if brut_total_attendu:
            attendu = parser_decimal(brut_total_attendu, defaut=None)
            if attendu is not None and abs(attendu - total_recalcule) > Decimal("1"):
                bloc["ecart"] = (
                    f"Montant attendu {attendu} ≠ montant recalculé {total_recalcule} "
                    f"(écart {attendu - total_recalcule})."
                )
                stats["ecarts"] += 1

        bloc["montant_ht"] = total_recalcule
        a_des_erreurs = bool(bloc["erreurs"]) or any(l["erreurs"] for l in bloc["lignes"]) or not bloc["lignes"]
        bloc["action"] = "erreur" if a_des_erreurs else "creation"
        if a_des_erreurs:
            stats["erreurs"] += 1
        else:
            stats["commandes"] += 1
            stats["lignes"] += len(bloc["lignes"])

        resultats.append(bloc)

    return resultats, stats


@transaction.atomic
def appliquer_commandes_import(lignes):
    """Cree les commandes valides (groupes sans erreur). Les groupes en
    erreur sont ignores (comptabilises) sans annuler les autres."""
    from .models import Commande, LigneCommande

    resultats, _ = analyser_commandes(lignes)
    compteurs = {"commandes": 0, "lignes": 0, "ignorees": 0}

    for bloc in resultats:
        if bloc["action"] == "erreur":
            compteurs["ignorees"] += 1
            continue

        commande = Commande.objects.create(
            client=bloc["client"], date=bloc["date"], note=bloc["note"],
            appliquer_tva=bloc["appliquer_tva"],
        )
        for ligne in bloc["lignes"]:
            LigneCommande.objects.create(
                commande=commande, article=ligne["article_obj"], conditionnement=ligne["cond_obj"],
                quantite=parser_entier(ligne["quantite"]), remise_unitaire=parser_decimal(ligne["remise"]),
            )
            compteurs["lignes"] += 1
        compteurs["commandes"] += 1

    return compteurs


# --------------------------------------------------------------------------- #
#  Import Excel des factures : meme fichier/format que l'import commandes,
#  mais chaque groupe est en plus valide (decremente le stock) puis facture
#  immediatement. A reserver a la saisie en masse de ventes deja realisees
#  (reprise d'historique) : contrairement a l'import commandes (qui ne cree
#  que des bons de commande en attente), celui-ci a un effet immediat sur
#  le stock et genere des factures non annulables sans justificatif.
# --------------------------------------------------------------------------- #
def generer_modele_factures():
    return generer_modele_xlsx(COLONNES_MODELE_COMMANDES, exemples=EXEMPLES_MODELE_COMMANDES, feuille="Factures")


def analyser_factures(lignes):
    """Comme `analyser_commandes`, avec en plus une verification du stock
    disponible (necessaire pour valider la commande et emettre la facture)."""
    from stock.services import stock_unites

    resultats, stats = analyser_commandes(lignes)
    stats["factures"] = stats.pop("commandes")

    for bloc in resultats:
        if bloc["action"] == "erreur":
            continue
        for ligne in bloc["lignes"]:
            article, cond = ligne["article_obj"], ligne["cond_obj"]
            quantite = parser_entier(ligne["quantite"], defaut=0)
            if article and cond and quantite:
                disponible = stock_unites(article)
                demande = quantite * cond.nombre_unites
                if disponible < demande:
                    ligne["erreurs"].append(
                        f"Stock insuffisant pour {article} : disponible {disponible}, demandé {demande}."
                    )
        if any(l["erreurs"] for l in bloc["lignes"]):
            bloc["action"] = "erreur"
            stats["factures"] -= 1
            stats["erreurs"] += 1

    return resultats, stats


def appliquer_factures_import(lignes, user=None):
    """Cree, valide (decremente le stock) et facture chaque groupe valide.
    Un groupe qui echoue (stock change entre l'aperçu et l'import, etc.) est
    annule individuellement (savepoint) et ignore ; les autres groupes de
    l'import ne sont pas affectes."""
    from .models import Commande, Facture, LigneCommande

    resultats, _ = analyser_factures(lignes)
    compteurs = {"factures": 0, "lignes": 0, "ignorees": 0}

    for bloc in resultats:
        if bloc["action"] == "erreur":
            compteurs["ignorees"] += 1
            continue

        try:
            with transaction.atomic():
                commande = Commande.objects.create(
                    client=bloc["client"], date=bloc["date"], note=bloc["note"],
                    appliquer_tva=bloc["appliquer_tva"],
                )
                for ligne in bloc["lignes"]:
                    LigneCommande.objects.create(
                        commande=commande, article=ligne["article_obj"], conditionnement=ligne["cond_obj"],
                        quantite=parser_entier(ligne["quantite"]), remise_unitaire=parser_decimal(ligne["remise"]),
                    )
                commande.valider(user=user)
                commande.generer_facture(user=user)
        except Exception:
            compteurs["ignorees"] += 1
            continue

        compteurs["factures"] += 1
        compteurs["lignes"] += len(bloc["lignes"])

    return compteurs
