from django.contrib import admin, messages

from .models import (
    Approvisionnement,
    Inventaire,
    LigneApprovisionnement,
    LigneInventaire,
    MouvementStock,
    SortieExceptionnelle,
    Taxe,
    Fournisseur,
)


@admin.register(MouvementStock)
class MouvementStockAdmin(admin.ModelAdmin):
    list_display = ["date", "article", "type_mouvement", "quantite_unites", "reference", "motif"]
    list_filter = ["type_mouvement", "date"]
    search_fields = ["article__designation", "article__code", "reference"]
    date_hierarchy = "date"

    def has_change_permission(self, request, obj=None):
        # Les mouvements sont historiques : consultation seule.
        return False

    def has_add_permission(self, request):
        return False


class LigneApprovisionnementInline(admin.TabularInline):
    model = LigneApprovisionnement
    extra = 1
    autocomplete_fields = ["article", "conditionnement"]


@admin.register(Approvisionnement)
class ApprovisionnementAdmin(admin.ModelAdmin):
    list_display = ["reference", "date", "fournisseur", "afficher_taxes", "valide"]
    list_filter = ["valide", "date"]
    search_fields = ["reference", "fournisseur__nom_fournisseur"]
    autocomplete_fields = ["fournisseur"]
    inlines = [LigneApprovisionnementInline]
    actions = ["action_valider"]
    filter_horizontal = ["taxes"]
    
    fieldsets = (
        ("Informations générales", {
            "fields": ("reference", "date", "fournisseur", "note")
        }),
        ("Taxes applicables", {
            "fields": ("taxes",),
            "description": "Sélectionnez une ou plusieurs taxes à appliquer au montant HT de cet approvisionnement."
        }),
        ("Montants", {
            "fields": ("montant_ht", "montant_taxes", "montant_ttc"),
            "classes": ("collapse",)
        }),
        ("Validation", {
            "fields": ("valide",),
            "classes": ("collapse",)
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.valide:
            return ["reference", "date", "fournisseur", "note", "valide", "montant_ht", "montant_taxes", "montant_ttc"]
        return ["reference", "montant_ht", "montant_taxes", "montant_ttc"]

    def afficher_taxes(self, obj):
        """Affiche les taxes associées à l'approvisionnement."""
        taxes = obj.taxes.all()
        if not taxes:
            return "—"
        return ", ".join([f"{t.type_taxe} ({t.valeur_taxe}%)" for t in taxes])
    afficher_taxes.short_description = "Taxes"

    def montant_ht(self, obj):
        """Affiche le montant HT."""
        return f"{obj.montant_ht:.2f}"
    montant_ht.short_description = "Montant HT"

    def montant_taxes(self, obj):
        """Affiche le montant des taxes."""
        return f"{obj.montant_taxes:.2f}"
    montant_taxes.short_description = "Montant Taxes"

    def montant_ttc(self, obj):
        """Affiche le montant TTC."""
        return f"{obj.montant_ttc:.2f}"
    montant_ttc.short_description = "Montant TTC"

    @admin.action(description="Valider (generer les entrees en stock)")
    def action_valider(self, request, queryset):
        for appro in queryset:
            try:
                appro.valider(user=request.user)
                self.message_user(request, f"{appro.reference} valide.", messages.SUCCESS)
            except Exception as exc:  # noqa: BLE001
                self.message_user(request, f"{appro.reference} : {exc}", messages.ERROR)


@admin.register(Taxe)
class TaxeAdmin(admin.ModelAdmin):
    list_display = ["type_taxe", "nature", "valeur_taxe", "sur_boissons_alcoolisees"]
    list_filter = ["nature", "sur_boissons_alcoolisees"]
    list_editable = ["nature"]
    search_fields = ["type_taxe"]


@admin.register(SortieExceptionnelle)
class SortieExceptionnelleAdmin(admin.ModelAdmin):
    list_display = ["date", "article", "type_sortie", "quantite", "motif"]
    list_filter = ["type_sortie", "date"]
    search_fields = ["article__designation", "motif"]
    autocomplete_fields = ["article", "conditionnement"]

    def save_model(self, request, obj, form, change):
        if not change and not obj.cree_par:
            obj.cree_par = request.user
        super().save_model(request, obj, form, change)


class LigneInventaireInline(admin.TabularInline):
    model = LigneInventaire
    extra = 1
    autocomplete_fields = ["article"]
    readonly_fields = ["quantite_theorique", "ecart"]


@admin.register(Inventaire)
class InventaireAdmin(admin.ModelAdmin):
    list_display = ["reference", "date", "periode", "valide"]
    list_filter = ["periode", "valide", "date"]
    search_fields = ["reference"]
    inlines = [LigneInventaireInline]
    actions = ["action_valider"]

    def get_readonly_fields(self, request, obj=None):
        base = ["reference"]
        if obj and obj.valide:
            return base + ["date", "periode", "note", "valide"]
        return base

    @admin.action(description="Valider (calculer les ecarts et ajuster le stock)")
    def action_valider(self, request, queryset):
        for inv in queryset:
            try:
                inv.valider(user=request.user)
                self.message_user(request, f"{inv.reference} valide.", messages.SUCCESS)
            except Exception as exc:  # noqa: BLE001
                self.message_user(request, f"{inv.reference} : {exc}", messages.ERROR)

@admin.register(Fournisseur)
class FournisseurAdmin(admin.ModelAdmin):
    list_display = ["nom_fournisseur", "typefour", "emailfour", "telephonefourn", "afficher_taxes"]
    search_fields = ["nom_fournisseur"]
    filter_horizontal = ["taxes"]

    def afficher_taxes(self, obj):
        taxes = obj.taxes.all()
        if not taxes:
            return "—"
        return ", ".join(f"{t.type_taxe} ({t.valeur_taxe}%)" for t in taxes)
    afficher_taxes.short_description = "Taxes"