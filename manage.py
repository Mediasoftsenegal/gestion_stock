#!/usr/bin/env python
import os
import sys



def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django introuvable. Activez l'environnement virtuel et "
            "installez les dependances (pip install -r requirements.txt)."
        ) from exc

    # ⚠️ Import du patch APRÈS avoir défini DJANGO_SETTINGS_MODULE
    # et APRÈS que Django ait pu être initialisé
    import django
    django.setup()


    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()