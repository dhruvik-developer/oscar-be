"""Tests for Punch In / Punch Out, punch timeline, working hours and payroll integration."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Client, Role, User
from apps.hrms.models import Attendance, AttendanceBreak, AttendancePunch, Department, Employee, PayrollRun, Payslip
from apps.hrms import services


class PunchSystemTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(
            name="Punch Test Client",
            slug="punch-client",
        )
        self.dept = Department.objects.create(
            client=self.client_obj,
            name="Engineering",
        )
        self.employee = Employee.objects.create(
            client=self.client_obj,
            employee_code="EMP-101",
            name="Marcus Vance",
            email="marcus@example.com",
            department=self.dept,
            joining_date=date(2025, 1, 1),
            shift="General",
            standard_salary=Decimal("60000"),
        )
        self.user = User.objects.create(
            client=self.client_obj,
            email="marcus@example.com",
            name="Marcus Vance",
            employee=self.employee,
        )


    def test_late_punch_in_and_break_working_hour_shortfall_affects_payroll(self):
        """Total working hour = punch out - punch in - (break out - break in).
        If working hours < 8 hours (e.g. 7 hours), payroll deducts the 1 hour shortfall.
        """
        work_date = date(2026, 9, 16)
        # 10:30 Punch In (1 hour late)
        t_in = timezone.make_aware(datetime.combine(work_date, time(10, 30)))
        # 13:00 Break In
        t_b_in = timezone.make_aware(datetime.combine(work_date, time(13, 0)))
        # 14:00 Break Out (1 hr break)
        t_b_out = timezone.make_aware(datetime.combine(work_date, time(14, 0)))
        # 18:30 Punch Out
        t_out = timezone.make_aware(datetime.combine(work_date, time(18, 30)))

        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t_in, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t_b_in, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_OUT", punch_time=t_b_out, user=self.user)
        p_out, status = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t_out, user=self.user)

        # Net working duration: (18:30 - 10:30) - 1 hour break = 7.0 hours
        self.assertEqual(status["working_hours"], 7.0)
        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(float(att.working_hours), 7.0)
        self.assertEqual(att.total_break_minutes, 60)

        # Process payroll:
        # Standard salary = 26000. Total days = 22.
        # Shortfall = 8 - 7 = 1 hour.
        # Hourly rate = 26000 / (22 * 8) = 147.7272...
        # 1 hr shortfall deduction = 147.73
        payroll_result = services.process_payroll(
            client=self.client_obj,
            period_month=date(2026, 9, 1),
            employee_ids=[self.employee.id],
            user=self.user,
        )
        payslip = Payslip.objects.filter(payroll_run=payroll_result["run"], employee=self.employee).first()
        self.assertIsNotNone(payslip)
        self.assertGreater(payslip.deductions, Decimal("0.00"))
        # Check component was created
        comp = payslip.components.filter(kind="deduction").first()
        self.assertIsNotNone(comp)
        self.assertIn("Shortfall", comp.name)

    def test_single_punch_in_and_out(self):
        work_date = date(2026, 9, 28)
        # 09:30 AM Punch In
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        p1, status1 = services.record_punch(
            client=self.client_obj,
            employee=self.employee,
            punch_type="IN",
            punch_time=t1,
            user=self.user,
        )
        self.assertEqual(p1.punch_type, "IN")
        self.assertTrue(status1["is_punched_in"])
        self.assertEqual(status1["status"], "Working")

        # 06:00 PM Punch Out (8.5 hours later)
        t2 = timezone.make_aware(datetime.combine(work_date, time(18, 0)))
        p2, status2 = services.record_punch(
            client=self.client_obj,
            employee=self.employee,
            punch_type="OUT",
            punch_time=t2,
            user=self.user,
        )
        self.assertEqual(p2.punch_type, "OUT")
        self.assertFalse(status2["is_punched_in"])
        self.assertEqual(status2["status"], "Punched Out")
        self.assertEqual(status2["working_hours"], 8.5)
        self.assertEqual(status2["formatted_working_time"], "08h 30m")

        # Check Attendance row
        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.hours, Decimal("8.50"))
        self.assertEqual(att.status, "Present")
        self.assertEqual(att.late_minutes, 0)
        self.assertEqual(att.punches.count(), 2)

    def test_single_punch_per_day_enforced(self):
        """Only one Punch In and Punch Out allowed per day."""
        work_date = date(2026, 9, 28)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(18, 30)))
        t3 = timezone.make_aware(datetime.combine(work_date, time(19, 0)))

        # 1. Punch In succeeds
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        # 2. Punch Out succeeds
        _, status_out = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)
        self.assertTrue(status_out["day_completed"])
        self.assertFalse(status_out["can_punch_in"])
        self.assertFalse(status_out["can_punch_out"])

        # 3. Second Punch In on the same day must be rejected
        with self.assertRaises(Exception) as ctx:
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t3, user=self.user)
        self.assertIn("already completed your punch in and punch out for today", str(ctx.exception))

    def test_validation_prevents_in_in_and_out_out(self):
        work_date = date(2026, 9, 28)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(10, 0)))

        # 1. OUT without IN
        with self.assertRaises(Exception):
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t1, user=self.user)

        # 2. IN succeeds
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)

        # 3. Second IN fails
        with self.assertRaises(Exception):
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t2, user=self.user)

    def test_late_arrival_detection(self):
        work_date = date(2026, 9, 28)
        # Shift start 09:30 + 15 min grace = 09:45. Arrived at 10:15 (45 min late past 09:30)
        t1 = timezone.make_aware(datetime.combine(work_date, time(10, 15)))
        p1, status = services.record_punch(
            client=self.client_obj,
            employee=self.employee,
            punch_type="IN",
            punch_time=t1,
            user=self.user,
        )
        self.assertEqual(status["late_minutes"], 45)
        self.assertEqual(status["attendance_status"], "Late")

        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.late_minutes, 45)
        self.assertEqual(att.status, "Late")

    def test_payroll_uses_attendance_punches(self):
        """Attendance derived from punches flows seamlessly into monthly payroll calculation."""
        work_date = date(2026, 9, 15)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(18, 0)))

        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)

        # Process payroll for September 2026
        period_month = date(2026, 9, 1)
        payroll_result = services.process_payroll(
            client=self.client_obj,
            period_month=period_month,
            employee_ids=[self.employee.id],
            user=self.user,
        )
        self.assertIsNotNone(payroll_result)
        run = payroll_result["run"]
        payslip = Payslip.objects.filter(payroll_run=run, employee=self.employee).first()
        self.assertIsNotNone(payslip)
        # Present for 1 day
        self.assertEqual(payslip.attended_days, Decimal("1.00"))
        self.assertTrue(payslip.net_payable > 0)

    def test_punch_in_check_out_is_none_until_punch_out(self):
        work_date = date(2026, 9, 28)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertIsNotNone(att.check_in)
        self.assertIsNone(att.check_out)

        # After punch out, check_out is set
        t2 = timezone.make_aware(datetime.combine(work_date, time(18, 30)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)
        att.refresh_from_db()
        self.assertEqual(att.check_out, t2)

    def test_early_punch_out_marking_and_regularization_link(self):
        """Early punch out calculates early_leaving_minutes and links with AttendanceRegularization."""
        from apps.hrms.models import AttendanceRegularization
        work_date = date(2026, 9, 28)
        # Shift is 09:30 - 18:30 (9 hours total). Leaving at 15:00 = 3.5 hours (210 min) early. Worked 5.5 hours.
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(15, 0)))

        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        p_out, status = services.record_punch(
            client=self.client_obj,
            employee=self.employee,
            punch_type="OUT",
            punch_time=t2,
            early_reason="Doctor appointment",
            request_regularization=True,
            user=self.user,
        )

        self.assertTrue(status["is_early_out"])
        self.assertEqual(status["early_leaving_minutes"], 210)
        self.assertTrue(status["has_pending_regularization"])

        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.early_leaving_minutes, 210)
        self.assertIn("Doctor appointment", att.remark)

        # Verify AttendanceRegularization row was created in Pending status
        reg = AttendanceRegularization.objects.filter(client=self.client_obj, employee=self.employee, work_date=work_date).first()
        self.assertIsNotNone(reg)
        self.assertEqual(reg.status, "Pending")
        self.assertEqual(reg.requested_status, "Present")
        self.assertIn("Doctor appointment", reg.reason)

    def test_rapid_duplicate_punch_idempotent(self):
        work_date = date(2026, 9, 28)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        p1, s1 = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        # 2 seconds later accidental duplicate click
        t2 = t1 + timedelta(seconds=2)
        p2, s2 = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t2, user=self.user)
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(AttendancePunch.objects.filter(employee=self.employee, work_date=work_date).count(), 1)

    def test_half_day_rule_under_threshold(self):
        work_date = date(2026, 9, 28)
        # Worked 2.5 hours (under 4h threshold)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(12, 0)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)
        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.status, "Half Day")
        self.assertEqual(att.hours, Decimal("2.50"))

    def test_overtime_calculation(self):
        work_date = date(2026, 9, 28)
        # Worked 9.5 hours (8h full day + 1.5h overtime)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 0)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(18, 30)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        p, status = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)
        self.assertEqual(status["overtime_hours"], 1.5)
        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.overtime_hours, Decimal("1.50"))

    def test_early_punch_out_payroll_and_regularization_approval(self):
        """Early punch out under 4 hours counts as Half Day in payroll; approved regularization restores full day."""
        from apps.hrms.models import AttendanceRegularization
        work_date = date(2026, 9, 20)
        # Worked 3 hours: 09:30 to 12:30 -> Under 4h half-day threshold
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(12, 30)))

        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        services.record_punch(
            client=self.client_obj,
            employee=self.employee,
            punch_type="OUT",
            punch_time=t2,
            early_reason="Sudden illness",
            request_regularization=True,
            user=self.user,
        )

        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.status, "Half Day")
        self.assertEqual(att.early_leaving_minutes, 360) # 18:30 - 12:30 = 6 hours = 360 min

        # 1. Before regularization approval: Payroll calculates 0.5 days attended
        payroll_1 = services.process_payroll(
            client=self.client_obj,
            period_month=date(2026, 9, 1),
            employee_ids=[self.employee.id],
            user=self.user,
        )
        payslip_1 = Payslip.objects.get(payroll_run=payroll_1["run"], employee=self.employee)
        self.assertEqual(payslip_1.attended_days, Decimal("0.50"))

        # 2. Manager approves regularization request
        reg = AttendanceRegularization.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        reg.status = "Approved"
        reg.approver = self.user
        reg.decided_at = timezone.now()
        reg.save()
        # Updating attendance via regularization service
        services.mark_attendance(
            client=self.client_obj,
            employee=self.employee,
            work_date=work_date,
            check_in=reg.requested_check_in,
            check_out=reg.requested_check_out,
            status=reg.requested_status, # "Present"
            remark=f"Regularized: {reg.reason}",
            source="regularization",
            user=self.user,
        )

        att.refresh_from_db()
        self.assertEqual(att.status, "Present")

        # 3. After regularization approval: Payroll calculates 1.0 day attended (full day pay restored)
        payroll_2 = services.process_payroll(
            client=self.client_obj,
            period_month=date(2026, 9, 1),
            employee_ids=[self.employee.id],
            user=self.user,
        )
        payslip_2 = Payslip.objects.get(payroll_run=payroll_2["run"], employee=self.employee)
        self.assertEqual(payslip_2.attended_days, Decimal("1.00"))

    def test_punch_in_break_in_break_out_punch_out_single_break(self):
        """Workflow: Punch In 09:00 -> Break In 13:00 -> Break Out 13:30 -> Punch Out 18:00."""
        work_date = date(2026, 10, 5)
        t_punch_in = timezone.make_aware(datetime.combine(work_date, time(9, 0)))
        t_break_in = timezone.make_aware(datetime.combine(work_date, time(13, 0)))
        t_break_out = timezone.make_aware(datetime.combine(work_date, time(13, 30)))
        t_punch_out = timezone.make_aware(datetime.combine(work_date, time(18, 0)))

        # 1. State 1 -> State 2: Punch In
        p_in, s1 = services.record_punch(
            client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t_punch_in, user=self.user
        )
        self.assertEqual(s1["status"], "Working")
        self.assertTrue(s1["is_punched_in"])
        self.assertFalse(s1["is_on_break"])
        self.assertFalse(s1["can_punch_in"])
        self.assertTrue(s1["can_break_in"])
        self.assertFalse(s1["can_break_out"])
        self.assertTrue(s1["can_punch_out"])

        # 2. State 2 -> State 3: Break In (start break)
        b_in, s2 = services.record_punch(
            client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t_break_in, user=self.user
        )
        self.assertEqual(s2["status"], "On Break")
        self.assertTrue(s2["is_punched_in"])
        self.assertTrue(s2["is_on_break"])
        self.assertFalse(s2["can_punch_in"])
        self.assertFalse(s2["can_break_in"])
        self.assertTrue(s2["can_break_out"])
        self.assertFalse(s2["can_punch_out"])

        # 3. State 3 -> State 4: Break Out (end break, resume working)
        b_out, s3 = services.record_punch(
            client=self.client_obj, employee=self.employee, punch_type="BREAK_OUT", punch_time=t_break_out, user=self.user
        )
        self.assertEqual(s3["status"], "Working")
        self.assertTrue(s3["is_punched_in"])
        self.assertFalse(s3["is_on_break"])
        self.assertFalse(s3["can_punch_in"])
        self.assertTrue(s3["can_break_in"])
        self.assertFalse(s3["can_break_out"])
        self.assertTrue(s3["can_punch_out"])
        self.assertEqual(s3["total_break_minutes"], 30)

        # 4. State 4 -> State 5: Punch Out (session completed)
        p_out, s4 = services.record_punch(
            client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t_punch_out, user=self.user
        )
        self.assertEqual(s4["status"], "Punched Out")
        self.assertFalse(s4["is_punched_in"])
        self.assertFalse(s4["is_on_break"])
        self.assertTrue(s4["day_completed"])
        self.assertFalse(s4["can_punch_in"])
        self.assertFalse(s4["can_break_in"])
        self.assertFalse(s4["can_break_out"])
        self.assertFalse(s4["can_punch_out"])

        # Durations:
        # Total attendance duration = 18:00 - 09:00 = 9h = 32400s
        # Total break duration = 13:30 - 13:00 = 30m = 1800s
        # Net working duration = 9h - 30m = 8h 30m = 8.5h
        self.assertEqual(s4["attendance_hours"], 9.0)
        self.assertEqual(s4["total_break_seconds"], 1800)
        self.assertEqual(s4["total_break_minutes"], 30)
        self.assertEqual(s4["formatted_break_time"], "00h 30m")
        self.assertEqual(s4["working_hours"], 8.5)
        self.assertEqual(s4["formatted_working_time"], "08h 30m")

        # Database checks
        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.working_hours, Decimal("8.50"))
        self.assertEqual(att.total_break_minutes, 30)
        self.assertEqual(att.total_attendance_hours, Decimal("9.00"))
        self.assertEqual(att.breaks.count(), 1)
        brk = att.breaks.first()
        self.assertEqual(brk.duration_seconds, 1800)
        self.assertEqual(brk.duration_minutes, 30)

    def test_multiple_breaks_workflow(self):
        """Workflow with multiple breaks: Punch In -> Break In 1 -> Break Out 1 -> Break In 2 -> Break Out 2 -> Punch Out."""
        work_date = date(2026, 10, 6)
        t_in = timezone.make_aware(datetime.combine(work_date, time(9, 0)))
        t_b1_in = timezone.make_aware(datetime.combine(work_date, time(11, 0)))
        t_b1_out = timezone.make_aware(datetime.combine(work_date, time(11, 15))) # 15m
        t_b2_in = timezone.make_aware(datetime.combine(work_date, time(13, 0)))
        t_b2_out = timezone.make_aware(datetime.combine(work_date, time(13, 30))) # 30m
        t_out = timezone.make_aware(datetime.combine(work_date, time(18, 0)))

        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t_in, user=self.user)
        # Break 1
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t_b1_in, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_OUT", punch_time=t_b1_out, user=self.user)
        # Break 2
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t_b2_in, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_OUT", punch_time=t_b2_out, user=self.user)
        # Punch Out
        _, status = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t_out, user=self.user)

        # Expected:
        # Total attendance: 9h
        # Total break: 45m (15m + 30m = 2700s)
        # Net working: 8h 15m (8.25h)
        self.assertEqual(status["total_break_seconds"], 2700)
        self.assertEqual(status["total_break_minutes"], 45)
        self.assertEqual(status["formatted_break_time"], "00h 45m")
        self.assertEqual(status["working_hours"], 8.25)
        self.assertEqual(status["formatted_working_time"], "08h 15m")
        self.assertEqual(len(status["breaks"]), 2)

    def test_break_in_before_punch_in_rejected(self):
        """Break In before Punch In must be rejected."""
        work_date = date(2026, 10, 7)
        t = timezone.make_aware(datetime.combine(work_date, time(9, 0)))
        with self.assertRaises(Exception) as ctx:
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t, user=self.user)
        self.assertIn("before punching in", str(ctx.exception))

    def test_break_out_without_break_in_rejected(self):
        """Break Out without an active break must be rejected."""
        work_date = date(2026, 10, 7)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 0)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(11, 0)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)

        with self.assertRaises(Exception) as ctx:
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_OUT", punch_time=t2, user=self.user)
        self.assertIn("No active break to end", str(ctx.exception))

    def test_duplicate_break_in_rejected(self):
        """Cannot Break In twice without Break Out in between."""
        work_date = date(2026, 10, 7)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 0)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(11, 0)))
        t3 = timezone.make_aware(datetime.combine(work_date, time(11, 10)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t2, user=self.user)

        with self.assertRaises(Exception) as ctx:
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t3, user=self.user)
        self.assertIn("already on break", str(ctx.exception))

    def test_punch_out_during_active_break_rejected(self):
        """Cannot Punch Out while on an active break."""
        work_date = date(2026, 10, 7)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 0)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(12, 0)))
        t3 = timezone.make_aware(datetime.combine(work_date, time(13, 0)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t2, user=self.user)

        with self.assertRaises(Exception) as ctx:
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t3, user=self.user)
        self.assertIn("while a break is active", str(ctx.exception))

    def test_break_actions_after_day_completed_rejected(self):
        """Cannot Break In or Break Out after Punch Out."""
        work_date = date(2026, 10, 7)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 0)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(18, 0)))
        t3 = timezone.make_aware(datetime.combine(work_date, time(19, 0)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)

        with self.assertRaises(Exception) as ctx1:
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t3, user=self.user)
        self.assertIn("completed", str(ctx1.exception))

        with self.assertRaises(Exception) as ctx2:
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_OUT", punch_time=t3, user=self.user)
        self.assertIn("completed", str(ctx2.exception))

    def test_chronological_validations(self):
        """Timestamps must follow chronological order."""
        work_date = date(2026, 10, 8)
        t_in = timezone.make_aware(datetime.combine(work_date, time(10, 0)))
        t_early = timezone.make_aware(datetime.combine(work_date, time(9, 0)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t_in, user=self.user)

        # Break In earlier than Punch In
        with self.assertRaises(Exception) as ctx:
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t_early, user=self.user)
        self.assertIn("cannot be earlier", str(ctx.exception))

    def test_rapid_duplicate_break_idempotent(self):
        """Accidental double clicks on Break In are idempotent within 5s."""
        work_date = date(2026, 10, 9)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 0)))
        t_b1 = timezone.make_aware(datetime.combine(work_date, time(12, 0)))
        t_b2 = t_b1 + timedelta(seconds=2)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)

        b1, _ = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t_b1, user=self.user)
        b2, _ = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t_b2, user=self.user)
        self.assertEqual(b1.id, b2.id)
        self.assertEqual(AttendanceBreak.objects.filter(employee=self.employee, work_date=work_date).count(), 1)

    def test_auto_punch_out_when_forgot_punch_out(self):
        """If employee forgets to punch out, auto punch-out closes the day at shift end."""
        work_date = date(2026, 10, 12)
        t_in = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t_in, user=self.user)

        # As of next day, day has ended:
        closed = services.auto_punch_out_pending_sessions(
            client=self.client_obj, employee=self.employee, as_of_date=date(2026, 10, 13)
        )
        self.assertEqual(len(closed), 1)
        out_punch = closed[0]
        self.assertEqual(out_punch.punch_type, "OUT")
        self.assertEqual(out_punch.source, "auto_day_end")
        self.assertIn("Auto punch-out at day end", out_punch.remark)
        # Shift ends at 18:30 by default policy
        self.assertEqual(timezone.localtime(out_punch.punch_time).time(), time(18, 30))

        # Check attendance record was synced
        att = Attendance.objects.get(employee=self.employee, work_date=work_date)
        self.assertEqual(att.status, "Present")
        self.assertEqual(att.working_hours, Decimal("9.0"))

    def test_auto_punch_out_closes_active_break(self):
        """Auto punch out closes any open break before punching out."""
        work_date = date(2026, 10, 13)
        t_in = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t_b_in = timezone.make_aware(datetime.combine(work_date, time(13, 0)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t_in, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t_b_in, user=self.user)

        closed = services.auto_punch_out_pending_sessions(
            client=self.client_obj, employee=self.employee, as_of_date=date(2026, 10, 14)
        )
        self.assertEqual(len(closed), 1)

        # Break should be closed
        brk = AttendanceBreak.objects.get(employee=self.employee, work_date=work_date)
        self.assertIsNotNone(brk.break_out_time)
        self.assertIn("Auto break-out at day end", brk.remark)

        # Punches should include BREAK_OUT and OUT
        types = list(AttendancePunch.objects.filter(employee=self.employee, work_date=work_date).values_list("punch_type", flat=True))
        self.assertEqual(types, ["IN", "BREAK_IN", "BREAK_OUT", "OUT"])

        att = Attendance.objects.get(employee=self.employee, work_date=work_date)
        # 9h session - 1h auto break = 8.0 working hours
        self.assertEqual(att.working_hours, Decimal("8.0"))

    def test_auto_punch_out_late_arrival_shortfall_payroll(self):
        """Late arrival + forgot punch out results in working hours shortfall deducted in payroll."""
        work_date = date(2026, 10, 14)
        # Punched in at 10:30 (1 hour late)
        t_in = timezone.make_aware(datetime.combine(work_date, time(10, 30)))
        # Took 1 hr lunch break 13:00 to 14:00
        t_b_in = timezone.make_aware(datetime.combine(work_date, time(13, 0)))
        t_b_out = timezone.make_aware(datetime.combine(work_date, time(14, 0)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t_in, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_IN", punch_time=t_b_in, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="BREAK_OUT", punch_time=t_b_out, user=self.user)

        # Forgot to punch out: auto punch out at 18:30
        closed = services.auto_punch_out_pending_sessions(
            client=self.client_obj, employee=self.employee, as_of_date=date(2026, 10, 15)
        )
        self.assertEqual(len(closed), 1)

        att = Attendance.objects.get(employee=self.employee, work_date=work_date)
        # Total duration = 10:30 to 18:30 (8.0h) - 1.0h break = 7.0h net work
        self.assertEqual(att.working_hours, Decimal("7.0"))

        # Process payroll for October 2026: shortfall of 1.0h is deducted
        payroll_result = services.process_payroll(client=self.client_obj, period_month=date(2026, 10, 1), user=self.user)
        p = Payslip.objects.filter(payroll_run=payroll_result["run"], employee=self.employee).first()
        self.assertIsNotNone(p)

        # Check shortfall deduction component exists
        shortfall_comp = p.components.filter(name__icontains="Shortfall").first()
        self.assertIsNotNone(shortfall_comp)
        self.assertGreater(shortfall_comp.amount, Decimal("0"))

    def test_auto_punch_out_management_command(self):
        """Management command auto_punch_out successfully runs and closes pending sessions."""
        from django.core.management import call_command
        work_date = date(2026, 10, 15)
        t_in = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t_in, user=self.user)

        call_command("auto_punch_out", date="2026-10-16")
        out_exists = AttendancePunch.objects.filter(employee=self.employee, work_date=work_date, punch_type="OUT").exists()
        self.assertTrue(out_exists)

    def test_auto_punch_out_api_endpoint(self):
        """API endpoint /api/v1/hrms/attendance/auto-punch-out/ triggers auto punch out."""
        from rest_framework.test import APIClient
        from apps.accounts.authentication import build_tokens
        work_date = date(2026, 10, 16)
        t_in = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t_in, user=self.user)

        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")
        res = api.post("/api/v1/hrms/attendance/auto-punch-out/", {"forceToday": True}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "success")
        self.assertGreaterEqual(res.data["count"], 1)
