"""Reprise des factures 2024+ dans les tables operationnelles
(Facture/LigneFacture/Paiement), comme simple historique de reference.

Contrairement a l'import des commandes/factures archivees (purement
consultatives), ces enregistrements apparaissent dans le module
Facturation courant (ils sont consultables/listes comme une vraie
facture). Mais, comme pour les commandes vives, cette reprise ne
recalcule rien : les montants sont ceux de l'epoque (PRIX_LIQUIDE +
PRIX_EMBALLAGE des lignes archivees), pas le tarif actuel, et aucun
mouvement de stock n'est declenche (le stock a deja ete ajuste par
l'import des commandes).

Source : les tables `FactureArchivee`/`LigneFactureArchivee`, deja
peuplees depuis les fichiers Excel (ancien systeme) et deja nettoyees
(client/article resolus). Pas besoin de re-parser les fichiers Excel.

Usage :
    python manage.py importer_factures_vives
    python manage.py importer_factures_vives --annee-min 2024
"""

from datetime import datetime, time
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from catalogue.management.commands.importer_legacy_catalogue import (
    resoudre_conditionnement_legacy,
)
from ventes.models import (
    Commande,
    Facture,
    FactureArchivee,
    LigneFacture,
    LigneFactureArchivee,
    Paiement,
)

MODES_PAIEMENT = {
    "ESPECES": Paiement.Mode.ESPECES,
    "CASH": Paiement.Mode.ESPECES,
    "MOBILE": Paiement.Mode.MOBILE,
    "MOBILE MONEY": Paiement.Mode.MOBILE,
    "CHEQUE": Paiement.Mode.CHEQUE,
    "VIREMENT": Paiement.Mode.VIREMENT,
}


class Command(BaseCommand):
    help = (
        "Importe les factures (deja chargees dans FactureArchivee) a partir de "
        "l'annee donnee dans les tables operationnelles Facture/LigneFacture, "
        "comme historique de reference (sans effet sur le stock)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--annee-min", type=int, default=2024,
            help="Premiere annee a reprendre dans les tables operationnelles (defaut 2024).",
        )

    def handle(self, *args, **options):
        self._importer(options["annee_min"])

    @transaction.atomic
    def _importer(self, annee_min):
        self.stdout.write(self.style.MIGRATE_HEADING("--- Factures ---"))

        commandes_par_ancien_id = dict(
            Commande.objects.filter(ancien_id__isnull=False).values_list("ancien_id", "pk")
        )
        appliquer_tva_par_commande_pk = dict(
            Commande.objects.filter(ancien_id__isnull=False).values_list("pk", "appliquer_tva")
        )
        deja_importees = set(
            Facture.objects.filter(ancien_id__isnull=False).values_list("ancien_id", flat=True)
        )
        numeros_vus = set(Facture.objects.values_list("numero", flat=True))

        source = (
            FactureArchivee.objects.filter(date__year__gte=annee_min)
            .order_by("ancien_id")
            .iterator(chunk_size=2000)
        )

        a_creer, paiements_a_creer = [], []
        factures_importees_par_ancien_id = {}
        commande_absente, client_absent, refs_dupliquees, deja_presentes = 0, 0, 0, 0

        for fa in source:
            if fa.ancien_id in deja_importees:
                deja_presentes += 1
                continue

            commande_pk = commandes_par_ancien_id.get(fa.commande_ancien_id)
            if commande_pk is None:
                commande_absente += 1
                continue

            if fa.client_id is None:
                client_absent += 1
                continue

            ref = (fa.reference or "").strip() or f"HIST-FAC-{fa.ancien_id}"
            if ref in numeros_vus:
                ref = f"{ref}-{fa.ancien_id}"
                refs_dupliquees += 1
            numeros_vus.add(ref)

            annulee = (fa.modalite or "").strip().upper() == "ANNULEE"
            date_facture = timezone.make_aware(datetime.combine(fa.date, time.min)) if fa.date else timezone.now()
            date_annulation = None
            if annulee:
                date_annulation_brute = fa.date_reglement or fa.date
                date_annulation = (
                    timezone.make_aware(datetime.combine(date_annulation_brute, time.min))
                    if date_annulation_brute else date_facture
                )

            facture = Facture(
                ancien_id=fa.ancien_id,
                numero=ref,
                commande_id=commande_pk,
                client_id=fa.client_id,
                date=date_facture,
                appliquer_tva=appliquer_tva_par_commande_pk.get(commande_pk, True),
                annulee=annulee,
                justificatif_annulation="Facture annulee (reprise de l'ancien systeme)." if annulee else "",
                annulee_le=date_annulation,
            )
            a_creer.append(facture)
            factures_importees_par_ancien_id[fa.ancien_id] = fa

            if not annulee and fa.montant_regle and fa.montant_regle > 0:
                paiements_a_creer.append((fa.ancien_id, fa))

        Facture.objects.bulk_create(a_creer, batch_size=2000)
        self.stdout.write(self.style.SUCCESS(
            f"{len(a_creer)} facture(s) creee(s), {deja_presentes} deja presente(s) (ignoree(s))."
        ))
        if commande_absente:
            self.stdout.write(self.style.WARNING(
                f"{commande_absente} facture(s) sans commande vive correspondante (ignoree(s))."
            ))
        if client_absent:
            self.stdout.write(self.style.WARNING(
                f"{client_absent} facture(s) sans client (ignoree(s))."
            ))
        if refs_dupliquees:
            self.stdout.write(self.style.WARNING(
                f"{refs_dupliquees} reference(s) en doublon dans l'historique : suffixees par l'identifiant historique."
            ))

        pk_facture_par_ancien_id = dict(
            Facture.objects.filter(ancien_id__isnull=False).values_list("ancien_id", "pk")
        )

        self.stdout.write(self.style.MIGRATE_HEADING("--- Paiements ---"))
        self._importer_paiements(paiements_a_creer, pk_facture_par_ancien_id)

        self.stdout.write(self.style.MIGRATE_HEADING("--- Lignes de facture ---"))
        self._importer_lignes(pk_facture_par_ancien_id, annee_min)

    def _importer_paiements(self, paiements_a_creer, pk_facture_par_ancien_id):
        a_creer = []
        for ancien_id, fa in paiements_a_creer:
            facture_pk = pk_facture_par_ancien_id.get(ancien_id)
            if facture_pk is None:
                continue
            mode = MODES_PAIEMENT.get((fa.mode_reglement or "").strip().upper(), Paiement.Mode.ESPECES)
            type_paiement = (
                Paiement.TypePaiement.COMPTANT
                if (fa.modalite or "").strip().upper() == "COMPTANT"
                else Paiement.TypePaiement.ACOMPTE
            )
            date_brute = fa.date_reglement or fa.date
            date_paiement = (
                timezone.make_aware(datetime.combine(date_brute, time.min))
                if date_brute else timezone.now()
            )
            a_creer.append(Paiement(
                facture_id=facture_pk,
                type_paiement=type_paiement,
                mode=mode,
                montant=fa.montant_regle,
                date=date_paiement,
            ))
        Paiement.objects.bulk_create(a_creer, batch_size=2000)
        self.stdout.write(self.style.SUCCESS(f"{len(a_creer)} paiement(s) cree(s)."))

    def _importer_lignes(self, pk_facture_par_ancien_id, annee_min):
        base_qs = LigneFactureArchivee.objects.filter(facture__date__year__gte=annee_min)

        # Resout/cree tous les conditionnements AVANT d'ouvrir le curseur non
        # tamponne sur les lignes : interroger la base (get_or_create) pendant
        # qu'un .iterator() est encore en cours de lecture desynchronise le
        # connecteur MySQL et tronque silencieusement le resultat.
        cache_cond = {}
        valeurs_distinctes = list(base_qs.values_list("conditionnement", flat=True).distinct())
        for brut in valeurs_distinctes:
            resoudre_conditionnement_legacy(brut, cache_cond)

        lignes_source = base_qs.select_related("facture").iterator(chunk_size=2000)

        a_creer = []
        facture_absente, article_absent, cond_inconnu_set, vides = 0, 0, set(), 0

        for ligne in lignes_source:
            facture_pk = pk_facture_par_ancien_id.get(ligne.facture.ancien_id)
            if facture_pk is None:
                facture_absente += 1
                continue

            if ligne.article_id is None:
                article_absent += 1
                continue

            quantite = ligne.quantite or Decimal("0")
            if quantite == 0:
                vides += 1
                continue

            cond, cond_brut_non_reconnu = resoudre_conditionnement_legacy(ligne.conditionnement, cache_cond)
            if cond is None:
                if cond_brut_non_reconnu:
                    cond_inconnu_set.add(cond_brut_non_reconnu)
                vides += 1
                continue

            prix = (ligne.prix_liquide or Decimal("0")) + (ligne.prix_emballage or Decimal("0"))

            a_creer.append(LigneFacture(
                facture_id=facture_pk,
                article_id=ligne.article_id,
                conditionnement=cond,
                quantite=quantite,
                prix_unitaire=prix,
                remise_unitaire=ligne.remise or Decimal("0"),
            ))

        LigneFacture.objects.bulk_create(a_creer, batch_size=2000)
        self.stdout.write(self.style.SUCCESS(
            f"{len(a_creer)} ligne(s) creee(s), {vides} vide(s)/non reconnue(s) ignoree(s)."
        ))
        if facture_absente:
            self.stdout.write(self.style.WARNING(
                f"{facture_absente} ligne(s) dont la facture n'a pas ete reprise (ignoree(s))."
            ))
        if article_absent:
            self.stdout.write(self.style.WARNING(
                f"{article_absent} ligne(s) sans article reconnu (ignoree(s))."
            ))
        if cond_inconnu_set:
            self.stdout.write(self.style.WARNING(
                f"Conditionnement non reconnu sur {len(cond_inconnu_set)} valeur(s) distincte(s) : "
                f"{sorted(cond_inconnu_set)[:30]}"
            ))
