from django import forms
from django.contrib import admin, messages

from .models import (
    Commande,
    Facture,
    LigneCommande,
    LigneFacture,
    Paiement,
)


class LigneCommandeInline(admin.TabularInline):
    model = LigneCommande
    extra = 1
    autocomplete_fields = ["article", "conditionnement"]


@admin.register(Commande)
class CommandeAdmin(admin.ModelAdmin):
    list_display = ["reference", "client", "date", "statut", "montant_total"]
    list_filter = ["statut", "date"]
    search_fields = ["reference", "client__nom", "client__code"]
    autocomplete_fields = ["client", "liste_tarif"]
    inlines = [LigneCommandeInline]
    actions = ["action_valider"]

    @admin.display(description="montant")
    def montant_total(self, obj):
        return obj.montant_total

    def get_readonly_fields(self, request, obj=None):
        base = ["reference"]
        if obj and obj.statut != Commande.Statut.BON_DE_COMMANDE:
            return base + ["client", "liste_tarif", "date", "statut", "note"]
        return base

    def save_model(self, request, obj, form, change):
        if not obj.cree_par:
            obj.cree_par = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description="Valider -> generer la facture")
    def action_valider(self, request, queryset):
        if not request.user.has_perm("ventes.peut_valider_commande"):
            self.message_user(
                request, "Vous n'avez pas le droit de valider une commande.", messages.ERROR
            )
            return
        for commande in queryset:
            try:
                facture = commande.valider(user=request.user)
                self.message_user(
                    request,
                    f"{commande.reference} -> facture {facture.numero} creee.",
                    messages.SUCCESS,
                )
            except Exception as exc:  # noqa: BLE001
                self.message_user(request, f"{commande.reference} : {exc}", messages.ERROR)


class LigneFactureInline(admin.TabularInline):
    model = LigneFacture
    extra = 0
    can_delete = False
    readonly_fields = ["article", "conditionnement", "quantite", "prix_unitaire", "montant"]

    @admin.display(description="montant")
    def montant(self, obj):
        return obj.montant

    def has_add_permission(self, request, obj=None):
        return False


class PaiementInline(admin.TabularInline):
    model = Paiement
    extra = 0
    autocomplete_fields = []
    fields = ["type_paiement", "mode", "montant", "date", "reference"]


class AnnulationForm(forms.Form):
    justificatif = forms.CharField(widget=forms.Textarea, label="Justificatif d'annulation")


@admin.register(Facture)
class FactureAdmin(admin.ModelAdmin):
    list_display = [
        "numero",
        "client",
        "date",
        "montant_total",
        "montant_paye",
        "reliquat",
        "statut_paiement",
        "annulee",
    ]
    list_filter = ["annulee", "date"]
    search_fields = ["numero", "client__nom", "client__code"]
    inlines = [LigneFactureInline, PaiementInline]
    actions = ["action_annuler"]
    readonly_fields = [
        "numero",
        "commande",
        "client",
        "date",
        "annulee",
        "justificatif_annulation",
        "annulee_par",
        "annulee_le",
    ]

    @admin.display(description="total")
    def montant_total(self, obj):
        return obj.montant_total

    @admin.display(description="paye")
    def montant_paye(self, obj):
        return obj.montant_paye

    @admin.display(description="reliquat")
    def reliquat(self, obj):
        return obj.reliquat

    @admin.display(description="paiement")
    def statut_paiement(self, obj):
        return obj.statut_paiement.label

    # Les factures ne se suppriment jamais depuis l'admin (regle de gestion).
    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        # Une facture nait de la validation d'une commande, jamais a la main.
        return False

    @admin.action(description="Annuler la facture (justificatif requis)")
    def action_annuler(self, request, queryset):
        from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
        from django.template.response import TemplateResponse

        if not request.user.has_perm("ventes.peut_annuler_facture"):
            self.message_user(
                request, "Vous n'avez pas le droit d'annuler une facture.", messages.ERROR
            )
            return

        # Etape 2 : le justificatif a ete saisi -> on annule.
        if request.POST.get("confirmer_annulation"):
            form = AnnulationForm(request.POST)
            if form.is_valid():
                justificatif = form.cleaned_data["justificatif"]
                for facture in queryset:
                    try:
                        facture.annuler(user=request.user, justificatif=justificatif)
                        self.message_user(
                            request, f"{facture.numero} annulee.", messages.SUCCESS
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.message_user(
                            request, f"{facture.numero} : {exc}", messages.ERROR
                        )
                return None
        else:
            form = AnnulationForm()

        # Etape 1 : afficher le formulaire de justificatif.
        contexte = {
            **self.admin_site.each_context(request),
            "titre": "Annulation de facture(s)",
            "factures": queryset,
            "form": form,
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
            "selection": queryset.values_list("pk", flat=True),
        }
        return TemplateResponse(request, "admin/ventes/annuler_facture.html", contexte)


@admin.register(Paiement)
class PaiementAdmin(admin.ModelAdmin):
    list_display = ["facture", "type_paiement", "mode", "montant", "date"]
    list_filter = ["type_paiement", "mode", "date"]
    search_fields = ["facture__numero", "reference"]
    autocomplete_fields = ["facture"]

    def save_model(self, request, obj, form, change):
        if not obj.cree_par:
            obj.cree_par = request.user
        super().save_model(request, obj, form, change)
