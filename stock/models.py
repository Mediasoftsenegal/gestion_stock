from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction

from catalogue.models import Article, Conditionnement
from core.models import TimeStampedModel
from core.utils import PERIODES

# Nom de la categorie d'article soumise a la T.B.A (SOBOA uniquement).
CATEGORIE_ALCOOL_NOM = "BOISSON ALCOLISEE"


class TypeMouvement(models.TextChoices):
    ENTREE_APPRO = "ENTREE_APPRO", "Entree (approvisionnement)"
    SORTIE_VENTE = "SORTIE_VENTE", "Sortie (vente)"
    RETOUR_VENTE = "RETOUR_VENTE", "Retour (annulation facture)"
    SORTIE_VOL = "SORTIE_VOL", "Sortie (vol)"
    SORTIE_CASSE = "SORTIE_CASSE", "Sortie (casse)"
    SORTIE_OFFERT = "SORTIE_OFFERT", "Sortie (offert)"
    AJUSTEMENT = "AJUSTEMENT", "Ajustement (inventaire)"


class MouvementStock(TimeStampedModel):
    """Source de verite unique du stock. Ne jamais modifier a la main :
    tout passe par un approvisionnement, une vente, une sortie ou un inventaire.
    """

    article = models.ForeignKey(
        Article, on_delete=models.PROTECT, related_name="mouvements"
    )
    type_mouvement = models.CharField(max_length=20, choices=TypeMouvement.choices)
    quantite_unites = models.IntegerField(
        "quantite (unites, signee)",
        help_text="Positif = entree, negatif = sortie.",
    )
    date = models.DateTimeField()
    reference = models.CharField(max_length=60, blank=True)
    motif = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "mouvement de stock"
        verbose_name_plural = "mouvements de stock"
        ordering = ["-date"]
        indexes = [models.Index(fields=["article", "date"])]

    def __str__(self):
        return f"{self.get_type_mouvement_display()} {self.quantite_unites} - {self.article}"


# --------------------------------------------------------------------------- #
#  Approvisionnement (entrees en stock)
# --------------------------------------------------------------------------- #
class Approvisionnement(TimeStampedModel):
    reference = models.CharField(max_length=60, unique=True, blank=True)
    date = models.DateField()
    fournisseur = models.ForeignKey(
        "Fournisseur", on_delete=models.PROTECT, related_name="approvisionnements"
    )
    note = models.TextField(blank=True)
    valide = models.BooleanField(default=False)
    taxes = models.ManyToManyField(
        'Taxe',
        related_name="approvisionnements",
        blank=True,
        help_text="Une ou plusieurs taxes applicables à cet approvisionnement (s'appliquent au montant HT)"
    )

    class Meta:
        verbose_name = "approvisionnement"
        verbose_name_plural = "approvisionnements"
        ordering = ["-date"]

    def __str__(self):
        return self.reference or f"Appro #{self.pk}"

    def save(self, *args, **kwargs):
        if not self.reference:
            from core.utils import generer_reference

            self.reference = generer_reference("APP", Approvisionnement)
        super().save(*args, **kwargs)

    # ----------------------------------------------------------------- #
    #  Montants (calcules a partir des lignes et des taxes selectionnees)
    # ----------------------------------------------------------------- #
    @property
    def montant_ht(self):
        """Total hors taxes : somme des lignes (prix d'achat x quantite, remise deduite)."""
        total = Decimal("0")
        for ligne in self.lignes.all():
            total += ligne.montant_ht
        return total

    @property
    def montant_ht_alcoolise(self):
        """Total HT des seules lignes de boissons alcoolisees (assiette de la TBA)."""
        total = Decimal("0")
        for ligne in self.lignes.select_related("article"):
            if ligne.est_alcoolise:
                total += ligne.montant_ht
        return total

    @property
    def taux_taxes(self):
        """Somme des taux de taxe (%) applicables (informationnel)."""
        total = Decimal("0")
        for taxe in self.taxes.all():
            total += taxe.valeur_taxe
        return total

    def detail_taxes(self):
        """Calcule la cascade de taxes et renvoie le détail complet.

        Cascade :
          - TBA   = HT alcoolisé              x taux TBA
          - TIGB  = (HT - HT alcoolisé)       x taux TIGB
          - TVA   = (HT + TBA + TIGB)         x taux TVA
          - Taxe d'égalisation : affichée mais NON calculée
          - Autres taxes éventuelles : sur le HT total
        TOTAL FACTURE = HT + TBA + TIGB + TVA (+ autres)
        """
        cents = Decimal("0.01")
        ht = self.montant_ht
        ht_alco = self.montant_ht_alcoolise
        taxes = list(self.taxes.all())

        def _taux(nature):
            for t in taxes:
                if t.nature == nature:
                    return t.valeur_taxe
            return None

        taux_tba = _taux(Taxe.Nature.TBA)
        taux_tigb = _taux(Taxe.Nature.TIGB)
        taux_tva = _taux(Taxe.Nature.TVA)
        egalisation = next(
            (t for t in taxes if t.nature == Taxe.Nature.EGALISATION), None
        )
        autres = [t for t in taxes if t.nature == Taxe.Nature.AUTRE]

        tba = (ht_alco * taux_tba / Decimal("100")).quantize(cents) if taux_tba is not None else None
        tba_v = tba or Decimal("0")

        tigb = ((ht - ht_alco) * taux_tigb / Decimal("100")).quantize(cents) if taux_tigb is not None else None
        tigb_v = tigb or Decimal("0")

        base_tva = ht + tba_v + tigb_v
        tva = (base_tva * taux_tva / Decimal("100")).quantize(cents) if taux_tva is not None else None
        tva_v = tva or Decimal("0")

        autres_montant = Decimal("0")
        for t in autres:
            autres_montant += (ht * t.valeur_taxe / Decimal("100")).quantize(cents)

        total_taxes = tba_v + tigb_v + tva_v + autres_montant
        return {
            "ht": ht,
            "ht_alcoolise": ht_alco,
            "taux_tba": taux_tba, "tba": tba,
            "taux_tigb": taux_tigb, "tigb": tigb,
            "base_tva": base_tva if taux_tva is not None else None,
            "taux_tva": taux_tva, "tva": tva,
            "egalisation": egalisation,
            "autres": autres_montant,
            "total_taxes": total_taxes,
            "total_facture": ht + total_taxes,
        }

    @property
    def montant_taxes(self):
        """Montant total des taxes (hors taxe d'égalisation, non calculée)."""
        return self.detail_taxes()["total_taxes"]

    @property
    def montant_ttc(self):
        """Total de la facture = HT + TBA + TIGB + TVA (+ autres)."""
        return self.detail_taxes()["total_facture"]

    @transaction.atomic
    def valider(self, user=None):
        """Genere les mouvements d'entree pour chaque ligne (idempotent)."""
        if self.valide:
            raise ValidationError("Approvisionnement deja valide.")
        from .services import enregistrer_mouvement

        for ligne in self.lignes.select_related("article", "conditionnement"):
            enregistrer_mouvement(
                article=ligne.article,
                type_mouvement=TypeMouvement.ENTREE_APPRO,
                quantite_unites_signee=ligne.quantite_unites,
                reference=self.reference,
                motif=f"Approvisionnement {self.reference}",
                user=user,
            )
            # Met a jour le prix d'achat de reference (dernier cout connu).
            pau = ligne.prix_achat_unitaire
            if pau is not None and pau != ligne.article.prix_achat:
                ligne.article.prix_achat = pau
                ligne.article.save(update_fields=["prix_achat_liquide", "modifie_le"])
        self.valide = True
        self.save(update_fields=["valide", "modifie_le"])


class LigneApprovisionnement(models.Model):
    approvisionnement = models.ForeignKey(
        Approvisionnement, on_delete=models.CASCADE, related_name="lignes"
    )
    article = models.ForeignKey(Article, on_delete=models.PROTECT)
    conditionnement = models.ForeignKey(
        Conditionnement, on_delete=models.PROTECT, null=True, blank=True
    )
    quantite = models.PositiveIntegerField(help_text="Quantité achetée.")
    prix_achat = models.DecimalField(
        "prix d'achat (par conditionnement)",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Optionnel. Si renseigne, met a jour le prix d'achat de reference "
            "de l'article (ramene a l'unite de base) lors de la validation."
        ),
    )
    remise = models.DecimalField(
        "remise (en %)", max_digits=5, decimal_places=0, default=0, help_text="En pourcentage."
    )
    nobre_bouteilles = models.PositiveIntegerField(
        "nombre de bouteilles (ou unites de base)", default=0, help_text="Nombre de bouteilles (ou unites de base) correspondant a la quantite achetee."
    )
    taxes = models.ManyToManyField(
        'Taxe',
        related_name="lignes_approvisionnement",
        blank=True,
        help_text="Une ou plusieurs taxes applicables a cette ligne"
    )

    class Meta:
        verbose_name = "ligne d'approvisionnement"
        verbose_name_plural = "lignes d'approvisionnement"

    @property
    def facteur(self):
        """Nombre d'unites de base (bouteilles) par conditionnement (casier/carton)."""
        return self.conditionnement.nombre_unites if self.conditionnement else 1

    @property
    def quantite_unites(self):
        """Nombre de bouteilles = quantite saisie x conditionnement."""
        return self.quantite * self.facteur

    @property
    def prix_achat_unitaire(self):
        """Prix d'achat ramene a l'unite de base, ou None."""
        if self.prix_achat is None:
            return None
        return self.prix_achat / self.facteur if self.facteur else self.prix_achat

    @property
    def montant_ht(self):
        """Montant HT de la ligne : prix d'achat x quantite, remise deduite."""
        if self.prix_achat is None:
            return Decimal("0")
        brut = self.prix_achat * self.quantite
        remise = (self.remise or Decimal("0")) / Decimal("100")
        return (brut * (Decimal("1") - remise)).quantize(Decimal("0.01"))

    @property
    def est_alcoolise(self):
        """Vrai si la ligne entre dans l'assiette de la T.B.A : fournisseur SOBOA
        ET article de categorie 'BOISSON ALCOLISEE'."""
        fournisseur = self.approvisionnement.fournisseur
        if not (fournisseur and fournisseur.nom_fournisseur == 'SOBOA'):
            return False
        cat = self.article.categorie_articles if self.article_id else None
        return bool(cat and cat.categories_articles == CATEGORIE_ALCOOL_NOM)

    def save(self, *args, **kwargs):
        # Le nombre de bouteilles decoule toujours de quantite x conditionnement.
        self.nobre_bouteilles = self.quantite_unites
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.article} x {self.quantite}"


class Taxe(models.Model):
    class Nature(models.TextChoices):
        TBA = "TBA", "TBA (boissons alcoolisées)"
        TIGB = "TIGB", "TIGB"
        TVA = "TVA", "TVA"
        EGALISATION = "EGALISATION", "Taxe d'égalisation"
        AUTRE = "AUTRE", "Autre"

    type_taxe = models.CharField(max_length=100)
    valeur_taxe = models.DecimalField(max_digits=5, decimal_places=2)
    nature = models.CharField(
        max_length=20,
        choices=Nature.choices,
        default=Nature.AUTRE,
        help_text=(
            "Détermine l'assiette et l'ordre de calcul dans la cascade : "
            "TBA (HT alcoolisé) → TIGB (HT − TBA) → TVA (HT + TBA + TIGB). "
            "« Taxe d'égalisation » est affichée mais non calculée."
        ),
    )
    sur_boissons_alcoolisees = models.BooleanField(
        "s'applique aux boissons alcoolisées uniquement",
        default=False,
        help_text="Renseigné automatiquement pour la TBA.",
    )

    class Meta:
        verbose_name = "taxe"
        verbose_name_plural = "taxes"

    def __str__(self):
        return f"{self.type_taxe} ({self.valeur_taxe}%)"


# --------------------------------------------------------------------------- #
#  Sorties exceptionnelles (vol, casse, offert)
# --------------------------------------------------------------------------- #
class SortieExceptionnelle(TimeStampedModel):
    class TypeSortie(models.TextChoices):
        VOL = TypeMouvement.SORTIE_VOL, "Vol"
        CASSE = TypeMouvement.SORTIE_CASSE, "Casse"
        OFFERT = TypeMouvement.SORTIE_OFFERT, "Offert"

    article = models.ForeignKey(
        Article, on_delete=models.PROTECT, related_name="sorties_exceptionnelles"
    )
    type_sortie = models.CharField(max_length=20, choices=TypeSortie.choices)
    conditionnement = models.ForeignKey(
        Conditionnement, on_delete=models.PROTECT, null=True, blank=True
    )
    quantite = models.PositiveIntegerField(help_text="Nombre de conditionnements.")
    motif = models.CharField(max_length=255)
    date = models.DateField()
    mouvement_genere = models.BooleanField(default=False, editable=False)

    class Meta:
        verbose_name = "sortie exceptionnelle"
        verbose_name_plural = "sorties exceptionnelles"
        ordering = ["-date"]

    def __str__(self):
        return f"{self.get_type_sortie_display()} - {self.article} x {self.quantite}"

    @property
    def quantite_unites(self):
        facteur = self.conditionnement.nombre_unites if self.conditionnement else 1
        return self.quantite * facteur

    def save(self, *args, **kwargs):
        creation = self._state.adding
        super().save(*args, **kwargs)
        if creation and not self.mouvement_genere:
            from .services import enregistrer_mouvement

            enregistrer_mouvement(
                article=self.article,
                type_mouvement=self.type_sortie,
                quantite_unites_signee=-self.quantite_unites,
                motif=f"{self.get_type_sortie_display()} : {self.motif}",
                user=self.cree_par,
                date=None,
            )
            SortieExceptionnelle.objects.filter(pk=self.pk).update(mouvement_genere=True)


# --------------------------------------------------------------------------- #
#  Inventaires physiques periodiques
# --------------------------------------------------------------------------- #
class Inventaire(TimeStampedModel):
    reference = models.CharField(max_length=60, unique=True, blank=True)
    date = models.DateField()
    periode = models.CharField(max_length=15, choices=PERIODES, default="MOIS")
    note = models.TextField(blank=True)
    valide = models.BooleanField(default=False)

    class Meta:
        verbose_name = "inventaire"
        verbose_name_plural = "inventaires"
        ordering = ["-date"]
        permissions = [("peut_importer_inventaire", "Peut importer un inventaire")]

    def __str__(self):
        return self.reference or f"Inventaire #{self.pk}"

    def save(self, *args, **kwargs):
        if not self.reference:
            from core.utils import generer_reference

            self.reference = generer_reference("INV", Inventaire)
        super().save(*args, **kwargs)

    @transaction.atomic
    def valider(self, user=None):
        """Fige le stock theorique, calcule l'ecart et genere les ajustements."""
        if self.valide:
            raise ValidationError("Inventaire deja valide.")
        from .services import enregistrer_mouvement, stock_unites

        for ligne in self.lignes.select_related("article"):
            theorique = stock_unites(ligne.article)
            ecart = ligne.quantite_physique - theorique
            ligne.quantite_theorique = theorique
            ligne.ecart = ecart
            ligne.save(update_fields=["quantite_theorique", "ecart"])
            if ecart != 0:
                enregistrer_mouvement(
                    article=ligne.article,
                    type_mouvement=TypeMouvement.AJUSTEMENT,
                    quantite_unites_signee=ecart,
                    reference=self.reference,
                    motif=f"Ajustement inventaire {self.reference}",
                    user=user,
                )
        self.valide = True
        self.save(update_fields=["valide", "modifie_le"])


class LigneInventaire(models.Model):
    inventaire = models.ForeignKey(
        Inventaire, on_delete=models.CASCADE, related_name="lignes"
    )
    article = models.ForeignKey(Article, on_delete=models.PROTECT)
    quantite_physique = models.IntegerField("quantite physique comptee (unites)")
    quantite_theorique = models.IntegerField(null=True, blank=True, editable=False)
    ecart = models.IntegerField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "ligne d'inventaire"
        verbose_name_plural = "lignes d'inventaire"
        unique_together = ("inventaire", "article")

    def __str__(self):
        return f"{self.article} : {self.quantite_physique}"

class Fournisseur(models.Model):
    ancien_id = models.IntegerField(
        "identifiant historique", null=True, blank=True, db_index=True, editable=False,
        help_text="IDFOURNISSEURS de l'ancien systeme (reprise de donnees).",
    )
    nom_fournisseur = models.CharField(max_length=100)
    adressefour = models.TextField(blank=True)
    emailfour = models.CharField("e-mail", max_length=120, blank=True)
    telephonefourn = models.CharField("telephone", max_length=50, blank=True)
    mobile = models.CharField(max_length=50, blank=True)
    fax = models.CharField(max_length=50, blank=True)
    typefour = models.CharField(max_length=50, blank=True)
    numerofour = models.CharField(max_length=50, blank=True)
    taxes = models.ManyToManyField(
        "Taxe",
        related_name="fournisseurs",
        blank=True,
        help_text="Taxes (TVA, etc.) applicables aux approvisionnements de ce fournisseur.",
    )

    class Meta:
        verbose_name = "fournisseur"
        verbose_name_plural = "fournisseurs"

    def __str__(self):
        return self.nom_fournisseur
