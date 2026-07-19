"""Fonctions utilitaires transverses."""

import datetime as dt
import functools
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone


def reessayer_si_conflit_bd(tentatives=3):
    """Decorateur de vue : rejoue l'operation entiere (nouvelle transaction)
    si la base leve un deadlock transitoire (`OperationalError`), plutot que
    de renvoyer une erreur 500 pour un conflit sans consequence.

    A utiliser sur les vues qui creent des enregistrements references
    (commande, facture, approvisionnement, inventaire...), sous la
    decoration `login_required`/`permission_required`/`require_POST` pour
    ne rejouer que la logique metier, pas les verifications d'acces.
    """

    def decorateur(vue):
        @functools.wraps(vue)
        def wrapper(*args, **kwargs):
            for essai in range(1, tentatives + 1):
                try:
                    return vue(*args, **kwargs)
                except OperationalError:
                    if essai == tentatives:
                        raise

        return wrapper

    return decorateur


def generer_reference(prefixe, model, champ="reference"):
    """Genere une reference du type PREFIXE-ANNEE-0001, garantie sans doublon
    meme en cas de creations concurrentes (plusieurs utilisateurs sur le
    reseau qui valident au meme moment) : incremente un compteur verrouille
    (`SELECT ... FOR UPDATE`) plutot que de relire/recalculer la derniere
    reference existante, ce qui serait sujet a une condition de course.

    Sous tres forte concurrence (creations simultanees d'un tout nouveau
    compteur, ex. premiere commande de l'annee), MySQL peut lever un
    deadlock (`OperationalError`) : c'est une erreur propre et sans danger
    a rejouer (rien n'a ete valide) ; voir `core.utils.reessayer_si_conflit_bd`
    pour l'encadrer au niveau vue/formulaire.
    """
    from core.models import SequenceCompteur

    annee = timezone.now().year
    cle = f"{prefixe}-{annee}"
    base = f"{cle}-"

    with transaction.atomic():
        try:
            compteur = SequenceCompteur.objects.select_for_update().get(cle=cle)
        except SequenceCompteur.DoesNotExist:
            # Premiere utilisation de ce compteur : on part du plus grand
            # numero deja utilise (donnees existantes) pour ne pas regenerer
            # une reference deja prise.
            numero_initial = 0
            dernier = (
                model.objects.filter(**{f"{champ}__startswith": base})
                .order_by(f"-{champ}")
                .values_list(champ, flat=True)
                .first()
            )
            if dernier:
                try:
                    numero_initial = int(dernier.rsplit("-", 1)[1])
                except (ValueError, IndexError):
                    numero_initial = model.objects.filter(
                        **{f"{champ}__startswith": base}
                    ).count()
            try:
                with transaction.atomic():
                    compteur = SequenceCompteur.objects.create(
                        cle=cle, dernier_numero=numero_initial
                    )
            except IntegrityError:
                # Une transaction concurrente vient de le creer : on la
                # relit, verrouillee, plutot que d'echouer.
                compteur = SequenceCompteur.objects.select_for_update().get(cle=cle)

        compteur.dernier_numero += 1
        compteur.save(update_fields=["dernier_numero"])
        numero = compteur.dernier_numero

    return f"{base}{numero:04d}"


def generer_code(prefixe, model, champ="code", chiffres=5):
    """Genere un code sequentiel permanent (sans annee), du type PREFIXE00001,
    garanti sans doublon meme en cas de creations concurrentes (meme
    verrouillage que `generer_reference`). A la difference de celle-ci, la
    numerotation n'est jamais remise a zero : il s'agit d'un catalogue
    permanent (articles...), pas de documents annuels.
    """
    from core.models import SequenceCompteur

    cle = f"CODE-{prefixe}"

    with transaction.atomic():
        try:
            compteur = SequenceCompteur.objects.select_for_update().get(cle=cle)
        except SequenceCompteur.DoesNotExist:
            numero_initial = 0
            dernier = (
                model.objects.filter(**{f"{champ}__startswith": prefixe})
                .order_by(f"-{champ}")
                .values_list(champ, flat=True)
                .first()
            )
            if dernier:
                try:
                    numero_initial = int(dernier[len(prefixe):])
                except ValueError:
                    numero_initial = model.objects.filter(
                        **{f"{champ}__startswith": prefixe}
                    ).count()
            try:
                with transaction.atomic():
                    compteur = SequenceCompteur.objects.create(
                        cle=cle, dernier_numero=numero_initial
                    )
            except IntegrityError:
                compteur = SequenceCompteur.objects.select_for_update().get(cle=cle)

        compteur.dernier_numero += 1
        compteur.save(update_fields=["dernier_numero"])
        numero = compteur.dernier_numero

    return f"{prefixe}{numero:0{chiffres}d}"


PERIODES = [
    ("JOUR", "Jour"),
    ("SEMAINE", "Semaine"),
    ("QUINZAINE", "Quinzaine"),
    ("MOIS", "Mois"),
    ("TRIMESTRE", "Trimestre"),
    ("SEMESTRE", "Semestre"),
    ("ANNEE", "Annee"),
]


def bornes_periode(periode, reference=None):
    """Retourne (debut, fin) inclusifs pour une periode donnee.

    `reference` est une date situee dans la periode (defaut : aujourd'hui).
    """
    ref = reference or timezone.localdate()

    if periode == "JOUR":
        return ref, ref
    if periode == "SEMAINE":
        debut = ref - dt.timedelta(days=ref.weekday())
        return debut, debut + dt.timedelta(days=6)
    if periode == "QUINZAINE":
        debut = ref.replace(day=1) if ref.day <= 15 else ref.replace(day=16)
        if ref.day <= 15:
            return debut, ref.replace(day=15)
        fin = _dernier_jour_mois(ref)
        return debut, fin
    if periode == "MOIS":
        return ref.replace(day=1), _dernier_jour_mois(ref)
    if periode == "TRIMESTRE":
        premier_mois = 3 * ((ref.month - 1) // 3) + 1
        debut = ref.replace(month=premier_mois, day=1)
        fin_mois = premier_mois + 2
        return debut, _dernier_jour_mois(ref.replace(month=fin_mois, day=1))
    if periode == "SEMESTRE":
        premier_mois = 1 if ref.month <= 6 else 7
        debut = ref.replace(month=premier_mois, day=1)
        fin = ref.replace(month=premier_mois + 5, day=1)
        return debut, _dernier_jour_mois(fin)
    if periode == "ANNEE":
        return ref.replace(month=1, day=1), ref.replace(month=12, day=31)

    raise ValueError(f"Periode inconnue : {periode}")


def _dernier_jour_mois(date):
    if date.month == 12:
        suivant = date.replace(year=date.year + 1, month=1, day=1)
    else:
        suivant = date.replace(month=date.month + 1, day=1)
    return suivant - dt.timedelta(days=1)


# --------------------------------------------------------------------------- #
#  Conversion d'un montant en toutes lettres (francais)
# --------------------------------------------------------------------------- #
_UNITES = [
    "zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf",
    "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize",
    "dix-sept", "dix-huit", "dix-neuf",
]
_DIZAINES = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante", 8: "quatre-vingt"}


def _deux_chiffres_en_lettres(n):
    """n entre 0 et 99."""
    if n < 20:
        return _UNITES[n]
    if n < 70:
        d, u = divmod(n, 10)
        mot = _DIZAINES[d]
        if u == 0:
            return mot
        if u == 1:
            return f"{mot} et un"
        return f"{mot}-{_UNITES[u]}"
    if n < 80:
        reste = n - 60
        if reste == 11:
            return "soixante et onze"
        return f"soixante-{_UNITES[reste]}"
    reste = n - 80
    if reste == 0:
        return "quatre-vingts"
    return f"quatre-vingt-{_UNITES[reste]}"


def _trois_chiffres_en_lettres(n):
    """n entre 0 et 999."""
    centaines, reste = divmod(n, 100)
    parts = []
    if centaines > 0:
        if centaines == 1:
            parts.append("cent")
        else:
            parts.append(f"{_UNITES[centaines]} cent" + ("s" if reste == 0 else ""))
    if reste > 0:
        parts.append(_deux_chiffres_en_lettres(reste))
    return " ".join(parts) if parts else "zéro"


def nombre_en_lettres(n):
    """Convertit un entier (positif, negatif ou nul) en toutes lettres (francais)."""
    n = int(n)
    if n == 0:
        return "zéro"
    if n < 0:
        return "moins " + nombre_en_lettres(-n)

    tranches = []
    reste = n
    while reste > 0:
        tranches.append(reste % 1000)
        reste //= 1000

    mots = []
    for i in range(len(tranches) - 1, -1, -1):
        valeur = tranches[i]
        if valeur == 0:
            continue
        if i == 1:
            mots.append("mille" if valeur == 1 else f"{_trois_chiffres_en_lettres(valeur)} mille")
        elif i >= 2:
            echelle = "million" if i == 2 else "milliard"
            mots.append(f"{_trois_chiffres_en_lettres(valeur)} {echelle}{'s' if valeur > 1 else ''}")
        else:
            mots.append(_trois_chiffres_en_lettres(valeur))
    return " ".join(mots)


def montant_en_lettres(montant, devise_singulier="franc CFA", devise_pluriel="francs CFA"):
    """Convertit un montant en toutes lettres, ex : 'Cent vingt-cinq mille francs CFA'."""
    entier = int(Decimal(montant).to_integral_value(rounding=ROUND_HALF_UP))
    devise = devise_singulier if abs(entier) <= 1 else devise_pluriel
    mots = nombre_en_lettres(entier)
    return f"{mots[0].upper()}{mots[1:]} {devise}"
