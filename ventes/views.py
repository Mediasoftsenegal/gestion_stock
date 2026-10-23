from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from catalogue.models import Article, Conditionnement, Tarif, get_prix
from clients.models import Client
from core.utils import PERIODES, lire_xlsx, reessayer_si_conflit_bd

from . import services
from .models import (
    Commande,
    CommandeArchivee,
    Facture,
    FactureArchivee,
    FactureRetour,
    LigneCommande,
    LigneFactureRetour,
    Paiement,
)
from .services import (
    client_comptoir,
    conditionnement_unite,
    liste_tarif_boutique,
    releve_annuel_client,
    suivi_client,
)


def _montant(valeur, defaut=Decimal("0")):
    """Convertit une saisie utilisateur ('1 500', '1500,50') en Decimal."""
    if valeur in (None, ""):
        return defaut
    texte = str(valeur).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        montant = Decimal(texte)
    except (InvalidOperation, ValueError):
        return defaut
    return montant if montant > 0 else defaut


# --------------------------------------------------------------------------- #
#  Commandes
# --------------------------------------------------------------------------- #
@login_required
def commande_liste(request):
    statut = request.GET.get("statut", "").strip()
    q = request.GET.get("q", "").strip()
    aujourdhui = timezone.localdate()
    date_filtre = _date_depuis_get(request, "date", aujourdhui)

    commandes = Commande.objects.select_related("client").prefetch_related("lignes").filter(date=date_filtre)
    if statut:
        commandes = commandes.filter(statut=statut)
    if q:
        commandes = commandes.filter(
            Q(reference__icontains=q) | Q(client__nomclient__icontains=q)
        )
    commandes = commandes.order_by("-date", "-cree_le")

    page_obj = Paginator(commandes, 25).get_page(request.GET.get("page"))

    contexte = {
        "actif": "commandes",
        "commandes": page_obj,
        "page_obj": page_obj,
        "statuts": Commande.Statut.choices,
        "statut_sel": statut,
        "q": q,
        "date_filtre": date_filtre,
        "aujourdhui": aujourdhui,
    }
    return render(request, "ventes/commande_liste.html", contexte)


# --------------------------------------------------------------------------- #
#  Archives (historique 2013-2023, consultation seule) — reservees aux
#  administrateurs/superusers (voir core.context_processors.droits_globaux).
# --------------------------------------------------------------------------- #
def _est_admin(user):
    return bool(user.is_superuser or user.groups.filter(name="Administrateur").exists())


def _date_depuis_get(request, nom_param, defaut):
    """Parse une date 'YYYY-MM-DD' depuis la query string ; la date du jour
    (ou `defaut`) si absente/invalide."""
    brute = request.GET.get(nom_param, "").strip()
    if not brute:
        return defaut
    try:
        return datetime.strptime(brute, "%Y-%m-%d").date()
    except ValueError:
        return defaut


def _plage_jour(date_obj):
    """Bornes [debut, fin) d'une journee pour filtrer un DateTimeField, calculees
    cote Python (timezone active) plutot qu'avec un lookup __date/.dates() : ceux-ci
    s'appuient sur CONVERT_TZ, indisponible sur ce serveur MySQL (tables de fuseaux
    horaires non chargees), et renverraient silencieusement un resultat vide."""
    debut = timezone.make_aware(datetime.combine(date_obj, time.min))
    fin = timezone.make_aware(datetime.combine(date_obj + timedelta(days=1), time.min))
    return debut, fin


@login_required
def archive_liste(request):
    if not _est_admin(request.user):
        raise PermissionDenied("Module réservé aux administrateurs.")

    q = request.GET.get("q", "").strip()

    annees_disponibles = sorted(
        {d.year for d in CommandeArchivee.objects.dates("date", "year")}, reverse=True
    )
    annee = request.GET.get("annee", "").strip()
    annee = int(annee) if annee.isdigit() and int(annee) in annees_disponibles else (
        annees_disponibles[0] if annees_disponibles else None
    )

    commandes = CommandeArchivee.objects.select_related("client")
    if annee:
        commandes = commandes.filter(date__year=annee)
    if q:
        commandes = commandes.filter(
            Q(reference__icontains=q) | Q(client__nomclient__icontains=q)
        )
    commandes = commandes.order_by("-date", "-ancien_id")

    page = Paginator(commandes, 50).get_page(request.GET.get("page"))

    return render(request, "ventes/archive_liste.html", {
        "actif": "archives",
        "page": page,
        "q": q,
        "annee": annee,
        "annees_disponibles": annees_disponibles,
    })


@login_required
def archive_detail(request, pk):
    if not _est_admin(request.user):
        raise PermissionDenied("Module réservé aux administrateurs.")

    commande = get_object_or_404(
        CommandeArchivee.objects.select_related("client").prefetch_related("lignes__article"), pk=pk
    )
    return render(request, "ventes/archive_detail.html", {"actif": "archives", "commande": commande})


@login_required
def archive_facture_liste(request):
    if not _est_admin(request.user):
        raise PermissionDenied("Module réservé aux administrateurs.")

    q = request.GET.get("q", "").strip()
    annees_disponibles = sorted(
        {d.year for d in FactureArchivee.objects.dates("date", "year")}, reverse=True
    )
    annee = request.GET.get("annee", "").strip()
    annee = int(annee) if annee.isdigit() and int(annee) in annees_disponibles else (
        annees_disponibles[0] if annees_disponibles else None
    )

    factures = FactureArchivee.objects.select_related("client")
    if annee:
        factures = factures.filter(date__year=annee)
    if q:
        factures = factures.filter(
            Q(reference__icontains=q) | Q(client__nomclient__icontains=q) | Q(nom_client__icontains=q)
        )
    factures = factures.order_by("-date", "-ancien_id")

    page = Paginator(factures, 50).get_page(request.GET.get("page"))

    return render(request, "ventes/archive_facture_liste.html", {
        "actif": "archives",
        "page": page,
        "q": q,
        "annee": annee,
        "annees_disponibles": annees_disponibles,
    })


@login_required
def archive_facture_detail(request, pk):
    if not _est_admin(request.user):
        raise PermissionDenied("Module réservé aux administrateurs.")

    facture = get_object_or_404(
        FactureArchivee.objects.select_related("client").prefetch_related("lignes__article"), pk=pk
    )
    return render(request, "ventes/archive_facture_detail.html", {"actif": "archives", "facture": facture})


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
        remises = request.POST.getlist("remise")
        prix_saisis = request.POST.getlist("prix")
        # Aligne les listes optionnelles sur celle des lignes.
        remises += [""] * (len(articles) - len(remises))
        prix_saisis += [""] * (len(articles) - len(prix_saisis))

        client = get_object_or_404(Client, pk=client_id) if client_id else None
        if not client:
            messages.error(request, "Veuillez sélectionner un client.")
            return redirect("ventes:commande_create")

        lignes_valides = [
            (a, c, qv, rm, pu)
            for a, c, qv, rm, pu in zip(articles, conds, quantites, remises, prix_saisis)
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
                for a, c, qv, rm, pu in lignes_valides:
                    ligne = LigneCommande(
                        commande=commande,
                        article_id=int(a),
                        conditionnement_id=int(c),
                        quantite=int(qv),
                        remise_unitaire=_montant(rm),
                        # Prix modifiable (negociation) ; vide = prix de la liste
                        # de tarifs (garde-fou plancher applique dans save()).
                        prix_unitaire=_montant(pu) if pu.strip() else None,
                    )
                    ligne.save()
            messages.success(
                request, f"Bon de commande {commande.reference} créé."
            )
            return redirect("ventes:commande_detail", pk=commande.pk)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("ventes:commande_create")

    # GET
    client_pre = request.GET.get("client")
    client_pre_obj = Client.objects.filter(pk=client_pre, actif=True).first() if client_pre else None
    contexte = {
        "actif": "commandes",
        "client_pre": client_pre_obj.pk if client_pre_obj else "",
        "client_pre_obj": client_pre_obj,
    }
    return render(request, "ventes/commande_form.html", contexte)


# --------------------------------------------------------------------------- #
#  Import Excel des commandes
# --------------------------------------------------------------------------- #
SESSION_KEY_IMPORT_COMMANDES = "commandes_import"


@login_required
@permission_required("ventes.add_commande", raise_exception=True)
def commande_import_modele(request):
    reponse = HttpResponse(
        services.generer_modele_commandes(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    reponse["Content-Disposition"] = 'attachment; filename="modele_commandes.xlsx"'
    return reponse


@login_required
@permission_required("ventes.add_commande", raise_exception=True)
def commande_import(request):
    if request.method == "POST":
        fichier = request.FILES.get("fichier")
        if not fichier:
            messages.error(request, "Veuillez sélectionner un fichier Excel (.xlsx).")
            return redirect("ventes:commande_import")
        try:
            entetes, lignes = lire_xlsx(fichier)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("ventes:commande_import")

        if not lignes:
            messages.error(request, "Fichier vide ou illisible.")
            return redirect("ventes:commande_import")

        request.session[SESSION_KEY_IMPORT_COMMANDES] = {"nom_fichier": fichier.name, "lignes": lignes}
        return redirect("ventes:commande_import_apercu")

    return render(request, "ventes/commande_import.html", {"actif": "commandes"})


@login_required
@permission_required("ventes.add_commande", raise_exception=True)
@reessayer_si_conflit_bd()
def commande_import_apercu(request):
    data = request.session.get(SESSION_KEY_IMPORT_COMMANDES)
    if not data:
        messages.error(request, "Aucun import en cours. Chargez d'abord un fichier.")
        return redirect("ventes:commande_import")

    lignes = data["lignes"]
    action = request.POST.get("action", "") if request.method == "POST" else ""

    if action == "annuler":
        del request.session[SESSION_KEY_IMPORT_COMMANDES]
        messages.info(request, "Import annulé.")
        return redirect("ventes:commande_liste")

    resultats, stats = services.analyser_commandes(lignes)

    if action == "appliquer":
        compteurs = services.appliquer_commandes_import(lignes)
        del request.session[SESSION_KEY_IMPORT_COMMANDES]
        messages.success(
            request,
            f"Import terminé : {compteurs['commandes']} commande(s) créée(s) "
            f"({compteurs['lignes']} ligne(s)), {compteurs['ignorees']} groupe(s) ignoré(s).",
        )
        return redirect("ventes:commande_liste")

    return render(request, "ventes/commande_import_apercu.html", {
        "actif": "commandes",
        "nom_fichier": data.get("nom_fichier", ""),
        "resultats": resultats,
        "stats": stats,
        "nb_lignes": len(lignes),
    })


# --------------------------------------------------------------------------- #
#  Import Excel des factures (commande + validation + facturation
#  immediates : a reserver a la reprise de ventes deja realisees)
# --------------------------------------------------------------------------- #
SESSION_KEY_IMPORT_FACTURES = "factures_import"


@login_required
@permission_required("ventes.peut_valider_commande", raise_exception=True)
def facture_import_modele(request):
    reponse = HttpResponse(
        services.generer_modele_factures(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    reponse["Content-Disposition"] = 'attachment; filename="modele_factures.xlsx"'
    return reponse


@login_required
@permission_required("ventes.peut_valider_commande", raise_exception=True)
def facture_import(request):
    if request.method == "POST":
        fichier = request.FILES.get("fichier")
        if not fichier:
            messages.error(request, "Veuillez sélectionner un fichier Excel (.xlsx).")
            return redirect("ventes:facture_import")
        try:
            entetes, lignes = lire_xlsx(fichier)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("ventes:facture_import")

        if not lignes:
            messages.error(request, "Fichier vide ou illisible.")
            return redirect("ventes:facture_import")

        request.session[SESSION_KEY_IMPORT_FACTURES] = {"nom_fichier": fichier.name, "lignes": lignes}
        return redirect("ventes:facture_import_apercu")

    return render(request, "ventes/facture_import.html", {"actif": "factures"})


@login_required
@permission_required("ventes.peut_valider_commande", raise_exception=True)
@reessayer_si_conflit_bd()
def facture_import_apercu(request):
    data = request.session.get(SESSION_KEY_IMPORT_FACTURES)
    if not data:
        messages.error(request, "Aucun import en cours. Chargez d'abord un fichier.")
        return redirect("ventes:facture_import")

    lignes = data["lignes"]
    action = request.POST.get("action", "") if request.method == "POST" else ""

    if action == "annuler":
        del request.session[SESSION_KEY_IMPORT_FACTURES]
        messages.info(request, "Import annulé.")
        return redirect("ventes:facture_liste")

    resultats, stats = services.analyser_factures(lignes)

    if action == "appliquer":
        compteurs = services.appliquer_factures_import(lignes, user=request.user)
        del request.session[SESSION_KEY_IMPORT_FACTURES]
        messages.success(
            request,
            f"Import terminé : {compteurs['factures']} facture(s) créée(s) et validée(s) "
            f"({compteurs['lignes']} ligne(s)), {compteurs['ignorees']} groupe(s) ignoré(s).",
        )
        return redirect("ventes:facture_liste")

    return render(request, "ventes/facture_import_apercu.html", {
        "actif": "factures",
        "nom_fichier": data.get("nom_fichier", ""),
        "resultats": resultats,
        "stats": stats,
        "nb_lignes": len(lignes),
    })


@login_required
def commande_detail(request, pk):
    commande = get_object_or_404(
        Commande.objects.select_related("client", "liste_tarif").prefetch_related(
            "lignes__article", "lignes__conditionnement"
        ),
        pk=pk,
    )
    facture = commande.factures.filter(annulee=False).order_by("-date").first()
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
    aujourdhui = timezone.localdate()
    date_filtre = _date_depuis_get(request, "date", aujourdhui)
    debut_jour, fin_jour = _plage_jour(date_filtre)

    factures = Facture.objects.select_related("client").prefetch_related("lignes", "paiements").filter(
        date__gte=debut_jour, date__lt=fin_jour
    )
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

    page_obj = Paginator(lignes, 25).get_page(request.GET.get("page"))

    contexte = {
        "actif": "factures",
        "factures": page_obj,
        "page_obj": page_obj,
        "statut_sel": statut,
        "q": q,
        "date_filtre": date_filtre,
        "aujourdhui": aujourdhui,
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
        "retour": getattr(facture, "retour", None),
        "peut_valider_retour": request.user.has_perm("ventes.peut_valider_retour"),
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
@permission_required("ventes.peut_valider_commande", raise_exception=True)
@require_POST
def facture_tva(request, pk):
    facture = get_object_or_404(Facture, pk=pk)
    if facture.annulee:
        messages.error(request, "Facture annulée : la TVA ne peut plus être modifiée.")
        return redirect("ventes:facture_detail", pk=pk)
    facture.appliquer_tva = bool(request.POST.get("appliquer_tva"))
    facture.save(update_fields=["appliquer_tva", "modifie_le"])
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


@login_required
def commande_pdf(request, pk):
    commande = get_object_or_404(
        Commande.objects.select_related("client").prefetch_related(
            "lignes__article", "lignes__conditionnement"
        ),
        pk=pk,
    )
    from core.models import Parametrage

    html = render_to_string(
        "ventes/commande_pdf.html",
        {"commande": commande, "now": timezone.now(), "parametrage": Parametrage.get_solo()},
        request=request,
    )
    try:
        from weasyprint import HTML
    except ImportError:
        return HttpResponse(html)

    pdf = HTML(string=html, base_url=request.build_absolute_uri("/")).write_pdf()
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{commande.reference}.pdf"'
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
    pour la vente au detail. Prix issu de la liste de tarifs BOUTIQUE ;
    a defaut (article non repertorie dans cette liste), prix de reference
    Article.prix_bouteille."""
    from stock.services import stock_unites

    code_barre = request.GET.get("code_barre", "").strip()
    q = request.GET.get("q", "").strip()
    cond_unite = conditionnement_unite()
    liste_boutique = liste_tarif_boutique()

    def prix_vente(article):
        prix = get_prix(liste_boutique, article, cond_unite)
        return prix if prix is not None else article.prix_bouteille

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
            "prix_bouteille": float(prix_vente(article)),
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
                    "prix_bouteille": float(prix_vente(a)), "stock": stock_unites(a),
                }
                for a in articles
            ]
        })

    return JsonResponse({"resultats": []})


@login_required
def ajax_recherche_client(request):
    """Recherche un client par nom/code, pour la saisie de commande."""
    q = request.GET.get("q", "").strip()
    if not q:
        return JsonResponse({"resultats": []})

    clients = Client.objects.filter(
        Q(nomclient__icontains=q) | Q(codeclient__icontains=q),
        actif=True,
    ).select_related("liste_tarif").order_by("nomclient")[:15]
    return JsonResponse({
        "resultats": [
            {
                "client_id": c.id, "nom": c.nom, "code": c.codeclient,
                "liste_tarif": c.liste_tarif.nomtarif if c.liste_tarif else "",
            }
            for c in clients
        ]
    })


@login_required
def ajax_recherche_fournisseur(request):
    """Recherche un fournisseur par nom/code, pour la saisie d'approvisionnement."""
    from stock.models import Fournisseur

    q = request.GET.get("q", "").strip()
    if not q:
        return JsonResponse({"resultats": []})

    fournisseurs = Fournisseur.objects.filter(
        Q(nom_fournisseur__icontains=q) | Q(numerofour__icontains=q)
    ).order_by("nom_fournisseur")[:15]
    return JsonResponse({
        "resultats": [
            {"fournisseur_id": f.id, "nom": f.nom_fournisseur, "code": f.numerofour}
            for f in fournisseurs
        ]
    })


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
        liste_boutique = liste_tarif_boutique()

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
                    prix = get_prix(liste_boutique, article, cond_unite)
                    ligne = LigneCommande(
                        commande=commande,
                        article=article,
                        conditionnement=cond_unite,
                        quantite=int(qv),
                        prix_unitaire=prix if prix is not None else article.prix_bouteille,
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


# --------------------------------------------------------------------------- #
#  Depot-vente : facture de retour (rappel des quantites facturees,
#  saisie des quantites vendues, reintegration de l'ecart en stock)
# --------------------------------------------------------------------------- #
def _retour_complet(pk):
    return get_object_or_404(
        FactureRetour.objects.select_related(
            "facture__client", "facture__commande"
        ).prefetch_related(
            "lignes__ligne_facture__article",
            "lignes__ligne_facture__conditionnement",
            "facture__paiements",
        ),
        pk=pk,
    )


@login_required
def retour_liste(request):
    """Toutes les factures de retour (clients en depot-vente)."""
    statut = request.GET.get("statut", "").strip()
    q = request.GET.get("q", "").strip()
    retours = FactureRetour.objects.select_related(
        "facture", "facture__client"
    ).prefetch_related("lignes__ligne_facture__conditionnement")
    if statut:
        retours = retours.filter(statut=statut)
    if q:
        retours = retours.filter(
            Q(numero__icontains=q)
            | Q(facture__numero__icontains=q)
            | Q(facture__client__nomclient__icontains=q)
        )
    contexte = {
        "actif": "retours",
        "retours": retours.order_by("-date", "-cree_le"),
        "statuts": FactureRetour.Statut.choices,
        "statut_sel": statut,
        "q": q,
    }
    return render(request, "ventes/retour_liste.html", contexte)


@login_required
@require_POST
@reessayer_si_conflit_bd()
def retour_creer(request, pk):
    """Ouvre la facture de retour d'une facture de sortie en depot-vente."""
    facture = get_object_or_404(Facture, pk=pk)
    try:
        retour = FactureRetour.creer_pour(facture, user=request.user)
        messages.success(
            request,
            f"Facture de retour {retour.numero} ouverte : saisissez les "
            f"quantites vendues.",
        )
        return redirect("ventes:retour_detail", pk=retour.pk)
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
        return redirect("ventes:facture_detail", pk=pk)


@login_required
def retour_detail(request, pk):
    """Saisie des quantites vendues + rappel des quantites facturees."""
    retour = _retour_complet(pk)
    contexte = {
        "actif": "retours",
        "retour": retour,
        "calcul": retour.detail_calcul(),
        "peut_valider": request.user.has_perm("ventes.peut_valider_retour"),
    }
    return render(request, "ventes/retour_detail.html", contexte)


@login_required
@require_POST
@reessayer_si_conflit_bd()
def retour_enregistrer(request, pk):
    """Enregistre les quantites vendues saisies (sans valider)."""
    retour = _retour_complet(pk)
    quantites = {}
    for ligne in retour.lignes.all():
        brut = request.POST.get(f"vendue_{ligne.pk}", "").strip()
        if brut == "":
            continue
        if not brut.isdigit():
            messages.error(request, f"Quantite vendue invalide pour {ligne.article}.")
            return redirect("ventes:retour_detail", pk=pk)
        quantites[ligne.pk] = int(brut)
    try:
        retour.enregistrer_quantites(quantites, user=request.user)
        messages.success(request, "Quantites vendues enregistrees.")
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
    return redirect("ventes:retour_detail", pk=pk)


@login_required
@permission_required("ventes.peut_valider_retour", raise_exception=True)
@require_POST
@reessayer_si_conflit_bd()
def retour_valider(request, pk):
    """Valide le retour : l'ecart repart en stock, la facture de sortie est
    ramenee aux quantites vendues."""
    retour = _retour_complet(pk)
    # On enregistre d'abord la derniere saisie si elle est postee avec le bouton.
    quantites = {}
    for ligne in retour.lignes.all():
        brut = request.POST.get(f"vendue_{ligne.pk}", "").strip()
        if brut.isdigit():
            quantites[ligne.pk] = int(brut)
    try:
        with transaction.atomic():
            if quantites:
                retour.enregistrer_quantites(quantites, user=request.user)
            retour.valider(user=request.user)
        messages.success(
            request,
            f"Retour {retour.numero} valide : "
            f"{retour.unites_retournees_totales} unite(s) reintegree(s) en stock. "
            f"Montant du apres retour : {retour.montant_vendu_ttc:.0f}.",
        )
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
    return redirect("ventes:retour_detail", pk=pk)


@login_required
def retour_calcul(request, pk):
    """Fenetre de calcul : detail des quantites facturees / vendues /
    retournees et des montants pour une facture de depot-vente."""
    retour = _retour_complet(pk)
    contexte = {
        "actif": "retours",
        "retour": retour,
        "calcul": retour.detail_calcul(),
        "fenetre": True,
    }
    return render(request, "ventes/retour_calcul.html", contexte)


@login_required
def retour_pdf(request, pk):
    """Etat d'impression de la facture de retour."""
    from core.models import Parametrage

    retour = _retour_complet(pk)
    html = render_to_string(
        "ventes/retour_pdf.html",
        {
            "retour": retour,
            "calcul": retour.detail_calcul(),
            "facture": retour.facture,
            "now": timezone.now(),
            "parametrage": Parametrage.get_solo(),
        },
        request=request,
    )
    try:
        from weasyprint import HTML
    except ImportError:
        return HttpResponse(html)

    pdf = HTML(string=html, base_url=request.build_absolute_uri("/")).write_pdf()
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{retour.numero}.pdf"'
    return resp
