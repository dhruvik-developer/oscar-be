"""Warehouse endpoints (``/inventory/warehouses/...``). Logic lives in
``warehouse_services``; these views only parse input and check permissions."""

import csv

from django.db import transaction
from django.http import HttpResponse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import record_audit
from apps.core.exceptions import BusinessRuleViolation, ValidationFailed
from apps.core.permissions import HasModulePermission, require_permission
from apps.masters.models import Item, Location

from . import warehouse_services as wh

WRITE = "manage_warehouses"


def _audit(request, action, location, description):
    record_audit(
        client=request.client_id, actor=request.user, action=action,
        entity_type="Location", entity_id=location.id, entity_label=location.name,
        description=description,
    )


def _item(request, item_id, field="itemId"):
    item = Item.objects.filter(client_id=request.client_id, pk=item_id, deleted_at__isnull=True).first() if item_id else None
    if item is None:
        raise ValidationFailed("Unknown item.", field_errors={field: ["Pick an item."]})
    return item


def _apply(location, data):
    """Copy the warehouse form onto a Location. No zones, racks or bins."""
    address = dict(location.address) if isinstance(location.address, dict) else {}
    for key, src in (("line1", "address"), ("city", "city"), ("state", "state"), ("pincode", "pincode")):
        if src in data:
            address[key] = (data.get(src) or "").strip()
    location.address = address
    for field in ("name", "manager", "contact"):
        if field in data:
            value = (data.get(field) or "").strip()
            setattr(location, field, value or (None if field != "name" else value))
    if "isActive" in data:
        location.is_active = bool(data["isActive"])
    pin = address.get("pincode") or ""
    if pin and not (pin.isdigit() and len(pin) == 6):
        raise ValidationFailed("PIN code must be 6 digits.", field_errors={"pincode": ["6 digits."]})
    if not (location.name or "").strip():
        raise ValidationFailed("Warehouse name is required.", field_errors={"name": ["Required."]})


class WarehouseListView(APIView):
    """GET list (``?includeInactive=1``) · POST create a warehouse."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        include = request.query_params.get("includeInactive") in ("1", "true")
        return Response({"results": [
            wh.serialize_warehouse(w) for w in wh.warehouse_queryset(request.client_id, include_inactive=include)
        ]})

    def post(self, request):
        require_permission(request.user, WRITE)
        data = request.data
        code = (data.get("code") or "").strip().upper()
        if not code:
            # Blank: from the name (Surat Main -> WH-SURAT-MAIN), never clashing.
            from apps.masters.serializers import slug_code, unique_code

            if not (data.get("name") or "").strip():
                raise ValidationFailed("Enter the warehouse name.", field_errors={"name": ["Required."]})
            peers = Location.objects.filter(client_id=request.client_id, deleted_at__isnull=True)
            code = unique_code(peers, "WH-" + slug_code(data.get("name"), "WAREHOUSE"))
        if code.startswith(wh.SYSTEM_PREFIX):
            raise ValidationFailed("Codes starting SYS- are reserved.", field_errors={"code": ["Reserved prefix."]})
        if Location.objects.filter(client_id=request.client_id, code__iexact=code, deleted_at__isnull=True).exists():
            raise ValidationFailed(f"Code {code} is already used.", field_errors={"code": ["Already used."]})
        location = Location(client_id=request.client_id, code=code, type="Warehouse", is_active=True,
                            created_by=request.user)
        _apply(location, data)
        location.save()
        _audit(request, "create", location, "Warehouse created")
        return Response(wh.serialize_warehouse(location), status=status.HTTP_201_CREATED)


class WarehouseDetailView(APIView):
    """GET · PATCH (incl. ``isActive``) one warehouse."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request, pk):
        return Response(wh.serialize_warehouse(wh.get_warehouse(request.client_id, pk)))

    def patch(self, request, pk):
        require_permission(request.user, WRITE)
        location = wh.get_warehouse(request.client_id, pk)
        was_active = location.is_active
        with transaction.atomic():
            _apply(location, request.data)
            if was_active and not location.is_active:
                rows = wh.stock_rows(request.client_id, warehouse_id=location.id)
                if any(float(r["physical"]) > 0 or float(r["inTransit"]) > 0 for r in rows):
                    raise BusinessRuleViolation(
                        f"{location.name} still holds stock; transfer it out before deactivating.",
                        code="WAREHOUSE_HAS_STOCK",
                    )
                from .models import StockTransfer

                if StockTransfer.objects.filter(
                    client_id=request.client_id, deleted_at__isnull=True, status__in=wh.OPEN_STATUSES + ("Draft",),
                ).filter(from_location_id__in=wh.warehouse_location_ids(request.client_id, location.id)).exists() or \
                        StockTransfer.objects.filter(
                            client_id=request.client_id, deleted_at__isnull=True,
                            status__in=wh.OPEN_STATUSES + ("Draft",),
                            to_location_id__in=wh.warehouse_location_ids(request.client_id, location.id)).exists():
                    raise BusinessRuleViolation(
                        f"{location.name} has open transfers; finish or cancel them first.", code="WAREHOUSE_HAS_TRANSFERS",
                    )
            location.save()
            _audit(request, "update", location, "Warehouse updated")
        return Response(wh.serialize_warehouse(location))


class WarehouseDashboardView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        return Response(wh.dashboard(request.client_id))


class WarehouseStockView(APIView):
    """``?warehouseId&itemId&search&groupBy=item|batch&lowStock=1&ownership&export=csv``"""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        p = request.query_params
        rows = wh.stock_rows(
            request.client_id, warehouse_id=p.get("warehouseId") or None, item_id=p.get("itemId") or None,
            search=p.get("search"), group_by="batch" if p.get("groupBy") == "batch" else "item",
            low_stock_only=p.get("lowStock") in ("1", "true"), ownership=p.get("ownership") or None,
        )
        if p.get("export") == "csv":
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = 'attachment; filename="warehouse-stock.csv"'
            cols = ["warehouseCode", "warehouseName", "sku", "itemName", "batchCode", "uom", "physical",
                    "reserved", "qcPending", "customerOwned", "available", "inTransit", "minQty", "lowStock"]
            writer = csv.writer(response)
            writer.writerow(cols)
            for r in rows:
                writer.writerow([r.get(c) if r.get(c) is not None else "" for c in cols])
            return response
        return Response({"count": len(rows), "results": rows})


class WarehouseAvailabilityView(APIView):
    """``?itemId&qty&warehouseId`` -- free stock in each warehouse and the shortage."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        p = request.query_params
        item = _item(request, p.get("itemId"))
        return Response(wh.availability(request.client_id, item.id, p.get("qty"), p.get("warehouseId") or None))


class WarehouseShortageTransferView(APIView):
    """POST ``{itemId, qty, fromWarehouseId, toWarehouseId, salesOrderId?}`` --
    raises a Requested transfer (stock reserved at the source)."""

    permission_classes = [HasModulePermission]
    required_permissions = ["create_transfer"]

    def post(self, request):
        from apps.core.audit import record_audit as audit

        data = request.data
        item = _item(request, data.get("itemId"))
        reference = None
        if data.get("salesOrderId"):
            from apps.sales.models import SalesOrder

            order = SalesOrder.objects.filter(
                client_id=request.client_id, pk=data["salesOrderId"], deleted_at__isnull=True
            ).first()
            if order is None:
                raise ValidationFailed("Unknown sales order.", field_errors={"salesOrderId": ["Not found."]})
            reference = {"type": "SalesOrder", "id": order.id, "number": order.order_number}
        with transaction.atomic():
            transfer = wh.shortage_transfer(
                client=request.user.client, user=request.user, item=item, qty=data.get("qty"),
                from_warehouse_id=data.get("fromWarehouseId"), to_warehouse_id=data.get("toWarehouseId"),
                reference=reference, notes=data.get("notes"),
            )
            audit(client=request.client_id, actor=request.user, action="create", entity_type="StockTransfer",
                  entity_id=transfer.id, entity_label=transfer.transfer_number,
                  description=f"Shortage transfer for {reference['number'] if reference else item.name}")
        from .serializers import StockTransferSerializer

        return Response(StockTransferSerializer(transfer).data, status=status.HTTP_201_CREATED)


class WarehouseStockLevelView(APIView):
    """GET ``?warehouseId`` · POST ``{warehouseId, itemId, minQty}`` (0 clears the alert)."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        return Response({"results": wh.stock_levels(request.client_id, request.query_params.get("warehouseId") or None)})

    def post(self, request):
        require_permission(request.user, WRITE)
        item = _item(request, request.data.get("itemId"))
        level = wh.set_stock_level(request.client_id, request.data.get("warehouseId"), item, request.data.get("minQty") or 0)
        _audit(request, "update", level.location, f"Minimum stock of {item.name} set to {level.min_qty}")
        return Response({"id": str(level.id), "minQty": str(level.min_qty)})


class WarehouseLedgerView(APIView):
    """``?warehouseId&itemId&batch&from&to&limit``"""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request):
        p = request.query_params
        try:
            limit = min(2000, max(1, int(p.get("limit") or 500)))
        except ValueError:
            limit = 500
        return Response(wh.ledger(
            request.client_id, warehouse_id=p.get("warehouseId") or None, item_id=p.get("itemId") or None,
            batch_code=p.get("batch") or None, date_from=p.get("from") or None, date_to=p.get("to") or None,
            limit=limit,
        ))
