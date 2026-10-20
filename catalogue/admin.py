from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import path
from django.utils.html import format_html

from .models import Article, Conditionnement, Tarif, TypeArticle,categorie_article


@admin.register(TypeArticle)
class TypeArticleAdmin(admin.ModelAdmin):
    list_display = ["nom", "description"]
    search_fields = ["nom"]
    list_filter = ["nom"]

@admin.register(categorie_article)
class Categorie_articleAdmin(admin.ModelAdmin):
    list_display = ["categories_articles", "description"]
    search_fields = ["categories_articles"]
    list_filter = ["categories_articles"]

@admin.register(Conditionnement)
class ConditionnementAdmin(admin.ModelAdmin):
    list_display = ["nom", "nombre_unites"]
    search_fields = ["nom"]
    list_filter = ["nom"]


class TarifInline(admin.TabularInline):
    model = Tarif
    extra = 1
    autocomplete_fields = ["liste_tarif", "conditionnement"]
    fields = ["liste_tarif", "conditionnement", "prix_unitaire"]

@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    change_list_template = "admin/catalogue/article/change_list.html"
    list_display = [
        "code",
        "libarticle",
        "categorie_articles",
        "type_article",
        "unite_base",
        "conditionnements_display",
        "stock_actuel",
        "seuil_alerte",
        "fournisseur",
        "prix_achat",
        "prix_bouteille",
        "actif",
    ]
    list_editable = ["categorie_articles","type_article","unite_base","seuil_alerte", "prix_achat", "prix_bouteille", "actif"]
    list_display_links = ["code", "libarticle"]
    list_filter = ["categorie_articles","type_article", "actif"]
    search_fields = ["code", "libarticle"]
    filter_horizontal = ["conditionnements"]
    autocomplete_fields = ["type_article"]
    inlines = [TarifInline]
    
    @admin.display(description="Conditionnements")
    def conditionnements_display(self, obj):
        conds = obj.conditionnements.all()
        if not conds:
            return "-"
        options = "".join(
            f'<option value="{c.pk}">{c.nom} ({c.nombre_unites} u.)</option>'
            for c in conds
        )
        return format_html(
            '<select style="max-width:180px;">{}</select>', format_html(options)
        )

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
        """L'import d'articles se fait desormais par l'ecran dedie (apercu des
        creations/mises a jour avant application) : on y redirige."""
        from django.urls import reverse

        return HttpResponseRedirect(reverse("catalogue:article_import"))

    @admin.display(description="stock (unités)")
    def stock_actuel(self, obj):
        return getattr(obj, "stock_actuel", 0)


@admin.register(Tarif)
class TarifAdmin(admin.ModelAdmin):
    list_display = ["article", "liste_tarif", "conditionnement", "prix_unitaire"]
    list_filter = ["liste_tarif", "conditionnement"]
    search_fields = ["article__libarticle", "article__code"]
    autocomplete_fields = ["article", "liste_tarif", "conditionnement"]
