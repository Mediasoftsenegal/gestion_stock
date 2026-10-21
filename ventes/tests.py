import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from catalogue.models import Article, Conditionnement, Tarif
from clients.models import CategorieClient, Client, ListeTarif

from . import services
from .models import Commande, LigneCommande

User = get_user_model()


class ImportCommandesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("vendeur", password="x")
        self.user.user_permissions.set(Permission.objects.filter(codename="add_commande"))
        self.client.force_login(self.user)

        self.liste = ListeTarif.objects.create(nomtarif="Gros")
        self.categorie = CategorieClient.objects.create(nom_categorie="Boutiques")
        self.client_a = Client.objects.create(
            nomclient="Boutique A", codeclient="CL-0001", categorie=self.categorie, liste_tarif=self.liste,
        )
        self.article1 = Article.objects.create(libarticle="Bissap 33cl", code="ART00001", prix_achat=Decimal("500"))
        self.article2 = Article.objects.create(libarticle="Gazelle 1L", code="ART00002", prix_achat=Decimal("800"))
        self.cond24 = Conditionnement.objects.create(nom="Casier 24", nombre_unites=24)
        self.cond12 = Conditionnement.objects.create(nom="Carton 12", nombre_unites=12)
        Tarif.objects.create(liste_tarif=self.liste, article=self.article1, conditionnement=self.cond24,
                             prix_unitaire=Decimal("15000"))
        Tarif.objects.create(liste_tarif=self.liste, article=self.article2, conditionnement=self.cond12,
                             prix_unitaire=Decimal("9600"))

    def _entetes(self):
        return [
            "GROUPE (REGROUPE LES LIGNES DE LA MEME COMMANDE)", "CLIENT (CODE)", "DATE (JJ/MM/AAAA)",
            "ARTICLE (CODE OU LIBELLE)", "CONDITIONNEMENT", "QUANTITE", "REMISE UNITAIRE",
            "APPLIQUER TVA (OUI/NON)", "NOTE", "MONTANT TOTAL ATTENDU (VERIFICATION, OPTIONNEL)",
        ]

    def _lignes(self, *rows):
        entetes = self._entetes()
        return [dict(zip(entetes, row)) for row in rows]

    def test_regroupement_de_deux_lignes_en_une_commande(self):
        lignes = self._lignes(
            ("CMD1", "CL-0001", "15/01/2026", "ART00001", "Casier 24", "10", "0", "Oui", "", ""),
            ("CMD1", "CL-0001", "15/01/2026", "ART00002", "Carton 12", "5", "0", "", "", ""),
        )
        resultats, stats = services.analyser_commandes(lignes)
        self.assertEqual(len(resultats), 1)
        self.assertEqual(stats["commandes"], 1)
        self.assertEqual(stats["lignes"], 2)
        self.assertEqual(resultats[0]["montant_ht"], Decimal("15000") * 10 + Decimal("9600") * 5)

    def test_ligne_sans_groupe_devient_sa_propre_commande(self):
        lignes = self._lignes(
            ("", "CL-0001", "15/01/2026", "ART00001", "Casier 24", "1", "0", "", "", ""),
            ("", "CL-0001", "15/01/2026", "ART00002", "Carton 12", "1", "0", "", "", ""),
        )
        resultats, stats = services.analyser_commandes(lignes)
        self.assertEqual(len(resultats), 2)
        self.assertEqual(stats["commandes"], 2)

    def test_client_inconnu_est_une_erreur(self):
        lignes = self._lignes(("", "INEXISTANT", "", "ART00001", "Casier 24", "1", "0", "", "", ""))
        resultats, stats = services.analyser_commandes(lignes)
        self.assertEqual(stats["erreurs"], 1)
        self.assertIn("Client inconnu", resultats[0]["erreurs"][0])

    def test_article_sans_tarif_est_une_erreur(self):
        # cond12 n'a pas de tarif pour article1
        lignes = self._lignes(("", "CL-0001", "", "ART00001", "Carton 12", "1", "0", "", "", ""))
        resultats, stats = services.analyser_commandes(lignes)
        self.assertEqual(stats["erreurs"], 1)
        self.assertIn("Aucun tarif", resultats[0]["lignes"][0]["erreurs"][0])

    def test_prix_sous_le_plancher_est_refuse(self):
        # plancher = prix_achat (500) * 24 = 12000 ; remise ramene le prix net a 4000
        lignes = self._lignes(("", "CL-0001", "", "ART00001", "Casier 24", "1", "11500", "", "", ""))
        resultats, stats = services.analyser_commandes(lignes)
        self.assertEqual(stats["erreurs"], 1)
        self.assertIn("inférieur au prix d'achat", resultats[0]["lignes"][0]["erreurs"][0])

    def test_ecart_de_montant_attendu_est_signale_sans_bloquer(self):
        lignes = self._lignes(
            ("CMD1", "CL-0001", "", "ART00001", "Casier 24", "1", "0", "", "", "999999"),
        )
        resultats, stats = services.analyser_commandes(lignes)
        self.assertEqual(stats["erreurs"], 0)
        self.assertEqual(stats["ecarts"], 1)
        self.assertTrue(resultats[0]["ecart"])

    def test_application_cree_la_commande_et_ses_lignes(self):
        lignes = self._lignes(
            ("CMD1", "CL-0001", "15/01/2026", "ART00001", "Casier 24", "10", "0", "Oui", "Note test", ""),
            ("CMD1", "CL-0001", "15/01/2026", "ART00002", "Carton 12", "5", "0", "", "", ""),
        )
        compteurs = services.appliquer_commandes_import(lignes)
        self.assertEqual(compteurs["commandes"], 1)
        self.assertEqual(compteurs["lignes"], 2)
        commande = Commande.objects.get(client=self.client_a)
        self.assertEqual(commande.date, dt.date(2026, 1, 15))
        self.assertEqual(commande.note, "Note test")
        self.assertTrue(commande.appliquer_tva)
        self.assertEqual(LigneCommande.objects.filter(commande=commande).count(), 2)

    def test_groupe_en_erreur_n_empeche_pas_les_autres(self):
        lignes = self._lignes(
            ("BAD", "INEXISTANT", "", "ART00001", "Casier 24", "1", "0", "", "", ""),
            ("OK", "CL-0001", "", "ART00001", "Casier 24", "1", "0", "", "", ""),
        )
        compteurs = services.appliquer_commandes_import(lignes)
        self.assertEqual(compteurs["commandes"], 1)
        self.assertEqual(compteurs["ignorees"], 1)

    def test_vue_apercu_bout_en_bout(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        contenu = services.generer_modele_commandes()
        fichier = SimpleUploadedFile(
            "import.xlsx", contenu,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        r = self.client.post(reverse("ventes:commande_import"), {"fichier": fichier})
        self.assertRedirects(r, reverse("ventes:commande_import_apercu"))

        r2 = self.client.post(reverse("ventes:commande_import_apercu"), {"action": "appliquer"})
        self.assertRedirects(r2, reverse("ventes:commande_liste"))
        self.assertTrue(Commande.objects.filter(client=self.client_a).exists())


class ImportFacturesTests(TestCase):
    def setUp(self):
        from stock.models import TypeMouvement
        from stock.services import enregistrer_mouvement

        self.user = User.objects.create_user("vendeur", password="x")
        self.user.user_permissions.set(Permission.objects.filter(codename="peut_valider_commande"))
        self.client.force_login(self.user)

        self.liste = ListeTarif.objects.create(nomtarif="Gros")
        self.categorie = CategorieClient.objects.create(nom_categorie="Boutiques")
        self.client_a = Client.objects.create(
            nomclient="Boutique A", codeclient="CL-0001", categorie=self.categorie, liste_tarif=self.liste,
        )
        self.article1 = Article.objects.create(libarticle="Bissap 33cl", code="ART00001", prix_achat=Decimal("500"))
        self.article2 = Article.objects.create(libarticle="Gazelle 1L", code="ART00002", prix_achat=Decimal("800"))
        self.cond24 = Conditionnement.objects.create(nom="Casier 24", nombre_unites=24)
        self.cond12 = Conditionnement.objects.create(nom="Carton 12", nombre_unites=12)
        Tarif.objects.create(liste_tarif=self.liste, article=self.article1, conditionnement=self.cond24,
                             prix_unitaire=Decimal("15000"))
        Tarif.objects.create(liste_tarif=self.liste, article=self.article2, conditionnement=self.cond12,
                             prix_unitaire=Decimal("9600"))
        enregistrer_mouvement(self.article1, TypeMouvement.ENTREE_APPRO, 24 * 20, reference="INIT")
        enregistrer_mouvement(self.article2, TypeMouvement.ENTREE_APPRO, 12 * 20, reference="INIT")

    def _entetes(self):
        return [
            "GROUPE (REGROUPE LES LIGNES DE LA MEME COMMANDE)", "CLIENT (CODE)", "DATE (JJ/MM/AAAA)",
            "ARTICLE (CODE OU LIBELLE)", "CONDITIONNEMENT", "QUANTITE", "REMISE UNITAIRE",
            "APPLIQUER TVA (OUI/NON)", "NOTE", "MONTANT TOTAL ATTENDU (VERIFICATION, OPTIONNEL)",
        ]

    def _lignes(self, *rows):
        entetes = self._entetes()
        return [dict(zip(entetes, row)) for row in rows]

    def test_stock_suffisant_cree_valide_et_facture(self):
        lignes = self._lignes(("CMD1", "CL-0001", "15/01/2026", "ART00001", "Casier 24", "10", "0", "Oui", "", ""))
        compteurs = services.appliquer_factures_import(lignes, user=self.user)
        self.assertEqual(compteurs["factures"], 1)
        commande = Commande.objects.get(client=self.client_a)
        self.assertEqual(commande.statut, Commande.Statut.VALIDEE)
        self.assertTrue(commande.factures.exists())
        from stock.services import stock_unites
        self.assertEqual(stock_unites(self.article1), 24 * 20 - 24 * 10)

    def test_stock_insuffisant_est_signale_sans_rien_creer(self):
        lignes = self._lignes(("CMD1", "CL-0001", "", "ART00001", "Casier 24", "100", "0", "", "", ""))
        resultats, stats = services.analyser_factures(lignes)
        self.assertEqual(stats["erreurs"], 1)
        self.assertIn("Stock insuffisant", resultats[0]["lignes"][0]["erreurs"][0])

        compteurs = services.appliquer_factures_import(lignes, user=self.user)
        self.assertEqual(compteurs["factures"], 0)
        self.assertEqual(compteurs["ignorees"], 1)
        self.assertFalse(Commande.objects.exists())

    def test_vue_apercu_bout_en_bout(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        contenu = services.generer_modele_factures()
        fichier = SimpleUploadedFile(
            "import.xlsx", contenu,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        r = self.client.post(reverse("ventes:facture_import"), {"fichier": fichier})
        self.assertRedirects(r, reverse("ventes:facture_import_apercu"))

        r2 = self.client.post(reverse("ventes:facture_import_apercu"), {"action": "appliquer"})
        self.assertRedirects(r2, reverse("ventes:facture_liste"))
        self.assertTrue(Commande.objects.filter(client=self.client_a, statut=Commande.Statut.VALIDEE).exists())
