"""Serializers for Loom & Weaving Manufacturing module."""
from rest_framework import serializers
from .models_loom import (
    LoomCoordinator,
    LoomFactory,
    LoomMachine,
    YarnMaster,
    LoomQuality,
    LoomBeam,
    DailyLoomProduction,
    FactoryDayLog,
    YarnBooking,
    YarnDelivery,
    ProductionProgram,
    FabricDispatch,
)


class LoomCoordinatorSerializer(serializers.ModelSerializer):
    name = serializers.CharField(required=False, allow_blank=True, default="")
    employee_details = serializers.SerializerMethodField()
    user_name = serializers.CharField(source="user.name", read_only=True, default="")

    class Meta:
        model = LoomCoordinator
        fields = [
            "id", "name", "phone", "email", "user", "user_name",
            "employee", "employee_details",
        ]

    def get_employee_details(self, obj):
        if not obj.employee:
            return None
        code = getattr(obj.employee, "code", None) or getattr(obj.employee, "employee_code", None)
        desig = getattr(getattr(obj.employee, "designation", None), "name", None)
        return {
            "id": str(obj.employee.id),
            "name": obj.employee.name,
            "code": code or "",
            "designation": desig or "",
            "phone": obj.employee.phone or "",
            "email": obj.employee.email or "",
        }

    def validate(self, attrs):
        emp = attrs.get("employee")
        if emp:
            if not attrs.get("name"):
                attrs["name"] = emp.name
            if not attrs.get("phone") and emp.phone:
                attrs["phone"] = emp.phone
            if not attrs.get("email") and emp.email:
                attrs["email"] = emp.email
            if not attrs.get("user"):
                from apps.accounts.models import User
                user_match = User.objects.filter(employee=emp, deleted_at__isnull=True).first()
                if not user_match and emp.email:
                    user_match = User.objects.filter(email__iexact=emp.email, deleted_at__isnull=True).first()
                if user_match:
                    attrs["user"] = user_match
        return attrs


class LoomMachineSerializer(serializers.ModelSerializer):
    factory_name = serializers.CharField(source="factory.name", read_only=True)

    class Meta:
        model = LoomMachine
        fields = ["id", "factory", "factory_name", "no", "reed", "is_active"]


class LoomFactorySerializer(serializers.ModelSerializer):
    coord_name = serializers.CharField(source="coord.name", read_only=True, default="")
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True, default="")
    job_worker_party_name = serializers.CharField(source="job_worker_party.name", read_only=True, default="")
    warehouse_details = serializers.SerializerMethodField()
    job_worker_party_details = serializers.SerializerMethodField()
    machines = serializers.SerializerMethodField()
    machines_count = serializers.SerializerMethodField()
    assigned_employee_names = serializers.SerializerMethodField()
    assigned_user_names = serializers.SerializerMethodField()

    class Meta:
        model = LoomFactory
        fields = [
            "id", "name", "code", "coord", "coord_name", "address",
            "factory_type", "warehouse", "warehouse_name", "warehouse_details",
            "job_worker_party", "job_worker_party_name", "job_worker_party_details",
            "is_active", "machines", "machines_count",
            "assigned_employees", "assigned_users",
            "assigned_employee_names", "assigned_user_names",
        ]

    def get_assigned_employee_names(self, obj):
        return [e.name for e in obj.assigned_employees.all() if e.name]

    def get_assigned_user_names(self, obj):
        return [u.name for u in obj.assigned_users.all() if u.name]

    def get_warehouse_details(self, obj):
        if not obj.warehouse:
            return None
        return {
            "id": str(obj.warehouse.id),
            "name": obj.warehouse.name,
            "code": getattr(obj.warehouse, "code", ""),
            "type": getattr(obj.warehouse, "type", ""),
        }

    def get_job_worker_party_details(self, obj):
        if not obj.job_worker_party:
            return None
        return {
            "id": str(obj.job_worker_party.id),
            "name": obj.job_worker_party.name,
            "code": getattr(obj.job_worker_party, "code", ""),
            "phone": getattr(obj.job_worker_party, "phone", ""),
        }

    def get_machines(self, obj):
        active_machines = obj.machines.filter(deleted_at__isnull=True).order_by("no")
        return LoomMachineSerializer(active_machines, many=True).data

    def get_machines_count(self, obj):
        return obj.machines.filter(deleted_at__isnull=True).count()


class YarnMasterSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True, default="")
    item_sku = serializers.CharField(source="item.sku", read_only=True, default="")
    item_name = serializers.CharField(source="item.name", read_only=True, default="")

    class Meta:
        model = YarnMaster
        fields = [
            "id", "name", "denier", "kg_per_bag", "rate", "note",
            "item", "item_sku", "item_name", "category", "category_name",
        ]


class LoomQualitySerializer(serializers.ModelSerializer):
    name = serializers.CharField(required=False, allow_blank=True, default="")
    warp_yarn_name = serializers.CharField(source="warp_yarn.name", read_only=True)
    weft_yarn_name = serializers.CharField(source="weft_yarn.name", read_only=True)
    fabric_quality_name = serializers.CharField(source="fabric_quality.name", read_only=True, default="")
    item_name = serializers.CharField(source="item.name", read_only=True, default="")
    item_sku = serializers.CharField(source="item.sku", read_only=True, default="")
    category_name = serializers.CharField(source="category.name", read_only=True, default="")
    fabric_quality_details = serializers.SerializerMethodField()
    item_details = serializers.SerializerMethodField()
    ppm = serializers.FloatField(read_only=True)
    warp_kg_m = serializers.FloatField(read_only=True)
    weft_kg_m = serializers.FloatField(read_only=True)
    glm = serializers.FloatField(read_only=True)
    gsm = serializers.FloatField(read_only=True)
    warp_cost = serializers.FloatField(read_only=True)
    weft_cost = serializers.FloatField(read_only=True)
    job_cost = serializers.FloatField(read_only=True)
    total_cost = serializers.FloatField(read_only=True)

    class Meta:
        model = LoomQuality
        fields = [
            "id", "name", "fabric_quality", "fabric_quality_name", "fabric_quality_details",
            "item", "item_sku", "item_name", "item_details", "category", "category_name",
            "warp_yarn", "warp_yarn_name", "ends", "crimp",
            "weft_yarn", "weft_yarn_name", "reed", "reed_w", "ppi", "wastage",
            "pick_charge", "ppm", "warp_kg_m", "weft_kg_m", "glm", "gsm",
            "warp_cost", "weft_cost", "job_cost", "total_cost",
        ]

    def get_fabric_quality_details(self, obj):
        if not obj.fabric_quality:
            return None
        fq = obj.fabric_quality
        return {
            "id": str(fq.id),
            "name": fq.name,
            "code": getattr(fq, "code", ""),
            "reed": getattr(fq, "reed", None),
            "pick": getattr(fq, "pick", None),
            "width": float(fq.width) if getattr(fq, "width", None) else None,
            "gsm": float(fq.gsm) if getattr(fq, "gsm", None) else None,
        }

    def get_item_details(self, obj):
        if not obj.item:
            return None
        return {
            "id": str(obj.item.id),
            "name": obj.item.name,
            "code": getattr(obj.item, "code", ""),
        }

    def validate(self, attrs):
        fq = attrs.get("fabric_quality")
        if fq and not attrs.get("name"):
            attrs["name"] = fq.name
        return attrs


class LoomBeamSerializer(serializers.ModelSerializer):
    factory_name = serializers.CharField(source="factory.name", read_only=True)
    quality_name = serializers.CharField(source="quality.name", read_only=True)
    machine_no = serializers.IntegerField(source="machine.no", read_only=True, default=None)
    produced_meters = serializers.SerializerMethodField()
    remaining_meters = serializers.SerializerMethodField()
    warp_kg = serializers.SerializerMethodField()

    class Meta:
        model = LoomBeam
        fields = [
            "id", "beam_no", "factory", "factory_name", "quality", "quality_name",
            "length", "date", "status", "machine", "machine_no",
            "produced_meters", "remaining_meters", "warp_kg",
        ]

    def get_produced_meters(self, obj):
        from django.db.models import Sum
        total = obj.daily_productions.filter(deleted_at__isnull=True).aggregate(s=Sum("meters"))["s"] or 0
        return float(total)

    def get_remaining_meters(self, obj):
        prod = self.get_produced_meters(obj)
        return max(0.0, float(obj.length) - prod)

    def get_warp_kg(self, obj):
        return obj.quality.warp_kg_m * float(obj.length) if obj.quality else 0.0


class DailyLoomProductionSerializer(serializers.ModelSerializer):
    factory_name = serializers.CharField(source="factory.name", read_only=True)
    machine_no = serializers.IntegerField(source="machine.no", read_only=True)
    beam_no = serializers.CharField(source="beam.beam_no", read_only=True)
    quality_name = serializers.CharField(source="beam.quality.name", read_only=True, default="")
    created_by_name = serializers.CharField(source="created_by.name", read_only=True, default="")
    updated_by_name = serializers.CharField(source="updated_by.name", read_only=True, default="")

    class Meta:
        model = DailyLoomProduction
        fields = [
            "id", "factory", "factory_name", "machine", "machine_no", "beam",
            "beam_no", "quality_name", "date", "meters", "rpm", "hours",
            "stop", "reason", "damage", "created_by_name", "updated_by_name",
            "created_at", "updated_at",
        ]


class FactoryDayLogSerializer(serializers.ModelSerializer):
    factory_name = serializers.CharField(source="factory.name", read_only=True)

    class Meta:
        model = FactoryDayLog
        fields = ["id", "factory", "factory_name", "date", "power"]


class YarnDeliverySerializer(serializers.ModelSerializer):
    supplier = serializers.CharField(source="booking.supplier", read_only=True)
    yarn_name = serializers.CharField(source="booking.yarn.name", read_only=True)
    factory_name = serializers.CharField(source="factory.name", read_only=True)

    class Meta:
        model = YarnDelivery
        fields = [
            "id", "booking", "supplier", "yarn_name", "factory", "factory_name",
            "kgs", "rate", "due", "recd_kgs", "recd_date", "status",
        ]


class YarnBookingSerializer(serializers.ModelSerializer):
    supplier = serializers.CharField(required=False, allow_blank=True, default="")
    yarn_name = serializers.CharField(source="yarn.name", read_only=True)
    supplier_party_name = serializers.CharField(source="supplier_party.name", read_only=True, default="")
    supplier_party_details = serializers.SerializerMethodField()
    deliveries = serializers.SerializerMethodField()
    scheduled_kgs = serializers.SerializerMethodField()
    delivered_kgs = serializers.SerializerMethodField()
    unscheduled_kgs = serializers.SerializerMethodField()
    pending_kgs = serializers.SerializerMethodField()

    class Meta:
        model = YarnBooking
        fields = [
            "id", "supplier", "supplier_party", "supplier_party_name",
            "supplier_party_details", "yarn", "yarn_name", "kgs", "rate", "date",
            "note", "deliveries", "scheduled_kgs", "delivered_kgs",
            "unscheduled_kgs", "pending_kgs",
        ]

    def get_supplier_party_details(self, obj):
        if not obj.supplier_party:
            return None
        party = obj.supplier_party
        city = ""
        if isinstance(getattr(party, "billing_address", None), dict):
            city = party.billing_address.get("city", "")
        return {
            "id": str(party.id),
            "name": party.name,
            "code": getattr(party, "code", "") or "",
            "gstin": getattr(party, "gstin", "") or "",
            "phone": getattr(party, "phone", "") or "",
            "city": city,
            "type": getattr(party, "type", "") or "",
        }

    def validate(self, attrs):
        sp = attrs.get("supplier_party")
        if sp and not attrs.get("supplier"):
            attrs["supplier"] = sp.name
        return attrs

    def get_deliveries(self, obj):
        active_deliveries = obj.deliveries.filter(deleted_at__isnull=True).order_by("due", "-created_at")
        return YarnDeliverySerializer(active_deliveries, many=True).data

    def get_scheduled_kgs(self, obj):
        from django.db.models import Sum
        return float(obj.deliveries.filter(deleted_at__isnull=True).aggregate(s=Sum("kgs"))["s"] or 0)

    def get_delivered_kgs(self, obj):
        from django.db.models import Sum
        return float(obj.deliveries.filter(deleted_at__isnull=True, status="Received").aggregate(s=Sum("recd_kgs"))["s"] or 0)

    def get_unscheduled_kgs(self, obj):
        return max(0.0, float(obj.kgs) - self.get_scheduled_kgs(obj))

    def get_pending_kgs(self, obj):
        return max(0.0, self.get_scheduled_kgs(obj) - self.get_delivered_kgs(obj))


class ProductionProgramSerializer(serializers.ModelSerializer):
    factory_name = serializers.CharField(source="factory.name", read_only=True)
    quality_name = serializers.CharField(source="quality.name", read_only=True)
    stats = serializers.SerializerMethodField()

    class Meta:
        model = ProductionProgram
        fields = [
            "id", "name", "factory", "factory_name", "quality", "quality_name",
            "target", "start", "deadline", "t_rpm", "t_eff", "t_power",
            "status", "stats",
        ]

    def get_stats(self, obj):
        from .loom_services import get_program_stats
        return get_program_stats(obj)


class FabricDispatchSerializer(serializers.ModelSerializer):
    factory_name = serializers.CharField(source="factory.name", read_only=True)
    quality_name = serializers.CharField(source="quality.name", read_only=True)
    job_work_order_number = serializers.CharField(source="job_work_order.order_number", read_only=True, default="")
    dyeing_mill_party_name = serializers.CharField(source="dyeing_mill_party.name", read_only=True, default="")
    delivery_challan_number = serializers.CharField(source="delivery_challan.challan_number", read_only=True, default="")
    transporter_party_name = serializers.CharField(source="transporter_party.name", read_only=True, default="")
    batch_code = serializers.CharField(source="batch.batch_code", read_only=True, default="")
    job_work_order_details = serializers.SerializerMethodField()
    dyeing_mill_party_details = serializers.SerializerMethodField()
    delivery_challan_details = serializers.SerializerMethodField()

    class Meta:
        model = FabricDispatch
        fields = [
            "id", "factory", "factory_name", "quality", "quality_name",
            "date", "taka", "meters", "dest", "dest_name",
            "job_work_order", "job_work_order_number", "job_work_order_details",
            "dyeing_mill_party", "dyeing_mill_party_name", "dyeing_mill_party_details",
            "delivery_challan", "delivery_challan_number", "delivery_challan_details",
            "batch", "batch_code", "target_location",
            "challan", "vehicle",
            "weaving_rate", "yarn_rate", "dyeing_rate", "logistic_rate",
            "transporter_party", "transporter_party_name", "landed_cost_rate",
            "mill_inward_status", "mill_inward_date", "mill_inward_meters",
            "mill_inward_taka", "mill_inward_challan", "history_passport",
        ]

    def get_job_work_order_details(self, obj):
        if not obj.job_work_order:
            return None
        jwo = obj.job_work_order
        return {
            "id": str(jwo.id),
            "order_number": getattr(jwo, "order_number", ""),
            "vendor_name": getattr(getattr(jwo, "vendor", None), "name", ""),
            "status": getattr(jwo, "status", ""),
        }

    def get_dyeing_mill_party_details(self, obj):
        if not obj.dyeing_mill_party:
            return None
        p = obj.dyeing_mill_party
        return {
            "id": str(p.id),
            "name": p.name,
            "code": getattr(p, "code", "") or "",
            "gstin": getattr(p, "gstin", "") or "",
        }

    def get_delivery_challan_details(self, obj):
        if not obj.delivery_challan:
            return None
        dc = obj.delivery_challan
        return {
            "id": str(dc.id),
            "challan_number": getattr(dc, "challan_number", "") or "",
            "status": getattr(dc, "status", "") or "",
        }

    def validate(self, attrs):
        if attrs.get("dest") == "Dyeing mill":
            dp = attrs.get("dyeing_mill_party")
            if dp and not attrs.get("dest_name"):
                attrs["dest_name"] = dp.name
            jwo = attrs.get("job_work_order")
            if jwo and not attrs.get("dyeing_mill_party") and getattr(jwo, "vendor", None):
                attrs["dyeing_mill_party"] = jwo.vendor
                if not attrs.get("dest_name"):
                    attrs["dest_name"] = jwo.vendor.name
        return attrs


class LoomMachineBatchCreateSerializer(serializers.Serializer):
    factory = serializers.UUIDField(required=False)
    factory_id = serializers.UUIDField(required=False)
    from_no = serializers.IntegerField(min_value=1)
    to_no = serializers.IntegerField(min_value=1)
    reed = serializers.IntegerField(min_value=1, required=False, default=84)

    def validate(self, attrs):
        fid = attrs.get("factory") or attrs.get("factory_id")
        if not fid:
            raise serializers.ValidationError({"factory": ["This field is required."]})
        attrs["factory_id"] = fid
        if attrs["from_no"] > attrs["to_no"]:
            raise serializers.ValidationError({"to_no": ["to_no must be greater than or equal to from_no."]})
        return attrs


class DailyLoomProductionBulkSaveRowSerializer(serializers.Serializer):
    machine_id = serializers.UUIDField()
    beam_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    meters = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    rpm = serializers.DecimalField(max_digits=8, decimal_places=2, required=False, default=650)
    hours = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=22)
    stop = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, default=0)
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    damage = serializers.DecimalField(max_digits=8, decimal_places=2, required=False, default=0)

    def validate_beam_id(self, value):
        if not value or str(value).strip() == "" or str(value).lower() in ("null", "none"):
            return None
        import uuid
        try:
            return uuid.UUID(str(value))
        except (ValueError, AttributeError):
            raise serializers.ValidationError("Must be a valid UUID.")


class DailyLoomProductionBulkSaveSerializer(serializers.Serializer):
    factory_id = serializers.UUIDField(required=False)
    factory = serializers.UUIDField(required=False)
    date = serializers.DateField()
    power = serializers.DecimalField(max_digits=8, decimal_places=2)
    rows = DailyLoomProductionBulkSaveRowSerializer(many=True)

    def validate(self, attrs):
        fid = attrs.get("factory_id") or attrs.get("factory")
        if not fid:
            raise serializers.ValidationError({"factory_id": ["This field is required."]})
        attrs["factory_id"] = fid
        return attrs


class YarnDeliveryConfirmReceiptSerializer(serializers.Serializer):
    recd_kgs = serializers.DecimalField(max_digits=12, decimal_places=2)
    recd_date = serializers.DateField()


class LoomBeamLoadSerializer(serializers.Serializer):
    machine_id = serializers.UUIDField()


class LoomBeamAssignSpareSerializer(serializers.Serializer):
    machine_id = serializers.UUIDField(required=False, allow_null=True)
