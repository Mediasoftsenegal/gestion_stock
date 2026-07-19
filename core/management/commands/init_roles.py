"""Cree les groupes (profils) et leur affecte les permissions.

Profils :
  - Administrateur : parametrage complet (clients, catalogue, tarifs, stock).
                     NB : il ne peut PAS supprimer de facture (regle appliquee
                     au niveau du modele et de l'admin, indeboulonnable).
  - Gestionnaire   : catalogue + stock + inventaires + approvisionnements.
  - Vendeur        : commandes + validation en facture.
  - Caissier       : encaissements + cloture de caisse.

L'annulation de facture (avec justificatif) est reservee aux profils
non-administrateurs, conformement a l'expression de besoin.

Usage : python manage.py init_roles
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand


def perms(*codenames):
    """Recupere les permissions par codename (silencieux si absente)."""
    trouvees = []
    for code in codenames:
        p = Permission.objects.filter(codename=code).first()
        if p:
            trouvees.append(p)
    return trouvees


class Command(BaseCommand):
    help = "Initialise les profils (groupes) et leurs permissions."

    def handle(self, *args, **options):
        catalogue = perms(
            *[f"{a}_{m}" for m in ("typearticle", "conditionnement", "article", "tarif")
              for a in ("add", "change", "view")]
        )
        stock_perms = perms(
            *[f"{a}_{m}" for m in ("approvisionnement", "ligneapprovisionnement",
                                    "sortieexceptionnelle", "inventaire", "ligneinventaire")
              for a in ("add", "change", "view", "delete")],
            "view_mouvementstock",
            "peut_importer_inventaire",
        )
        clients_perms = perms(
            *[f"{a}_{m}" for m in ("categorieclient", "listetarif", "client")
              for a in ("add", "change", "view")]
        )
        ventes_view = perms("view_commande", "view_facture", "view_lignecommande",
                            "view_lignefacture", "view_paiement")
        commandes = perms("add_commande", "change_commande", "view_commande",
                         "add_lignecommande", "change_lignecommande", "delete_lignecommande",
                         "peut_valider_commande")
        encaissement = perms("add_paiement", "view_paiement", "peut_encaisser",
                            "view_facture", "peut_cloturer_caisse", "view_caisse")
        annulation = perms("peut_annuler_facture")

        profils = {
            "Administrateur": clients_perms + catalogue + stock_perms + ventes_view
            + encaissement,
            "Gestionnaire": catalogue + stock_perms + perms("view_client", "view_listetarif"),
            "Vendeur": commandes + annulation + perms("view_client", "view_article",
                                                      "view_tarif", "view_facture"),
            "Caissier": encaissement + annulation + perms("view_client"),
        }

        for nom, permissions in profils.items():
            groupe, cree = Group.objects.get_or_create(name=nom)
            groupe.permissions.set(permissions)
            etat = "cree" if cree else "mis a jour"
            self.stdout.write(
                self.style.SUCCESS(f"Profil '{nom}' {etat} ({len(permissions)} permissions).")
            )

        self.stdout.write(
            self.style.WARNING(
                "Rappel : un superutilisateur contourne les permissions, mais la "
                "suppression de facture reste bloquee au niveau du modele."
            )
        )
