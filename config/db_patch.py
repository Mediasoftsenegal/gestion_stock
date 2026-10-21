"""
Correctif PyMySQL + Django :
Convertit les valeurs DATETIME renvoyées en str par PyMySQL avant que
Django n'appelle timezone.make_aware(), qui exige un vrai datetime.
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