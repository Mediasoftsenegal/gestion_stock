from rest_framework import serializers

from catalogue.models import Article


class ArticleStockSerializer(serializers.ModelSerializer):
    """Expose le niveau de stock temps reel d'un article.

    `stock_actuel` est calcule a partir des mouvements (source de verite),
    jamais stocke : la valeur est donc toujours a jour, meme en multi-postes.
    """

    type_article = serializers.CharField(source="type_article.nom", read_only=True)
    stock_actuel = serializers.SerializerMethodField()
    sous_seuil = serializers.SerializerMethodField()

    class Meta:
        model = Article
        fields = [
            "id",
            "code",
            "designation",
            "type_article",
            "unite_base",
            "seuil_alerte",
            "stock_actuel",
            "sous_seuil",
            "prix_achat",
        ]

    def get_stock_actuel(self, obj):
        # Utilise la table pre-agregee passee par la vue si disponible.
        cache = self.context.get("stock_map")
        if cache is not None:
            return cache.get(obj.id, 0) or 0
        return obj.stock_actuel

    def get_sous_seuil(self, obj):
        stock = self.get_stock_actuel(obj)
        return bool(obj.seuil_alerte and stock <= obj.seuil_alerte)
