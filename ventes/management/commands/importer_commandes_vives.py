"""Reprise des commandes 2024-2026 dans les tables operationnelles
(Commande/LigneCommande), comme simple historique de reference.

IMPORTANT : contrairement a une commande saisie normalement, cette reprise
n'appelle JAMAIS `Commande.valider()` ni `Commande.generer_facture()` : le
statut (BON_DE_COMMANDE/VALIDEE) est repris tel quel depuis ETATCOMMANDE,
mais aucun mouvement de stock n'est enregistre et aucune facture n'est
creee. Le prix de chaque ligne est celui de l'epoque (PRIXVENTELIQUIDE +
PRIXEMBALLAGE du fichier), pas le tarif actuel.

Vu le volume (dizaines de milliers de commandes, centaines de milliers de
lignes), l'import passe par `bulk_create` (pas de `save()`/`full_clean()`
ligne par ligne) : les validations (remise, prix plancher) sont donc
refaites ici en Python, et les lignes invalides sont ignorees et comptees
plutot que de faire echouer tout l'import.

Usage :
    python manage.py importer_commandes_vives --commandes "<commandes.xlsx>" --lignes "<ligne_commande.xlsx>"
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalogue.management.commands.importer_legacy_catalogue import (
    lire_feuille,
    parser_decimal,
    parser_entier,
    resoudre_conditionnement_legacy,
)
from catalogue.models import Article
from clients.models import Client
from ventes.models import Commande, LigneCommande


class Command(BaseCommand):
    help = "Importe les commandes 2024-2026 comme historique de reference (sans effet sur le stock/la facturation)."

    def add_arguments(self, parser):
        parser.add_argument("--commandes", required=True, help="Chemin du fichier commandes (en-tetes).")
        parser.add_argument("--lignes", required=True, help="Chemin du fichier des lignes de commande.")

    def handle(self, *args, **options):
        chemin_cmd = Path(options["commandes"])
        chemin_lignes = Path(options["lignes"])
        if not chemin_cmd.exists():
            raise CommandError(f"Fichier introuvable : {chemin_cmd}")
        if not chemin_lignes.exists():
            raise CommandError(f"Fichier introuvable : {chemin_lignes}")

        self._importer(chemin_cmd, chemin_lignes)

    @transaction.atomic
    def _importer(self, chemin_cmd, chemin_lignes):
        self.stdout.write(self.style.MIGRATE_HEADING("--- Commandes ---"))
        _, lignes_cmd = lire_feuille(chemin_cmd)

        clients = {c.ancien_id: c for c in Client.objects.exclude(ancien_id=None)}
        deja_importees = set(Commande.objects.filter(ancien_id__isnull=False).values_list("ancien_id", flat=True))
        references_vues = set(Commande.objects.values_list("reference", flat=True))

        a_creer, client_inconnu, refs_dupliquees, deja_presentes = [], set(), 0, 0

        for row in lignes_cmd:
            ancien_id = parser_entier(row.get("IDCOMMANDES"), defaut=None)
            if ancien_id is None:
                continue
            if ancien_id in deja_importees:
                deja_presentes += 1
                continue

            id_client = parser_entier(row.get("IDCLIENTS"), defaut=None)
            client = clients.get(id_client)
            if client is None:
                client_inconnu.add(id_client)
                continue

            ref = str(row.get("NUMCOMMANDE") or "").strip() or f"HIST-{ancien_id}"
            if ref in references_vues:
                ref = f"{ref}-{ancien_id}"
                refs_dupliquees += 1
            references_vues.add(ref)

            date_brute = row.get("DATECOMMANDE")
            date_cmd = date_brute.date() if hasattr(date_brute, "date") else None

            statut = Commande.Statut.VALIDEE if row.get("ETATCOMMANDE") == 1 else Commande.Statut.BON_DE_COMMANDE

            a_creer.append(Commande(
                # TAUXTVA de l'ancien systeme n'est pas un signal fiable (quasi toujours
                # > 0 sur tout l'historique) : la TVA reste desactivee par defaut, comme
                # pour toute commande/facture creee depuis l'application.
                ancien_id=ancien_id, reference=ref, client=client, liste_tarif=client.liste_tarif,
                date=date_cmd, statut=statut, appliquer_tva=False,
                note=str(row.get("OBSERVATIONS") or "").strip(),
            ))

        Commande.objects.bulk_create(a_creer, batch_size=2000)
        self.stdout.write(self.style.SUCCESS(
            f"{len(a_creer)} commande(s) creee(s), {deja_presentes} deja presente(s) (ignoree(s))."
        ))
        if client_inconnu:
            self.stdout.write(self.style.WARNING(
                f"{len(client_inconnu)} IDCLIENTS absent(s) de la table des clients : {sorted(client_inconnu)[:30]}"
            ))
        if refs_dupliquees:
            self.stdout.write(self.style.WARNING(
                f"{refs_dupliquees} NUMCOMMANDE en doublon dans le fichier (collision de l'ancien systeme) : "
                f"suffixe par l'IDCOMMANDES pour garantir l'unicite."
            ))

        self.stdout.write(self.style.MIGRATE_HEADING("--- Lignes de commande ---"))
        self._importer_lignes(chemin_lignes)

    def _importer_lignes(self, chemin_lignes):
        _, lignes_det = lire_feuille(chemin_lignes)

        pk_par_ancien_id = dict(
            Commande.objects.filter(ancien_id__isnull=False).values_list("ancien_id", "pk")
        )
        articles = {a.ancien_id: a for a in Article.objects.exclude(ancien_id=None)}
        cache_cond = {}

        a_creer = []
        commande_inconnue, article_inconnu, cond_inconnu = set(), set(), set()
        vides, rejetees = 0, 0

        for row in lignes_det:
            id_commande = parser_entier(row.get("IDCOMMANDES"), defaut=None)
            commande_pk = pk_par_ancien_id.get(id_commande)
            if commande_pk is None:
                commande_inconnue.add(id_commande)
                continue

            id_article = parser_entier(row.get("IDARTICLE"), defaut=None)
            article = articles.get(id_article)
            if article is None:
                article_inconnu.add(id_article)
                continue

            quantite = parser_decimal(row.get("QUANTITE"), decimales=3)
            if quantite == 0:
                vides += 1
                continue

            cond, cond_brut_non_reconnu = resoudre_conditionnement_legacy(row.get("CONDITIONNEMENT"), cache_cond)
            if cond is None:
                if cond_brut_non_reconnu:
                    cond_inconnu.add(cond_brut_non_reconnu)
                rejetees += 1
                continue

            remise = parser_decimal(row.get("REMISE"), decimales=2)
            prix = parser_decimal(row.get("PRIXVENTELIQUIDE"), decimales=2) + parser_decimal(row.get("PRIXEMBALLAGE"), decimales=2)

            if remise < 0 or remise > prix:
                rejetees += 1
                continue
            if quantite > 0:
                plancher = article.prix_vente_minimum(cond)
                if plancher and (prix - remise) < plancher:
                    rejetees += 1
                    continue

            a_creer.append(LigneCommande(
                commande_id=commande_pk, article=article, conditionnement=cond,
                quantite=quantite, prix_unitaire=prix, remise_unitaire=remise,
            ))

        LigneCommande.objects.bulk_create(a_creer, batch_size=2000)
        self.stdout.write(self.style.SUCCESS(
            f"{len(a_creer)} ligne(s) creee(s), {vides} vide(s) ignoree(s), {rejetees} rejetee(s)."
        ))
        if commande_inconnue:
            self.stdout.write(self.style.WARNING(
                f"{len(commande_inconnue)} IDCOMMANDES absent(s) des commandes importees : {sorted(commande_inconnue)[:30]}"
            ))
        if article_inconnu:
            self.stdout.write(self.style.WARNING(
                f"{len(article_inconnu)} IDARTICLE absent(s) de la table des articles : {sorted(article_inconnu)[:30]}"
            ))
        if cond_inconnu:
            self.stdout.write(self.style.WARNING(
                f"Conditionnement non reconnu sur {len(cond_inconnu)} valeur(s) distincte(s) : {sorted(cond_inconnu)[:30]}"
            ))
