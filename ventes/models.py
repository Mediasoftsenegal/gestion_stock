from collections import OrderedDict
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.utils import timezone

from catalogue.models import Article, Conditionnement, get_prix
from clients.models import Client, ListeTarif
from core.models import TimeStampedModel
from core.utils import montant_en_lettres as _montant_en_lettres

TAUX_TVA_VENTE = Decimal("0.18")


def _soumis_tva(client):
    """Un client est soumis a la TVA s'il est marque 'avec TVA' et non exempte."""
    return bool(client) and client.avectva and not client.exempttva


def _decompte_conditionnements(lignes):
    """Agrege les lignes par conditionnement : [{'conditionnement': c, 'quantite': n}].

    `lignes` est un iterable d'objets ayant `.conditionnement` et `.quantite`
    (LigneCommande ou LigneFacture). Trie du plus grand au plus petit conditionnement.
    """
    totaux = OrderedDict()
    for ligne in lignes:
        c = ligne.conditionnement
        if c.id not in totaux:
            totaux[c.id] = {"conditionnement": c, "quantite": 0}
        totaux[c.id]["quantite"] += ligne.quantite
    return sorted(totaux.values(), key=lambda d: -d["conditionnement"].nombre_unites)


# --------------------------------------------------------------------------- #
#  Commande (bon de commande tant qu'elle n'est pas validee)
# --------------------------------------------------------------------------- #
class Commande(TimeStampedModel):
    class Statut(models.TextChoices):
        BON_DE_COMMANDE = "BON_DE_COMMANDE", "Bon de commande"
        VALIDEE = "VALIDEE", "Validee (facturee)"
        ANNULEE = "ANNULEE", "Annulee"

    reference = models.CharField(max_length=60, unique=True, blank=True)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="commandes")
    liste_tarif = models.ForeignKey(
        ListeTarif,
        on_delete=models.PROTECT,
        related_name="commandes",
        help_text="Copie de la liste de tarifs du client au moment de la commande.",
    )
    date = models.DateField(default=timezone.localdate)
    statut = models.CharField(
        max_length=20, choices=Statut.choices, default=Statut.BON_DE_COMMANDE
    )
    appliquer_tva = models.BooleanField(
        "Appliquer la TVA (18%)", default=True,
        help_text="Les prix de la liste de tarifs sont HT ; la TVA est ajoutee si cette case est cochee.",
    )
    note = models.TextField(blank=True)

    class Meta:
        verbose_name = "commande"
        verbose_name_plural = "commandes"
        ordering = ["-date", "-cree_le"]
        permissions = [("peut_valider_commande", "Peut valider une commande")]

    def __str__(self):
        return self.reference or f"Commande #{self.pk}"

    def save(self, *args, **kwargs):
        if not self.reference:
            from core.utils import generer_reference

            self.reference = generer_reference("BC", Commande)
        if not self.liste_tarif_id and self.client_id:
            self.liste_tarif = self.client.liste_tarif
        super().save(*args, **kwargs)

    @property
    def montant_ht(self):
        """Montant HT (les prix de la liste de tarifs sont HT)."""
        return sum((l.montant for l in self.lignes.all()), Decimal("0"))

    @property
    def soumis_tva(self):
        """Suggestion par defaut (a la creation) d'apres le client ; l'utilisateur
        peut ensuite l'activer/desactiver via `appliquer_tva`."""
        return _soumis_tva(self.client)

    @property
    def montant_tva(self):
        if not self.appliquer_tva:
            return Decimal("0")
        return (self.montant_ht * TAUX_TVA_VENTE).quantize(Decimal("0.01"))

    @property
    def montant_total(self):
        """Montant a payer : HT + TVA (si activee)."""
        return self.montant_ht + self.montant_tva

    @property
    def nombre_bouteilles(self):
        return sum((l.quantite_unites for l in self.lignes.all()), 0)

    def decompte_conditionnements(self):
        return _decompte_conditionnements(self.lignes.select_related("conditionnement"))

    @property
    def montant_en_lettres(self):
        return _montant_en_lettres(self.montant_total)

    @transaction.atomic
    def valider(self, user=None):
        """Fige le bon de commande et decremente le stock.

        Ne genere pas la facture : voir `generer_facture`. Une commande non
        validee reste un simple bon de commande.
        """
        from stock.models import TypeMouvement
        from stock.services import enregistrer_mouvement, stock_unites

        if self.statut != self.Statut.BON_DE_COMMANDE:
            raise ValidationError("Seul un bon de commande peut etre valide.")
        lignes = list(self.lignes.select_related("article", "conditionnement"))
        if not lignes:
            raise ValidationError("Impossible de valider une commande sans ligne.")

        for ligne in lignes:
            if stock_unites(ligne.article) < ligne.quantite_unites:
                raise ValidationError(
                    f"Stock insuffisant pour {ligne.article} "
                    f"(demande {ligne.quantite_unites} unites)."
                )
            plancher = ligne.article.prix_vente_minimum(ligne.conditionnement)
            if plancher and ligne.prix_unitaire < plancher:
                raise ValidationError(
                    f"Prix de vente ({ligne.prix_unitaire}) inferieur au prix "
                    f"d'achat ({plancher}) pour {ligne.article}."
                )

        for ligne in lignes:
            enregistrer_mouvement(
                article=ligne.article,
                type_mouvement=TypeMouvement.SORTIE_VENTE,
                quantite_unites_signee=-ligne.quantite_unites,
                reference=self.reference,
                motif=f"Vente {self.reference}",
                user=user,
            )

        self.statut = self.Statut.VALIDEE
        self.save(update_fields=["statut", "modifie_le"])

    @transaction.atomic
    def generer_facture(self, user=None):
        """Genere la facture (et ses lignes) a partir d'une commande validee.

        Le stock a deja ete decremente par `valider()` ; cette etape ne fait
        que produire le document de facturation.
        """
        if self.statut != self.Statut.VALIDEE:
            raise ValidationError("Seule une commande validee peut etre facturee.")
        if hasattr(self, "facture"):
            raise ValidationError("Cette commande a deja une facture.")

        lignes = list(self.lignes.select_related("article", "conditionnement"))
        from core.utils import generer_reference

        facture = Facture.objects.create(
            numero=generer_reference("FAC", Facture, champ="numero"),
            commande=self,
            client=self.client,
            appliquer_tva=self.appliquer_tva,
            cree_par=user,
        )
        for ligne in lignes:
            LigneFacture.objects.create(
                facture=facture,
                article=ligne.article,
                conditionnement=ligne.conditionnement,
                quantite=ligne.quantite,
                prix_unitaire=ligne.prix_unitaire,
            )
        return facture


class LigneCommande(models.Model):
    commande = models.ForeignKey(Commande, on_delete=models.CASCADE, related_name="lignes")
    article = models.ForeignKey(Article, on_delete=models.PROTECT)
    conditionnement = models.ForeignKey(Conditionnement, on_delete=models.PROTECT)
    quantite = models.PositiveIntegerField(help_text="Nombre de conditionnements.")
    prix_unitaire = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True,
        help_text="Renseigne automatiquement depuis la liste de tarifs si laisse vide.",
    )

    class Meta:
        verbose_name = "ligne de commande"
        verbose_name_plural = "lignes de commande"

    def save(self, *args, **kwargs):
        if self.prix_unitaire in (None, "") and self.commande_id:
            prix = get_prix(self.commande.liste_tarif, self.article, self.conditionnement)
            if prix is None:
                raise ValidationError(
                    f"Aucun tarif defini pour {self.article} / {self.conditionnement} "
                    f"dans la liste {self.commande.liste_tarif}."
                )
            self.prix_unitaire = prix
        # Garde-fou : un prix de vente ne peut etre inferieur au prix d'achat.
        if self.prix_unitaire not in (None, "") and self.article_id and self.conditionnement_id:
            plancher = self.article.prix_vente_minimum(self.conditionnement)
            if plancher and Decimal(self.prix_unitaire) < plancher:
                raise ValidationError(
                    f"Prix de vente ({self.prix_unitaire}) inferieur au prix d'achat "
                    f"({plancher}) pour {self.article} / {self.conditionnement}."
                )
        super().save(*args, **kwargs)

    @property
    def quantite_unites(self):
        return self.quantite * self.conditionnement.nombre_unites

    @property
    def montant(self):
        return self.quantite * self.prix_unitaire

    def __str__(self):
        return f"{self.article} x {self.quantite}"


# --------------------------------------------------------------------------- #
#  Facture (non supprimable ; annulable avec justificatif)
# --------------------------------------------------------------------------- #
class Facture(TimeStampedModel):
    class StatutPaiement(models.TextChoices):
        NON_PAYEE = "NON_PAYEE", "Non payee"
        PARTIELLE = "PARTIELLE", "Partiellement payee"
        PAYEE = "PAYEE", "Payee"

    numero = models.CharField(max_length=60, unique=True, blank=True)
    commande = models.OneToOneField(
        Commande, on_delete=models.PROTECT, related_name="facture"
    )
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="factures")
    date = models.DateTimeField(default=timezone.now)
    appliquer_tva = models.BooleanField("Appliquer la TVA (18%)", default=True)

    annulee = models.BooleanField(default=False)
    justificatif_annulation = models.TextField(blank=True)
    annulee_par = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    annulee_le = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "facture"
        verbose_name_plural = "factures"
        ordering = ["-date"]
        permissions = [("peut_annuler_facture", "Peut annuler une facture")]

    def __str__(self):
        return self.numero or f"Facture #{self.pk}"

    # --- Montants (derives des lignes et des paiements) ---
    @property
    def montant_ht(self):
        """Montant HT (les prix de la liste de tarifs sont HT)."""
        return sum((l.montant for l in self.lignes.all()), Decimal("0"))

    @property
    def soumis_tva(self):
        return _soumis_tva(self.client)

    @property
    def montant_tva(self):
        if not self.appliquer_tva:
            return Decimal("0")
        return (self.montant_ht * TAUX_TVA_VENTE).quantize(Decimal("0.01"))

    @property
    def montant_total(self):
        """Montant a payer : HT + TVA (si activee)."""
        return self.montant_ht + self.montant_tva

    @property
    def nombre_bouteilles(self):
        return sum((l.quantite_unites for l in self.lignes.all()), 0)

    def decompte_conditionnements(self):
        return _decompte_conditionnements(self.lignes.select_related("conditionnement"))

    @property
    def montant_en_lettres(self):
        return _montant_en_lettres(self.montant_total)

    @property
    def montant_paye(self):
        return sum((p.montant for p in self.paiements.all()), Decimal("0"))

    @property
    def reliquat(self):
        if self.annulee:
            return Decimal("0")
        return self.montant_total - self.montant_paye

    @property
    def statut_paiement(self):
        if self.montant_paye <= 0:
            return self.StatutPaiement.NON_PAYEE
        if self.montant_paye < self.montant_total:
            return self.StatutPaiement.PARTIELLE
        return self.StatutPaiement.PAYEE

    # --- Regles de gestion ---
    def delete(self, *args, **kwargs):
        raise PermissionDenied(
            "Les factures ne peuvent pas etre supprimees. "
            "Utilisez l'annulation avec justificatif."
        )

    @transaction.atomic
    def annuler(self, user, justificatif):
        """Annule la facture (justificatif obligatoire) et restaure le stock."""
        from stock.models import TypeMouvement
        from stock.services import enregistrer_mouvement

        if self.annulee:
            raise ValidationError("Facture deja annulee.")
        if not justificatif or not justificatif.strip():
            raise ValidationError("Un justificatif est obligatoire pour annuler.")

        for ligne in self.lignes.select_related("article", "conditionnement"):
            enregistrer_mouvement(
                article=ligne.article,
                type_mouvement=TypeMouvement.RETOUR_VENTE,
                quantite_unites_signee=ligne.quantite_unites,
                reference=self.numero,
                motif=f"Annulation {self.numero} : {justificatif}",
                user=user,
            )
        self.annulee = True
        self.justificatif_annulation = justificatif
        self.annulee_par = user
        self.annulee_le = timezone.now()
        self.save(
            update_fields=[
                "annulee",
                "justificatif_annulation",
                "annulee_par",
                "annulee_le",
                "modifie_le",
            ]
        )


class LigneFacture(models.Model):
    facture = models.ForeignKey(Facture, on_delete=models.PROTECT, related_name="lignes")
    article = models.ForeignKey(Article, on_delete=models.PROTECT)
    conditionnement = models.ForeignKey(Conditionnement, on_delete=models.PROTECT)
    quantite = models.PositiveIntegerField()
    prix_unitaire = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = "ligne de facture"
        verbose_name_plural = "lignes de facture"

    @property
    def quantite_unites(self):
        return self.quantite * self.conditionnement.nombre_unites

    @property
    def montant(self):
        return self.quantite * self.prix_unitaire

    def __str__(self):
        return f"{self.article} x {self.quantite}"


# --------------------------------------------------------------------------- #
#  Paiement (comptant ou acompte)
# --------------------------------------------------------------------------- #
class Paiement(TimeStampedModel):
    class TypePaiement(models.TextChoices):
        COMPTANT = "COMPTANT", "Comptant"
        ACOMPTE = "ACOMPTE", "Acompte"

    class Mode(models.TextChoices):
        ESPECES = "ESPECES", "Especes"
        MOBILE = "MOBILE", "Mobile money"
        CHEQUE = "CHEQUE", "Cheque"
        VIREMENT = "VIREMENT", "Virement"

    facture = models.ForeignKey(Facture, on_delete=models.PROTECT, related_name="paiements")
    type_paiement = models.CharField(max_length=15, choices=TypePaiement.choices)
    mode = models.CharField(max_length=15, choices=Mode.choices, default=Mode.ESPECES)
    montant = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateTimeField(default=timezone.now)
    reference = models.CharField(max_length=60, blank=True)

    class Meta:
        verbose_name = "paiement"
        verbose_name_plural = "paiements"
        ordering = ["-date"]
        permissions = [("peut_encaisser", "Peut encaisser un paiement")]

    def __str__(self):
        return f"{self.get_type_paiement_display()} {self.montant} - {self.facture}"

    def clean(self):
        if self.montant is None or self.montant <= 0:
            raise ValidationError("Le montant doit etre positif.")
        if self.facture_id and self.facture.annulee:
            raise ValidationError("Impossible d'encaisser sur une facture annulee.")
        if self.facture_id:
            deja = self.facture.montant_paye
            if self._state.adding is False and self.pk:
                deja -= Paiement.objects.get(pk=self.pk).montant
            if self.montant > (self.facture.montant_total - deja):
                raise ValidationError(
                    "Le montant depasse le reliquat de la facture."
                )
