# stock/monnaie.py
from django import template

register = template.Library()

@register.filter
def format_money(value):
    """
    Formate un nombre avec un espace comme séparateur de milliers
    Exemple: 1500000.50 -> 1 500 000,50
    """
    if value is None:
        return "0,00"
    try:
        # Convertir en float si c'est une chaîne
        value = float(value)
        # Formater avec 2 décimales
        formatted = f"{value:,.2f}"
        # Remplacer la virgule par un espace pour les milliers
        # et le point par une virgule pour les décimales
        return formatted.replace(",", " ").replace(".", ",")
    except (ValueError, TypeError):
        return value

@register.filter
def format_money_with_currency(value):
    """
    Formate avec l'espace comme séparateur  
    Exemple: 1500000.50 -> 1 500 000,50 
    """
    if value is None:
        return "0,00 "
    try:
        value = float(value)
        formatted = f"{value:,.2f}".replace(",", " ").replace(".", ",")
        return f"{formatted} "
    except (ValueError, TypeError):
        return value

@register.filter
def format_money_without_decimal(value):
    """
    Formate sans décimales avec un espace comme séparateur
    Exemple: 1500000.50 -> 1 500 000
    """
    if value is None:
        return "0"
    try:
        value = float(value)
        formatted = f"{value:,.0f}".replace(",", " ")
        return formatted
    except (ValueError, TypeError):
        return value