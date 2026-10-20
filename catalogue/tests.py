from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from clients.models import CategorieClient, Client, ListeTarif
from .models import Article, Conditionnement, Tarif

User = get_user_model()


class TarifModuleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("vendeur", password="x")
        perms = Permission.objects.filter(
            codename__in=["add_listetarif", "change_listetarif", "add_tarif", "change_tarif"]
        )
        self.user.user_permissions.set(perms)
        self.client.force_login(self.user)

        self.liste = ListeTarif.objects.create(nomtarif="Gros", description="Clients grossistes")
        self.article = Article.objects.create(libarticle="Bissap 33cl", prix_achat=Decimal("500"))
        self.cond = Conditionnement.objects.create(nom="Casier 24", nombre_unites=24)

    def test_liste_tarif_liste_accessible(self):
        r = self.client.get(reverse("clients:tarifs_liste"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Gros")

    def test_creation_liste_tarif(self):
        r = self.client.post(reverse("clients:tarif_nouvelle"), {
            "nomtarif": "Detail", "description": "Clients detail", "active": "on",
        })
        self.assertEqual(r.status_code, 302)
        self.assertTrue(ListeTarif.objects.filter(nomtarif="Detail").exists())

    def test_basculer_active(self):
        self.assertTrue(self.liste.active)
        self.client.post(reverse("clients:tarif_basculer", args=[self.liste.pk]))
        self.liste.refresh_from_db()
        self.assertFalse(self.liste.active)

    def test_detail_liste_sans_permission_ok_mais_creation_refusee(self):
        autre = User.objects.create_user("simple", password="x")
        self.client.force_login(autre)
        r = self.client.get(reverse("clients:tarif_detail", args=[self.liste.pk]))
        self.assertEqual(r.status_code, 200)  # lecture ouverte
        r2 = self.client.get(reverse("clients:tarif_nouvelle"))
        self.assertEqual(r2.status_code, 403)  # creation refusee sans droit

    def test_creation_tarif_depuis_liste(self):
        r = self.client.post(reverse("catalogue:tarif_nouveau"), {
            "liste_tarif": self.liste.pk, "article": self.article.pk,
            "conditionnement": self.cond.pk, "prix_unitaire": "15000",
        })
        self.assertEqual(r.status_code, 302)
        t = Tarif.objects.get(liste_tarif=self.liste, article=self.article, conditionnement=self.cond)
        self.assertEqual(t.prix_unitaire, Decimal("15000"))
        detail = self.client.get(reverse("clients:tarif_detail", args=[self.liste.pk]))
        self.assertContains(detail, "Bissap 33cl")
        self.assertContains(detail, "15 000")  # affichage via floatformat:0

    def test_tarif_sous_le_plancher_refuse(self):
        # prix_vente_minimum = prix_achat (500) * nombre_unites (24) = 12000
        r = self.client.post(reverse("catalogue:tarif_nouveau"), {
            "liste_tarif": self.liste.pk, "article": self.article.pk,
            "conditionnement": self.cond.pk, "prix_unitaire": "5000",
        })
        self.assertEqual(r.status_code, 200)  # reste sur le formulaire
        self.assertFalse(Tarif.objects.filter(liste_tarif=self.liste, article=self.article).exists())
