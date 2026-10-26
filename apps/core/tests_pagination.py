"""EnvelopePagination: `page_size`, and `limit` as its alias, both capped at 200."""
from django.test import SimpleTestCase
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.core.pagination import EnvelopePagination

ROWS = list(range(450))


def page_of(query):
    request = Request(APIRequestFactory().get("/api/v1/things/", query))
    paginator = EnvelopePagination()
    rows = paginator.paginate_queryset(ROWS, request)
    return rows, paginator.get_paginated_response(rows).data


class EnvelopePaginationTests(SimpleTestCase):
    def test_default_page_is_25(self):
        rows, body = page_of({})
        self.assertEqual(len(rows), 25)
        self.assertEqual(body["count"], 450)

    def test_limit_is_honoured_like_page_size(self):
        rows, body = page_of({"limit": "200"})
        self.assertEqual(len(rows), 200)
        self.assertEqual(body["page_size"], 200)
        self.assertIsNotNone(body["next"])

    def test_limit_is_capped(self):
        rows, _ = page_of({"limit": "5000"})
        self.assertEqual(len(rows), 200)

    def test_page_size_wins_over_limit(self):
        rows, _ = page_of({"limit": "10", "page_size": "50"})
        self.assertEqual(len(rows), 50)

    def test_bad_limit_falls_back_to_default(self):
        for raw in ("abc", "0", "-3"):
            rows, _ = page_of({"limit": raw})
            self.assertEqual(len(rows), 25, raw)

    def test_limit_works_with_page(self):
        rows, body = page_of({"limit": "200", "page": "3"})
        self.assertEqual(rows, ROWS[400:450])
        self.assertIsNone(body["next"])
