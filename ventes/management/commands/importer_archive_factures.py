"""Reprise de l'historique de facturation (2013-2026) dans les tables
d'archive (consultation seule) : FactureArchivee / LigneFactureArchivee.

Le lien vers la commande d'origine (IDCOMMANDES) peut pointer soit vers
`ventes.Commande` (periode vive, 2024-2026), soit vers
`ventes.CommandeArchivee` (periode archivee, 2013-2023) : on cherche dans
les deux pour retrouver le client, et on garde l'identifiant brut
(`commande_ancien_id`) dans tous les cas.

Usage :
    python manage.py importer_archive_factures --factures "<Factures ....xlsx>" --lignes "<ligne_facture ....xlsx>"
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
from ventes.models import Commande, CommandeArchivee, FactureArchivee, LigneFactureArchivee


class Command(BaseCommand):
    help = "Importe un historique de factures dans les tables d'archive (consultation seule)."

    def add_arguments(self, parser):
        parser.add_argument("--factures", required=True)
        parser.add_argument("--lignes", required=True)

    def handle(self, *args, **options):
        chemin_fact = Path(options["factures"])
        chemin_lignes = Path(options["lignes"])
        if not chemin_fact.exists():
            raise CommandError(f"Fichier introuvable : {chemin_fact}")
        if not chemin_lignes.exists():
            raise CommandError(f"Fichier introuvable : {chemin_lignes}")

        self._importer(chemin_fact, chemin_lignes)

    @transaction.atomic
    def _importer(self, chemin_fact, chemin_lignes):
        self.stdout.write(self.style.MIGRATE_HEADING("--- Factures (archive) ---"))
        _, lignes_fact = lire_feuille(chemin_fact)

        client_par_commande_vive = dict(
            Commande.objects.exclude(ancien_id=None).values_list("ancien_id", "client_id")
        )
        client_par_commande_archivee = dict(
            CommandeArchivee.objects.exclude(client=None).values_list("ancien_id", "client_id")
        )
        clients_par_pk = {c.pk: c for c in Client.objects.all()}
        deja = set(FactureArchivee.objects.values_list("ancien_id", flat=True))

        a_creer, deja_presentes, sans_commande = [], 0, 0
        for row in lignes_fact:
            ancien_id = parser_entier(row.get("IDFACTURATION"), defaut=None)
            if ancien_id is None:
                continue
            if ancien_id in deja:
                deja_presentes += 1
                continue

            id_commande = parser_entier(row.get("IDCOMMANDES"), defaut=None)
            client_pk = None
            if id_commande is not None:
                client_pk = client_par_commande_vive.get(id_commande) or client_par_commande_archivee.get(id_commande)
            if client_pk is None:
                sans_commande += 1
            client = clients_par_pk.get(client_pk)

            date_brute = row.get("DATE_FACTURE")
            date_fact = date_brute.date() if hasattr(date_brute, "date") else None
            date_regl_brute = row.get("DATE_REGLEMENT")
            date_regl = date_regl_brute.date() if hasattr(date_regl_brute, "date") else None
            date_retour_brute = row.get("DATE_RETOUR")
            date_retour = date_retour_brute.date() if hasattr(date_retour_brute, "date") else None

            a_creer.append(FactureArchivee(
                ancien_id=ancien_id,
                reference=str(row.get("NUMFACTURE") or "").strip(),
                commande_ancien_id=id_commande,
                client=client,
                nom_client=str(row.get("NOMCLIENT") or "").strip(),
                date=date_fact,
                montant=parser_decimal(row.get("MONTANT_FACTURE")),
                montant_liquide=parser_decimal(row.get("MONTANTLIQ")),
                montant_emballage=parser_decimal(row.get("MONTANTEMB")),
                mode_reglement=str(row.get("MODEREGLEMENT") or "").strip(),
                montant_regle=parser_decimal(row.get("MONTANT_REGLE")),
                date_reglement=date_regl,
                modalite=str(row.get("MODALITE") or "").strip(),
                observations=str(row.get("OBSERVATIONS") or "").strip(),
                retour_casier=parser_decimal(row.get("RETOURCASIER")),
                date_retour=date_retour,
                retour_emballage=parser_decimal(row.get("RETOUREMB")),
                nb_casiers_gm=parser_decimal(row.get("NBRECASIERGM")),
                nb_casiers_pm=parser_decimal(row.get("NBRECASIERPM")),
                numero_ordre=parser_entier(row.get("NUMORDRE"), defaut=None),
            ))

        FactureArchivee.objects.bulk_create(a_creer, batch_size=2000)
        self.stdout.write(self.style.SUCCESS(
            f"{len(a_creer)} facture(s) archivee(s) creee(s), {deja_presentes} deja presente(s) (ignoree(s))."
        ))
        if sans_commande:
            self.stdout.write(self.style.WARNING(
                f"{sans_commande} facture(s) sans commande d'origine retrouvee (client laisse vide, "
                f"nom_client conserve en texte)."
            ))

        self.stdout.write(self.style.MIGRATE_HEADING("--- Lignes de facture (archive) ---"))
        self._importer_lignes(chemin_lignes)

    def _importer_lignes(self, chemin_lignes):
        _, lignes_det = lire_feuille(chemin_lignes)

        pk_par_ancien_id = dict(FactureArchivee.objects.values_list("ancien_id", "pk"))
        articles = {a.ancien_id: a for a in Article.objects.exclude(ancien_id=None)}

        a_creer, facture_inconnue, article_inconnu, vides = [], set(), 0, 0
        for row in lignes_det:
            id_facture = parser_entier(row.get("IDFACTURATION"), defaut=None)
            facture_pk = pk_par_ancien_id.get(id_facture)
            if facture_pk is None:
                facture_inconnue.add(id_facture)
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

            a_creer.append(LigneFactureArchivee(
                facture_id=facture_pk, article=article,
                conditionnement=str(row.get("CONDITIONNEMENT") or "").strip(),
                quantite=quantite,
                remise=parser_decimal(row.get("REMISE"), decimales=2),
                prix_liquide=prix_liquide, prix_emballage=prix_emballage,
            ))

        LigneFactureArchivee.objects.bulk_create(a_creer, batch_size=2000)
        self.stdout.write(self.style.SUCCESS(
            f"{len(a_creer)} ligne(s) archivee(s) creee(s), {vides} vide(s) ignoree(s), "
            f"{article_inconnu} sans article reconnu (ligne conservee quand meme)."
        ))
        if facture_inconnue:
            self.stdout.write(self.style.WARNING(
                f"{len(facture_inconnue)} IDFACTURATION absent(s) des factures archivees : {sorted(facture_inconnue)[:30]}"
            ))
