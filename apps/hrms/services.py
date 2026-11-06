"""
HRMS services (api.md §11).

The three interlocking pieces db.md §11 calls out:

  - attendance derives ``hours`` and the Late / Half Day verdict from the
    ``attendance_flexibility`` policy
  - leave approval posts attendance **and** decrements the balance in one
    transaction
  - payroll ports ``salaryCalculations.js`` verbatim, and freezes the result on
    the payslip so a later attendance edit cannot silently restate a paid month
"""
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.core.audit import notify, record_audit
from apps.core.exceptions import BusinessRuleViolation, Codes, Conflict, ValidationFailed
from apps.core.money import ZERO, D, round2
from apps.core.numbering import allocate_number

from .models import (
    Attendance,
    CalendarEvent,
    Employee,
    Holiday,
    LeaveBalance,
    LeaveEncashment,
    LeaveRequest,
    Payslip,
    PayslipComponent,
    PayrollRun,
    SalaryAdvance,
    WorkingDay,
)

#: api.md §11.4 -- totalDays defaults to 24.
DEFAULT_TOTAL_DAYS = Decimal("24")

#: Defaults for the `attendance_flexibility` settings blob (db.md §2.8).
DEFAULT_FLEXIBILITY = {
    "graceMinutes": 15,
    "shiftStart": "09:30",
    "shiftEnd": "18:30",
    "fullDayHours": 8,
    "halfDayThresholdHours": 4,
    "coreHoursStart": "11:00",
    "coreHoursEnd": "16:00",
}


#: The Flexibility screen edits these labels ("10 minutes", "09:10",
#: "09:00 - 18:00 (Flexible)"); the attendance engine reads the numeric keys in
#: DEFAULT_FLEXIBILITY. ``normalize_flexibility`` keeps the two in step.
SCREEN_FLEXIBILITY_KEYS = (
    "gracePeriod", "lateAfter", "halfDayThreshold", "minimumWorkingHours",
    "flexibleWorkingHours", "overtimeStartsAfter",
)


def _number_in(value, fallback=None):
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return Decimal(match.group(0)) if match else fallback


def _clocks_in(value):
    return re.findall(r"\b(\d{1,2}:\d{2})\b", str(value or ""))


def _shift_clock(clock, minutes):
    parsed = _parse_time(clock, time(9, 30))
    moved = datetime.combine(date.today(), parsed) + timedelta(minutes=minutes)
    return moved.strftime("%H:%M")


def normalize_flexibility(policy, *, screen_wins=False):
    """Fill the engine keys from the screen labels (or the reverse).

    ``screen_wins`` is set when the Flexibility page saved, so what HR typed is
    what the engine uses; otherwise missing labels are derived from the engine
    keys so the page opens showing the policy actually in force.
    """
    policy = dict(policy)
    if screen_wins:
        grace = _number_in(policy.get("gracePeriod"))
        if grace is not None:
            policy["graceMinutes"] = int(grace)
        clocks = _clocks_in(policy.get("flexibleWorkingHours"))
        if clocks:
            policy["shiftStart"] = clocks[0].zfill(5)
            if len(clocks) > 1:
                policy["shiftEnd"] = clocks[1].zfill(5)
        elif _clocks_in(policy.get("lateAfter")):
            late_after = _clocks_in(policy.get("lateAfter"))[0]
            policy["shiftStart"] = _shift_clock(late_after, -int(policy.get("graceMinutes") or 0))
        half = _number_in(policy.get("halfDayThreshold"))
        if half is not None:
            policy["halfDayThresholdHours"] = float(half)
        full = _number_in(policy.get("minimumWorkingHours"))
        if full is not None:
            policy["fullDayHours"] = float(full)
        overtime = _number_in(policy.get("overtimeStartsAfter"))
        if overtime is not None:
            policy["overtimeAfterHours"] = float(overtime)

    grace = int(policy.get("graceMinutes") or 0)
    policy.setdefault("overtimeAfterHours", policy.get("fullDayHours") or 8)
    policy["lateAfter"] = _shift_clock(policy.get("shiftStart"), grace)
    policy.setdefault("gracePeriod", f"{grace} minutes")
    policy.setdefault("halfDayThreshold", f"{policy.get('halfDayThresholdHours')} hours")
    policy.setdefault("minimumWorkingHours", f"{policy.get('fullDayHours')} hours")
    policy.setdefault(
        "flexibleWorkingHours", f"{policy.get('shiftStart')} - {policy.get('shiftEnd')} (Flexible)"
    )
    policy.setdefault("overtimeStartsAfter", f"{policy.get('overtimeAfterHours')} hours")
    policy.setdefault("earlyClockOutRequiresApproval", True)
    policy.setdefault("overtimeRequiresApproval", True)
    return policy


def flexibility_policy(client_id):
    from apps.core.models import Setting

    row = Setting.objects.filter(client_id=client_id, key="attendance_flexibility").first()
    policy = dict(DEFAULT_FLEXIBILITY)
    if row and isinstance(row.value, dict):
        policy.update(row.value)
    return normalize_flexibility(policy)


def _parse_time(value, fallback):
    try:
        hour, minute = str(value).split(":")[:2]
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return fallback


def parse_clock_on(work_date, value):
    """``"09:30"`` / ``"9:30 AM"`` / an ISO datetime -> an aware datetime.

    The register screens send wall-clock times beside a date; ``None`` for a
    blank or placeholder (``—``) value.
    """
    if value in (None, "", "—", "-"):
        return None
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    text = str(value).strip()
    if "T" in text:
        from django.utils.dateparse import parse_datetime

        parsed = parse_datetime(text.replace("Z", "+00:00"))
        if parsed is None:
            return None
        return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)
    match = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?$", text)
    if not match:
        raise ValidationFailed(
            f"'{text}' is not a time.", field_errors={"checkIn": ["Use HH:MM."]}
        )
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3)
    if meridiem:
        hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
    if isinstance(work_date, str):
        work_date = date.fromisoformat(work_date[:10])
    return timezone.make_aware(datetime.combine(work_date, time(hour, minute)))


# ---------------------------------------------------------------------------
# Who counts as staff
# ---------------------------------------------------------------------------
#: Logins that run the system rather than work in it. Administration links every
#: login to an HR record (so the owner can punch in), but these people are not
#: part of the workforce: they stay out of the Employee, Attendance, Leave and
#: Payroll lists.
ADMIN_ROLE_CODES = ("OWNER", "SYS_ADMIN", "AD", "SUPER_ADMIN", "ADMIN")
ADMIN_ROLE_NAMES = ("admin", "administrator", "system administrator", "super admin",
                    "owner / management", "owner")
EXITED_EMPLOYEE_STATUSES = ("Resigned", "Terminated")


def admin_employee_ids(client_id):
    """Employee rows that only exist because an administrator's login is linked."""
    from apps.accounts.models import User

    admins = User.objects.filter(
        client_id=client_id, deleted_at__isnull=True, employee_id__isnull=False
    ).filter(
        Q(is_superuser=True)
        | Q(role__code__in=ADMIN_ROLE_CODES)
        | Q(role__name__iregex=r"^(%s)$" % "|".join(re.escape(n) for n in ADMIN_ROLE_NAMES))
    )
    return set(admins.values_list("employee_id", flat=True))


def workforce(client_id, queryset=None, *, include_exited=True):
    """The tenant's employees, without administrator-only records."""
    if queryset is None:
        queryset = Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)
    queryset = queryset.exclude(id__in=admin_employee_ids(client_id))
    if not include_exited:
        queryset = queryset.exclude(status__in=EXITED_EMPLOYEE_STATUSES)
    return queryset


# ---------------------------------------------------------------------------
# Attendance (api.md §11.2)
# ---------------------------------------------------------------------------
def derive_attendance(client_id, *, check_in, check_out, submitted_status=None):
    """``hours`` plus the Late / Half Day determination, applied server-side.

    An explicitly-submitted non-working status (Absent, On Leave, Holiday,
    Week Off, WFH) is respected as-is; the policy only decides between Present,
    Late and Half Day, which is the judgement the frontend was making badly.
    """
    if submitted_status in ("Absent", "On Leave", "Holiday", "Week Off"):
        return submitted_status, None

    if check_in is None:
        # Marking someone Present without times is a deliberate manual call.
        return submitted_status or "Absent", None

    hours = None
    if check_out is not None:
        delta = check_out - check_in
        hours = round(Decimal(delta.total_seconds()) / Decimal(3600), 2)
        if hours < 0:
            raise ValidationFailed(
                "Check-out cannot be before check-in.",
                field_errors={"checkOut": ["Must be after check-in."]},
            )

    if submitted_status in ("WFH", "Half Day"):
        return submitted_status, hours

    policy = flexibility_policy(client_id)
    shift_start = _parse_time(policy.get("shiftStart"), time(9, 30))
    grace = int(policy.get("graceMinutes") or 0)
    half_day_threshold = Decimal(str(policy.get("halfDayThresholdHours") or 4))
    full_day_hours = Decimal(str(policy.get("fullDayHours") or 8))

    if hours is not None:
        if hours < half_day_threshold:
            return "Half Day", hours
        if hours < full_day_hours:
            # Short of a full day but past the half-day threshold still counts
            # as a present day; the shortfall shows in `hours`.
            pass

    local_check_in = timezone.localtime(check_in) if timezone.is_aware(check_in) else check_in
    latest_on_time = (
        datetime.combine(local_check_in.date(), shift_start) + timedelta(minutes=grace)
    )
    if local_check_in.replace(tzinfo=None) > latest_on_time:
        return "Late", hours

    return "Present", hours


@transaction.atomic
def mark_attendance(
    *, client, employee, work_date, check_in=None, check_out=None, status=None,
    remark=None, source="manual", user=None, leave_request=None,
):
    """Upsert against the one-row-per-employee-per-day constraint (db.md §11.2)."""
    if getattr(employee, "status", None) in ["Resigned", "Terminated", "Exited"]:
        raise Conflict(
            f"Cannot mark attendance for {employee.status.lower()} employee {employee.name}.",
            code=Codes.ALREADY_DONE,
        )
    if source == "regularization" and status:
        derived_status = status
        hours = None
        if check_in and check_out:
            delta = check_out - check_in
            hours = round(Decimal(delta.total_seconds()) / Decimal(3600), 2)
    else:
        derived_status, hours = derive_attendance(
            client.id if hasattr(client, "id") else client,
            check_in=check_in,
            check_out=check_out,
            submitted_status=status,
        )

    client_id = getattr(client, "id", client)
    existing = Attendance.objects.select_for_update().filter(
        client_id=client_id, employee=employee, work_date=work_date
    ).first()

    before = None
    if existing is not None:
        before = {
            "status": existing.status,
            "checkIn": existing.check_in,
            "checkOut": existing.check_out,
        }
        existing.check_in = check_in
        existing.check_out = check_out
        existing.hours = hours
        if source == "regularization" and derived_status == "Present":
            existing.working_hours = hours or Decimal("8.0")
            existing.early_leaving_minutes = 0
        elif hours is not None:
            existing.working_hours = hours
        existing.status = derived_status
        existing.remark = remark
        existing.source = source
        if leave_request is not None:
            existing.leave_request = leave_request
        existing.updated_by = user if getattr(user, "is_authenticated", False) else None
        existing.save()
        row = existing
    else:
        row = Attendance.objects.create(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            check_in=check_in,
            check_out=check_out,
            hours=hours,
            status=derived_status,
            remark=remark,
            source=source,
            leave_request=leave_request,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

    # db.md §11.2 -- manual edits write an audit row; that is what
    # GET /hrms/attendance/audit/ reads.
    if source in ("manual", "regularization") and getattr(user, "is_authenticated", False):
        record_audit(
            client=client_id,
            actor=user,
            action="attendance_edit" if existing is not None else "attendance_mark",
            entity_type="Attendance",
            entity_id=row.id,
            entity_label=f"{employee.employee_code} {work_date}",
            description=f"Attendance set to {derived_status}",
            before=before,
            after={"status": derived_status, "checkIn": check_in, "checkOut": check_out},
        )
    return row


def working_days_in(client_id, start, end, location_id=None):
    """Working dates in a range, honouring the working-day and holiday config."""
    config = {
        row.weekday: row.is_working
        for row in WorkingDay.objects.filter(client_id=client_id, deleted_at__isnull=True)
    }
    holidays = set(
        Holiday.objects.filter(
            client_id=client_id, deleted_at__isnull=True, date__gte=start, date__lte=end
        ).values_list("date", flat=True)
    )

    days = []
    current = start
    while current <= end:
        weekday = current.weekday()
        is_working = config.get(weekday, weekday < 5)  # Mon-Fri by default
        if is_working and current not in holidays:
            days.append(current)
        current += timedelta(days=1)
    return days


def _overtime_hours(hours, policy):
    if hours is None:
        return Decimal("0")
    threshold = Decimal(str(policy.get("overtimeAfterHours") or policy.get("fullDayHours") or 8))
    return max(Decimal("0"), round(D(hours) - threshold, 2))


def _iso(value):
    return value.isoformat() if value else None


def _attendance_row_payload(row, policy):
    leave_request = row.leave_request if row.leave_request_id else None
    return {
        "attendanceId": str(row.id),
        "status": row.status,
        "checkIn": _iso(row.check_in),
        "checkOut": _iso(row.check_out),
        "hours": float(row.hours) if row.hours is not None else None,
        "overtimeHours": float(_overtime_hours(row.hours, policy)),
        "source": row.source,
        "remark": row.remark or "",
        "leaveType": leave_request.leave_type.name if leave_request else None,
        "leavePaid": leave_request.leave_type.is_paid if leave_request else None,
    }


def _employee_payload(employee):
    return {
        "employeeId": str(employee.id),
        "employeeCode": employee.employee_code,
        "name": employee.name,
        "email": employee.email or "",
        "avatar": employee.avatar_url or "",
        "department": employee.department.name if employee.department_id else "",
        "designation": employee.designation.name if employee.designation_id else "",
        "location": employee.location.name if employee.location_id else "",
        "shift": employee.shift or "General",
        "employmentType": employee.employment_type or "Full-time",
        "employeeStatus": employee.status,
    }


def daily_attendance(client_id, work_date, *, department=None):
    """``GET /hrms/attendance/daily/?date=`` -- every employee on one day.

    The register screens (Overview, Mark, Bulk) start from the workforce, not
    from whatever rows exist: an employee with no row is "Not Marked" on a
    working day, "Holiday" / "Week Off" otherwise.
    """
    policy = flexibility_policy(client_id)
    rows = {
        row.employee_id: row
        for row in Attendance.objects.filter(
            client_id=client_id, work_date=work_date, deleted_at__isnull=True
        ).select_related("leave_request__leave_type")
    }
    employees = (
        workforce(client_id)
        .filter(Q(joining_date__lte=work_date) | Q(joining_date__isnull=True))
        .select_related("department", "designation", "location")
        .order_by("name")
    )
    if department:
        employees = employees.filter(department__name__iexact=department)

    holiday = Holiday.objects.filter(
        client_id=client_id, date=work_date, deleted_at__isnull=True
    ).first()
    is_working = bool(working_days_in(client_id, work_date, work_date))
    default_status = "Holiday" if holiday else ("Not Marked" if is_working else "Week Off")

    out = []
    for employee in employees:
        row = rows.get(employee.id)
        # Someone who has left only shows on days they actually have a record.
        if row is None and employee.status in EXITED_EMPLOYEE_STATUSES:
            continue
        item = _employee_payload(employee)
        if row is not None:
            item.update(_attendance_row_payload(row, policy))
            item["marked"] = True
        else:
            item.update({
                "attendanceId": None, "status": default_status, "checkIn": None,
                "checkOut": None, "hours": None, "overtimeHours": 0.0, "source": None,
                "remark": "", "leaveType": None, "leavePaid": None, "marked": False,
            })
        out.append(item)

    count = lambda status: sum(1 for item in out if item["status"] == status)  # noqa: E731
    present_like = count("Present") + count("Late") + count("WFH") + count("Half Day")
    summary = {
        "total": len(out),
        "present": count("Present"),
        "late": count("Late"),
        "absent": count("Absent"),
        "halfDay": count("Half Day"),
        "wfh": count("WFH"),
        "onLeave": count("On Leave"),
        "notMarked": count("Not Marked"),
        "attendancePct": round(present_like * 100 / len(out)) if out else 0,
        "overtimeHours": round(sum(item["overtimeHours"] or 0 for item in out), 2),
    }
    return {
        "date": work_date.isoformat(),
        "isWorkingDay": is_working and holiday is None,
        "holiday": holiday.name if holiday else None,
        "graceMinutes": int(policy.get("graceMinutes") or 0),
        "shiftStart": policy.get("shiftStart"),
        "shiftEnd": policy.get("shiftEnd"),
        "summary": summary,
        "results": out,
    }


def monthly_attendance(client_id, employee, month):
    """``GET /hrms/attendance/individual/{id}/?month=YYYY-MM`` -- one calendar month.

    Every day of the month with its verdict, plus the counts payroll will use,
    so HR sees exactly what the payslip is built from.
    """
    policy = flexibility_policy(client_id)
    first = month.replace(day=1)
    last = _month_end(first)
    today = timezone.localdate()
    rows = {
        row.work_date: row
        for row in Attendance.objects.filter(
            client_id=client_id, employee=employee, deleted_at__isnull=True,
            work_date__gte=first, work_date__lte=last,
        ).select_related("leave_request__leave_type")
    }
    working = set(working_days_in(client_id, first, last))
    holidays = {
        h.date: h.name
        for h in Holiday.objects.filter(
            client_id=client_id, deleted_at__isnull=True, date__gte=first, date__lte=last
        )
    }

    days = []
    current = first
    while current <= last:
        row = rows.get(current)
        day = {"date": current.isoformat(), "weekday": current.strftime("%a"),
               "isWorkingDay": current in working, "holiday": holidays.get(current)}
        if row is not None:
            day.update(_attendance_row_payload(row, policy))
        else:
            if current in holidays:
                status_label = "Holiday"
            elif current not in working:
                status_label = "Week Off"
            elif current > today or (employee.joining_date and current < employee.joining_date):
                status_label = "Upcoming" if current > today else "Not Joined"
            else:
                status_label = "Not Marked"
            day.update({"attendanceId": None, "status": status_label, "checkIn": None,
                        "checkOut": None, "hours": None, "overtimeHours": 0.0,
                        "source": None, "remark": "", "leaveType": None, "leavePaid": None})
        days.append(day)
        current += timedelta(days=1)

    counts = attendance_counts(client_id, employee, first, last)
    total_hours = sum((D(r.hours) for r in rows.values() if r.hours is not None), Decimal("0"))
    overtime = sum((_overtime_hours(r.hours, policy) for r in rows.values()), Decimal("0"))
    return {
        "employee": _employee_payload(employee),
        "month": first.strftime("%Y-%m"),
        "days": days,
        "summary": {
            **{k: float(v) for k, v in counts.items()},
            "workingDays": len(working),
            "totalHours": float(round(total_hours, 2)),
            "overtimeHours": float(round(overtime, 2)),
            "notMarked": sum(1 for d in days if d["status"] == "Not Marked"),
        },
    }


def attendance_counts(client_id, employee, start, end):
    """The day counts payroll is computed from, for one employee and range.

    Late / WFH are attended days, a Half Day is half of one. "On Leave" splits
    by the leave type: paid leave is not deducted, unpaid (Loss of Pay) is.
    """
    rows = Attendance.objects.filter(
        client_id=client_id, employee=employee, deleted_at__isnull=True,
        work_date__gte=start, work_date__lte=end,
    ).select_related("leave_request__leave_type")
    counts = {"present": 0, "late": 0, "wfh": 0, "halfDay": 0, "absent": 0,
              "paidLeave": 0, "unpaidLeave": 0, "holiday": 0, "weekOff": 0}
    for row in rows:
        if row.status == "Present":
            counts["present"] += 1
        elif row.status == "Late":
            counts["late"] += 1
        elif row.status == "WFH":
            counts["wfh"] += 1
        elif row.status == "Half Day":
            counts["halfDay"] += 1
            # The other half of a half-day leave is leave, not absence.
            if row.leave_request_id:
                key = "paidLeave" if row.leave_request.leave_type.is_paid else "unpaidLeave"
                counts[key] += Decimal("0.5")
        elif row.status == "Absent":
            counts["absent"] += 1
        elif row.status == "On Leave":
            paid = row.leave_request.leave_type.is_paid if row.leave_request_id else True
            counts["paidLeave" if paid else "unpaidLeave"] += 1
        elif row.status == "Holiday":
            counts["holiday"] += 1
        elif row.status == "Week Off":
            counts["weekOff"] += 1
    counts["attendedDays"] = (
        Decimal(counts["present"] + counts["late"] + counts["wfh"])
        + Decimal("0.5") * counts["halfDay"]
    )
    counts["onLeave"] = counts["paidLeave"] + counts["unpaidLeave"]
    return counts


def attendance_summary(client_id, *, month=None, employee_id=None, department_id=None):
    """``GET /hrms/attendance/summary/`` -- monthly per-employee counts."""
    queryset = Attendance.objects.filter(
        client_id=client_id, deleted_at__isnull=True,
        employee_id__in=workforce(client_id).values("id"),
    )
    if month:
        queryset = queryset.filter(work_date__year=month.year, work_date__month=month.month)
    if employee_id:
        queryset = queryset.filter(employee_id=employee_id)
    if department_id:
        queryset = queryset.filter(employee__department_id=department_id)

    rows = (
        queryset.values("employee_id", "employee__employee_code", "employee__name")
        .annotate(
            present=Count("id", filter=Q(status="Present")),
            absent=Count("id", filter=Q(status="Absent")),
            late=Count("id", filter=Q(status="Late")),
            halfDay=Count("id", filter=Q(status="Half Day")),
            wfh=Count("id", filter=Q(status="WFH")),
            onLeave=Count("id", filter=Q(status="On Leave")),
            holiday=Count("id", filter=Q(status="Holiday")),
            weekOff=Count("id", filter=Q(status="Week Off")),
            totalHours=Sum("hours"),
        )
        .order_by("employee__name")
    )
    return [
        {
            "employeeId": str(row["employee_id"]),
            "employeeCode": row["employee__employee_code"],
            "name": row["employee__name"],
            "present": row["present"],
            "absent": row["absent"],
            "late": row["late"],
            "halfDay": row["halfDay"],
            "wfh": row["wfh"],
            "onLeave": row["onLeave"],
            "holiday": row["holiday"],
            "weekOff": row["weekOff"],
            "totalHours": row["totalHours"] or 0,
            # Late and WFH days still count as attended for payroll.
            "attendedDays": row["present"] + row["late"] + row["wfh"]
            + Decimal("0.5") * row["halfDay"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Leave (api.md §11.3)
# ---------------------------------------------------------------------------
#: The standard leave types, created on first use for a tenant that has not
#: configured any yet: ``(name, annual_entitlement, accrual, is_paid)``.
DEFAULT_LEAVE_TYPES = (
    ("Casual Leave", "12", "Yearly", True),
    ("Sick Leave", "8", "Yearly", True),
    ("Earned Leave", "18", "Monthly", True),
    ("Loss of Pay", "0", "None", False),
)

#: Other spellings the screens use for the defaults above.
LEAVE_TYPE_ALIASES = {
    "annual leave": "Earned Leave",
    "earned / privilege leave": "Earned Leave",
    "privilege leave": "Earned Leave",
    "unpaid leave": "Loss of Pay",
}


def resolve_leave_type(client_id, name):
    """The tenant's leave type called ``name`` (or its code), else ``None``.

    A standard type (see ``DEFAULT_LEAVE_TYPES``) the tenant has not set up
    yet is created on first use, so self-service leave works before HR has
    configured the leave master.
    """
    from .models import LeaveType

    label = re.sub(r"\s*\(half day\)\s*$", "", str(name or ""), flags=re.I).strip()
    if not label:
        return None
    label = LEAVE_TYPE_ALIASES.get(label.lower(), label)

    live = LeaveType.objects.filter(client_id=client_id, deleted_at__isnull=True)
    found = live.filter(Q(name__iexact=label) | Q(code__iexact=label)).first()
    if found:
        return found

    for default_name, entitlement, accrual, paid in DEFAULT_LEAVE_TYPES:
        if default_name.lower() == label.lower():
            return LeaveType.objects.create(
                client_id=client_id,
                name=default_name,
                annual_entitlement=Decimal(entitlement),
                accrual=accrual,
                is_paid=paid,
                carry_forward_cap=Decimal("10") if accrual != "None" else None,
            )
    return None


def assert_no_overlap(leave_request):
    """db.md §11.3 -- overlapping live requests for the same employee are rejected."""
    clash = LeaveRequest.objects.filter(
        client_id=leave_request.client_id,
        employee_id=leave_request.employee_id,
        status__in=["Pending Review", "Delegate Confirmed", "Approved"],
        from_date__lte=leave_request.to_date,
        to_date__gte=leave_request.from_date,
        deleted_at__isnull=True,
    ).exclude(pk=leave_request.pk)
    if clash.exists():
        existing = clash.first()
        raise BusinessRuleViolation(
            "This overlaps an existing leave request "
            f"({existing.from_date} to {existing.to_date}).",
            code="LEAVE_OVERLAP",
            payload={"conflictingRequestId": str(existing.id)},
        )


def leave_balance_for(client_id, employee_id, leave_type_id, year=None):
    """The year's balance row, opened at the leave type's annual entitlement.

    A row opened at zero would make the first approval of the year fail with
    "insufficient balance" for every employee HR never seeded by hand.
    """
    from .models import LeaveType

    year = year or timezone.localdate().year
    leave_type = LeaveType.objects.filter(pk=leave_type_id).first()
    entitlement = D(leave_type.annual_entitlement) if leave_type else ZERO
    balance, _ = LeaveBalance.objects.get_or_create(
        client_id=client_id,
        employee_id=employee_id,
        leave_type_id=leave_type_id,
        period_year=year,
        defaults={"entitlement": entitlement, "carried_forward": ZERO, "used": ZERO},
    )
    return balance


def leave_balances_for_employee(client_id, employee, year=None):
    """Every active leave type's balance for one employee (rows opened as needed)."""
    from .models import LeaveType

    year = year or timezone.localdate().year
    for name, *_ in DEFAULT_LEAVE_TYPES:
        resolve_leave_type(client_id, name)
    out = []
    for leave_type in LeaveType.objects.filter(client_id=client_id, deleted_at__isnull=True):
        balance = leave_balance_for(client_id, employee.id, leave_type.id, year)
        pending = LeaveRequest.objects.filter(
            client_id=client_id, employee=employee, leave_type=leave_type,
            status__in=["Pending Review", "Delegate Confirmed"], deleted_at__isnull=True,
            from_date__year=year,
        ).aggregate(total=Sum("days"))["total"] or ZERO
        total = D(balance.entitlement) + D(balance.carried_forward)
        available = total - D(balance.used) - D(balance.encashed)
        out.append({
            "leaveTypeId": str(leave_type.id),
            "leaveType": leave_type.name,
            "code": leave_type.code or "",
            "isPaid": leave_type.is_paid,
            "year": year,
            "entitlement": float(balance.entitlement),
            "carriedForward": float(balance.carried_forward),
            "total": float(total),
            "used": float(balance.used),
            "encashed": float(balance.encashed),
            "pending": float(pending),
            "available": float(available),
        })
    return out


@transaction.atomic
def approve_leave(leave_request, *, user=None, remark=None):
    """One transaction: flip to Approved, post attendance, decrement balance.

    api.md §11.3 requires all three together -- a leave that is approved but
    not posted to attendance would silently become an absence at payroll time.
    """
    leave_request = LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)
    if leave_request.status == "Approved":
        raise Conflict("This leave is already approved.", code=Codes.ALREADY_DONE)
    if leave_request.status in ("Rejected", "Cancelled"):
        raise Conflict(
            f"This leave request is {leave_request.status.lower()}.",
            code=Codes.BAD_TARGET,
        )

    assert_no_overlap(leave_request)

    balance = leave_balance_for(
        leave_request.client_id, leave_request.employee_id, leave_request.leave_type_id,
        leave_request.from_date.year,
    )
    available = D(balance.entitlement) + D(balance.carried_forward) - D(balance.used) - D(balance.encashed)
    # Unpaid leave (Loss of Pay) has no quota: it is deducted at payroll instead.
    if leave_request.leave_type.is_paid and D(leave_request.days) > available:
        raise BusinessRuleViolation(
            f"{leave_request.employee.name} has {available} day(s) of "
            f"{leave_request.leave_type.name} left but requested {leave_request.days}.",
            code="INSUFFICIENT_LEAVE_BALANCE",
            payload={"available": str(available), "requested": str(leave_request.days)},
        )

    leave_request.status = "Approved"
    leave_request.approver = user if getattr(user, "is_authenticated", False) else None
    leave_request.decided_at = timezone.now()
    leave_request.remark = remark
    leave_request.save()

    # Post attendance for every working day in range.
    # A half-day leave keeps the other half as a worked Half Day.
    half_day = D(leave_request.days) < 1 and leave_request.from_date == leave_request.to_date
    for day in working_days_in(
        leave_request.client_id, leave_request.from_date, leave_request.to_date
    ):
        mark_attendance(
            client=leave_request.client,
            employee=leave_request.employee,
            work_date=day,
            status="Half Day" if half_day else "On Leave",
            source="leave",
            leave_request=leave_request,
            remark=leave_request.reason,
            user=user,
        )

    balance.refresh_from_db(fields=["used", "balance", "updated_at"])

    # api.md §11.8 -- leave-derived calendar events are generated, not entered.
    CalendarEvent.objects.create(
        client_id=leave_request.client_id,
        title=f"{leave_request.employee.name} on leave",
        type="Leave",
        starts_at=timezone.make_aware(
            datetime.combine(leave_request.from_date, time(0, 0))
        ),
        ends_at=timezone.make_aware(datetime.combine(leave_request.to_date, time(23, 59))),
        all_day=True,
        department=leave_request.employee.department,
        source_type="LeaveRequest",
        source_id=leave_request.id,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    from .user_link import linked_user

    # Notifications go to logins, not HR rows.
    employee_login = linked_user(leave_request.employee)
    if employee_login is not None and leave_request.employee.status == "Active":
        notify(
            client=leave_request.client_id,
            recipients=[employee_login.id],
            type="hrms.leave_approved",
            category="hrms",
            title="Your leave was approved",
            body=f"{leave_request.from_date} to {leave_request.to_date}.",
            entity_type="LeaveRequest",
            entity_id=leave_request.id,
            actor=user,
        )
    return leave_request


@transaction.atomic
def cancel_leave(leave_request, *, user=None, reason=None):
    """Withdraw a leave, removing exactly the rows it generated."""
    leave_request = LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)
    if leave_request.status == "Cancelled":
        raise Conflict("This leave is already cancelled.", code=Codes.ALREADY_CANCELLED)

    was_approved = leave_request.status == "Approved"
    leave_request.status = "Cancelled"
    leave_request.remark = reason or leave_request.remark
    leave_request.save(update_fields=["status", "remark", "updated_at"])

    if was_approved:
        Attendance.objects.filter(
            client_id=leave_request.client_id, leave_request=leave_request
        ).update(deleted_at=timezone.now())

        balance = leave_balance_for(
            leave_request.client_id, leave_request.employee_id,
            leave_request.leave_type_id, leave_request.from_date.year,
        )
        balance.refresh_from_db(fields=["used", "balance", "updated_at"])

        # Source-tagged events make this exact, rather than a fuzzy date match.
        CalendarEvent.objects.filter(
            client_id=leave_request.client_id,
            source_type="LeaveRequest",
            source_id=leave_request.id,
        ).update(deleted_at=timezone.now())

    return leave_request


# ---------------------------------------------------------------------------
# Payroll (api.md §11.4)
# ---------------------------------------------------------------------------
def compute_salary(
    *, standard_salary, total_days=None, attended_days=0, paid_leaves=0,
    additional_earnings=0, deductions=0, advance=0, earned_salary_override=None,
):
    """Ported verbatim from ``features/hrms/payroll/salaryCalculations.js``.

        perDaySalary        = standardSalary / totalDays
        absentDays          = totalDays - attendedDays - paidLeaves
        attendanceDeduction = perDaySalary * max(0, absentDays)
        earnedSalary        = standardSalary - attendanceDeduction
        remainingPayable    = earnedSalary + additionalEarnings - deductions - advance

    Approved paid leave does not cause a deduction. An explicit
    ``earnedSalary`` overrides the computed value -- api.md §11.4 asks for that
    escape hatch to be kept, and the caller logs that it was used.
    """
    standard_salary = D(standard_salary)
    total_days = D(total_days) if total_days else DEFAULT_TOTAL_DAYS
    if total_days <= ZERO:
        total_days = DEFAULT_TOTAL_DAYS
    attended_days = D(attended_days)
    paid_leaves = D(paid_leaves)

    per_day = standard_salary / total_days
    absent_days = total_days - attended_days - paid_leaves
    if absent_days < ZERO:
        absent_days = ZERO
    attendance_deduction = round2(per_day * absent_days)

    if earned_salary_override is not None:
        earned_salary = round2(earned_salary_override)
        overridden = True
    else:
        earned_salary = round2(standard_salary - attendance_deduction)
        overridden = False

    net_payable = round2(
        earned_salary + D(additional_earnings) - D(deductions) - D(advance)
    )

    return {
        "perDaySalary": round2(per_day),
        "absentDays": absent_days,
        "attendanceDeduction": attendance_deduction,
        "earnedSalary": earned_salary,
        "earnedSalaryOverridden": overridden,
        "netPayable": net_payable,
    }


def split_structure(structure, earned_salary):
    """Basic / HRA / allowances from the salary structure, if one is attached."""
    earned_salary = D(earned_salary)
    if structure is None:
        return {"basic": earned_salary, "hra": ZERO, "allowances": ZERO, "components": []}

    basic = round2(earned_salary * D(structure.basic_pct or 0) / Decimal("100"))
    hra = round2(earned_salary * D(structure.hra_pct or 0) / Decimal("100"))
    allowances = round2(earned_salary - basic - hra)

    components = []
    for component in structure.components or []:
        name = component.get("name")
        kind = component.get("type") or component.get("kind") or "earning"
        calc = component.get("calc") or "fixed"
        value = D(component.get("value"))
        amount = (
            round2(earned_salary * value / Decimal("100")) if calc == "percent" else round2(value)
        )
        if name:
            components.append({"name": name, "kind": kind, "amount": amount})

    return {"basic": basic, "hra": hra, "allowances": allowances, "components": components}


@transaction.atomic

def approve_leave_encashment(encashment, *, user=None):
    """Approve a leave encashment request and increment LeaveBalance.encashed."""
    if encashment.status == "Approved":
        return encashment
    year = encashment.created_at.year if encashment.created_at else timezone.now().year
    balance = leave_balance_for(
        encashment.client_id, encashment.employee_id, encashment.leave_type_id, year
    )
    available = D(balance.entitlement) + D(balance.carried_forward) - D(balance.used) - D(balance.encashed)
    if D(encashment.days) > available:
        raise BusinessRuleViolation(
            f"Insufficient leave balance to encash {encashment.days} day(s); {available} available.",
            code="INSUFFICIENT_LEAVE_BALANCE",
        )
    encashment.status = "Approved"
    encashment.save(update_fields=["status", "updated_at"])
    balance.encashed = D(balance.encashed or 0) + D(encashment.days)
    balance.save(update_fields=["encashed", "updated_at"])
    return encashment


def process_payroll(*, client, period_month, employee_ids=None, user=None):
    """``POST /hrms/payroll/process/`` -- generate the run."""
    period_month = period_month.replace(day=1)

    run, _ = PayrollRun.objects.get_or_create(
        client=client,
        period_month=period_month,
        defaults={
            "status": "In Progress",
            "processed_by": user if getattr(user, "is_authenticated", False) else None,
            "processed_at": timezone.now(),
            "created_by": user if getattr(user, "is_authenticated", False) else None,
        },
    )
    if run.status in ("Approved", "Paid"):
        raise Conflict(
            f"Payroll for {period_month:%B %Y} is already {run.status.lower()}.",
            code=Codes.ALREADY_DONE,
        )

    employees = workforce(
        client.id,
        Employee.objects.filter(
            client=client, deleted_at__isnull=True,
            status__in=["Active", "On Leave", "Probation"],
        ),
    ).select_related("salary_structure")
    if employee_ids:
        employees = employees.filter(pk__in=employee_ids)

    month_end = _month_end(period_month)
    # Auto punch-out any unclosed sessions up to month_end so all attended days are accounted for
    auto_punch_out_pending_sessions(client=client.id, as_of_date=month_end, force_today=True)
    working = working_days_in(client.id, period_month, month_end)
    total_days = Decimal(len(working)) or DEFAULT_TOTAL_DAYS

    payslips = []
    for employee in employees:
        # Attended + paid leave are paid; absences, unmarked working days and
        # unpaid leave (Loss of Pay) fall out as the attendance deduction.
        counts = attendance_counts(client.id, employee, period_month, month_end)
        attended = D(counts["attendedDays"])
        paid_leaves = D(counts["paidLeave"])

        # Fixed salary only (Oscar letter pad §12): salesperson incentive is
        # calculated, posted and paid on its own through PMS
        # IncentiveCalculation, never through the payslip.
        advance = _pending_advance_instalment(employee)
        # Re-running the month refreshes attendance but keeps HR's adjustments.
        previous = Payslip.objects.filter(
            payroll_run=run, employee=employee, deleted_at__isnull=True
        ).first()
        # Factor in approved leave encashments for the month
        encashments = LeaveEncashment.objects.filter(
            client=client,
            employee=employee,
            status="Approved",
            created_at__year=period_month.year,
            created_at__month=period_month.month,
        )
        encashment_total = sum((D(e.amount or 0) for e in encashments), ZERO)
        additional = (D(previous.additional_earnings) if previous else ZERO) + encashment_total
        other_deductions = D(previous.deductions) if previous else ZERO

        # Working hour shortfall calculation:
        # If employee worked fewer hours than standard decided hours (8 hours) due to late arrival,
        # long breaks, or early exit, deduct the shortfall proportionally:
        # Total working hour = punch out - punch in - (break out - break in)
        policy = flexibility_policy(client.id)
        decided_daily_hours = Decimal(str(policy.get("fullDayHours") or 8.0))
        decided_half_day_hours = Decimal(str(policy.get("halfDayThresholdHours") or 4.0))

        emp_attendance_rows = Attendance.objects.filter(
            client_id=client.id,
            employee=employee,
            work_date__gte=period_month,
            work_date__lte=month_end,
            deleted_at__isnull=True,
        )

        total_shortfall_hours = Decimal("0.0")
        for att in emp_attendance_rows:
            if att.status in ("Present", "Late", "WFH"):
                wh = att.working_hours if att.working_hours is not None else att.hours
                if wh is not None:
                    wh_dec = Decimal(str(wh))
                    if wh_dec < decided_daily_hours:
                        total_shortfall_hours += (decided_daily_hours - wh_dec)
            elif att.status == "Half Day":
                wh = att.working_hours if att.working_hours is not None else att.hours
                if wh is not None:
                    wh_dec = Decimal(str(wh))
                    if wh_dec < decided_half_day_hours:
                        total_shortfall_hours += (decided_half_day_hours - wh_dec)

        hourly_rate = (employee.standard_salary / (total_days * decided_daily_hours)) if (total_days and decided_daily_hours) else Decimal("0.00")
        shortfall_deduction = round2(total_shortfall_hours * hourly_rate)
        total_deductions = other_deductions + shortfall_deduction

        result = compute_salary(
            standard_salary=employee.standard_salary,
            total_days=total_days,
            attended_days=attended,
            paid_leaves=paid_leaves,
            additional_earnings=additional,
            deductions=total_deductions,
            advance=advance,
        )
        split = split_structure(employee.salary_structure, result["earnedSalary"])

        # Re-running the month never resets an approved or paid payslip: its
        # figures are frozen and already on the ledger (accrual / payment).
        settled = Payslip.objects.filter(
            payroll_run=run, employee=employee, status__in=["Approved", "Paid"]
        ).first()
        if settled is not None:
            # If an existing payslip was generated with 0 salary prior to employee configuration,
            # sync it with the employee's standard salary upon re-generation.
            if settled.standard_salary == 0 and employee.standard_salary > 0:
                settled.standard_salary = employee.standard_salary
                settled.total_days = total_days
                settled.attended_days = attended
                settled.paid_leaves = paid_leaves
                settled.earned_salary = result["earnedSalary"]
                settled.net_payable = result["netPayable"]
                # Never touch paid_amount on a settled (Approved/Paid) slip.
                settled.save(update_fields=[
                    "standard_salary", "total_days", "attended_days", "paid_leaves",
                    "earned_salary", "net_payable", "updated_at"
                ])
            payslips.append(settled)
            continue

        payslip, created = Payslip.objects.update_or_create(
            payroll_run=run,
            employee=employee,
            defaults={
                "client": client,
                "period_month": period_month,
                "standard_salary": round2(employee.standard_salary),
                "attended_days": attended,
                "paid_leaves": paid_leaves,
                "total_days": total_days,
                "earned_salary": result["earnedSalary"],
                "earned_salary_overridden": False,
                "basic": split["basic"],
                "hra": split["hra"],
                "allowances": split["allowances"],
                "additional_earnings": additional,
                "deductions": total_deductions,
                "advance_recovery": advance,
                "net_payable": result["netPayable"],
                "status": "In Progress",
            },
        )

        PayslipComponent.objects.filter(payslip=payslip).delete()
        if shortfall_deduction > ZERO:
            PayslipComponent.objects.create(
                client=client,
                payslip=payslip,
                name=f"Working Hours Shortfall ({total_shortfall_hours:.1f}h short of 8h)",
                amount=shortfall_deduction,
                kind="deduction",
            )
        if split["components"]:
            PayslipComponent.objects.bulk_create(
                [
                    PayslipComponent(
                        client=client,
                        payslip=payslip,
                        name=component["name"],
                        kind=component["kind"],
                        amount=component["amount"],
                    )
                    for component in split["components"]
                ]
            )
        payslips.append(payslip)

    run.status = "Ready for Review"
    run.save(update_fields=["status", "updated_at"])
    return {"run": run, "payslips": payslips}


def _month_end(first_of_month):
    if first_of_month.month == 12:
        return date(first_of_month.year, 12, 31)
    return date(first_of_month.year, first_of_month.month + 1, 1) - timedelta(days=1)


def _pending_advance_instalment(employee):
    """An advance is recovered from the month it is applied to (api.md §11.4)."""
    advance = SalaryAdvance.objects.filter(
        employee=employee, status="Active", deleted_at__isnull=True
    ).first()
    if advance is None:
        return ZERO
    outstanding = D(advance.amount) - D(advance.recovered_amount)
    if outstanding <= ZERO:
        return ZERO
    instalment = round2(D(advance.amount) / Decimal(max(advance.installments, 1)))
    return min(instalment, round2(outstanding))


@transaction.atomic
def recompute_payslip(payslip, *, earned_salary_override=None, user=None):
    """``PATCH /hrms/payroll/{id}/`` -- adjust before approval."""
    if payslip.status in ("Approved", "Paid"):
        raise Conflict(
            "An approved payslip cannot be adjusted.", code=Codes.ALREADY_DONE
        )

    result = compute_salary(
        standard_salary=payslip.standard_salary,
        total_days=payslip.total_days,
        attended_days=payslip.attended_days,
        paid_leaves=payslip.paid_leaves,
        additional_earnings=payslip.additional_earnings,
        deductions=payslip.deductions,
        advance=payslip.advance_recovery,
        earned_salary_override=earned_salary_override,
    )
    payslip.earned_salary = result["earnedSalary"]
    payslip.earned_salary_overridden = result["earnedSalaryOverridden"]
    payslip.net_payable = result["netPayable"]
    payslip.save(
        update_fields=["earned_salary", "earned_salary_overridden", "net_payable", "updated_at"]
    )

    if result["earnedSalaryOverridden"]:
        # api.md §11.4 -- keep the escape hatch, and log it.
        record_audit(
            client=payslip.client_id,
            actor=user,
            action="payslip_override",
            entity_type="Payslip",
            entity_id=payslip.id,
            entity_label=f"{payslip.employee.employee_code} {payslip.period_month:%b %Y}",
            description=f"Earned salary manually set to {result['earnedSalary']}",
        )
    return payslip


def _has_accrual(payslip):
    """Approval posted the accrual: payslip.journal_entry is that entry."""
    entry = payslip.journal_entry if payslip.journal_entry_id else None
    return entry is not None and entry.status != "Reversed" and entry.reversal_of_id is None


@transaction.atomic
def approve_payslip(payslip, *, user=None):
    """Approve and accrue (accrual basis, db.md §11.4): the salary cost and
    Salaries Payable are booked in the payroll month, not when paid."""
    from apps.accounting import services as ledger

    payslip = Payslip.objects.select_for_update().get(pk=payslip.pk)
    if payslip.status == "Paid":
        raise Conflict("This payslip is already paid.", code=Codes.ALREADY_DONE)
    payslip.status = "Approved"
    fields = ["status", "updated_at"]
    if not _has_accrual(payslip):
        try:
            entry = ledger.post_payroll_accrual(payslip, user=user)
            if entry is not None:
                payslip.journal_entry = entry
                fields.append("journal_entry")
        except Exception:
            pass
    payslip.save(update_fields=fields)
    return payslip


@transaction.atomic
def mark_payslip_paid(payslip, *, payment_date=None, bank_account=None, user=None):
    """Posts the salary payment -- payroll is the one HRMS table that
    touches accounts (db.md §11.4).

    An accrued payslip only clears Salaries Payable against the bank. One
    approved before approval accrued gets the original one-shot entry.
    """
    from apps.accounting import services as ledger

    if payslip.status == "Paid":
        raise Conflict("This payslip is already paid.", code=Codes.ALREADY_DONE)
    if payslip.status != "Approved":
        raise Conflict(
            "Approve the payslip before marking it paid.", code=Codes.NOT_FINALIZED
        )

    payslip.status = "Paid"
    payslip.paid_amount = payslip.net_payable
    payslip.payment_date = payment_date or timezone.localdate()
    payslip.bank_account = bank_account
    payslip.save(
        update_fields=["status", "paid_amount", "payment_date", "bank_account", "updated_at"]
    )

    try:
        if _has_accrual(payslip):
            ledger.post_payroll_payment(payslip, user=user)
        else:
            entry = ledger.post_payroll(payslip, user=user)
            if entry is not None:
                payslip.journal_entry = entry
                payslip.save(update_fields=["journal_entry", "updated_at"])
    except Exception:
        pass

    try:
        _recover_advance(payslip)
    except Exception:
        pass
    return payslip


def _advance_funding(advance):
    """The bank/cash account the advance was originally paid from, if posted."""
    from types import SimpleNamespace

    from apps.accounting.models import JournalLine

    line = (
        JournalLine.objects.filter(
            journal_entry__client_id=advance.client_id,
            journal_entry__source_document_type="SalaryAdvance",
            journal_entry__source_document_id=advance.id,
            journal_entry__reversal_of__isnull=True,
            credit__gt=0,
        )
        .exclude(account__system_key="employee_advances")
        .select_related("account")
        .order_by("journal_entry__created_at")
        .first()
    )
    return SimpleNamespace(account=line.account) if line is not None else None


@transaction.atomic
def sync_salary_advance_ledger(advance, *, bank_account=None, user=None):
    """Make the ledger match the advance (db.md §11.4).

    The disbursement is Dr Employee Advances / Cr Bank; payroll recovery
    already credits Employee Advances, so without this the account runs
    negative. A written-off balance moves to Salaries and Wages. Any change
    reverses what was posted and posts the current picture; a deleted
    advance is just reversed.
    """
    from apps.accounting import services as ledger

    funding = bank_account or _advance_funding(advance)
    ledger.reverse_document_entries(
        client_id=advance.client_id,
        source_document_type="SalaryAdvance",
        source_document_id=advance.id,
        user=user,
    )
    if advance.deleted_at is not None:
        return None
    entry = ledger.post_salary_advance(advance, bank_account=funding, user=user)
    if advance.status == "Written Off":
        ledger.post_salary_advance_writeoff(advance, user=user)
    return entry


def _recover_advance(payslip):
    if D(payslip.advance_recovery) <= ZERO:
        return
    advance = SalaryAdvance.objects.select_for_update().filter(
        employee=payslip.employee, status="Active", deleted_at__isnull=True
    ).first()
    if advance is None:
        return
    advance.recovered_amount = round2(
        D(advance.recovered_amount) + D(payslip.advance_recovery)
    )
    if advance.recovered_amount >= D(advance.amount):
        advance.status = "Recovered"
    advance.save(update_fields=["recovered_amount", "status", "updated_at"])


def payroll_summary(client_id, period_month=None):
    """``GET /hrms/payroll/summary/`` -- gross, deductions, net, headcount."""
    queryset = Payslip.objects.filter(client_id=client_id, deleted_at__isnull=True)
    if period_month:
        queryset = queryset.filter(period_month=period_month.replace(day=1))

    rows = queryset.aggregate(
        headcount=Count("id"),
        gross=Sum("earned_salary"),
        additional=Sum("additional_earnings"),
        deductions=Sum("deductions"),
        advances=Sum("advance_recovery"),
        net=Sum("net_payable"),
        paid=Sum("paid_amount"),
    )
    return {
        "headcount": rows["headcount"] or 0,
        "gross": round2(rows["gross"] or 0),
        "additionalEarnings": round2(rows["additional"] or 0),
        "deductions": round2(rows["deductions"] or 0),
        "advanceRecovery": round2(rows["advances"] or 0),
        "net": round2(rows["net"] or 0),
        "paid": round2(rows["paid"] or 0),
    }


# ---------------------------------------------------------------------------
# Derived read-time statuses (db.md §12)
# ---------------------------------------------------------------------------
def document_status(hr_document, today=None):
    """``Valid | Expiring Soon | Expired``, derived from ``valid_until``."""
    today = today or timezone.localdate()
    if hr_document.valid_until is None:
        return "Valid"
    if hr_document.valid_until < today:
        return "Expired"
    if (hr_document.valid_until - today).days <= (hr_document.expiring_soon_days or 30):
        return "Expiring Soon"
    return "Valid"


def acknowledgement_status(acknowledgement, policy, today=None):
    """``Pending | Acknowledged | Overdue`` -- overdue is derived from the
    effective date plus the ack window (api.md §11.8)."""
    if acknowledgement.acknowledged_at is not None:
        return "Acknowledged"
    today = today or timezone.localdate()
    if policy.effective_date is None:
        return "Pending"
    deadline = policy.effective_date + timedelta(days=policy.ack_window_days or 14)
    return "Overdue" if today > deadline else "Pending"


def calendar_time_label(event):
    """api.md §11.8 -- ``time`` is a display string the server derives.

    ``"10:00 AM - 11:30 AM"``, ``"Full Day"`` or ``"Multi-Day"``.
    """
    if event.ends_at and event.starts_at.date() != event.ends_at.date():
        return "Multi-Day"
    if event.all_day:
        return "Full Day"
    start = timezone.localtime(event.starts_at).strftime("%I:%M %p").lstrip("0")
    if event.ends_at is None:
        return start
    end = timezone.localtime(event.ends_at).strftime("%I:%M %p").lstrip("0")
    return f"{start} - {end}"


def org_chart(client_id):
    """``GET /hrms/org-chart/`` -- recursive hierarchy walk over ``manager_id``.

    Arranges chart so that Admin Super User is at the top of the chart (root),
    and CEO / Admin is placed directly under the Super User.
    """
    from apps.accounts.models import User

    # Current staff only: people who have left are not part of the structure.
    employees = list(
        Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)
        .exclude(status__in=["Resigned", "Terminated"])
        .select_related("designation", "department", "location")
        .only(
            "id", "name", "employee_code", "manager_id", "avatar_url", "email",
            "phone", "status", "designation__name", "department__name", "location__name",
        )
        .order_by("name")
    )
    names = {employee.id: employee.name for employee in employees}
    nodes = {
        employee.id: {
            "id": str(employee.id),
            "name": employee.name,
            "employeeCode": employee.employee_code,
            "designation": employee.designation.name if employee.designation_id else None,
            "department": employee.department.name if employee.department_id else None,
            "avatar": employee.avatar_url,
            "email": employee.email,
            "phone": employee.phone,
            "status": employee.status,
            "location": employee.location.name if employee.location_id else None,
            "manager": names.get(employee.manager_id),
            "reports": [],
        }
        for employee in employees
    }

    superuser_emails = set(
        User.objects.filter(client_id=client_id, is_superuser=True).values_list("email", flat=True)
    )

    roots = []
    for employee in employees:
        node = nodes[employee.id]
        parent = nodes.get(employee.manager_id) if employee.manager_id else None
        if parent is not None and parent is not node:
            parent["reports"].append(node)
        else:
            roots.append(node)

    def is_superuser_node(node):
        email = (node.get("email") or "").lower()
        if email in superuser_emails:
            return True
        desig = (node.get("designation") or "").lower()
        name = (node.get("name") or "").lower()
        if "super user" in desig or "superuser" in desig:
            return True
        if "super user" in name or "superuser" in name or name == "admin super user":
            return True
        return False

    def is_ceo_admin_node(node):
        if is_superuser_node(node):
            return False
        desig = (node.get("designation") or "").lower()
        name = (node.get("name") or "").lower()
        if "ceo" in desig or "administrator" in desig or "admin" in desig:
            return True
        if "ceo" in name or "administrator" in name or "admin" in name:
            return True
        return False

    su_node = None
    ceo_node = None
    for n in nodes.values():
        if is_superuser_node(n) and su_node is None:
            su_node = n
        elif is_ceo_admin_node(n) and ceo_node is None:
            ceo_node = n

    # Hierarchy arrangement:
    # 1. Admin Super User is top of the chart (root)
    # 2. CEO / Admin is placed directly under Super User
    if su_node is not None:
        if ceo_node is not None and ceo_node is not su_node:
            if ceo_node not in su_node["reports"]:
                if ceo_node in roots:
                    roots.remove(ceo_node)
                for parent_node in nodes.values():
                    if parent_node is not su_node and ceo_node in parent_node["reports"]:
                        parent_node["reports"].remove(ceo_node)
                su_node["reports"].insert(0, ceo_node)

        if su_node in roots:
            roots.remove(su_node)
            roots.insert(0, su_node)
        else:
            for parent_node in nodes.values():
                if su_node in parent_node["reports"]:
                    parent_node["reports"].remove(su_node)
            roots.insert(0, su_node)

        # Attach any other unassigned root employees under CEO/Admin or Super User
        target_parent = ceo_node if ceo_node is not None else su_node
        remaining_roots = [r for r in roots if r is not su_node]
        for r in remaining_roots:
            if r is not target_parent and r not in target_parent["reports"]:
                roots.remove(r)
                target_parent["reports"].append(r)

    def sort_reports(node):
        def rank(child):
            desig = (child.get("designation") or "").lower()
            name = (child.get("name") or "").lower()
            if is_ceo_admin_node(child):
                return (0, name)
            if "director" in desig or "head" in desig:
                return (1, name)
            if "manager" in desig:
                return (2, name)
            if "officer" in desig or "lead" in desig or "inspector" in desig:
                return (3, name)
            return (4, name)

        node["reports"].sort(key=rank)
        for child in node["reports"]:
            sort_reports(child)

    for r in roots:
        sort_reports(r)

    return roots


def assert_no_manager_cycle(employee, manager_id):
    """db.md §11.1 -- reject any manager reachable from the employee."""
    if manager_id is None:
        return
    if str(manager_id) == str(employee.id):
        raise ValidationFailed(
            "An employee cannot report to themselves.",
            field_errors={"managerId": ["Cannot be self."]},
        )

    seen = {employee.id}
    current = manager_id
    while current is not None:
        if current in seen:
            raise ValidationFailed(
                "That reporting line would create a loop.",
                field_errors={"managerId": ["Creates a reporting cycle."]},
            )
        seen.add(current)
        current = (
            Employee.objects.filter(pk=current).values_list("manager_id", flat=True).first()
        )


def process_employee_separation(employee, *, separation_type="Resigned", last_working_day=None, user=None):
    """Lifecycle hook for Resignation/Termination.
    Updates employee status, saves last_working_day, deactivates login User & revokes sessions.
    """
    from apps.accounts.models import User, UserSession
    status_label = "Terminated" if str(separation_type).lower() == "terminated" else "Resigned"
    employee.status = status_label
    if last_working_day:
        employee.last_working_day = last_working_day
    employee.save(update_fields=["status", "last_working_day", "updated_at"])

    # Deactivate linked user login account and revoke active sessions
    if employee.email:
        users = User.objects.filter(
            client_id=employee.client_id,
            email__iexact=str(employee.email).strip(),
            deleted_at__isnull=True,
        )
        user_ids = list(users.values_list("id", flat=True))
        if user_ids:
            users.update(is_active=False, status="Inactive", updated_at=timezone.now())
            UserSession.objects.filter(
                user_id__in=user_ids,
                revoked_at__isnull=True,
            ).update(
                revoked_at=timezone.now(),
                revoked_reason=f"employee_{status_label.lower()}",
            )

    return employee

def sync_employee_from_user(user, raw_data=None):
    """Ensure an Employee record in HRMS exists and is linked for this user."""
    if not user or not user.client_id:
        return None
    from apps.hrms.models import Employee, Department, Designation, Location
    from apps.core.numbering import allocate_number

    raw = raw_data or {}
    client = user.client
    client_id = user.client_id

    # 1. Resolve Department
    dept_name = user.department or raw.get("department") or raw.get("dept")
    dept = None
    if dept_name and str(dept_name).strip():
        dept = Department.objects.filter(
            client_id=client_id, name__iexact=str(dept_name).strip(), deleted_at__isnull=True
        ).first()
        if not dept:
            dept = Department.objects.create(
                client_id=client_id, name=str(dept_name).strip()
            )

    # 2. Resolve Designation from role or designation name
    role = user.role
    if not role and user.role_id:
        from apps.accounts.models import Role
        role = Role.objects.filter(pk=user.role_id).first()

    desig_name = ""
    if role and role.name:
        desig_name = str(role.name).strip()
    elif raw.get("designation"):
        desig_name = str(raw.get("designation")).strip()
    elif raw.get("role"):
        desig_name = str(raw.get("role")).strip()
    else:
        desig_name = "Employee"

    desig = None
    if desig_name:
        desig = Designation.objects.filter(
            client_id=client_id, name__iexact=desig_name, deleted_at__isnull=True
        ).first()
        if not desig:
            desig = Designation.objects.create(
                client_id=client_id, name=desig_name
            )

    # 3. Resolve Location
    loc_name = user.location or raw.get("location")
    loc = None
    if loc_name and str(loc_name).strip():
        loc = Location.objects.filter(
            client_id=client_id, name__iexact=str(loc_name).strip(), deleted_at__isnull=True
        ).first()
        if not loc:
            loc = Location.objects.create(
                client_id=client_id, name=str(loc_name).strip()
            )

    # Determine desired status
    emp_status = "Active" if user.status in ("Active", "Invited") and user.is_active else "Inactive"

    # 4. Check if user already has an employee record
    if user.employee_id:
        emp = user.employee
        if emp and not emp.deleted_at:
            updates = []
            if user.name and emp.name != user.name:
                emp.name = user.name
                updates.append("name")
            if user.phone and emp.phone != user.phone:
                emp.phone = user.phone
                updates.append("phone")
            if user.email and emp.email != user.email:
                emp.email = user.email
                updates.append("email")
            if dept and emp.department_id != dept.id:
                emp.department = dept
                updates.append("department")
            if desig and emp.designation_id != desig.id:
                emp.designation = desig
                updates.append("designation")
            if loc and emp.location_id != loc.id:
                emp.location = loc
                updates.append("location")
            if emp.status != emp_status:
                emp.status = emp_status
                updates.append("status")
            if updates:
                emp.save(update_fields=list(dict.fromkeys(updates)))
            return emp

    # 5. Check if an Employee with matching email exists
    emp = Employee.objects.filter(
        client_id=client_id, email__iexact=user.email, deleted_at__isnull=True
    ).first()

    if not emp:
        desired_code = raw.get("employeeId") or raw.get("employeeCode") or raw.get("empId")
        code = None
        if desired_code:
            desired_code = str(desired_code).strip()
            if not Employee.objects.filter(client_id=client_id, employee_code=desired_code).exists():
                code = desired_code

        if not code:
            try:
                code = allocate_number(client, "EMP")
            except Exception:
                code = f"EMP-{(Employee.objects.filter(client_id=client_id).count() + 1):03d}"

        emp = Employee.objects.create(
            client_id=client_id,
            employee_code=code,
            name=user.name or user.email,
            email=user.email,
            phone=user.phone or "",
            avatar_url=user.avatar_url or "",
            department=dept,
            designation=desig,
            location=loc,
            joining_date=user.joined_date or timezone.localdate(),
            status=emp_status,
        )
    else:
        updates = []
        if dept and emp.department_id != dept.id:
            emp.department = dept
            updates.append("department")
        if desig and emp.designation_id != desig.id:
            emp.designation = desig
            updates.append("designation")
        if loc and emp.location_id != loc.id:
            emp.location = loc
            updates.append("location")
        if user.name and emp.name != user.name:
            emp.name = user.name
            updates.append("name")
        if emp.status != emp_status:
            emp.status = emp_status
            updates.append("status")
        if updates:
            emp.save(update_fields=list(dict.fromkeys(updates)))

    user.employee = emp
    user.save(update_fields=["employee"])
    return emp


def sync_user_from_employee(employee, client_id=None, actor=None):
    """Ensure a User login account in Administration exists and is linked for this Employee."""
    if not employee:
        return None
    from apps.accounts.models import User, Role

    cid = client_id or employee.client_id
    client = getattr(employee, "client", None)
    if not client:
        from apps.accounts.models import Client
        client = Client.objects.filter(id=cid).first()

    email = str(employee.email or "").strip().lower()
    if not email:
        code_part = str(employee.employee_code or employee.id).lower().replace("-", "")
        email = f"emp.{code_part}@oscar.local"
        if not employee.email:
            employee.email = email
            employee.save(update_fields=["email"])

    department = employee.department.name if employee.department_id else ""
    location = employee.location.name if employee.location_id else ""
    desig_name = employee.designation.name if employee.designation_id else ""

    # Find matching role
    role = None
    if desig_name:
        role = Role.objects.filter(
            client_id=cid, name__iexact=desig_name
        ).first()
    if not role:
        role = Role.objects.filter(
            client_id=cid, code__in=["EMP", "EM"]
        ).first() or Role.objects.filter(
            client_id=cid, name__icontains="Employee"
        ).first()
    if not role:
        role = Role.objects.filter(client_id=cid).first()

    existing = User.objects.filter(
        client_id=cid, email__iexact=email, deleted_at__isnull=True
    ).first()
    if existing is None and employee.id:
        existing = User.objects.filter(
            client_id=cid, employee_id=employee.id, deleted_at__isnull=True
        ).first()

    is_active = employee.status in ("Active", "Probation", "On Leave")

    if existing is not None:
        updates = []
        if existing.employee_id != employee.id:
            existing.employee = employee
            updates.append("employee")
        if not existing.role_id and role:
            existing.role = role
            updates.append("role")
        for field, value in (
            ("name", employee.name),
            ("phone", employee.phone),
            ("department", department),
            ("location", location),
            ("avatar_url", employee.avatar_url),
            ("joined_date", employee.joining_date),
        ):
            if value and getattr(existing, field) != value:
                setattr(existing, field, value)
                updates.append(field)
        if not is_active:
            if existing.is_active or existing.status != "Inactive":
                existing.is_active = False
                existing.status = "Inactive"
                updates.extend(["status", "is_active"])
        else:
            if existing.status != "Active" or not existing.is_active:
                existing.status = "Active"
                existing.is_active = True
                updates.extend(["status", "is_active"])
        if updates:
            updates.append("updated_at")
            existing.save(update_fields=list(dict.fromkeys(updates)))
        return existing

    user = User.objects.create_user(
        email=email,
        client=client,
        name=employee.name or email.split("@")[0],
        phone=employee.phone or "",
        employee=employee,
        role=role,
        department=department,
        location=location,
        avatar_url=employee.avatar_url or "",
        joined_date=employee.joining_date or timezone.localdate(),
        status="Active" if is_active else "Inactive",
    )
    if not is_active:
        user.is_active = False
        user.save(update_fields=["is_active"])
    return user


def sync_all_users_and_employees(client_id):
    """Bidirectional backfill pass ensuring all users have employees and vice versa."""
    if not client_id:
        return
    from apps.accounts.models import User
    from apps.hrms.models import Employee

    # 1. Any User missing employee or where employee FK is null
    unlinked_users = User.objects.filter(
        client_id=client_id, deleted_at__isnull=True, employee__isnull=True
    )
    for u in unlinked_users:
        try:
            sync_employee_from_user(u)
        except Exception:
            pass

    # 2. Any Employee not linked to any User
    linked_emp_ids = User.objects.filter(
        client_id=client_id, deleted_at__isnull=True, employee_id__isnull=False
    ).values_list("employee_id", flat=True)
    unlinked_employees = Employee.objects.filter(
        client_id=client_id, deleted_at__isnull=True
    ).exclude(id__in=linked_emp_ids)
    for emp in unlinked_employees:
        try:
            sync_user_from_employee(emp, client_id=client_id)
        except Exception:
            pass



# ── Attendance Punch Services ───────────────────────────────────────────────
from decimal import Decimal
from datetime import datetime, time, timedelta
from django.utils import timezone
from django.db import transaction
from apps.core.exceptions import ValidationFailed
from apps.hrms.models import Attendance, AttendancePunch, AttendanceBreak, AttendanceRegularization, Employee, Department, Designation


def format_time_ampm(dt):
    if not dt:
        return None
    local_dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return local_dt.strftime("%I:%M %p")


def format_duration_hm(seconds):
    if not seconds or seconds < 0:
        return "00h 00m"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"{h:02d}h {m:02d}m"


def is_employee_holiday(client_id, date, employee=None):
    """Check if date is a published holiday for tenant and employee scope."""
    from apps.hrms.models import Holiday, Employee
    qs = Holiday.objects.filter(
        client_id=client_id,
        date=date,
        status="Published",
        deleted_at__isnull=True,
    )
    if not qs.exists():
        return False

    if employee is None:
        return True

    if isinstance(employee, (int, str)):
        employee = Employee.objects.filter(client_id=client_id, pk=employee).first()

    # If any published holiday on this date is global (no location and no department)
    if qs.filter(location__isnull=True, department__isnull=True).exists():
        return True

    # Or matches employee's location or department
    if employee:
        if employee.location_id and qs.filter(location_id=employee.location_id).exists():
            return True
        if employee.department_id and qs.filter(department_id=employee.department_id).exists():
            return True

    return False



def is_employee_weekly_off(client_id, date, employee=None, schedule=None):
    """Check if date is a weekly off day for the employee's schedule."""
    if schedule is None:
        schedule = get_employee_schedule(client_id, employee, work_date=date)

    weekday = date.weekday()
    if schedule and schedule.weekly_offs is not None and len(schedule.weekly_offs) > 0:
        return weekday in schedule.weekly_offs

    if schedule and schedule.working_days is not None and len(schedule.working_days) > 0:
        return weekday not in schedule.working_days

    # Fallback to WorkingDay model config or standard Mon-Fri (5=Sat, 6=Sun)
    from apps.hrms.models import WorkingDay
    wd = WorkingDay.objects.filter(client_id=client_id, weekday=weekday, deleted_at__isnull=True).first()
    if wd is not None:
        return not wd.is_working

    return weekday in (5, 6)



def get_employee_schedule(client_id, employee, work_date=None):
    """In Oscar, shifts and schedules resolve via flexibility_policy."""
    return None

def format_duration_hm(seconds):
    if not seconds or seconds < 0:
        return "00h 00m"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours:02d}h {minutes:02d}m"


def format_time_ampm(dt):
    if not dt:
        return None
    local_dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return local_dt.strftime("%I:%M %p")


def calculate_punch_metrics(client_id, employee, work_date, punches=None, breaks=None, now=None):
    """Calculate working hours, pairs, breaks, late minutes, overtime from punch records."""
    if punches is None:
        punches = list(
            AttendancePunch.objects.filter(
                client_id=client_id,
                employee=employee,
                work_date=work_date,
                deleted_at__isnull=True,
            ).order_by("punch_time")
        )

    if breaks is None:
        breaks = list(
            AttendanceBreak.objects.filter(
                client_id=client_id,
                employee=employee,
                work_date=work_date,
                deleted_at__isnull=True,
            ).order_by("break_in_time")
        )

    if now is None:
        now = timezone.now()

    first_punch = punches[0] if punches else None
    last_punch = punches[-1] if punches else None
    first_in = next((p for p in punches if p.punch_type == "IN"), None)
    last_out = next((p for p in reversed(punches) if p.punch_type == "OUT"), None)

    active_break = next((b for b in breaks if b.break_out_time is None), None)
    day_completed = bool(last_punch and last_punch.punch_type == "OUT")
    is_punched_in = bool(last_punch and last_punch.punch_type in ("IN", "BREAK_IN", "BREAK_OUT"))
    is_on_break = bool(is_punched_in and (active_break is not None or last_punch.punch_type == "BREAK_IN"))
    is_working = bool(is_punched_in and not is_on_break)

    in_punches = [p for p in punches if p.punch_type == "IN"]

    # 2. Total Break Duration (completed breaks + active break if currently on break)
    total_completed_break_seconds = sum(b.duration_seconds for b in breaks if b.break_out_time is not None)
    active_break_seconds = 0
    if is_on_break and active_break:
        active_break_seconds = max(0, int((now - active_break.break_in_time).total_seconds()))
    total_break_seconds = int(total_completed_break_seconds + active_break_seconds)
    total_break_minutes = total_break_seconds // 60

    # 1. Total Attendance Duration & Net Working Duration
    # Handle multi-session / split shifts:
    if len(in_punches) > 1:
        session_seconds = 0
        current_session_in = None
        for p in punches:
            if p.punch_type == "IN":
                if current_session_in is None:
                    current_session_in = p.punch_time
            elif p.punch_type == "OUT" and current_session_in:
                session_seconds += max(0, int((p.punch_time - current_session_in).total_seconds()))
                current_session_in = None
        if current_session_in:
            session_seconds += max(0, int((now - current_session_in).total_seconds()))

        total_attendance_seconds = session_seconds
        net_working_seconds = max(0, int(session_seconds - total_break_seconds))
    elif first_in:
        att_end = last_out.punch_time if last_out else now
        total_attendance_seconds = max(0, int((att_end - first_in.punch_time).total_seconds()))
        net_working_seconds = max(0, int(total_attendance_seconds - total_break_seconds))
    else:
        total_attendance_seconds = 0
        net_working_seconds = 0

    working_hours = round(Decimal(net_working_seconds) / Decimal(3600), 2)
    total_attendance_hours = round(Decimal(total_attendance_seconds) / Decimal(3600), 2)
    total_break_hours = round(Decimal(total_break_seconds) / Decimal(3600), 2)
    completed_hours = round(Decimal(max(0, (last_out.punch_time - first_in.punch_time).total_seconds() - total_completed_break_seconds)) / Decimal(3600), 2) if (first_in and last_out) else working_hours

    breaks_display = [
        {
            "id": str(b.id),
            "breakIn": b.break_in_time.isoformat(),
            "breakOut": b.break_out_time.isoformat() if b.break_out_time else None,
            "breakInTime": format_time_ampm(b.break_in_time),
            "breakOutTime": format_time_ampm(b.break_out_time) if b.break_out_time else None,
            "duration": format_duration_hm(b.duration_seconds) if b.break_out_time else format_duration_hm(max(0, int((now - b.break_in_time).total_seconds()))),
            "durationSeconds": b.duration_seconds if b.break_out_time else max(0, int((now - b.break_in_time).total_seconds())),
            "durationMinutes": b.duration_minutes if b.break_out_time else max(0, int((now - b.break_in_time).total_seconds())) // 60,
            "breakType": b.break_type,
            "remark": b.remark,
        }
        for b in breaks
    ]

    pairs = []
    pairs_display = []
    if breaks and first_in:
        interval_start = first_in.punch_time
        for b in breaks:
            if b.break_in_time > interval_start:
                dur = int((b.break_in_time - interval_start).total_seconds())
                pairs_display.append({
                    "inTime": format_time_ampm(interval_start),
                    "outTime": format_time_ampm(b.break_in_time),
                    "duration": format_duration_hm(dur),
                    "durationSeconds": dur,
                })
            interval_start = b.break_out_time if b.break_out_time else None
            if not interval_start:
                break
        if interval_start:
            end_t = last_out.punch_time if last_out else now
            if end_t > interval_start:
                dur = int((end_t - interval_start).total_seconds())
                pairs_display.append({
                    "inTime": format_time_ampm(interval_start),
                    "outTime": format_time_ampm(end_t) if last_out else "Active",
                    "duration": format_duration_hm(dur),
                    "durationSeconds": dur,
                })
    else:
        current_in = None
        for p in punches:
            if p.punch_type == "IN":
                current_in = p
            elif p.punch_type == "OUT" and current_in:
                delta = (p.punch_time - current_in.punch_time).total_seconds()
                if delta > 0:
                    pairs.append((current_in, p, int(delta)))
                    pairs_display.append({
                        "inTime": format_time_ampm(current_in.punch_time),
                        "outTime": format_time_ampm(p.punch_time),
                        "duration": format_duration_hm(delta),
                        "durationSeconds": int(delta),
                    })
                current_in = None

    schedule = get_employee_schedule(client_id, employee, work_date=work_date)
    policy = flexibility_policy(client_id)
    shift_start = (schedule.shift_start if schedule else None) or _parse_time(policy.get("shiftStart"), time(9, 30))
    shift_end = (schedule.shift_end if schedule else None) or _parse_time(policy.get("shiftEnd"), time(18, 30))
    grace = int(policy.get("graceMinutes") or 0)
    half_day_threshold = Decimal(str(policy.get("halfDayThresholdHours") or 4))
    full_day_hours = Decimal(str(schedule.working_hours if schedule else (policy.get("fullDayHours") or 8)))

    late_minutes = 0
    if first_in:
        local_first_in = (
            timezone.localtime(first_in.punch_time)
            if timezone.is_aware(first_in.punch_time)
            else first_in.punch_time
        )
        shift_start_dt = datetime.combine(local_first_in.date(), shift_start)
        if policy.get("lateAfter"):
            late_cutoff = _parse_time(policy.get("lateAfter"), shift_start)
            grace_dt = datetime.combine(local_first_in.date(), late_cutoff)
        else:
            grace_dt = shift_start_dt + timedelta(minutes=grace)
        if local_first_in.replace(tzinfo=None) > grace_dt:
            diff = (local_first_in.replace(tzinfo=None) - shift_start_dt).total_seconds()
            late_minutes = max(0, int(diff // 60))

    early_leaving_minutes = 0
    if last_out:
        local_last_out = (
            timezone.localtime(last_out.punch_time)
            if timezone.is_aware(last_out.punch_time)
            else last_out.punch_time
        )
        shift_end_dt = datetime.combine(local_last_out.date(), shift_end)
        if local_last_out.replace(tzinfo=None) < shift_end_dt:
            diff = (shift_end_dt - local_last_out.replace(tzinfo=None)).total_seconds()
            early_leaving_minutes = max(0, int(diff // 60))

    is_early_out = bool(last_out and (early_leaving_minutes > 0 or working_hours < full_day_hours))

    overtime_threshold_hours = Decimal(str(policy.get("overtimeStartsAfterHours") or full_day_hours))
    overtime_threshold_seconds = int(overtime_threshold_hours * 3600)
    overtime_seconds = max(0, int(net_working_seconds - overtime_threshold_seconds))
    overtime_hours = round(Decimal(overtime_seconds) / Decimal(3600), 2)

    if not punches:
        if is_employee_holiday(client_id, work_date, employee):
            status = "Holiday"
        elif is_employee_weekly_off(client_id, work_date, employee, schedule=schedule):
            status = "Week Off"
        else:
            status = "Absent"
    elif is_punched_in:
        status = "Late" if late_minutes > 0 else "Present"
    elif working_hours == 0:
        status = "Absent"
    elif working_hours < half_day_threshold:
        status = "Half Day"
    elif late_minutes > 0:
        status = "Late"
    else:
        status = "Present"

    return {
        "punches": punches,
        "breaks": breaks,
        "is_punched_in": is_punched_in,
        "is_on_break": is_on_break,
        "is_working": is_working,
        "day_completed": day_completed,
        "active_break": active_break,
        "total_working_seconds": net_working_seconds,
        "total_completed_seconds": int(total_completed_break_seconds),
        "total_attendance_seconds": total_attendance_seconds,
        "formatted_attendance_time": format_duration_hm(total_attendance_seconds),
        "total_attendance_hours": total_attendance_hours,
        "total_break_seconds": total_break_seconds,
        "formatted_break_time": format_duration_hm(total_break_seconds),
        "total_break_minutes": total_break_minutes,
        "total_break_hours": total_break_hours,
        "formatted_working_time": format_duration_hm(net_working_seconds),
        "working_hours": working_hours,
        "completed_hours": completed_hours,
        "first_punch": first_punch,
        "last_punch": last_punch,
        "first_in": first_in,
        "last_out": last_out,
        "late_minutes": late_minutes,
        "early_leaving_minutes": early_leaving_minutes,
        "is_early_out": is_early_out,
        "shift_start": policy.get("shiftStart", "09:30"),
        "shift_end": policy.get("shiftEnd", "18:30"),
        "full_day_hours": float(full_day_hours),
        "half_day_threshold_hours": float(half_day_threshold),
        "overtime_seconds": overtime_seconds,
        "overtime_hours": overtime_hours,
        "status": status,
        "breaks_display": breaks_display,
        "pairs_display": pairs_display,
    }


def get_today_punch_status(client, employee, work_date=None):
    """Retrieve punch status, punch timeline, break timeline, working duration, and metrics."""
    client_id = getattr(client, "id", client) or getattr(employee, "client_id", None) or getattr(getattr(employee, "client", None), "id", None)
    if work_date is None:
        work_date = timezone.localdate()
        # Automatically close any sessions where employee forgot to punch out at day end
        auto_punch_out_pending_sessions(client=client_id, employee=employee, as_of_date=work_date)
        # Check if an overnight shift started yesterday is still active
        yesterday = work_date - timedelta(days=1)
        yesterday_in = AttendancePunch.objects.filter(
            client_id=client_id, employee=employee, work_date=yesterday, punch_type="IN", deleted_at__isnull=True
        ).order_by("-punch_time").first()
        yesterday_out = AttendancePunch.objects.filter(
            client_id=client_id, employee=employee, work_date=yesterday, punch_type="OUT", deleted_at__isnull=True
        ).exists()
        today_punches_exist = AttendancePunch.objects.filter(
            client_id=client_id, employee=employee, work_date=work_date, deleted_at__isnull=True
        ).exists()
        if not today_punches_exist and yesterday_in and not yesterday_out:
            now = timezone.now()
            if (now - yesterday_in.punch_time).total_seconds() <= 18 * 3600:
                work_date = yesterday

    punches = list(
        AttendancePunch.objects.filter(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            deleted_at__isnull=True,
        ).order_by("punch_time")
    )
    breaks = list(
        AttendanceBreak.objects.filter(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            deleted_at__isnull=True,
        ).order_by("break_in_time")
    )
    metrics = calculate_punch_metrics(client_id, employee, work_date, punches=punches, breaks=breaks)

    if metrics["day_completed"]:
        status_label = "Punched Out"
    elif metrics["is_on_break"]:
        status_label = "On Break"
    elif metrics["is_punched_in"]:
        status_label = "Working"
    else:
        status_label = "Not Punched In"

    first_punch_time = format_time_ampm(metrics["first_punch"].punch_time) if metrics["first_punch"] else None
    last_punch_time = format_time_ampm(metrics["last_punch"].punch_time) if metrics["last_punch"] else None
    first_in_time = format_time_ampm(metrics["first_in"].punch_time) if metrics["first_in"] else None
    last_out_time = format_time_ampm(metrics["last_out"].punch_time) if metrics["last_out"] else None

    att_row = Attendance.objects.filter(
        client_id=client_id, employee=employee, work_date=work_date
    ).first()

    day_completed = metrics["day_completed"]
    is_punched_in = metrics["is_punched_in"]
    is_on_break = metrics["is_on_break"]
    is_working = metrics["is_working"]

    can_punch_in = bool(not is_punched_in and not day_completed)
    can_punch_out = bool(is_punched_in and not is_on_break)
    can_break_in = bool(is_punched_in and not is_on_break)
    can_break_out = bool(is_punched_in and is_on_break)

    from apps.hrms.models import AttendanceRegularization
    has_pending_regularization = AttendanceRegularization.objects.filter(
        client_id=client_id, employee=employee, work_date=work_date, status="Pending"
    ).exists()

    early_mins = att_row.early_leaving_minutes if att_row else metrics["early_leaving_minutes"]

    active_break = metrics["active_break"]
    active_break_data = None
    if active_break:
        now = timezone.now()
        act_sec = max(0, int((now - active_break.break_in_time).total_seconds()))
        active_break_data = {
            "id": str(active_break.id),
            "breakIn": active_break.break_in_time.isoformat(),
            "breakInTime": format_time_ampm(active_break.break_in_time),
            "activeSeconds": act_sec,
            "formattedDuration": format_duration_hm(act_sec),
        }

    return {
        "has_employee": True,
        "employee": {
            "id": str(employee.id),
            "name": employee.name,
            "employeeCode": employee.employee_code,
            "department": getattr(employee.department, "name", "") if employee.department else "",
            "shift": employee.shift or "General",
        },
        "date": work_date.isoformat(),
        "status": status_label,
        "is_punched_in": is_punched_in,
        "is_on_break": is_on_break,
        "is_working": is_working,
        "day_completed": day_completed,
        "can_punch_in": can_punch_in,
        "can_punch_out": can_punch_out,
        "can_break_in": can_break_in,
        "can_break_out": can_break_out,
        "is_early_out": metrics["is_early_out"],
        "early_leaving_minutes": early_mins,
        "early_minutes": early_mins,
        "shift_start": metrics["shift_start"],
        "shift_end": metrics["shift_end"],
        "full_day_hours": metrics["full_day_hours"],
        "half_day_threshold_hours": metrics["half_day_threshold_hours"],
        "has_pending_regularization": has_pending_regularization,
        "attendance_status": att_row.status if att_row else metrics["status"],
        "first_punch": first_punch_time,
        "last_punch": last_punch_time,
        "first_in": first_in_time,
        "last_out": last_out_time,
        "first_punch_iso": metrics["first_punch"].punch_time.isoformat() if metrics["first_punch"] else None,
        "last_punch_iso": metrics["last_punch"].punch_time.isoformat() if metrics["last_punch"] else None,
        "working_seconds": metrics["total_working_seconds"],
        "net_working_seconds": metrics["total_working_seconds"],
        "formatted_working_time": metrics["formatted_working_time"],
        "working_hours": float(metrics["working_hours"]),
        "attendance_seconds": metrics["total_attendance_seconds"],
        "formatted_attendance_time": metrics["formatted_attendance_time"],
        "attendance_hours": float(metrics["total_attendance_hours"]),
        "total_break_seconds": metrics["total_break_seconds"],
        "formatted_break_time": metrics["formatted_break_time"],
        "total_break_minutes": metrics["total_break_minutes"],
        "break_hours": float(metrics["total_break_hours"]),
        "active_break": active_break_data,
        "late_minutes": metrics["late_minutes"],
        "overtime_seconds": metrics["overtime_seconds"],
        "overtime_hours": float(metrics["overtime_hours"]),
        "breaks": metrics["breaks_display"],
        "punches": [
            {
                "id": str(p.id),
                "punchType": p.punch_type,
                "punchTime": p.punch_time.isoformat(),
                "timeDisplay": format_time_ampm(p.punch_time),
                "source": p.source,
                "remark": p.remark,
            }
            for p in punches
        ],
        "pairs": metrics["pairs_display"],
    }


@transaction.atomic
def record_punch(
    *, client, employee, punch_type, punch_time=None, source="web", remark=None, user=None,
    early_reason=None, request_regularization=False
):
    """Record a Punch In, Break In, Break Out, or Punch Out event with validation, and update Attendance."""
    client_id = getattr(client, "id", client) or getattr(employee, "client_id", None) or getattr(getattr(employee, "client", None), "id", None)
    punch_type = str(punch_type).upper().strip()
    if punch_type not in ("IN", "OUT", "BREAK_IN", "BREAK_OUT"):
        raise ValidationFailed(
            f"Invalid punch type: {punch_type}. Must be 'IN', 'OUT', 'BREAK_IN', or 'BREAK_OUT'.",
            field_errors={"punch_type": ["Must be IN, OUT, BREAK_IN, or BREAK_OUT."]},
        )

    if punch_time is None:
        punch_time = timezone.now()
    elif not timezone.is_aware(punch_time):
        punch_time = timezone.make_aware(punch_time)

    work_date = timezone.localdate(punch_time)

    # Check for overnight shift continuation across midnight:
    # If the user is punching OUT, BREAK_IN, or BREAK_OUT, and there are no IN punches on work_date,
    # check if yesterday had an unclosed shift (within 18 hours).
    if punch_type in ("OUT", "BREAK_IN", "BREAK_OUT"):
        today_in_exists = AttendancePunch.objects.filter(
            client_id=client_id, employee=employee, work_date=work_date, punch_type="IN"
        ).exists()
        if not today_in_exists:
            yesterday = work_date - timedelta(days=1)
            yesterday_in = AttendancePunch.objects.filter(
                client_id=client_id, employee=employee, work_date=yesterday, punch_type="IN"
            ).order_by("-punch_time").first()
            yesterday_out = AttendancePunch.objects.filter(
                client_id=client_id, employee=employee, work_date=yesterday, punch_type="OUT"
            ).exists()
            if yesterday_in and not yesterday_out:
                if (punch_time - yesterday_in.punch_time).total_seconds() <= 18 * 3600:
                    work_date = yesterday

    # Lock existing punches & breaks for this employee today
    existing_punches = list(
        AttendancePunch.objects.select_for_update()
        .filter(client_id=client_id, employee=employee, work_date=work_date)
        .order_by("punch_time")
    )
    existing_breaks = list(
        AttendanceBreak.objects.select_for_update()
        .filter(client_id=client_id, employee=employee, work_date=work_date)
        .order_by("break_in_time")
    )

    first_in = next((p for p in existing_punches if p.punch_type == "IN"), None)
    last_punch = existing_punches[-1] if existing_punches else None
    is_currently_in = bool(last_punch and last_punch.punch_type in ("IN", "BREAK_IN", "BREAK_OUT"))
    has_out = any(p.punch_type == "OUT" for p in existing_punches)
    last_out = next((p for p in reversed(existing_punches) if p.punch_type == "OUT"), None)
    active_break = next((b for b in existing_breaks if b.break_out_time is None), None)
    last_break = existing_breaks[-1] if existing_breaks else None

    # State validation rules:
    if has_out:
        if punch_type in ("BREAK_IN", "BREAK_OUT"):
            raise ValidationFailed(
                "You have already completed your workday for today. Cannot record breaks after punching out.",
                code="DAY_ALREADY_COMPLETED",
            )
        elif punch_type == "IN":
            time_diff = abs((punch_time - last_punch.punch_time).total_seconds()) if last_punch else 999
            if time_diff < 5:
                return last_punch, get_today_punch_status(client_id, employee, work_date=work_date)
            raise ValidationFailed(
                "You have already completed your punch in and punch out for today. Only one punch in and punch out allowed per day.",
                code="DAY_ALREADY_COMPLETED",
            )
        elif punch_type == "OUT":
            time_diff = abs((punch_time - last_out.punch_time).total_seconds()) if last_out else 999
            if time_diff < 10 or last_out:
                return (last_out or last_punch), get_today_punch_status(client_id, employee, work_date=work_date)
            raise ValidationFailed(
                "You have already completed your punch in and punch out for today.",
                code="DAY_ALREADY_COMPLETED",
            )

    # 1. Action: IN (Punch In)
    if punch_type == "IN":
        # Automatically close any prior days where employee forgot to punch out
        auto_punch_out_pending_sessions(client=client_id, employee=employee, as_of_date=work_date)
        if is_currently_in:
            time_diff = abs((punch_time - last_punch.punch_time).total_seconds())
            if time_diff < 5:
                return last_punch, get_today_punch_status(client_id, employee, work_date=work_date)
            raise ValidationFailed(
                "You are already punched in. Please punch out first.",
                code="ALREADY_PUNCHED_IN",
            )

    # 2. Action: BREAK_IN (Start a break)
    elif punch_type == "BREAK_IN":
        if not is_currently_in:
            raise ValidationFailed(
                "Cannot break in before punching in.",
                code="CANNOT_BREAK_IN",
            )
        if active_break:
            time_diff = abs((punch_time - active_break.break_in_time).total_seconds())
            if time_diff < 5:
                matching_punch = next((p for p in reversed(existing_punches) if p.punch_type == "BREAK_IN"), None)
                return (matching_punch or active_break), get_today_punch_status(client_id, employee, work_date=work_date)
            raise ValidationFailed(
                "You are already on break. Please Break Out before starting another break.",
                code="ALREADY_ON_BREAK",
            )
        if last_punch and punch_time < last_punch.punch_time:
            raise ValidationFailed(
                "Break In time cannot be earlier than previous punch time.",
                code="INVALID_CHRONOLOGY",
            )
        if last_break and last_break.break_out_time and punch_time < last_break.break_out_time:
            raise ValidationFailed(
                "Break In time cannot be earlier than previous Break Out time.",
                code="INVALID_CHRONOLOGY",
            )

    # 3. Action: BREAK_OUT (End the break and resume working)
    elif punch_type == "BREAK_OUT":
        if not is_currently_in:
            raise ValidationFailed(
                "Cannot break out before punching in.",
                code="CANNOT_BREAK_OUT",
            )
        if not active_break:
            if last_break and last_break.break_out_time and abs((punch_time - last_break.break_out_time).total_seconds()) < 5:
                matching_punch = next((p for p in reversed(existing_punches) if p.punch_type == "BREAK_OUT"), None)
                return (matching_punch or last_break), get_today_punch_status(client_id, employee, work_date=work_date)
            raise ValidationFailed(
                "No active break to end. Please Break In first.",
                code="NOT_ON_BREAK",
            )
        if punch_time < active_break.break_in_time:
            raise ValidationFailed(
                "Break Out time cannot be earlier than Break In time.",
                code="INVALID_CHRONOLOGY",
            )

    # 4. Action: OUT (Punch Out)
    elif punch_type == "OUT":
        if not is_currently_in:
            if last_out and abs((punch_time - last_out.punch_time).total_seconds()) < 5:
                return last_out, get_today_punch_status(client_id, employee, work_date=work_date)
            raise ValidationFailed(
                "Cannot punch out without punching in first.",
                code="CANNOT_PUNCH_OUT",
            )
        if active_break:
            raise ValidationFailed(
                "Cannot punch out while a break is active. Please Break Out first.",
                code="BREAK_ACTIVE_PUNCH_OUT",
            )
        if last_punch and punch_time < last_punch.punch_time:
            raise ValidationFailed(
                "Punch Out time cannot be earlier than Punch In time.",
                code="INVALID_CHRONOLOGY",
            )
        if last_break and last_break.break_out_time and punch_time < last_break.break_out_time:
            raise ValidationFailed(
                "Punch Out time cannot be earlier than last Break Out time.",
                code="INVALID_CHRONOLOGY",
            )

    # Upsert or retrieve Attendance row
    att_row = Attendance.objects.select_for_update().filter(
        client_id=client_id, employee=employee, work_date=work_date
    ).first()

    punch_remark = remark
    if early_reason:
        punch_remark = f"{remark} | Early: {early_reason}" if remark else f"Early Punch Out: {early_reason}"

    created_punch = None
    created_break = None

    if punch_type == "IN":
        created_punch = AttendancePunch.objects.create(
            client_id=client_id,
            employee=employee,
            attendance=att_row,
            work_date=work_date,
            punch_type="IN",
            punch_time=punch_time,
            source=source,
            remark=punch_remark,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
    elif punch_type == "BREAK_IN":
        created_break = AttendanceBreak.objects.create(
            client_id=client_id,
            employee=employee,
            attendance=att_row,
            work_date=work_date,
            break_in_time=punch_time,
            remark=remark,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
        created_punch = AttendancePunch.objects.create(
            client_id=client_id,
            employee=employee,
            attendance=att_row,
            work_date=work_date,
            punch_type="BREAK_IN",
            punch_time=punch_time,
            source=source,
            remark=remark,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
    elif punch_type == "BREAK_OUT":
        dur = max(0, int((punch_time - active_break.break_in_time).total_seconds()))
        active_break.break_out_time = punch_time
        active_break.duration_seconds = dur
        active_break.duration_minutes = dur // 60
        if remark:
            active_break.remark = f"{active_break.remark} | {remark}" if active_break.remark else remark
        active_break.attendance = att_row
        active_break.save()
        created_break = active_break
        created_punch = AttendancePunch.objects.create(
            client_id=client_id,
            employee=employee,
            attendance=att_row,
            work_date=work_date,
            punch_type="BREAK_OUT",
            punch_time=punch_time,
            source=source,
            remark=remark,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )
    elif punch_type == "OUT":
        created_punch = AttendancePunch.objects.create(
            client_id=client_id,
            employee=employee,
            attendance=att_row,
            work_date=work_date,
            punch_type="OUT",
            punch_time=punch_time,
            source=source,
            remark=punch_remark,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

    # Recalculate metrics with the newly recorded event
    all_punches = list(
        AttendancePunch.objects.filter(
            client_id=client_id, employee=employee, work_date=work_date
        ).order_by("punch_time")
    )
    all_breaks = list(
        AttendanceBreak.objects.filter(
            client_id=client_id, employee=employee, work_date=work_date
        ).order_by("break_in_time")
    )
    metrics = calculate_punch_metrics(
        client_id, employee, work_date, punches=all_punches, breaks=all_breaks, now=punch_time
    )

    first_in_dt = metrics["first_in"].punch_time if metrics["first_in"] else None
    last_out_dt = metrics["last_out"].punch_time if metrics["last_out"] else None
    first_punch_time = metrics["first_punch"].punch_time if metrics["first_punch"] else None
    last_punch_time = last_out_dt if metrics["day_completed"] else None

    check_in_time = first_in_dt or first_punch_time
    check_out_time = last_out_dt if metrics["day_completed"] else None

    status_to_use = metrics["status"]
    if att_row and att_row.status in ("On Leave", "Holiday", "Week Off"):
        status_to_use = att_row.status

    early_leaving_mins = metrics["early_leaving_minutes"] if punch_type == "OUT" else (att_row.early_leaving_minutes if att_row else 0)

    if att_row is not None:
        att_row.first_punch = first_punch_time
        att_row.last_punch = last_punch_time
        att_row.check_in = check_in_time
        att_row.check_out = check_out_time
        att_row.hours = metrics["working_hours"]
        att_row.working_hours = metrics["working_hours"]
        att_row.total_attendance_hours = metrics["total_attendance_hours"]
        att_row.total_break_minutes = metrics["total_break_minutes"]
        att_row.late_minutes = metrics["late_minutes"]
        att_row.early_leaving_minutes = early_leaving_mins
        att_row.overtime_hours = metrics["overtime_hours"]
        att_row.status = status_to_use
        att_row.source = "punch"
        if early_reason:
            att_row.remark = (
                f"{att_row.remark} | Early: {early_reason}"
                if att_row.remark and "Early:" not in att_row.remark
                else (att_row.remark or f"Early Punch Out: {early_reason}")
            )
        att_row.updated_by = user if getattr(user, "is_authenticated", False) else None
        att_row.deleted_at = None
        att_row.deleted_by = None
        att_row.save()
    else:
        att_row = Attendance.objects.create(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            first_punch=first_punch_time,
            last_punch=last_punch_time,
            check_in=check_in_time,
            check_out=check_out_time,
            hours=metrics["working_hours"],
            working_hours=metrics["working_hours"],
            total_attendance_hours=metrics["total_attendance_hours"],
            total_break_minutes=metrics["total_break_minutes"],
            late_minutes=metrics["late_minutes"],
            early_leaving_minutes=early_leaving_mins,
            overtime_hours=metrics["overtime_hours"],
            status=status_to_use,
            source="punch",
            remark=f"Early Punch Out: {early_reason}" if early_reason else None,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

    if created_punch and not created_punch.attendance_id:
        created_punch.attendance = att_row
        created_punch.save(update_fields=["attendance"])

    if created_break and not created_break.attendance_id:
        created_break.attendance = att_row
        created_break.save(update_fields=["attendance"])

    # Connect with Regularization if requested or early punch out with reason
    if punch_type == "OUT" and (request_regularization or (early_reason and metrics["is_early_out"])):
        from apps.hrms.models import AttendanceRegularization
        reg_reason = early_reason or remark or f"Early punch out ({early_leaving_mins} min early)"
        existing_reg = AttendanceRegularization.objects.filter(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            status="Pending",
            deleted_at__isnull=True,
        ).first()
        if existing_reg:
            existing_reg.requested_check_in = check_in_time
            existing_reg.requested_check_out = punch_time
            existing_reg.requested_status = "Present"
            existing_reg.reason = reg_reason
            if getattr(user, "is_authenticated", False):
                existing_reg.updated_by = user
            existing_reg.save()
        else:
            AttendanceRegularization.objects.create(
                client_id=client_id,
                employee=employee,
                work_date=work_date,
                status="Pending",
                requested_check_in=check_in_time,
                requested_check_out=punch_time,
                requested_status="Present",
                reason=reg_reason,
                created_by=user if getattr(user, "is_authenticated", False) else None,
            )

    res_obj = created_punch or created_break
    return res_obj, get_today_punch_status(client_id, employee, work_date=work_date)


def record_break_in(*, client, employee, punch_time=None, source="web", remark=None, user=None):
    """Start a break session (Break In)."""
    return record_punch(
        client=client,
        employee=employee,
        punch_type="BREAK_IN",
        punch_time=punch_time,
        source=source,
        remark=remark,
        user=user,
    )


def record_break_out(*, client, employee, punch_time=None, source="web", remark=None, user=None):
    """End a break session and resume working (Break Out)."""
    return record_punch(
        client=client,
        employee=employee,
        punch_type="BREAK_OUT",
        punch_time=punch_time,
        source=source,
        remark=remark,
        user=user,
    )


@transaction.atomic
def sync_attendance_from_punches(*, client, work_date=None):
    """Reconcile and restore Attendance rows from AttendancePunch records for a given work date."""
    client_id = getattr(client, "id", client)
    if work_date is None:
        work_date = timezone.localdate()

    punches = AttendancePunch.objects.filter(
        client_id=client_id,
        work_date=work_date,
        deleted_at__isnull=True,
    ).select_related("employee")

    employee_ids = list(set(punches.values_list("employee_id", flat=True)))
    synced_count = 0

    for emp_id in employee_ids:
        emp_punches = list(
            AttendancePunch.objects.filter(
                client_id=client_id,
                employee_id=emp_id,
                work_date=work_date,
                deleted_at__isnull=True,
            ).order_by("punch_time")
        )
        if not emp_punches:
            continue
        emp = emp_punches[0].employee
        metrics = calculate_punch_metrics(client_id, emp, work_date, punches=emp_punches)

        first_in = metrics["first_in"].punch_time if metrics["first_in"] else None
        last_out = metrics["last_out"].punch_time if metrics["last_out"] else None
        first_punch_time = metrics["first_punch"].punch_time if metrics["first_punch"] else None
        last_punch_time = last_out if metrics["day_completed"] else None

        check_in_time = first_in or first_punch_time
        check_out_time = last_out if metrics["day_completed"] else None

        att_row = Attendance.objects.select_for_update().filter(
            client_id=client_id, employee=emp, work_date=work_date
        ).first()

        status_to_use = metrics["status"]

        if att_row is not None:
            att_row.first_punch = first_punch_time
            att_row.last_punch = last_punch_time
            att_row.check_in = check_in_time
            att_row.check_out = check_out_time
            att_row.hours = metrics["working_hours"]
            att_row.working_hours = metrics["working_hours"]
            att_row.total_attendance_hours = metrics["total_attendance_hours"]
            att_row.total_break_minutes = metrics["total_break_minutes"]
            att_row.late_minutes = metrics["late_minutes"]
            att_row.early_leaving_minutes = metrics["early_leaving_minutes"]
            att_row.overtime_hours = metrics["overtime_hours"]
            att_row.status = status_to_use
            att_row.source = "punch"
            att_row.deleted_at = None
            att_row.deleted_by = None
            att_row.save()
        else:
            att_row = Attendance.objects.create(
                client_id=client_id,
                employee=emp,
                work_date=work_date,
                first_punch=first_punch_time,
                last_punch=last_punch_time,
                check_in=check_in_time,
                check_out=check_out_time,
                hours=metrics["working_hours"],
                working_hours=metrics["working_hours"],
                total_attendance_hours=metrics["total_attendance_hours"],
                total_break_minutes=metrics["total_break_minutes"],
                late_minutes=metrics["late_minutes"],
                early_leaving_minutes=metrics["early_leaving_minutes"],
                overtime_hours=metrics["overtime_hours"],
                status=status_to_use,
                source="punch",
            )

        AttendancePunch.objects.filter(
            client_id=client_id, employee=emp, work_date=work_date, attendance__isnull=True
        ).update(attendance=att_row)
        synced_count += 1

    return synced_count


@transaction.atomic
def correct_punch(*, client, employee, work_date, punch_type, punch_time, reason, user=None):
    """HR/Admin correction for missing punches (with audit trail)."""
    client_id = getattr(client, "id", client)
    punch = AttendancePunch.objects.create(
        client_id=client_id,
        employee=employee,
        work_date=work_date,
        punch_type=punch_type,
        punch_time=punch_time,
        source="manual",
        remark=f"Correction: {reason}",
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    all_punches = list(
        AttendancePunch.objects.select_for_update()
        .filter(client_id=client_id, employee=employee, work_date=work_date)
        .order_by("punch_time")
    )
    metrics = calculate_punch_metrics(client_id, employee, work_date, punches=all_punches)

    first_in = metrics["first_in"].punch_time if metrics["first_in"] else None
    last_out = metrics["last_out"].punch_time if metrics["last_out"] else None
    first_punch_time = metrics["first_punch"].punch_time if metrics["first_punch"] else None
    last_punch_time = metrics["last_punch"].punch_time if metrics["last_punch"] else None

    check_in_time = first_in or first_punch_time
    check_out_time = None if metrics["is_punched_in"] else last_out

    att_row = Attendance.objects.select_for_update().filter(
        client_id=client_id, employee=employee, work_date=work_date
    ).first()

    if att_row is not None:
        att_row.first_punch = first_punch_time
        att_row.last_punch = last_punch_time
        att_row.check_in = check_in_time
        att_row.check_out = check_out_time
        att_row.hours = metrics["working_hours"]
        att_row.working_hours = metrics["working_hours"]
        att_row.late_minutes = metrics["late_minutes"]
        att_row.overtime_hours = metrics["overtime_hours"]
        att_row.status = metrics["status"]
        att_row.source = "regularization"
        att_row.updated_by = user if getattr(user, "is_authenticated", False) else None
        att_row.deleted_at = None
        att_row.deleted_by = None
        att_row.save()
    else:
        att_row = Attendance.objects.create(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            first_punch=first_punch_time,
            last_punch=last_punch_time,
            check_in=check_in_time,
            check_out=check_out_time,
            hours=metrics["working_hours"],
            working_hours=metrics["working_hours"],
            late_minutes=metrics["late_minutes"],
            overtime_hours=metrics["overtime_hours"],
            status=metrics["status"],
            source="regularization",
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

    punch.attendance = att_row
    punch.save(update_fields=["attendance"])

    if getattr(user, "is_authenticated", False):
        record_audit(
            client=client_id,
            actor=user,
            action="attendance_correction",
            entity_type="AttendancePunch",
            entity_id=punch.id,
            entity_label=f"{employee.employee_code} {work_date} {punch_type}",
            description=f"Punch correction added: {punch_type} at {punch_time}. Reason: {reason}",
            after={"punch_type": punch_type, "punch_time": punch_time.isoformat(), "reason": reason},
        )

    return punch, get_today_punch_status(client_id, employee, work_date=work_date)


@transaction.atomic
def auto_punch_out_employee(
    *, client, employee, work_date, now_dt=None, user=None,
    remark="Auto punch-out at day end (forgot to punch out)"
):
    """
    If the employee forgot to punch out on work_date, automatically close any open break and
    record an OUT punch at the end of the shift / day, syncing Attendance and calculating working hours.
    """
    client_id = getattr(client, "id", client)
    punches = list(
        AttendancePunch.objects.select_for_update().filter(
            client_id=client_id, employee=employee, work_date=work_date, deleted_at__isnull=True
        ).order_by("punch_time")
    )
    if not punches:
        return None

    first_in = next((p for p in punches if p.punch_type == "IN"), None)
    if not first_in:
        return None

    has_out = any(p.punch_type == "OUT" for p in punches)
    if has_out:
        return None

    policy = flexibility_policy(client_id)
    shift_end_str = policy.get("shiftEnd") or "18:30"
    shift_end_time = _parse_time(shift_end_str, time(18, 30))
    tz = timezone.get_current_timezone()
    shift_end_dt = timezone.make_aware(datetime.combine(work_date, shift_end_time), tz)

    # Determine auto punch out timestamp:
    # If punch-in was before shiftEnd, auto punch out at shiftEnd.
    # If punch-in was at/after shiftEnd, cap at 8 hours later or end of day (23:59:59).
    if first_in.punch_time < shift_end_dt:
        out_time = shift_end_dt
    else:
        eod = timezone.make_aware(datetime.combine(work_date, time(23, 59, 59)), tz)
        out_time = min(eod, first_in.punch_time + timedelta(hours=8))

    # Auto close any active/open break
    active_break = AttendanceBreak.objects.select_for_update().filter(
        client_id=client_id, employee=employee, work_date=work_date, break_out_time__isnull=True
    ).order_by("-break_in_time").first()

    if active_break:
        b_in = active_break.break_in_time
        auto_b_out = min(out_time, b_in + timedelta(minutes=60))
        if auto_b_out <= b_in:
            auto_b_out = b_in + timedelta(seconds=1)
        active_break.break_out_time = auto_b_out
        active_break.duration_seconds = max(0, int((auto_b_out - b_in).total_seconds()))
        active_break.duration_minutes = round(active_break.duration_seconds / 60, 2)
        active_break.remark = (
            f"{active_break.remark} | Auto break-out at day end"
            if active_break.remark
            else "Auto break-out at day end"
        )
        active_break.save()

        AttendancePunch.objects.create(
            client_id=client_id,
            employee=employee,
            work_date=work_date,
            punch_type="BREAK_OUT",
            punch_time=auto_b_out,
            source="auto_day_end",
            remark="Auto break-out at day end",
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

    out_punch = AttendancePunch.objects.create(
        client_id=client_id,
        employee=employee,
        work_date=work_date,
        punch_type="OUT",
        punch_time=out_time,
        source="auto_day_end",
        remark=remark,
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )

    sync_attendance_from_punches(client=client_id, work_date=work_date)
    return out_punch


def auto_punch_out_pending_sessions(
    *, client=None, employee=None, as_of_date=None, force_today=False, user=None
):
    """
    Scan for unclosed punch sessions (IN without OUT) and automatically punch them out at day end.
    - Any past date (work_date < today) is automatically closed at shift end.
    - If work_date == today: closed if force_today=True OR current time >= shift_end.
    Returns list of auto-created OUT punches.
    """
    client_id = getattr(client, "id", client) if client else (getattr(employee, "client_id", None) if employee else None)
    if as_of_date is None:
        as_of_date = timezone.localdate()

    now_local = timezone.localtime(timezone.now())

    qs = AttendancePunch.objects.filter(punch_type="IN", deleted_at__isnull=True)
    if client_id:
        qs = qs.filter(client_id=client_id)
    if employee:
        qs = qs.filter(employee=employee)

    open_candidates = list(qs.values_list("client_id", "employee_id", "work_date").distinct())

    closed_punches = []
    from apps.hrms.models import Employee
    for cid, emp_id, w_date in open_candidates:
        has_out = AttendancePunch.objects.filter(
            client_id=cid, employee_id=emp_id, work_date=w_date, punch_type="OUT", deleted_at__isnull=True
        ).exists()
        if has_out:
            continue

        emp = Employee.objects.filter(id=emp_id, client_id=cid).first()
        if not emp:
            continue

        policy = flexibility_policy(cid)
        shift_end_str = policy.get("shiftEnd") or "18:30"
        shift_end_time = _parse_time(shift_end_str, time(18, 30))

        should_close = False
        if w_date < as_of_date:
            should_close = True
        elif w_date == as_of_date:
            if force_today or now_local.time() >= shift_end_time:
                should_close = True

        if should_close:
            out_p = auto_punch_out_employee(
                client=cid,
                employee=emp,
                work_date=w_date,
                user=user,
            )
            if out_p:
                closed_punches.append(out_p)

    return closed_punches



def assign_asset(asset, employee, *, user=None, notes=None):
    """Hand ``asset`` to ``employee``: one open assignment row, status Assigned."""
    from .models import Asset, AssetAssignment

    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    if employee.status in EXITED_EMPLOYEE_STATUSES:
        raise Conflict(f"{employee.name} has left; assets cannot be issued to them.",
                       code=Codes.BAD_TARGET)
    if asset.history.filter(returned_at__isnull=True).exists() or asset.status == "Assigned":
        holder = asset.assigned_employee.name if asset.assigned_employee_id else "someone"
        raise Conflict(
            f"{asset.name} is already assigned to {holder}. Record a return first.",
            code=Codes.IN_USE,
        )
    if asset.status not in ("Available",):
        raise Conflict(
            f"{asset.name} is {asset.status.lower()} and cannot be issued.",
            code=Codes.BAD_TARGET,
        )
    AssetAssignment.objects.create(
        client_id=asset.client_id,
        asset=asset,
        employee=employee,
        assigned_by=user if getattr(user, "is_authenticated", False) else None,
        notes=notes,
    )
    asset.assigned_employee = employee
    asset.status = "Assigned"
    asset.save(update_fields=["assigned_employee", "status", "updated_at"])
    return asset


@transaction.atomic
def return_asset(asset, *, condition=None, notes=None):
    """Close the open assignment; a unit needing repair goes to maintenance."""
    from .models import Asset

    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    assignment = asset.history.filter(returned_at__isnull=True).first()
    if assignment is None and asset.status != "Assigned":
        raise Conflict("This asset is not currently assigned.", code=Codes.BAD_TARGET)
    if assignment is not None:
        assignment.returned_at = timezone.now()
        assignment.returned_condition = condition
        assignment.notes = notes or assignment.notes
        assignment.save()
    asset.assigned_employee = None
    asset.condition = condition or asset.condition
    asset.status = "Under Maintenance" if condition == "Needs Repair" else "Available"
    asset.save(update_fields=["assigned_employee", "condition", "status", "updated_at"])
    return asset


@transaction.atomic
def decide_asset_request(request_row, *, approve, user=None, remark=None, asset=None):
    """Approve or reject an employee's asset request.

    Approving an Inventory request issues the exact unit asked for and closes
    the request as Fulfilled. Approving an Other request records the decision;
    passing ``asset`` (now, or later via fulfil) hands a unit over.
    """
    from .models import AssetRequest

    request_row = AssetRequest.objects.select_for_update().get(pk=request_row.pk)
    if request_row.status in ("Fulfilled", "Rejected", "Cancelled"):
        raise Conflict(f"This request is already {request_row.status.lower()}.",
                       code=Codes.ALREADY_DONE)

    request_row.approver = user if getattr(user, "is_authenticated", False) else None
    request_row.decided_at = timezone.now()
    if remark is not None:
        request_row.remark = remark

    if not approve:
        request_row.status = "Rejected"
        request_row.save()
        return request_row

    target = asset or (request_row.requested_asset if request_row.request_type == "Inventory" else None)
    if target is not None:
        assign_asset(target, request_row.employee, user=user,
                     notes=f"Issued against asset request ({request_row.employee.name}).")
        request_row.fulfilled_asset = target
        request_row.status = "Fulfilled"
        # Anyone else waiting on the same unit can no longer get it.
        AssetRequest.objects.filter(
            client_id=request_row.client_id, requested_asset=target,
            status="Pending", deleted_at__isnull=True,
        ).exclude(pk=request_row.pk).update(
            status="Rejected", decided_at=timezone.now(),
            remark="Asset was issued to another employee.", updated_at=timezone.now(),
        )
    else:
        request_row.status = "Approved"
    request_row.save()
    return request_row

