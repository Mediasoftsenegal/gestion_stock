import csv
import io

from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path

from .models import Article, Conditionnement, Tarif, TypeArticle,categorie_article


class ArticleImportForm(forms.Form):
    fichier = forms.FileField(label="Fichier CSV/TXT")
    separateur = forms.ChoiceField(
        choices=[
            (",", ","),
            (";", ";"),
            (" ", "espace"),
            ("\u00A0", "espace insécable"),
        ],
        initial=";",
        label="Séparateur",
    )


@admin.register(TypeArticle)
class TypeArticleAdmin(admin.ModelAdmin):
    list_display = ["nom", "description"]
    search_fields = ["nom"]


@admin.register(Conditionnement)
class ConditionnementAdmin(admin.ModelAdmin):
    list_display = ["nom", "nombre_unites"]
    search_fields = ["nom"]


class TarifInline(admin.TabularInline):
    model = Tarif
    extra = 1
    autocomplete_fields = ["liste_tarif", "conditionnement"]


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    change_list_template = "admin/catalogue/article/change_list.html"
    list_display = [
        "code",
        "libarticle",
        "type_article",
        "categorie_articles",
        "unite_base",
        "stock_actuel",
        "seuil_alerte",
        "fournisseur",
        "prix_achat",
        "prix_bouteille",
        "actif",
    ]
    list_filter = ["type_article", "actif"]
    search_fields = ["code", "libarticle"]
    filter_horizontal = ["conditionnements"]
    autocomplete_fields = ["type_article"]
    inlines = [TarifInline]

    def get_urls(self):
        urls = super().get_urls()
        info = (self.model._meta.app_label, self.model._meta.model_name)
        return [
            path(
                "import/",
                self.admin_site.admin_view(self.import_view),
                name=f"{info[0]}_{info[1]}_import",
            )
        ] + urls

    def import_view(self, request):
        if request.method == "POST":
            form = ArticleImportForm(request.POST, request.FILES)
            if form.is_valid():
                sep = form.cleaned_data["separateur"]
                uploaded_file = request.FILES["fichier"]
                fournisseur_from_form = form.cleaned_data.get("fournisseur")
                content = uploaded_file.read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(content), delimiter=sep)

                created = 0
                updated = 0

                for row in reader:
                    code = (row.get("code") or row.get("Code") or "").strip()
                    if not code:
                        continue

                    defaults = {}

                    libarticle = (row.get("libarticle") or row.get("designation") or "").strip()
                    if libarticle:
                        defaults["libarticle"] = libarticle

                    categorie = (
                        row.get("categorie")
                        or row.get("categorie_article")
                        or row.get("category")
                        or ""
                    ).strip()
                    if categorie:
                        defaults["categorie_article"] = categorie

                    type_name = (row.get("type") or row.get("type_article") or "").strip()
                    if type_name:
                        type_obj, _ = TypeArticle.objects.get_or_create(nom=type_name)
                        defaults["type_article"] = type_obj
                    else:
                        defaults["type_article"] = None

                    fournisseur_value = (
                        row.get("fournisseur")
                        or row.get("nom_fournisseur")
                        or row.get("fournisseur_nom")
                        or row.get("supplier")
                        or ""
                    ).strip()

                    resolved_fournisseur = self.resolve_fournisseur(fournisseur_value) if fournisseur_value else None
                    if resolved_fournisseur is None:
                        resolved_fournisseur = fournisseur_from_form

                    if resolved_fournisseur is not None:
                        defaults["fournisseur"] = resolved_fournisseur

                    unite_base = (row.get("unite_base") or row.get("unite") or "").strip()
                    if unite_base:
                        defaults["unite_base"] = unite_base

                    seuil = (row.get("seuil_alerte") or row.get("seuil") or "").strip()
                    if seuil:
                        try:
                            defaults["seuil_alerte"] = float(seuil.replace(",", "."))
                        except ValueError:
                            pass

                    prix = (row.get("prix_achat") or row.get("prix") or "").strip()
                    if prix:
                        try:
                            defaults["prix_achat"] = float(prix.replace(",", "."))
                        except ValueError:
                            pass

                    actif = (row.get("actif") or "").strip().lower()
                    if actif:
                        defaults["actif"] = actif in {"1", "true", "oui", "yes", "o", "y"}

                    _, created_obj = Article.objects.update_or_create(code=code, defaults=defaults)
                    if created_obj:
                        created += 1
                    else:
                        updated += 1

                messages.success(request, f"Import terminé : {created} créés, {updated} mis à jour.")
                return HttpResponseRedirect("../")
        else:
            form = ArticleImportForm()

        context = self.admin_site.each_context(request)
        context.update({
            "title": "Importer des articles",
            "form": form,
            "opts": self.model._meta,
        })
        return render(request, "admin/catalogue/article/import.html", context)

    @admin.display(description="stock (unités)")
    def stock_actuel(self, obj):
        return getattr(obj, "stock_actuel", 0)


@admin.register(Tarif)
class TarifAdmin(admin.ModelAdmin):
    list_display = ["article", "liste_tarif", "conditionnement", "prix_unitaire"]
    list_filter = ["liste_tarif", "conditionnement"]
    search_fields = ["article__libarticle", "article__code"]
    autocomplete_fields = ["article", "liste_tarif", "conditionnement"]
