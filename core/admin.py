from django.contrib import admin
from django.shortcuts import redirect

from .models import Parametrage, SequenceCompteur

# Le modele TimeStampedModel est abstrait : rien a enregistrer ici.


@admin.register(SequenceCompteur)
class SequenceCompteurAdmin(admin.ModelAdmin):
    """Lecture seule : modifier `dernier_numero` a la main peut reintroduire
    des doublons de reference. Reserve au diagnostic."""

    list_display = ["cle", "dernier_numero"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Parametrage)
class ParametrageAdmin(admin.ModelAdmin):
    list_display = ["nom_entreprise", "telephone", "email"]

    def has_add_permission(self, request):
        return not Parametrage.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = Parametrage.get_solo()
        return redirect("admin:core_parametrage_change", obj.pk)
