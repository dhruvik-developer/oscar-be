"""
Base serializers.

Enforces the wire conventions of api.md §1.6 and api-integration.md §10.1-10.2:

  - Money is a number with 2 decimals, never a formatted string.
  - Quantities are numbers and may be fractional (weight items are kg).
  - Dates are ISO: date-only ``YYYY-MM-DD``, instants ``...T09:00:00.000Z``.
  - Ids are opaque strings -- never parsed, sorted or built by the client.
  - Field names are camelCase (see :mod:`apps.core.naming`).
"""
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.db import models as django_models
from django.utils import timezone
from rest_framework import serializers

from .naming import camelize, underscoreize
from .tenancy import get_current_client_id


class ISODateTimeField(serializers.DateTimeField):
    """``2026-09-18T09:00:00.000Z`` -- the format PMS already uses (api.md §1.6).

    DRF's default renders ``+00:00``; the frontend's fixtures and date helpers
    are written against the ``Z`` form with milliseconds, so it is produced
    here rather than left to a client-side normaliser.
    """

    def to_representation(self, value):
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if timezone.is_aware(value):
            value = value.astimezone(dt_timezone.utc)
        return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


class MoneyField(serializers.DecimalField):
    """api.md §1.6 -- a number with 2 decimals, never a formatted string."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 18)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("coerce_to_string", False)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        # api-integration.md §10.1: the client is told never to send a display
        # string, but "" and "-" leak out of the legacy mock forms. Treat both
        # as absent rather than as a validation error the user cannot fix.
        if isinstance(data, str):
            cleaned = data.strip().replace(",", "")
            if cleaned in ("", "-", "—"):
                return None
            data = cleaned
        return super().to_internal_value(data)


class QuantityField(serializers.DecimalField):
    """Fractional quantities -- weight-tracked items are held in kg (db.md §1.5)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 18)
        kwargs.setdefault("decimal_places", 4)
        kwargs.setdefault("coerce_to_string", False)
        super().__init__(**kwargs)


class PercentField(serializers.DecimalField):
    """Store 18 for 18%, not 0.18 (db.md §1.5)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 7)
        kwargs.setdefault("decimal_places", 4)
        kwargs.setdefault("coerce_to_string", False)
        super().__init__(**kwargs)


class CamelCaseMixin:
    """Translates declared field names between snake_case and camelCase.

    Applies only to this serializer's own field names. Values are passed
    through untouched, so ``jsonb`` payloads keep their user-defined keys.
    """

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not isinstance(data, dict):
            return data
        return {camelize(str(key)): value for key, value in data.items()}

    def to_internal_value(self, data):
        if isinstance(data, dict):
            fields = self.fields
            renamed = {}
            for key, value in data.items():
                if key in fields:
                    renamed[key] = value
                    continue
                snake = underscoreize(str(key))
                renamed[snake if snake in fields else key] = value
            data = renamed
        return super().to_internal_value(data)


class BaseSerializer(CamelCaseMixin, serializers.Serializer):
    """Non-model serializer with the wire conventions applied."""


class BaseModelSerializer(CamelCaseMixin, serializers.ModelSerializer):
    """The base for every model serializer in the project."""

    serializer_field_mapping = {
        **serializers.ModelSerializer.serializer_field_mapping,
        django_models.DateTimeField: ISODateTimeField,
    }

    def build_standard_field(self, field_name, model_field):
        field_class, field_kwargs = super().build_standard_field(field_name, model_field)
        if isinstance(model_field, django_models.DecimalField):
            decimals = model_field.decimal_places
            field_kwargs.setdefault("coerce_to_string", False)
            if decimals == 2:
                field_class = MoneyField
                field_kwargs.pop("max_digits", None)
                field_kwargs.pop("decimal_places", None)
                field_kwargs.pop("coerce_to_string", None)
            elif decimals == 4 and model_field.max_digits == 7:
                field_class = PercentField
                field_kwargs.pop("max_digits", None)
                field_kwargs.pop("decimal_places", None)
                field_kwargs.pop("coerce_to_string", None)
            elif decimals == 4:
                field_class = QuantityField
                field_kwargs.pop("max_digits", None)
                field_kwargs.pop("decimal_places", None)
                field_kwargs.pop("coerce_to_string", None)
        return field_class, field_kwargs


class TenantPrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):
    """A relation that can only resolve inside the caller's tenant.

    db.md §1.3 wants cross-tenant references to be impossible rather than
    merely unlikely; this is the serializer half of that (composite FKs are
    the database half). An id from another tenant fails validation as "does
    not exist", which is also what api.md §1.11 asks for -- 404, never 403.

    ``model`` takes an ``"app_label.ModelName"`` string, resolved on first
    access. Importing the model at class-definition time would create import
    cycles across the twelve document modules; the label defers that to the
    first read, by which point the app registry is ready.

    ``queryset`` is a property rather than a plain attribute because schema
    generation reads ``field.queryset.model`` directly, without going through
    :meth:`get_queryset` -- so the label has to be resolved by the time anyone
    touches the attribute, not just by the time a value is validated.
    """

    #: DRF's RelatedField.__init__ reads ``self.queryset`` as the default before
    #: assigning, so the backing attribute must exist on the class.
    _queryset = None

    def __init__(self, model=None, **kwargs):
        self.model_label = model
        if not kwargs.get("read_only") and kwargs.get("queryset") is None and model:
            kwargs["queryset"] = model
        super().__init__(**kwargs)

    @property
    def queryset(self):
        value = self._queryset
        if isinstance(value, str):
            from django.apps import apps as django_apps

            value = django_apps.get_model(value)._default_manager.all()
            self._queryset = value  # resolve once per field instance
        return value

    @queryset.setter
    def queryset(self, value):
        self._queryset = value

    def get_queryset(self):
        queryset = self.queryset
        if hasattr(queryset, "all"):
            queryset = queryset.all()

        request = self.context.get("request")
        client_id = getattr(request, "client_id", None) or get_current_client_id()
        if client_id is not None and hasattr(queryset.model, "client_id"):
            queryset = queryset.filter(client_id=client_id)
        if hasattr(queryset.model, "deleted_at"):
            queryset = queryset.filter(deleted_at__isnull=True)
        return queryset


class DisplayField(serializers.ReadOnlyField):
    """A frozen or joined display value (``partyName``, ``itemName``, ``locationName``).

    db.md §3.2: denormalised copies on documents are not redundancy, they are
    the record -- a posted invoice must render identically in three years.
    """


def decimal_or_zero(value):
    return Decimal("0") if value is None else value


class WriteOnceMixin:
    """Fields that may be set at creation but never updated.

    Used for the frozen document columns (``party_name``, ``place_of_supply``,
    address snapshots) so an update request cannot rewrite history.
    """

    write_once_fields = ()

    def update(self, instance, validated_data):
        for field in self.write_once_fields:
            validated_data.pop(field, None)
        return super().update(instance, validated_data)
