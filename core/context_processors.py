def droits_globaux(request):
    """Expose `est_admin` aux templates : vrai pour un superuser Django ou un
    membre du groupe « Administrateur » (utilise pour restreindre l'acces a
    des modules sensibles, ex. les archives historiques)."""
    user = getattr(request, "user", None)
    est_admin = bool(
        user and user.is_authenticated
        and (user.is_superuser or user.groups.filter(name="Administrateur").exists())
    )
    return {"est_admin": est_admin}
