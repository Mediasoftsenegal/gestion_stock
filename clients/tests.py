from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from core.utils import lire_xlsx

from . import services
from .models import ListeTarif

User = get_user_model()


class ImportListesTarifsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("gestionnaire", password="x")
        self.user.user_permissions.set(Permission.objects.filter(codename="add_listetarif"))
        self.client.force_login(self.user)
        ListeTarif.objects.create(nomtarif="Gros", description="Ancienne description", active=True)

    def _lignes(self, *rows):
        entetes = ["NOM", "DESCRIPTION", "ACTIVE"]
        return [dict(zip(entetes, row)) for row in rows]

    def test_modele_telechargeable(self):
        r = self.client.get(reverse("clients:tarif_import_modele"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r["Content-Disposition"], 'attachment; filename="modele_listes_de_tarifs.xlsx"'
        )

    def test_modele_est_un_xlsx_lisible_avec_les_bonnes_colonnes(self):
        import io

        contenu = services.generer_modele()
        entetes, lignes = lire_xlsx(io.BytesIO(contenu))
        self.assertEqual(entetes, ["NOM", "DESCRIPTION", "ACTIVE"])
        self.assertTrue(lignes)  # lignes d'exemple presentes

    def test_creation_et_mise_a_jour(self):
        lignes = self._lignes(
            ("Gros", "Clients grossistes (maj)", "Oui"),  # mise a jour
            ("Detail", "Clients au detail", "Oui"),  # creation
        )
        resultats, stats = services.analyser(lignes)
        self.assertEqual(stats["creations"], 1)
        self.assertEqual(stats["mises_a_jour"], 1)

        compteurs = services.appliquer(lignes)
        self.assertEqual(compteurs["crees"], 1)
        self.assertEqual(compteurs["modifies"], 1)
        self.assertTrue(ListeTarif.objects.filter(nomtarif="Detail").exists())
        gros = ListeTarif.objects.get(nomtarif="Gros")
        self.assertEqual(gros.description, "Clients grossistes (maj)")

    def test_nom_manquant_est_une_erreur(self):
        lignes = self._lignes(("", "Sans nom", "Oui"))
        resultats, stats = services.analyser(lignes)
        self.assertEqual(stats["erreurs"], 1)
        self.assertIn("Nom manquant", resultats[0]["erreurs"][0])

    def test_doublon_dans_le_fichier_est_signale(self):
        lignes = self._lignes(("Semi-gros", "A", "Oui"), ("Semi-gros", "B", "Oui"))
        resultats, stats = services.analyser(lignes)
        self.assertEqual(stats["erreurs"], 1)
        self.assertIn("Doublon", resultats[1]["erreurs"][0])

    def test_vue_apercu_bout_en_bout(self):
        import io
        from django.core.files.uploadedfile import SimpleUploadedFile

        contenu = services.generer_modele()
        fichier = SimpleUploadedFile("import.xlsx", contenu, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        r = self.client.post(reverse("clients:tarif_import"), {"fichier": fichier})
        self.assertRedirects(r, reverse("clients:tarif_import_apercu"))

        r2 = self.client.post(reverse("clients:tarif_import_apercu"), {"action": "appliquer",
                                                                        "creer_nouveaux": "on", "mettre_a_jour": "on"})
        self.assertRedirects(r2, reverse("clients:tarifs_liste"))
        self.assertTrue(ListeTarif.objects.filter(nomtarif="Detail").exists())
