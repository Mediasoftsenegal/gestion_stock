from django.conf import settings
from django.db import models


class Parametrage(models.Model):
    """Parametres de l'entreprise (raison sociale, adresse, logo...) utilises
    sur les documents imprimes (factures, etc.). Ligne unique (singleton) :
    utiliser `Parametrage.get_solo()` plutot que `.objects.get(...)`.
    """

    nom_entreprise = models.CharField(max_length=200, default="Ets J.C.Tavarès")
    slogan = models.CharField(max_length=200, blank=True, default="Négoce · Gestion de stock")
    adresse = models.CharField(max_length=255, blank=True)
    telephone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    rccm = models.CharField("RCCM", max_length=60, blank=True)
    ninea = models.CharField("NINEA / identifiant fiscal", max_length=60, blank=True)
    logo = models.ImageField(
        upload_to="parametrage/", blank=True, null=True,
        help_text="Remplace le logo par défaut (static/img/logo.jpg) si renseigné.",
    )

    class Meta:
        verbose_name = "paramétrage de l'entreprise"
        verbose_name_plural = "paramétrage de l'entreprise"

    def __str__(self):
        return self.nom_entreprise

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SequenceCompteur(models.Model):
    """Compteur verrouillable utilise par `core.utils.generer_reference` pour
    generer des references (BC-2026-0001, FAC-2026-0001...) sans doublon
    meme en cas de creations concurrentes (plusieurs utilisateurs sur le
    reseau validant au meme moment).
    """

    cle = models.CharField(max_length=30, unique=True, help_text="Ex: 'BC-2026', 'FAC-2026'.")
    dernier_numero = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "compteur de référence"
        verbose_name_plural = "compteurs de référence"

    def __str__(self):
        return f"{self.cle} → {self.dernier_numero}"


class TimeStampedModel(models.Model):
    """Modele abstrait : tracabilite (creation / modification / auteur)."""

    cree_le = models.DateTimeField("cree le", auto_now_add=True)
    modifie_le = models.DateTimeField("modifie le", auto_now=True)
    cree_par = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="cree par",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        abstract = True
