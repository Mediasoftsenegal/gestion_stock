from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import CategorieClient, Client, ListeTarif


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


# --------------------------------------------------------------------------- #
#  Listes de prix / tarifs (devant etre geres depuis le front, pas seulement
#  depuis l'admin Django)
# --------------------------------------------------------------------------- #

@login_required
def liste_tarif_liste(request):
    q = request.GET.get("q", "").strip()

    listes = ListeTarif.objects.all()
    if q:
        listes = listes.filter(Q(nomtarif__icontains=q) | Q(description__icontains=q))
    listes = listes.order_by("nomtarif")

    contexte = {
        "actif": "tarifs",
        "listes": listes,
        "q": q,
        "total": listes.count(),
        "peut_gerer": request.user.has_perm("clients.add_listetarif"),
    }
    return render(request, "clients/tarif_liste.html", contexte)


@login_required
def liste_tarif_detail(request, pk):
    liste = get_object_or_404(
        ListeTarif.objects.prefetch_related("tarifs__article", "tarifs__conditionnement"),
        pk=pk,
    )
    contexte = {
        "actif": "tarifs",
        "liste": liste,
        "tarifs": liste.tarifs.select_related("article", "conditionnement").order_by(
            "article__libarticle", "conditionnement__nombre_unites"
        ),
        "clients_lies": liste.clients.order_by("nomclient"),
        "peut_gerer": request.user.has_perm("clients.change_listetarif"),
        "peut_gerer_tarifs": request.user.has_perm("catalogue.add_tarif"),
    }
    return render(request, "clients/tarif_detail.html", contexte)


@login_required
def liste_tarif_form(request, pk=None):
    liste = get_object_or_404(ListeTarif, pk=pk) if pk else None
    code = "change_listetarif" if liste else "add_listetarif"
    if not request.user.has_perm(f"clients.{code}"):
        raise PermissionDenied

    if request.method == "POST":
        nomtarif = request.POST.get("nomtarif", "").strip()
        description = request.POST.get("description", "").strip()
        active = bool(request.POST.get("active"))

        if not nomtarif:
            messages.error(request, "Le nom de la liste de tarifs est obligatoire.")
        else:
            liste = liste or ListeTarif()
            liste.nomtarif = nomtarif
            liste.description = description
            liste.active = active
            try:
                liste.full_clean()
                liste.save()
                messages.success(request, f"Liste de tarifs « {liste.nomtarif} » enregistrée.")
                return redirect("clients:tarif_detail", pk=liste.pk)
            except ValidationError as e:
                for erreurs in e.message_dict.values():
                    for err in erreurs:
                        messages.error(request, err)

    contexte = {"actif": "tarifs", "liste": liste}
    return render(request, "clients/tarif_form.html", contexte)


@login_required
@permission_required("clients.change_listetarif", raise_exception=True)
@require_POST
def liste_tarif_basculer(request, pk):
    liste = get_object_or_404(ListeTarif, pk=pk)
    liste.active = not liste.active
    liste.save(update_fields=["active"])
    messages.success(
        request,
        f"Liste de tarifs « {liste.nomtarif} » {'activée' if liste.active else 'désactivée'}.",
    )
    return redirect("clients:tarif_detail", pk=liste.pk)
