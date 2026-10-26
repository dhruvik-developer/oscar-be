"""Quotation maths: item discounts, a global discount on top, and the GST
that follows the discounted value (the composer screen does the same)."""
from decimal import Decimal

from django.test import SimpleTestCase

from apps.core.money import compute_document_totals, compute_line

D = Decimal


def totals(override=None, *lines):
    return compute_document_totals(list(lines), discount_total_override=override, freight_charges=0, other_charges=0)


class DiscountTotalsTests(SimpleTestCase):
    def setUp(self):
        # 100 m × ₹100, 10% item discount, 5% GST
        self.line = compute_line(100, 100, 10, 5)

    def test_item_discount_only(self):
        t = totals(None, self.line)
        self.assertEqual((t.total_discount, t.taxable_value, t.total_tax, t.total), (D("1000.00"), D("9000.00"), D("450.00"), D("9450.00")))

    def test_global_discount_on_top_reduces_gst_too(self):
        # 5% global on the ₹9,000 net = ₹450 → total discount ₹1,450.
        t = totals(D("1450"), self.line)
        self.assertEqual(t.taxable_value, D("8550.00"))
        self.assertEqual(t.total_tax, D("427.50"))  # 5% of 8,550
        self.assertEqual(t.total, D("8977.50"))

    def test_override_equal_to_item_discounts_changes_nothing(self):
        self.assertEqual(totals(D("1000"), self.line).total, totals(None, self.line).total)

    def test_mixed_rates_scale_proportionally(self):
        a = compute_line(10, 100, 0, 5)    # 1000 → tax 50
        b = compute_line(10, 100, 0, 12)   # 1000 → tax 120
        t = totals(D("200"), a, b)          # 10% off the whole document
        self.assertEqual(t.taxable_value, D("1800.00"))
        self.assertEqual(t.total_tax, D("153.00"))  # 170 × 0.9
