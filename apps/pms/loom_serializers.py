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
    class Meta:
        model = LoomCoordinator
        fields = ["id", "name", "phone", "email", "user"]


class LoomMachineSerializer(serializers.ModelSerializer):
    factory_name = serializers.CharField(source="factory.name", read_only=True)

    class Meta:
        model = LoomMachine
        fields = ["id", "factory", "factory_name", "no", "reed", "is_active"]


class LoomFactorySerializer(serializers.ModelSerializer):
    coord_name = serializers.CharField(source="coord.name", read_only=True, default="")
    machines = LoomMachineSerializer(many=True, read_only=True)
    machines_count = serializers.SerializerMethodField()

    class Meta:
        model = LoomFactory
        fields = ["id", "name", "code", "coord", "coord_name", "address", "is_active", "machines", "machines_count"]

    def get_machines_count(self, obj):
        return obj.machines.filter(deleted_at__isnull=True).count()


class YarnMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = YarnMaster
        fields = ["id", "name", "denier", "kg_per_bag", "rate", "note"]


class LoomQualitySerializer(serializers.ModelSerializer):
    warp_yarn_name = serializers.CharField(source="warp_yarn.name", read_only=True)
    weft_yarn_name = serializers.CharField(source="weft_yarn.name", read_only=True)
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
            "id", "name", "warp_yarn", "warp_yarn_name", "ends", "crimp",
            "weft_yarn", "weft_yarn_name", "reed", "reed_w", "ppi", "wastage",
            "pick_charge", "ppm", "warp_kg_m", "weft_kg_m", "glm", "gsm",
            "warp_cost", "weft_cost", "job_cost", "total_cost",
        ]


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

    class Meta:
        model = DailyLoomProduction
        fields = [
            "id", "factory", "factory_name", "machine", "machine_no", "beam",
            "beam_no", "quality_name", "date", "meters", "rpm", "hours",
            "stop", "reason", "damage",
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
    yarn_name = serializers.CharField(source="yarn.name", read_only=True)
    deliveries = YarnDeliverySerializer(many=True, read_only=True)
    scheduled_kgs = serializers.SerializerMethodField()
    delivered_kgs = serializers.SerializerMethodField()
    unscheduled_kgs = serializers.SerializerMethodField()
    pending_kgs = serializers.SerializerMethodField()

    class Meta:
        model = YarnBooking
        fields = [
            "id", "supplier", "yarn", "yarn_name", "kgs", "rate", "date",
            "note", "deliveries", "scheduled_kgs", "delivered_kgs",
            "unscheduled_kgs", "pending_kgs",
        ]

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

    class Meta:
        model = FabricDispatch
        fields = [
            "id", "factory", "factory_name", "quality", "quality_name",
            "date", "taka", "meters", "dest", "dest_name", "challan", "vehicle",
        ]
