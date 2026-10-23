from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from clients.models import ListeTarif
from core.utils import lire_xlsx
from stock.models import MouvementStock

from . import services
from .models import Article, Conditionnement, Tarif, TypeArticle


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


# --------------------------------------------------------------------------- #
#  Mise a niveau du catalogue : chargement d'une nouvelle liste d'articles
# --------------------------------------------------------------------------- #
SESSION_KEY_IMPORT_ARTICLES = "articles_import"


@login_required
@permission_required("catalogue.change_article", raise_exception=True)
def article_import(request):
    """Etape 1 : chargement du fichier (CSV/TXT ou Excel)."""
    from django.contrib import messages
    from django.shortcuts import redirect

    from .services import CHAMPS_FICHIER, LIBELLES, detecter_mapping, lire_fichier

    if request.method == "POST":
        fichier = request.FILES.get("fichier")
        separateur = request.POST.get("separateur", "").strip()
        if not fichier:
            messages.error(request, "Veuillez sélectionner un fichier.")
            return redirect("catalogue:article_import")
        try:
            entetes, lignes = lire_fichier(fichier, separateur)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("catalogue:article_import")

        if not entetes or not lignes:
            messages.error(request, "Fichier vide ou illisible.")
            return redirect("catalogue:article_import")

        request.session[SESSION_KEY_IMPORT_ARTICLES] = {
            "nom_fichier": fichier.name,
            "entetes": entetes,
            "lignes": lignes,
            "mapping": detecter_mapping(entetes),
        }
        return redirect("catalogue:article_import_preview")

    return render(request, "catalogue/article_import.html", {
        "actif": "articles",
        "champs": [(champ, LIBELLES.get(champ, champ), CHAMPS_FICHIER[champ]) for champ in CHAMPS_FICHIER],
    })


@login_required
@permission_required("catalogue.change_article", raise_exception=True)
def article_import_preview(request):
    """Etape 2 : correspondance des colonnes, aperçu des changements, puis
    application (creation des nouveaux articles, mise a jour des existants)."""
    from django.contrib import messages
    from django.shortcuts import redirect

    from .services import CHAMPS_FICHIER, LIBELLES, analyser, appliquer

    data = request.session.get(SESSION_KEY_IMPORT_ARTICLES)
    if not data:
        messages.error(request, "Aucun import en cours. Chargez d'abord un fichier.")
        return redirect("catalogue:article_import")

    entetes = data["entetes"]
    lignes = data["lignes"]
    mapping = data["mapping"]
    action = request.POST.get("action", "") if request.method == "POST" else ""

    if action == "annuler":
        del request.session[SESSION_KEY_IMPORT_ARTICLES]
        messages.info(request, "Import annulé.")
        return redirect("catalogue:article_liste")

    if action in ("remap", "appliquer"):
        mapping = {champ: (request.POST.get(f"map_{champ}") or None) for champ in CHAMPS_FICHIER}
        data["mapping"] = mapping
        request.session[SESSION_KEY_IMPORT_ARTICLES] = data
        request.session.modified = True

    erreurs_mapping = []
    if not mapping.get("libarticle") and not mapping.get("code"):
        erreurs_mapping.append(
            "Indiquez au moins la colonne du code article ou celle du libellé."
        )

    resultats, stats = ([], {}) if erreurs_mapping else analyser(lignes, mapping)

    if action == "appliquer" and not erreurs_mapping:
        try:
            compteurs = appliquer(
                lignes,
                mapping,
                creer_nouveaux=bool(request.POST.get("creer_nouveaux")),
                mettre_a_jour=bool(request.POST.get("mettre_a_jour")),
                appliquer_tarifs=bool(request.POST.get("appliquer_tarifs")),
            )
        except Exception as exc:  # noqa: BLE001
            messages.error(request, f"Import interrompu, aucune modification enregistrée : {exc}")
            return redirect("catalogue:article_import_preview")

        del request.session[SESSION_KEY_IMPORT_ARTICLES]
        messages.success(
            request,
            f"Catalogue mis à niveau : {compteurs['crees']} article(s) créé(s), "
            f"{compteurs['modifies']} mis à jour, {compteurs['tarifs']} tarif(s) "
            f"enregistré(s), {compteurs['ignores']} ligne(s) ignorée(s), "
            f"{compteurs['erreurs']} en erreur.",
        )
        return redirect("catalogue:article_liste")

    return render(request, "catalogue/article_import_preview.html", {
        "actif": "articles",
        "nom_fichier": data.get("nom_fichier", ""),
        "entetes": entetes,
        "mapping": mapping,
        "champs": [
            {
                "nom": champ,
                "libelle": LIBELLES.get(champ, champ),
                "selection": mapping.get(champ) or "",
            }
            for champ in CHAMPS_FICHIER
        ],
        "resultats": resultats,
        "stats": stats,
        "erreurs_mapping": erreurs_mapping,
        "nb_lignes": len(lignes),
    })


# --------------------------------------------------------------------------- #
#  Tarifs (prix d'un article, pour une liste de tarifs et un conditionnement)
# --------------------------------------------------------------------------- #

@login_required
def tarif_form(request, pk=None):
    tarif = get_object_or_404(Tarif, pk=pk) if pk else None
    code = "change_tarif" if tarif else "add_tarif"
    if not request.user.has_perm(f"catalogue.{code}"):
        raise PermissionDenied

    liste_tarif_id = request.POST.get("liste_tarif") or request.GET.get("liste_tarif") \
        or (tarif.liste_tarif_id if tarif else None)
    liste_tarif = get_object_or_404(ListeTarif, pk=liste_tarif_id) if liste_tarif_id else None

    if request.method == "POST":
        article_id = request.POST.get("article")
        conditionnement_id = request.POST.get("conditionnement")
        prix_unitaire = request.POST.get("prix_unitaire", "").strip()

        if not (liste_tarif and article_id and conditionnement_id and prix_unitaire):
            messages.error(request, "Tous les champs sont obligatoires.")
        else:
            tarif = tarif or Tarif()
            tarif.liste_tarif = liste_tarif
            tarif.article_id = article_id
            tarif.conditionnement_id = conditionnement_id
            tarif.prix_unitaire = prix_unitaire
            try:
                tarif.full_clean()
                tarif.save()
                messages.success(
                    request,
                    f"Tarif enregistré pour « {tarif.article.libarticle} » "
                    f"({tarif.conditionnement.nom}) sur la liste « {liste_tarif.nomtarif} ».",
                )
                return redirect("clients:tarif_detail", pk=liste_tarif.pk)
            except ValidationError as e:
                erreurs = e.message_dict if hasattr(e, "message_dict") else {"__all__": e.messages}
                for champ_erreurs in erreurs.values():
                    for err in champ_erreurs:
                        messages.error(request, err)

    contexte = {
        "actif": "tarifs",
        "tarif": tarif,
        "liste_tarif": liste_tarif,
        "articles": Article.objects.filter(actif=True).order_by("libarticle"),
        "conditionnements": Conditionnement.objects.all(),
    }
    return render(request, "catalogue/tarif_form.html", contexte)


# --------------------------------------------------------------------------- #
#  Import Excel des tarifs (prix pour des articles et listes existants)
# --------------------------------------------------------------------------- #
SESSION_KEY_IMPORT_TARIFS = "tarifs_import"


@login_required
@permission_required("catalogue.add_tarif", raise_exception=True)
def tarif_import_modele(request):
    reponse = HttpResponse(
        services.generer_modele_tarifs(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    reponse["Content-Disposition"] = 'attachment; filename="modele_tarifs.xlsx"'
    return reponse


@login_required
@permission_required("catalogue.add_tarif", raise_exception=True)
def tarif_import(request):
    if request.method == "POST":
        fichier = request.FILES.get("fichier")
        if not fichier:
            messages.error(request, "Veuillez sélectionner un fichier Excel (.xlsx).")
            return redirect("catalogue:tarif_import")
        try:
            entetes, lignes = lire_xlsx(fichier)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect("catalogue:tarif_import")

        if not lignes:
            messages.error(request, "Fichier vide ou illisible.")
            return redirect("catalogue:tarif_import")

        request.session[SESSION_KEY_IMPORT_TARIFS] = {"nom_fichier": fichier.name, "lignes": lignes}
        return redirect("catalogue:tarif_import_apercu")

    return render(request, "catalogue/tarif_import.html", {"actif": "tarifs"})


@login_required
@permission_required("catalogue.add_tarif", raise_exception=True)
def tarif_import_apercu(request):
    data = request.session.get(SESSION_KEY_IMPORT_TARIFS)
    if not data:
        messages.error(request, "Aucun import en cours. Chargez d'abord un fichier.")
        return redirect("catalogue:tarif_import")

    lignes = data["lignes"]
    action = request.POST.get("action", "") if request.method == "POST" else ""

    if action == "annuler":
        del request.session[SESSION_KEY_IMPORT_TARIFS]
        messages.info(request, "Import annulé.")
        return redirect("clients:tarifs_liste")

    resultats, stats = services.analyser_tarifs(lignes)

    if action == "appliquer":
        compteurs = services.appliquer_tarifs_import(
            lignes,
            creer_nouveaux=bool(request.POST.get("creer_nouveaux")),
            mettre_a_jour=bool(request.POST.get("mettre_a_jour")),
        )
        del request.session[SESSION_KEY_IMPORT_TARIFS]
        messages.success(
            request,
            f"Import terminé : {compteurs['crees']} tarif(s) créé(s), "
            f"{compteurs['modifies']} mis à jour, {compteurs['ignores']} ligne(s) ignorée(s), "
            f"{compteurs['erreurs']} en erreur.",
        )
        return redirect("clients:tarifs_liste")

    return render(request, "catalogue/tarif_import_apercu.html", {
        "actif": "tarifs",
        "nom_fichier": data.get("nom_fichier", ""),
        "resultats": resultats,
        "stats": stats,
        "nb_lignes": len(lignes),
    })


# --------------------------------------------------------------------------- #
#  Comparaison prix d'achat / prix de vente
# --------------------------------------------------------------------------- #
@login_required
def comparaison_prix(request):
    """Compare, pour un article (ou tous les articles d'un type), le prix
    d'achat et le(s) prix de vente (tarifs), en unite de base (bouteille)
    et par conditionnement."""
    q = request.GET.get("q", "").strip()
    type_id = request.GET.get("type", "").strip()

    articles = Article.objects.filter(actif=True).select_related("type_article").prefetch_related(
        "conditionnements", "tarifs__liste_tarif", "tarifs__conditionnement"
    )
    if q:
        articles = articles.filter(Q(libarticle__icontains=q) | Q(code__icontains=q))
    if type_id.isdigit():
        articles = articles.filter(type_article_id=type_id)
    articles = articles.order_by("libarticle")[:30] if (q or type_id) else Article.objects.none()

    resultats = []
    for a in articles:
        tarifs_par_cond = {}
        for t in a.tarifs.all():
            tarifs_par_cond.setdefault(t.conditionnement_id, []).append(t)

        lignes = []
        prix_vente_base = a.prix_bouteille or Decimal("0")
        prix_achat_base = a.prix_achat
        lignes.append({
            "conditionnement": "Bouteille (unité de base)",
            "prix_achat": prix_achat_base,
            "ventes": [{"liste": "Prix de référence (comptoir)", "prix": prix_vente_base}] if prix_vente_base else [],
            "marge": prix_vente_base - prix_achat_base if prix_vente_base else None,
        })
        for cond in a.conditionnements.all():
            prix_achat_cond = prix_achat_base * cond.nombre_unites
            tarifs_cond = sorted(
                tarifs_par_cond.get(cond.id, []), key=lambda t: t.liste_tarif.nomtarif
            )
            ventes = [{"liste": t.liste_tarif.nomtarif, "prix": t.prix_unitaire} for t in tarifs_cond]
            marge = None
            if ventes:
                prix_min = min(v["prix"] for v in ventes)
                marge = prix_min - prix_achat_cond
            lignes.append({
                "conditionnement": str(cond),
                "prix_achat": prix_achat_cond,
                "ventes": ventes,
                "marge": marge,
            })

        resultats.append({"article": a, "lignes": lignes})

    contexte = {
        "actif": "comparaison_prix",
        "q": q,
        "type_id": type_id,
        "types": TypeArticle.objects.order_by("nom"),
        "resultats": resultats,
    }
    return render(request, "catalogue/comparaison_prix.html", contexte)
