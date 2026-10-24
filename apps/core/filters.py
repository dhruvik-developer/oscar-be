"""
The standard list parameters every list endpoint must accept (api.md §1.3).

    page, page_size, search, ordering, status (repeatable), date_from, date_to,
    date_field, ids (repeatable)

``buildQuery()`` in the frontend serialises arrays as repeated keys
(``?status=Draft&status=Sent``), so repeated values OR together.
"""
from django.db.models import Q

from .exceptions import ValidationFailed


class StandardListFilterBackend:
    """Applies the api.md §1.3 parameter set to any queryset.

    A view opts in per parameter by declaring:

        status_field      -- the column ``?status=`` filters (default "status")
        default_date_field -- the column ``?date_from/?date_to`` apply to
        allowed_date_fields -- which columns ``?date_field=`` may name
        filter_map        -- { query param: ORM lookup }
    """

    def filter_queryset(self, request, queryset, view):
        params = request.query_params
        queryset = self._filter_ids(params, queryset)
        queryset = self._filter_status(params, queryset, view)
        queryset = self._filter_dates(params, queryset, view)
        queryset = self._filter_mapped(params, queryset, view)
        return queryset

    # -- ?ids=a&ids=b -- fetch a specific set after a bulk selection ---------
    def _filter_ids(self, params, queryset):
        ids = [value for value in params.getlist("ids") if value]
        if not ids:
            return queryset
        # Comma-separated is also accepted; the frontend emits repeated keys but
        # hand-built links in the app use commas.
        flattened = []
        for value in ids:
            flattened.extend(part.strip() for part in value.split(",") if part.strip())
        return queryset.filter(pk__in=flattened)

    # -- ?status=Draft&status=Sent (multiple values OR together) ------------
    def _filter_status(self, params, queryset, view):
        statuses = [value for value in params.getlist("status") if value]
        if not statuses:
            return queryset
        field = getattr(view, "status_field", "status")
        if field is None:
            return queryset
        flattened = []
        for value in statuses:
            flattened.extend(part.strip() for part in value.split(",") if part.strip())
        return queryset.filter(**{f"{field}__in": flattened})

    # -- ?date_from / ?date_to / ?date_field --------------------------------
    def _filter_dates(self, params, queryset, view):
        date_from = params.get("date_from")
        date_to = params.get("date_to")
        if not date_from and not date_to:
            return queryset

        field = getattr(view, "default_date_field", None)
        requested = params.get("date_field")
        if requested:
            allowed = getattr(view, "allowed_date_fields", None) or ()
            if requested not in allowed:
                raise ValidationFailed(
                    "That date field cannot be filtered on.",
                    field_errors={"date_field": [f"Unknown date field '{requested}'."]},
                )
            field = requested
        if not field:
            return queryset

        if date_from:
            queryset = queryset.filter(**{f"{field}__gte": date_from})
        if date_to:
            queryset = queryset.filter(**{f"{field}__lte": date_to})
        return queryset

    # -- per-view simple equality filters -----------------------------------
    def _filter_mapped(self, params, queryset, view):
        filter_map = getattr(view, "filter_map", None) or {}
        for param, lookup in filter_map.items():
            values = [value for value in params.getlist(param) if value not in (None, "")]
            if not values:
                continue
            flattened = []
            for value in values:
                flattened.extend(part.strip() for part in str(value).split(",") if part.strip())
            if len(flattened) == 1:
                value = flattened[0]
                if value.lower() in ("true", "false") and not lookup.endswith("__in"):
                    queryset = queryset.filter(**{lookup: value.lower() == "true"})
                else:
                    queryset = queryset.filter(**{lookup: value})
            else:
                queryset = queryset.filter(**{f"{lookup}__in": flattened})
        return queryset

    def get_schema_operation_parameters(self, view):  # pragma: no cover - OpenAPI only
        return [
            {
                "name": name,
                "required": False,
                "in": "query",
                "description": description,
                "schema": {"type": "string"},
            }
            for name, description in (
                ("search", "Free text over name / number / party / email."),
                ("ordering", "`field` or `-field`, comma-separated for multiple."),
                ("status", "Repeatable; multiple values OR together."),
                ("date_from", "ISO date, applied to the entity's primary date field."),
                ("date_to", "ISO date, applied to the entity's primary date field."),
                ("date_field", "Which date column the range applies to."),
                ("ids", "Repeatable; fetch a specific set after a bulk selection."),
            )
        ]


def search_q(term, fields):
    """Build an OR'd ``icontains`` filter over a set of fields."""
    if not term:
        return Q()
    query = Q()
    for field in fields:
        query |= Q(**{f"{field}__icontains": term})
    return query


def parse_bool(value, default=False):
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")
