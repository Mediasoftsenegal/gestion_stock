from decimal import Decimal, InvalidOperation
from django import template

register = template.Library()

@register.filter
def format_money(value):
    if value in (None, ""):
        return "0,00"

    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value

    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", " ")