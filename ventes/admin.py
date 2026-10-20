from django import forms
from django.contrib import admin, messages

from .models import (
    Commande,
    Facture,
    FactureRetour,
    LigneCommande,
    LigneFacture,
    LigneFactureRetour,
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
    readonly_fields = [
        "article", "conditionnement", "quantite", "prix_unitaire",
        "remise_unitaire", "montant_remise", "montant",
    ]

    @admin.display(description="remise")
    def montant_remise(self, obj):
        return obj.montant_remise

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


# --------------------------------------------------------------------------- #
#  Depot-vente : factures de retour
# --------------------------------------------------------------------------- #
class LigneFactureRetourInline(admin.TabularInline):
    model = LigneFactureRetour
    extra = 0
    can_delete = False
    fields = [
        "ligne_facture", "quantite_facturee", "quantite_vendue",
        "quantite_retournee", "unites_retournees", "montant_vendu", "montant_retourne",
    ]
    readonly_fields = [
        "ligne_facture", "quantite_facturee", "quantite_retournee",
        "unites_retournees", "montant_vendu", "montant_retourne",
    ]

    @admin.display(description="qte facturee")
    def quantite_facturee(self, obj):
        return obj.quantite_facturee

    @admin.display(description="qte retournee")
    def quantite_retournee(self, obj):
        return obj.quantite_retournee

    @admin.display(description="unites reprises")
    def unites_retournees(self, obj):
        return obj.unites_retournees

    @admin.display(description="montant vendu")
    def montant_vendu(self, obj):
        return obj.montant_vendu

    @admin.display(description="montant retourne")
    def montant_retourne(self, obj):
        return obj.montant_retourne

    def has_add_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        base = list(self.readonly_fields)
        if obj and obj.statut == FactureRetour.Statut.VALIDEE:
            base.append("quantite_vendue")
        return base


@admin.register(FactureRetour)
class FactureRetourAdmin(admin.ModelAdmin):
    list_display = [
        "numero", "facture", "client", "date", "statut",
        "quantite_vendue_totale", "quantite_retournee_totale", "montant_vendu_ttc",
    ]
    list_filter = ["statut", "date"]
    search_fields = ["numero", "facture__numero", "facture__client__nomclient"]
    readonly_fields = ["numero", "facture", "statut", "validee_le", "validee_par"]
    inlines = [LigneFactureRetourInline]
    actions = ["action_valider"]

    @admin.display(description="client")
    def client(self, obj):
        return obj.facture.client

    @admin.display(description="qte vendue")
    def quantite_vendue_totale(self, obj):
        return obj.quantite_vendue_totale

    @admin.display(description="qte retournee")
    def quantite_retournee_totale(self, obj):
        return obj.quantite_retournee_totale

    @admin.display(description="net a payer")
    def montant_vendu_ttc(self, obj):
        return obj.montant_vendu_ttc

    def has_add_permission(self, request):
        # Un retour nait d'une facture de sortie en depot-vente.
        return False

    @admin.action(description="Valider le retour (ecart remis en stock)")
    def action_valider(self, request, queryset):
        if not request.user.has_perm("ventes.peut_valider_retour"):
            self.message_user(
                request, "Vous n'avez pas le droit de valider une facture de retour.",
                messages.ERROR,
            )
            return
        for retour in queryset:
            try:
                retour.valider(user=request.user)
                self.message_user(
                    request,
                    f"{retour.numero} valide : {retour.unites_retournees_totales} unite(s) "
                    f"remise(s) en stock.",
                    messages.SUCCESS,
                )
            except Exception as exc:  # noqa: BLE001
                self.message_user(request, f"{retour.numero} : {exc}", messages.ERROR)
