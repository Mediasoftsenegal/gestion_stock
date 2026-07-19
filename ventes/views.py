from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from catalogue.models import Article, Conditionnement, Tarif
from clients.models import Client
from core.utils import PERIODES, reessayer_si_conflit_bd

from .models import Commande, Facture, LigneCommande, Paiement
from .services import client_comptoir, conditionnement_unite, releve_annuel_client, suivi_client


# --------------------------------------------------------------------------- #
#  Commandes
# --------------------------------------------------------------------------- #
@login_required
def commande_liste(request):
    statut = request.GET.get("statut", "").strip()
    q = request.GET.get("q", "").strip()
    commandes = Commande.objects.select_related("client").prefetch_related("lignes")
    if statut:
        commandes = commandes.filter(statut=statut)
    if q:
        commandes = commandes.filter(
            Q(reference__icontains=q) | Q(client__nomclient__icontains=q)
        )
    commandes = commandes.order_by("-date", "-cree_le")

    contexte = {
        "actif": "commandes",
        "commandes": commandes,
        "statuts": Commande.Statut.choices,
        "statut_sel": statut,
        "q": q,
    }
    return render(request, "ventes/commande_liste.html", contexte)


@login_required
@reessayer_si_conflit_bd()
def commande_create(request):
    """Saisie d'une commande : client + lignes (prix issu de la liste de tarifs)."""
    if request.method == "POST":
        client_id = request.POST.get("client")
        note = request.POST.get("note", "").strip()
        appliquer_tva = bool(request.POST.get("appliquer_tva"))
        articles = request.POST.getlist("article")
        conds = request.POST.getlist("conditionnement")
        quantites = request.POST.getlist("quantite")

        client = get_object_or_404(Client, pk=client_id) if client_id else None
        if not client:
            messages.error(request, "Veuillez sélectionner un client.")
            return redirect("ventes:commande_create")

        lignes_valides = [
            (a, c, qv)
            for a, c, qv in zip(articles, conds, quantites)
            if a and c and qv and qv.isdigit() and int(qv) > 0
        ]
        if not lignes_valides:
            messages.error(request, "Ajoutez au moins une ligne valide.")
            return redirect("ventes:commande_create")

        try:
            with transaction.atomic():
                commande = Commande.objects.create(
                    client=client, date=timezone.localdate(), note=note,
                    appliquer_tva=appliquer_tva, cree_par=request.user,
                )
                for a, c, qv in lignes_valides:
                    ligne = LigneCommande(
                        commande=commande,
                        article_id=int(a),
                        conditionnement_id=int(c),
                        quantite=int(qv),
                    )
                    ligne.save()  # prix auto depuis la liste de tarifs + garde-fou plancher
            messages.success(
                request, f"Bon de commande {commande.reference} créé."
            )
            return redirect("ventes:commande_detail", pk=commande.pk)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("ventes:commande_create")

    # GET
    client_pre = request.GET.get("client")
    contexte = {
        "actif": "commandes",
        "clients": Client.objects.filter(actif=True).select_related("liste_tarif").order_by("nomclient"),
        "client_pre": client_pre,
    }
    return render(request, "ventes/commande_form.html", contexte)


@login_required
def commande_detail(request, pk):
    commande = get_object_or_404(
        Commande.objects.select_related("client", "liste_tarif").prefetch_related(
            "lignes__article", "lignes__conditionnement"
        ),
        pk=pk,
    )
    facture = getattr(commande, "facture", None)
    contexte = {
        "actif": "commandes",
        "commande": commande,
        "facture": facture,
        "peut_valider": request.user.has_perm("ventes.peut_valider_commande"),
    }
    return render(request, "ventes/commande_detail.html", contexte)


@login_required
@permission_required("ventes.peut_valider_commande", raise_exception=True)
@require_POST
@reessayer_si_conflit_bd()
def commande_valider(request, pk):
    commande = get_object_or_404(Commande, pk=pk)
    try:
        commande.valider(user=request.user)
        messages.success(
            request,
            f"Commande {commande.reference} validée, stock mis à jour. "
            f"Vous pouvez maintenant générer la facture.",
        )
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
    return redirect("ventes:commande_detail", pk=pk)


@login_required
@permission_required("ventes.peut_valider_commande", raise_exception=True)
@require_POST
@reessayer_si_conflit_bd()
def commande_generer_facture(request, pk):
    commande = get_object_or_404(Commande, pk=pk)
    try:
        facture = commande.generer_facture(user=request.user)
        messages.success(request, f"Facture {facture.numero} générée.")
        return redirect("ventes:facture_detail", pk=facture.pk)
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
        return redirect("ventes:commande_detail", pk=pk)


@login_required
@require_POST
def commande_supprimer(request, pk):
    commande = get_object_or_404(Commande, pk=pk)
    if commande.statut != Commande.Statut.BON_DE_COMMANDE:
        messages.error(request, "Seul un bon de commande non validé peut être supprimé.")
        return redirect("ventes:commande_detail", pk=pk)
    ref = commande.reference
    commande.delete()
    messages.success(request, f"Bon de commande {ref} supprimé.")
    return redirect("ventes:commande_liste")


# --------------------------------------------------------------------------- #
#  Factures
# --------------------------------------------------------------------------- #
@login_required
def facture_liste(request):
    statut = request.GET.get("statut", "").strip()  # payee / partielle / non_payee / annulee
    q = request.GET.get("q", "").strip()
    factures = Facture.objects.select_related("client").prefetch_related("lignes", "paiements")
    if q:
        factures = factures.filter(
            Q(numero__icontains=q) | Q(client__nomclient__icontains=q)
        )
    factures = factures.order_by("-date")

    lignes = []
    for f in factures:
        etat = "annulee" if f.annulee else f.statut_paiement.value.lower()
        if statut and etat != statut:
            continue
        lignes.append(f)

    contexte = {
        "actif": "factures",
        "factures": lignes,
        "statut_sel": statut,
        "q": q,
    }
    return render(request, "ventes/facture_liste.html", contexte)


@login_required
def facture_detail(request, pk):
    facture = get_object_or_404(
        Facture.objects.select_related("client", "commande").prefetch_related(
            "lignes__article", "lignes__conditionnement", "paiements"
        ),
        pk=pk,
    )
    contexte = {
        "actif": "factures",
        "facture": facture,
        "peut_encaisser": request.user.has_perm("ventes.peut_encaisser"),
        "peut_annuler": request.user.has_perm("ventes.peut_annuler_facture"),
        "modes": Paiement.Mode.choices,
    }
    return render(request, "ventes/facture_detail.html", contexte)


@login_required
@permission_required("ventes.peut_encaisser", raise_exception=True)
@require_POST
def encaisser(request, pk):
    facture = get_object_or_404(Facture, pk=pk)
    montant_str = request.POST.get("montant", "").replace(" ", "").replace(",", ".")
    mode = request.POST.get("mode", Paiement.Mode.ESPECES)
    reference = request.POST.get("reference", "").strip()
    try:
        montant = Decimal(montant_str)
    except (InvalidOperation, ValueError):
        messages.error(request, "Montant invalide.")
        return redirect("ventes:facture_detail", pk=pk)

    # Type : comptant si solde complet du reliquat, sinon acompte.
    type_paiement = (
        Paiement.TypePaiement.COMPTANT
        if montant >= facture.reliquat
        else Paiement.TypePaiement.ACOMPTE
    )
    paiement = Paiement(
        facture=facture, type_paiement=type_paiement, mode=mode,
        montant=montant, reference=reference, cree_par=request.user,
    )
    try:
        paiement.full_clean()
        paiement.save()
        messages.success(
            request,
            f"Encaissement de {montant:.0f} enregistré. "
            f"Reliquat : {facture.reliquat:.0f}.",
        )
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
    return redirect("ventes:facture_detail", pk=pk)


@login_required
@permission_required("ventes.peut_annuler_facture", raise_exception=True)
@require_POST
def facture_annuler(request, pk):
    facture = get_object_or_404(Facture, pk=pk)
    justificatif = request.POST.get("justificatif", "").strip()
    try:
        facture.annuler(user=request.user, justificatif=justificatif)
        messages.success(request, f"Facture {facture.numero} annulée, stock restauré.")
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
    return redirect("ventes:facture_detail", pk=pk)


@login_required
def facture_pdf(request, pk):
    facture = get_object_or_404(
        Facture.objects.select_related("client", "commande").prefetch_related(
            "lignes__article", "lignes__conditionnement", "paiements"
        ),
        pk=pk,
    )
    from core.models import Parametrage

    html = render_to_string(
        "ventes/facture_pdf.html",
        {"facture": facture, "now": timezone.now(), "parametrage": Parametrage.get_solo()},
        request=request,
    )
    try:
        from weasyprint import HTML
    except ImportError:
        # WeasyPrint (ou ses libs systeme) indisponible : on sert la version
        # imprimable HTML plutot que d'echouer.
        return HttpResponse(html)

    pdf = HTML(string=html, base_url=request.build_absolute_uri("/")).write_pdf()
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{facture.numero}.pdf"'
    return resp


# --------------------------------------------------------------------------- #
#  Etat de suivi client (filtrable par periode)
# --------------------------------------------------------------------------- #
@login_required
def suivi_client_view(request):
    clients = Client.objects.filter(actif=True).order_by("nomclient")
    client_id = request.GET.get("client")
    periode = request.GET.get("periode", "MOIS")
    reference = request.GET.get("reference") or None

    rapport = None
    client = None
    if client_id:
        client = get_object_or_404(Client, pk=client_id)
        rapport = suivi_client(client, periode=periode, reference=reference)

    contexte = {
        "actif": "clients",
        "clients": clients,
        "client_sel": client_id,
        "periode_sel": periode,
        "periodes": PERIODES,
        "reference": reference or "",
        "rapport": rapport,
        "client": client,
    }
    return render(request, "ventes/suivi_client.html", contexte)


@login_required
def releve_annuel_client_view(request):
    """Etat de suivi annuel d'un client : ses operations regroupees mois par
    mois (janvier a decembre) avec report a nouveau du reliquat impaye."""
    clients = Client.objects.filter(actif=True).order_by("nomclient")
    client_id = request.GET.get("client")
    annee_actuelle = timezone.localdate().year
    try:
        annee = int(request.GET.get("annee", annee_actuelle))
    except ValueError:
        annee = annee_actuelle

    rapport = None
    client = None
    if client_id:
        client = get_object_or_404(Client, pk=client_id)
        rapport = releve_annuel_client(client, annee)

    contexte = {
        "actif": "suivi_annuel",
        "clients": clients,
        "client_sel": client_id,
        "annee_sel": annee,
        "annees": range(annee_actuelle, annee_actuelle - 6, -1),
        "rapport": rapport,
        "client": client,
    }
    return render(request, "ventes/releve_annuel_client.html", contexte)


# --------------------------------------------------------------------------- #
#  AJAX : grille de tarifs d'un client (pour la saisie de commande)
# --------------------------------------------------------------------------- #
@login_required
def ajax_tarifs(request):
    """Retourne les articles/conditionnements tarifes pour la liste d'un client."""
    client_id = request.GET.get("client")
    client = get_object_or_404(Client, pk=client_id)
    tarifs = (
        Tarif.objects.filter(liste_tarif=client.liste_tarif)
        .select_related("article", "conditionnement")
        .order_by("article__libarticle")
    )
    data = {}
    for t in tarifs:
        a = t.article
        data.setdefault(
            a.id,
            {"article_id": a.id, "libarticle": a.libarticle,
             "code": a.code, "conditionnements": []},
        )
        data[a.id]["conditionnements"].append({
            "conditionnement_id": t.conditionnement_id,
            "nom": t.conditionnement.nom,
            "nombre_unites": t.conditionnement.nombre_unites,
            "prix": float(t.prix_unitaire),
        })
    return JsonResponse({
        "client": client.nom,
        "liste_tarif": client.liste_tarif.nomtarif,
        "avectva": client.avectva,
        "exempttva": client.exempttva,
        "articles": list(data.values()),
    })


# --------------------------------------------------------------------------- #
#  Vente au detail (comptoir) : scan code-barre, encaissement comptant
# --------------------------------------------------------------------------- #
@login_required
def ajax_recherche_article(request):
    """Recherche un article par code-barre (scan) ou par texte (nom/code),
    pour la vente au detail."""
    from stock.services import stock_unites

    code_barre = request.GET.get("code_barre", "").strip()
    q = request.GET.get("q", "").strip()

    if code_barre:
        article = Article.objects.filter(code_barre=code_barre, actif=True).first()
        if not article:
            return JsonResponse({
                "trouve": False,
                "message": f"Aucun article pour le code-barre « {code_barre} ».",
            })
        return JsonResponse({
            "trouve": True,
            "article_id": article.id,
            "libarticle": article.libarticle,
            "code": article.code,
            "prix_bouteille": float(article.prix_bouteille),
            "stock": stock_unites(article),
        })

    if q:
        articles = Article.objects.filter(
            Q(libarticle__icontains=q) | Q(code__icontains=q) | Q(code_barre__icontains=q),
            actif=True,
        ).order_by("libarticle")[:15]
        return JsonResponse({
            "resultats": [
                {
                    "article_id": a.id, "libarticle": a.libarticle, "code": a.code,
                    "prix_bouteille": float(a.prix_bouteille), "stock": stock_unites(a),
                }
                for a in articles
            ]
        })

    return JsonResponse({"resultats": []})


@login_required
@reessayer_si_conflit_bd()
def vente_detail_create(request):
    """Vente au detail (comptoir) : panier scanne/recherche, encaissement
    immediat au comptant, decompte du stock, ticket imprimable en retour."""
    if request.method == "POST":
        client_id = request.POST.get("client")
        mode = request.POST.get("mode", Paiement.Mode.ESPECES)
        articles = request.POST.getlist("article")
        quantites = request.POST.getlist("quantite")

        client = get_object_or_404(Client, pk=client_id) if client_id else client_comptoir()

        lignes_valides = [
            (a, qv)
            for a, qv in zip(articles, quantites)
            if a and qv and qv.isdigit() and int(qv) > 0
        ]
        if not lignes_valides:
            messages.error(request, "Ajoutez au moins un article au panier.")
            return redirect("ventes:vente_detail_create")

        cond_unite = conditionnement_unite()

        try:
            with transaction.atomic():
                commande = Commande.objects.create(
                    client=client,
                    date=timezone.localdate(),
                    appliquer_tva=False,
                    note="Vente au détail (comptoir)",
                    cree_par=request.user,
                )
                for a, qv in lignes_valides:
                    article = get_object_or_404(Article, pk=int(a))
                    ligne = LigneCommande(
                        commande=commande,
                        article=article,
                        conditionnement=cond_unite,
                        quantite=int(qv),
                        prix_unitaire=article.prix_bouteille,
                    )
                    ligne.save()
                commande.valider(user=request.user)
                facture = commande.generer_facture(user=request.user)

                paiement = Paiement(
                    facture=facture,
                    type_paiement=Paiement.TypePaiement.COMPTANT,
                    mode=mode,
                    montant=facture.montant_total,
                    cree_par=request.user,
                )
                paiement.full_clean()
                paiement.save()
            messages.success(request, f"Vente {facture.numero} encaissée : {facture.montant_total:.0f} FCFA.")
            return redirect("ventes:ticket_caisse", pk=facture.pk)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("ventes:vente_detail_create")

    contexte = {
        "actif": "vente_detail",
        "clients": Client.objects.filter(actif=True).order_by("nomclient"),
        "modes": Paiement.Mode.choices,
    }
    return render(request, "ventes/vente_detail.html", contexte)


@login_required
def ticket_caisse(request, pk):
    """Ticket de caisse imprimable (format 80mm, imprimante thermique type
    Epson TM-T20)."""
    from core.models import Parametrage

    facture = get_object_or_404(
        Facture.objects.select_related("client").prefetch_related(
            "lignes__article", "lignes__conditionnement", "paiements"
        ),
        pk=pk,
    )
    contexte = {
        "facture": facture,
        "parametrage": Parametrage.get_solo(),
        "now": timezone.now(),
    }
    return render(request, "ventes/ticket_caisse.html", contexte)
