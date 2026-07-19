"""Cloture automatique de la caisse.

A programmer via un cron cPanel, par exemple chaque soir a 23h55 :
    55 23 * * *  /home/USER/virtualenv/.../bin/python /home/USER/app/manage.py cloturer_caisse

Sans argument : cloture la journee courante.
Option --date AAAA-MM-JJ : cloture (ou recalcule) une journee precise.
Option --hier : cloture la veille (utile si le cron tourne apres minuit).
"""

import datetime as dt

from django.core.management.base import BaseCommand
from django.utils import timezone

from caisse.models import Caisse


class Command(BaseCommand):
    help = "Cloture la caisse d'une journee (par defaut aujourd'hui)."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Date a cloturer (AAAA-MM-JJ).")
        parser.add_argument(
            "--hier", action="store_true", help="Cloturer la journee precedente."
        )

    def handle(self, *args, **options):
        if options.get("date"):
            date = dt.date.fromisoformat(options["date"])
        elif options.get("hier"):
            date = timezone.localdate() - dt.timedelta(days=1)
        else:
            date = timezone.localdate()

        caisse = Caisse.cloturer(date=date)
        self.stdout.write(
            self.style.SUCCESS(
                f"Caisse du {caisse.date} : encaisse={caisse.total_encaisse}, "
                f"facture={caisse.total_facture}, non paye={caisse.total_non_paye}, "
                f"reliquat global={caisse.reliquat_global}."
            )
        )
