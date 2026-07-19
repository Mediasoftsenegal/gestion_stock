from django.db import models

from core.models import TimeStampedModel


class CategorieClient(TimeStampedModel):
    nom_categorie = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name = "categorie de client"
        verbose_name_plural = "categories de clients"
        ordering = ["nom_categorie"]

    def __str__(self):
        return self.nom_categorie


class ListeTarif(TimeStampedModel):
    """Regroupe un ou plusieurs clients partageant les memes tarifs.

    Un article peut donc avoir un prix different d'une liste a l'autre
    (voir catalogue.Tarif).
    """

    nomtarif = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "liste de tarifs"
        verbose_name_plural = "listes de tarifs"
        ordering = ["nomtarif"]

    def __str__(self):
        return self.nomtarif


class Client(TimeStampedModel):
    nomclient = models.CharField(max_length=200)
    adresse = models.CharField(max_length=255, blank=True)
    categorie = models.ForeignKey(
        CategorieClient, on_delete=models.PROTECT, related_name="clients"
    )
    codeclient = models.CharField(max_length=30, unique=True)
    telephone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    exempttva = models.BooleanField(default=False)
    avectva = models.BooleanField(default=True)
    béneficiaire = models.CharField(max_length=200, blank=True)
    liste_tarif = models.ForeignKey(
        ListeTarif, on_delete=models.PROTECT, related_name="clients"
    )

    actif = models.BooleanField(default=True)

    class Meta:
        verbose_name = "client"
        verbose_name_plural = "clients"
        ordering = ["nomclient"]

    def __str__(self):
        return f"{self.nomclient} ({self.codeclient})"

    @property
    def nom(self):
        return self.nomclient

