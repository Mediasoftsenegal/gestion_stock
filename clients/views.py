from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, render

from .models import CategorieClient, Client


@login_required
def client_liste(request):
    q = request.GET.get("q", "").strip()
    categorie = request.GET.get("categorie", "").strip()

    clients = Client.objects.select_related("categorie", "liste_tarif")
    if q:
        clients = clients.filter(
            Q(nomclient__icontains=q)
            | Q(codeclient__icontains=q)
            | Q(telephone__icontains=q)
        )
    if categorie:
        clients = clients.filter(categorie_id=categorie)

    clients = clients.order_by("nomclient")

    contexte = {
        "actif": "clients",
        "clients": clients,
        "categories": CategorieClient.objects.all(),
        "q": q,
        "categorie_sel": categorie,
        "total": clients.count(),
    }
    return render(request, "clients/liste.html", contexte)


@login_required
def client_detail(request, pk):
    client = get_object_or_404(
        Client.objects.select_related("categorie", "liste_tarif"), pk=pk
    )
    factures = client.factures.order_by("-date")[:15]
    contexte = {
        "actif": "clients",
        "client": client,
        "factures": factures,
    }
    return render(request, "clients/detail.html", contexte)
