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
    def montant_brut(self):
        """Montant HT avant remise."""
        return sum((l.montant_brut for l in self.lignes.all()), Decimal("0"))

    @property
    def montant_remise(self):
        """Total des remises accordees (remise unitaire x quantite)."""
        return sum((l.montant_remise for l in self.lignes.all()), Decimal("0"))

    @property
    def montant_ht(self):
        """Montant HT remise deduite (les prix de la liste de tarifs sont HT)."""
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
            if plancher and ligne.prix_net < plancher:
                raise ValidationError(
                    f"Prix de vente remise deduite ({ligne.prix_net}) inferieur au prix "
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
                remise_unitaire=ligne.remise_unitaire,
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
    remise_unitaire = models.DecimalField(
        "remise unitaire",
        max_digits=12, decimal_places=2, default=Decimal("0"), blank=True,
        help_text=(
            "Remise accordee sur une unite de conditionnement. "
            "Le montant de la remise de la ligne = remise unitaire x quantite."
        ),
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
        # Remise : montant par conditionnement, jamais negative ni superieure au prix.
        if self.remise_unitaire in (None, ""):
            self.remise_unitaire = Decimal("0")
        self.remise_unitaire = Decimal(self.remise_unitaire)
        if self.remise_unitaire < 0:
            raise ValidationError("La remise ne peut pas etre negative.")
        if self.prix_unitaire not in (None, "") and self.remise_unitaire > Decimal(self.prix_unitaire):
            raise ValidationError(
                f"La remise ({self.remise_unitaire}) ne peut pas depasser le prix "
                f"unitaire ({self.prix_unitaire}) pour {self.article}."
            )
        # Garde-fou : un prix de vente (remise deduite) ne peut etre inferieur
        # au prix d'achat.
        if self.prix_unitaire not in (None, "") and self.article_id and self.conditionnement_id:
            plancher = self.article.prix_vente_minimum(self.conditionnement)
            prix_net = Decimal(self.prix_unitaire) - self.remise_unitaire
            if plancher and prix_net < plancher:
                raise ValidationError(
                    f"Prix de vente remise deduite ({prix_net}) inferieur au prix d'achat "
                    f"({plancher}) pour {self.article} / {self.conditionnement}."
                )
        super().save(*args, **kwargs)

    @property
    def quantite_unites(self):
        return self.quantite * self.conditionnement.nombre_unites

    @property
    def montant_brut(self):
        """Montant avant remise."""
        return self.quantite * self.prix_unitaire

    @property
    def montant_remise(self):
        """Montant de la remise de la ligne = remise unitaire x quantite."""
        return self.quantite * (self.remise_unitaire or Decimal("0"))

    @property
    def prix_net(self):
        return Decimal(self.prix_unitaire) - (self.remise_unitaire or Decimal("0"))

    @property
    def montant(self):
        """Montant net de la ligne (remise deduite)."""
        return self.montant_brut - self.montant_remise

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

    # --- Depot-vente ---
    @property
    def est_depot_vente(self):
        """La facture concerne un client en depot-vente : elle appelle une
        facture de retour (rappel des quantites facturees + saisie des
        quantites vendues)."""
        return bool(self.client) and self.client.est_depot_vente

    @property
    def retour_valide(self):
        """Facture de retour validee, si elle existe."""
        retour = getattr(self, "retour", None)
        if retour and retour.statut == FactureRetour.Statut.VALIDEE:
            return retour
        return None

    # --- Montants (derives des lignes et des paiements) ---
    @property
    def montant_brut(self):
        """Montant HT avant remise, sur les quantites facturees."""
        return sum((l.montant_brut for l in self.lignes.all()), Decimal("0"))

    @property
    def montant_remise(self):
        """Total des remises accordees (remise unitaire x quantite)."""
        return sum((l.montant_remise for l in self.lignes.all()), Decimal("0"))

    @property
    def montant_ht_facture(self):
        """Montant HT facture a la sortie (remise deduite, avant retour)."""
        return sum((l.montant for l in self.lignes.all()), Decimal("0"))

    @property
    def montant_ht(self):
        """Montant HT reellement du : sur les quantites vendues si une facture
        de retour a ete validee (depot-vente), sinon sur les quantites
        facturees."""
        return sum((l.montant_du for l in self.lignes.all()), Decimal("0"))

    @property
    def montant_retour_ht(self):
        """Montant HT deduit par la facture de retour (marchandise reprise)."""
        return self.montant_ht_facture - self.montant_ht

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
            # Si une facture de retour a deja reintegre l'ecart (depot-vente),
            # on ne restaure que les quantites encore dues.
            unites = ligne.quantite_due * ligne.conditionnement.nombre_unites
            if not unites:
                continue
            enregistrer_mouvement(
                article=ligne.article,
                type_mouvement=TypeMouvement.RETOUR_VENTE,
                quantite_unites_signee=unites,
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
    remise_unitaire = models.DecimalField(
        "remise unitaire",
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Montant de la remise de la ligne = remise unitaire x quantite.",
    )

    class Meta:
        verbose_name = "ligne de facture"
        verbose_name_plural = "lignes de facture"

    @property
    def quantite_unites(self):
        return self.quantite * self.conditionnement.nombre_unites

    # --- Montants ---
    @property
    def montant_brut(self):
        """Montant avant remise."""
        return self.quantite * self.prix_unitaire

    @property
    def montant_remise(self):
        """Montant de la remise de la ligne = remise unitaire x quantite."""
        return self.quantite * (self.remise_unitaire or Decimal("0"))

    @property
    def prix_net(self):
        return Decimal(self.prix_unitaire) - (self.remise_unitaire or Decimal("0"))

    @property
    def montant(self):
        """Montant facture de la ligne (remise deduite)."""
        return self.montant_brut - self.montant_remise

    # --- Depot-vente : quantites vendues / retournees ---
    @property
    def ligne_retour(self):
        """Ligne de la facture de retour validee correspondante, s'il y en a une."""
        retour = self.facture.retour_valide
        if not retour:
            return None
        return next((lr for lr in retour.lignes.all() if lr.ligne_facture_id == self.id), None)

    @property
    def quantite_vendue(self):
        """Quantite reellement vendue par le depositaire (= quantite facturee
        tant qu'aucune facture de retour n'est validee)."""
        lr = self.ligne_retour
        return lr.quantite_vendue if lr else self.quantite

    @property
    def quantite_retournee(self):
        """Ecart entre quantite facturee et quantite vendue (reintegre en stock)."""
        return self.quantite - self.quantite_vendue

    @property
    def quantite_due(self):
        """Quantite effectivement due par le client."""
        return self.quantite_vendue

    @property
    def montant_du(self):
        """Montant du : quantite vendue x prix net (remise deduite)."""
        return self.quantite_due * self.prix_net

    @property
    def montant_retourne(self):
        return self.quantite_retournee * self.prix_net

    def __str__(self):
        return f"{self.article} x {self.quantite}"


# --------------------------------------------------------------------------- #
#  Facture de retour (clients en depot-vente)
# --------------------------------------------------------------------------- #
class FactureRetour(TimeStampedModel):
    """Facture de retour d'un client en depot-vente.

    Le client "DEPOT VENTE" recoit deux documents :
      1. la facture de sortie (facturation habituelle, remises comprises) ;
      2. cette facture de retour, qui rappelle les quantites facturees et
         permet de saisir les quantites reellement vendues.

    A la validation, l'ecart (facture - vendu) est reintegre en stock et
    deduit du montant du sur la facture de sortie : le depositaire ne paie
    que ce qu'il a vendu.
    """

    class Statut(models.TextChoices):
        BROUILLON = "BROUILLON", "En saisie"
        VALIDEE = "VALIDEE", "Validee"

    numero = models.CharField(max_length=60, unique=True, blank=True)
    facture = models.OneToOneField(
        Facture, on_delete=models.PROTECT, related_name="retour",
        help_text="Facture de sortie a laquelle ce retour se rapporte.",
    )
    date = models.DateField(default=timezone.localdate)
    statut = models.CharField(
        max_length=15, choices=Statut.choices, default=Statut.BROUILLON
    )
    note = models.TextField(blank=True)
    validee_le = models.DateTimeField(null=True, blank=True)
    validee_par = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "facture de retour (depot-vente)"
        verbose_name_plural = "factures de retour (depot-vente)"
        ordering = ["-date", "-cree_le"]
        permissions = [("peut_valider_retour", "Peut valider une facture de retour")]

    def __str__(self):
        return self.numero or f"Retour #{self.pk}"

    def save(self, *args, **kwargs):
        if not self.numero:
            from core.utils import generer_reference

            self.numero = generer_reference("FRT", FactureRetour, champ="numero")
        super().save(*args, **kwargs)

    # --- Creation ---
    @classmethod
    @transaction.atomic
    def creer_pour(cls, facture, user=None):
        """Ouvre la facture de retour d'une facture de sortie en depot-vente,
        avec rappel des quantites facturees ligne a ligne."""
        if not facture.est_depot_vente:
            raise ValidationError(
                "La facture de retour est reservee aux clients en depot-vente."
            )
        if facture.annulee:
            raise ValidationError("Cette facture est annulee.")
        if getattr(facture, "retour", None) is not None:
            raise ValidationError("Cette facture a deja une facture de retour.")

        retour = cls.objects.create(facture=facture, cree_par=user)
        for ligne in facture.lignes.all():
            LigneFactureRetour.objects.create(
                retour=retour, ligne_facture=ligne, quantite_vendue=0
            )
        return retour

    # --- Totaux ---
    @property
    def modifiable(self):
        return self.statut == self.Statut.BROUILLON and not self.facture.annulee

    @property
    def quantite_facturee_totale(self):
        return sum((l.quantite_facturee for l in self.lignes.all()), 0)

    @property
    def quantite_vendue_totale(self):
        return sum((l.quantite_vendue for l in self.lignes.all()), 0)

    @property
    def quantite_retournee_totale(self):
        return sum((l.quantite_retournee for l in self.lignes.all()), 0)

    @property
    def unites_retournees_totales(self):
        return sum((l.unites_retournees for l in self.lignes.all()), 0)

    @property
    def montant_facture_ht(self):
        """Montant HT initialement facture (remises deduites)."""
        return sum((l.montant_facture for l in self.lignes.all()), Decimal("0"))

    @property
    def montant_remise_total(self):
        return sum((l.montant_remise for l in self.lignes.all()), Decimal("0"))

    @property
    def montant_vendu_ht(self):
        return sum((l.montant_vendu for l in self.lignes.all()), Decimal("0"))

    @property
    def montant_retourne_ht(self):
        return sum((l.montant_retourne for l in self.lignes.all()), Decimal("0"))

    def _tva(self, base):
        if not self.facture.appliquer_tva:
            return Decimal("0")
        return (base * TAUX_TVA_VENTE).quantize(Decimal("0.01"))

    @property
    def montant_vendu_tva(self):
        return self._tva(self.montant_vendu_ht)

    @property
    def montant_vendu_ttc(self):
        """Montant reellement du par le depositaire apres retour."""
        return self.montant_vendu_ht + self.montant_vendu_tva

    @property
    def montant_retourne_tva(self):
        return self._tva(self.montant_retourne_ht)

    @property
    def montant_retourne_ttc(self):
        """Montant deduit de la facture de sortie (marchandise reprise)."""
        return self.montant_retourne_ht + self.montant_retourne_tva

    @property
    def montant_en_lettres(self):
        return _montant_en_lettres(self.montant_vendu_ttc)

    def decompte_conditionnements_retournes(self):
        """Decompte des conditionnements repris, du plus grand au plus petit."""
        totaux = OrderedDict()
        for ligne in self.lignes.all():
            c = ligne.conditionnement
            if c.id not in totaux:
                totaux[c.id] = {"conditionnement": c, "quantite": 0}
            totaux[c.id]["quantite"] += ligne.quantite_retournee
        return sorted(
            (d for d in totaux.values() if d["quantite"]),
            key=lambda d: -d["conditionnement"].nombre_unites,
        )

    def detail_calcul(self):
        """Fenetre de calcul : detail ligne a ligne (facture / vendu / retourne)
        et totaux, pour affichage et impression."""
        lignes = []
        for l in self.lignes.select_related(
            "ligne_facture__article", "ligne_facture__conditionnement"
        ):
            lignes.append(
                {
                    "ligne": l,
                    "article": l.article,
                    "conditionnement": l.conditionnement,
                    "prix_unitaire": l.prix_unitaire,
                    "remise_unitaire": l.remise_unitaire,
                    "prix_net": l.prix_net,
                    "quantite_facturee": l.quantite_facturee,
                    "quantite_vendue": l.quantite_vendue,
                    "quantite_retournee": l.quantite_retournee,
                    "unites_retournees": l.unites_retournees,
                    "montant_remise": l.montant_remise,
                    "montant_facture": l.montant_facture,
                    "montant_vendu": l.montant_vendu,
                    "montant_retourne": l.montant_retourne,
                }
            )
        return {
            "retour": self,
            "facture": self.facture,
            "client": self.facture.client,
            "lignes": lignes,
            "quantite_facturee_totale": self.quantite_facturee_totale,
            "quantite_vendue_totale": self.quantite_vendue_totale,
            "quantite_retournee_totale": self.quantite_retournee_totale,
            "unites_retournees_totales": self.unites_retournees_totales,
            "montant_remise_total": self.montant_remise_total,
            "montant_facture_ht": self.montant_facture_ht,
            "montant_vendu_ht": self.montant_vendu_ht,
            "montant_vendu_tva": self.montant_vendu_tva,
            "montant_vendu_ttc": self.montant_vendu_ttc,
            "montant_retourne_ht": self.montant_retourne_ht,
            "montant_retourne_tva": self.montant_retourne_tva,
            "montant_retourne_ttc": self.montant_retourne_ttc,
            "deja_paye": self.facture.montant_paye,
            "reste_a_payer": self.montant_vendu_ttc - self.facture.montant_paye,
            "decompte_retourne": self.decompte_conditionnements_retournes(),
        }

    # --- Saisie / validation ---
    @transaction.atomic
    def enregistrer_quantites(self, quantites, user=None):
        """Enregistre les quantites vendues saisies.

        `quantites` : dict {pk de LigneFactureRetour: quantite vendue}.
        """
        if not self.modifiable:
            raise ValidationError(
                "Cette facture de retour est validee : elle n'est plus modifiable."
            )
        for ligne in self.lignes.select_related("ligne_facture"):
            if ligne.pk not in quantites:
                continue
            valeur = quantites[ligne.pk]
            if valeur is None or valeur < 0:
                raise ValidationError(
                    f"Quantite vendue invalide pour {ligne.article}."
                )
            if valeur > ligne.quantite_facturee:
                raise ValidationError(
                    f"{ligne.article} : quantite vendue ({valeur}) superieure a la "
                    f"quantite facturee ({ligne.quantite_facturee})."
                )
            ligne.quantite_vendue = valeur
            ligne.save(update_fields=["quantite_vendue"])

    @transaction.atomic
    def valider(self, user=None):
        """Valide le retour : reintegre l'ecart en stock et ramene le montant
        du de la facture de sortie aux seules quantites vendues."""
        from stock.models import TypeMouvement
        from stock.services import enregistrer_mouvement

        if self.statut != self.Statut.BROUILLON:
            raise ValidationError("Cette facture de retour est deja validee.")
        if self.facture.annulee:
            raise ValidationError("La facture de sortie est annulee.")

        lignes = list(
            self.lignes.select_related(
                "ligne_facture__article", "ligne_facture__conditionnement"
            )
        )
        if not lignes:
            raise ValidationError("Facture de retour sans ligne.")
        for ligne in lignes:
            if ligne.quantite_vendue > ligne.quantite_facturee:
                raise ValidationError(
                    f"{ligne.article} : quantite vendue superieure a la quantite facturee."
                )

        # Le client ne paie que le vendu : les encaissements deja effectues ne
        # peuvent pas depasser ce nouveau montant.
        nouveau_total = self.montant_vendu_ttc
        deja_paye = self.facture.montant_paye
        if deja_paye > nouveau_total:
            raise ValidationError(
                f"Les encaissements deja enregistres ({deja_paye:.0f}) depassent le "
                f"montant du apres retour ({nouveau_total:.0f}). Regularisez les "
                f"paiements avant de valider le retour."
            )

        for ligne in lignes:
            unites = ligne.unites_retournees
            if not unites:
                continue
            enregistrer_mouvement(
                article=ligne.article,
                type_mouvement=TypeMouvement.RETOUR_VENTE,
                quantite_unites_signee=unites,
                reference=self.numero,
                motif=(
                    f"Retour depot-vente {self.numero} "
                    f"(facture {self.facture.numero})"
                ),
                user=user,
            )

        self.statut = self.Statut.VALIDEE
        self.validee_le = timezone.now()
        self.validee_par = user
        self.save(update_fields=["statut", "validee_le", "validee_par", "modifie_le"])


class LigneFactureRetour(models.Model):
    retour = models.ForeignKey(
        FactureRetour, on_delete=models.CASCADE, related_name="lignes"
    )
    ligne_facture = models.ForeignKey(
        LigneFacture, on_delete=models.PROTECT, related_name="retours"
    )
    quantite_vendue = models.PositiveIntegerField(
        "quantite vendue", default=0,
        help_text="Quantite reellement vendue par le depositaire.",
    )

    class Meta:
        verbose_name = "ligne de facture de retour"
        verbose_name_plural = "lignes de facture de retour"
        constraints = [
            models.UniqueConstraint(
                fields=["retour", "ligne_facture"], name="unique_ligne_retour"
            )
        ]

    def __str__(self):
        return f"{self.article} : {self.quantite_vendue}/{self.quantite_facturee}"

    def clean(self):
        if self.quantite_vendue > self.quantite_facturee:
            raise ValidationError(
                "La quantite vendue ne peut pas depasser la quantite facturee."
            )

    # --- Rappel de la ligne facturee ---
    @property
    def article(self):
        return self.ligne_facture.article

    @property
    def conditionnement(self):
        return self.ligne_facture.conditionnement

    @property
    def quantite_facturee(self):
        return self.ligne_facture.quantite

    @property
    def prix_unitaire(self):
        return self.ligne_facture.prix_unitaire

    @property
    def remise_unitaire(self):
        return self.ligne_facture.remise_unitaire

    @property
    def prix_net(self):
        return self.ligne_facture.prix_net

    @property
    def montant_remise(self):
        return self.ligne_facture.montant_remise

    @property
    def montant_facture(self):
        return self.ligne_facture.montant

    # --- Ecart et montants ---
    @property
    def quantite_retournee(self):
        return self.quantite_facturee - self.quantite_vendue

    @property
    def unites_retournees(self):
        return self.quantite_retournee * self.conditionnement.nombre_unites

    @property
    def montant_vendu(self):
        return self.quantite_vendue * self.prix_net

    @property
    def montant_retourne(self):
        return self.quantite_retournee * self.prix_net


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
