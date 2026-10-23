import csv
import io
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q, Sum
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.core.paginator import Paginator

from catalogue.models import Article, Conditionnement, TypeArticle, categorie_article
from core.utils import PERIODES, reessayer_si_conflit_bd

from .models import (
    Approvisionnement,
    LigneApprovisionnement,
    LigneInventaire,
    Taxe,
    Inventaire,
    MouvementStock,
    SortieExceptionnelle,
    Fournisseur,
)


def _normaliser_texte(valeur):
    """Normalise un texte pour comparaison tolerante (accents/casse/espaces)."""
    valeur = (valeur or "").strip()
    valeur = unicodedata.normalize("NFD", valeur).encode("ascii", "ignore").decode("ascii")
    return " ".join(valeur.upper().split())


def _parser_decimal(valeur, defaut=Decimal("0")):
    valeur = (valeur or "").strip()
    if not valeur:
        return defaut
    valeur = valeur.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(valeur)
    except Exception:
        return defaut


def _parser_entier(valeur, defaut=0):
    dec = _parser_decimal(valeur, defaut=None)
    if dec is None:
        return defaut
    try:
        return int(dec.to_integral_value(rounding=ROUND_HALF_UP))
    except Exception:
        return defaut


# Candidats (normalises) pour la detection automatique des colonnes du CSV
# d'inventaire. Le premier match exact gagne, sinon on retombe sur un match
# "colonne contient le candidat".
CHAMPS_CSV_INVENTAIRE = {
    "libelle": ["ARTICLE.LIBARTICLE", "LIBARTICLE", "LIBELLE", "LIBELLE ARTICLE", "DESIGNATION", "ARTICLE"],
    "categorie": ["LIQUIDE", "CATEGORIE", "CATEGORIE ARTICLE", "CATEGORIES ARTICLES"],
    "prix_achat": ["PRIX ACHAT"],
    "quantite": ["TOT BTS TTC"],
    # Colonnes optionnelles : import detaille par conditionnement (casier/carton/pack)
    # + bouteilles au detail. Si l'une des deux est mappee, elle prend le pas sur
    # la colonne "quantite" (total deja calcule) ci-dessus.
    "conditionnement": ["CONDITIONNEMENT", "COND", "EMBALLAGE", "COLISAGE"],
    "quantite_carton": [
        "NBRE CARTON", "NOMBRE CARTON", "QTE CARTON", "QUANTITE CARTON",
        "NBRE CASIER", "QTE CASIER", "CASIER", "CARTON", "PACK", "NBRE PACK",
    ],
    "nombre_bouteilles": [
        "NBRE BOUTEILLE", "NOMBRE BOUTEILLE", "QTE BOUTEILLE", "NBRE BTS DETAIL",
        "BOUTEILLES", "BOUTEILLE",
    ],
}


def _detecter_colonne(entetes, candidats):
    normalisees = {h: _normaliser_texte(h) for h in entetes}
    for cand in candidats:
        for h, n in normalisees.items():
            if n == cand:
                return h
    for cand in candidats:
        for h, n in normalisees.items():
            if cand in n:
                return h
    return None


def stock_map():
    return {
        row["article_id"]: row["q"]
        for row in MouvementStock.objects.values("article_id").annotate(
            q=Sum("quantite_unites")
        )
    }


@login_required
def etat(request):
    """Etat du stock en temps reel (rafraichi via l'API).

    Affichage regroupe par type d'article : d'abord les articles dont le stock
    est superieur a 0, ensuite le reste (stock nul ou negatif), chaque bloc
    etant lui-meme regroupe par type d'article."""
    q = request.GET.get("q", "").strip()
    type_sel = request.GET.get("type", "").strip()
    filtre = request.GET.get("filtre", "").strip()  # "alerte" pour sous seuil

    articles = Article.objects.filter(actif=True).select_related("type_article")
    if q:
        articles = articles.filter(
            Q(libarticle__icontains=q) | Q(code__icontains=q)
        )
    if type_sel:
        articles = articles.filter(type_article_id=type_sel)
    articles = articles.order_by("libarticle")

    smap = stock_map()
    lignes = []
    valeur_totale = Decimal("0")
    nb_alerte = 0
    for a in articles:
        s = smap.get(a.id, 0) or 0
        sous = bool(a.seuil_alerte and s <= a.seuil_alerte)
        if sous:
            nb_alerte += 1
        valeur_totale += (a.prix_achat or Decimal("0")) * s
        if filtre == "alerte" and not sous:
            continue
        lignes.append({"a": a, "stock": s, "sous_seuil": sous,
                       "valeur": (a.prix_achat or Decimal("0")) * s})

    def _nom_type(l):
        t = l["a"].type_article
        return t.nom if t else "Sans type"

    # Tri : stock > 0 d'abord, puis par type d'article, puis par libelle.
    lignes.sort(key=lambda l: (
        l["stock"] <= 0,
        _nom_type(l) == "Sans type",
        _nom_type(l).lower(),
        l["a"].libarticle.lower(),
    ))

    paginator = Paginator(lignes, 25)
    page_number = request.GET.get("page", "1")
    page_obj = paginator.get_page(page_number)

    # Sections de la page courante : (en stock / stock nul) puis types d'article.
    sections = []
    for titre, en_stock in (("En stock", True), ("Stock nul ou épuisé", False)):
        groupes = {}
        for l in page_obj.object_list:
            if (l["stock"] > 0) == en_stock:
                groupes.setdefault(_nom_type(l), []).append(l)
        if groupes:
            sections.append({
                "titre": titre,
                "en_stock": en_stock,
                "nb": sum(len(g) for g in groupes.values()),
                "groupes": list(groupes.items()),
            })

    params = request.GET.copy()
    params.pop("page", None)

    contexte = {
        "actif": "stock",
        "lignes": page_obj.object_list,
        "sections": sections,
        "page_obj": page_obj,
        "current_params": params,
        "types": TypeArticle.objects.all(),
        "q": q, "type_sel": type_sel, "filtre": filtre,
        "valeur_totale": valeur_totale,
        "nb_alerte": nb_alerte,
        "nb_articles": articles.count(),
    }
    return render(request, "stock/etat.html", contexte)


@login_required
def appro_liste(request):
    """Liste des approvisionnements avec filtres."""
    appros = (
        Approvisionnement.objects.select_related("fournisseur")
        .prefetch_related("taxes", "lignes")
        .order_by("-date")
    )

    # Filtres
    statut = request.GET.get("statut")
    if statut == "valide":
        appros = appros.filter(valide=True)
    elif statut == "brouillon":
        appros = appros.filter(valide=False)

    fournisseur = request.GET.get("fournisseur")
    if fournisseur:
        appros = appros.filter(fournisseur_id=fournisseur)

    # Pagination
    paginator = Paginator(appros, 20)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    fournisseurs = Fournisseur.objects.order_by("nom_fournisseur")

    return render(request, "stock/appro_liste.html", {
        "page_obj": page_obj,
        "fournisseurs": fournisseurs,
        "statut": statut,
        "fournisseur": fournisseur,
    })


@login_required
@require_POST
def appro_valider(request, pk):
    appro = get_object_or_404(Approvisionnement, pk=pk)

    if appro.valide:
        messages.error(request, "Approvisionnement déjà validé.")
    elif not appro.lignes.exists():
        messages.error(request, "Impossible de valider sans lignes.")
    else:
        try:
            appro.valider(user=request.user)
            messages.success(request, f"Approvisionnement {appro.reference} validé.")
        except Exception as e:
            messages.error(request, f"Erreur : {e}")

    return redirect("stock:appro_detail", pk=appro.pk)


@login_required
def sortie_liste(request):
    sorties = SortieExceptionnelle.objects.select_related(
        "article", "conditionnement"
    ).order_by("-date")
    contexte = {"actif": "sorties", "sorties": sorties}
    return render(request, "stock/sortie_liste.html", contexte)


@login_required
def inventaire_liste(request):
    inventaires = Inventaire.objects.prefetch_related("lignes").order_by("-date")
    contexte = {"actif": "inventaires", "inventaires": inventaires}
    return render(request, "stock/inventaire_liste.html", contexte)


@login_required
@require_POST
def inventaire_valider(request, pk):
    inv = get_object_or_404(Inventaire, pk=pk)
    try:
        inv.valider(user=request.user)
        messages.success(request, f"Inventaire {inv.reference} validé : ajustements appliqués.")
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
    return redirect("stock:inventaire_liste")


SESSION_KEY_IMPORT_INVENTAIRE = "inventaire_import"


@login_required
def inventaire_import(request):
    """Etape 1 : upload du CSV d'inventaire physique.

    L'import peut se faire en plusieurs fois : soit en créant un nouvel
    inventaire, soit en complétant un inventaire existant non encore validé
    (utile pour charger le fichier catégorie par catégorie, ou en plusieurs
    fichiers)."""
    inventaires_en_cours = Inventaire.objects.filter(valide=False).order_by("-date")

    if request.method == "POST":
        fichier = request.FILES.get("fichier")
        separateur = request.POST.get("separateur", "").strip()
        inventaire_id = request.POST.get("inventaire_id", "").strip()

        inventaire_existant = None
        if inventaire_id:
            inventaire_existant = get_object_or_404(Inventaire, pk=inventaire_id, valide=False)
            date = inventaire_existant.date.isoformat()
            periode = inventaire_existant.periode
        else:
            date = request.POST.get("date", "").strip()
            periode = request.POST.get("periode", "MOIS").strip()

        if not fichier:
            messages.error(request, "Veuillez sélectionner un fichier CSV.")
            return redirect("stock:inventaire_import")
        if not date:
            messages.error(request, "Veuillez indiquer la date de l'inventaire.")
            return redirect("stock:inventaire_import")

        brut = fichier.read()
        try:
            contenu = brut.decode("utf-8-sig")
        except UnicodeDecodeError:
            contenu = brut.decode("latin-1")

        if not separateur:
            premiere_ligne = contenu.splitlines()[0] if contenu else ""
            separateur = max([";", ",", "\t"], key=lambda s: premiere_ligne.count(s))

        lecteur = csv.DictReader(io.StringIO(contenu), delimiter=separateur)
        entetes = lecteur.fieldnames or []
        lignes = [row for row in lecteur if any((v or "").strip() for v in row.values())]

        if not entetes or not lignes:
            messages.error(request, "Fichier vide ou illisible.")
            return redirect("stock:inventaire_import")

        mapping = {
            champ: _detecter_colonne(entetes, candidats)
            for champ, candidats in CHAMPS_CSV_INVENTAIRE.items()
        }

        request.session[SESSION_KEY_IMPORT_INVENTAIRE] = {
            "entetes": entetes,
            "lignes": lignes,
            "mapping": mapping,
            "date": date,
            "periode": periode,
            "inventaire_id": inventaire_existant.id if inventaire_existant else None,
        }
        return redirect("stock:inventaire_import_preview")

    return render(request, "stock/inventaire_import.html", {
        "actif": "inventaires",
        "periodes": PERIODES,
        "inventaires_en_cours": inventaires_en_cours,
    })


@login_required
@reessayer_si_conflit_bd()
def inventaire_import_preview(request):
    """Etape 2 : mapping des colonnes, comparaison au catalogue, creation des
    articles manquants (apres validation de l'utilisateur connecte) puis
    creation de l'inventaire et de ses lignes."""
    data = request.session.get(SESSION_KEY_IMPORT_INVENTAIRE)
    if not data:
        messages.error(request, "Aucun import en cours. Merci de charger un fichier.")
        return redirect("stock:inventaire_import")

    if request.method == "POST" and request.POST.get("action") == "annuler":
        del request.session[SESSION_KEY_IMPORT_INVENTAIRE]
        messages.info(request, "Import annulé.")
        return redirect("stock:inventaire_liste")

    entetes = data["entetes"]
    lignes = data["lignes"]
    mapping = data["mapping"]

    if request.method == "POST" and request.POST.get("action") == "remap":
        mapping = {
            "libelle": request.POST.get("map_libelle") or None,
            "categorie": request.POST.get("map_categorie") or None,
            "prix_achat": request.POST.get("map_prix_achat") or None,
            "quantite": request.POST.get("map_quantite") or None,
            "conditionnement": request.POST.get("map_conditionnement") or None,
            "quantite_carton": request.POST.get("map_quantite_carton") or None,
            "nombre_bouteilles": request.POST.get("map_nombre_bouteilles") or None,
        }
        data["mapping"] = mapping
        request.session[SESSION_KEY_IMPORT_INVENTAIRE] = data
        request.session.modified = True

    # Mode "detail" : au moins une des colonnes carton/bouteilles est mappee.
    # Dans ce cas le Total bts est recalcule ligne a ligne, et la colonne
    # "quantite" (TOT BTS TTC) n'est plus necessaire.
    mode_carton = bool(mapping.get("quantite_carton") or mapping.get("nombre_bouteilles"))

    erreurs_mapping = []
    if not mapping.get("libelle"):
        erreurs_mapping.append("Choisissez la colonne du libellé article.")
    if not mode_carton and not mapping.get("quantite"):
        erreurs_mapping.append(
            "Choisissez la colonne de quantité (TOT BTS TTC), ou bien les colonnes "
            "casier/carton et/ou bouteilles."
        )

    lignes_analysees = []
    manquantes = []
    a_conditionner = []
    erreurs_confirmation = []
    categories = []
    conditionnements = []
    lignes_par_categorie = {}

    # Sur la confirmation, on ne retient que les cases cochées ; sinon (premier
    # affichage ou apres remap) tout est coche par defaut.
    selection_soumise = request.method == "POST" and request.POST.get("action") == "confirmer"

    if not erreurs_mapping:
        categories = list(categorie_article.objects.all())
        categories_par_nom = {_normaliser_texte(c.categories_articles): c for c in categories}

        articles_par_nom = {}
        for art in Article.objects.select_related("categorie_articles"):
            articles_par_nom.setdefault(_normaliser_texte(art.libarticle), art)

        conditionnements = list(Conditionnement.objects.all())
        conditionnements_par_nom = {_normaliser_texte(c.nom): c for c in conditionnements}

        for i, row in enumerate(lignes):
            libelle = (row.get(mapping["libelle"]) or "").strip()
            if not libelle:
                continue
            prix = (
                _parser_decimal(row.get(mapping["prix_achat"]))
                if mapping.get("prix_achat") else Decimal("0")
            )
            categorie_raw = (
                (row.get(mapping["categorie"]) or "").strip()
                if mapping.get("categorie") else ""
            )
            article = articles_par_nom.get(_normaliser_texte(libelle))
            categorie_suggeree = (
                categories_par_nom.get(_normaliser_texte(categorie_raw))
                if categorie_raw else None
            )

            conditionnement = None
            conditionnement_raw = ""
            quantite_carton = 0
            nombre_bouteilles_saisi = 0

            if mode_carton:
                quantite_carton = (
                    _parser_entier(row.get(mapping["quantite_carton"]))
                    if mapping.get("quantite_carton") else 0
                )
                nombre_bouteilles_saisi = (
                    _parser_entier(row.get(mapping["nombre_bouteilles"]))
                    if mapping.get("nombre_bouteilles") else 0
                )
                conditionnement_raw = (
                    (row.get(mapping["conditionnement"]) or "").strip()
                    if mapping.get("conditionnement") else ""
                )
                if conditionnement_raw:
                    conditionnement = conditionnements_par_nom.get(_normaliser_texte(conditionnement_raw))

                if quantite_carton and not conditionnement:
                    # On connait le nombre de cartons mais pas leur contenance :
                    # l'utilisateur devra choisir le conditionnement a l'ecran.
                    quantite = nombre_bouteilles_saisi
                else:
                    facteur = conditionnement.nombre_unites if conditionnement else 1
                    quantite = quantite_carton * facteur + nombre_bouteilles_saisi
            else:
                quantite = _parser_entier(row.get(mapping["quantite"]))

            selectionnee = (
                request.POST.get(f"select_{i}") is not None
                if selection_soumise else True
            )

            lignes_analysees.append({
                "index": i,
                "libelle": libelle,
                "quantite": quantite,
                "prix_achat": prix,
                "categorie_raw": categorie_raw,
                "categorie_suggeree": categorie_suggeree,
                "article": article,
                "mode_carton": mode_carton,
                "conditionnement_raw": conditionnement_raw,
                "conditionnement": conditionnement,
                "quantite_carton": quantite_carton,
                "nombre_bouteilles_saisi": nombre_bouteilles_saisi,
                "selectionnee": selectionnee,
            })

        manquantes = [l for l in lignes_analysees if not l["article"]]
        a_conditionner = [
            l for l in lignes_analysees
            if l["mode_carton"] and l["quantite_carton"] and not l["conditionnement"]
        ]

        # Regroupement par categorie pour l'affichage : categorie de l'article
        # deja au catalogue, sinon categorie suggeree/saisie dans le CSV.
        def _nom_categorie(l):
            if l["article"] and l["article"].categorie_articles:
                return l["article"].categorie_articles.categories_articles
            if l["categorie_suggeree"]:
                return l["categorie_suggeree"].categories_articles
            if l["categorie_raw"]:
                return l["categorie_raw"]
            return "Sans catégorie"

        groupes = {}
        for l in lignes_analysees:
            groupes.setdefault(_nom_categorie(l), []).append(l)
        lignes_par_categorie = dict(
            sorted(groupes.items(), key=lambda kv: (kv[0] == "Sans catégorie", kv[0].lower()))
        )

        if request.method == "POST" and request.POST.get("action") == "confirmer":
            lignes_a_importer = [l for l in lignes_analysees if l["selectionnee"]]
            if not lignes_a_importer:
                erreurs_confirmation.append("Sélectionnez au moins un produit à importer.")

            codes_soumis = {}
            for l in manquantes:
                if not l["selectionnee"]:
                    continue
                i = l["index"]
                code = (request.POST.get(f"code_{i}") or "").strip()
                l["code_saisi"] = code
                l["categorie_id_saisi"] = request.POST.get(f"categorie_{i}") or ""
                l["prix_saisi"] = request.POST.get(f"prix_{i}") or ""

                if not code:
                    # Code laisse vide : genere automatiquement a la creation
                    # de l'article (voir Article.save()).
                    continue
                if code in codes_soumis:
                    erreurs_confirmation.append(f"Code '{code}' utilisé plusieurs fois dans ce lot.")
                    continue
                codes_soumis[code] = l
                if Article.objects.filter(code=code).exists():
                    erreurs_confirmation.append(f"Code '{code}' déjà utilisé par un article existant.")

            conditionnements_par_id = {c.id: c for c in conditionnements}
            for l in a_conditionner:
                if not l["selectionnee"]:
                    continue
                i = l["index"]
                choix = request.POST.get(f"cond_{i}") or ""
                l["conditionnement_id_saisi"] = choix

                if choix == "nouveau":
                    nom_nouveau = (request.POST.get(f"cond_nom_{i}") or "").strip()
                    unites_str = request.POST.get(f"cond_unites_{i}") or ""
                    l["cond_nom_saisi"] = nom_nouveau
                    l["cond_unites_saisi"] = unites_str
                    unites_nouveau = _parser_entier(unites_str)
                    if not nom_nouveau or not unites_nouveau:
                        erreurs_confirmation.append(
                            f"Renseignez le nom et le nombre d'unités du nouveau conditionnement "
                            f"pour « {l['libelle']} »."
                        )
                        continue
                    l["cond_nouveau_valide"] = (nom_nouveau, unites_nouveau)
                    # Apercu ; la creation effective et le calcul definitif se font
                    # a la confirmation, dans la transaction.
                    l["quantite"] = l["quantite_carton"] * unites_nouveau + l["nombre_bouteilles_saisi"]
                elif not choix:
                    erreurs_confirmation.append(
                        f"Choisissez ou créez le conditionnement pour « {l['libelle']} » "
                        f"({l['quantite_carton']} carton(s), valeur '{l['conditionnement_raw'] or '—'}' inconnue)."
                    )
                else:
                    cond = conditionnements_par_id.get(int(choix))
                    if cond:
                        l["conditionnement"] = cond
                        l["quantite"] = l["quantite_carton"] * cond.nombre_unites + l["nombre_bouteilles_saisi"]

            if not erreurs_confirmation:
                with transaction.atomic():
                    if data.get("inventaire_id"):
                        inv = get_object_or_404(Inventaire, pk=data["inventaire_id"], valide=False)
                    else:
                        inv = Inventaire.objects.create(date=data["date"], periode=data["periode"])
                    conditionnements_crees = {}
                    for l in lignes_a_importer:
                        nouveau = l.get("cond_nouveau_valide")
                        if nouveau:
                            nom_nouveau, unites_nouveau = nouveau
                            cle = _normaliser_texte(nom_nouveau)
                            cond = (
                                conditionnements_crees.get(cle)
                                or conditionnements_par_nom.get(cle)
                                or Conditionnement.objects.create(nom=nom_nouveau, nombre_unites=unites_nouveau)
                            )
                            conditionnements_crees[cle] = cond
                            l["conditionnement"] = cond
                            l["quantite"] = l["quantite_carton"] * cond.nombre_unites + l["nombre_bouteilles_saisi"]

                        article = l["article"]
                        if not article:
                            categorie_id = l.get("categorie_id_saisi") or None
                            article = Article.objects.create(
                                code=l["code_saisi"],
                                libarticle=l["libelle"],
                                categorie_articles_id=int(categorie_id) if categorie_id else None,
                                prix_achat=_parser_decimal(l.get("prix_saisi"), defaut=l["prix_achat"]),
                            )
                        LigneInventaire.objects.update_or_create(
                            inventaire=inv,
                            article=article,
                            defaults={"quantite_physique": l["quantite"]},
                        )
                del request.session[SESSION_KEY_IMPORT_INVENTAIRE]
                articles_crees = sum(1 for l in lignes_a_importer if not l["article"])
                messages.success(
                    request,
                    f"{inv.reference} : {len(lignes_a_importer)} ligne(s) importée(s), "
                    f"{articles_crees} article(s) créé(s). Vous pouvez charger un autre fichier "
                    f"pour compléter cet inventaire tant qu'il n'est pas validé."
                )
                return redirect("stock:inventaire_liste")

    return render(request, "stock/inventaire_import_preview.html", {
        "actif": "inventaires",
        "entetes": entetes,
        "mapping": mapping,
        "mode_carton": mode_carton,
        "erreurs_mapping": erreurs_mapping,
        "erreurs_confirmation": erreurs_confirmation,
        "lignes_analysees": lignes_analysees,
        "lignes_par_categorie": lignes_par_categorie,
        "manquantes": manquantes,
        "a_conditionner": a_conditionner,
        "trouvees": len(lignes_analysees) - len(manquantes),
        "categories": categories,
        "conditionnements": conditionnements,
        "date": data["date"],
        "periode": data["periode"],
        "inventaire_id": data.get("inventaire_id"),
    })


@login_required
@reessayer_si_conflit_bd()
def appro_creer(request):
    """Créer un nouvel approvisionnement (hors admin)."""
    if request.method == "POST":
        date = request.POST.get("date")
        fournisseur_id = request.POST.get("fournisseur")
        numero = request.POST.get("numero", "").strip()
        note = request.POST.get("note", "").strip()
        taxes_ids = request.POST.getlist("taxes")

        if not numero:
            messages.error(request, "Le numéro d'appro est obligatoire.")
            return redirect("stock:appro_creer")
        if Approvisionnement.objects.filter(numero=numero).exists():
            messages.error(request, f"Le numéro d'appro « {numero} » existe déjà.")
            return redirect("stock:appro_creer")

        fournisseur = get_object_or_404(Fournisseur, pk=fournisseur_id)

        # On ne retient que les taxes reellement rattachees au fournisseur.
        taxes_valides = fournisseur.taxes.filter(pk__in=taxes_ids)

        with transaction.atomic():
            appro = Approvisionnement.objects.create(
                date=date,
                fournisseur=fournisseur,
                numero=numero,
                note=note,
            )
            if taxes_valides:
                appro.taxes.set(taxes_valides)

        messages.success(request, f"Approvisionnement {appro.reference} créé.")
        return redirect("stock:appro_editer", pk=appro.pk)

    fournisseurs = Fournisseur.objects.order_by("nom_fournisseur")
    return render(request, "stock/appro_creer.html", {"fournisseurs": fournisseurs})


@login_required
def fournisseur_taxes_json(request, pk):
    """Retourne les taxes d'un fournisseur (pour le chargement dynamique du formulaire)."""
    fournisseur = get_object_or_404(Fournisseur, pk=pk)
    taxes = [
        {"id": t.id, "type_taxe": t.type_taxe, "valeur_taxe": float(t.valeur_taxe)}
        for t in fournisseur.taxes.all()
    ]
    return JsonResponse({"taxes": taxes})


@login_required
def appro_editer(request, pk):
    """Éditer un approvisionnement et ses lignes (hors admin)."""
    appro = get_object_or_404(Approvisionnement, pk=pk)

    if appro.valide:
        messages.error(request, "Impossible de modifier un approvisionnement validé.")
        return redirect("stock:appro_detail", pk=appro.pk)

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "update_appro":
            appro.date = request.POST.get("date", appro.date)
            fournisseur_id = request.POST.get("fournisseur")
            if fournisseur_id:
                appro.fournisseur = get_object_or_404(Fournisseur, pk=fournisseur_id)
            appro.note = request.POST.get("note", appro.note)
            appro.save()
            # Seules les taxes rattachees au fournisseur sont retenues.
            taxes_ids = request.POST.getlist("taxes")
            appro.taxes.set(appro.fournisseur.taxes.filter(pk__in=taxes_ids))
            messages.success(request, "Approvisionnement mis à jour.")

        elif action == "add_ligne":
            article_id = request.POST.get("article_id")
            quantite = request.POST.get("quantite")
            conditionnement_id = request.POST.get("conditionnement_id")
            prix_achat = request.POST.get("prix_achat")
            remise = request.POST.get("remise", 0)

            if article_id and quantite:
                article = get_object_or_404(Article, pk=article_id)
                conditionnement = None
                if conditionnement_id:
                    conditionnement = get_object_or_404(Conditionnement, pk=conditionnement_id)

                # nobre_bouteilles est recalcule automatiquement dans LigneApprovisionnement.save().
                LigneApprovisionnement.objects.create(
                    approvisionnement=appro,
                    article=article,
                    conditionnement=conditionnement,
                    quantite=int(quantite),
                    prix_achat=prix_achat or None,
                    remise=Decimal(str(remise or 0)),
                )
                messages.success(request, f"{article.libarticle} ajouté.")
            else:
                messages.error(request, "Article et quantité sont obligatoires.")

        elif action == "delete_ligne":
            ligne_id = request.POST.get("ligne_id")
            ligne = get_object_or_404(LigneApprovisionnement, pk=ligne_id, approvisionnement=appro)
            article_name = ligne.article.libarticle
            ligne.delete()
            messages.success(request, f"{article_name} supprimé.")

        return redirect("stock:appro_editer", pk=appro.pk)

    lignes = appro.lignes.select_related("article", "conditionnement")
    articles = Article.objects.filter(actif=True, fournisseur=appro.fournisseur).order_by("libarticle")
    conditionnements = Conditionnement.objects.all()
    taxes_fournisseur = appro.fournisseur.taxes.all()
    taxes_selectionnees_ids = set(appro.taxes.values_list("id", flat=True))

    return render(request, "stock/appro_editer.html", {
        "appro": appro,
        "lignes": lignes,
        "articles": articles,
        "conditionnements": conditionnements,
        "taxes_fournisseur": taxes_fournisseur,
        "taxes_selectionnees_ids": taxes_selectionnees_ids,
    })


@login_required
def appro_detail(request, pk):
    appro = get_object_or_404(Approvisionnement, pk=pk)
    lignes = appro.lignes.all()
    
    montant_ht = appro.montant_ht
    ht_alcool = Decimal('0')

    for ligne in lignes:
        if ligne.est_alcoolise:
            ht_alcool += ligne.montant_ht
    
    # Calcul des taxes
    taxes_details = []
    montant_tba = Decimal('0')
    montant_tigb = Decimal('0')
    montant_taxe_egalisation = Decimal('0')
    
    if appro.fournisseur and appro.fournisseur.nom_fournisseur == 'SOBOA':
        for tax in appro.taxes.all():
            if tax.type_taxe == 'T.B.A':
                montant_tba = round(ht_alcool * (tax.valeur_taxe / 100), 2)
                taxes_details.append({
                    "nom": "T.B.A",
                    "taux": tax.valeur_taxe,
                    "montant": montant_tba,
                    "base": ht_alcool,
                })
            elif tax.type_taxe == 'T.I.G.B':
                ht_non_alcool = montant_ht - ht_alcool
                montant_tigb = round(ht_non_alcool * (tax.valeur_taxe / 100), 2)
                taxes_details.append({
                    "nom": "T.I.G.B",
                    "taux": tax.valeur_taxe,
                    "montant": montant_tigb,
                    "base": ht_non_alcool,
                })
            elif tax.type_taxe == 'TAXE D\'EGALISATION':
                # Taxe d'égalisation = 2% mais toujours à 0 dans ce cas
                montant_taxe_egalisation = Decimal('0')
                taxes_details.append({
                    "nom": "TAXE D\'EGALISATION",
                    "taux": tax.valeur_taxe,
                    "montant": montant_taxe_egalisation,
                    "base": Decimal('0'),
                })
            elif tax.type_taxe == 'TVA':
                # La TVA sera calculée après les autres taxes
                # On ne l'ajoute pas tout de suite
                pass
            else:
                montant = round(montant_ht * (tax.valeur_taxe / 100), 2)
                taxes_details.append({
                    "nom": tax.type_taxe,
                    "taux": tax.valeur_taxe,
                    "montant": montant,
                    "base": montant_ht,
                })
        
        # Calcul de la TVA après avoir calculé T.B.A et T.I.G.B
        # TVA = (montant_ht + T.B.A + T.I.G.B) * 18%
        base_tva = montant_ht + montant_tba + montant_tigb
        montant_tva = round(base_tva * Decimal('0.18'), 2)  # 18%
        
        # Ajouter la TVA aux détails
        taxes_details.append({
            "nom": "TVA",
            "taux": 18,
            "montant": montant_tva,
            "base": base_tva,
        })
        
    else:
        # Pour les autres fournisseurs, calcul standard
        for tax in appro.taxes.all():
            if tax.type_taxe == 'TVA':
                # TVA standard sur le montant HT
                montant = round(montant_ht * (tax.valeur_taxe / 100), 2)
            else:
                montant = round(montant_ht * (tax.valeur_taxe / 100), 2)
            
            taxes_details.append({
                "nom": tax.type_taxe,
                "taux": tax.valeur_taxe,
                "montant": montant,
                "base": montant_ht,
            })
    
    total_taxes = sum(tax["montant"] for tax in taxes_details)
    total_facture = round(montant_ht + total_taxes, 2)
    
    context = {
        "appro": appro,
        "lignes": lignes,
        "taxes_details": taxes_details,
        "total_facture": total_facture,
        "montant_ht": montant_ht,
        "ht_alcool": ht_alcool,
        "ht_non_alcool": montant_ht - ht_alcool,
        "is_soboa": appro.fournisseur and appro.fournisseur.nom_fournisseur == 'SOBOA',
        "montant_tba": montant_tba,
        "montant_tigb": montant_tigb,
    }
    
    return render(request, 'stock/appro_detail.html', context)

# etat_stock : meme vue que etat (routage urls.py).
etat_stock = etat
