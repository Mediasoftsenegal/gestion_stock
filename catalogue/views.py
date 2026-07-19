from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, render

from stock.models import MouvementStock

from .models import Article, TypeArticle


def _stock_map():
    return {
        row["article_id"]: row["q"]
        for row in MouvementStock.objects.values("article_id").annotate(
            q=Sum("quantite_unites")
        )
    }


@login_required
def article_liste(request):
    q = request.GET.get("q", "").strip()
    type_id = request.GET.get("type", "").strip()

    qs = Article.objects.select_related("type_article").order_by("libarticle")

    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(libarticle__icontains=q))

    if type_id:
        qs = qs.filter(type_article_id=type_id)

    total = qs.count()

    # Construire la liste à paginer
    lignes = []
    for a in qs:
        stock = 0
        sous_seuil = False
        lignes.append({
            "a": a,
            "stock": stock,
            "sous_seuil": sous_seuil,
        })

    paginator = Paginator(lignes, 25)  # 20 articles par page
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "lignes": page_obj.object_list,
        "page_obj": page_obj,
        "total": total,
        "q": q,
        "type_sel": type_id,
        "types": TypeArticle.objects.all(),
    }
    return render(request, "catalogue/article_liste.html", context)


@login_required
def article_detail(request, pk):
    article = get_object_or_404(
        Article.objects.select_related("type_article").prefetch_related(
            "conditionnements", "tarifs__liste_tarif", "tarifs__conditionnement"
        ),
        pk=pk,
    )
    mouvements = article.mouvements.order_by("-date")[:20]
    contexte = {
        "actif": "articles",
        "article": article,
        "stock": article.stock_actuel,
        "mouvements": mouvements,
        "tarifs": article.tarifs.all(),
    }
    return render(request, "catalogue/article_detail.html", contexte)
