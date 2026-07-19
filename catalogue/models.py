from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from clients.models import ListeTarif
from core.models import TimeStampedModel


class TypeArticle(TimeStampedModel):
    nom = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name = "type d'article"
        verbose_name_plural = "types d'articles"
        ordering = ["nom"]

    def __str__(self):
        return self.nom


class Conditionnement(TimeStampedModel):
    """Unite de conditionnement, ex. « Casier 12 » = 12 unites de base.

    L'unite simple se represente par un conditionnement de nombre_unites = 1.
    """

    nom = models.CharField(max_length=60)
    nombre_unites = models.PositiveIntegerField(
        "nombre d'unites",
        help_text="Nombre d'unites de base contenues (ex. casier de 12 -> 12).",
    )

    class Meta:
        verbose_name = "conditionnement"
        verbose_name_plural = "conditionnements"
        ordering = ["nombre_unites", "nom"]

    def __str__(self):
        return f"{self.nom} ({self.nombre_unites})"
class categorie_article(models.Model):
    categories_articles = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name = "categorie d'article"
        verbose_name_plural = "categories d'articles"
        ordering = ["categories_articles"]

    def __str__(self):
        return self.categories_articles

class Article(TimeStampedModel):
    code = models.CharField(
        max_length=40, unique=True, blank=True,
        help_text="Laisser vide pour generer automatiquement un code (ART00001...).",
    )
    libarticle = models.CharField(max_length=200)
    categorie_articles = models.ForeignKey('categorie_article', on_delete=models.SET_NULL, null=True, blank=True)
    type_article = models.ForeignKey(
        TypeArticle,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="articles",
    )
    unite_base = models.CharField(max_length=30, default="unite")
    prix_achat = models.DecimalField(
        "prix d'achat (par unite de base)",
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text=(
            "Cout d'acquisition d'une unite de base. Sert de plancher : un prix "
            "de vente ne peut lui etre inferieur. Mis a jour automatiquement lors "
            "de la validation d'un approvisionnement chiffre."
        ),
    )
    conditionnements = models.ManyToManyField(
        Conditionnement,
        related_name="articles",
        blank=True,
        help_text="Conditionnements disponibles a la vente pour cet article.",
    )
    seuil_alerte = models.PositiveIntegerField(
        "seuil d'alerte", default=0, help_text="En unites de base."
    )
    image_article = models.ImageField(
        upload_to="articles/", blank=True, null=True, help_text="Image de l'article"
    )
    code_barre = models.CharField(max_length=100, blank=True, help_text="Code barre de l'article")
    prix_bouteille = models.DecimalField(
        "prix de vente par bouteille",
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Prix de vente par bouteille (ou unite de base).",
    )
    consignation = models.DecimalField(
        "consignation par bouteille",
        max_digits=12,
        decimal_places=0,
        default=0,
        help_text="Consignation par bouteille (ou unite de base).",
    )
    fournisseur = models.CharField(max_length=200, blank=True, help_text="Fournisseur de l'article")
    groupe = models.CharField(max_length=100, blank=True, help_text="Groupe de l'article")
    actif = models.BooleanField(default=True)

    class Meta:
        verbose_name = "article"
        verbose_name_plural = "articles"
        ordering = ["libarticle"]

    def __str__(self):
        return f"{self.libarticle} ({self.code})"

    def save(self, *args, **kwargs):
        if not self.code:
            from core.utils import generer_code

            self.code = generer_code("ART", Article)
        super().save(*args, **kwargs)

    @property
    def stock_actuel(self):
        """Stock disponible en unites de base (somme des mouvements)."""
        from stock.services import stock_unites

        return stock_unites(self)

    @property
    def sous_seuil(self):
        return self.seuil_alerte and self.stock_actuel <= self.seuil_alerte

    def prix_vente_minimum(self, conditionnement):
        """Prix de vente plancher pour un conditionnement (= cout d'achat).

        Le prix d'achat est stocke par unite de base ; on le rapporte au
        conditionnement pour pouvoir le comparer a un prix de vente.
        """
        facteur = conditionnement.nombre_unites if conditionnement else 1
        return (self.prix_achat or Decimal("0")) * facteur


class Tarif(TimeStampedModel):
    """Prix d'un article, pour une liste de tarifs et un conditionnement donnes.

    -> permet qu'un meme article vaille un prix different selon le client
       (via la liste de tarifs a laquelle il appartient).
    """

    liste_tarif = models.ForeignKey(
        ListeTarif, on_delete=models.CASCADE, related_name="tarifs"
    )
    article = models.ForeignKey(
        Article, on_delete=models.CASCADE, related_name="tarifs"
    )
    conditionnement = models.ForeignKey(
        Conditionnement, on_delete=models.PROTECT, related_name="tarifs"
    )
    prix_unitaire = models.DecimalField(
        "prix (par conditionnement)", max_digits=12, decimal_places=0, default=0
    )

    class Meta:
        verbose_name = "tarif"
        verbose_name_plural = "tarifs"
        unique_together = ("liste_tarif", "article", "conditionnement")
        ordering = ["id"]

    def clean(self):
        super().clean()
        if self.article_id and self.conditionnement_id and self.prix_unitaire is not None:
            plancher = self.article.prix_vente_minimum(self.conditionnement)
            if plancher and self.prix_unitaire < plancher:
                raise ValidationError(
                    {
                        "prix_unitaire": (
                            f"Le prix de vente ({self.prix_unitaire}) ne peut etre "
                            f"inferieur au prix d'achat ({plancher}) pour "
                            f"{self.conditionnement}."
                        )
                    }
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.article} - {self.conditionnement} : {self.prix_unitaire}"


def get_prix(liste_tarif, article, conditionnement):
    """Retourne le prix defini, ou None si aucun tarif n'est renseigne."""
    tarif = Tarif.objects.filter(
        liste_tarif=liste_tarif, article=article, conditionnement=conditionnement
    ).first()
    return tarif.prix_unitaire if tarif else None
