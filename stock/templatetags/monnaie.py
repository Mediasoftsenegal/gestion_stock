from decimal import Decimal, InvalidOperation
from django import template

register = template.Library()

@register.filter
def money(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return "0,00 FCFA"

    s = f"{amount:,.2f}"
    integer, decimal = s.split(".")
    integer = integer.replace(",", " ")
    return f"{integer},{decimal} FCFA"

@register.filter
def format_money_with_currency(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    return "{:,.0f}".format(amount).replace(",", " ")