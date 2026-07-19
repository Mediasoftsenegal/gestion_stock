from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Caisse


@login_required
def liste(request):
    caisses = Caisse.objects.order_by("-date")[:60]
    today = timezone.localdate()
    caisse_jour = Caisse.objects.filter(date=today).first()
    contexte = {
        "actif": "caisse",
        "caisses": caisses,
        "today": today,
        "caisse_jour": caisse_jour,
    }
    return render(request, "caisse/liste.html", contexte)


@login_required
def voir(request):
    """Consulte la caisse d'une date choisie : affiche la cloture existante
    si elle existe, sinon un calcul en temps reel (non enregistre)."""
    date_str = request.GET.get("date", "").strip()
    if date_str:
        try:
            jour = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Date invalide.")
            return redirect("caisse:liste")
    else:
        jour = timezone.localdate()

    caisse = Caisse.objects.filter(date=jour).first()
    if not caisse:
        caisse = Caisse(**Caisse.calculer(jour))

    from ventes.models import Facture, Paiement

    factures = Facture.objects.filter(date__date=jour, annulee=False).select_related("client")
    paiements = Paiement.objects.filter(date__date=jour).select_related("facture")
    contexte = {
        "actif": "caisse",
        "caisse": caisse,
        "factures": factures,
        "paiements": paiements,
        "peut_cloturer": request.user.has_perm("caisse.peut_cloturer_caisse"),
    }
    return render(request, "caisse/detail.html", contexte)


@login_required
@permission_required("caisse.peut_cloturer_caisse", raise_exception=True)
@require_POST
def cloturer(request):
    date_str = request.POST.get("date", "").strip()
    if date_str:
        try:
            jour = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Date invalide.")
            return redirect("caisse:liste")
    else:
        jour = timezone.localdate()

    caisse = Caisse.cloturer(date=jour, user=request.user)
    messages.success(
        request,
        f"Caisse du {jour:%d/%m/%Y} clôturée : "
        f"encaissé {caisse.total_encaisse:.0f}, reliquat jour {caisse.total_non_paye:.0f}.",
    )
    return redirect("caisse:detail", pk=caisse.pk)


@login_required
def detail(request, pk):
    caisse = get_object_or_404(Caisse, pk=pk)
    from ventes.models import Facture, Paiement

    factures = Facture.objects.filter(date__date=caisse.date, annulee=False).select_related("client")
    paiements = Paiement.objects.filter(date__date=caisse.date).select_related("facture")
    contexte = {
        "actif": "caisse",
        "caisse": caisse,
        "factures": factures,
        "paiements": paiements,
        "peut_cloturer": request.user.has_perm("caisse.peut_cloturer_caisse"),
    }
    return render(request, "caisse/detail.html", contexte)
