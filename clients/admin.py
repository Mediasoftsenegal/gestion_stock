import csv
import io

from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path

from .models import CategorieClient, Client, ListeTarif


class ClientImportForm(forms.Form):
    fichier = forms.FileField(label="Fichier CSV/TXT")
    separateur = forms.ChoiceField(
        choices=[(",", ",",""), (";", ";","")],
        initial="",
        label="Séparateur",
    )


@admin.register(CategorieClient)
class CategorieClientAdmin(admin.ModelAdmin):
    list_display = ["nom_categorie", "description"]
    search_fields = ["nom_categorie"]


@admin.register(ListeTarif)
class ListeTarifAdmin(admin.ModelAdmin):
    list_display = ["nom_display", "active", "nb_clients"]
    list_filter = ["active"]
    search_fields = ["id"]

    @admin.display(description="Nom")
    def nom_display(self, obj):
        for attr in ("nomclient", "nom", "nom_client", "name", "designation"):
            value = getattr(obj, attr, None)
            if value:
                return value
        return str(obj)

    @admin.display(description="clients")
    def nb_clients(self, obj):
        related = getattr(obj, "clients", None)
        if related is None:
            return 0
        return related.count()


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    change_list_template = "admin/clients/client/change_list.html"
    list_display = ["code_display", "nomclient", "categorie_display", "liste_tarif", "telephone", "depot_vente", "actif"]
    list_filter = ["actif", "depot_vente", "liste_tarif"]
    search_fields = ["nomclient", "telephone", "email"]
    autocomplete_fields = ["liste_tarif"]

    def get_urls(self):
        urls = super().get_urls()
        info = (self.model._meta.app_label, self.model._meta.model_name)
        return [
            path("import/", self.admin_site.admin_view(self.import_view), name=f"{info[0]}_{info[1]}_import")
        ] + urls

    def import_view(self, request):
        if request.method == "POST":
            form = ClientImportForm(request.POST, request.FILES)
            if form.is_valid():
                sep = form.cleaned_data["separateur"]
                content = request.FILES["fichier"].read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(content), delimiter=sep)

                created = 0
                updated = 0

                for row in reader:
                    code = (row.get("codeclient") or row.get("code") or "").strip()
                    if not code:
                        continue

                    defaults = {}

                    nom = (row.get("nomclient") or row.get("nom") or "").strip()
                    if nom:
                        defaults["nomclient"] = nom

                    telephone = (row.get("telephone") or "").strip()
                    if telephone:
                        defaults["telephone"] = telephone

                    email = (row.get("email") or "").strip()
                    if email:
                        defaults["email"] = email

                    adresse = (row.get("adresse") or "").strip()
                    if adresse:
                        defaults["adresse"] = adresse

                    categorie_name = (row.get("categorie") or "").strip()
                    if categorie_name:
                        categorie_obj, _ = CategorieClient.objects.get_or_create(nom_categorie=categorie_name)
                        defaults["categorie"] = categorie_obj

                    actif = (row.get("actif") or "").strip().lower()
                    if actif:
                        defaults["actif"] = actif in {"1", "true", "oui", "yes", "o", "y"}

                    obj, created_obj = Client.objects.update_or_create(codeclient=code, defaults=defaults)
                    if created_obj:
                        created += 1
                    else:
                        updated += 1

                messages.success(request, f"Import terminé : {created} créés, {updated} mis à jour.")
                return HttpResponseRedirect("../")

        else:
            form = ClientImportForm()

        context = self.admin_site.each_context(request)
        context.update({
            "title": "Importer des clients",
            "form": form,
            "opts": self.model._meta,
        })
        return render(request, "admin/clients/client/import.html", context)

    @admin.display(description="Code")
    def code_display(self, obj):
        for attr in ("code", "code_client", "codeclient"):
            value = getattr(obj, attr, None)
            if value:
                return value
        return ""

    @admin.display(description="Catégorie")
    def categorie_display(self, obj):
        categorie = getattr(obj, "categorie", None)
        if categorie is None:
            return ""
        value = getattr(categorie, "nom_categorie", None)
        if value:
            return value
        return str(categorie)
