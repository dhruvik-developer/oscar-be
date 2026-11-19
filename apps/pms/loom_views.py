"""
Loom & Weaving API ViewSets for Oscar PMS.
"""
from datetime import datetime, date
from decimal import Decimal
from django.db import transaction
from django.db.models import Q, Sum
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied

from apps.core.permissions import HasModulePermission
from apps.core.exceptions import NotFound, ValidationFailed, Conflict
from apps.core.viewsets import TenantModelViewSet
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
from .loom_serializers import (
    LoomCoordinatorSerializer,
    LoomFactorySerializer,
    LoomMachineSerializer,
    YarnMasterSerializer,
    LoomQualitySerializer,
    LoomBeamSerializer,
    DailyLoomProductionSerializer,
    FactoryDayLogSerializer,
    YarnBookingSerializer,
    YarnDeliverySerializer,
    ProductionProgramSerializer,
    FabricDispatchSerializer,
    LoomMachineBatchCreateSerializer,
    DailyLoomProductionBulkSaveSerializer,
    YarnDeliveryConfirmReceiptSerializer,
    LoomBeamLoadSerializer,
    LoomBeamAssignSpareSerializer,
)
from .loom_services import (
    get_factory_status,
    get_factory_yarn_stock,
    get_program_stats,
    seed_loom_demo_data,
)


def is_loom_admin_or_manager(user):
    """
    Returns True if the user is an Administrator, Admin, or General Manager / Owner / Management.
    Employees / Field Staff / Coordinators return False.
    """
    if not user or not user.is_authenticated:
        return False

    role_code = (getattr(getattr(user, "role", None), "code", "") or "").upper()
    role_name = (getattr(getattr(user, "role", None), "name", "") or "").lower()

    # Explicit employee / coordinator / staff roles are always restricted to assigned factories
    if role_code in ["PROD_STAFF", "PC", "PROD_COORD", "EMPLOYEE", "STAFF"]:
        return False
    if "employee" in role_name or "field staff" in role_name:
        return False

    # Superuser check (when not an explicit employee role)
    if user.is_superuser:
        return True

    # Administrator, Admin, General Manager, Owner, Management
    admin_codes = ["SYS_ADMIN", "ADMIN", "AD", "OWNER", "GM", "GENERAL_MANAGER", "MGMT"]
    if role_code in admin_codes:
        return True

    admin_keywords = ["administrator", "admin", "general manager", "owner", "management", "gm"]
    if any(kw in role_name for kw in admin_keywords):
        return True

    return False


def get_user_assigned_factories(user, client_id):
    """
    Returns QuerySet of LoomFactory that this user is permitted to see.
    Admins and General Managers can see all active factories.
    Employees / Field Staff can only see the factories assigned to them.
    """
    qs = LoomFactory.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if is_loom_admin_or_manager(user):
        return qs
    if not user or not getattr(user, "is_authenticated", False):
        return qs.none()

    emp = getattr(user, "employee", None)
    q = Q(coord__user=user) | Q(assigned_users=user)
    if emp:
        q |= Q(coord__employee=emp) | Q(assigned_employees=emp)
    user_email = getattr(user, "email", None)
    if user_email:
        q |= Q(coord__email__iexact=user_email)
    user_name = getattr(user, "name", None)
    if user_name:
        q |= Q(coord__name__iexact=user_name)

    return qs.filter(q).distinct()


class LoomCoordinatorViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = LoomCoordinator.objects.select_related("user", "employee").all()
    serializer_class = LoomCoordinatorSerializer


class LoomFactoryViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    serializer_class = LoomFactorySerializer

    def get_queryset(self):
        user = getattr(self.request, "user", None)
        client_id = getattr(self.request, "client_id", None)
        return (
            get_user_assigned_factories(user, client_id)
            .select_related("coord", "warehouse", "job_worker_party")
            .prefetch_related("assigned_employees", "assigned_users")
        )

    @transaction.atomic
    def perform_create(self, serializer):
        factory = super().perform_create(serializer)
        client_id = getattr(self.request, "client_id", None)
        # Ensure factory is synced as a Vendor in Parties master
        if not factory.job_worker_party:
            from apps.masters.models import Party
            party = Party.objects.filter(
                client_id=client_id,
                name__iexact=factory.name.strip(),
                deleted_at__isnull=True,
            ).first()
            if not party:
                user = getattr(self.request, "user", None)
                party = Party.objects.create(
                    client_id=client_id,
                    name=factory.name.strip(),
                    type="Vendor",
                    vendor_type="Manufacturing",
                    created_by=user if getattr(user, "is_authenticated", False) else None,
                )
            factory.job_worker_party = party
            factory.save(update_fields=["job_worker_party"])
        return factory

    @action(detail=True, methods=["get"])
    def status(self, request, pk=None):
        factory = self.get_object()
        st = get_factory_status(factory)
        return Response(st)

    @action(detail=True, methods=["get"])
    def yarn_stock(self, request, pk=None):
        factory = self.get_object()
        yarns = YarnMaster.objects.filter(client_id=request.client_id, deleted_at__isnull=True)
        results = [get_factory_yarn_stock(factory, y) for y in yarns]
        return Response(results)


class LoomMachineViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = LoomMachine.objects.all()
    serializer_class = LoomMachineSerializer
    filterset_fields = ["factory"]

    @action(detail=False, methods=["post"])
    def batch_create(self, request):
        ser = LoomMachineBatchCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        factory_id = data["factory_id"]
        from_no = data["from_no"]
        to_no = data["to_no"]
        reed = data.get("reed", 84)
        client_id = getattr(request, "client_id", None)

        if not LoomFactory.objects.filter(id=factory_id, client_id=client_id, deleted_at__isnull=True).exists():
            raise NotFound("Loom factory not found.")

        created = []
        for no in range(from_no, to_no + 1):
            m = LoomMachine.objects.filter(
                client_id=client_id,
                factory_id=factory_id,
                no=no,
            ).first()
            if m:
                m.reed = reed
                if m.deleted_at is not None:
                    m.deleted_at = None
                    m.deleted_by = None
                m.is_active = True
                m.save()
            else:
                m = LoomMachine.objects.create(
                    client_id=client_id,
                    factory_id=factory_id,
                    no=no,
                    reed=reed,
                    is_active=True,
                )
            created.append(m)
        return Response({"created": len(created)}, status=status.HTTP_201_CREATED)


class YarnMasterViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = YarnMaster.objects.all()
    serializer_class = YarnMasterSerializer


class LoomQualityViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = LoomQuality.objects.select_related("warp_yarn", "weft_yarn", "fabric_quality", "item").all()
    serializer_class = LoomQualitySerializer


class LoomBeamViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = LoomBeam.objects.all()
    serializer_class = LoomBeamSerializer
    filterset_fields = ["factory", "status", "machine"]

    @action(detail=True, methods=["post"])
    def load_to_loom(self, request, pk=None):
        beam = self.get_object()
        ser = LoomBeamLoadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        machine_id = ser.validated_data["machine_id"]

        with transaction.atomic():
            # Mark currently on-loom beam for this machine as Finished
            LoomBeam.objects.filter(
                factory=beam.factory, machine_id=machine_id, status="On loom"
            ).exclude(id=beam.id).update(status="Finished")

            beam.status = "On loom"
            beam.machine_id = machine_id
            beam.save(update_fields=["status", "machine", "updated_at"])

        return Response(LoomBeamSerializer(beam).data)

    @action(detail=True, methods=["post"])
    def assign_spare(self, request, pk=None):
        beam = self.get_object()
        ser = LoomBeamAssignSpareSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        machine_id = ser.validated_data.get("machine_id")
        beam.machine_id = machine_id or None
        beam.status = "Spare"
        beam.save(update_fields=["machine", "status", "updated_at"])
        return Response(LoomBeamSerializer(beam).data)

    @action(detail=True, methods=["post"])
    def cutoff(self, request, pk=None):
        beam = self.get_object()
        beam.status = "Finished"
        beam.save(update_fields=["status", "updated_at"])
        return Response({"status": "Finished"})


class DailyLoomProductionViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = DailyLoomProduction.objects.all()
    serializer_class = DailyLoomProductionSerializer
    filterset_fields = ["factory", "machine", "date", "beam"]

    @action(detail=False, methods=["get"])
    def factory_day(self, request):
        factory_id = request.query_params.get("factory")
        dt_str = request.query_params.get("date", date.today().isoformat())
        if not factory_id:
            raise ValidationFailed("factory query param required", field_errors={"factory": ["Required."]})

        try:
            factory = LoomFactory.objects.filter(id=factory_id, client_id=request.client_id, deleted_at__isnull=True).first()
        except (ValueError, Exception):
            raise ValidationFailed("Invalid factory ID.")
        if not factory:
            raise NotFound("Loom factory not found.")

        # Permission check: ensure user has permission for this factory
        allowed_factories = get_user_assigned_factories(request.user, request.client_id)
        if not allowed_factories.filter(id=factory.id).exists():
            raise PermissionDenied("You do not have access to this factory. Only assigned factories can be accessed.")

        try:
            parsed_date = date.fromisoformat(dt_str)
            dt_str = parsed_date.isoformat()
        except (ValueError, TypeError):
            raise ValidationFailed("Invalid date format. Expected YYYY-MM-DD.", field_errors={"date": ["Expected YYYY-MM-DD."]})

        day_log = FactoryDayLog.objects.filter(factory=factory, date=dt_str, deleted_at__isnull=True).first()
        power = float(day_log.power) if day_log else 0.0

        machines = LoomMachine.objects.filter(factory=factory, is_active=True, deleted_at__isnull=True).order_by("no")
        rows = []
        for m in machines:
            beam = LoomBeam.objects.filter(machine=m, status="On loom", deleted_at__isnull=True).first()
            prod = DailyLoomProduction.objects.filter(machine=m, date=dt_str, deleted_at__isnull=True).first()
            rem = float(beam.length) - float(DailyLoomProduction.objects.filter(beam=beam, deleted_at__isnull=True).aggregate(s=Sum("meters"))["s"] or 0) if beam else 0.0
            rows.append({
                "machine_id": str(m.id),
                "machine_no": m.no,
                "has_beam": bool(beam),
                "beam_id": str(beam.id) if beam else None,
                "beam_no": beam.beam_no if beam else "",
                "quality_name": beam.quality.name if beam else "",
                "rem_m": round(rem, 1),
                "meters": float(prod.meters) if prod else None,
                "rpm": float(prod.rpm) if prod else 650,
                "hours": float(prod.hours) if prod else 22,
                "stop": float(prod.stop) if prod else 0,
                "reason": prod.reason if prod else "",
                "damage": float(prod.damage) if prod else 0,
            })

        return Response({
            "factory_id": str(factory.id),
            "date": dt_str,
            "power": power,
            "rows": rows,
            "last_updated_at": day_log.updated_at.isoformat() if (day_log and day_log.updated_at) else None,
            "is_admin_or_manager": is_loom_admin_or_manager(request.user),
        })

    @action(detail=False, methods=["post"])
    def bulk_save(self, request):
        ser = DailyLoomProductionBulkSaveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        factory_id = data["factory_id"]
        dt = data["date"]
        dt_str = dt.isoformat()
        power = data["power"]
        rows = data["rows"]
        client_id = getattr(request, "client_id", None)

        if not LoomFactory.objects.filter(id=factory_id, client_id=client_id, deleted_at__isnull=True).exists():
            raise NotFound("Loom factory not found.")

        # Permission check: ensure user is allowed to submit for this factory
        allowed_factories = get_user_assigned_factories(request.user, client_id)
        if not allowed_factories.filter(id=factory_id).exists():
            raise PermissionDenied("You do not have permission to submit entries for this factory.")

        with transaction.atomic():
            # Update/create FactoryDayLog
            f_log, _ = FactoryDayLog.objects.get_or_create(
                client_id=client_id,
                factory_id=factory_id,
                date=dt_str,
                defaults={"power": power},
            )
            f_log.power = power
            f_log.save(update_fields=["power", "updated_at"])

            saved = 0
            for r in rows:
                m_val = r.get("meters")
                if m_val is None or m_val <= 0:
                    continue

                machine_id = r["machine_id"]
                beam_id = r.get("beam_id")
                if not beam_id:
                    active_beam = LoomBeam.objects.filter(machine_id=machine_id, status="On loom", deleted_at__isnull=True).first()
                    if active_beam:
                        beam_id = active_beam.id
                if not beam_id:
                    raise ValidationFailed(
                        f"Machine {machine_id} has no active beam assigned.",
                        field_errors={"rows": ["Active beam is required for production rows."]},
                    )
                prod, was_created = DailyLoomProduction.objects.get_or_create(
                    client_id=client_id,
                    factory_id=factory_id,
                    machine_id=machine_id,
                    date=dt_str,
                    defaults={
                        "beam_id": beam_id,
                        "meters": Decimal(str(m_val)),
                        "rpm": Decimal(str(r.get("rpm", 650))),
                        "hours": Decimal(str(r.get("hours", 22))),
                        "stop": Decimal(str(r.get("stop", 0))),
                        "reason": r.get("reason", ""),
                        "damage": Decimal(str(r.get("damage", 0))),
                    },
                )
                if not was_created:
                    prod.beam_id = beam_id
                    prod.meters = Decimal(str(m_val))
                    prod.rpm = Decimal(str(r.get("rpm", 650)))
                    prod.hours = Decimal(str(r.get("hours", 22)))
                    prod.stop = Decimal(str(r.get("stop", 0)))
                    prod.reason = r.get("reason", "")
                    prod.damage = Decimal(str(r.get("damage", 0)))
                    prod.save()
                saved += 1

                # Check if beam is exhausted
                if beam_id:
                    beam = LoomBeam.objects.filter(id=beam_id, client_id=client_id, deleted_at__isnull=True).first()
                    if beam:
                        tot_prod = DailyLoomProduction.objects.filter(beam=beam, deleted_at__isnull=True).aggregate(s=Sum("meters"))["s"] or 0
                        if float(tot_prod) >= float(beam.length):
                            beam.status = "Finished"
                            beam.save(update_fields=["status", "updated_at"])

        return Response({
            "saved_count": saved,
            "last_updated_at": f_log.updated_at.isoformat() if (f_log and f_log.updated_at) else None,
        }, status=status.HTTP_200_OK)


class YarnBookingViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = YarnBooking.objects.select_related("yarn", "supplier_party").all()
    serializer_class = YarnBookingSerializer

    def check_delete_allowed(self, instance):
        if instance.deliveries.filter(deleted_at__isnull=True, status="Received").exists():
            raise Conflict("Cannot delete booking with received yarn deliveries. You can edit or adjust the booking quantity instead.")

    def perform_destroy(self, instance):
        super().perform_destroy(instance)
        user = getattr(self.request, "user", None)
        user_obj = user if getattr(user, "is_authenticated", False) else None
        for d in instance.deliveries.filter(deleted_at__isnull=True, status="Scheduled"):
            d.soft_delete(user_obj)


class YarnDeliveryViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = YarnDelivery.objects.all()
    serializer_class = YarnDeliverySerializer
    filterset_fields = ["factory", "status"]

    @action(detail=True, methods=["post"])
    def confirm_receipt(self, request, pk=None):
        delivery = self.get_object()
        ser = YarnDeliveryConfirmReceiptSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        got_kgs = data["recd_kgs"]
        recd_date = data["recd_date"]
        client_id = getattr(request, "client_id", None)

        short = delivery.kgs - got_kgs
        with transaction.atomic():
            delivery.recd_kgs = got_kgs
            delivery.recd_date = recd_date
            delivery.status = "Received"
            delivery.save(update_fields=["recd_kgs", "recd_date", "status", "updated_at"])

            if short > 0:
                # Adjust current delivery schedule to actual received
                delivery.kgs = got_kgs
                delivery.save(update_fields=["kgs", "updated_at"])
                # Auto-create new scheduled delivery for the shortage balance
                YarnDelivery.objects.create(
                    client_id=client_id,
                    booking=delivery.booking,
                    factory=delivery.factory,
                    kgs=short,
                    rate=delivery.rate,
                    due=recd_date,
                    recd_kgs=Decimal("0"),
                    status="Scheduled",
                )
            elif short < 0:
                # Received MORE than scheduled (excess arrival): update scheduled kgs to actual received
                delivery.kgs = got_kgs
                delivery.save(update_fields=["kgs", "updated_at"])
                # Auto-expand booking total kg if total deliveries now exceed booked quantity
                booking = delivery.booking
                if booking:
                    total_sched = (
                        booking.deliveries.filter(deleted_at__isnull=True).aggregate(s=Sum("kgs"))["s"] or Decimal("0")
                    )
                    if total_sched > booking.kgs:
                        booking.kgs = total_sched
                        booking.save(update_fields=["kgs", "updated_at"])

        return Response(YarnDeliverySerializer(delivery).data)


class ProductionProgramViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = ProductionProgram.objects.all()
    serializer_class = ProductionProgramSerializer

    @action(detail=True, methods=["get"])
    def stats(self, request, pk=None):
        prog = self.get_object()
        return Response(get_program_stats(prog))


class FabricDispatchViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = FabricDispatch.objects.select_related("factory", "quality", "job_work_order", "dyeing_mill_party", "delivery_challan").all()
    serializer_class = FabricDispatchSerializer
    filterset_fields = ["factory", "quality"]

    @action(detail=False, methods=["get"])
    def inventory_summary(self, request):
        factory_id = request.query_params.get("factory")
        if not factory_id:
            raise ValidationFailed("factory query param required", field_errors={"factory": ["Required."]})

        try:
            factory = LoomFactory.objects.filter(id=factory_id, client_id=request.client_id, deleted_at__isnull=True).first()
        except (ValueError, Exception):
            raise ValidationFailed("Invalid factory ID.")
        if not factory:
            raise NotFound("Loom factory not found.")

        qualities = LoomQuality.objects.filter(client_id=request.client_id, deleted_at__isnull=True)
        results = []
        for q in qualities:
            prod_m = DailyLoomProduction.objects.filter(
                factory=factory, beam__quality=q, deleted_at__isnull=True
            ).aggregate(s=Sum("meters"))["s"] or 0
            
            dispatches = FabricDispatch.objects.filter(
                factory=factory, quality=q, deleted_at__isnull=True
            )
            godown = dispatches.filter(dest="Godown").aggregate(s=Sum("meters"))["s"] or 0
            mill = dispatches.filter(dest="Dyeing mill").aggregate(s=Sum("meters"))["s"] or 0
            other = dispatches.filter(dest="Other factory").aggregate(s=Sum("meters"))["s"] or 0
            total_sent = float(godown) + float(mill) + float(other)
            at_factory = max(0.0, float(prod_m) - total_sent)

            if prod_m or total_sent:
                results.append({
                    "quality_id": str(q.id),
                    "quality_name": q.name,
                    "produced_m": float(prod_m),
                    "godown_m": float(godown),
                    "mill_m": float(mill),
                    "other_m": float(other),
                    "at_factory_m": at_factory,
                })

        # Grey at dyeing mills
        mills = {}
        all_mill_outs = FabricDispatch.objects.filter(client_id=request.client_id, dest="Dyeing mill", deleted_at__isnull=True).select_related("quality")
        for o in all_mill_outs:
            k = f"{o.dest_name or '(unnamed)'}|{o.quality.name}"
            if k not in mills:
                mills[k] = {"mill_name": o.dest_name or "(unnamed)", "quality_name": o.quality.name, "meters": 0.0, "taka": 0}
            mills[k]["meters"] += float(o.meters)
            mills[k]["taka"] += int(o.taka)

        return Response({
            "factory_inventory": results,
            "grey_at_mills": list(mills.values()),
        })


class LoomDashboardOverviewView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_pms"]

    def get(self, request):
        factories = LoomFactory.objects.filter(client_id=request.client_id, is_active=True, deleted_at__isnull=True)
        factory_statuses = [get_factory_status(f) for f in factories]
        
        programs = ProductionProgram.objects.filter(client_id=request.client_id, deleted_at__isnull=True).exclude(status="Closed")
        programs_data = [ProductionProgramSerializer(p).data for p in programs]

        return Response({
            "factories": factory_statuses,
            "programs": programs_data,
        })


class LoomDemoDataView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["create_pms_project"]

    def post(self, request):
        client_id = getattr(request, "client_id", None)
        action = request.data.get("action", "reset")
        if action == "reset":
            with transaction.atomic():
                DailyLoomProduction.objects.filter(client_id=client_id).delete()
                FactoryDayLog.objects.filter(client_id=client_id).delete()
                FabricDispatch.objects.filter(client_id=client_id).delete()
                ProductionProgram.objects.filter(client_id=client_id).delete()
                YarnDelivery.objects.filter(client_id=client_id).delete()
                YarnBooking.objects.filter(client_id=client_id).delete()
                LoomBeam.objects.filter(client_id=client_id).delete()
                LoomMachine.objects.filter(client_id=client_id).delete()
                LoomQuality.objects.filter(client_id=client_id).delete()
                YarnMaster.objects.filter(client_id=client_id).delete()
                LoomFactory.objects.filter(client_id=client_id).delete()
                LoomCoordinator.objects.filter(client_id=client_id).delete()
                seed_loom_demo_data(client_id)
            return Response({"detail": "Demo dataset reseeded successfully"}, status=status.HTTP_200_OK)
        return Response({"detail": "Action not recognized"}, status=status.HTTP_400_BAD_REQUEST)
