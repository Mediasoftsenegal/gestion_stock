from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone


@login_required
def dashboard(request):
    from catalogue.models import Article
    from stock.models import MouvementStock
    from ventes.models import Commande, Facture, Paiement

    today = timezone.localdate()
    debut_mois = today.replace(day=1)

    # --- Stock : agregation globale des mouvements par article ---
    articles = list(Article.objects.filter(actif=True))
    stock_par_article = {
        row["article_id"]: row["q"]
        for row in MouvementStock.objects.values("article_id").annotate(
            q=Sum("quantite_unites")
        )
    }
    nb_articles = len(articles)
    sous_seuil = []
    valeur_stock = Decimal("0")
    for a in articles:
        q = stock_par_article.get(a.id, 0) or 0
        valeur_stock += (a.prix_achat or Decimal("0")) * q
        if a.seuil_alerte and q <= a.seuil_alerte:
            sous_seuil.append((a, q))

    # --- Ventes du jour ---
    factures_jour = Facture.objects.filter(date__date=today, annulee=False)
    ca_jour = sum((f.montant_total for f in factures_jour), Decimal("0"))
    encaisse_jour = Paiement.objects.filter(date__date=today).aggregate(
        s=Sum("montant")
    )["s"] or Decimal("0")

    # --- Ventes du mois ---
    factures_mois = Facture.objects.filter(
        date__date__gte=debut_mois, annulee=False
    )
    ca_mois = sum((f.montant_total for f in factures_mois), Decimal("0"))

    # --- Bons de commande en attente ---
    bons_attente = Commande.objects.filter(
        statut=Commande.Statut.BON_DE_COMMANDE
    ).select_related("client").order_by("-date")[:8]
    nb_bons = Commande.objects.filter(statut=Commande.Statut.BON_DE_COMMANDE).count()

    # --- Dernieres factures ---
    dernieres_factures = (
        Facture.objects.select_related("client").order_by("-date")[:8]
    )

    contexte = {
        "actif": "dashboard",
        "nb_articles": nb_articles,
        "nb_sous_seuil": len(sous_seuil),
        "sous_seuil": sous_seuil[:8],
        "valeur_stock": valeur_stock,
        "ca_jour": ca_jour,
        "encaisse_jour": encaisse_jour,
        "nb_factures_jour": factures_jour.count(),
        "ca_mois": ca_mois,
        "nb_bons": nb_bons,
        "bons_attente": bons_attente,
        "dernieres_factures": dernieres_factures,
        "today": today,
    }
    return render(request, "core/dashboard.html", contexte)
