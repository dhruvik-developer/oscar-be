"""Serializers for the inventory module (api.md §7)."""
from rest_framework import serializers

from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)
from apps.masters.models import Item, ItemPart, Location

from .models import (
    FaultyPart,
    LotCostEntry,
    QCInspection,
    QCInspectionLine,
    QCInspectionLineDefect,
    QualityStandard,
    Sample,
    ServiceUsage,
    StockAudit,
    StockAuditLine,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    Trip,
    TripMaterialLine,
    ZoneRequest,
    ZoneRequestLine,
)


class StockMovementSerializer(BaseModelSerializer):
    """The movement record of api.md §7.2, field for field."""

    itemId = serializers.CharField(source="item_id", read_only=True)
    itemSku = serializers.CharField(source="item.sku", read_only=True)
    itemName = serializers.CharField(source="item.name", read_only=True)
    locationId = serializers.CharField(source="location_id", read_only=True)
    locationName = serializers.CharField(source="location.name", read_only=True)
    referenceId = serializers.CharField(source="reference_id", read_only=True)
    sourceDocumentId = serializers.CharField(source="source_document_id", read_only=True)
    originalMovementId = serializers.CharField(source="original_movement_id", read_only=True)
    reversalMovementId = serializers.CharField(source="reversal_movement_id", read_only=True)
    date = serializers.DateField(source="movement_date", read_only=True)
    serials = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = [
            "id", "itemId", "itemSku", "itemName", "locationId", "locationName",
            "type", "quantity", "uom", "weighed_qty", "unit_cost",
            "reference_type", "referenceId", "reference_number",
            "source_document_type", "sourceDocumentId",
            "originalMovementId", "reversalMovementId",
            "batch_number", "serials", "date", "notes", "created_at",
        ]

    def get_serials(self, movement):
        from apps.masters.models import ItemSerial

        cached = getattr(movement, "_serials", None)
        if cached is not None:
            return cached
        return list(
            ItemSerial.objects.filter(
                sold_movement_id=movement.id
            ).values_list("serial_no", flat=True)
        ) or list(
            ItemSerial.objects.filter(
                received_movement_id=movement.id
            ).values_list("serial_no", flat=True)
        )


class StockAdjustmentSerializer(BaseSerializer):
    """``POST /inventory/adjustments/`` -- replaces ``adjustItemStock``."""

    itemId = TenantPrimaryKeyRelatedField(queryset=Item.objects.all())
    locationId = TenantPrimaryKeyRelatedField(
        queryset=Location.objects.all(), required=False, allow_null=True
    )
    quantity = QuantityField()
    #: True means "set stock to this number"; False means "add this delta".
    isAbsolute = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField()
    unitCost = serializers.DecimalField(
        max_digits=18, decimal_places=4, coerce_to_string=False, required=False
    )


class StockPositionSerializer(BaseSerializer):
    itemId = serializers.CharField()
    sku = serializers.CharField()
    name = serializers.CharField()
    category = serializers.CharField(allow_null=True)
    uom = serializers.CharField()
    onHand = QuantityField()
    reserved = QuantityField()
    available = QuantityField()
    damaged = QuantityField()
    reorderLevel = QuantityField()
    status = serializers.CharField()
    unitCost = serializers.DecimalField(
        max_digits=18, decimal_places=4, coerce_to_string=False
    )
    value = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=False)


class StockTransferLineSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    name = serializers.CharField(source="item.name", read_only=True)

    #: ``[{"lotTakaId": uuid, "qty": "12.5"}]`` -- validated against the
    #: source location when the transfer is created.
    takas = serializers.ListField(child=serializers.DictField(), required=False, default=list)

    class Meta:
        model = StockTransferLine
        fields = ["id", "itemId", "sku", "name", "qty", "received_qty", "takas"]

    def validate_qty(self, value):
        from decimal import Decimal
        if value is not None and Decimal(str(value)) <= Decimal("0"):
            raise serializers.ValidationError("Transfer quantity must be greater than zero.")
        return value


class StockTransferSerializer(BaseModelSerializer):
    """db.md §7.4 standardises on ``items[]`` and keeps the denormalised names
    as read-only echoes, which is what the mock data's flat form used."""

    items = StockTransferLineSerializer(many=True, required=False)
    sourceLocationId = TenantPrimaryKeyRelatedField(
        source="from_location", queryset=Location.objects.all()
    )
    destLocationId = TenantPrimaryKeyRelatedField(
        source="to_location", queryset=Location.objects.all()
    )
    sourceLocation = serializers.CharField(source="from_location.name", read_only=True)
    destLocation = serializers.CharField(source="to_location.name", read_only=True)
    itemsCount = serializers.SerializerMethodField()
    date = serializers.DateField(source="transfer_date", required=False)
    lines = serializers.SerializerMethodField()
    requestedBy = serializers.SerializerMethodField()
    approvedBy = serializers.SerializerMethodField()
    receivedBy = serializers.SerializerMethodField()

    class Meta:
        model = StockTransfer
        fields = [
            "id", "transfer_number", "sourceLocationId", "destLocationId",
            "sourceLocation", "destLocation", "date", "itemsCount", "status",
            "shipped_by", "notes", "items", "lines", "requestedBy", "approvedBy",
            "receivedBy", "approved_at", "dispatched_at", "received_at", "completed_at",
            "reference_type", "reference_id", "reference_number", "created_at", "updated_at",
        ]
        # Status moves only through the request / approve / dispatch /
        # receive / complete / cancel actions.
        read_only_fields = [
            "transfer_number", "status", "approved_at", "dispatched_at", "received_at",
            "completed_at", "reference_type", "reference_id", "reference_number",
            "created_at", "updated_at",
        ]

    def get_itemsCount(self, transfer):
        return transfer.items.filter(deleted_at__isnull=True).count()

    def get_lines(self, transfer):
        from .warehouse_services import serialize_transfer_extra

        return serialize_transfer_extra(transfer)

    @staticmethod
    def _user(user):
        return (user.name or user.email) if user else None

    def get_requestedBy(self, transfer):
        return self._user(transfer.requested_by)

    def get_approvedBy(self, transfer):
        return self._user(transfer.approved_by)

    def get_receivedBy(self, transfer):
        return self._user(transfer.received_by)

    def validate(self, attrs):
        source = attrs.get("from_location") or getattr(self.instance, "from_location", None)
        destination = attrs.get("to_location") or getattr(self.instance, "to_location", None)
        if source and destination and source.id == destination.id:
            raise serializers.ValidationError(
                {"destLocationId": ["Must differ from the source location."]}
            )
        items = attrs.get("items")
        if self.instance is None and items is not None and len(items) == 0:
            raise serializers.ValidationError({"items": ["Add at least one item to transfer."]})
        return attrs


class FaultyPartSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    itemName = serializers.CharField(source="item.name", read_only=True)
    vendor = serializers.CharField(source="vendor.name", read_only=True)
    date = serializers.DateField(source="reported_date")
    qty = QuantityField(source="quantity")
    timeline = serializers.SerializerMethodField()

    class Meta:
        model = FaultyPart
        fields = [
            "id", "rma_number", "date", "itemId", "sku", "itemName", "qty",
            "vendor", "status", "notes", "fault_description", "timeline",
            "created_at", "updated_at",
        ]
        read_only_fields = ["rma_number", "created_at", "updated_at"]

    def get_timeline(self, part):
        """api.md §7.3 -- an ordered progress tracker derived from the status.

        The frontend renders ``{ label, timestamp, status }`` directly, so the
        server produces it rather than storing a parallel list.
        """
        steps = [
            "Reported", "Pending Action", "Sent for Replacement",
            "Replaced", "Credited", "Closed",
        ]
        try:
            current = steps.index(part.status)
        except ValueError:
            current = 0
        return [
            {
                "label": label,
                "timestamp": part.created_at if index == 0 else None,
                "status": (
                    "completed" if index < current
                    else "current" if index == current
                    else "future"
                ),
            }
            for index, label in enumerate(steps)
        ]


class ServiceUsageSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    qty = QuantityField(source="quantity")
    date = serializers.DateField(source="used_on")

    class Meta:
        model = ServiceUsage
        fields = [
            "id", "ticket_number", "technician", "itemId", "sku", "qty", "date",
            "notes", "job_reference", "chargeable", "created_at",
        ]
        read_only_fields = ["ticket_number", "created_at"]


class ZoneRequestLineSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    product = serializers.CharField(source="item.name", read_only=True)
    qty = QuantityField(source="requested_qty")
    warehouseStock = serializers.SerializerMethodField()

    class Meta:
        model = ZoneRequestLine
        fields = ["id", "itemId", "sku", "product", "qty", "issued_qty", "warehouseStock"]

    def get_warehouseStock(self, line):
        """api.md §7.3 -- ``warehouseStock`` is the available quantity *at the
        time the request is read*, so it is computed on read."""
        from . import services as stock

        client_id = self.context.get("client_id") or line.client_id
        return stock.calculate_item_stock(client_id, line.item_id)["available"]


class ZoneRequestSerializer(BaseModelSerializer):
    lines = ZoneRequestLineSerializer(many=True, required=False)
    zone = serializers.CharField(source="zone_location.name", read_only=True)
    zoneLocationId = TenantPrimaryKeyRelatedField(
        source="zone_location", queryset=Location.objects.all()
    )
    requestedBy = serializers.CharField(source="requested_by_name", required=False, allow_null=True)
    date = serializers.DateField(source="request_date", required=False, allow_null=True)

    class Meta:
        model = ZoneRequest
        fields = [
            "id", "request_number", "requestedBy", "zone", "zoneLocationId",
            "target_sector", "date", "requested_at", "status", "notes",
            "manager_signoff_needed", "reject_reason", "lines",
            "created_at", "updated_at",
        ]
        read_only_fields = ["request_number", "requested_at", "created_at", "updated_at"]


class StockAuditLineSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    name = serializers.CharField(source="item.name", read_only=True)
    costPrice = serializers.DecimalField(source="item.cost_price", max_digits=18, decimal_places=4, read_only=True)
    variance = QuantityField(read_only=True)

    class Meta:
        model = StockAuditLine
        fields = [
            "id", "itemId", "sku", "name", "costPrice", "system_qty", "counted_qty",
            "variance", "reason", "adjustment_movement",
        ]
        read_only_fields = ["variance", "adjustment_movement"]


class StockAuditSerializer(BaseModelSerializer):
    lines = StockAuditLineSerializer(many=True, required=False)
    locationName = serializers.CharField(source="location.name", read_only=True, allow_null=True)
    location_name = serializers.CharField(source="location.name", read_only=True, allow_null=True)
    conductedByName = serializers.SerializerMethodField(read_only=True)
    conducted_by_name = serializers.SerializerMethodField(read_only=True)
    createdByName = serializers.SerializerMethodField(read_only=True)
    created_by_name = serializers.SerializerMethodField(read_only=True)
    linesCount = serializers.SerializerMethodField(read_only=True)
    lines_count = serializers.SerializerMethodField(read_only=True)
    auditNumber = serializers.CharField(source="audit_number", read_only=True)
    periodMonth = serializers.DateField(source="period_month", read_only=True)
    postedAt = serializers.DateTimeField(source="posted_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    def get_conductedByName(self, obj):
        if not obj.conducted_by:
            return None
        return obj.conducted_by.name or obj.conducted_by.email

    def get_conducted_by_name(self, obj):
        return self.get_conductedByName(obj)

    def get_createdByName(self, obj):
        user = obj.conducted_by or getattr(obj, "created_by", None)
        if not user:
            return None
        return user.name or user.email

    def get_created_by_name(self, obj):
        return self.get_createdByName(obj)

    def get_linesCount(self, obj):
        if hasattr(obj, "_prefetched_objects_cache") and "lines" in obj._prefetched_objects_cache:
            return len(obj.lines.all())
        return obj.lines.count()

    def get_lines_count(self, obj):
        return self.get_linesCount(obj)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret['audit_number'] = instance.audit_number
        ret['period_month'] = str(instance.period_month) if instance.period_month else None
        ret['location_name'] = instance.location.name if instance.location else None
        ret['status'] = instance.status
        return ret

    class Meta:
        model = StockAudit
        fields = [
            "id", "audit_number", "auditNumber", "location", "locationName", "location_name",
            "period_month", "periodMonth", "status",
            "conducted_by", "conductedByName", "conducted_by_name",
            "created_by", "createdByName", "created_by_name",
            "posted_at", "postedAt", "notes", "lines", "linesCount", "lines_count",
            "created_at", "createdAt", "updated_at",
        ]
        read_only_fields = ["audit_number", "auditNumber", "posted_at", "postedAt", "created_at", "createdAt", "updated_at"]


class QualityStandardSerializer(BaseModelSerializer):
    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="masters.ItemCategory", required=False, allow_null=True
    )
    checks = serializers.JSONField(source="checklist", required=False)
    active = serializers.BooleanField(source="is_active", required=False)

    class Meta:
        model = QualityStandard
        fields = [
            "id", "name", "category", "categoryId", "checks", "tolerance_pct",
            "active", "created_at", "updated_at",
        ]



class ValuationRowSerializer(BaseSerializer):
    itemId = serializers.CharField()
    sku = serializers.CharField()
    name = serializers.CharField()
    category = serializers.CharField(allow_null=True)
    location = serializers.CharField(allow_null=True)
    quantity = QuantityField()
    unitCost = serializers.DecimalField(
        max_digits=18, decimal_places=4, coerce_to_string=False
    )
    value = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=False)
    ageingBucket = serializers.CharField(allow_null=True)


class ItemPartSerializer(BaseModelSerializer):
    """Machine BOM lines (``itemParts``)."""

    parentItemId = TenantPrimaryKeyRelatedField(source="parent_item", model="masters.Item")
    partItemId = TenantPrimaryKeyRelatedField(source="part_item", model="masters.Item")
    partName = serializers.CharField(source="part_item.name", read_only=True)
    partSku = serializers.CharField(source="part_item.sku", read_only=True)
    requiredQty = QuantityField(source="required_qty", required=False)

    class Meta:
        model = ItemPart
        fields = [
            "id", "parentItemId", "partItemId", "partName", "partSku",
            "requiredQty", "created_at",
        ]
        read_only_fields = ["created_at"]


class QCInspectionLineDefectSerializer(BaseModelSerializer):
    defectId = TenantPrimaryKeyRelatedField(source="defect", model="masters.DefectMaster")
    defectCode = serializers.ReadOnlyField(source="defect.code")
    defectName = serializers.ReadOnlyField(source="defect.name")
    defectCategory = serializers.ReadOnlyField(source="defect.category")
    defectSeverity = serializers.ReadOnlyField(source="defect.severity")

    class Meta:
        model = QCInspectionLineDefect
        fields = [
            "id", "defectId", "defectCode", "defectName", "defectCategory",
            "defectSeverity", "defect_count", "points", "distance_meter", "remarks",
        ]
        read_only_fields = ["id", "defectCode", "defectName", "defectCategory", "defectSeverity"]


class QCInspectionLineSerializer(BaseModelSerializer):
    lotTakaId = TenantPrimaryKeyRelatedField(
        source="lot_taka", model="inventory.LotTaka", required=False, allow_null=True
    )
    takaNo = serializers.CharField(source="taka_number", required=False)
    lotNo = serializers.CharField(source="lot_number", required=False, allow_null=True, allow_blank=True)
    originalMeter = QuantityField(source="original_meter", required=False)
    actualMeter = QuantityField(source="actual_meter", required=False)
    rollWeight = serializers.DecimalField(source="roll_weight", max_digits=12, decimal_places=4, required=False, allow_null=True)
    penaltyPoints = serializers.IntegerField(source="penalty_points", required=False)
    pointsPer100SqMeter = serializers.DecimalField(
        source="points_per_100_sq_meter", max_digits=9, decimal_places=2, required=False
    )
    defects = QCInspectionLineDefectSerializer(many=True, required=False)

    class Meta:
        model = QCInspectionLine
        fields = [
            "id", "lotTakaId", "takaNo", "lotNo", "originalMeter", "actualMeter",
            "rollWeight", "width", "gsm", "grade", "penaltyPoints", "pointsPer100SqMeter",
            "disposition", "remarks", "defects",
        ]
        read_only_fields = ["id"]


class QCInspectionSerializer(BaseModelSerializer):
    inspectionNumber = serializers.CharField(source="inspection_number", required=False)
    sourceDocumentType = serializers.CharField(source="source_document_type", required=False)
    sourceDocumentId = serializers.UUIDField(source="source_document_id", required=False, allow_null=True)
    sourceDocumentNumber = serializers.CharField(source="source_document_number", required=False, allow_null=True, allow_blank=True)
    inspectionDate = serializers.DateField(source="inspection_date", required=False)
    inspectorId = TenantPrimaryKeyRelatedField(source="inspector", model="accounts.User", required=False, allow_null=True)
    inspectorName = serializers.SerializerMethodField()
    inspectorEmployeeId = TenantPrimaryKeyRelatedField(source="inspector_employee", model="hrms.Employee", required=False, allow_null=True)
    purchaseOrderId = TenantPrimaryKeyRelatedField(source="purchase_order", model="purchase.PurchaseOrder", required=False, allow_null=True)
    productionInstructionId = TenantPrimaryKeyRelatedField(source="production_instruction", model="pms.ProductionInstruction", required=False, allow_null=True)
    fabricItemId = TenantPrimaryKeyRelatedField(source="fabric_item", model="masters.Item", required=False, allow_null=True)
    inspectionType = serializers.CharField(source="inspection_type", required=False)
    inspectionMethod = serializers.CharField(source="inspection_method", required=False)
    gsm = serializers.DecimalField(max_digits=9, decimal_places=2, required=False, allow_null=True)
    width = serializers.DecimalField(max_digits=9, decimal_places=2, required=False, allow_null=True)
    defectsFound = serializers.CharField(source="defects_found", required=False, allow_blank=True, allow_null=True)
    qualityGrade = serializers.CharField(source="quality_grade", required=False, allow_blank=True, allow_null=True)
    photos = serializers.ListField(child=serializers.CharField(), required=False)
    lines = QCInspectionLineSerializer(many=True, required=False)

    class Meta:
        model = QCInspection
        fields = [
            "id", "inspectionNumber", "sourceDocumentType", "sourceDocumentId", "sourceDocumentNumber",
            "purchaseOrderId", "productionInstructionId", "fabricItemId",
            "inspectionType", "inspectionMethod", "gsm", "width", "defectsFound", "qualityGrade", "photos",
            "inspectionDate", "inspectorId", "inspectorName", "inspectorEmployeeId", "shift", "status",
            "total_received_qty", "total_inspected_qty", "passed_qty", "second_quality_qty",
            "reprocess_qty", "rejected_qty", "hold_qty", "notes", "lines", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "inspectionNumber", "created_at", "updated_at"]

    def get_inspectorName(self, obj):
        if obj.inspector:
            return getattr(obj.inspector, "name", None) or getattr(obj.inspector, "email", None)
        if obj.inspector_employee:
            return getattr(obj.inspector_employee, "name", None)
        return None


class LotCostEntrySerializer(BaseModelSerializer):
    lotTakaId = TenantPrimaryKeyRelatedField(
        source="lot_taka", model="inventory.LotTaka", required=False, allow_null=True
    )
    costType = serializers.CharField(source="cost_type")
    costPerMeter = serializers.DecimalField(source="cost_per_meter", max_digits=18, decimal_places=4, read_only=True)
    sourceDocumentType = serializers.CharField(source="source_document_type", required=False)
    sourceDocumentNumber = serializers.CharField(source="source_document_number", required=False, allow_null=True, allow_blank=True)
    isApproved = serializers.BooleanField(source="is_approved", required=False, default=True)
    approvalStatus = serializers.CharField(source="approval_status", required=False, default="Approved")

    class Meta:
        model = LotCostEntry
        fields = [
            "id", "lot_number", "lotTakaId", "costType", "amount", "quantity", "costPerMeter",
            "sourceDocumentType", "source_document_id", "sourceDocumentNumber",
            "date", "posted_by", "isApproved", "approvalStatus", "remarks",
            "created_at", "updated_at",
        ]
        read_only_fields = ["costPerMeter", "created_at", "updated_at"]


class SampleSerializer(BaseModelSerializer):
    sampleNumber = serializers.CharField(source="sample_number", required=False)
    sampleDate = serializers.DateField(source="sample_date", required=False)
    partyId = TenantPrimaryKeyRelatedField(
        source="party", model="masters.Party", required=False, allow_null=True
    )
    partyName = serializers.CharField(source="party.name", read_only=True)
    salespersonId = TenantPrimaryKeyRelatedField(
        source="salesperson", model="hrms.Employee", required=False, allow_null=True
    )
    salespersonName = serializers.CharField(source="salesperson.name", read_only=True)
    brokerId = TenantPrimaryKeyRelatedField(
        source="broker", model="masters.Broker", required=False, allow_null=True
    )
    brokerName = serializers.CharField(source="broker.name", read_only=True)
    sourceLotTakaId = TenantPrimaryKeyRelatedField(
        source="source_lot_taka", model="inventory.LotTaka", required=False, allow_null=True
    )
    fabricQualityId = TenantPrimaryKeyRelatedField(
        source="fabric_quality", model="masters.FabricQuality", required=False, allow_null=True
    )
    fabricQualityName = serializers.CharField(source="fabric_quality.name", read_only=True)
    designId = TenantPrimaryKeyRelatedField(
        source="design", model="masters.Design", required=False, allow_null=True
    )
    designName = serializers.CharField(source="design.name", read_only=True)
    shadeId = TenantPrimaryKeyRelatedField(
        source="shade", model="masters.Shade", required=False, allow_null=True
    )
    shadeName = serializers.CharField(source="shade.name", read_only=True)
    sampleType = serializers.CharField(source="sample_type", required=False, default="CUT_PIECE")
    isReturnable = serializers.BooleanField(source="is_returnable", required=False, default=True)
    dueDate = serializers.DateField(source="due_date", required=False, allow_null=True)
    returnedQuantity = serializers.DecimalField(source="returned_quantity", max_digits=18, decimal_places=4, read_only=True)
    consumedQuantity = serializers.DecimalField(source="consumed_quantity", max_digits=18, decimal_places=4, read_only=True)
    estimatedCost = serializers.DecimalField(source="estimated_cost", max_digits=18, decimal_places=4, read_only=True)
    batchCode = serializers.SerializerMethodField()
    customer = serializers.CharField(source="party.name", read_only=True)
    unitCost = serializers.SerializerMethodField()
    totalValue = serializers.DecimalField(source="estimated_cost", max_digits=18, decimal_places=2, read_only=True)
    availableQuantity = serializers.SerializerMethodField()

    def get_batchCode(self, obj):
        if obj.source_lot_taka and obj.source_lot_taka.batch:
            return obj.source_lot_taka.batch.batch_code
        return obj.source_lot_taka.lot_number if obj.source_lot_taka else ""

    def get_unitCost(self, obj):
        if obj.source_lot_taka and obj.source_lot_taka.rate:
            return float(obj.source_lot_taka.rate)
        if obj.quantity and obj.estimated_cost:
            from apps.core.money import D, round2
            return float(round2(D(obj.estimated_cost) / D(obj.quantity)))
        return 0.0

    def get_availableQuantity(self, obj):
        from apps.core.money import D, ZERO
        out = D(obj.quantity) - D(obj.returned_quantity) - D(obj.consumed_quantity)
        return float(max(ZERO, out))

    class Meta:
        model = Sample
        fields = [
            "id", "sampleNumber", "sampleDate", "partyId", "partyName", "customer",
            "salespersonId", "salespersonName", "brokerId", "brokerName",
            "sourceLotTakaId", "batchCode", "fabricQualityId", "fabricQualityName",
            "designId", "designName", "shadeId", "shadeName",
            "sampleType", "quantity", "uom", "isReturnable", "dueDate", "status",
            "returnedQuantity", "consumedQuantity", "availableQuantity",
            "unitCost", "totalValue", "estimatedCost",
            "remarks", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "sampleNumber", "customer", "batchCode", "returnedQuantity",
            "consumedQuantity", "availableQuantity", "unitCost", "totalValue",
            "estimatedCost", "created_at", "updated_at"
        ]


class TripMaterialLineSerializer(BaseModelSerializer):
    lotTakaId = TenantPrimaryKeyRelatedField(
        source="lot_taka", model="inventory.LotTaka", required=False, allow_null=True
    )
    lotNumber = serializers.CharField(source="lot_number", required=False, allow_null=True, allow_blank=True)
    batchCode = serializers.SerializerMethodField()
    poNumber = serializers.SerializerMethodField()
    takaNumber = serializers.CharField(source="taka_number", required=False, allow_null=True, allow_blank=True)
    itemId = TenantPrimaryKeyRelatedField(
        source="item", model="masters.Item", required=False, allow_null=True
    )
    itemName = serializers.CharField(source="item.name", read_only=True)
    takaId = serializers.CharField(source="lot_taka.taka_id", read_only=True)
    shade = serializers.CharField(source="lot_taka.shade.name", read_only=True, default=None)
    rollWeight = serializers.DecimalField(source="roll_weight", max_digits=12, decimal_places=4, required=False, allow_null=True)
    allocatedFreight = serializers.DecimalField(source="allocated_freight", max_digits=18, decimal_places=4, read_only=True)

    class Meta:
        model = TripMaterialLine
        fields = [
            "id", "lotTakaId", "batchCode", "poNumber", "lotNumber", "takaNumber", "takaId", "shade", "itemId", "itemName",
            "quantity", "rollWeight", "document_reference_type", "document_reference_id",
            "document_reference_number", "allocatedFreight", "created_at",
        ]
        read_only_fields = ["allocatedFreight", "created_at"]

    def get_batchCode(self, obj):
        if obj.lot_taka and getattr(obj.lot_taka, "batch", None):
            return obj.lot_taka.batch.batch_code or obj.lot_number or ""
        return obj.lot_number or ""

    def get_poNumber(self, obj):
        if obj.lot_taka and getattr(obj.lot_taka, "batch", None) and getattr(obj.lot_taka.batch, "purchase_order", None):
            return obj.lot_taka.batch.purchase_order.po_number or ""
        if obj.document_reference_type == "PURCHASE_ORDER" and obj.document_reference_number:
            return obj.document_reference_number
        return ""



class TripSerializer(BaseModelSerializer):
    tripNumber = serializers.CharField(source="trip_number", required=False)
    tripDate = serializers.DateField(source="trip_date", required=False)
    fromLocation = serializers.CharField(source="from_location")
    toLocation = serializers.CharField(source="to_location")
    fromWarehouseId = TenantPrimaryKeyRelatedField(
        source="from_warehouse", model="masters.Location", required=False, allow_null=True
    )
    toWarehouseId = TenantPrimaryKeyRelatedField(
        source="to_warehouse", model="masters.Location", required=False, allow_null=True
    )
    isStockDeducted = serializers.BooleanField(source="is_stock_deducted", read_only=True)
    isStockReceived = serializers.BooleanField(source="is_stock_received", read_only=True)
    transporterId = TenantPrimaryKeyRelatedField(
        source="transporter", model="masters.Transporter", required=False, allow_null=True
    )
    transporterName = serializers.CharField(source="transporter.name", read_only=True)
    transporterPartyId = TenantPrimaryKeyRelatedField(
        source="transporter_party", model="masters.Party", required=False, allow_null=True
    )
    vehicleId = TenantPrimaryKeyRelatedField(
        source="vehicle", model="masters.Vehicle", required=False, allow_null=True
    )
    driverId = TenantPrimaryKeyRelatedField(
        source="driver", model="masters.Driver", required=False, allow_null=True
    )
    vehicleNumber = serializers.CharField(source="vehicle_number", required=False, allow_blank=True)
    driverName = serializers.CharField(source="driver_name", required=False, allow_null=True, allow_blank=True)
    driverMobile = serializers.CharField(source="driver_mobile", required=False, allow_null=True, allow_blank=True)
    proofFileId = TenantPrimaryKeyRelatedField(
        source="proof_file", model="core.File", required=False, allow_null=True
    )
    proofFileUrl = serializers.SerializerMethodField()

    def get_proofFileUrl(self, obj):
        if obj.proof_file_id:
            from apps.core import files as file_service
            from apps.core.models import File
            file_row = getattr(obj, "proof_file", None) or File.objects.filter(pk=obj.proof_file_id).first()
            if file_row:
                if file_row.status != "committed":
                    file_row.status = "committed"
                    file_row.save(update_fields=["status"])
                url = file_service.public_url(file_row)
                if url:
                    sep = "&" if "?" in url else "?"
                    return f"{url}{sep}inline=true"
            token = file_service.sign_download(obj.proof_file_id)
            return f"/api/v1/files/{obj.proof_file_id}/download/?token={token}&inline=true"
        return None
    lrNumber = serializers.CharField(source="lr_number", required=False, allow_null=True, allow_blank=True)
    lrDate = serializers.DateField(source="lr_date", required=False, allow_null=True)
    tripType = serializers.CharField(source="trip_type", required=False, default="OUTWARD")
    freightAmount = serializers.DecimalField(source="freight_amount", max_digits=18, decimal_places=2, required=False, default=0)
    loadingCharges = serializers.DecimalField(source="loading_charges", max_digits=18, decimal_places=2, required=False, default=0)
    unloadingCharges = serializers.DecimalField(source="unloading_charges", max_digits=18, decimal_places=2, required=False, default=0)
    otherCharges = serializers.DecimalField(source="other_charges", max_digits=18, decimal_places=2, required=False, default=0)
    totalCharges = serializers.DecimalField(source="total_charges", max_digits=18, decimal_places=2, read_only=True)
    paidBy = serializers.CharField(source="paid_by", required=False, default="Company")
    costAllocationTarget = serializers.CharField(source="cost_allocation_target", required=False, default="LOT")
    isCostAllocated = serializers.BooleanField(source="is_cost_allocated", read_only=True)
    materials = TripMaterialLineSerializer(many=True, required=False)
    signatures = serializers.JSONField(read_only=True)

    class Meta:
        model = Trip
        fields = [
            "id", "tripNumber", "tripDate", "fromLocation", "toLocation",
            "fromWarehouseId", "toWarehouseId", "isStockDeducted", "isStockReceived",
            "transporterId", "transporterName", "transporterPartyId",
            "vehicleId", "driverId", "proofFileId", "proofFileUrl", "proof_file", "vehicleNumber", "driverName", "driverMobile", "lrNumber", "lrDate",
            "tripType", "status", "freightAmount", "loadingCharges", "unloadingCharges",
            "otherCharges", "totalCharges", "paidBy", "costAllocationTarget",
            "isCostAllocated", "dispatch_time", "delivery_time", "remarks",
            "materials", "signatures", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "tripNumber", "totalCharges", "isCostAllocated", "isStockDeducted", "isStockReceived", "created_at", "updated_at"]


