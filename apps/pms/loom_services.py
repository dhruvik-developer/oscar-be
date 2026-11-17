"""
Loom & Weaving Business Logic & Calculation Services.
Accurately implements calculations from Oscar PMS prototype v0.4.
"""
from datetime import datetime, date, timedelta
from decimal import Decimal
from django.db.models import Sum, Avg
from django.utils import timezone

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


def get_machine_info(m):
    """Computes active beam, spare beam, remaining meters, 7-day avg and days left."""
    beam = LoomBeam.objects.filter(
        machine=m, status="On loom", deleted_at__isnull=True
    ).select_related("quality", "quality__warp_yarn", "quality__weft_yarn").first()

    spare = LoomBeam.objects.filter(
        machine=m, status="Spare", deleted_at__isnull=True
    ).select_related("quality").first()

    q = beam.quality if beam else None
    
    # Produced on active beam
    if beam:
        prod_m = DailyLoomProduction.objects.filter(
            beam=beam, deleted_at__isnull=True
        ).aggregate(s=Sum("meters"))["s"] or 0
        rem = max(0.0, float(beam.length) - float(prod_m))
    else:
        rem = 0.0

    # Recent 7 entries for machine
    recent = list(DailyLoomProduction.objects.filter(
        machine=m, deleted_at__isnull=True
    ).order_by("-date")[:7])
    
    avg_m = (sum(float(p.meters) for p in recent) / len(recent)) if recent else 0.0
    days_left = (rem / avg_m) if (beam and avg_m > 0) else (999.0 if beam else 0.0)

    return {
        "machine_id": str(m.id),
        "no": m.no,
        "reed": m.reed,
        "beam": {
            "id": str(beam.id),
            "no": beam.beam_no,
            "length": float(beam.length),
            "remaining": rem,
        } if beam else None,
        "spare": {
            "id": str(spare.id),
            "no": spare.beam_no,
            "quality_name": spare.quality.name,
            "length": float(spare.length),
        } if spare else None,
        "quality": {
            "id": str(q.id),
            "name": q.name,
            "reed": q.reed,
        } if q else None,
        "rem": rem,
        "avg": avg_m,
        "days_left": days_left,
    }


def get_factory_yarn_stock(factory, yarn):
    """
    Computes stock for a factory and yarn:
    Balance = Total Received Delivery - Warp Consumed (Beams made) - Weft Consumed (Daily Production meters * weft_kg_m).
    """
    recd = YarnDelivery.objects.filter(
        factory=factory,
        status="Received",
        booking__yarn=yarn,
        deleted_at__isnull=True,
    ).aggregate(s=Sum("recd_kgs"))["s"] or 0

    pending = YarnDelivery.objects.filter(
        factory=factory,
        status="Scheduled",
        booking__yarn=yarn,
        deleted_at__isnull=True,
    ).aggregate(s=Sum("kgs"))["s"] or 0

    # Warp consumed by beams for this factory
    beams = LoomBeam.objects.filter(
        factory=factory,
        quality__warp_yarn=yarn,
        deleted_at__isnull=True,
    ).select_related("quality", "quality__warp_yarn")
    warp_consumed = sum(float(b.length) * b.quality.warp_kg_m for b in beams)

    # Weft consumed by daily production for this factory
    prods = DailyLoomProduction.objects.filter(
        factory=factory,
        beam__quality__weft_yarn=yarn,
        deleted_at__isnull=True,
    ).select_related("beam__quality", "beam__quality__weft_yarn")
    weft_consumed = sum(float(p.meters) * p.beam.quality.weft_kg_m for p in prods)

    bal = float(recd) - warp_consumed - weft_consumed
    kpb = float(yarn.kg_per_bag) if float(yarn.kg_per_bag) > 0 else 1.0
    bags = bal / kpb

    return {
        "yarn_id": str(yarn.id),
        "yarn_name": yarn.name,
        "recd": float(recd),
        "pending": float(pending),
        "warp": warp_consumed,
        "weft": weft_consumed,
        "bal": bal,
        "bags": bags,
    }


def get_factory_status(factory):
    """Computes high-level factory status: machines running, meters on looms, spare meters, weft cover days."""
    machines = list(LoomMachine.objects.filter(factory=factory, is_active=True, deleted_at__isnull=True).order_by("no"))
    ms_info = [get_machine_info(m) for m in machines]

    running = [x for x in ms_info if x["beam"]]
    idle = [x for x in ms_info if not x["beam"]]
    on_loom_m = sum(x["rem"] for x in running)

    spares = LoomBeam.objects.filter(factory=factory, status="Spare", deleted_at__isnull=True)
    spare_m = float(spares.aggregate(s=Sum("length"))["s"] or 0)

    daily_out = sum(x["avg"] for x in ms_info)
    days_on_loom = (on_loom_m / daily_out) if daily_out > 0 else 0.0
    days_with_spare = ((on_loom_m + spare_m) / daily_out) if daily_out > 0 else 0.0

    urgent = [x for x in running if x["days_left"] <= 2.0 and not x["spare"]]
    within7 = [x for x in running if x["days_left"] <= 7.0 and not x["spare"]]

    # Weft daily burn rate
    weft_daily = {}
    for x in running:
        q_obj = LoomQuality.objects.get(id=x["quality"]["id"])
        wid = str(q_obj.weft_yarn_id)
        weft_daily[wid] = weft_daily.get(wid, 0.0) + (x["avg"] * q_obj.weft_kg_m)

    yarns = YarnMaster.objects.filter(deleted_at__isnull=True)
    yarn_cover = []
    for y in yarns:
        st = get_factory_yarn_stock(factory, y)
        d_rate = weft_daily.get(str(y.id), 0.0)
        d_cover = (st["bal"] / d_rate) if d_rate > 0 else None
        if st["bal"] != 0 or st["pending"] != 0 or d_rate > 0:
            yarn_cover.append({
                "yarn_id": str(y.id),
                "yarn_name": y.name,
                "bal": st["bal"],
                "bags": st["bags"],
                "daily": d_rate,
                "days": d_cover,
                "pending": st["pending"],
            })

    return {
        "factory_id": str(factory.id),
        "factory_name": factory.name,
        "total_machines": len(machines),
        "running_machines": len(running),
        "idle_machines": len(idle),
        "on_loom_m": on_loom_m,
        "spares_count": spares.count(),
        "spares_m": spare_m,
        "daily_out": daily_out,
        "days_on_loom": days_on_loom,
        "days_with_spare": days_with_spare,
        "urgent_machines": [x["no"] for x in urgent],
        "within7_count": len(within7),
        "yarn_cover": yarn_cover,
        "machines": ms_info,
    }


def get_program_stats(p):
    """Calculates program progress, expected progress by today, efficiency, and yarn needs."""
    q = p.quality
    # Productions matching program factory & quality within dates
    prods = DailyLoomProduction.objects.filter(
        factory=p.factory,
        beam__quality=q,
        date__gte=p.start,
        date__lte=p.deadline,
        deleted_at__isnull=True,
    )
    meters = float(prods.aggregate(s=Sum("meters"))["s"] or 0)
    damage = float(prods.aggregate(s=Sum("damage"))["s"] or 0)
    
    prod_list = list(prods)
    days_count = len({p_row.date for p_row in prod_list})
    avg_rpm = (sum(float(p_row.rpm) for p_row in prod_list) / len(prod_list)) if prod_list else 0.0

    # Theoretical meters = (rpm * 60 * hours) / ppm
    ppm = q.ppm
    theo = sum((float(p_row.rpm) * 60 * float(p_row.hours)) / ppm for p_row in prod_list) if ppm > 0 else 0.0
    eff = (meters / theo * 100.0) if theo > 0 else 0.0

    # Power cut avg
    day_logs = FactoryDayLog.objects.filter(factory=p.factory, date__gte=p.start, date__lte=p.deadline, deleted_at__isnull=True)
    power_avg = float(day_logs.aggregate(a=Avg("power"))["a"] or 0)

    # Working days & expected
    total_days = max(1, (p.deadline - p.start).days + 1)
    today_dt = date.today()
    cur_end = min(today_dt, p.deadline) if today_dt >= p.start else p.start
    days_used = max(0, min((cur_end - p.start).days + 1, total_days))
    per_day = float(p.target) / total_days
    expected = per_day * days_used

    # Active machines currently running this quality at this factory
    active_machines = LoomMachine.objects.filter(
        factory=p.factory,
        beams__status="On loom",
        beams__quality=q,
        is_active=True,
        deleted_at__isnull=True,
    ).distinct().count()

    remaining = max(0.0, float(p.target) - meters)
    warp_need = remaining * q.warp_kg_m
    weft_need = remaining * q.weft_kg_m

    # Dispatches
    dispatches = FabricDispatch.objects.filter(
        factory=p.factory, quality=q, date__gte=p.start, deleted_at__isnull=True
    )
    sent = float(dispatches.aggregate(s=Sum("meters"))["s"] or 0)
    godown = float(dispatches.filter(dest="Godown").aggregate(s=Sum("meters"))["s"] or 0)
    mill = float(dispatches.filter(dest="Dyeing mill").aggregate(s=Sum("meters"))["s"] or 0)

    return {
        "meters": meters,
        "target": float(p.target),
        "damage": damage,
        "days_count": days_count,
        "avg_rpm": avg_rpm,
        "theo_meters": theo,
        "eff_pct": eff,
        "power_avg": power_avg,
        "total_days": total_days,
        "days_used": days_used,
        "per_day": per_day,
        "expected": expected,
        "pct": (meters / float(p.target) * 100.0) if float(p.target) > 0 else 0.0,
        "machines": active_machines,
        "remaining": remaining,
        "warp_need": warp_need,
        "weft_need": weft_need,
        "cost_m": q.total_cost,
        "sent": sent,
        "godown": godown,
        "mill": mill,
        "at_factory": max(0.0, meters - sent),
    }


def seed_loom_demo_data(client_id):
    """Seeds the exact prototype v0.4 dataset for instant live testing."""
    # 1. Coordinators
    c_mayur, _ = LoomCoordinator.objects.get_or_create(client_id=client_id, name="Mayur")
    c_het, _ = LoomCoordinator.objects.get_or_create(client_id=client_id, name="Het")

    # 2. Factories
    f_laxmi, _ = LoomFactory.objects.get_or_create(client_id=client_id, name="Shree Laxmi Fab", defaults={"coord": c_het})
    f_royal, _ = LoomFactory.objects.get_or_create(client_id=client_id, name="Royal King Texo", defaults={"coord": c_mayur})
    f_diya, _ = LoomFactory.objects.get_or_create(client_id=client_id, name="Diya", defaults={"coord": c_mayur})
    f_grow, _ = LoomFactory.objects.get_or_create(client_id=client_id, name="Grow Tech", defaults={"coord": c_het})

    # 3. Yarns
    y1, _ = YarnMaster.objects.get_or_create(client_id=client_id, name="60 SIRO", defaults={"denier": Decimal("89.00"), "kg_per_bag": Decimal("25"), "rate": Decimal("210"), "note": "60s spun ≈ 89 den"})
    y2, _ = YarnMaster.objects.get_or_create(client_id=client_id, name="30 HT", defaults={"denier": Decimal("177.00"), "kg_per_bag": Decimal("60"), "rate": Decimal("160"), "note": "30s ≈ 177 den"})
    y3, _ = YarnMaster.objects.get_or_create(client_id=client_id, name="60 si-poly", defaults={"denier": Decimal("89.00"), "kg_per_bag": Decimal("25"), "rate": Decimal("190"), "note": ""})

    # 4. Qualities
    q_map = {
        "3103-A": LoomQuality.objects.get_or_create(client_id=client_id, name="3103-A", defaults={"warp_yarn": y1, "ends": 4872, "crimp": Decimal("5"), "weft_yarn": y2, "reed": 84, "reed_w": Decimal("58"), "ppi": Decimal("56"), "wastage": Decimal("8"), "pick_charge": Decimal("0.16")})[0],
        "3128": LoomQuality.objects.get_or_create(client_id=client_id, name="3128", defaults={"warp_yarn": y1, "ends": 5568, "crimp": Decimal("5"), "weft_yarn": y2, "reed": 96, "reed_w": Decimal("58"), "ppi": Decimal("60"), "wastage": Decimal("8"), "pick_charge": Decimal("0.16")})[0],
        "2170": LoomQuality.objects.get_or_create(client_id=client_id, name="2170", defaults={"warp_yarn": y3, "ends": 4872, "crimp": Decimal("5"), "weft_yarn": y2, "reed": 84, "reed_w": Decimal("58"), "ppi": Decimal("52"), "wastage": Decimal("8"), "pick_charge": Decimal("0.15")})[0],
        "3118": LoomQuality.objects.get_or_create(client_id=client_id, name="3118", defaults={"warp_yarn": y1, "ends": 5568, "crimp": Decimal("5"), "weft_yarn": y2, "reed": 96, "reed_w": Decimal("58"), "ppi": Decimal("64"), "wastage": Decimal("8"), "pick_charge": Decimal("0.17")})[0],
        "MULL": LoomQuality.objects.get_or_create(client_id=client_id, name="MULL", defaults={"warp_yarn": y1, "ends": 4872, "crimp": Decimal("5"), "weft_yarn": y3, "reed": 84, "reed_w": Decimal("58"), "ppi": Decimal("48"), "wastage": Decimal("8"), "pick_charge": Decimal("0.14")})[0],
    }

    # 5. Machines 1 to 18 for Shree Laxmi Fab
    reeds = {1:84, 2:96, 3:84, 4:84, 5:84, 6:84, 7:84, 8:96, 9:84, 10:84, 11:84, 12:96, 13:84, 14:84, 15:84, 16:96, 17:84, 18:84}
    machs = {}
    for no, r_cnt in reeds.items():
        m, _ = LoomMachine.objects.get_or_create(client_id=client_id, factory=f_laxmi, no=no, defaults={"reed": r_cnt})
        machs[no] = m

    # 6. Loom beams & production matching prototype
    loom_data = [
        (1, '3103-A', 500, '3103-A'), (17, '3103-A', 285, '3103-A'), (8, '3128', 1693, '3103-A'),
        (14, '3103-A', 1833, 'MULL'), (18, '3103-A', 1907, 'MULL'), (16, '3118', 2427, 'MULL'),
        (12, '3118', 3258, ''), (5, '2170', 6081, ''), (4, '2170', 6676, ''), (9, '3103-A', 6947, ''),
        (7, '2170', 7037, ''), (15, '3103-A', 7030, ''), (13, '3103-A', 7135, ''), (6, '2170', 7260, ''),
        (11, '2170', 7198, ''), (3, '2170', 7443, ''), (2, '3128', 8028, ''), (10, '3103-A', 8375, '')
    ]
    b_no = 100
    for no, q_name, rem, spare_q in loom_data:
        m = machs[no]
        q = q_map[q_name]
        d1 = round(380 + no * 4)
        d2 = round(400 + no * 3)
        b_no += 1
        beam, _ = LoomBeam.objects.get_or_create(
            client_id=client_id, factory=f_laxmi, beam_no=f"B{b_no}",
            defaults={"quality": q, "length": Decimal(str(rem + d1 + d2)), "date": date(2026, 10, 26), "status": "On loom", "machine": m}
        )
        DailyLoomProduction.objects.get_or_create(
            client_id=client_id, factory=f_laxmi, machine=m, beam=beam, date=date(2026, 10, 28),
            defaults={"meters": Decimal(str(d1)), "rpm": Decimal("640"), "hours": Decimal("22"), "stop": Decimal("0.5"), "reason": "", "damage": Decimal("1")}
        )
        DailyLoomProduction.objects.get_or_create(
            client_id=client_id, factory=f_laxmi, machine=m, beam=beam, date=date(2026, 10, 29),
            defaults={"meters": Decimal(str(d2)), "rpm": Decimal("650"), "hours": Decimal("22.5"), "stop": Decimal("0"), "reason": "", "damage": Decimal("0")}
        )
        if spare_q:
            b_no += 1
            LoomBeam.objects.get_or_create(
                client_id=client_id, factory=f_laxmi, beam_no=f"B{b_no}",
                defaults={"quality": q_map[spare_q], "length": Decimal("9000"), "date": date(2026, 10, 29), "status": "Spare", "machine": m}
            )

    # 7. Yarn Bookings & Deliveries
    bk1, _ = YarnBooking.objects.get_or_create(client_id=client_id, supplier="Indospun Yarn", yarn=y1, defaults={"kgs": Decimal("12000"), "rate": Decimal("210"), "date": date(2026, 10, 20)})
    bk2, _ = YarnBooking.objects.get_or_create(client_id=client_id, supplier="Indospun Yarn", yarn=y2, defaults={"kgs": Decimal("8000"), "rate": Decimal("160"), "date": date(2026, 10, 20)})
    bk3, _ = YarnBooking.objects.get_or_create(client_id=client_id, supplier="Reliance dealer", yarn=y3, defaults={"kgs": Decimal("4000"), "rate": Decimal("190"), "date": date(2026, 10, 22)})

    YarnDelivery.objects.get_or_create(client_id=client_id, booking=bk1, factory=f_laxmi, defaults={"kgs": Decimal("8400"), "rate": Decimal("210"), "due": date(2026, 10, 25), "recd_kgs": Decimal("8400"), "recd_date": date(2026, 10, 25), "status": "Received"})
    YarnDelivery.objects.get_or_create(client_id=client_id, booking=bk2, factory=f_laxmi, defaults={"kgs": Decimal("4500"), "rate": Decimal("160"), "due": date(2026, 10, 25), "recd_kgs": Decimal("4500"), "recd_date": date(2026, 10, 25), "status": "Received"})
    YarnDelivery.objects.get_or_create(client_id=client_id, booking=bk3, factory=f_laxmi, defaults={"kgs": Decimal("2600"), "rate": Decimal("190"), "due": date(2026, 10, 26), "recd_kgs": Decimal("2600"), "recd_date": date(2026, 10, 26), "status": "Received"})
    YarnDelivery.objects.get_or_create(client_id=client_id, booking=bk1, factory=f_laxmi, defaults={"kgs": Decimal("1500"), "rate": Decimal("212"), "due": date(2026, 11, 2), "recd_kgs": Decimal("0"), "status": "Scheduled"})

    # 8. Programs
    ProductionProgram.objects.get_or_create(
        client_id=client_id, name="3103-A – Shree Laxmi – Nov",
        defaults={"factory": f_laxmi, "quality": q_map["3103-A"], "target": Decimal("60000"), "start": date(2026, 10, 28), "deadline": date(2026, 11, 30), "t_rpm": Decimal("650"), "t_eff": Decimal("85"), "t_power": Decimal("2"), "status": "Running"}
    )
    ProductionProgram.objects.get_or_create(
        client_id=client_id, name="2170 – Shree Laxmi – Nov",
        defaults={"factory": f_laxmi, "quality": q_map["2170"], "target": Decimal("45000"), "start": date(2026, 10, 28), "deadline": date(2026, 11, 30), "t_rpm": Decimal("650"), "t_eff": Decimal("85"), "t_power": Decimal("2"), "status": "Running"}
    )

    # 9. Factory Day logs
    FactoryDayLog.objects.get_or_create(client_id=client_id, factory=f_laxmi, date=date(2026, 10, 28), defaults={"power": Decimal("1.5")})
    FactoryDayLog.objects.get_or_create(client_id=client_id, factory=f_laxmi, date=date(2026, 10, 29), defaults={"power": Decimal("0.5")})

    # 10. Fabric Dispatch
    FabricDispatch.objects.get_or_create(
        client_id=client_id, factory=f_laxmi, quality=q_map["3103-A"], date=date(2026, 10, 29),
        defaults={"taka": 12, "meters": Decimal("1440"), "dest": "Dyeing mill", "dest_name": "Shree Dyeing", "challan": "SL/101"}
    )
    return True
