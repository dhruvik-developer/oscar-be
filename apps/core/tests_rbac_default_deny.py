"""Default-deny RBAC tests and URL coverage verification."""
import uuid
from decimal import Decimal
from django.test import TestCase
from django.urls import get_resolver
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.models import File
from apps.core.permissions import AllowPublic
from apps.masters.models import Item, ItemCategory, Location, Party, Unit
from apps.sales.models import SalesOrder


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class RBACDefaultDenyTests(TestCase):
    def setUp(self):
        self.tenant = Client.objects.create(slug=uid("rbac"), name="RBAC Test Tenant")
        sync_permissions()
        seed_roles(self.tenant)

    def user_with(self, *permission_ids, role_code=None):
        if role_code:
            role = Role.objects.get(client=self.tenant, code=role_code)
        else:
            role = Role.objects.create(client=self.tenant, code=uid("ROLE"), name=uid("Role"))
            for permission_id in permission_ids:
                RolePermission.objects.create(role=role, permission_id=permission_id)
        return User.objects.create_user(
            email=f"{uid('u')}@example.com", password="pass", client=self.tenant, role=role
        )

    def api_for(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return api

    def test_all_registered_drf_views_have_explicit_permissions_or_allow_any(self):
        """Iterate over every registered DRF view in the URL patterns and assert that
        every view either declares permissions, has AllowPublic, or explicitly sets
        allow_any_authenticated = True."""
        resolver = get_resolver()

        def extract_views(urlpatterns, prefix=""):
            views = []
            for pattern in urlpatterns:
                if hasattr(pattern, "url_patterns"):
                    views.extend(extract_views(pattern.url_patterns, prefix + str(pattern.pattern)))
                elif hasattr(pattern, "callback"):
                    views.append((prefix + str(pattern.pattern), pattern.callback))
            return views

        all_views = extract_views(resolver.url_patterns)
        unprotected = []

        for url, callback in all_views:
            view_class = getattr(callback, "cls", None)
            if not view_class:
                continue

            # Skip swagger/redoc views (configured via SPECTACULAR_SETTINGS SERVE_PERMISSIONS)
            # and default DRF APIRootView
            if "spectacular" in view_class.__module__ or view_class.__name__ == "APIRootView":
                continue

            perm_classes = getattr(view_class, "permission_classes", [])
            req_perms = getattr(view_class, "required_permissions", None)
            perm_map = getattr(view_class, "permission_map", None)
            allow_any = getattr(view_class, "allow_any_authenticated", False)
            is_public = any(
                issubclass(p, AllowPublic) for p in perm_classes if isinstance(p, type)
            )

            if not is_public and not allow_any and not req_perms and not perm_map:
                unprotected.append(f"{view_class.__module__}.{view_class.__name__} (url: {url})")

        self.assertEqual(
            unprotected,
            [],
            f"The following {len(unprotected)} DRF view classes fail closed without declarations: {unprotected}",
        )

    def test_hr_user_forbidden_on_other_modules(self):
        """An HR-only user gets 403 on masters, sales, purchase, and inventory."""
        hr_user = self.user_with("view_staff")
        api = self.api_for(hr_user)

        # Masters / Parties
        res = api.get("/api/v1/parties/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res.json()["code"], "view_masters")

        # Inventory Items & Masters
        res = api.get("/api/v1/inventory/items/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        res = api.get("/api/v1/inventory/categories/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        res = api.get("/api/v1/inventory/units/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        res = api.get("/api/v1/inventory/fabrics/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        # Sales & Purchase
        res = api.get("/api/v1/sales/orders/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        res = api.get("/api/v1/purchase/orders/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_file_viewset_isolation_and_urls(self):
        """A normal user cannot list every file in the tenant: only files they uploaded.
        Admins can list all files."""
        user_a = self.user_with("view_staff")
        user_b = self.user_with("view_staff")
        admin = self.user_with("manage_company_profile")

        file_a = File.objects.create(
            client_id=self.tenant.id,
            uploaded_by=user_a,
            file_name="doc_a.pdf",
            storage_key="test/doc_a.pdf",
            file_size=100,
            content_type="application/pdf",
            scope="general",
            status="committed",
        )
        file_b = File.objects.create(
            client_id=self.tenant.id,
            uploaded_by=user_b,
            file_name="doc_b.pdf",
            storage_key="test/doc_b.pdf",
            file_size=200,
            content_type="application/pdf",
            scope="general",
            status="committed",
        )

        api_a = self.api_for(user_a)
        res_a = api_a.get("/api/v1/files/")
        self.assertEqual(res_a.status_code, status.HTTP_200_OK)
        items_a = res_a.json().get("results") or res_a.json().get("payload", [])
        ids_a = [item["id"] for item in items_a]
        self.assertIn(str(file_a.id), ids_a)
        self.assertNotIn(str(file_b.id), ids_a)

        # User A cannot retrieve details of User B's file
        res_detail = api_a.get(f"/api/v1/files/{file_b.id}/")
        self.assertEqual(res_detail.status_code, status.HTTP_404_NOT_FOUND)

        # Admin can view all files
        api_admin = self.api_for(admin)
        res_admin = api_admin.get("/api/v1/files/")
        self.assertEqual(res_admin.status_code, status.HTTP_200_OK)
        items_admin = res_admin.json().get("results") or res_admin.json().get("payload", [])
        ids_admin = [item["id"] for item in items_admin]
        self.assertIn(str(file_a.id), ids_admin)
        self.assertIn(str(file_b.id), ids_admin)

    def test_global_search_filters_by_permission(self):
        """Global search only returns result types the caller has permission to view."""
        # Create records
        party = Party.objects.create(
            client=self.tenant, name="Alpha Customer", code=uid("C"), type="Customer"
        )
        unit = Unit.objects.create(
            client=self.tenant, code="MTR", label="Meter", uom_type="Length", decimal_places=2
        )
        item = Item.objects.create(
            client=self.tenant, name="Alpha Cotton", sku=uid("SKU"), uom=unit
        )
        order = SalesOrder.objects.create(
            client=self.tenant,
            order_number=uid("SO"),
            party=party,
            stage="Draft",
            doc_date=timezone.localdate(),
            subtotal=Decimal("100.00"),
            total=Decimal("100.00"),
        )

        # 1. User with only view_sales should see sales orders and parties, but not items
        sales_user = self.user_with("view_sales")
        api_sales = self.api_for(sales_user)
        res_sales = api_sales.get("/api/v1/search/?q=Alpha")
        self.assertEqual(res_sales.status_code, status.HTTP_200_OK)
        results_sales = res_sales.json().get("results") or res_sales.json().get("payload", [])
        types_sales = {r["type"] for r in results_sales}
        self.assertIn("customer", types_sales)
        self.assertNotIn("item", types_sales)

        # 2. User with only view_inventory should see items, but not parties or sales orders
        inv_user = self.user_with("view_inventory")
        api_inv = self.api_for(inv_user)
        res_inv = api_inv.get("/api/v1/search/?q=Alpha")
        self.assertEqual(res_inv.status_code, status.HTTP_200_OK)
        results_inv = res_inv.json().get("results") or res_inv.json().get("payload", [])
        types_inv = {r["type"] for r in results_inv}
        self.assertIn("item", types_inv)
        self.assertNotIn("customer", types_inv)
        self.assertNotIn("salesOrder", types_inv)

        # 3. HR user (view_staff) gets empty results for "Alpha"
        hr_user = self.user_with("view_staff")
        api_hr = self.api_for(hr_user)
        res_hr = api_hr.get("/api/v1/search/?q=Alpha")
        self.assertEqual(res_hr.status_code, status.HTTP_200_OK)
        results_hr = res_hr.json().get("results") or res_hr.json().get("payload", [])
        self.assertEqual(len(results_hr), 0)
