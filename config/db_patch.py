"""
Correctifs pour mysql-connector-python + Django 4.2.

1. Convertit les valeurs DATETIME renvoyees en str avant que Django
   n'appelle timezone.make_aware(), qui exige un vrai datetime.
2. `DatabaseWrapper.display_name` est declare `@cached_property` au-dessus
   de `@staticmethod` dans mysql.connector.django.base (mysql-connector-
   python 26.7.0) : cette combinaison est cassee et fait planter
   `manage.py migrate`/`check` avec `TypeError: display_name() takes 0
   positional arguments but 1 was given` des que le sql_mode de la
   connexion n'inclut pas STRICT_TRANS_TABLES/STRICT_ALL_TABLES (le cas
   sur MariaDB, dont le sql_mode par defaut differe de MySQL 8).
"""
import datetime

from django.db.backends.mysql import operations as mysql_operations


_orig_convert_datetimefield_value = (
    mysql_operations.DatabaseOperations.convert_datetimefield_value
)


def _patched_convert_datetimefield_value(self, value, expression, connection):
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                value = datetime.datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    return _orig_convert_datetimefield_value(self, value, expression, connection)


mysql_operations.DatabaseOperations.convert_datetimefield_value = (
    _patched_convert_datetimefield_value
)

try:
    import mysql.connector.django.base as _mysql_connector_django_base
except ImportError:
    pass
else:
    _mysql_connector_django_base.DatabaseWrapper.display_name = "MySQL"