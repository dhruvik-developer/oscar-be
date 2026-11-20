"""
Loom & Weaving API ViewSets for Oscar PMS.
"""
import logging
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
from .inventory_bridge import (
    sync_yarn_delivery_receipt_to_inventory,
    sync_beam_warp_consumption_to_inventory,
    sync_daily_weft_consumption_to_inventory,
    sync_fabric_dispatch_to_inventory,
    sync_yarn_master_to_inventory,
    sync_loom_quality_to_inventory,
    sync_daily_loom_production_to_pi,
    record_mill_inward_with_landed_cost,
)

logger = logging.getLogger(__name__)
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
    queryset = LoomFactory.objects.all()
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
        user = getattr(self.request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            factory.assigned_users.add(user)
            if getattr(user, "employee", None):
                factory.assigned_employees.add(user.employee)
        # Ensure factory is synced as a Vendor in Parties master
        if not factory.job_worker_party:
            from apps.masters.models import Party
            party = Party.objects.filter(
                client_id=client_id,
                name__iexact=factory.name.strip(),
                deleted_at__isnull=True,
            ).first()
            if not party:
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
    filter_map = {"factory": "factory_id"}

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
    queryset = YarnMaster.objects.select_related("item", "category").all()
    serializer_class = YarnMasterSerializer

    def perform_create(self, serializer):
        # [OLD CODE - COMMENTED OUT]: yarn = serializer.save() (did not set client_id via TenantModelViewSet)
        # yarn = serializer.save()
        yarn = super().perform_create(serializer)
        try:
            sync_yarn_master_to_inventory(yarn, category_id=self.request.data.get("category"))
            serializer.instance.refresh_from_db()
        except Exception as exc:
            logger.warning("Failed to sync yarn to inventory item: %s", exc)
        return yarn

    def perform_update(self, serializer):
        # [OLD CODE - COMMENTED OUT]: yarn = serializer.save()
        # yarn = serializer.save()
        yarn = super().perform_update(serializer)
        try:
            sync_yarn_master_to_inventory(yarn, category_id=self.request.data.get("category"))
            serializer.instance.refresh_from_db()
        except Exception as exc:
            logger.warning("Failed to sync yarn to inventory item: %s", exc)
        return yarn


class LoomQualityViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = LoomQuality.objects.select_related("warp_yarn", "weft_yarn", "fabric_quality", "item", "category").all()
    serializer_class = LoomQualitySerializer

    def perform_create(self, serializer):
        # [OLD CODE - COMMENTED OUT]: quality = serializer.save()
        # quality = serializer.save()
        quality = super().perform_create(serializer)
        try:
            sync_loom_quality_to_inventory(
                quality,
                category_id=self.request.data.get("category"),
                fabric_quality_id=self.request.data.get("fabric_quality"),
            )
            serializer.instance.refresh_from_db()
        except Exception as exc:
            logger.warning("Failed to sync quality to inventory: %s", exc)
        return quality

    def perform_update(self, serializer):
        # [OLD CODE - COMMENTED OUT]: quality = serializer.save()
        # quality = serializer.save()
        quality = super().perform_update(serializer)
        try:
            sync_loom_quality_to_inventory(
                quality,
                category_id=self.request.data.get("category"),
                fabric_quality_id=self.request.data.get("fabric_quality"),
            )
            serializer.instance.refresh_from_db()
        except Exception as exc:
            logger.warning("Failed to sync quality to inventory: %s", exc)
        return quality


class LoomBeamViewSet(TenantModelViewSet):
    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_pms"], "write": ["create_pms_project"]}
    queryset = LoomBeam.objects.all()
    serializer_class = LoomBeamSerializer
    filterset_fields = ["factory", "status", "machine"]
    filter_map = {
        "factory": "factory_id",
        "status": "status",
        "machine": "machine_id",
    }

    def perform_create(self, serializer):
        beam = super().perform_create(serializer)
        return beam

    @action(detail=True, methods=["post"])
    def load_to_loom(self, request, pk=None):
        beam = self.get_object()
        ser = LoomBeamLoadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        machine_id = ser.validated_data["machine_id"]

        with transaction.atomic():
            # Check if this machine currently has another active beam on loom
            active_on_loom = LoomBeam.objects.filter(
                factory=beam.factory, machine_id=machine_id, status="On loom", deleted_at__isnull=True
            ).exclude(id=beam.id).first()
            if active_on_loom:
                raise Conflict(
                    f"Machine already has active beam '{active_on_loom.beam_no}' on loom. Cut off or finish the active beam before loading a new one."
                )

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
    queryset = DailyLoomProduction.objects.select_related("factory", "machine", "beam", "beam__quality", "created_by", "updated_by").all()
    serializer_class = DailyLoomProductionSerializer
    filterset_fields = ["factory", "machine", "date", "beam"]
    filter_map = {
        "factory": "factory_id",
        "machine": "machine_id",
        "date": "date",
        "beam": "beam_id",
    }
    search_fields = ["machine__no", "beam__beam_no", "beam__quality__name", "reason"]
    ordering_fields = ["date", "machine__no", "created_at", "meters"]
    ordering = ["-date", "machine__no"]

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
            active_beam = LoomBeam.objects.filter(machine=m, status="On loom", deleted_at__isnull=True).first()
            prod = DailyLoomProduction.objects.filter(machine=m, date=dt_str, deleted_at__isnull=True).first()
            beam = active_beam or (prod.beam if prod and prod.beam else None)
            rem = float(beam.length) - float(DailyLoomProduction.objects.filter(beam=beam, deleted_at__isnull=True).aggregate(s=Sum("meters"))["s"] or 0) if beam else 0.0
            has_beam = bool(active_beam or (prod and prod.beam))
            rows.append({
                "machine_id": str(m.id),
                "machine_no": m.no,
                "has_beam": has_beam,
                "beam_id": str(beam.id) if beam else None,
                "beam_no": beam.beam_no if beam else "",
                "beam_status": beam.status if beam else None,
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
            "last_updated_by": day_log.updated_by.name if (day_log and day_log.updated_by) else (day_log.created_by.name if (day_log and day_log.created_by) else None),
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
            user_obj = request.user if getattr(request.user, "is_authenticated", False) else None
            # Update/create FactoryDayLog
            f_log, _ = FactoryDayLog.objects.get_or_create(
                client_id=client_id,
                factory_id=factory_id,
                date=dt_str,
                defaults={"power": power, "created_by": user_obj},
            )
            f_log.power = power
            f_log.updated_by = user_obj
            f_log.save(update_fields=["power", "updated_by", "updated_at"])

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
                    else:
                        existing_prod = DailyLoomProduction.objects.filter(
                            client_id=client_id,
                            factory_id=factory_id,
                            machine_id=machine_id,
                            date=dt_str,
                            deleted_at__isnull=True,
                        ).first()
                        if existing_prod and existing_prod.beam_id:
                            beam_id = existing_prod.beam_id
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
                        "created_by": user_obj,
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
                    prod.updated_by = user_obj
                    prod.save()
                saved += 1

                try:
                    sync_daily_loom_production_to_pi(prod, user=request.user)
                except Exception as exc:
                    logger.warning("Failed to auto-sync PI progress for prod %s: %s", prod.id, exc)

                # Check if beam is exhausted
                if beam_id:
                    beam = LoomBeam.objects.filter(id=beam_id, client_id=client_id, deleted_at__isnull=True).first()
                    if beam:
                        tot_prod = DailyLoomProduction.objects.filter(beam=beam, deleted_at__isnull=True).aggregate(s=Sum("meters"))["s"] or 0
                        if float(tot_prod) >= float(beam.length):
                            beam.status = "Finished"
                            beam.save(update_fields=["status", "updated_at"])

                # Auto-upgrade matching ProductionProgram progress & status if target is finished
                if prod.beam and getattr(prod.beam, "quality_id", None):
                    matching_progs = ProductionProgram.objects.filter(
                        client_id=client_id,
                        factory_id=factory_id,
                        quality_id=prod.beam.quality_id,
                        deleted_at__isnull=True,
                    ).exclude(status__in=["Done", "Closed"])

                    for prog in matching_progs:
                        prog_prods = DailyLoomProduction.objects.filter(
                            client_id=client_id,
                            factory_id=prog.factory_id,
                            beam__quality_id=prog.quality_id,
                            date__gte=prog.start,
                            date__lte=prog.deadline,
                            deleted_at__isnull=True,
                        )
                        prog_tot = float(prog_prods.aggregate(s=Sum("meters"))["s"] or 0)
                        if prog_tot >= float(prog.target):
                            prog.status = "Done"
                            prog.updated_by = user_obj
                            prog.save(update_fields=["status", "updated_by", "updated_at"])
                        elif prog.status == "Planned" and prog_tot > 0:
                            prog.status = "Running"
                            prog.updated_by = user_obj
                            prog.save(update_fields=["status", "updated_by", "updated_at"])

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
    filter_map = {"factory": "factory_id", "status": "status"}

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
    filter_map = {"factory": "factory_id", "quality": "quality_id", "status": "status"}

    def perform_update(self, serializer):
        instance = serializer.instance
        user = getattr(self.request, "user", None)
        is_admin = is_loom_admin_or_manager(user)

        new_status = serializer.validated_data.get("status", instance.status)
        # If current status is Done or Closed, only Administrator / GM can reopen or modify it
        if instance.status in ["Done", "Closed"]:
            if not is_admin:
                raise PermissionDenied("Only an Administrator or General Manager can modify or reopen a completed/done production program.")
        elif new_status in ["Done", "Closed"] and not is_admin:
            raise PermissionDenied("Only an Administrator or General Manager can finalize or close a production program.")

        user_obj = user if getattr(user, "is_authenticated", False) else None
        serializer.save(updated_by=user_obj)

    def perform_destroy(self, instance):
        user = getattr(self.request, "user", None)
        if not is_loom_admin_or_manager(user):
            raise PermissionDenied("Only an Administrator or General Manager can delete a production program.")
        super().perform_destroy(instance)

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
    filter_map = {"factory": "factory_id", "quality": "quality_id"}

    def perform_create(self, serializer):
        return super().perform_create(serializer)

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
        mill_dispatches_list = []
        all_mill_outs = FabricDispatch.objects.filter(
            client_id=request.client_id, dest="Dyeing mill", deleted_at__isnull=True
        ).select_related("quality", "dyeing_mill_party", "factory", "transporter_party").order_by("-date", "-created_at")
        
        for o in all_mill_outs:
            mill_name = getattr(o.dyeing_mill_party, "name", None) or o.dest_name or "(unnamed)"
            k = f"{mill_name}|{o.quality.name}"
            pending_m = max(0.0, float(o.meters) - float(o.mill_inward_meters or 0)) if o.mill_inward_status == "Received" else float(o.meters)
            
            if k not in mills:
                mills[k] = {"mill_name": mill_name, "quality_name": o.quality.name, "meters": 0.0, "taka": 0}
            mills[k]["meters"] += pending_m
            mills[k]["taka"] += int(o.taka)

            mill_dispatches_list.append({
                "id": str(o.id),
                "date": str(o.date),
                "factory_name": getattr(o.factory, "name", "Factory"),
                "quality_name": o.quality.name,
                "mill_name": mill_name,
                "mill_party_id": str(o.dyeing_mill_party.id) if o.dyeing_mill_party else None,
                "meters": float(o.meters),
                "taka": int(o.taka),
                "challan": o.challan or "-",
                "vehicle": o.vehicle or "-",
                "weaving_rate": float(o.weaving_rate or 0),
                "yarn_rate": float(o.yarn_rate or 0),
                "dyeing_rate": float(o.dyeing_rate or 0),
                "logistic_rate": float(o.logistic_rate or 0),
                "landed_cost_rate": float(o.landed_cost_rate or 0),
                "mill_inward_status": o.mill_inward_status or "Pending",
                "mill_inward_date": str(o.mill_inward_date) if o.mill_inward_date else None,
                "mill_inward_meters": float(o.mill_inward_meters or 0),
                "mill_inward_taka": int(o.mill_inward_taka or 0),
                "mill_inward_challan": o.mill_inward_challan or "",
                "has_passport": bool(o.history_passport),
            })

        return Response({
            "factory_inventory": results,
            "grey_at_mills": list(mills.values()),
            "mill_dispatches": mill_dispatches_list,
        })

    @action(detail=True, methods=["post"], url_path="receive-from-mill")
    def receive_from_mill(self, request, pk=None):
        """
        Record inward of dyed/finished fabric returning from external processing mill:
        1. Computes composite landed rate: Weaving + Yarn + Dyeing + Logistics.
        2. Adds to Inventory (creates Batch & LotTaka rolls with LotCostEntry breakdown).
        3. Generates complete Fabric Traceability Passport.
        4. Auto-posts balanced double-entry JournalEntry in Accounts.
        """
        dispatch = self.get_object()
        data = request.data or {}

        inward_meters = data.get("inward_meters") or data.get("meters")
        if not inward_meters:
            raise ValidationFailed("inward_meters is required.", field_errors={"inward_meters": ["Required."]})

        inward_taka = data.get("inward_taka") or data.get("taka") or dispatch.taka or 1
        dyeing_rate = data.get("dyeing_rate")
        logistic_rate = data.get("logistic_rate")
        weaving_rate = data.get("weaving_rate")
        yarn_rate = data.get("yarn_rate")
        inward_challan = data.get("inward_challan")
        inward_date = data.get("inward_date")
        transporter_party_id = data.get("transporter_party_id")

        try:
            result = record_mill_inward_with_landed_cost(
                dispatch=dispatch,
                inward_meters=inward_meters,
                inward_taka=inward_taka,
                dyeing_rate=dyeing_rate,
                logistic_rate=logistic_rate,
                weaving_rate=weaving_rate,
                yarn_rate=yarn_rate,
                inward_challan=inward_challan,
                inward_date=inward_date,
                transporter_party_id=transporter_party_id,
                user=request.user,
            )
            return Response({
                "message": "Fabric successfully received from mill with landed cost and accounts journal entry.",
                "dispatch": FabricDispatchSerializer(result["dispatch"], context=self.get_serializer_context()).data,
                "passport": result["passport"],
                "created_takas_count": len(result.get("created_takas", [])),
                "journal_entry_id": str(result["journal_entry"].id) if result.get("journal_entry") else None,
            }, status=status.HTTP_200_OK)
        except Exception as exc:
            logger.exception("Error receiving from mill: %s", exc)
            raise ValidationFailed(f"Failed to process mill inward: {str(exc)}")

    @action(detail=True, methods=["get"], url_path="passport")
    def passport(self, request, pk=None):
        """Returns the full fabric traceability passport & lineage breakdown."""
        dispatch = self.get_object()
        return Response({
            "passport": dispatch.history_passport or {},
            "dispatch_id": str(dispatch.id),
            "quality_name": dispatch.quality.name,
            "factory_name": getattr(dispatch.factory, "name", ""),
            "mill_name": getattr(dispatch.dyeing_mill_party, "name", "") or dispatch.dest_name or "",
            "landed_cost_rate": float(dispatch.landed_cost_rate or 0),
            "status": dispatch.mill_inward_status,
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
