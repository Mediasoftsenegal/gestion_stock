"""Reprise de l'historique 2013-2023 dans les tables d'archive (consultation
seule) : CommandeArchivee / LigneCommandeArchivee. Aucun effet sur le stock
ni la facturation ; pas de logique metier (contrairement a
`importer_commandes_vives`, aucune validation de prix plancher n'est
appliquee ici : c'est un instantane brut de l'ancien systeme).

Le conditionnement est conserve en texte brut (pas de resolution vers un
objet `Conditionnement`) : l'archive est faite pour etre consultee, pas
recalculee.

A executer un an a la fois (fichiers "commandes <annee>.xlsx" et
"ligne_commande <annee>.xlsx"), pour eviter la troncature Excel sur
plusieurs annees cumulees.

Usage :
    python manage.py importer_archive_commandes --commandes "<commandes 23.xlsx>" --lignes "<ligne_commande 23.xlsx>"
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalogue.management.commands.importer_legacy_catalogue import (
    lire_feuille,
    parser_decimal,
    parser_entier,
)
from catalogue.models import Article
from clients.models import Client
from ventes.models import CommandeArchivee, LigneCommandeArchivee


class Command(BaseCommand):
    help = "Importe un historique annuel (2013-2023) dans les tables d'archive (consultation seule)."

    def add_arguments(self, parser):
        parser.add_argument("--commandes", required=True)
        parser.add_argument("--lignes", required=True)

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
        self.stdout.write(self.style.MIGRATE_HEADING("--- Commandes (archive) ---"))
        _, lignes_cmd = lire_feuille(chemin_cmd)

        clients = {c.ancien_id: c for c in Client.objects.exclude(ancien_id=None)}
        deja = set(CommandeArchivee.objects.values_list("ancien_id", flat=True))

        a_creer, client_inconnu, deja_presentes = [], set(), 0
        for row in lignes_cmd:
            ancien_id = parser_entier(row.get("IDCOMMANDES"), defaut=None)
            if ancien_id is None:
                continue
            if ancien_id in deja:
                deja_presentes += 1
                continue

            id_client = parser_entier(row.get("IDCLIENTS"), defaut=None)
            client = clients.get(id_client)
            if client is None:
                client_inconnu.add(id_client)

            date_brute = row.get("DATECOMMANDE")
            date_cmd = date_brute.date() if hasattr(date_brute, "date") else None

            a_creer.append(CommandeArchivee(
                ancien_id=ancien_id,
                reference=str(row.get("NUMCOMMANDE") or "").strip(),
                client=client,
                date=date_cmd,
                montant=parser_decimal(row.get("MONTANTCOMMANDE")),
                montant_emballage=parser_decimal(row.get("MONTANT_EMB")),
                taux_tva=parser_decimal(row.get("TAUXTVA"), decimales=0),
                etat=str(row.get("ETATCOMMANDE") if row.get("ETATCOMMANDE") is not None else "").strip(),
                mode_reglement=str(row.get("MODEREGLEMENT") or "").strip(),
                numero_cheque=str(row.get("NUMCHEQUE") or "").strip(),
                saisie_par=str(row.get("SAISIE_PAR") or "").strip(),
                auteur=str(row.get("AUTEUR") or "").strip(),
                observations=str(row.get("OBSERVATIONS") or "").strip(),
                nb_casiers_gm=parser_entier(row.get("NBRECASIERGM")),
                nb_casiers_pm=parser_entier(row.get("NBRECASIERPM")),
            ))

        CommandeArchivee.objects.bulk_create(a_creer, batch_size=2000)
        self.stdout.write(self.style.SUCCESS(
            f"{len(a_creer)} commande(s) archivee(s) creee(s), {deja_presentes} deja presente(s) (ignoree(s))."
        ))
        if client_inconnu:
            self.stdout.write(self.style.WARNING(
                f"{len(client_inconnu)} IDCLIENTS absent(s) de la table des clients (client laisse vide) : "
                f"{sorted(client_inconnu)[:30]}"
            ))

        self.stdout.write(self.style.MIGRATE_HEADING("--- Lignes de commande (archive) ---"))
        self._importer_lignes(chemin_lignes)

    def _importer_lignes(self, chemin_lignes):
        _, lignes_det = lire_feuille(chemin_lignes)

        pk_par_ancien_id = dict(CommandeArchivee.objects.values_list("ancien_id", "pk"))
        articles = {a.ancien_id: a for a in Article.objects.exclude(ancien_id=None)}

        a_creer, commande_inconnue, article_inconnu, vides = [], set(), 0, 0
        for row in lignes_det:
            id_commande = parser_entier(row.get("IDCOMMANDES"), defaut=None)
            commande_pk = pk_par_ancien_id.get(id_commande)
            if commande_pk is None:
                commande_inconnue.add(id_commande)
                continue

            quantite = parser_decimal(row.get("QUANTITE"), decimales=3)
            prix_liquide = parser_decimal(row.get("PRIXVENTELIQUIDE"), decimales=2)
            prix_emballage = parser_decimal(row.get("PRIXEMBALLAGE"), decimales=2)
            if quantite == 0 and prix_liquide == 0 and prix_emballage == 0:
                vides += 1
                continue

            id_article = parser_entier(row.get("IDARTICLE"), defaut=None)
            article = articles.get(id_article)
            if article is None:
                article_inconnu += 1

            a_creer.append(LigneCommandeArchivee(
                commande_id=commande_pk, article=article,
                conditionnement=str(row.get("CONDITIONNEMENT") or "").strip(),
                quantite=quantite,
                remise=parser_decimal(row.get("REMISE"), decimales=2),
                prix_liquide=prix_liquide, prix_emballage=prix_emballage,
            ))

        LigneCommandeArchivee.objects.bulk_create(a_creer, batch_size=2000)
        self.stdout.write(self.style.SUCCESS(
            f"{len(a_creer)} ligne(s) archivee(s) creee(s), {vides} vide(s) ignoree(s), "
            f"{article_inconnu} sans article reconnu (ligne conservee quand meme)."
        ))
        if commande_inconnue:
            self.stdout.write(self.style.WARNING(
                f"{len(commande_inconnue)} IDCOMMANDES absent(s) des commandes archivees : {sorted(commande_inconnue)[:30]}"
            ))
