from decimal import Decimal

from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import TimeStampedModel


class Caisse(TimeStampedModel):
    """Cloture de caisse d'une journee (calculee automatiquement).

    - total_encaisse : argent effectivement recu ce jour (dates de paiement) ;
    - total_facture  : total facture ce jour (factures non annulees) ;
    - total_non_paye : reliquat des factures du jour ;
    - reliquat_global: reliquat cumule sur toutes les factures ouvertes.
    """

    date = models.DateField(unique=True)
    total_facture = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_encaisse = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_non_paye = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    reliquat_global = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    nombre_factures = models.PositiveIntegerField(default=0)
    cloturee = models.BooleanField(default=False)
    cloturee_le = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "caisse"
        verbose_name_plural = "caisses"
        ordering = ["-date"]
        permissions = [("peut_cloturer_caisse", "Peut cloturer la caisse")]

    def __str__(self):
        return f"Caisse du {self.date}"

    @classmethod
    def calculer(cls, date=None):
        """Calcule les totaux de caisse d'une journee sans rien enregistrer
        (consultation en temps reel, y compris pour une date non encore
        cloturee)."""
        from ventes.models import Facture, Paiement

        date = date or timezone.localdate()

        factures_jour = list(
            Facture.objects.filter(date__date=date, annulee=False).prefetch_related(
                "lignes", "paiements"
            )
        )
        total_facture = sum((f.montant_total for f in factures_jour), Decimal("0"))
        total_non_paye = sum((f.reliquat for f in factures_jour), Decimal("0"))

        total_encaisse = Paiement.objects.filter(date__date=date).aggregate(
            s=Sum("montant")
        )["s"] or Decimal("0")

        reliquat_global = sum(
            (f.reliquat for f in Facture.objects.filter(annulee=False).prefetch_related(
                "lignes", "paiements"
            )),
            Decimal("0"),
        )

        return {
            "date": date,
            "total_facture": total_facture,
            "total_encaisse": total_encaisse,
            "total_non_paye": total_non_paye,
            "reliquat_global": reliquat_global,
            "nombre_factures": len(factures_jour),
        }

    @classmethod
    @transaction.atomic
    def cloturer(cls, date=None, user=None):
        """Calcule (ou recalcule) la caisse d'une journee et l'enregistre."""
        valeurs = cls.calculer(date)
        date = valeurs.pop("date")

        caisse, _ = cls.objects.update_or_create(
            date=date,
            defaults={
                **valeurs,
                "cloturee": True,
                "cloturee_le": timezone.now(),
                "cree_par": user,
            },
        )
        return caisse
