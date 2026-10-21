from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.utils import lire_xlsx

from . import services
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


# --------------------------------------------------------------------------- #
#  Import Excel des listes de tarifs (noms)
# --------------------------------------------------------------------------- #
SESSION_KEY_IMPORT_LISTES = "listes_tarifs_import"


@login_required
@permission_required("clients.add_listetarif", raise_exception=True)
def tarif_import_modele(request):
    reponse = HttpResponse(
        services.generer_modele(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    reponse["Content-Disposition"] = 'attachment; filename="modele_listes_de_tarifs.xlsx"'
    return reponse


@login_required
@permission_required("clients.add_listetarif", raise_exception=True)
def tarif_import(request):
    if request.method == "POST":
        fichier = request.FILES.get("fichier")
        if not fichier:
            messages.error(request, "Veuillez sélectionner un fichier Excel (.xlsx).")
            return redirect("clients:tarif_import")
        try:
            entetes, lignes = lire_xlsx(fichier)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("clients:tarif_import")

        if not lignes:
            messages.error(request, "Fichier vide ou illisible.")
            return redirect("clients:tarif_import")

        request.session[SESSION_KEY_IMPORT_LISTES] = {"nom_fichier": fichier.name, "lignes": lignes}
        return redirect("clients:tarif_import_apercu")

    return render(request, "clients/tarif_import.html", {"actif": "tarifs"})


@login_required
@permission_required("clients.add_listetarif", raise_exception=True)
def tarif_import_apercu(request):
    data = request.session.get(SESSION_KEY_IMPORT_LISTES)
    if not data:
        messages.error(request, "Aucun import en cours. Chargez d'abord un fichier.")
        return redirect("clients:tarif_import")

    lignes = data["lignes"]
    action = request.POST.get("action", "") if request.method == "POST" else ""

    if action == "annuler":
        del request.session[SESSION_KEY_IMPORT_LISTES]
        messages.info(request, "Import annulé.")
        return redirect("clients:tarifs_liste")

    resultats, stats = services.analyser(lignes)

    if action == "appliquer":
        try:
            compteurs = services.appliquer(
                lignes,
                creer_nouveaux=bool(request.POST.get("creer_nouveaux")),
                mettre_a_jour=bool(request.POST.get("mettre_a_jour")),
            )
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("clients:tarif_import_apercu")

        del request.session[SESSION_KEY_IMPORT_LISTES]
        messages.success(
            request,
            f"Import terminé : {compteurs['crees']} liste(s) créée(s), "
            f"{compteurs['modifies']} mise(s) à jour, {compteurs['ignores']} ligne(s) ignorée(s), "
            f"{compteurs['erreurs']} en erreur.",
        )
        return redirect("clients:tarifs_liste")

    return render(request, "clients/tarif_import_apercu.html", {
        "actif": "tarifs",
        "nom_fichier": data.get("nom_fichier", ""),
        "resultats": resultats,
        "stats": stats,
        "nb_lignes": len(lignes),
    })
