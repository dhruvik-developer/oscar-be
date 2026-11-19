"""Purchase views base constants and helpers."""
from decimal import Decimal
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce

MONEY = DecimalField(max_digits=18, decimal_places=2)


def money_sum(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), Value(Decimal("0.00")), output_field=MONEY)
