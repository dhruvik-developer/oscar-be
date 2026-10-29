"""``/jobwork/service-orders/`` -- dyeing service orders. Logic: ``service_orders``."""

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import record_audit
from apps.core.exceptions import NotFound, ValidationFailed
from apps.core.permissions import HasModulePermission, require_permission

from . import service_orders as svc
from .models import JobWorkOrder, ServiceOrder
from .serializers import JWOInwardSerializer, JWOOutwardSerializer
from .services import save_order_row

WRITE = "manage_jobwork"


def _audit(request, order, action, description):
    record_audit(client=request.client_id, actor=request.user, action=action, entity_type="ServiceOrder",
                 entity_id=order.id, entity_label=order.order_number, description=description)


def _get(request, pk):
    order = ServiceOrder.objects.filter(client_id=request.client_id, pk=pk, deleted_at__isnull=True).select_related(
        "party", "crm_lead", "sales_order", "item", "vendor").first()
    if order is None:
        raise NotFound()
    return order


class ServiceOrderListView(APIView):
    """GET ``?status&orderType&q&page&page_size`` · POST create."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_jobwork"]

    def get(self, request):
        from django.db.models import Q

        p = request.query_params
        qs = ServiceOrder.objects.filter(client_id=request.client_id, deleted_at__isnull=True).select_related(
            "party", "crm_lead", "sales_order", "item", "vendor")
        if p.get("status"):
            qs = qs.filter(status=p["status"])
        if p.get("orderType"):
            qs = qs.filter(order_type=p["orderType"])
        if p.get("partyId"):
            qs = qs.filter(party_id=p["partyId"])
        q = (p.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(order_number__icontains=q) | Q(party__name__icontains=q) | Q(shade__icontains=q)
                           | Q(fabric_description__icontains=q) | Q(batches__batch_code__icontains=q)).distinct()
        try:
            page = max(1, int(p.get("page") or 1))
            size = min(100, max(1, int(p.get("page_size") or 25)))
        except ValueError:
            page, size = 1, 25
        total = qs.count()
        rows = [svc.serialize(o) for o in qs.order_by("-order_date", "-created_at")[(page - 1) * size: page * size]]
        return Response({"count": total, "page": page, "page_size": size, "results": rows})

    def post(self, request):
        require_permission(request.user, WRITE)
        with transaction.atomic():
            order = svc.create_order(client=request.user.client, user=request.user, data=request.data)
            _audit(request, order, "create", "Service order created")
        return Response(svc.serialize(order, detail=True), status=status.HTTP_201_CREATED)


class ServiceOrderDetailView(APIView):
    """GET · PATCH · POST ``<action>``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_jobwork"]

    def get(self, request, pk):
        return Response(svc.serialize(_get(request, pk), detail=True))

    def patch(self, request, pk):
        require_permission(request.user, WRITE)
        order = _get(request, pk)
        with transaction.atomic():
            order = svc.update_order(order, request.data)
            _audit(request, order, "update", "Service order updated")
        return Response(svc.serialize(_get(request, pk), detail=True))

    def post(self, request, pk, action_name):
        handler = getattr(self, "do_" + action_name.replace("-", "_"), None)
        if handler is None:
            raise NotFound()
        require_permission(request.user, "create_invoice" if action_name == "invoice" else WRITE)
        order = _get(request, pk)
        with transaction.atomic():
            message = handler(request, order) or action_name
            _audit(request, order, action_name, message)
        return Response(svc.serialize(_get(request, pk), detail=True))

    # -- actions ---------------------------------------------------------------
    def do_confirm(self, request, order):
        svc.confirm(order)
        return "Confirmed"

    def do_close(self, request, order):
        svc.set_closed(order, "Closed")
        return "Closed"

    def do_cancel(self, request, order):
        svc.set_closed(order, "Cancelled", request.data.get("reason"))
        return "Cancelled"

    def do_link_batch(self, request, order):
        svc.link_batch(order, request.data.get("batchCode"))
        return f"Batch {request.data.get('batchCode')} linked"

    def do_unlink_batch(self, request, order):
        svc.unlink_batch(order, request.data.get("batchCode"))
        return f"Batch {request.data.get('batchCode')} unlinked"

    def do_receive_fabric(self, request, order):
        from apps.masters.models import Location

        location = Location.objects.filter(
            client_id=request.client_id, pk=request.data.get("locationId"), deleted_at__isnull=True,
        ).first() if request.data.get("locationId") else None
        if location is None:
            raise ValidationFailed("Pick the warehouse receiving the fabric.", field_errors={"locationId": ["Required."]})
        batch, receipt = svc.receive_fabric(
            order, user=request.user, location=location, rolls=request.data.get("rolls"),
            total_meters=request.data.get("totalMeters"), reference=request.data.get("reference"),
            received_on=request.data.get("receivedOn") or None,
        )
        return f"Customer fabric received ({receipt}) as batch {batch.batch_code}"

    def do_create_jwo(self, request, order):
        jwo = svc.create_jwo(
            order, user=request.user, vendor_id=request.data.get("vendorId"), rate=request.data.get("rate"),
            qty=request.data.get("qty"), expected_completion=request.data.get("expectedCompletion") or None,
            remarks=request.data.get("remarks"),
        )
        return f"Job work order {jwo.jwo_no} opened"

    def _jwo(self, request, order):
        jwo = JobWorkOrder.objects.filter(
            client_id=request.client_id, pk=request.data.get("jwoId"), service_order=order, deleted_at__isnull=True,
        ).first()
        if jwo is None:
            raise ValidationFailed("Pick a job work order of this service order.", field_errors={"jwoId": ["Required."]})
        return jwo

    def do_send(self, request, order):
        """Outward to the vendor: ``{jwoId, takas: [{lotTakaId, meters}], challanNo?, date?}``."""
        jwo = self._jwo(request, order)
        takas = [t for t in request.data.get("takas") or [] if t.get("lotTakaId")]
        if not takas:
            raise ValidationFailed("Pick the rolls to send.", field_errors={"takas": ["Required."]})
        svc.assert_rolls_belong(order, [t["lotTakaId"] for t in takas])
        total = sum(float(t.get("meters") or 0) for t in takas)
        serializer = JWOOutwardSerializer(data={
            "date": request.data.get("date") or timezone.localdate().isoformat(), "qty": total,
            "challanNo": request.data.get("challanNo") or None, "vehicleNo": request.data.get("vehicleNo") or None,
            "status": "Sent", "remarks": order.order_number,
            "takas": [{"lotTakaId": t["lotTakaId"], "meters": t.get("meters")} for t in takas],
        }, context={"request": request})
        serializer.is_valid(raise_exception=True)
        row = save_order_row(request, jwo, "outwards", serializer)
        svc.refresh_status(order)
        return f"Sent {total:g} to {jwo.vendor_name} on {row.no}"

    def do_receive_return(self, request, order):
        """Inward from the vendor: ``{jwoId, rows: [{sourceTakaId, meters, weightKg?}], challanNo?}``.
        The dyed rolls go to QC Hold and appear in Dyeing QC."""
        jwo = self._jwo(request, order)
        rows = [r for r in request.data.get("rows") or [] if float(r.get("meters") or 0) > 0]
        if not rows or any(not r.get("sourceTakaId") for r in rows):
            raise ValidationFailed("Each returned roll needs the roll it was sent as.", field_errors={"rows": ["Required."]})
        svc.assert_rolls_belong(order, [r["sourceTakaId"] for r in rows])
        total = sum(float(r["meters"]) for r in rows)
        serializer = JWOInwardSerializer(data={
            "date": request.data.get("date") or timezone.localdate().isoformat(), "qty": total,
            "challanNo": request.data.get("challanNo") or None, "status": "Received",
            "takas": [{"sourceTakaId": r["sourceTakaId"], "meters": r["meters"], "weight_kg": r.get("weightKg") or None,
                       "shade": order.shade} for r in rows],
        }, context={"request": request})
        serializer.is_valid(raise_exception=True)
        row = save_order_row(request, jwo, "inwards", serializer)
        svc.refresh_status(order)
        return f"Received {total:g} back on {row.no}; waiting for Dyeing QC"

    def do_deliver(self, request, order):
        numbers = svc.deliver(order, rolls=request.data.get("rolls"), user=request.user)
        return f"Delivered to customer ({', '.join(numbers)})"

    def do_invoice(self, request, order):
        invoice = svc.create_invoice(order, request=request)
        return f"Draft invoice {invoice.id} created"
