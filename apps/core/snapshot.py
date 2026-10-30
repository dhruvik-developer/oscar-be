"""
Tenant snapshot export / import (api.md §3.4).

Replaces ``exportDatabaseSnapshot()`` / ``importDatabaseSnapshot()``, which
today serialise the whole of ``localStorage``.

The export is a plain JSON document of the tenant's own rows, in dependency
order (db.md §14.1 step 4) so an import can replay it. Import is deliberately
**additive and non-destructive**: it will not overwrite an existing row,
because a restore that silently clobbers live financial data is worse than one
that refuses.
"""
from django.apps import apps
from django.contrib.auth.hashers import make_password
from django.db import transaction

from .audit import _jsonable, record_audit

#: Dependency order. A restore replays this list top to bottom; an export
#: writes it in the same order so the file is readable as a build sequence.
SNAPSHOT_MODELS = [
    "accounts.Role",
    "accounts.User",
    "accounting.Account",
    "accounting.BankAccount",
    "accounting.ExpenseCategory",
    "masters.Unit",
    "masters.Location",
    "masters.Fabric",
    "masters.ItemCategory",
    "masters.CategoryCustomField",
    "masters.Item",
    "masters.CategoryPart",
    "masters.ItemPart",
    "masters.Party",
    "masters.PartyContact",
    "masters.ItemSerial",
    "sales.Quotation",
    "sales.QuotationLine",
    "sales.SalesOrder",
    "sales.SalesOrderLine",
    "sales.ProformaInvoice",
    "sales.ProformaInvoiceLine",
    "sales.DeliveryChallan",
    "sales.DeliveryChallanLine",
    "sales.SalesInvoice",
    "sales.SalesInvoiceLine",
    "sales.PaymentIn",
    "sales.PaymentAllocation",
    "sales.SalesReturn",
    "sales.SalesReturnLine",
    "sales.WarrantyCard",
    "sales.WarrantyCardItem",
    "purchase.PurchaseOrder",
    "purchase.PurchaseOrderLine",
    "purchase.PurchaseBill",
    "purchase.PurchaseBillLine",
    "purchase.VendorBill",
    "purchase.VendorBillLine",
    "purchase.GoodsReceipt",
    "purchase.GoodsReceiptLine",
    "purchase.PaymentOut",
    "purchase.PurchaseReturn",
    "purchase.PurchaseReturnLine",
    "purchase.Expense",
    "inventory.StockMovement",
    "inventory.StockBalance",
    "inventory.StockTransfer",
    "inventory.StockTransferLine",
    "inventory.FaultyPart",
    "inventory.ServiceUsage",
    "inventory.ZoneRequest",
    "inventory.ZoneRequestLine",
    "inventory.StockAudit",
    "inventory.StockAuditLine",
    "inventory.QualityStandard",
    "accounting.JournalEntry",
    "accounting.JournalLine",
    "accounting.Budget",
    "accounting.BankTransfer",
    "crm.Stage",
    "crm.DealStage",
    "crm.Source",
    "crm.Industry",
    "crm.LostReason",
    "crm.Lead",
    "crm.MasterTask",
    "crm.StageTask",
    "crm.Task",
    "crm.Deal",
    "crm.Contract",
    "crm.CrmProject",
    "crm.Form",
    "pms.Department",
    "pms.StageConfig",
    "pms.Project",
    "pms.ProjectStage",
    "pms.Task",
    "pms.Document",
    "pms.Approval",
    "pms.Delay",
    "pms.Settings",
    "hrms.Department",
    "hrms.Designation",
    "hrms.Location",
    "hrms.SalaryStructure",
    "hrms.Employee",
    "hrms.Attendance",
    "hrms.LeaveType",
    "hrms.LeaveRequest",
    "hrms.LeaveBalance",
    "hrms.PayrollRun",
    "hrms.Payslip",
    "hrms.Holiday",
    "hrms.WorkingDay",
]

#: Never exported. Credentials, tokens and replay caches are not backup data.
EXCLUDED_FIELDS = {"password", "token_hash", "refresh_token_hash", "request_hash"}

#: Never imported either: a restore must not be a way to grant privilege.
#: Restored users come back without a usable password and reset it by email.
PRIVILEGED_FIELDS = {"is_superuser", "is_staff"}


def export_snapshot(client_id):
    """A JSON document of this tenant's rows, newest schema first."""
    from django.utils import timezone

    tables = {}
    for label in SNAPSHOT_MODELS:
        model = apps.get_model(label)
        if not hasattr(model, "client_id"):
            continue
        rows = []
        for instance in model.objects.filter(client_id=client_id).iterator(chunk_size=500):
            row = {}
            for field in instance._meta.concrete_fields:
                if field.name in EXCLUDED_FIELDS:
                    continue
                row[field.attname] = _jsonable(getattr(instance, field.attname, None))
            rows.append(row)
        if rows:
            tables[label] = rows

    return {
        "version": 1,
        "exportedAt": timezone.now().isoformat(),
        "clientId": str(client_id),
        "tableOrder": [label for label in SNAPSHOT_MODELS if label in tables],
        "tables": tables,
    }


@transaction.atomic
def import_snapshot(client_id, snapshot, *, user=None):
    """Replay a snapshot into this tenant.

    Additive only: existing primary keys are skipped rather than overwritten,
    and every row is re-stamped with the target tenant so a snapshot cannot be
    used to write into someone else's data. The file is untrusted input, so:

    * only the tables in ``SNAPSHOT_MODELS`` are restored, in that order,
      whatever ``tableOrder`` says;
    * credential and privilege columns are dropped, and users get an
      unusable password;
    * a foreign key must point at a row of this tenant, existing or created by
      this same snapshot. Rows that point anywhere else are rejected.
    """
    tables = snapshot.get("tables") or {}
    labels = [label for label in SNAPSHOT_MODELS if tables.get(label)]
    models = {label: apps.get_model(label) for label in labels}
    labels = [label for label in labels if hasattr(models[label], "client_id")]

    # Primary keys anywhere in the database (any tenant) are never re-created,
    # so they cannot count as "created by this snapshot" either.
    taken = {
        label: {str(pk) for pk in models[label]._base_manager.values_list("pk", flat=True)}
        for label in labels
    }
    incoming = {
        label: {str(row.get("id")) for row in tables[label] if isinstance(row, dict) and row.get("id")}
        - taken[label]
        for label in labels
    }
    allowed_cache = {}

    def allowed_ids(target):
        """PKs of `target` a restored row may reference."""
        key = target._meta.label
        if key not in allowed_cache:
            ids = {str(pk) for pk in target._base_manager.filter(client_id=client_id).values_list("pk", flat=True)}
            allowed_cache[key] = ids | incoming.get(key, set())
        return allowed_cache[key]

    created = {}
    skipped = {}
    rejected = {}
    for label in labels:
        model = models[label]
        fields = [f for f in model._meta.concrete_fields if f.name not in EXCLUDED_FIELDS | PRIVILEGED_FIELDS]
        field_names = {f.attname for f in fields}
        # Tenant-scoped targets only; "client" itself is overwritten below.
        tenant_fks = [
            f for f in fields
            if f.is_relation and f.attname != "client_id" and hasattr(f.related_model, "client_id")
        ]

        to_create = []
        for row in tables[label]:
            if not isinstance(row, dict):
                rejected[label] = rejected.get(label, 0) + 1
                continue
            payload = {key: value for key, value in row.items() if key in field_names}
            payload["client_id"] = client_id  # never trust the snapshot's tenant
            pk = str(payload.get("id") or "")
            if pk and pk in taken[label]:
                skipped[label] = skipped.get(label, 0) + 1
                continue
            if any(
                payload.get(f.attname) not in (None, "")
                and str(payload[f.attname]) not in allowed_ids(f.related_model)
                for f in tenant_fks
            ):
                rejected[label] = rejected.get(label, 0) + 1
                continue
            if label == "accounts.User":
                payload["password"] = make_password(None)
            to_create.append(model(**payload))

        if to_create:
            model.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
            created[label] = len(to_create)

    record_audit(
        client=client_id,
        actor=user,
        action="restore",
        entity_type="Client",
        entity_id=None,
        description=f"Snapshot restored: {sum(created.values())} rows created",
        after={"created": created, "skipped": skipped, "rejected": rejected},
    )
    return {"created": created, "skipped": skipped, "rejected": rejected}
