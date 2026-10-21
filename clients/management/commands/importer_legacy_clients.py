"""Reprise de donnees depuis l'ancien systeme (lot 2 : listes de tarifs et clients).

Importe, dans l'ordre (la liste de tarifs doit exister avant le client qui la
reference) :
  1. nomliste.xlsx         -> clients.ListeTarif
  2. clients.xlsx + client_nomliste.xlsx -> clients.Client

`client_nomliste.xlsx` fait le lien client <-> liste de tarifs (IDCLIENTS,
IDNOMLISTE). Certains clients y apparaissent plusieurs fois (plusieurs
listes) : le modele Client n'autorisant qu'une seule liste, seule la
premiere rencontree est retenue ; les autres sont signalees dans le rapport
pour correction manuelle eventuelle.

Usage :
    python manage.py importer_legacy_clients --dossier "<dossier>"
    python manage.py importer_legacy_clients --dossier "<dossier>" --seulement nomliste|clients
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalogue.management.commands.importer_legacy_catalogue import (
    lire_feuille,
    normaliser,
    parser_entier,
)
from clients.models import CategorieClient, Client, ListeTarif
from core.utils import parser_booleen

FICHIERS = {
    "nomliste": "nomliste.xlsx",
    "clients": "clients.xlsx",
}
FICHIER_LIEN = "client_nomliste.xlsx"


class Command(BaseCommand):
    help = "Importe les listes de tarifs (noms) et les clients de l'ancien systeme."

    def add_arguments(self, parser):
        parser.add_argument("--dossier", required=True)
        parser.add_argument("--seulement", choices=list(FICHIERS), default=None)

    def handle(self, *args, **options):
        dossier = Path(options["dossier"])
        if not dossier.is_dir():
            raise CommandError(f"Dossier introuvable : {dossier}")

        etapes = [options["seulement"]] if options["seulement"] else list(FICHIERS)

        for etape in etapes:
            chemin = dossier / FICHIERS[etape]
            if not chemin.exists():
                raise CommandError(f"Fichier introuvable : {chemin}")
            self.stdout.write(self.style.MIGRATE_HEADING(f"--- {FICHIERS[etape]} ---"))
            if etape == "nomliste":
                self._importer_nomliste(chemin)
            else:
                chemin_lien = dossier / FICHIER_LIEN
                if not chemin_lien.exists():
                    raise CommandError(f"Fichier introuvable : {chemin_lien}")
                self._importer_clients(chemin, chemin_lien)

    @transaction.atomic
    def _importer_nomliste(self, chemin):
        _, lignes = lire_feuille(chemin)
        crees, maj = 0, 0
        for row in lignes:
            ancien_id = parser_entier(row.get("IDNOMLISTE"), defaut=None)
            nom = str(row.get("NOMLISTE") or "").strip()
            if ancien_id is None or not nom:
                continue
            obj, cree = ListeTarif.objects.update_or_create(
                ancien_id=ancien_id, defaults={"nomtarif": nom},
            )
            crees += cree
            maj += not cree
        self.stdout.write(self.style.SUCCESS(f"Listes de tarifs : {crees} cree(s), {maj} mise(s) a jour."))

    @transaction.atomic
    def _importer_clients(self, chemin_clients, chemin_lien):
        _, liens = lire_feuille(chemin_lien)
        # Un seul IDNOMLISTE retenu par client (le premier rencontre dans le fichier).
        liste_par_client, clients_multi_listes = {}, set()
        for row in liens:
            id_client = parser_entier(row.get("IDCLIENTS"), defaut=None)
            id_liste = parser_entier(row.get("IDNOMLISTE"), defaut=None)
            if id_client is None or id_liste is None:
                continue
            if id_client in liste_par_client:
                if liste_par_client[id_client] != id_liste:
                    clients_multi_listes.add(id_client)
                continue
            liste_par_client[id_client] = id_liste

        listes = {l.ancien_id: l for l in ListeTarif.objects.exclude(ancien_id=None)}
        categories_cache = {}

        def categorie_obj(nom):
            nom = (nom or "").strip() or "Non renseigné"
            cle = normaliser(nom)
            if cle not in categories_cache:
                obj = CategorieClient.objects.filter(nom_categorie__iexact=nom).first()
                if obj is None:
                    obj = CategorieClient.objects.create(nom_categorie=nom)
                categories_cache[cle] = obj
            return categories_cache[cle]

        _, lignes = lire_feuille(chemin_clients)
        crees, maj, sans_liste = 0, 0, 0

        for row in lignes:
            ancien_id = parser_entier(row.get("IDCLIENTS"), defaut=None)
            nom = str(row.get("NOMCLIENT") or "").strip()
            code = str(row.get("CODECLIENT") or "").strip()
            if ancien_id is None or not nom or not code:
                continue

            id_liste = liste_par_client.get(ancien_id)
            liste_tarif = listes.get(id_liste) if id_liste is not None else None
            if liste_tarif is None:
                sans_liste += 1
                liste_tarif, _ = ListeTarif.objects.get_or_create(
                    nomtarif="A corriger (liste inconnue)", defaults={"active": False},
                )

            etat = normaliser(row.get("ETATCLIENT"))
            client, cree = Client.objects.update_or_create(
                ancien_id=ancien_id,
                defaults={
                    "nomclient": nom,
                    "codeclient": code,
                    "adresse": str(row.get("ADRESSE") or "").strip(),
                    "categorie": categorie_obj(row.get("TYPECLIENT")),
                    "telephone": str(row.get("TEL") or "").strip(),
                    "fax": str(row.get("FAX") or "").strip(),
                    "mobile": str(row.get("MOBILE") or "").strip(),
                    "societe": str(row.get("SOCIETE") or "").strip(),
                    "email": str(row.get("EMAIL") or "").strip(),
                    "exempttva": parser_booleen(row.get("EXEMPTTVA"), defaut=False),
                    "avectva": parser_booleen(row.get("AVECTVA"), defaut=True),
                    "béneficiaire": str(row.get("BENEFICAIRE") or "").strip(),
                    "liste_tarif": liste_tarif,
                    "actif": etat != "INACTIF",
                },
            )
            crees += cree
            maj += not cree

        self.stdout.write(self.style.SUCCESS(f"Clients : {crees} cree(s), {maj} mis a jour."))
        if sans_liste:
            self.stdout.write(self.style.WARNING(
                f"{sans_liste} client(s) sans liste de tarifs connue dans client_nomliste.xlsx, "
                f"rattache(s) a la liste technique « A corriger (liste inconnue) »."
            ))
        if clients_multi_listes:
            self.stdout.write(self.style.WARNING(
                f"{len(clients_multi_listes)} client(s) relie(s) a plusieurs listes de tarifs : "
                f"seule la premiere a ete retenue (IDCLIENTS legacy : {sorted(clients_multi_listes)[:30]}"
                f"{'...' if len(clients_multi_listes) > 30 else ''})."
            ))
