"""
Loom & Weaving API ViewSets for Oscar PMS.
"""
from datetime import datetime, date
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

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
)
from .loom_services import (
    get_factory_status,
    get_factory_yarn_stock,
    get_program_stats,
    seed_loom_demo_data,
)


class LoomCoordinatorViewSet(TenantModelViewSet):
    queryset = LoomCoordinator.objects.all()
    serializer_class = LoomCoordinatorSerializer


class LoomFactoryViewSet(TenantModelViewSet):
    queryset = LoomFactory.objects.all()
    serializer_class = LoomFactorySerializer

    @action(detail=True, methods=["get"])
    def status(self, request, pk=None):
        factory = self.get_object()
        st = get_factory_status(factory)
        return Response(st)

    @action(detail=True, methods=["get"])
    def yarn_stock(self, request, pk=None):
        factory = self.get_object()
        yarns = YarnMaster.objects.filter(deleted_at__isnull=True)
        results = [get_factory_yarn_stock(factory, y) for y in yarns]
        return Response(results)


class LoomMachineViewSet(TenantModelViewSet):
    queryset = LoomMachine.objects.all()
    serializer_class = LoomMachineSerializer
    filterset_fields = ["factory"]

    @action(detail=False, methods=["post"])
    def batch_create(self, request):
        factory_id = request.data.get("factory")
        from_no = int(request.data.get("from_no", 1))
        to_no = int(request.data.get("to_no", 1))
        reed = int(request.data.get("reed", 84))
        client_id = getattr(request, "client_id", None)

        created = []
        for no in range(from_no, to_no + 1):
            m, was_created = LoomMachine.objects.get_or_create(
                client_id=client_id,
                factory_id=factory_id,
                no=no,
                defaults={"reed": reed},
            )
            created.append(m)
        return Response({"created": len(created)}, status=status.HTTP_201_CREATED)


class YarnMasterViewSet(TenantModelViewSet):
    queryset = YarnMaster.objects.all()
    serializer_class = YarnMasterSerializer


class LoomQualityViewSet(TenantModelViewSet):
    queryset = LoomQuality.objects.all()
    serializer_class = LoomQualitySerializer


class LoomBeamViewSet(TenantModelViewSet):
    queryset = LoomBeam.objects.all()
    serializer_class = LoomBeamSerializer
    filterset_fields = ["factory", "status", "machine"]

    @action(detail=True, methods=["post"])
    def load_to_loom(self, request, pk=None):
        beam = self.get_object()
        machine_id = request.data.get("machine_id")
        if not machine_id:
            return Response({"detail": "machine_id required"}, status=status.HTTP_400_BAD_REQUEST)

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
        machine_id = request.data.get("machine_id")
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
    queryset = DailyLoomProduction.objects.all()
    serializer_class = DailyLoomProductionSerializer
    filterset_fields = ["factory", "machine", "date"]

    @action(detail=False, methods=["get"])
    def factory_day(self, request):
        factory_id = request.query_params.get("factory")
        dt_str = request.query_params.get("date", date.today().isoformat())
        if not factory_id:
            return Response({"detail": "factory query param required"}, status=status.HTTP_400_BAD_REQUEST)

        factory = LoomFactory.objects.get(id=factory_id)
        day_log = FactoryDayLog.objects.filter(factory=factory, date=dt_str, deleted_at__isnull=True).first()
        power = float(day_log.power) if day_log else 0.0

        machines = LoomMachine.objects.filter(factory=factory, is_active=True, deleted_at__isnull=True).order_by("no")
        rows = []
        for m in machines:
            beam = LoomBeam.objects.filter(machine=m, status="On loom", deleted_at__isnull=True).first()
            prod = DailyLoomProduction.objects.filter(machine=m, date=dt_str, deleted_at__isnull=True).first()
            rem = float(beam.length) - float(DailyLoomProduction.objects.filter(beam=beam).aggregate(s=Sum("meters"))["s"] or 0) if beam else 0.0
            rows.append({
                "machine_id": str(m.id),
                "machine_no": m.no,
                "has_beam": bool(beam),
                "beam_id": str(beam.id) if beam else None,
                "beam_no": beam.beam_no if beam else "",
                "quality_name": beam.quality.name if beam else "",
                "remaining_m": max(0.0, rem),
                "done": bool(prod),
                "prod_id": str(prod.id) if prod else None,
                "meters": float(prod.meters) if prod else "",
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
        })

    @action(detail=False, methods=["post"])
    def bulk_save(self, request):
        factory_id = request.data.get("factory_id")
        dt_str = request.data.get("date", date.today().isoformat())
        power = Decimal(str(request.data.get("power", 0)))
        rows = request.data.get("rows", [])
        client_id = getattr(request, "client_id", None)

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
                if m_val is None or m_val == "" or float(m_val) <= 0:
                    continue

                machine_id = r.get("machine_id")
                beam_id = r.get("beam_id")
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
                    beam = LoomBeam.objects.get(id=beam_id)
                    tot_prod = DailyLoomProduction.objects.filter(beam=beam).aggregate(s=Sum("meters"))["s"] or 0
                    if float(tot_prod) >= float(beam.length):
                        beam.status = "Finished"
                        beam.save(update_fields=["status", "updated_at"])

        return Response({"saved_count": saved}, status=status.HTTP_200_OK)


class YarnBookingViewSet(TenantModelViewSet):
    queryset = YarnBooking.objects.all()
    serializer_class = YarnBookingSerializer


class YarnDeliveryViewSet(TenantModelViewSet):
    queryset = YarnDelivery.objects.all()
    serializer_class = YarnDeliverySerializer
    filterset_fields = ["factory", "status"]

    @action(detail=True, methods=["post"])
    def confirm_receipt(self, request, pk=None):
        delivery = self.get_object()
        got_kgs = Decimal(str(request.data.get("recd_kgs", delivery.kgs)))
        recd_date = request.data.get("recd_date", date.today().isoformat())
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

        return Response(YarnDeliverySerializer(delivery).data)


class ProductionProgramViewSet(TenantModelViewSet):
    queryset = ProductionProgram.objects.all()
    serializer_class = ProductionProgramSerializer

    @action(detail=True, methods=["get"])
    def stats(self, request, pk=None):
        prog = self.get_object()
        return Response(get_program_stats(prog))


class FabricDispatchViewSet(TenantModelViewSet):
    queryset = FabricDispatch.objects.all()
    serializer_class = FabricDispatchSerializer
    filterset_fields = ["factory", "quality"]

    @action(detail=False, methods=["get"])
    def inventory_summary(self, request):
        factory_id = request.query_params.get("factory")
        if not factory_id:
            return Response({"detail": "factory query param required"}, status=status.HTTP_400_BAD_REQUEST)

        qualities = LoomQuality.objects.filter(deleted_at__isnull=True)
        results = []
        for q in qualities:
            prod_m = DailyLoomProduction.objects.filter(
                factory_id=factory_id, beam__quality=q, deleted_at__isnull=True
            ).aggregate(s=Sum("meters"))["s"] or 0
            
            dispatches = FabricDispatch.objects.filter(
                factory_id=factory_id, quality=q, deleted_at__isnull=True
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
        all_mill_outs = FabricDispatch.objects.filter(dest="Dyeing mill", deleted_at__isnull=True).select_related("quality")
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
    permission_classes = [IsAuthenticated]

    def get(self, request):
        factories = LoomFactory.objects.filter(is_active=True, deleted_at__isnull=True)
        factory_statuses = [get_factory_status(f) for f in factories]
        
        programs = ProductionProgram.objects.filter(deleted_at__isnull=True).exclude(status="Closed")
        programs_data = [ProductionProgramSerializer(p).data for p in programs]

        return Response({
            "factories": factory_statuses,
            "programs": programs_data,
        })


class LoomDemoDataView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, action_name=None):
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
