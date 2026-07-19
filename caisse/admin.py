from django.contrib import admin, messages

from .models import Caisse


@admin.register(Caisse)
class CaisseAdmin(admin.ModelAdmin):
    list_display = [
        "date",
        "total_facture",
        "total_encaisse",
        "total_non_paye",
        "reliquat_global",
        "nombre_factures",
        "cloturee",
        "cloturee_le",
    ]
    list_filter = ["cloturee", "date"]
    date_hierarchy = "date"
    actions = ["action_cloturer_aujourdhui"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.action(description="(Re)cloturer la caisse du jour")
    def action_cloturer_aujourdhui(self, request, queryset):
        if not request.user.has_perm("caisse.peut_cloturer_caisse"):
            self.message_user(request, "Droit insuffisant.", messages.ERROR)
            return
        caisse = Caisse.cloturer(user=request.user)
        self.message_user(
            request, f"Caisse du {caisse.date} cloturee.", messages.SUCCESS
        )
