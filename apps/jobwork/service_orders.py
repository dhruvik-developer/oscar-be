"""Dyeing service orders (``ServiceOrder``).

A dyeing order does not depend on OSCAR's own grey production:

* ``CUSTOMER_FABRIC`` -- the customer's fabric is received as a CUSTOMER-owned
  batch (no value, no journal), dyed through a job work order, inspected in
  Dyeing QC and delivered back with ``deliver_to_owner``. OSCAR invoices only
  the service charge; the vendor's dyeing charge is booked to COGS by the job
  work accrual because every roll sent is customer-owned.
* ``OSCAR_GREY`` -- an OSCAR grey batch is dyed and sold. The dyeing charge
  accrues into the roll's cost (Dr Stock), so the sale's COGS carries grey +
  dyeing cost; the invoice shows fabric and dyeing charge as separate lines.

Outward, inward, Dyeing QC and re-dyeing are ordinary job work rows on the
order's JWOs, so stock movements, batch inheritance (``parent_taka``) and
accruals are exactly the job work module's.
"""

from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.exceptions import BusinessRuleViolation, Conflict, ValidationFailed
from apps.core.money import D, ZERO, round2, round4
from apps.core.numbering import allocate_number

from .models import JobWorkOrder, JWOMaterial, ServiceOrder

EDITABLE_STATUSES = ("Draft", "Confirmed", "Fabric Received", "At Vendor", "Dyeing QC", "Ready", "Delivered")
MANUAL_STATUSES = ("Closed", "Cancelled")
MONEY_FIELDS = ("service_rate", "service_value", "fabric_rate", "freight_charge", "other_charges",
                "final_selling_price", "vendor_rate", "quantity")


def _party(client_id, party_id, field, kinds=None):
    from apps.masters.models import Party

    if not party_id:
        return None
    party = Party.objects.filter(client_id=client_id, pk=party_id, deleted_at__isnull=True).first()
    if party is None:
        raise ValidationFailed("Unknown party.", field_errors={field: ["Not found."]})
    return party


def apply_fields(order, data):
    """Copy the order form onto ``order``. Rates and prices are always the
    user's own figures; ``serviceValue`` defaults to quantity x rate."""
    from apps.crm.models import Lead
    from apps.masters.models import Item
    from apps.sales.models import SalesOrder

    client_id = order.client_id
    mapping = {
        "serviceType": "service_type", "fabricDescription": "fabric_description",
        "fabricQuality": "fabric_quality", "shade": "shade", "colourReference": "colour_reference",
        "uom": "uom", "otherChargesNote": "other_charges_note", "notes": "notes",
    }
    for key, field in mapping.items():
        if key in data:
            setattr(order, field, (data.get(key) or "").strip() or None if field != "service_type" else (data.get(key) or "Dyeing"))
    if "orderType" in data:
        if data["orderType"] not in dict(ServiceOrder.TYPES):
            raise ValidationFailed("Unknown order type.", field_errors={"orderType": ["Pick a type."]})
        if order.pk and order.order_type != data["orderType"] and order.batches.exists():
            raise BusinessRuleViolation("Unlink the batches before changing the order type.", code="HAS_BATCHES")
        order.order_type = data["orderType"]
    for key, field, cast in (
        ("orderDate", "order_date", None), ("expectedFabricDate", "expected_fabric_date", None),
        ("expectedVendorReturnDate", "expected_vendor_return_date", None),
        ("expectedDeliveryDate", "expected_delivery_date", None),
    ):
        if key in data:
            setattr(order, field, data.get(key) or None)
    camel = {"service_rate": "serviceRate", "service_value": "serviceValue", "fabric_rate": "fabricRate",
             "freight_charge": "freightCharge", "other_charges": "otherCharges",
             "final_selling_price": "finalSellingPrice", "vendor_rate": "vendorRate", "quantity": "quantity"}
    for field, key in camel.items():
        if key in data:
            raw = data.get(key)
            if raw in (None, ""):
                setattr(order, field, None if field == "final_selling_price" else ZERO)
                continue
            try:
                value = D(raw)
            except Exception:  # noqa: BLE001
                raise ValidationFailed(f"{key} must be a number.", field_errors={key: ["Number."]})
            if value < 0:
                raise ValidationFailed(f"{key} cannot be negative.", field_errors={key: ["0 or more."]})
            setattr(order, field, value)
    if "serviceValue" not in data or data.get("serviceValue") in (None, ""):
        if "serviceRate" in data or "quantity" in data:
            order.service_value = round2(D(order.quantity) * D(order.service_rate))
    if "partyId" in data:
        order.party = _party(client_id, data.get("partyId"), "partyId")
    if "vendorId" in data:
        order.vendor = _party(client_id, data.get("vendorId"), "vendorId")
    if "itemId" in data:
        order.item = Item.objects.filter(client_id=client_id, pk=data["itemId"]).first() if data.get("itemId") else None
    if "crmLeadId" in data:
        lead = None
        if data.get("crmLeadId"):
            lead = Lead.objects.filter(client_id=client_id, pk=data["crmLeadId"], deleted_at__isnull=True).first()
            if lead is None:
                raise ValidationFailed("Unknown lead.", field_errors={"crmLeadId": ["Not found."]})
            if order.party_id is None and getattr(lead, "party_id", None):
                order.party_id = lead.party_id
        order.crm_lead = lead
    if "salesOrderId" in data:
        so = None
        if data.get("salesOrderId"):
            so = SalesOrder.objects.filter(client_id=client_id, pk=data["salesOrderId"], deleted_at__isnull=True).first()
            if so is None:
                raise ValidationFailed("Unknown sales order.", field_errors={"salesOrderId": ["Not found."]})
        order.sales_order = so
    if order.party_id is None and order.crm_lead_id is None:
        raise ValidationFailed("Pick the customer or the lead.", field_errors={"partyId": ["Required."]})


@transaction.atomic
def create_order(*, client, user, data):
    order = ServiceOrder(client=client, order_date=data.get("orderDate") or timezone.localdate(), created_by=user)
    apply_fields(order, data)
    order.order_number = allocate_number(client, "DSO", order.order_date)
    order.save()
    return order


@transaction.atomic
def update_order(order, data):
    order = ServiceOrder.objects.select_for_update().get(pk=order.pk)
    if order.status not in EDITABLE_STATUSES:
        raise Conflict(f"A {order.status} service order cannot be edited.", code="BAD_TARGET")
    if order.sales_invoice_id and any(k in data for k in ("serviceRate", "serviceValue", "fabricRate", "quantity")):
        from apps.sales.models import SalesInvoice

        if SalesInvoice.objects.filter(pk=order.sales_invoice_id).exclude(status__in=["Draft", "Cancelled"]).exists():
            raise Conflict("The invoice is finalized; raise a credit / debit note instead of changing the price.",
                           code="INVOICED")
    apply_fields(order, data)
    order.save()
    return refresh_status(order)


@transaction.atomic
def confirm(order):
    order = ServiceOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != "Draft":
        raise Conflict("Only a draft can be confirmed.", code="BAD_TARGET")
    if order.party_id is None:
        raise ValidationFailed("Pick the customer (party) before confirming; a lead alone cannot be billed.",
                               field_errors={"partyId": ["Required."]})
    if D(order.quantity) <= 0:
        raise ValidationFailed("Enter the quantity.", field_errors={"quantity": ["Required."]})
    order.status = "Confirmed"
    order.save(update_fields=["status", "updated_at"])
    return refresh_status(order)


@transaction.atomic
def set_closed(order, status, reason=None):
    order = ServiceOrder.objects.select_for_update().get(pk=order.pk)
    if status == "Cancelled":
        from .models import JWOOutward

        if JWOOutward.objects.filter(
            job_work_order__service_order=order, job_work_order__deleted_at__isnull=True, deleted_at__isnull=True,
        ).exists():
            raise BusinessRuleViolation(
                "Fabric was already sent to the vendor; close the order instead of cancelling it.",
                code="HAS_MOVEMENTS",
            )
        order.cancelled_reason = reason
    order.status = status
    order.save(update_fields=["status", "cancelled_reason", "updated_at"])
    return order


# ---------------------------------------------------------------------------
# Fabric
# ---------------------------------------------------------------------------
def _check_batch(order, batch):
    if order.order_type == ServiceOrder.TYPE_CUSTOMER_FABRIC:
        if batch.ownership == "OSCAR":
            raise BusinessRuleViolation(
                f"{batch.batch_code} is OSCAR stock. A dyeing service order takes the customer's own fabric; "
                "use 'OSCAR grey + dyeing' to dye and sell OSCAR fabric.",
                code="NOT_CUSTOMER_MATERIAL",
            )
        if order.party_id and batch.owner_party_id and batch.owner_party_id != order.party_id:
            raise BusinessRuleViolation(
                f"{batch.batch_code} belongs to {batch.owner_party.name}, not {order.party.name}.",
                code="OTHER_OWNER",
            )
    elif batch.ownership != "OSCAR":
        raise BusinessRuleViolation(
            f"{batch.batch_code} is customer material; it cannot be sold as OSCAR grey fabric.",
            code="NOT_OSCAR_STOCK",
        )


def _require_active(order):
    if order.status in ("Draft",):
        raise Conflict("Confirm the service order first.", code="BAD_TARGET")
    if order.status in MANUAL_STATUSES:
        raise Conflict(f"The service order is {order.status}.", code="BAD_TARGET")


@transaction.atomic
def link_batch(order, code):
    from apps.inventory.batch_services import find_batch

    _require_active(order)
    batch = find_batch(order.client_id, code)
    if batch is None:
        raise ValidationFailed("Unknown batch.", field_errors={"batchCode": ["Not found."]})
    _check_batch(order, batch)
    if order.item_id is None and batch.item_id:
        order.item_id = batch.item_id
        order.save(update_fields=["item", "updated_at"])
    order.batches.add(batch)
    return refresh_status(order)


@transaction.atomic
def unlink_batch(order, code):
    from apps.inventory.batch_services import find_batch
    from .models import JWOOutwardTaka

    batch = find_batch(order.client_id, code)
    if batch is None:
        raise ValidationFailed("Unknown batch.", field_errors={"batchCode": ["Not found."]})
    if JWOOutwardTaka.objects.filter(
        outward__job_work_order__service_order=order, lot_taka__batch=batch, deleted_at__isnull=True,
    ).exists():
        raise BusinessRuleViolation("Rolls of this batch were already sent to the vendor.", code="HAS_MOVEMENTS")
    order.batches.remove(batch)
    return refresh_status(order)


@transaction.atomic
def receive_fabric(order, *, user, location, rolls=None, total_meters=None, reference=None, received_on=None):
    """Receive the customer's fabric for a CUSTOMER_FABRIC order into a new
    CUSTOMER-owned batch and link it."""
    from apps.inventory.batch_services import receive_customer_material

    _require_active(order)
    if order.order_type != ServiceOrder.TYPE_CUSTOMER_FABRIC:
        raise BusinessRuleViolation("Only a customer-fabric order receives the customer's fabric.", code="BAD_TARGET")
    if order.item_id is None:
        raise ValidationFailed("Pick the fabric item on the order first.", field_errors={"itemId": ["Required."]})
    batch, receipt_no = receive_customer_material(
        client=order.client, customer=order.party, item=order.item, location=location, rolls=rolls,
        reference=reference or order.order_number, user=user, received_on=received_on, total_meters=total_meters,
    )
    order.batches.add(batch)
    refresh_status(order)
    return batch, receipt_no


def order_takas(order):
    """Free rolls of the order's batches that can still go to the vendor."""
    from apps.inventory.batch_services import BULK_TAG
    from apps.inventory.models import LotTaka

    rows = []
    for lot in LotTaka.objects.filter(
        client_id=order.client_id, batch__in=order.batches.all(), deleted_at__isnull=True,
        available_quantity__gt=0,
    ).select_related("location", "batch").order_by("batch__batch_code", "received_date", "taka_number"):
        rows.append({
            "lotTakaId": str(lot.id), "takaId": lot.taka_id, "batchCode": lot.batch.batch_code,
            "lotNumber": lot.lot_number, "qty": str(round4(lot.available_quantity)), "uom": lot.uom,
            "location": lot.location.name, "locationCode": lot.location.code,
            "status": lot.status, "qcStatus": lot.qc_status, "bulk": BULK_TAG in (lot.tags or []),
            "ownership": lot.batch.ownership,
        })
    return rows


# ---------------------------------------------------------------------------
# Job work (dyeing vendor)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_jwo(order, *, user, vendor_id=None, rate=None, qty=None, expected_completion=None, remarks=None):
    """Open a dyeing job work order for this service order at the vendor's
    rate entered here (or on the order)."""
    _require_active(order)
    vendor = _party(order.client_id, vendor_id, "vendorId") if vendor_id else order.vendor
    if vendor is None:
        raise ValidationFailed("Pick the dyeing vendor.", field_errors={"vendorId": ["Required."]})
    rate = D(rate) if rate not in (None, "") else D(order.vendor_rate)
    if rate < 0:
        raise ValidationFailed("Rate cannot be negative.", field_errors={"rate": ["0 or more."]})
    qty = D(qty) if qty not in (None, "") else D(order.quantity)
    if qty <= 0:
        raise ValidationFailed("Enter the quantity to dye.", field_errors={"qty": ["Required."]})
    jwo = JobWorkOrder.objects.create(
        client_id=order.client_id, jwo_no=allocate_number(order.client, "JWO", timezone.localdate()),
        project_destination="Service Order", service_order=order, process=order.service_type or "Dyeing",
        vendor=vendor, vendor_name=vendor.name, order_date=timezone.localdate(), planned_qty=qty,
        rate=rate, total_amount=round2(qty * rate), expected_completion=expected_completion or order.expected_vendor_return_date,
        status="Draft", remarks=remarks or f"{order.order_number} · {order.party.name if order.party_id else ''}",
        created_by=user,
    )
    JWOMaterial.objects.create(
        client_id=order.client_id, job_work_order=jwo,
        fabric_item=(order.item.name if order.item_id else order.fabric_description) or "",
        fabric_quality=order.fabric_quality or "", shade=order.shade, qty=qty, rate=rate,
        amount=round2(qty * rate),
    )
    if order.vendor_id is None:
        order.vendor = vendor
        order.save(update_fields=["vendor", "updated_at"])
    return jwo


def assert_rolls_belong(order, lot_ids):
    """Outward from a service order's JWO may only send that order's rolls."""
    from apps.inventory.models import LotTaka

    batch_ids = set(order.batches.values_list("id", flat=True))
    for lot in LotTaka.objects.filter(client_id=order.client_id, pk__in=lot_ids).select_related("batch"):
        if lot.batch_id not in batch_ids:
            raise BusinessRuleViolation(
                f"Roll {lot.taka_id} is not from a batch linked to {order.order_number}.", code="NOT_ORDER_BATCH",
            )
        if "BULK" in (lot.tags or []):
            raise BusinessRuleViolation(
                f"{lot.taka_id} is unregistered bulk metres; register takas on the batch first.", code="REGISTER_TAKAS_FIRST",
            )


# ---------------------------------------------------------------------------
# Status, quantities, costing
# ---------------------------------------------------------------------------
def figures(order):
    from apps.inventory.models import LotTaka, StockMovement

    from .models import JWOInwardTaka, JWOOutwardTaka

    jwos = list(order.job_work_orders.filter(deleted_at__isnull=True))
    sent = D(JWOOutwardTaka.objects.filter(
        outward__job_work_order__in=jwos, outward__deleted_at__isnull=True, deleted_at__isnull=True,
        outward__status__in=["Sent", "Confirmed"],
    ).aggregate(q=Sum("meters"))["q"] or 0)
    returned = D(JWOInwardTaka.objects.filter(
        inward__job_work_order__in=jwos, inward__deleted_at__isnull=True, deleted_at__isnull=True,
    ).aggregate(q=Sum("meters"))["q"] or 0)
    dyed = LotTaka.objects.filter(
        client_id=order.client_id, source_document_type="JobWorkInward",
        source_document_id__in=[j.id for j in jwos], deleted_at__isnull=True,
    )
    qc_pending = D(dyed.filter(qc_status__in=["Pending", "On Hold"], available_quantity__gt=0)
                   .aggregate(q=Sum("available_quantity"))["q"] or 0)
    ready = D(dyed.filter(qc_status__in=["Passed", "Pass"], available_quantity__gt=0)
              .exclude(location__code__startswith="SYS-").aggregate(q=Sum("available_quantity"))["q"] or 0)
    rework = D(dyed.filter(qc_status="Reprocess", available_quantity__gt=0)
               .aggregate(q=Sum("available_quantity"))["q"] or 0)
    rejected = D(dyed.filter(qc_status__in=["Rejected", "Reject"], available_quantity__gt=0)
                 .aggregate(q=Sum("available_quantity"))["q"] or 0)
    batches = list(order.batches.all())
    received = D(LotTaka.objects.filter(
        client_id=order.client_id, batch__in=batches, parent_taka__isnull=True, deleted_at__isnull=True,
    ).aggregate(q=Sum("received_quantity"))["q"] or 0)
    delivered = abs(D(StockMovement.objects.filter(
        client_id=order.client_id, type="CUSTOMER_OUT", lot_taka__batch__in=batches,
    ).aggregate(q=Sum("quantity"))["q"] or 0))
    sold = abs(D(StockMovement.objects.filter(
        client_id=order.client_id, type="SALE", lot_taka__in=dyed,
    ).aggregate(q=Sum("quantity"))["q"] or 0))
    return {
        "received": received, "sentToVendor": sent, "returnedFromVendor": returned,
        "atVendor": max(sent - returned, ZERO), "dyeingQcPending": qc_pending, "ready": ready,
        "rework": rework, "rejected": rejected, "delivered": delivered, "sold": sold,
    }


def refresh_status(order):
    if order.status in MANUAL_STATUSES or order.status == "Draft":
        return order
    from apps.sales.models import SalesInvoice

    f = figures(order)
    status = "Confirmed"
    if order.batches.exists():
        status = "Fabric Received"
    if f["sentToVendor"] > 0:
        status = "At Vendor"
    if f["returnedFromVendor"] > 0 and f["dyeingQcPending"] > 0:
        status = "Dyeing QC"
    elif f["returnedFromVendor"] > 0 and f["ready"] > 0:
        status = "Ready"
    if f["delivered"] > 0 or f["sold"] > 0:
        status = "Delivered"
    if order.sales_invoice_id and SalesInvoice.objects.filter(
        pk=order.sales_invoice_id).exclude(status__in=["Draft", "Cancelled"]).exists():
        status = "Invoiced"
    if status != order.status:
        order.status = status
        order.save(update_fields=["status", "updated_at"])
    return order


def costing(order):
    """Planned and actual money of the order, each figure from its source.

    Planned: the order's own entered rates. Actual cost: the vendor's job work
    accrual and charges on this order's JWOs, and (OSCAR_GREY) the grey cost of
    the rolls sent, at their landed rate. Actual revenue: the linked invoice
    once finalized (taxable value, excluding GST).
    """
    from apps.sales.models import SalesInvoice

    from .costing import ACCRUAL_ACCOUNT  # noqa: F401  (documented source)
    from .models import JWOCharge, JWOInwardTaka, JWOOutwardTaka

    qty = D(order.quantity)
    planned_service = D(order.service_value) or round2(qty * D(order.service_rate))
    planned_fabric = round2(qty * D(order.fabric_rate)) if order.order_type == ServiceOrder.TYPE_OSCAR_GREY else ZERO
    planned_total = round2(planned_service + planned_fabric + D(order.freight_charge) + D(order.other_charges))
    jwos = list(order.job_work_orders.filter(deleted_at__isnull=True))
    returned = D(JWOInwardTaka.objects.filter(
        inward__job_work_order__in=jwos, inward__deleted_at__isnull=True, deleted_at__isnull=True,
    ).aggregate(q=Sum("meters"))["q"] or 0)
    dyeing_cost = round2(sum(
        (D(JWOInwardTaka.objects.filter(inward__job_work_order=j, inward__deleted_at__isnull=True,
                                        deleted_at__isnull=True).aggregate(q=Sum("meters"))["q"] or 0) * D(j.rate)
         for j in jwos), ZERO))
    charges = round2(D(JWOCharge.objects.filter(
        job_work_order__in=jwos, deleted_at__isnull=True,
    ).exclude(status="Cancelled").aggregate(a=Sum("amount"))["a"] or 0))
    grey_cost = ZERO
    if order.order_type == ServiceOrder.TYPE_OSCAR_GREY:
        for ot in JWOOutwardTaka.objects.filter(
            outward__job_work_order__in=jwos, outward__deleted_at__isnull=True, deleted_at__isnull=True,
        ).select_related("lot_taka"):
            grey_cost += D(ot.meters) * D(ot.lot_taka.rate or 0)
        grey_cost = round2(grey_cost)
    invoice = None
    revenue = None
    if order.sales_invoice_id:
        invoice = SalesInvoice.objects.filter(pk=order.sales_invoice_id).first()
        if invoice and invoice.status not in ("Draft", "Cancelled"):
            revenue = round2(D(invoice.taxable_value))
    actual_cost = round2(dyeing_cost + charges + grey_cost)
    return {
        "planned": {
            "serviceValue": str(planned_service), "fabricValue": str(planned_fabric),
            "freight": str(round2(order.freight_charge)), "otherCharges": str(round2(order.other_charges)),
            "total": str(planned_total),
            "finalSellingPrice": str(round2(order.final_selling_price)) if order.final_selling_price is not None else None,
            "vendorCost": str(round2(qty * D(order.vendor_rate))),
        },
        "actual": {
            "greyFabricCost": str(grey_cost), "dyeingCharges": str(dyeing_cost), "otherJobWorkCharges": str(charges),
            "totalCost": str(actual_cost), "returnedQty": str(round4(returned)),
            "revenue": str(revenue) if revenue is not None else None,
            "margin": str(round2(revenue - actual_cost)) if revenue is not None else None,
            "invoiceStatus": invoice.status if invoice else None,
            "invoiceNumber": invoice.invoice_number if invoice else None,
        },
        "notes": [
            "Customer fabric carries no value: only the service charge is OSCAR revenue."
            if order.order_type == ServiceOrder.TYPE_CUSTOMER_FABRIC else
            "Grey cost is the landed rate of the rolls sent for dyeing; dyeing charges accrue into the dyed rolls' cost.",
        ],
    }


# ---------------------------------------------------------------------------
# Delivery and invoice
# ---------------------------------------------------------------------------
@transaction.atomic
def deliver(order, *, rolls, user):
    """CUSTOMER_FABRIC: hand dyed (QC-passed) rolls back to the customer."""
    from apps.inventory.batch_services import deliver_to_owner
    from apps.inventory.models import LotTaka

    _require_active(order)
    if order.order_type != ServiceOrder.TYPE_CUSTOMER_FABRIC:
        raise BusinessRuleViolation("OSCAR fabric is delivered through the sales invoice / challan.", code="BAD_TARGET")
    by_batch = defaultdict(list)
    for row in rolls or []:
        lot = LotTaka.objects.filter(
            client_id=order.client_id, pk=row.get("lotTakaId"), batch__in=order.batches.all(), deleted_at__isnull=True,
        ).select_related("batch").first()
        if lot is None:
            raise ValidationFailed("A roll is not from this order's batches.", field_errors={"rolls": ["Unknown roll."]})
        if lot.qc_status not in ("Passed", "Pass"):
            raise BusinessRuleViolation(f"Roll {lot.taka_id} has not passed Dyeing QC ({lot.qc_status}).", code="QC_BLOCKED")
        by_batch[lot.batch].append(row)
    if not by_batch:
        raise ValidationFailed("Pick the rolls to deliver.", field_errors={"rolls": ["Required."]})
    numbers = [deliver_to_owner(batch, rolls=rows, reference=order.order_number, user=user)
               for batch, rows in by_batch.items()]
    refresh_status(order)
    return numbers


def deliverable_rolls(order):
    from apps.inventory.models import LotTaka

    return [
        {"lotTakaId": str(l.id), "takaId": l.taka_id, "batchCode": l.batch.batch_code,
         "qty": str(round4(l.available_quantity)), "uom": l.uom, "location": l.location.name,
         "qcStatus": l.qc_status, "dyed": l.source_document_type == "JobWorkInward"}
        for l in LotTaka.objects.filter(
            client_id=order.client_id, batch__in=order.batches.all(), deleted_at__isnull=True,
            available_quantity__gt=0, qc_status__in=["Passed", "Pass"],
        ).exclude(location__code__startswith="SYS-").exclude(status="Reserved").select_related("batch", "location")
        .order_by("batch__batch_code", "taka_id")
    ]


@transaction.atomic
def create_invoice(order, *, request):
    """A Draft sales invoice for review: the user checks tax and finalizes it
    through the normal invoice screen, which posts the accounting.

    CUSTOMER_FABRIC: service lines only (no item, so no stock and no COGS) --
    the customer's fabric is never billed. OSCAR_GREY: the dyed rolls as a
    fabric line (sold from stock at its cost) plus a separate dyeing line."""
    from apps.sales.models import SalesInvoice
    from apps.sales.serializers import SalesInvoiceSerializer

    order = ServiceOrder.objects.select_for_update().get(pk=order.pk)
    _require_active(order)
    if order.party_id is None:
        raise ValidationFailed("The order has no customer.", field_errors={"partyId": ["Required."]})
    if order.sales_invoice_id and SalesInvoice.objects.filter(
            pk=order.sales_invoice_id, deleted_at__isnull=True).exclude(status="Cancelled").exists():
        raise Conflict("This order already has an invoice.", code="ALREADY_INVOICED")
    f = figures(order)
    label = f"{order.service_type or 'Dyeing'} charges" + (f" — {order.shade}" if order.shade else "")
    lines = []
    if order.order_type == ServiceOrder.TYPE_CUSTOMER_FABRIC:
        qty = f["delivered"] or f["returnedFromVendor"] or D(order.quantity)
        if order.service_value and D(order.quantity) and qty != D(order.quantity) and not D(order.service_rate):
            rate = round4(D(order.service_value) / D(order.quantity))
        else:
            rate = D(order.service_rate) or (round4(D(order.service_value) / qty) if qty else ZERO)
        lines.append({"description": f"{label} ({order.order_number})", "itemName": label, "qty": str(round4(qty)),
                      "uom": order.uom, "rate": str(rate)})
    else:
        rolls = [r for r in deliverable_rolls(order) if r["dyed"]]
        if not rolls:
            raise BusinessRuleViolation("No dyed, QC-passed rolls are in stock to invoice yet.", code="NOTHING_READY")
        if order.item_id is None:
            raise ValidationFailed("Pick the fabric item on the order.", field_errors={"itemId": ["Required."]})
        fabric_qty = sum((D(r["qty"]) for r in rolls), ZERO)
        lines.append({
            "itemId": str(order.item_id), "itemName": order.item.name, "qty": str(round4(fabric_qty)),
            "uom": order.item.uom or order.uom, "rate": str(D(order.fabric_rate)),
            "takaAllocations": [{"lotTakaId": r["lotTakaId"], "qty": r["qty"]} for r in rolls],
        })
        lines.append({"description": f"{label} ({order.order_number})", "itemName": label,
                      "qty": str(round4(fabric_qty)), "uom": order.uom, "rate": str(D(order.service_rate))})
    if D(order.freight_charge) > 0:
        lines.append({"description": "Freight", "itemName": "Freight", "qty": "1", "rate": str(D(order.freight_charge))})
    if D(order.other_charges) > 0:
        lines.append({"description": order.other_charges_note or "Other charges",
                      "itemName": order.other_charges_note or "Other charges", "qty": "1",
                      "rate": str(D(order.other_charges))})
    serializer = SalesInvoiceSerializer(data={
        "partyId": str(order.party_id), "date": timezone.localdate().isoformat(),
        "notes": f"Service order {order.order_number}", "items": lines,
        **({"salesOrderId": str(order.sales_order_id)} if order.sales_order_id else {}),
    }, context={"request": request})
    serializer.is_valid(raise_exception=True)
    invoice = serializer.save(client_id=order.client_id, created_by=request.user, status="Draft")
    order.sales_invoice = invoice
    order.save(update_fields=["sales_invoice", "updated_at"])
    return invoice


def serialize(order, detail=False):
    f = figures(order)
    data = {
        "id": str(order.id), "orderNumber": order.order_number, "orderType": order.order_type,
        "orderTypeLabel": dict(ServiceOrder.TYPES).get(order.order_type), "serviceType": order.service_type,
        "orderDate": order.order_date.isoformat() if order.order_date else None, "status": order.status,
        "partyId": str(order.party_id) if order.party_id else None,
        "partyName": order.party.name if order.party_id else None,
        "crmLeadId": str(order.crm_lead_id) if order.crm_lead_id else None,
        "crmLeadName": (getattr(order.crm_lead, "name", None) or getattr(order.crm_lead, "company", None) or str(order.crm_lead))
        if order.crm_lead_id else None,
        "salesOrderId": str(order.sales_order_id) if order.sales_order_id else None,
        "salesOrderNumber": order.sales_order.order_number if order.sales_order_id else None,
        "itemId": str(order.item_id) if order.item_id else None,
        "itemName": order.item.name if order.item_id else None,
        "fabricDescription": order.fabric_description, "fabricQuality": order.fabric_quality,
        "shade": order.shade, "colourReference": order.colour_reference,
        "quantity": str(round4(order.quantity)), "uom": order.uom,
        "serviceRate": str(order.service_rate), "serviceValue": str(round2(order.service_value)),
        "fabricRate": str(order.fabric_rate), "freightCharge": str(round2(order.freight_charge)),
        "otherCharges": str(round2(order.other_charges)), "otherChargesNote": order.other_charges_note,
        "finalSellingPrice": str(round2(order.final_selling_price)) if order.final_selling_price is not None else None,
        "vendorId": str(order.vendor_id) if order.vendor_id else None,
        "vendorName": order.vendor.name if order.vendor_id else None, "vendorRate": str(order.vendor_rate),
        "expectedFabricDate": order.expected_fabric_date.isoformat() if order.expected_fabric_date else None,
        "expectedVendorReturnDate": order.expected_vendor_return_date.isoformat() if order.expected_vendor_return_date else None,
        "expectedDeliveryDate": order.expected_delivery_date.isoformat() if order.expected_delivery_date else None,
        "notes": order.notes, "batchCodes": [b.batch_code for b in order.batches.all()],
        "salesInvoiceId": str(order.sales_invoice_id) if order.sales_invoice_id else None,
        "quantities": {k: str(round4(v)) for k, v in f.items()},
    }
    if detail:
        from .models import JWOInward, JWOOutward

        data["batches"] = [
            {"batchCode": b.batch_code, "ownership": b.ownership,
             "ownerPartyName": b.owner_party.name if b.owner_party_id else None,
             "itemName": b.item.name if b.item_id else None}
            for b in order.batches.select_related("owner_party", "item")
        ]
        data["jobWorkOrders"] = []
        for j in order.job_work_orders.filter(deleted_at__isnull=True).select_related("vendor").order_by("created_at"):
            data["jobWorkOrders"].append({
                "id": str(j.id), "jwoNo": j.jwo_no, "vendorName": j.vendor_name, "rate": str(j.rate),
                "plannedQty": str(round4(j.planned_qty)), "status": j.status,
                "outwards": [{"id": str(o.id), "no": o.no, "date": o.date.isoformat() if o.date else None,
                              "status": o.status,
                              "meters": str(round4(sum((D(t.meters) for t in o.takas.filter(deleted_at__isnull=True)), ZERO)))}
                             for o in JWOOutward.objects.filter(job_work_order=j, deleted_at__isnull=True).order_by("created_at")],
                "inwards": [{"id": str(i.id), "no": i.no, "date": i.date.isoformat() if i.date else None,
                             "status": i.status, "qty": str(round4(i.qty)),
                             "accepted": str(round4(i.accepted or 0)), "rejected": str(round4(i.rejected or 0))}
                            for i in JWOInward.objects.filter(job_work_order=j, deleted_at__isnull=True).order_by("created_at")],
                "atVendorRolls": at_vendor_rolls(j),
            })
        data["availableRolls"] = order_takas(order)
        data["deliverableRolls"] = deliverable_rolls(order)
        data["costing"] = costing(order)
    return data


def at_vendor_rolls(jwo):
    """The order's rolls sitting at this JWO's vendor (External WIP), for
    recording the return. A partly sent roll is the split-off holder."""
    from apps.inventory import services as stock
    from apps.inventory.models import LotTaka

    order = jwo.service_order
    if order is None:
        return []
    wip = stock.wip_location_for_party(jwo.client_id, jwo.vendor or jwo.vendor_name)
    return [
        {"lotTakaId": str(l.id), "takaId": l.taka_id, "batchCode": l.batch.batch_code,
         "qty": str(round4(l.available_quantity))}
        for l in LotTaka.objects.filter(
            client_id=jwo.client_id, batch__in=order.batches.all(), location=wip, status="External WIP",
            available_quantity__gt=0, deleted_at__isnull=True,
        ).select_related("batch").order_by("taka_id")
    ]
