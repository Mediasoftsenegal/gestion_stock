"""Etat de suivi des achats par client sur une periode."""

import datetime as dt
from decimal import Decimal

from core.utils import bornes_periode

MOIS_LABELS = [
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]


def client_comptoir():
    """Client generique pour les ventes au detail (comptoir) sans client B2B
    identifie. Cree une seule fois (categorie + liste de tarifs technique a
    l'appui), puis reutilise pour toutes les ventes suivantes."""
    from clients.models import CategorieClient, Client, ListeTarif

    categorie, _ = CategorieClient.objects.get_or_create(
        nom_categorie="VENTE COMPTOIR",
        defaults={"description": "Categorie technique pour les ventes au detail sans client identifie."},
    )
    liste_tarif, _ = ListeTarif.objects.get_or_create(
        nomtarif="VENTE COMPTOIR",
        defaults={
            "description": (
                "Liste technique : les prix de vente au detail proviennent "
                "d'Article.prix_bouteille, pas de cette liste."
            ),
        },
    )
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
    return client


def conditionnement_unite():
    """Conditionnement 'unite' (nombre_unites=1) utilise pour les ventes au
    detail : chaque article scanne compte pour une unite de base."""
    from catalogue.models import Conditionnement

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
