"""HRMS serializers (api.md §11)."""
from datetime import date, datetime
from decimal import Decimal
import re
from django.utils import timezone

from rest_framework import serializers

from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    TenantPrimaryKeyRelatedField,
)

from apps.core.money import ZERO, D, round2
from apps.pms.production_serializers import (
    IncentiveCalculationSerializer as _PMSIncentiveCalculationSerializer,
)
from . import services
from apps.sales.models import BrokerCommission as SalesBrokerCommission
from .models import (
    EmployeeTransfer, EmployeePromotion, EmployeeWarning, EmployeeAward,
    Appraisal,
    AppraisalCycle,
    AppraisalHistory,
    AppraisalKpiScore,
    ApprovalChain,
    Application,
    Asset,
    AssetAssignment,
    AssetCategory,
    AssetRequest,
    Attendance,
    AttendanceBreak,
    AttendancePunch,
    AttendanceRegularization,
    CalendarEvent,
    Candidate,
    Complaint,
    CompOff,
    Department,
    Designation,
    Employee,
    Goal,
    Holiday,
    HrDocument,
    Interview,
    Job,
    Kpi,
    LeaveBalance,
    LeaveEncashment,
    LeaveRequest,
    LeaveType,
    Location,
    Offer,
    OnboardingTask,
    PayrollRun,
    Payslip,
    PayslipComponent,
    PerformanceIndicator,
    Policy,
    PolicyAcknowledgement,
    PolicyCategory,
    PolicyVersion,
    Resignation,
    ResignationChecklistItem,
    SalaryAdvance,
    SalaryStructure,
    ScreeningAnswer,
    ScreeningQuestion,
    Team,
    Termination,
    Trainer,
    Training,
    TrainingParticipant,
    WorkingDay,
    SalespersonIncentiveScheme,
    SalespersonIncentiveAccrual,
    BrokerCommissionRecord,
)


# ---------------------------------------------------------------------------
# Organisation (api.md §11.1)
# ---------------------------------------------------------------------------
class DepartmentSerializer(BaseModelSerializer):
    head = serializers.CharField(source="head_employee.name", read_only=True)
    headEmployeeId = TenantPrimaryKeyRelatedField(
        source="head_employee", model="hrms.Employee", required=False, allow_null=True
    )
    employees = serializers.SerializerMethodField()
    teams = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = [
            "id", "name", "code", "head", "headEmployeeId", "parent", "status",
            "description", "employees", "teams", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        """A department without a code gets one from its name (Dyeing -> DYEING)."""
        from apps.masters.serializers import slug_code, unique_code

        attrs = super().validate(attrs)
        if self.instance is None and not attrs.get("code"):
            peers = Department.objects.filter(client_id=self.context.get("client_id"), deleted_at__isnull=True)
            attrs["code"] = unique_code(peers, slug_code(attrs.get("name"), "DEPT"))
        return attrs

    def get_employees(self, department):
        return getattr(department, "employee_count", None)

    def get_teams(self, department):
        return getattr(department, "team_count", None)


class DesignationSerializer(BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )

    class Meta:
        model = Designation
        fields = ["id", "name", "level", "department", "departmentId", "created_at"]


class LocationSerializer(BaseModelSerializer):
    employeeCount = serializers.SerializerMethodField()

    class Meta:
        model = Location
        fields = ["id", "name", "address", "timezone", "employeeCount", "created_at"]

    def get_employeeCount(self, location):
        return location.employees.filter(deleted_at__isnull=True).count()


class EmployeeSerializer(BaseModelSerializer):
    employeeCode = serializers.CharField(source="employee_code", read_only=True)
    designation = serializers.CharField(source="designation.name", read_only=True)
    designationId = TenantPrimaryKeyRelatedField(
        source="designation", model="hrms.Designation", required=False, allow_null=True
    )
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    manager = serializers.CharField(source="manager.name", read_only=True)
    managerId = TenantPrimaryKeyRelatedField(
        source="manager", model="hrms.Employee", required=False, allow_null=True
    )
    location = serializers.CharField(source="location.name", read_only=True)
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )
    joining = serializers.DateField(source="joining_date", required=False)
    # Frontend posts `joiningDate` (hrmsSync.toApi); accept it as an alias so
    # a missing `joining` never 400s the create.
    joiningDate = serializers.DateField(write_only=True, required=False)
    employmentType = serializers.CharField(
        source="employment_type", required=False, allow_null=True
    )
    salaryStructureId = TenantPrimaryKeyRelatedField(
        source="salary_structure", model="hrms.SalaryStructure",
        required=False, allow_null=True,
    )
    standardSalary = MoneyField(source="standard_salary", required=False)
    avatar = serializers.CharField(source="avatar_url", required=False, allow_null=True)

    class Meta:
        model = Employee
        fields = [
            "id", "employeeCode", "name", "email", "phone", "avatar",
            "designation", "designationId", "department", "departmentId",
            "manager", "managerId", "location", "locationId", "joining", "joiningDate",
            "employmentType", "shift", "salaryStructureId", "standard_salary", "standardSalary",
            "status", "date_of_birth", "gender", "blood_group", "personal_email",
            "emergency_contact", "address", "bank_account_number", "ifsc_code",
            "pan", "uan", "aadhaar_last4", "last_working_day",
            "termination_reason", "created_at", "updated_at",
        ]
        read_only_fields = ["employeeCode", "created_at", "updated_at"]

    def to_internal_value(self, data):
        if isinstance(data, dict) and "status" in data and data["status"]:
            st = str(data["status"]).strip()
            mapping = {
                "active": "Active",
                "inactive": "Inactive",
                "on leave": "On Leave",
                "on-leave": "On Leave",
                "on_leave": "On Leave",
                "probation": "Probation",
                "resigned": "Resigned",
                "notice period": "Notice Period",
                "notice-period": "Notice Period",
                "notice_period": "Notice Period",
                "terminated": "Terminated",
            }
            if st.lower() in mapping:
                data = {**data, "status": mapping[st.lower()]}
        return super().to_internal_value(data)

    def validate(self, attrs):
        from django.utils import timezone as dj_timezone
        manager = attrs.get("manager")
        if manager is not None and self.instance is not None:
            services.assert_no_manager_cycle(self.instance, manager.id)
        # `joiningDate` is the write alias for `joining_date`; default today.
        alias = attrs.pop("joiningDate", None)
        if attrs.get("joining_date") is None and alias is not None:
            attrs["joining_date"] = alias
        if attrs.get("joining_date") is None and self.instance is None:
            attrs["joining_date"] = dj_timezone.localdate()
        return attrs


# ---------------------------------------------------------------------------
# Attendance (api.md §11.2)
# ---------------------------------------------------------------------------
class AttendanceSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    name = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    empId = serializers.CharField(source="employee.employee_code", read_only=True)
    department = serializers.CharField(source="employee.department.name", read_only=True)
    dept = serializers.CharField(source="employee.department.name", read_only=True)
    shift = serializers.CharField(source="employee.shift", read_only=True)
    date = serializers.DateField(source="work_date")
    checkIn = serializers.DateTimeField(source="check_in", required=False, allow_null=True)
    checkOut = serializers.DateTimeField(source="check_out", required=False, allow_null=True)
    firstPunch = serializers.DateTimeField(source="first_punch", required=False, allow_null=True)
    lastPunch = serializers.SerializerMethodField()
    workingHours = serializers.DecimalField(source="working_hours", max_digits=6, decimal_places=2, required=False, allow_null=True)
    totalBreakMinutes = serializers.IntegerField(source="total_break_minutes", required=False)
    totalAttendanceHours = serializers.DecimalField(source="total_attendance_hours", max_digits=6, decimal_places=2, required=False, allow_null=True)
    lateMinutes = serializers.IntegerField(source="late_minutes", required=False)
    earlyLeavingMinutes = serializers.IntegerField(source="early_leaving_minutes", required=False)
    overtimeHours = serializers.DecimalField(source="overtime_hours", max_digits=6, decimal_places=2, required=False)
    punches = serializers.SerializerMethodField()
    breaks = serializers.SerializerMethodField()
    formattedWorkingHours = serializers.SerializerMethodField()

    class Meta:
        model = Attendance
        fields = [
            "id", "employeeId", "employeeName", "name", "employeeCode", "empId", "department", "dept", "shift",
            "date", "checkIn", "checkOut", "firstPunch", "lastPunch", "hours", "workingHours",
            "totalBreakMinutes", "totalAttendanceHours", "lateMinutes", "earlyLeavingMinutes", "overtimeHours",
            "punches", "breaks", "formattedWorkingHours",
            "status", "source", "remark", "leave_request", "created_at", "updated_at",
        ]
        read_only_fields = ["hours", "workingHours", "totalBreakMinutes", "totalAttendanceHours", "lateMinutes", "earlyLeavingMinutes", "overtimeHours", "source", "leave_request", "created_at", "updated_at"]

    def get_lastPunch(self, obj):
        val = obj.last_punch or obj.check_out
        if not val:
            return None
        local_dt = timezone.localtime(val) if timezone.is_aware(val) else val
        return local_dt.strftime("%I:%M %p")

    def get_formattedWorkingHours(self, obj):
        wh = obj.working_hours if obj.working_hours is not None else obj.hours
        if wh is None:
            return "00h 00m"
        total_m = int(float(wh) * 60)
        h = total_m // 60
        m = total_m % 60
        return f"{h:02d}h {m:02d}m"

    def get_punches(self, obj):
        punches = obj.punches.all().order_by("punch_time")
        return [
            {
                "id": str(p.id),
                "punchType": p.punch_type,
                "punchTime": p.punch_time.isoformat(),
                "timeDisplay": (timezone.localtime(p.punch_time) if timezone.is_aware(p.punch_time) else p.punch_time).strftime("%I:%M %p"),
                "source": p.source,
                "remark": p.remark,
            }
            for p in punches
        ]

    def get_breaks(self, obj):
        breaks = obj.breaks.all().order_by("break_in_time")
        return [
            {
                "id": str(b.id),
                "breakIn": b.break_in_time.isoformat(),
                "breakOut": b.break_out_time.isoformat() if b.break_out_time else None,
                "breakInTime": (timezone.localtime(b.break_in_time) if timezone.is_aware(b.break_in_time) else b.break_in_time).strftime("%I:%M %p"),
                "breakOutTime": (timezone.localtime(b.break_out_time) if timezone.is_aware(b.break_out_time) else b.break_out_time).strftime("%I:%M %p") if b.break_out_time else None,
                "duration": f"{(b.duration_seconds // 3600):02d}h {((b.duration_seconds % 3600) // 60):02d}m" if b.break_out_time else "In Progress",
                "durationSeconds": b.duration_seconds,
                "durationMinutes": b.duration_minutes,
                "breakType": b.break_type,
                "remark": b.remark,
            }
            for b in breaks
        ]

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        work_date = data.get("date") or data.get("work_date")
        if work_date:
            for time_field in ("checkIn", "check_in"):
                val = data.get(time_field)
                if val and isinstance(val, str) and len(val) <= 8 and ":" in val:
                    data[time_field] = f"{work_date}T{val}:00" if len(val) == 5 else f"{work_date}T{val}"
            for time_field in ("checkOut", "check_out"):
                val = data.get(time_field)
                if val and isinstance(val, str) and len(val) <= 8 and ":" in val:
                    data[time_field] = f"{work_date}T{val}:00" if len(val) == 5 else f"{work_date}T{val}"

        emp_id = data.get("employeeId") or data.get("employee_id")
        if emp_id and isinstance(emp_id, str):
            import uuid
            try:
                uuid.UUID(emp_id)
            except ValueError:
                from apps.hrms.models import Employee
                request = self.context.get("request")
                client_id = getattr(request, "client_id", None) or (request.user.client_id if request and hasattr(request, "user") else None)
                if client_id:
                    emp = Employee.objects.filter(
                        employee_code=emp_id, client_id=client_id, deleted_at__isnull=True
                    ).first()
                    if emp:
                        data["employeeId"] = str(emp.id)
        return super().to_internal_value(data)


class BulkAttendanceSerializer(BaseSerializer):
    date = serializers.DateField(required=False)
    records = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    status = serializers.CharField(required=False, allow_null=True)


class AttendancePunchSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee", read_only=True)
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    punchType = serializers.CharField(source="punch_type")
    punchTime = serializers.DateTimeField(source="punch_time")
    timeDisplay = serializers.SerializerMethodField()
    date = serializers.DateField(source="work_date", read_only=True)

    class Meta:
        model = AttendancePunch
        fields = [
            "id", "employeeId", "employeeName", "employeeCode", "date",
            "punchType", "punchTime", "timeDisplay", "source", "remark", "created_at"
        ]
        read_only_fields = ["id", "employeeId", "date", "created_at"]

    def get_timeDisplay(self, obj):
        local_dt = timezone.localtime(obj.punch_time) if timezone.is_aware(obj.punch_time) else obj.punch_time
        return local_dt.strftime("%I:%M %p")


class AttendanceBreakSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee", read_only=True)
    breakIn = serializers.DateTimeField(source="break_in_time")
    breakOut = serializers.DateTimeField(source="break_out_time", allow_null=True)
    breakInTime = serializers.SerializerMethodField()
    breakOutTime = serializers.SerializerMethodField()
    duration = serializers.SerializerMethodField()
    durationSeconds = serializers.IntegerField(source="duration_seconds", read_only=True)
    durationMinutes = serializers.IntegerField(source="duration_minutes", read_only=True)
    breakType = serializers.CharField(source="break_type")

    class Meta:
        model = AttendanceBreak
        fields = [
            "id", "employeeId", "breakIn", "breakOut", "breakInTime", "breakOutTime",
            "duration", "durationSeconds", "durationMinutes", "breakType", "remark",
        ]

    def get_breakInTime(self, obj):
        local_dt = timezone.localtime(obj.break_in_time) if timezone.is_aware(obj.break_in_time) else obj.break_in_time
        return local_dt.strftime("%I:%M %p")

    def get_breakOutTime(self, obj):
        if not obj.break_out_time:
            return None
        local_dt = timezone.localtime(obj.break_out_time) if timezone.is_aware(obj.break_out_time) else obj.break_out_time
        return local_dt.strftime("%I:%M %p")

    def get_duration(self, obj):
        if not obj.break_out_time:
            return "In Progress"
        h = obj.duration_seconds // 3600
        m = (obj.duration_seconds % 3600) // 60
        return f"{h:02d}h {m:02d}m"


class RegularizationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    date = serializers.DateField(source="work_date")
    requestedCheckIn = serializers.DateTimeField(
        source="requested_check_in", required=False, allow_null=True
    )
    requestedCheckOut = serializers.DateTimeField(
        source="requested_check_out", required=False, allow_null=True
    )
    requestedStatus = serializers.CharField(
        source="requested_status", required=False, allow_null=True
    )

    class Meta:
        model = AttendanceRegularization
        fields = [
            "id", "employeeId", "employeeName", "date", "requestedCheckIn",
            "requestedCheckOut", "requestedStatus", "reason", "status",
            "approver", "decided_at", "remark", "created_at",
        ]
        read_only_fields = ["approver", "decided_at", "created_at"]

    #: ``09:00`` / ``09:00:00`` -- a bare clock time on the request's own date.
    _TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")

    def to_internal_value(self, data):
        """Accept ``requestedCheckIn: "09:00"`` as well as a full datetime.

        The self-service forms collect a clock time for the day being
        regularized; it is pinned to ``date`` here so it parses as a datetime.
        """
        day = data.get("date") if hasattr(data, "get") else None
        if day:
            patched = None
            for key in ("requestedCheckIn", "requestedCheckOut"):
                value = data.get(key)
                if isinstance(value, str) and self._TIME_ONLY.match(value.strip()):
                    patched = patched or (data.copy() if hasattr(data, "copy") else dict(data))
                    patched[key] = f"{day}T{value.strip()}"
            if patched is not None:
                data = patched
        return super().to_internal_value(data)


# ---------------------------------------------------------------------------
# Leave (api.md §11.3)
# ---------------------------------------------------------------------------
class LeaveTypeSerializer(BaseModelSerializer):
    class Meta:
        model = LeaveType
        fields = [
            "id", "name", "code", "annual_entitlement", "accrual",
            "carry_forward_cap", "is_encashable", "is_paid", "created_at",
        ]


class LeaveRequestSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    type = serializers.CharField(source="leave_type.name", read_only=True)
    leaveTypeId = TenantPrimaryKeyRelatedField(
        source="leave_type", model="hrms.LeaveType", required=False, allow_null=True
    )
    #: Write-only alternative to ``leaveTypeId``: the leave type's name or code.
    leaveTypeName = serializers.CharField(
        write_only=True, required=False, allow_blank=True, allow_null=True
    )
    fromDate = serializers.DateField(source="from_date")
    toDate = serializers.DateField(source="to_date")
    delegateId = TenantPrimaryKeyRelatedField(
        source="delegate_employee", model="hrms.Employee",
        required=False, allow_null=True,
    )

    class Meta:
        model = LeaveRequest
        fields = [
            "id", "employeeId", "employeeName", "employeeCode", "type",
            "leaveTypeId", "leaveTypeName", "fromDate", "toDate", "days", "reason",
            "delegateId", "delegate_confirmed_at", "status", "approver", "decided_at",
            "remark", "created_at", "updated_at",
        ]
        read_only_fields = [
            "status", "approver", "decided_at", "delegate_confirmed_at",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        start = attrs.get("from_date") or getattr(self.instance, "from_date", None)
        end = attrs.get("to_date") or getattr(self.instance, "to_date", None)
        if start and end and end < start:
            raise serializers.ValidationError({"toDate": ["Must be on or after the start date."]})

        type_name = attrs.pop("leaveTypeName", None)
        if self.instance is None:
            request = self.context.get("request")
            client_id = getattr(request, "client_id", None) or getattr(
                getattr(request, "user", None), "client_id", None
            )
            if not attrs.get("leave_type"):
                leave_type = services.resolve_leave_type(client_id, type_name)
                if leave_type is None:
                    raise serializers.ValidationError({"leaveTypeId": [
                        f"No leave type named \"{type_name}\" is set up."
                        if type_name else "This field is required."
                    ]})
                attrs["leave_type"] = leave_type
            if "days" not in attrs and start and end:
                attrs["days"] = len(services.working_days_in(client_id, start, end)) or (
                    (end - start).days + 1
                )
        return attrs


class LeaveBalanceSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    leaveType = serializers.CharField(source="leave_type.name", read_only=True)
    # GeneratedField (computed by Postgres) -- DRF falls back to ModelField
    # for it, which drf-spectacular cannot map (DecimalField() with no args).
    # Declared explicitly so schema generation sees a real DecimalField.
    balance = serializers.DecimalField(
        max_digits=6, decimal_places=2, coerce_to_string=False, read_only=True
    )

    class Meta:
        model = LeaveBalance
        fields = [
            "id", "employee", "employeeName", "employeeCode", "leave_type",
            "leaveType", "period_year", "entitlement", "carried_forward",
            "used", "encashed", "balance",
        ]
        read_only_fields = ["balance", "used"]


class CompOffSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employee = serializers.CharField(source="employee.name", read_only=True)
    workedDate = serializers.DateField(source="worked_date")
    workType = serializers.CharField(source="work_type", required=False, allow_null=True)
    creditDays = serializers.DecimalField(source="days", max_digits=4, decimal_places=2, required=False)
    expiryDate = serializers.DateField(source="expiry_date", required=False, allow_null=True)
    rejectReason = serializers.CharField(source="reject_reason", required=False, allow_null=True)

    class Meta:
        model = CompOff
        fields = [
            "id", "employeeId", "employeeName", "employee", "workedDate", "workType",
            "days", "creditDays", "expiryDate", "status", "rejectReason",
            "used", "created_at",
        ]


class LeaveEncashmentSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    leaveTypeId = TenantPrimaryKeyRelatedField(
        source="leave_type", model="hrms.LeaveType", required=False, allow_null=True
    )
    leaveTypeName = serializers.CharField(source="leave_type.name", read_only=True)
    perDayRate = MoneyField(source="per_day_rate", required=False)
    # GeneratedField (days * per_day_rate) -- see LeaveBalanceSerializer.balance.
    amount = MoneyField(read_only=True)

    class Meta:
        model = LeaveEncashment
        fields = [
            "id", "employeeId", "employeeName", "leaveTypeId", "leaveTypeName",
            "days", "perDayRate", "amount", "status", "created_at",
        ]
        read_only_fields = ["amount"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        client = getattr(self.context.get("request"), "client", None)
        client_id = getattr(self.context.get("request"), "client_id", None) or (client.id if client else None)

        # Allow flexible employee resolution: employeeId, or matching by employee name
        if not attrs.get("employee"):
            emp_name = self.initial_data.get("employee") or self.initial_data.get("employeeName")
            if emp_name and client_id:
                emp = Employee.objects.filter(client_id=client_id, name__iexact=str(emp_name).strip()).first()
                if emp:
                    attrs["employee"] = emp

        # Allow fallback perDayRate from ratePerDay or default
        if not attrs.get("per_day_rate"):
            rate = self.initial_data.get("ratePerDay") or self.initial_data.get("perDayRate") or 2083
            attrs["per_day_rate"] = Decimal(str(rate))

        # Allow fallback leaveTypeId to first active leave type if not provided
        if not attrs.get("leave_type"):
            lt_id = self.initial_data.get("leaveTypeId") or self.initial_data.get("leaveType")
            if lt_id:
                lt = LeaveType.objects.filter(id=lt_id).first()
                if lt:
                    attrs["leave_type"] = lt
            if not attrs.get("leave_type") and client_id:
                attrs["leave_type"] = LeaveType.objects.filter(client_id=client_id).first()

        return attrs


# ---------------------------------------------------------------------------
# Payroll (api.md §11.4)
# ---------------------------------------------------------------------------
class SalaryStructureSerializer(BaseModelSerializer):
    class Meta:
        model = SalaryStructure
        fields = [
            "id", "name", "basic_pct", "hra_pct", "components", "is_active",
            "created_at", "updated_at",
        ]


class PayslipComponentSerializer(BaseModelSerializer):
    class Meta:
        model = PayslipComponent
        fields = ["id", "name", "kind", "amount"]


class PayslipSerializer(BaseModelSerializer):
    """The payroll row api.md §11.4 documents."""

    empId = serializers.CharField(source="employee.employee_code", read_only=True)
    employeeId = serializers.CharField(source="employee_id", read_only=True)
    name = serializers.CharField(source="employee.name", read_only=True)
    role = serializers.CharField(source="employee.designation.name", read_only=True)
    department = serializers.CharField(source="employee.department.name", read_only=True)
    month = serializers.DateField(source="period_month", read_only=True)
    standardSalary = MoneyField(source="standard_salary", read_only=True)
    earnedSalary = MoneyField(source="earned_salary", read_only=True)
    additionalEarnings = MoneyField(source="additional_earnings", required=False)
    advance = MoneyField(source="advance_recovery", required=False)
    paidLeaves = serializers.DecimalField(
        source="paid_leaves", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    attendedDays = serializers.DecimalField(
        source="attended_days", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    totalDays = serializers.DecimalField(
        source="total_days", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    paidAmount = MoneyField(source="paid_amount", read_only=True)
    paymentDate = serializers.DateField(source="payment_date", read_only=True)
    bank = serializers.CharField(source="bank_account.name", read_only=True)
    components = PayslipComponentSerializer(many=True, read_only=True)
    netPayable = MoneyField(source="net_payable", read_only=True)
    breakdown = serializers.SerializerMethodField()
    attendance = serializers.SerializerMethodField()

    class Meta:
        model = Payslip
        fields = [
            "id", "empId", "employeeId", "name", "role", "department", "month",
            "standardSalary", "earnedSalary", "additionalEarnings", "deductions",
            "advance", "paidLeaves", "basic", "hra", "allowances", "status",
            "paidAmount", "paymentDate", "bank", "attendedDays", "totalDays",
            "netPayable", "earned_salary_overridden", "components", "breakdown",
            "attendance", "created_at", "updated_at",
        ]
        read_only_fields = ["status", "earned_salary_overridden", "created_at", "updated_at"]

    def get_breakdown(self, payslip):
        """Per-day rate, absent days and the deduction, as the payslip computed them."""
        result = services.compute_salary(
            standard_salary=payslip.standard_salary,
            total_days=payslip.total_days,
            attended_days=payslip.attended_days,
            paid_leaves=payslip.paid_leaves,
            additional_earnings=payslip.additional_earnings,
            deductions=payslip.deductions,
            advance=payslip.advance_recovery,
        )
        return {
            "perDaySalary": float(result["perDaySalary"]),
            "absentDays": float(result["absentDays"]),
            "attendanceDeduction": float(result["attendanceDeduction"]),
        }

    def get_attendance(self, payslip):
        """The month's attendance and leave counts behind the attended/paid-leave days."""
        counts = services.attendance_counts(
            payslip.client_id, payslip.employee, payslip.period_month,
            services._month_end(payslip.period_month),
        )
        return {key: float(value) for key, value in counts.items()}


class PayrollRunSerializer(BaseModelSerializer):
    payslipCount = serializers.SerializerMethodField()

    class Meta:
        model = PayrollRun
        fields = [
            "id", "period_month", "status", "processed_by", "processed_at",
            "approved_by", "approved_at", "payslipCount", "created_at",
        ]

    def get_payslipCount(self, run):
        return run.payslips.filter(deleted_at__isnull=True).count()


class ProcessPayrollSerializer(BaseSerializer):
    month = serializers.DateField()
    employeeIds = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )


class SalaryAdvanceSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    issuedOn = serializers.DateField(source="issued_on")
    recoveredAmount = MoneyField(source="recovered_amount", read_only=True)
    outstanding = serializers.SerializerMethodField()

    class Meta:
        model = SalaryAdvance
        fields = [
            "id", "employeeId", "employeeName", "amount", "issuedOn",
            "installments", "recoveredAmount", "outstanding", "status",
            "notes", "created_at",
        ]
        read_only_fields = ["status", "created_at"]

    def get_outstanding(self, advance):
        return advance.outstanding


# ---------------------------------------------------------------------------
# Recruitment (api.md §11.5)
# ---------------------------------------------------------------------------
class JobSerializer(BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    designationId = TenantPrimaryKeyRelatedField(
        source="designation", model="hrms.Designation", required=False, allow_null=True
    )
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )
    applicantCount = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = [
            "id", "title", "department", "departmentId", "designationId",
            "locationId", "openings", "experience_range", "salary_range",
            "employment_type", "description", "status", "is_published",
            "published_at", "slug", "applicantCount", "created_at", "updated_at",
        ]
        read_only_fields = ["published_at", "slug", "created_at", "updated_at"]

    def get_applicantCount(self, job):
        return getattr(job, "applicant_count", None)


class CandidateSerializer(BaseModelSerializer):
    resumeFileId = TenantPrimaryKeyRelatedField(
        source="resume_file", model="core.File", required=False, allow_null=True
    )

    class Meta:
        model = Candidate
        fields = [
            "id", "name", "email", "phone", "resumeFileId", "source",
            "current_ctc", "expected_ctc", "notice_period_days", "stage",
            "rating", "employee", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["employee", "created_at", "updated_at"]


class ApplicationSerializer(BaseModelSerializer):
    candidateId = TenantPrimaryKeyRelatedField(source="candidate", model="hrms.Candidate")
    candidateName = serializers.CharField(source="candidate.name", read_only=True)
    jobId = TenantPrimaryKeyRelatedField(source="job", model="hrms.Job")
    jobTitle = serializers.CharField(source="job.title", read_only=True)

    class Meta:
        model = Application
        fields = [
            "id", "candidateId", "candidateName", "jobId", "jobTitle",
            "applied_at", "stage", "rejection_reason",
        ]


class InterviewSerializer(BaseModelSerializer):
    applicationId = TenantPrimaryKeyRelatedField(
        source="application", model="hrms.Application"
    )
    candidateName = serializers.CharField(
        source="application.candidate.name", read_only=True
    )
    scheduledAt = serializers.DateTimeField(source="scheduled_at")
    panelUserIds = serializers.JSONField(source="panel_user_ids", required=False)

    class Meta:
        model = Interview
        fields = [
            "id", "applicationId", "candidateName", "round", "scheduledAt",
            "mode", "panelUserIds", "status", "rating", "notes",
            "recommendation", "created_at",
        ]


class OfferSerializer(BaseModelSerializer):
    applicationId = TenantPrimaryKeyRelatedField(
        source="application", model="hrms.Application"
    )
    candidateName = serializers.CharField(
        source="application.candidate.name", read_only=True
    )
    offeredCtc = MoneyField(source="offered_ctc", required=False, allow_null=True)
    joiningDate = serializers.DateField(
        source="joining_date", required=False, allow_null=True
    )

    class Meta:
        model = Offer
        fields = [
            "id", "applicationId", "candidateName", "offeredCtc", "joiningDate",
            "status", "letter_file", "sent_at", "responded_at", "notes",
            "position", "department", "job_type", "location", "work_mode",
            "reporting_manager", "probation_period", "expiry_date", "created_at",
        ]
        read_only_fields = ["sent_at", "responded_at", "created_at"]


class OnboardingTaskSerializer(BaseModelSerializer):
    candidateId = TenantPrimaryKeyRelatedField(source="candidate", model="hrms.Candidate")

    class Meta:
        model = OnboardingTask
        fields = ["id", "candidateId", "title", "owner", "due_date", "completed_at"]


class ScreeningQuestionSerializer(BaseModelSerializer):
    jobId = TenantPrimaryKeyRelatedField(
        source="job", model="hrms.Job", required=False, allow_null=True
    )

    class Meta:
        model = ScreeningQuestion
        fields = ["id", "jobId", "question", "type", "options", "is_active", "sort_order"]


# ---------------------------------------------------------------------------
# Performance (api.md §11.6)
# ---------------------------------------------------------------------------
class AppraisalCycleSerializer(BaseModelSerializer):
    startDate = serializers.DateField(source="period_start", required=False)
    endDate = serializers.DateField(source="period_end", required=False)
    periodStart = serializers.DateField(source="period_start", required=False)
    periodEnd = serializers.DateField(source="period_end", required=False)
    reviewPeriod = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    reviewFrequency = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = AppraisalCycle
        fields = [
            "id", "name", "period_start", "period_end", "startDate", "endDate",
            "periodStart", "periodEnd", "status", "participants_scope",
            "reviewPeriod", "reviewFrequency", "description", "created_at",
        ]

    def to_internal_value(self, data):
        if isinstance(data, dict):
            data = data.copy()
            start = data.get("period_start") or data.get("startDate") or data.get("periodStart")
            if start:
                data["period_start"] = str(start)[:10]
            end = data.get("period_end") or data.get("endDate") or data.get("periodEnd")
            if end:
                data["period_end"] = str(end)[:10]

            if not data.get("period_start"):
                import datetime
                data["period_start"] = str(datetime.date.today())
            if not data.get("period_end"):
                import datetime
                data["period_end"] = str(datetime.date.today())

            raw_id = data.get("id")
            if raw_id:
                try:
                    import uuid
                    uuid.UUID(str(raw_id))
                except Exception:
                    data.pop("id", None)
        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["startDate"] = str(instance.period_start) if instance.period_start else ""
        ret["endDate"] = str(instance.period_end) if instance.period_end else ""
        ret["periodStart"] = str(instance.period_start) if instance.period_start else ""
        ret["periodEnd"] = str(instance.period_end) if instance.period_end else ""
        if instance.period_start and instance.period_end:
            ret["reviewPeriod"] = f"{instance.period_start.strftime('%d %b %Y')} – {instance.period_end.strftime('%d %b %Y')}"
        scope = instance.participants_scope if isinstance(instance.participants_scope, dict) else {}
        ret["reviewFrequency"] = scope.get("reviewFrequency", "Quarterly")
        ret["description"] = scope.get("description", "")
        return ret

    def validate(self, attrs):
        attrs = super().validate(attrs)
        scope = dict(attrs.get("participants_scope") or {})
        for field in ("reviewPeriod", "reviewFrequency", "description"):
            val = attrs.pop(field, None) or self.initial_data.get(field)
            if val:
                scope[field] = val
        attrs["participants_scope"] = scope
        return attrs


class PerformanceIndicatorSerializer(BaseModelSerializer):
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )

    class Meta:
        model = PerformanceIndicator
        fields = ["id", "name", "category", "weight", "departmentId", "created_at"]


class KpiSerializer(BaseModelSerializer):
    indicatorId = TenantPrimaryKeyRelatedField(
        source="indicator", model="hrms.PerformanceIndicator",
        required=False, allow_null=True,
    )
    # Target can be a formatted string (e.g. ">= 90%") or decimal number from frontend
    target = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    # Extra fields used by frontend KPI Data feature
    targetDisplay = serializers.CharField(source="target", required=False, allow_null=True)
    department = serializers.CharField(source="applies_to", required=False, allow_null=True)
    designation = serializers.CharField(required=False, allow_null=True, write_only=True)
    assignedEmployee = serializers.CharField(required=False, allow_null=True, write_only=True)
    measurementType = serializers.CharField(source="unit", required=False, allow_null=True)
    weight = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, allow_null=True, write_only=True)
    status = serializers.CharField(required=False, allow_null=True, write_only=True)

    class Meta:
        model = Kpi
        fields = [
            "id", "indicatorId", "name", "target", "unit", "applies_to", "created_at",
            "targetDisplay", "department", "designation", "assignedEmployee", "measurementType",
            "weight", "status",
        ]

    def to_internal_value(self, data):
        # Clean target string before model field validation
        if isinstance(data, dict) and "target" in data:
            data = data.copy()
            raw_target = data.get("target")
            if raw_target is not None and not isinstance(raw_target, (int, float, Decimal)):
                import re
                cleaned_str = str(raw_target).strip()
                match = re.search(r"[-+]?\d*\.?\d+", cleaned_str)
                if match:
                    data["target"] = match.group()
                    if not data.get("unit") and "%" in cleaned_str:
                        data["unit"] = "%"
                elif cleaned_str == "":
                    data["target"] = None
                else:
                    data["target"] = None
        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Pass fields expected by frontend
        ret["target"] = str(instance.target) if instance.target is not None else ""
        if instance.unit and "%" in instance.unit and ret["target"] and not ret["target"].endswith("%"):
            ret["target"] = f"{ret['target']}%"
        ret["department"] = instance.applies_to or ""
        ret["measurementType"] = instance.unit or "Percentage"
        return ret

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Ensure target is converted to Decimal if it's a string
        if "target" in attrs and attrs["target"] is not None:
            try:
                attrs["target"] = Decimal(str(attrs["target"]))
            except Exception:
                attrs["target"] = None

        # Map department or designation into applies_to if applies_to not explicitly provided
        if not attrs.get("applies_to"):
            dept = self.initial_data.get("department") or self.initial_data.get("designation")
            if dept:
                attrs["applies_to"] = str(dept)

        return attrs


class AppraisalKpiScoreSerializer(BaseModelSerializer):
    kpiId = TenantPrimaryKeyRelatedField(source="kpi", model="hrms.Kpi")
    kpiName = serializers.CharField(source="kpi.name", read_only=True)

    class Meta:
        model = AppraisalKpiScore
        fields = ["id", "kpiId", "kpiName", "target", "achieved", "score", "weight"]


class AppraisalHistorySerializer(BaseModelSerializer):
    actorName = serializers.CharField(source="actor.name", read_only=True)

    class Meta:
        model = AppraisalHistory
        fields = [
            "id", "from_stage", "to_stage", "actor", "actorName", "action",
            "comment", "created_at",
        ]


class AppraisalSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employee = serializers.CharField(source="employee.name", read_only=True)
    managerId = TenantPrimaryKeyRelatedField(
        source="manager", model="hrms.Employee", required=False, allow_null=True
    )
    cycleId = TenantPrimaryKeyRelatedField(source="cycle", model="hrms.AppraisalCycle", required=False, allow_null=True)
    kpiScores = AppraisalKpiScoreSerializer(source="kpi_scores", many=True, read_only=True)
    history = AppraisalHistorySerializer(many=True, read_only=True)
    selfRating = serializers.DecimalField(source="self_rating", max_digits=3, decimal_places=2, required=False, allow_null=True)
    managerRating = serializers.DecimalField(source="manager_rating", max_digits=3, decimal_places=2, required=False, allow_null=True)
    finalRating = serializers.DecimalField(source="final_rating", max_digits=3, decimal_places=2, required=False, allow_null=True)
    rating = serializers.SerializerMethodField()

    def get_rating(self, obj):
        val = obj.final_rating or obj.manager_rating or obj.self_rating or 0.0
        return float(val)

    def to_internal_value(self, data):
        if isinstance(data, dict):
            data = data.copy()
            # Strip client temporary non-UUID id like 'APR-001'
            raw_id = data.get("id")
            if raw_id:
                try:
                    import uuid
                    uuid.UUID(str(raw_id))
                except Exception:
                    data.pop("id", None)

            # Cycle ID resolution
            raw_cycle = data.get("cycleId") or data.get("cycle")
            is_valid_cycle_uuid = False
            if raw_cycle:
                try:
                    import uuid
                    uuid.UUID(str(raw_cycle))
                    is_valid_cycle_uuid = True
                except Exception:
                    is_valid_cycle_uuid = False

            client_id = self.context.get("client_id")
            if not is_valid_cycle_uuid and client_id:
                active_cycle = AppraisalCycle.objects.filter(client_id=client_id).order_by("-period_start").first()
                if not active_cycle:
                    import datetime
                    year = datetime.date.today().year
                    active_cycle = AppraisalCycle.objects.create(
                        client_id=client_id,
                        name=f"Annual Review {year}",
                        period_start=datetime.date(year, 1, 1),
                        period_end=datetime.date(year, 12, 31),
                        status="Active",
                    )
                data["cycleId"] = str(active_cycle.id)

            # Employee resolution if name sent without UUID employeeId
            raw_emp = data.get("employeeId") or data.get("employee")
            is_valid_emp_uuid = False
            if raw_emp:
                try:
                    import uuid
                    uuid.UUID(str(raw_emp))
                    is_valid_emp_uuid = True
                except Exception:
                    is_valid_emp_uuid = False

            if not is_valid_emp_uuid and client_id:
                emp_name = str(data.get("employee") or raw_emp)
                matched_emp = Employee.objects.filter(client_id=client_id, name__iexact=emp_name).first()
                if not matched_emp:
                    matched_emp = Employee.objects.filter(client_id=client_id).first()
                if matched_emp:
                    data["employeeId"] = str(matched_emp.id)

            if "rating" in data and not data.get("final_rating"):
                try:
                    data["final_rating"] = float(data["rating"])
                except Exception:
                    pass

        return super().to_internal_value(data)

    class Meta:
        model = Appraisal
        fields = [
            "id", "cycleId", "employeeId", "employeeName", "employee", "managerId", "stage",
            "status", "self_rating", "manager_rating", "final_rating",
            "selfRating", "managerRating", "finalRating", "rating",
            "strengths", "areas_for_improvement", "development_feedback",
            "hr_comments", "self_comments", "kpiScores", "history",
            "created_at", "updated_at",
        ]
        read_only_fields = ["stage", "status", "created_at", "updated_at"]


class GoalSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employee = serializers.CharField(source="employee.name", read_only=True)
    cycleId = TenantPrimaryKeyRelatedField(
        source="cycle", model="hrms.AppraisalCycle", required=False, allow_null=True
    )
    dueDate = serializers.DateField(source="target_date", required=False, allow_null=True)
    progress = serializers.IntegerField(source="progress_pct", required=False)
    progressPct = serializers.IntegerField(source="progress_pct", required=False)

    def to_internal_value(self, data):
        if isinstance(data, dict):
            data = data.copy()
            raw_id = data.get("id")
            if raw_id:
                try:
                    import uuid
                    uuid.UUID(str(raw_id))
                except Exception:
                    data.pop("id", None)

            raw_cycle = data.get("cycleId") or data.get("cycle")
            is_valid_cycle_uuid = False
            if raw_cycle:
                try:
                    import uuid
                    uuid.UUID(str(raw_cycle))
                    is_valid_cycle_uuid = True
                except Exception:
                    is_valid_cycle_uuid = False

            client_id = self.context.get("client_id")
            if not is_valid_cycle_uuid and client_id:
                active_cycle = AppraisalCycle.objects.filter(client_id=client_id).order_by("-period_start").first()
                if active_cycle:
                    data["cycleId"] = str(active_cycle.id)
                else:
                    data.pop("cycleId", None)
                    data.pop("cycle", None)
        return super().to_internal_value(data)

    class Meta:
        model = Goal
        fields = [
            "id", "employeeId", "employeeName", "employee", "cycle", "cycleId",
            "title", "description", "target_date", "dueDate", "progress_pct",
            "progress", "progressPct", "status", "created_at",
        ]


# ---------------------------------------------------------------------------
# Training (api.md §11.7)
# ---------------------------------------------------------------------------
class TrainerSerializer(BaseModelSerializer):
    class Meta:
        model = Trainer
        fields = [
            "id", "name", "kind", "employee", "organisation", "expertise",
            "rate", "contact", "created_at",
        ]


class TrainingParticipantSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)

    class Meta:
        model = TrainingParticipant
        fields = [
            "id", "employeeId", "employeeName", "attendance_status", "rating",
            "feedback", "evaluated_at",
        ]


class TrainingSerializer(BaseModelSerializer):
    trainerId = TenantPrimaryKeyRelatedField(
        source="trainer", model="hrms.Trainer", required=False, allow_null=True
    )
    trainerName = serializers.CharField(source="trainer.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    participants = TrainingParticipantSerializer(many=True, read_only=True)

    class Meta:
        model = Training
        fields = [
            "id", "title", "description", "type", "trainerId", "trainerName",
            "departmentId", "start_date", "end_date", "venue", "cost", "stage",
            "participants", "created_at", "updated_at",
        ]


# ---------------------------------------------------------------------------
# Assets, documents, policies, calendar, HR admin (api.md §11.8)
# ---------------------------------------------------------------------------
class AssetCategorySerializer(BaseModelSerializer):
    class Meta:
        model = AssetCategory
        fields = ["id", "name", "depreciation_pct", "default_warranty_months"]


class AssetAssignmentSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)

    class Meta:
        model = AssetAssignment
        fields = [
            "id", "employee", "employeeName", "assigned_at", "assigned_by",
            "returned_at", "returned_condition", "notes",
        ]


def _asset_category_named(context, name):
    """The tenant's asset category called ``name``, created on first use."""
    name = str(name or "").strip()
    if not name:
        return None
    request = context.get("request")
    client_id = context.get("client_id") or getattr(request, "client_id", None)
    row = AssetCategory.objects.filter(
        client_id=client_id, name__iexact=name, deleted_at__isnull=True
    ).first()
    return row or AssetCategory.objects.create(client_id=client_id, name=name)


class AssetSerializer(BaseModelSerializer):
    assetCode = serializers.CharField(source="asset_code", read_only=True)
    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.AssetCategory", required=False, allow_null=True
    )
    #: The register form picks a category by name; it is created if new.
    categoryName = serializers.CharField(
        write_only=True, required=False, allow_blank=True, allow_null=True
    )
    employeeCode = serializers.CharField(
        source="assigned_employee.employee_code", read_only=True
    )
    pendingRequests = serializers.SerializerMethodField()
    assignedTo = serializers.CharField(source="assigned_employee.name", read_only=True)
    employeeId = TenantPrimaryKeyRelatedField(
        source="assigned_employee", model="hrms.Employee", required=False, allow_null=True
    )
    dept = serializers.CharField(
        source="assigned_employee.department.name", read_only=True
    )
    serialNumber = serializers.CharField(
        source="serial_number", required=False, allow_null=True, allow_blank=True
    )
    purchaseDate = serializers.DateField(
        source="purchase_date", required=False, allow_null=True
    )
    purchaseCost = MoneyField(source="purchase_cost", required=False, allow_null=True)
    warrantyExpiry = serializers.DateField(
        source="warranty_expiry", required=False, allow_null=True
    )
    history = AssetAssignmentSerializer(many=True, read_only=True)

    class Meta:
        model = Asset
        fields = [
            "id", "assetCode", "name", "category", "categoryId", "categoryName",
            "serialNumber", "assignedTo", "employeeId", "employeeCode", "dept",
            "status", "condition", "purchaseDate", "purchaseCost", "warrantyExpiry",
            "location", "notes", "history", "pendingRequests", "created_at", "updated_at",
        ]
        read_only_fields = ["assetCode", "created_at", "updated_at"]

    def get_pendingRequests(self, asset):
        return sum(
            1 for row in asset.requests.all()
            if row.status == "Pending" and row.deleted_at is None
        )

    def validate(self, attrs):
        name = attrs.pop("categoryName", None)
        if name and not attrs.get("category"):
            attrs["category"] = _asset_category_named(self.context, name)
        return attrs


class AssetRequestSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    dept = serializers.CharField(source="employee.department.name", read_only=True)
    requestType = serializers.ChoiceField(
        source="request_type", choices=AssetRequest.REQUEST_TYPES, required=False
    )
    requestedAssetId = TenantPrimaryKeyRelatedField(
        source="requested_asset", model="hrms.Asset", required=False, allow_null=True
    )
    requestedAsset = serializers.CharField(source="requested_asset.name", read_only=True)
    requestedAssetCode = serializers.CharField(
        source="requested_asset.asset_code", read_only=True
    )
    requestedAssetStatus = serializers.CharField(
        source="requested_asset.status", read_only=True
    )
    itemName = serializers.CharField(
        source="item_name", required=False, allow_blank=True, allow_null=True
    )
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.AssetCategory", required=False, allow_null=True
    )
    category = serializers.CharField(source="category.name", read_only=True)
    categoryName = serializers.CharField(
        write_only=True, required=False, allow_blank=True, allow_null=True
    )
    priority = serializers.ChoiceField(choices=AssetRequest.PRIORITIES, required=False)
    approverName = serializers.CharField(source="approver.name", read_only=True)
    decidedAt = serializers.DateTimeField(source="decided_at", read_only=True)
    fulfilledAssetId = serializers.CharField(source="fulfilled_asset_id", read_only=True)
    fulfilledAsset = serializers.CharField(source="fulfilled_asset.name", read_only=True)
    fulfilledAssetCode = serializers.CharField(
        source="fulfilled_asset.asset_code", read_only=True
    )
    requestedDate = serializers.SerializerMethodField()

    class Meta:
        model = AssetRequest
        fields = [
            "id", "employeeId", "employeeName", "employeeCode", "dept",
            "requestType", "requestedAssetId", "requestedAsset", "requestedAssetCode",
            "requestedAssetStatus", "itemName", "categoryId", "category", "categoryName",
            "priority", "justification", "status", "approver", "approverName",
            "decidedAt", "remark", "fulfilledAssetId", "fulfilledAsset",
            "fulfilledAssetCode", "requestedDate", "created_at",
        ]
        # Status moves only through the approve / reject / fulfil / cancel actions.
        read_only_fields = ["status", "approver", "remark", "created_at"]

    def get_requestedDate(self, row):
        return row.created_at.date().isoformat() if row.created_at else None

    def validate(self, attrs):
        name = attrs.pop("categoryName", None)
        if name and not attrs.get("category"):
            attrs["category"] = _asset_category_named(self.context, name)

        instance = self.instance
        request_type = attrs.get("request_type") or getattr(instance, "request_type", None)
        asset = attrs.get("requested_asset", getattr(instance, "requested_asset", None))
        if request_type is None:
            request_type = "Inventory" if asset is not None else "Other"
            attrs["request_type"] = request_type

        if request_type == "Inventory":
            if asset is None:
                raise serializers.ValidationError(
                    {"requestedAssetId": ["Pick an asset from the inventory."]}
                )
            if instance is None and asset.status != "Available":
                raise serializers.ValidationError({"requestedAssetId": [
                    f"{asset.name} is {asset.status.lower()} and cannot be requested."
                ]})
            attrs.setdefault("category", asset.category)
            attrs.setdefault("item_name", asset.name)
        else:
            attrs["requested_asset"] = None
            item = (attrs.get("item_name") or getattr(instance, "item_name", None) or "").strip()
            if not item:
                raise serializers.ValidationError(
                    {"itemName": ["Describe the item you need."]}
                )

        if instance is None and not (attrs.get("justification") or "").strip():
            raise serializers.ValidationError(
                {"justification": ["Explain why you need it."]}
            )
        return attrs


class HrDocumentSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employee = serializers.CharField(source="employee.name", read_only=True)
    fileId = TenantPrimaryKeyRelatedField(
        source="file", model="core.File", required=False, allow_null=True
    )
    fileSize = serializers.IntegerField(source="file.file_size", read_only=True)
    fileType = serializers.CharField(source="file.content_type", read_only=True)
    fileName = serializers.CharField(source="file.file_name", read_only=True)
    hasFile = serializers.SerializerMethodField()
    expiry = serializers.DateField(source="valid_until", required=False, allow_null=True)
    #: DERIVED from ``valid_until`` at read time (api.md §11.8).
    status = serializers.SerializerMethodField()
    uploadedBy = serializers.CharField(source="created_by.name", read_only=True)
    updatedOn = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = HrDocument
        fields = [
            "id", "title", "category", "employee", "employeeId", "version",
            "expiry", "status", "fileId", "hasFile", "fileSize", "fileType", "fileName", "uploadedBy",
            "updatedOn", "tags", "description", "valid_from", "is_confidential",
            "created_at",
        ]

    def to_internal_value(self, data):
        # Resolve employeeId whether passed as UUID, employee code, or employee name
        if isinstance(data, dict) and "employeeId" in data and data["employeeId"]:
            val = str(data["employeeId"]).strip()
            import uuid
            is_valid_uuid = False
            try:
                uuid.UUID(val)
                is_valid_uuid = True
            except ValueError:
                pass

            if not is_valid_uuid:
                from apps.hrms.models import Employee
                client_id = getattr(self.context.get("request"), "client_id", None) or getattr(getattr(self.context.get("request"), "user", None), "client_id", None)
                qs = Employee.objects.all()
                if client_id:
                    qs = qs.filter(client_id=client_id)
                emp = qs.filter(models.Q(employee_code=val) | models.Q(name__iexact=val)).first()
                if emp:
                    data = {**data, "employeeId": str(emp.id)}
        return super().to_internal_value(data)

    def get_hasFile(self, document):
        return document.file_id is not None

    def get_status(self, document):
        return services.document_status(document)


class PolicyVersionSerializer(BaseModelSerializer):
    changedByName = serializers.CharField(source="changed_by.name", read_only=True)

    class Meta:
        model = PolicyVersion
        fields = ["id", "version", "change_note", "changedByName", "created_at"]


class PolicySerializer(BaseModelSerializer):
    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.PolicyCategory", required=False, allow_null=True
    )
    ownerDept = serializers.CharField(source="owner_department.name", read_only=True)
    ownerDepartmentId = TenantPrimaryKeyRelatedField(
        source="owner_department", model="hrms.Department",
        required=False, allow_null=True,
    )
    applicableTo = serializers.CharField(
        source="applicable_to", required=False, allow_null=True, allow_blank=True
    )
    effectiveDate = serializers.DateField(
        source="effective_date", required=False, allow_null=True
    )
    reviewDate = serializers.DateField(
        source="review_date", required=False, allow_null=True
    )
    approvalRequired = serializers.BooleanField(source="approval_required", required=False)
    ackRequired = serializers.BooleanField(source="ack_required", required=False)
    versionHistory = PolicyVersionSerializer(
        source="version_history", many=True, read_only=True
    )

    class Meta:
        model = Policy
        fields = [
            "id", "name", "category", "categoryId", "ownerDept",
            "ownerDepartmentId", "applicableTo", "version", "effectiveDate",
            "reviewDate", "approvalRequired", "ackRequired", "ack_window_days",
            "status", "summary", "body", "file", "versionHistory",
            "approved_by", "approved_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "version", "status", "approved_by", "approved_at", "created_at", "updated_at",
        ]


class PolicyAcknowledgementSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = PolicyAcknowledgement
        fields = [
            "id", "employee", "employeeName", "employeeCode", "policy_version",
            "acknowledged_at", "status",
        ]

    def get_status(self, acknowledgement):
        policy = self.context.get("policy") or acknowledgement.policy
        return services.acknowledgement_status(acknowledgement, policy)


class PolicyCategorySerializer(BaseModelSerializer):
    class Meta:
        model = PolicyCategory
        fields = ["id", "name", "description"]


class CalendarEventSerializer(BaseModelSerializer):
    startDate = serializers.CharField(source="starts_at", required=False)
    endDate = serializers.CharField(source="ends_at", required=False, allow_null=True)
    date = serializers.CharField(source="starts_at", required=False)
    category = serializers.CharField(source="type", required=False)
    dept = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    virtualLink = serializers.CharField(
        source="virtual_link", required=False, allow_null=True, allow_blank=True
    )
    time = serializers.SerializerMethodField()

    class Meta:
        model = CalendarEvent
        fields = [
            "id", "title", "date", "startDate", "endDate", "type", "category", "time",
            "location", "dept", "departmentId", "organizer", "description",
            "virtualLink", "all_day", "source_type", "source_id", "created_at",
        ]
        read_only_fields = ["source_type", "source_id", "created_at"]

    def to_internal_value(self, data):
        if isinstance(data, dict):
            data = data.copy()
            raw_id = data.get("id")
            if raw_id:
                try:
                    import uuid
                    uuid.UUID(str(raw_id))
                except Exception:
                    data.pop("id", None)

            if not data.get("type") and data.get("category"):
                data["type"] = data["category"]

            start = data.get("startDate") or data.get("date") or data.get("starts_at")
            if start:
                import datetime
                from django.utils import timezone as dj_timezone
                start_str = str(start)[:10]
                try:
                    d = datetime.date.fromisoformat(start_str)
                    data["starts_at"] = dj_timezone.make_aware(datetime.datetime.combine(d, datetime.time(0, 0)))
                except Exception:
                    pass

            end = data.get("endDate") or data.get("ends_at") or start
            if end:
                import datetime
                from django.utils import timezone as dj_timezone
                end_str = str(end)[:10]
                try:
                    d = datetime.date.fromisoformat(end_str)
                    data["ends_at"] = dj_timezone.make_aware(datetime.datetime.combine(d, datetime.time(23, 59, 59)))
                except Exception:
                    pass

        return super().to_internal_value(data)

    def to_representation(self, event):
        ret = super().to_representation(event)
        from django.utils import timezone as dj_timezone
        if event.starts_at:
            local_starts = dj_timezone.localtime(event.starts_at)
            ret["date"] = local_starts.strftime("%Y-%m-%d")
            ret["startDate"] = local_starts.strftime("%Y-%m-%d")
        if event.ends_at:
            local_ends = dj_timezone.localtime(event.ends_at)
            ret["endDate"] = local_ends.strftime("%Y-%m-%d")
        else:
            ret["endDate"] = ret.get("startDate")
        ret["category"] = event.type
        ret["isMandatory"] = (event.type == "Holiday")
        return ret

    def validate(self, attrs):
        attrs = super().validate(attrs)
        import datetime
        from django.utils import timezone as dj_timezone
        starts = attrs.get("starts_at")
        if isinstance(starts, str):
            try:
                if "T" in starts or " " in starts:
                    dt = datetime.datetime.fromisoformat(starts)
                    attrs["starts_at"] = dj_timezone.make_aware(dt) if dj_timezone.is_naive(dt) else dt
                else:
                    d = datetime.date.fromisoformat(starts[:10])
                    attrs["starts_at"] = dj_timezone.make_aware(datetime.datetime.combine(d, datetime.time(0, 0)))
            except Exception:
                pass

        ends = attrs.get("ends_at")
        if isinstance(ends, str):
            try:
                if "T" in ends or " " in ends:
                    dt = datetime.datetime.fromisoformat(ends)
                    attrs["ends_at"] = dj_timezone.make_aware(dt) if dj_timezone.is_naive(dt) else dt
                else:
                    d = datetime.date.fromisoformat(ends[:10])
                    attrs["ends_at"] = dj_timezone.make_aware(datetime.datetime.combine(d, datetime.time(23, 59, 59)))
            except Exception:
                pass

        if not attrs.get("ends_at") and attrs.get("starts_at"):
            attrs["ends_at"] = attrs["starts_at"]

        return attrs

    def get_time(self, event):
        return services.calendar_time_label(event)


class HolidaySerializer(BaseModelSerializer):
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )
    title = serializers.CharField(source="name", required=False)
    type = serializers.CharField(source="holiday_type", required=False, allow_blank=True, allow_null=True)
    appliesTo = serializers.CharField(source="applies_to", required=False, allow_blank=True, allow_null=True)
    day = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = Holiday
        fields = [
            "id", "date", "name", "title", "locationId", "is_optional", "applies_to",
            "appliesTo", "holiday_type", "type", "day", "status", "created_at",
        ]

    def to_internal_value(self, data):
        if isinstance(data, dict):
            data = data.copy()
            raw_id = data.get("id")
            if raw_id:
                try:
                    import uuid
                    uuid.UUID(str(raw_id))
                except Exception:
                    data.pop("id", None)
            if not data.get("name") and data.get("title"):
                data["name"] = data["title"]
            if not data.get("holiday_type") and data.get("type"):
                data["holiday_type"] = data["type"]
            if not data.get("applies_to") and data.get("appliesTo"):
                data["applies_to"] = data["appliesTo"]
            if data.get("date"):
                data["date"] = str(data["date"])[:10]
        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["title"] = instance.name
        ret["type"] = instance.holiday_type or "National Gazetted"
        ret["appliesTo"] = instance.applies_to or "All Locations"
        if instance.date:
            ret["day"] = instance.date.strftime("%A")
            ret["date"] = str(instance.date)
        return ret


class WorkingDaySerializer(BaseModelSerializer):
    class Meta:
        model = WorkingDay
        fields = ["id", "weekday", "is_working", "shift_start", "shift_end", "location"]


class TeamSerializer(BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    leadName = serializers.CharField(source="lead.name", read_only=True)
    memberCount = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = [
            "id", "name", "department", "departmentId", "lead", "leadName",
            "members", "memberCount", "description", "status", "created_at",
        ]

    def get_memberCount(self, team):
        return team.members.count()


class ApprovalChainSerializer(BaseModelSerializer):
    module = serializers.CharField(source="name", required=False)
    name = serializers.CharField(required=False)
    applies_to = serializers.CharField(required=False)
    tier1 = serializers.CharField(required=False, write_only=True)
    tier2 = serializers.CharField(required=False, write_only=True)
    tier3 = serializers.CharField(required=False, write_only=True)
    autoEscalateDays = serializers.CharField(required=False, write_only=True)

    class Meta:
        model = ApprovalChain
        fields = [
            "id", "name", "module", "applies_to", "steps", "status", "created_at",
            "tier1", "tier2", "tier3", "autoEscalateDays",
        ]
        read_only_fields = ["id", "created_at"]

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        ret["module"] = instance.name or instance.applies_to
        steps = instance.steps if isinstance(instance.steps, list) else []
        ret["tier1"] = steps[0].get("approver", "") if len(steps) > 0 and isinstance(steps[0], dict) else ""
        ret["tier2"] = steps[1].get("approver", "") if len(steps) > 1 and isinstance(steps[1], dict) else ""
        ret["tier3"] = steps[2].get("approver", "-") if len(steps) > 2 and isinstance(steps[2], dict) else "-"
        ret["autoEscalateDays"] = steps[0].get("autoEscalateDays", 3) if steps and isinstance(steps[0], dict) else 3
        return ret

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Always remove write_only helper fields from attrs to avoid TypeError in model create
        tier1 = attrs.pop("tier1", None) or self.initial_data.get("tier1")
        tier2 = attrs.pop("tier2", None) or self.initial_data.get("tier2")
        tier3 = attrs.pop("tier3", None) or self.initial_data.get("tier3")
        raw_escalate = attrs.pop("autoEscalateDays", None) or self.initial_data.get("autoEscalateDays", 3)

        # Clean string escalation like "3 days" into integer
        escalate = 3
        if raw_escalate is not None:
            try:
                import re
                m = re.search(r"\d+", str(raw_escalate))
                escalate = int(m.group()) if m else 3
            except Exception:
                escalate = 3

        # Support either module/tier frontend format or standard name/applies_to/steps format
        name = attrs.get("name") or self.initial_data.get("module") or self.initial_data.get("name") or "Workflow"
        attrs["name"] = name
        if not attrs.get("applies_to"):
            attrs["applies_to"] = self.initial_data.get("applies_to") or self.initial_data.get("module", "leave").lower().replace(" ", "_")

        if tier1 or tier2 or tier3:
            steps = []
            if tier1:
                steps.append({"level": 1, "approver": str(tier1), "approver_role": str(tier1), "autoEscalateDays": int(escalate)})
            if tier2:
                steps.append({"level": 2, "approver": str(tier2), "approver_role": str(tier2)})
            if tier3 and tier3 != "-":
                steps.append({"level": 3, "approver": str(tier3), "approver_role": str(tier3)})
            attrs["steps"] = steps

        # Strip client-generated non-UUID id (e.g. 'AC-01')
        raw_id = attrs.get("id") or self.initial_data.get("id")
        if raw_id:
            try:
                import uuid
                uuid.UUID(str(raw_id))
            except Exception:
                attrs.pop("id", None)

        return attrs

    def create(self, validated_data):
        for k in ("tier1", "tier2", "tier3", "autoEscalateDays"):
            validated_data.pop(k, None)
        raw_id = validated_data.get("id")
        if raw_id:
            try:
                import uuid
                uuid.UUID(str(raw_id))
            except Exception:
                validated_data.pop("id", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        for k in ("tier1", "tier2", "tier3", "autoEscalateDays", "id"):
            validated_data.pop(k, None)
        return super().update(instance, validated_data)


class TerminationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    lastWorkingDay = serializers.DateField(
        source="last_working_day", required=False, allow_null=True
    )
    terminationDate = serializers.DateField(
        source="last_working_day", required=False, allow_null=True
    )
    settlementAmount = MoneyField(source="settlement_amount", required=False, allow_null=True)

    class Meta:
        model = Termination
        fields = [
            "id", "employeeId", "employeeName", "reason", "lastWorkingDay",
            "terminationDate", "letter_file", "settlement_amount", "settlementAmount",
            "status", "created_at",
        ]


class ResignationChecklistItemSerializer(BaseModelSerializer):
    class Meta:
        model = ResignationChecklistItem
        fields = ["id", "title", "owner", "completed_at"]


class ResignationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    submittedOn = serializers.DateField(source="submitted_on")
    noticePeriodDays = serializers.IntegerField(source="notice_period_days", required=False)
    lastWorkingDay = serializers.DateField(
        source="last_working_day", required=False, allow_null=True
    )
    handoverTo = serializers.CharField(source="handover_to", required=False, allow_null=True)
    exitInterviewAt = serializers.DateTimeField(source="exit_interview_at", required=False, allow_null=True)
    checklist = ResignationChecklistItemSerializer(many=True, read_only=True)

    class Meta:
        model = Resignation
        fields = [
            "id", "employeeId", "employeeName", "submittedOn", "noticePeriodDays",
            "lastWorkingDay", "exit_interview_at", "exitInterviewAt", "reason",
            "handover_to", "handoverTo", "status", "checklist", "created_at",
        ]


class ComplaintSerializer(BaseModelSerializer):
    """``is_anonymous`` is honoured by nulling the raiser in every serialiser --
    including exports (db.md §11.8)."""

    raisedBy = serializers.SerializerMethodField()
    complainant = serializers.SerializerMethodField()
    raisedByEmployeeId = TenantPrimaryKeyRelatedField(
        source="raised_by_employee", model="hrms.Employee",
        required=False, allow_null=True, write_only=True,
    )
    againstEmployeeId = TenantPrimaryKeyRelatedField(
        source="against_employee", model="hrms.Employee",
        required=False, allow_null=True,
    )
    againstName = serializers.SerializerMethodField()
    against = serializers.SerializerMethodField()
    description = serializers.CharField(required=False, allow_blank=True)
    summary = serializers.CharField(source="description", required=False, allow_blank=True)
    isAnonymous = serializers.BooleanField(source="is_anonymous", required=False)
    raisedDate = serializers.DateTimeField(source="created_at", read_only=True)
    filedOn = serializers.DateTimeField(source="created_at", read_only=True)
    priority = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    assignedInvestigator = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = Complaint
        fields = [
            "id", "raisedBy", "complainant", "raisedByEmployeeId", "againstEmployeeId",
            "againstName", "against", "category", "description", "summary", "isAnonymous",
            "status", "assigned_to", "resolution", "resolved_at", "created_at",
            "raisedDate", "filedOn", "priority", "assignedInvestigator",
        ]
        read_only_fields = ["resolved_at", "created_at", "raisedDate", "filedOn"]

    def to_internal_value(self, data):
        if isinstance(data, dict):
            data = data.copy()
            # Strip non-UUID id like 'GRV-304'
            raw_id = data.get("id")
            if raw_id:
                try:
                    import uuid
                    uuid.UUID(str(raw_id))
                except Exception:
                    data.pop("id", None)

            # Ensure description is populated from summary or fallback
            if not data.get("description"):
                data["description"] = data.get("summary") or "Grievance / Complaint filed"

            # Resolve complainant to employee if name provided
            client_id = self.context.get("client_id")
            raw_complainant = data.get("complainant") or data.get("raisedBy")
            if raw_complainant and not data.get("raisedByEmployeeId") and client_id:
                emp = Employee.objects.filter(client_id=client_id, name__iexact=str(raw_complainant).strip()).first()
                if emp:
                    data["raisedByEmployeeId"] = str(emp.id)

            # Resolve against to employee if name provided
            raw_against = data.get("against") or data.get("againstName")
            if raw_against and not data.get("againstEmployeeId") and client_id:
                emp = Employee.objects.filter(client_id=client_id, name__iexact=str(raw_against).strip()).first()
                if emp:
                    data["againstEmployeeId"] = str(emp.id)

        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        import json
        meta = {}
        priority = attrs.pop("priority", None) or self.initial_data.get("priority")
        if priority:
            meta["priority"] = str(priority)
        investigator = attrs.pop("assignedInvestigator", None) or self.initial_data.get("assignedInvestigator")
        if investigator:
            meta["assignedInvestigator"] = str(investigator)
        complainant = self.initial_data.get("complainant")
        if complainant and not attrs.get("raised_by_employee"):
            meta["complainant"] = str(complainant)
        against = self.initial_data.get("against")
        if against and not attrs.get("against_employee"):
            meta["against"] = str(against)

        if meta and not attrs.get("resolution"):
            attrs["resolution"] = json.dumps(meta)
        return attrs

    def create(self, validated_data):
        for k in ("priority", "assignedInvestigator"):
            validated_data.pop(k, None)
        raw_id = validated_data.get("id")
        if raw_id:
            try:
                import uuid
                uuid.UUID(str(raw_id))
            except Exception:
                validated_data.pop("id", None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        for k in ("priority", "assignedInvestigator", "id"):
            validated_data.pop(k, None)
        return super().update(instance, validated_data)

    def get_raisedBy(self, complaint):
        if complaint.is_anonymous:
            return None
        return complaint.raised_by_employee.name if complaint.raised_by_employee_id else None

    def get_complainant(self, complaint):
        return self.get_raisedBy(complaint)

    def get_againstName(self, complaint):
        return complaint.against_employee.name if complaint.against_employee_id else None

    def get_against(self, complaint):
        return self.get_againstName(complaint)

    def to_representation(self, complaint):
        ret = super().to_representation(complaint)
        import json
        meta = {}
        if complaint.resolution and complaint.resolution.strip().startswith("{"):
            try:
                meta = json.loads(complaint.resolution)
            except Exception:
                pass
        ret["priority"] = meta.get("priority", "High")
        ret["assignedInvestigator"] = meta.get("assignedInvestigator", "Ayesha Khan (HR Director)")
        if not ret.get("complainant"):
            ret["complainant"] = meta.get("complainant", "")
            ret["raisedBy"] = ret["complainant"]
        if not ret.get("against"):
            ret["against"] = meta.get("against", "")
            ret["againstName"] = ret["against"]
        ret["filedOn"] = complaint.created_at.strftime("%Y-%m-%d") if complaint.created_at else ""
        return ret


# =============================================================================
# Textile Operational HR & Commercial Serializers (Oscar Letter Pad Scope)
# =============================================================================

class SalespersonIncentiveSchemeSerializer(BaseModelSerializer):
    salespersonCount = serializers.IntegerField(
        source="applicable_salespersons.count", read_only=True
    )

    class Meta:
        model = SalespersonIncentiveScheme
        fields = [
            "id", "name", "code", "basis", "frequency", "slab_type",
            "slabs", "flat_rate_percent", "deduct_returns", "deduct_bad_debts",
            "is_active", "description", "salespersonCount", "applicable_salespersons",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class SalespersonIncentiveAccrualSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee", required=False)
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    schemeName = serializers.CharField(source="scheme.name", read_only=True)

    class Meta:
        model = SalespersonIncentiveAccrual
        fields = [
            "id", "employee", "employeeId", "employeeName", "employeeCode", "scheme", "schemeName",
            "period_type", "period_name", "start_date", "end_date",
            "gross_sales", "returns_deduction", "net_eligible_sales",
            "applicable_rate", "calculated_incentive", "status", "hold_reason",
            "approved_by", "approved_at", "paid_at", "journal_entry", "notes",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class BrokerCommissionRecordSerializer(BaseModelSerializer):
    brokerName = serializers.CharField(source="broker.name", read_only=True)
    brokerCode = serializers.CharField(source="broker.code", read_only=True)
    partyName = serializers.CharField(source="party.name", read_only=True)
    invoiceNumber = serializers.CharField(source="invoice.invoice_number", read_only=True)
    salespersonName = serializers.CharField(source="salesperson.name", read_only=True)

    class Meta:
        model = SalesBrokerCommission
        fields = [
            "id", "commission_number", "broker", "brokerName", "brokerCode",
            "invoice", "invoiceNumber", "party", "partyName", "salesperson", "salespersonName",
            "invoice_date", "invoice_amount", "commission_basis", "commission_type",
            "rate", "commission_amount", "paid_amount", "status",
            "approved_by", "approved_at", "paid_at", "notes",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


# ---------------------------------------------------------------------------
# Employee Lifecycle Serializers (§11.9)
# ---------------------------------------------------------------------------
class EmployeeTransferSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    fromDepartmentName = serializers.SerializerMethodField()
    toDepartmentName = serializers.SerializerMethodField()
    from_department = serializers.SerializerMethodField()
    to_department = serializers.SerializerMethodField()
    effectiveDate = serializers.DateField(source="effective_date", required=False)

    def get_fromDepartmentName(self, obj):
        return (obj.from_department.name if obj.from_department else None) or obj.from_department_name or ""

    def get_toDepartmentName(self, obj):
        return (obj.to_department.name if obj.to_department else None) or obj.to_department_name or ""

    def get_from_department(self, obj):
        return self.get_fromDepartmentName(obj)

    def get_to_department(self, obj):
        return self.get_toDepartmentName(obj)

    class Meta:
        model = EmployeeTransfer
        fields = [
            "id", "employee", "employeeId", "employeeName",
            "from_department", "to_department",
            "fromDepartmentName", "toDepartmentName",
            "from_location", "to_location",
            "effective_date", "effectiveDate",
            "reason", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class EmployeePromotionSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    fromDesignationTitle = serializers.SerializerMethodField()
    toDesignationTitle = serializers.SerializerMethodField()
    previous_designation = serializers.SerializerMethodField()
    new_designation = serializers.SerializerMethodField()
    effectiveDate = serializers.DateField(source="effective_date", required=False)
    previousSalary = serializers.DecimalField(source="previous_salary", max_digits=18, decimal_places=2, required=False)
    newSalary = serializers.DecimalField(source="new_salary", max_digits=18, decimal_places=2, required=False)

    def get_fromDesignationTitle(self, obj):
        return (obj.previous_designation.title if obj.previous_designation else None) or obj.previous_designation_title or ""

    def get_toDesignationTitle(self, obj):
        return (obj.new_designation.title if obj.new_designation else None) or obj.new_designation_title or ""

    def get_previous_designation(self, obj):
        return self.get_fromDesignationTitle(obj)

    def get_new_designation(self, obj):
        return self.get_toDesignationTitle(obj)

    class Meta:
        model = EmployeePromotion
        fields = [
            "id", "employee", "employeeId", "employeeName",
            "previous_designation", "new_designation",
            "fromDesignationTitle", "toDesignationTitle",
            "previous_salary", "previousSalary",
            "new_salary", "newSalary",
            "effective_date", "effectiveDate",
            "notes", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class EmployeeWarningSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    title = serializers.CharField(source="subject", required=False)
    severity = serializers.CharField(source="warning_level", required=False)
    warning_date = serializers.DateField(source="issue_date", required=False)
    date = serializers.DateField(source="issue_date", required=False)
    explanation = serializers.CharField(source="description", required=False)

    class Meta:
        model = EmployeeWarning
        fields = [
            "id", "employee", "employeeId", "employeeName",
            "subject", "title", "warning_level", "severity",
            "issue_date", "warning_date", "date",
            "description", "explanation", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class EmployeeAwardSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    title = serializers.CharField(source="award_name", required=False)
    presentation_date = serializers.DateField(source="award_date", required=False)
    date = serializers.DateField(source="award_date", required=False)
    cash_reward = serializers.DecimalField(source="gift_amount", max_digits=18, decimal_places=2, required=False)

    class Meta:
        model = EmployeeAward
        fields = [
            "id", "employee", "employeeId", "employeeName",
            "award_name", "title", "category",
            "award_date", "presentation_date", "date",
            "gift_amount", "cash_reward", "citation",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class HrmsIncentiveSerializer(_PMSIncentiveCalculationSerializer):
    """A canonical pms.IncentiveCalculation in the shape the HRMS Incentives
    page reads (the old SalespersonIncentiveAccrual fields and statuses)."""

    #: Ledger status -> the page's workflow status.
    STATUS_MAP = {
        "Calculated": "ACCRUED",
        "Approved": "APPROVED",
        "Posted": "APPROVED",
        "Paid": "PAID",
        "Reversed": "REVERSED",
    }

    status = serializers.SerializerMethodField()
    ledger_status = serializers.CharField(source="status", read_only=True)
    period_name = serializers.CharField(source="period_label", read_only=True)
    start_date = serializers.DateField(source="period_start", read_only=True)
    end_date = serializers.DateField(source="period_end", read_only=True)
    gross_sales = serializers.SerializerMethodField()
    returns_deduction = serializers.SerializerMethodField()
    net_eligible_sales = serializers.SerializerMethodField()
    applicable_rate = serializers.SerializerMethodField()
    calculated_incentive = serializers.SerializerMethodField()
    payable_amount = serializers.SerializerMethodField()
    hold_reason = serializers.SerializerMethodField()
    scheme_name = serializers.CharField(source="scheme.name", read_only=True)
    payable_now = serializers.SerializerMethodField()
    next_payout_date = serializers.SerializerMethodField()
    payout_after_days = serializers.IntegerField(source="scheme.payout_after_days", read_only=True)

    class Meta(_PMSIncentiveCalculationSerializer.Meta):
        fields = _PMSIncentiveCalculationSerializer.Meta.fields + [
            "ledger_status", "period_name", "start_date", "end_date",
            "gross_sales", "returns_deduction", "net_eligible_sales",
            "applicable_rate", "calculated_incentive", "payable_amount",
            "hold_reason", "scheme_name", "payable_now", "next_payout_date",
            "payout_after_days",
        ]

    @staticmethod
    def _money(value):
        return float(round2(D(value or 0)))

    def _base(self, obj):
        base = (obj.detail or {}).get("baseAmount")
        return D(base) if base not in (None, "") else D(obj.net_sales or 0)

    def get_status(self, obj):
        if (obj.detail or {}).get("managementHold") and obj.status not in ("Paid", "Reversed"):
            return "HELD"
        return self.STATUS_MAP.get(obj.status, (obj.status or "").upper())

    def get_gross_sales(self, obj):
        return self._money(D(obj.net_sales or 0) + D(obj.returns_total or 0))

    def get_returns_deduction(self, obj):
        return self._money(obj.returns_total)

    def get_net_eligible_sales(self, obj):
        return self._money(self._base(obj))

    def get_applicable_rate(self, obj):
        # Effective rate, so tiered slabs show what was actually earned.
        base = self._base(obj)
        if base > ZERO:
            return float(round2(D(obj.incentive_amount or 0) * D(100) / base))
        return float(round2(D(obj.scheme.rate_pct or 0)))

    def get_calculated_incentive(self, obj):
        return self._money(obj.incentive_amount)

    def get_payable_amount(self, obj):
        return self._money(D(obj.incentive_amount or 0) - D(obj.hold_amount or 0))

    def _schedule(self, obj):
        # One payout schedule per row, shared by the fields that read it.
        if not hasattr(obj, "_payout_schedule"):
            from apps.pms.production_services import incentive_payout_schedule

            obj._payout_schedule = incentive_payout_schedule(obj)
        return obj._payout_schedule

    def get_payable_now(self, obj):
        if obj.status in ("Paid", "Reversed"):
            return 0.0
        payable = D(obj.incentive_amount or 0) - D(obj.hold_amount or 0)
        now = min(payable, self._schedule(obj)["eligible"]) - D(obj.paid_amount or 0)
        return self._money(max(ZERO, now))

    def get_next_payout_date(self, obj):
        if obj.status in ("Paid", "Reversed"):
            return None
        next_date = self._schedule(obj)["nextDate"]
        return next_date.isoformat() if next_date else None

    def get_hold_reason(self, obj):
        hold = (obj.detail or {}).get("managementHold")
        if hold:
            return hold.get("reason") or "Management hold"
        if D(obj.hold_amount or 0) > ZERO:
            return f"Rs. {round2(D(obj.hold_amount))} held until the customer pays"
        if obj.status in ("Approved", "Posted") and self.get_payable_now(obj) <= 0:
            schedule = self._schedule(obj)
            if schedule["nextDate"]:
                return f"Payable from {schedule['nextDate']:%d %b %Y} ({schedule['days']} days after payment)"
            return f"Awaiting customer payment ({schedule['days']}-day rule)"
        return ""

