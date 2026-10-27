"""
In-house production: yarn / raw material -> grey fabric batch.

    create run -> issue material (PRODUCTION_ISSUE, at WAC) -> record output
    rolls (PRODUCTION_OUTPUT into QC Hold, one batch) -> QC disposition ->
    complete (wastage recorded; issued material + expenses become the batch
    cost, once).

Agency production stays in ``apps.pms`` (ProductionInstruction).
"""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.exceptions import BusinessRuleViolation, Conflict, ValidationFailed
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number

from . import qc as qc_service
from . import services as stock
from .models import LotTaka, ProductionExpense, ProductionMaterialIssue, ProductionRun

EXPENSE_TYPES = ("Production labour", "Machine / production expense", "Other direct")
OPEN_STATUSES = ("Draft", "In Progress")


def _locked(run):
    return ProductionRun.objects.select_for_update().get(pk=run.pk)


def _assert_open(run):
    if run.status not in OPEN_STATUSES:
        raise Conflict(f"Production run {run.run_number} is {run.status}.", code="RUN_CLOSED")


def _post_journal(run, *, postings, source_type, source_id, narration, entry_date=None, user=None):
    """Post one production journal entry, once per source record."""
    from apps.accounting import services as ledger
    from apps.accounting.models import JournalEntry

    if JournalEntry.objects.filter(
        client_id=run.client_id, source_document_type=source_type, source_document_id=source_id,
        reversal_of__isnull=True, deleted_at__isnull=True,
    ).exclude(status="Reversed").exists():
        return None
    return ledger.post_entry(
        client=run.client_id, postings=postings, entry_date=entry_date or timezone.localdate(),
        narration=narration, source_document_type=source_type, source_document_id=source_id, user=user,
    )


def _audit(run, user, action, description, after=None):
    record_audit(
        client=run.client_id, actor=user, action=action, entity_type="ProductionRun",
        entity_id=run.id, entity_label=run.run_number, description=description, after=after,
    )


@transaction.atomic
def create_run(*, client, output_item, planned_qty, user=None, **fields):
    planned_qty = round4(D(planned_qty or 0))
    if planned_qty <= ZERO:
        raise ValidationFailed("Enter the planned quantity.", field_errors={"plannedQty": ["Must be above 0."]})
    run = ProductionRun.objects.create(
        client=client,
        run_number=allocate_number(client, "PRUN", fields.get("run_date")),
        output_item=output_item,
        planned_qty=planned_qty,
        uom=fields.pop("uom", None) or output_item.uom or "Meter",
        created_by=user if getattr(user, "is_authenticated", False) else None,
        **fields,
    )
    _audit(run, user, "create", f"Production run {run.run_number} created")
    return run


@transaction.atomic
def issue_material(run, *, item, location, quantity, customer_supplied=False, user=None):
    """Issue yarn / raw material to the run: stock goes down now, at WAC,
    and its cost moves from Inventory to Work in Progress."""
    from apps.accounting import services as ledger

    run = _locked(run)
    _assert_open(run)
    quantity = round4(D(quantity or 0))
    if quantity <= ZERO:
        raise ValidationFailed("Enter the quantity issued.", field_errors={"quantity": ["Must be above 0."]})
    if str(item.id) == str(run.output_item_id):
        raise ValidationFailed("Issue the raw material, not the fabric being made.", field_errors={"itemId": ["Same as output."]})
    stock.assert_sufficient_stock(run.client_id, item, quantity, location_id=location.id)
    unit_cost = ZERO if customer_supplied else round4(D(ledger.inventory_unit_cost(run.client_id, item, location.id) or 0))
    movement = stock.post_movement(
        client_id=run.client_id,
        item=item,
        location=location,
        type="PRODUCTION_ISSUE",
        quantity=-quantity,
        unit_cost=unit_cost,
        reference_type="ProductionRun",
        reference_id=run.id,
        reference_number=run.run_number,
        notes="Customer-supplied material" if customer_supplied else None,
        user=user,
    )
    issue = ProductionMaterialIssue.objects.create(
        client_id=run.client_id, run=run, item=item, location=location, quantity=quantity,
        unit_cost=unit_cost, amount=round2(unit_cost * quantity), customer_supplied=customer_supplied,
        movement=movement, created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    # Dr Work in Progress | Cr Inventory -- at the cost the stock left at.
    # Customer-supplied material is not OSCAR's, so it carries no entry.
    if issue.amount > ZERO:
        _post_journal(
            run,
            postings=[
                ledger.debit(ledger.system_account(run.client_id, "wip"), issue.amount,
                             description=f"{run.run_number}: {quantity} {item.name} issued"),
                ledger.credit(ledger.system_account(run.client_id, "stock"), issue.amount,
                              description=f"{item.name} issued to {run.run_number}"),
            ],
            source_type="ProductionIssue",
            source_id=issue.id,
            narration=f"Production {run.run_number}: material issue ({item.name})",
            entry_date=issue.issued_on,
            user=user,
        )
    if run.status == "Draft":
        run.status = "In Progress"
        run.save(update_fields=["status", "updated_at"])
    _audit(run, user, "issue_material", f"Issued {quantity} {item.name}")
    return issue


@transaction.atomic
def record_output(run, *, rolls, user=None):
    """Record produced rolls: each becomes a taka of the run's batch at QC Hold.

    ``rolls`` = [{meters, weightKg?, width?}]. Partial output is fine; later
    rolls join the same lot and batch.
    """
    from apps.inventory.batch_services import set_ownership

    run = _locked(run)
    _assert_open(run)
    rolls = [r for r in (rolls or []) if D(r.get("meters") or 0) > ZERO]
    if not rolls:
        raise ValidationFailed("Enter at least one roll with metres.", field_errors={"rolls": ["Required."]})
    qc_location = stock.system_location(run.client_id, "QC_HOLD")
    if not run.lot_number:
        run.lot_number = allocate_number(run.client_id, "LOT")
        run.save(update_fields=["lot_number", "updated_at"])
    created = []
    for roll in rolls:
        meters = round4(D(roll.get("meters")))
        taka_code = allocate_number(run.client_id, "TAK")
        taka = LotTaka.objects.create(
            client_id=run.client_id,
            item=run.output_item,
            fabric_quality=run.fabric_quality,
            shade=run.shade,
            location=qc_location,
            lot_number=run.lot_number,
            taka_number=stock._unique_taka_number(run.client_id, run.lot_number, str(len(created) + 1)),
            taka_id=taka_code,
            barcode=taka_code,
            received_quantity=meters,
            available_quantity=ZERO,
            roll_weight=roll.get("weightKg") or None,
            width=roll.get("width") or run.width,
            gsm=run.gsm,
            uom=run.uom,
            status="QC Hold",
            qc_status="Pending",
            source_document_type="Production",
            source_document_id=run.id,
            source_document_number=run.run_number,
        )
        stock.post_movement(
            client_id=run.client_id,
            item=run.output_item,
            location=qc_location,
            type="PRODUCTION_OUTPUT",
            quantity=meters,
            reference_type="ProductionRun",
            reference_id=run.id,
            reference_number=run.run_number,
            lot_taka=taka,
            user=user,
        )
        created.append(taka)
    if run.batch_id is None:
        run.batch_id = created[0].batch_id
        run.save(update_fields=["batch", "updated_at"])
        if run.customer_id:
            set_ownership(run.batch, ownership="CUSTOMER", owner_party_id=run.customer_id, user=user,
                          reason=f"Manufactured for the customer on {run.run_number}")
    if run.status == "Draft":
        run.status = "In Progress"
        run.save(update_fields=["status", "updated_at"])
    _audit(run, user, "record_output", f"{len(created)} roll(s), {sum((D(t.received_quantity) for t in created), ZERO)} produced")
    return created


@transaction.atomic
def add_expense(run, *, cost_type, amount, remarks=None, user=None):
    run = _locked(run)
    if run.costs_posted:
        raise Conflict(f"Costs of {run.run_number} are already posted.", code="RUN_COSTED")
    if cost_type not in EXPENSE_TYPES:
        raise ValidationFailed("Unknown expense type.", field_errors={"costType": list(EXPENSE_TYPES)})
    amount = round2(D(amount or 0))
    if amount <= ZERO:
        raise ValidationFailed("Enter the amount.", field_errors={"amount": ["Must be above 0."]})
    from apps.accounting import services as ledger

    expense = ProductionExpense.objects.create(
        client_id=run.client_id, run=run, cost_type=cost_type, amount=amount, remarks=remarks,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    # Dr Work in Progress | Cr Manufacturing Overhead Absorbed (labour and
    # overhead are booked to expense by payroll / vouchers; this absorbs the
    # production share into WIP without counting it twice).
    _post_journal(
        run,
        postings=[
            ledger.debit(ledger.system_account(run.client_id, "wip"), amount,
                         description=f"{run.run_number}: {cost_type}"),
            ledger.credit(ledger.system_account(run.client_id, "production_absorbed"), amount,
                          description=f"{cost_type} absorbed into {run.run_number}"),
        ],
        source_type="ProductionExpense",
        source_id=expense.id,
        narration=f"Production {run.run_number}: {cost_type}" + (f" ({remarks})" if remarks else ""),
        user=user,
    )
    return expense


def qc_pool(run):
    qc_location = stock.system_location(run.client_id, "QC_HOLD")
    lots = LotTaka.objects.filter(
        client_id=run.client_id, source_document_type="Production", source_document_id=run.id,
        location_id=qc_location.id, available_quantity__gt=0, deleted_at__isnull=True,
    ).order_by("created_at", "taka_number")
    return [{"line": None, "item_id": t.item_id, "lot": t, "qty": round4(t.available_quantity),
             "from": qc_location.id} for t in lots]


@transaction.atomic
def dispose_qc(run, *, quantities, good_location_id, user=None, remarks=None):
    run = _locked(run)
    units = qc_pool(run)
    if not units:
        raise Conflict(f"No rolls of {run.run_number} are waiting for QC.", code="QC_ALREADY_DONE")
    return qc_service.dispose_lot_units(
        units, client_id=run.client_id, quantities=quantities, good_location_id=good_location_id,
        reference_type="ProductionRun", reference_id=run.id, reference_number=run.run_number,
        source_document_type="ProductionRun", source_document_id=run.id, label=run.run_number,
        user=user, remarks=remarks,
    )


@transaction.atomic
def complete_run(run, *, wastage_qty=None, wastage_uom=None, user=None):
    """Close the run and book its cost to the batch, exactly once."""
    from . import costing_services

    run = _locked(run)
    _assert_open(run)
    if not run.lot_number:
        raise BusinessRuleViolation("Record the output before completing the run.", code="NO_OUTPUT")
    if wastage_qty is not None:
        run.wastage_qty = round4(D(wastage_qty or 0))
        run.wastage_uom = wastage_uom or run.wastage_uom
    material = sum((D(i.amount) for i in run.issues.filter(deleted_at__isnull=True)), ZERO)
    produced = sum(
        (D(t.received_quantity) for t in LotTaka.objects.filter(
            client_id=run.client_id, source_document_type="Production", source_document_id=run.id,
            deleted_at__isnull=True,
        )),
        ZERO,
    )
    if not run.costs_posted:
        if material > ZERO:
            costing_services.post_lot_cost(
                client_id=run.client_id, lot_number=run.lot_number, cost_type="Yarn / raw material",
                amount=material, quantity=produced, source_document_type="ProductionRun",
                source_document_id=run.id, source_document_number=run.run_number, user=user,
            )
        for expense in run.expenses.filter(deleted_at__isnull=True):
            costing_services.post_lot_cost(
                client_id=run.client_id, lot_number=run.lot_number, cost_type=expense.cost_type,
                amount=expense.amount, quantity=produced, source_document_type="ProductionRun",
                source_document_id=run.id, source_document_number=run.run_number, user=user,
                remarks=expense.remarks,
            )
        run.costs_posted = True
    # Dr Inventory (finished grey fabric) | Cr Work in Progress, at the run's
    # actual cost. A run for a customer makes the customer's material, so its
    # cost is OSCAR's cost of the manufacturing service, not stock.
    from apps.accounting import services as ledger

    total = round2(material + sum((D(e.amount) for e in run.expenses.filter(deleted_at__isnull=True)), ZERO))
    if total > ZERO:
        to_key = "cogs" if run.customer_id else "stock"
        batch_code = run.batch.batch_code if run.batch_id else "-"
        _post_journal(
            run,
            postings=[
                ledger.debit(ledger.system_account(run.client_id, to_key), total,
                             description=f"Batch {batch_code} from {run.run_number}"),
                ledger.credit(ledger.system_account(run.client_id, "wip"), total,
                              description=f"{run.run_number} completed"),
            ],
            source_type="ProductionRun",
            source_id=run.id,
            narration=f"Production {run.run_number} completed: batch {batch_code}, lot {run.lot_number}",
            user=user,
        )
    run.status = "Completed"
    run.completed_at = timezone.now()
    run.save(update_fields=["wastage_qty", "wastage_uom", "costs_posted", "status", "completed_at", "updated_at"])
    _audit(run, user, "complete", f"Completed: {produced} produced, wastage {run.wastage_qty} {run.wastage_uom or ''}".strip())
    return run


@transaction.atomic
def cancel_run(run, *, reason=None, user=None):
    """Cancel an unfinished run: stock movements and journal entries are
    reversed (never deleted). Not allowed once any roll has left QC Hold."""
    from apps.accounting import services as ledger

    run = _locked(run)
    _assert_open(run)
    produced = sum(
        (D(t.received_quantity) for t in LotTaka.objects.filter(
            client_id=run.client_id, source_document_type="Production", source_document_id=run.id,
            parent_taka__isnull=True, deleted_at__isnull=True,
        )),
        ZERO,
    )
    waiting = sum((u["qty"] for u in qc_pool(run)), ZERO)
    if produced > ZERO and waiting < produced:
        raise BusinessRuleViolation(
            f"Rolls of {run.run_number} have already been through QC; reverse those first.",
            code="RUN_HAS_QC",
        )
    stock.reverse_movements(reference_type="ProductionRun", reference_id=run.id, client_id=run.client_id,
                            user=user, notes=f"Production {run.run_number} cancelled")
    for issue in run.issues.filter(deleted_at__isnull=True):
        ledger.reverse_document_entries(client_id=run.client_id, source_document_type="ProductionIssue",
                                        source_document_id=issue.id, user=user)
    for expense in run.expenses.filter(deleted_at__isnull=True):
        ledger.reverse_document_entries(client_id=run.client_id, source_document_type="ProductionExpense",
                                        source_document_id=expense.id, user=user)
    run.status = "Cancelled"
    run.notes = ((run.notes or "") + f"\nCancelled: {reason}" if reason else run.notes)
    run.save(update_fields=["status", "notes", "updated_at"])
    _audit(run, user, "cancel", f"Cancelled{': ' + reason if reason else ''}")
    return run


def run_accounting(run):
    """The run's journal entries and the figures that must agree."""
    from apps.accounting.models import JournalEntry, JournalLine

    from .models import LotCostEntry, StockMovement

    issue_ids = list(run.issues.values_list("id", flat=True))
    expense_ids = list(run.expenses.values_list("id", flat=True))
    sources = Q(source_document_type="ProductionRun", source_document_id=run.id) \
        | Q(source_document_type="ProductionIssue", source_document_id__in=issue_ids) \
        | Q(source_document_type="ProductionExpense", source_document_id__in=expense_ids)
    entries = list(
        JournalEntry.objects.filter(sources, client_id=run.client_id, deleted_at__isnull=True)
        .prefetch_related("lines__account").order_by("entry_date", "created_at")
    )
    live = [e for e in entries if e.status != "Reversed" and e.reversal_of_id is None]
    wip_lines = JournalLine.objects.filter(journal_entry__in=live, account__system_key="wip")
    wip_balance = sum((D(l.debit) - D(l.credit) for l in wip_lines), ZERO)
    issued_value = -sum(
        (D(m.quantity) * D(m.unit_cost or 0) for m in StockMovement.objects.filter(
            client_id=run.client_id, reference_type="ProductionRun", reference_id=run.id, type="PRODUCTION_ISSUE",
        )),
        ZERO,
    )
    batch_cost = sum(
        (D(e.amount) for e in LotCostEntry.objects.filter(
            client_id=run.client_id, source_document_type="ProductionRun", source_document_id=run.id,
            deleted_at__isnull=True, is_approved=True,
        )),
        ZERO,
    )
    return {
        "entries": [{
            "entryNumber": e.entry_number,
            "date": e.entry_date.isoformat(),
            "narration": e.narration,
            "status": e.status,
            "reversal": e.reversal_of_id is not None,
            "lines": [{
                "account": f"{l.account.code} {l.account.name}",
                "debit": str(l.debit),
                "credit": str(l.credit),
                "description": l.description,
            } for l in e.lines.all()],
        } for e in entries],
        "wipBalance": str(round2(wip_balance)),
        "materialIssuedValue": str(round2(issued_value)),
        "costPostedToBatch": str(round2(batch_cost)),
        "reconciled": (
            (run.status != "Completed" or round2(wip_balance) == ZERO)
            and (not run.costs_posted or round2(batch_cost) == round2(
                issued_value + sum((D(e.amount) for e in run.expenses.filter(deleted_at__isnull=True)), ZERO)
            ))
        ),
    }


def serialize_run(run):
    takas = list(LotTaka.objects.filter(
        client_id=run.client_id, source_document_type="Production", source_document_id=run.id,
        deleted_at__isnull=True,
    ).order_by("created_at"))
    produced = sum((D(t.received_quantity) for t in takas if t.parent_taka_id is None), ZERO)
    waiting_qc = sum((u["qty"] for u in qc_pool(run)), ZERO)
    return {
        "id": str(run.id),
        "runNumber": run.run_number,
        "runDate": run.run_date.isoformat(),
        "status": run.status,
        "outputItemId": str(run.output_item_id),
        "outputItem": run.output_item.name,
        "plannedQty": str(run.planned_qty),
        "producedQty": str(round4(produced)),
        "waitingQcQty": str(round4(waiting_qc)),
        "uom": run.uom,
        "customerId": str(run.customer_id) if run.customer_id else None,
        "customerName": run.customer.name if run.customer_id else None,
        "machine": run.machine,
        "lotNumber": run.lot_number,
        "batchCode": run.batch.batch_code if run.batch_id else None,
        "wastageQty": str(run.wastage_qty),
        "wastageUom": run.wastage_uom,
        "costsPosted": run.costs_posted,
        "notes": run.notes,
        "issues": [{
            "id": str(i.id), "item": i.item.name, "location": i.location.name,
            "quantity": str(i.quantity), "unitCost": str(i.unit_cost), "amount": str(i.amount),
            "customerSupplied": i.customer_supplied, "issuedOn": i.issued_on.isoformat(),
        } for i in run.issues.filter(deleted_at__isnull=True).select_related("item", "location")],
        "expenses": [{
            "id": str(e.id), "costType": e.cost_type, "amount": str(e.amount), "remarks": e.remarks,
        } for e in run.expenses.filter(deleted_at__isnull=True)],
        "rolls": [{
            "id": str(t.id), "takaId": t.taka_id, "meters": str(t.received_quantity),
            "status": t.status, "qcStatus": t.qc_status,
        } for t in takas if t.parent_taka_id is None],
        "accounting": run_accounting(run),
    }
