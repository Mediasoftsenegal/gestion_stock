from django.db.models import Sum
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from catalogue.models import Article
from stock.models import MouvementStock

from .serializers import ArticleStockSerializer


class StockViewSet(viewsets.ReadOnlyModelViewSet):
    """Consultation du stock en temps reel (lecture seule).

    - GET /api/stock/            -> liste des articles avec stock courant
    - GET /api/stock/{id}/       -> un article
    - GET /api/stock/alertes/    -> uniquement les articles sous le seuil

    Le stock est recalcule a chaque appel depuis les mouvements, donc
    plusieurs utilisateurs voient toujours la meme valeur a jour.
    """

    serializer_class = ArticleStockSerializer

    def get_queryset(self):
        qs = Article.objects.filter(actif=True).select_related("type_article")
        actif_seulement = self.request.query_params.get("tous")
        if actif_seulement == "1":
            qs = Article.objects.all().select_related("type_article")
        return qs.order_by("designation")

    def _stock_map(self):
        return {
            row["article_id"]: row["q"]
            for row in MouvementStock.objects.values("article_id").annotate(
                q=Sum("quantite_unites")
            )
        }

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["stock_map"] = self._stock_map()
        return ctx

    @action(detail=False)
    def alertes(self, request):
        stock_map = self._stock_map()
        articles = [
            a
            for a in self.get_queryset()
            if a.seuil_alerte and (stock_map.get(a.id, 0) or 0) <= a.seuil_alerte
        ]
        ser = self.get_serializer(articles, many=True)
        return Response(ser.data)
