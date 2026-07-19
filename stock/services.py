"""Logique de stock centralisee : le stock derive TOUJOURS des mouvements."""

from django.db.models import Sum


def stock_unites(article):
    """Stock disponible d'un article, en unites de base."""
    from .models import MouvementStock

    total = MouvementStock.objects.filter(article=article).aggregate(
        s=Sum("quantite_unites")
    )["s"]
    return total or 0


def enregistrer_mouvement(
    article, type_mouvement, quantite_unites_signee, reference="", motif="", user=None, date=None
):
    """Cree un mouvement de stock.

    `quantite_unites_signee` : positif pour une entree, negatif pour une sortie.
    """
    from django.utils import timezone

    from .models import MouvementStock

    return MouvementStock.objects.create(
        article=article,
        type_mouvement=type_mouvement,
        quantite_unites=quantite_unites_signee,
        reference=reference,
        motif=motif,
        date=date or timezone.now(),
        cree_par=user,
    )
