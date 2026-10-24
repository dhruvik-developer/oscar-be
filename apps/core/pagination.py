"""
The list response envelope (api.md §1.4).

    {
      "count": 412, "page": 1, "page_size": 25,
      "next": "/api/v1/sales/invoices/?page=2", "previous": null,
      "results": [],
      "aggregates": { "totalValue": 1284000, "outstanding": 322500 }
    }

``aggregates`` is required on every endpoint whose screen renders KPI tiles.
Without it the UI would have to fetch every row to compute a tile -- which is
exactly what the mock frontend does today and what server pagination breaks
(api-integration.md §10.3).
"""
from collections import OrderedDict

from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class EnvelopePagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 200  # api.md §1.3 -- capped by contract, enforced here too
    page_query_param = "page"

    def paginate_queryset(self, queryset, request, view=None):
        self._view = view
        self._unpaginated = queryset
        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response(self, data):
        aggregates = {}
        view = getattr(self, "_view", None)
        if view is not None and hasattr(view, "get_aggregates"):
            aggregates = view.get_aggregates(getattr(self, "_unpaginated", None)) or {}

        return Response(
            OrderedDict(
                [
                    ("count", self.page.paginator.count),
                    ("page", self.page.number),
                    ("page_size", self.get_page_size(self.request)),
                    ("next", self._relative(self.get_next_link())),
                    ("previous", self._relative(self.get_previous_link())),
                    ("results", data),
                    ("aggregates", aggregates),
                ]
            )
        )

    @staticmethod
    def _relative(url):
        """api.md §1.4 shows ``next`` as a path, not an absolute URL."""
        if not url:
            return None
        marker = "/api/v1"
        index = url.find(marker)
        return url[index:] if index != -1 else url


def envelope(results, *, count=None, page=1, page_size=None, aggregates=None):
    """Build the same envelope by hand.

    For endpoints that are lists but not querysets -- computed rollups,
    handoff blockers, dashboard rows -- so the client sees one shape
    everywhere.
    """
    results = list(results)
    total = count if count is not None else len(results)
    return {
        "count": total,
        "page": page,
        "page_size": page_size if page_size is not None else len(results),
        "next": None,
        "previous": None,
        "results": results,
        "aggregates": aggregates or {},
    }
