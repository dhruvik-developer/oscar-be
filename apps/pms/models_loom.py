"""
Loom & Weaving Manufacturing Models for Oscar PMS.
Recreates the prototype loom production, beam lifecycle, quality specs,
3-step yarn booking/delivery, and fabric dispatch tracking.
"""
from django.db import models
from django.utils import timezone
from apps.core.models import TenantModel


class LoomCoordinator(TenantModel):
    name = models.TextField()
    phone = models.TextField(null=True, blank=True)
    email = models.TextField(null=True, blank=True)
    user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="loom_coordinators"
    )

    class Meta:
        db_table = "loom_coordinators"
        ordering = ["name"]

    def __str__(self):
        return self.name


class LoomFactory(TenantModel):
    name = models.TextField()
    code = models.TextField(null=True, blank=True)
    coord = models.ForeignKey(
        LoomCoordinator, null=True, blank=True, on_delete=models.SET_NULL, related_name="factories"
    )
    address = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "loom_factories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class LoomMachine(TenantModel):
    factory = models.ForeignKey(LoomFactory, on_delete=models.CASCADE, related_name="machines")
    no = models.IntegerField()
    reed = models.IntegerField(default=84)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "loom_machines"
        ordering = ["factory", "no"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "factory", "no"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_loom_machine_no_per_factory",
            )
        ]

    def __str__(self):
        return f"{self.factory.name} - MCN {self.no} (Reed {self.reed})"


class YarnMaster(TenantModel):
    name = models.TextField()
    denier = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    kg_per_bag = models.DecimalField(max_digits=10, decimal_places=2, default=25)
    rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    note = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "loom_yarns"
        ordering = ["name"]

    def __str__(self):
        return self.name


class LoomQuality(TenantModel):
    name = models.TextField()
    warp_yarn = models.ForeignKey(YarnMaster, on_delete=models.PROTECT, related_name="warp_qualities")
    ends = models.IntegerField()
    crimp = models.DecimalField(max_digits=5, decimal_places=2, default=5.0)
    weft_yarn = models.ForeignKey(YarnMaster, on_delete=models.PROTECT, related_name="weft_qualities")
    reed = models.IntegerField()
    reed_w = models.DecimalField(max_digits=6, decimal_places=2, default=58.0)
    ppi = models.DecimalField(max_digits=6, decimal_places=2)
    wastage = models.DecimalField(max_digits=5, decimal_places=2, default=8.0)
    pick_charge = models.DecimalField(max_digits=6, decimal_places=4, default=0.15)

    class Meta:
        db_table = "loom_qualities"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def ppm(self):
        return float(self.ppi) * 39.37

    @property
    def warp_kg_m(self):
        return float(self.ends) * float(self.warp_yarn.denier) * (1.0 + float(self.crimp) / 100.0) / 9000000.0

    @property
    def weft_kg_m(self):
        return float(self.weft_yarn.denier) * float(self.reed_w) * float(self.ppi) * (1.0 + float(self.wastage) / 100.0) / 9000000.0

    @property
    def glm(self):
        return (float(self.ends) * float(self.warp_yarn.denier) + float(self.weft_yarn.denier) * float(self.reed_w) * float(self.ppi)) / 9000000.0 * 1000.0

    @property
    def gsm(self):
        rw = float(self.reed_w) * 0.0254
        return (self.glm / rw) if rw > 0 else 0.0

    @property
    def warp_cost(self):
        return self.warp_kg_m * float(self.warp_yarn.rate)

    @property
    def weft_cost(self):
        return self.weft_kg_m * float(self.weft_yarn.rate)

    @property
    def job_cost(self):
        return float(self.ppi) * float(self.pick_charge)

    @property
    def total_cost(self):
        return self.warp_cost + self.weft_cost + self.job_cost


class LoomBeam(TenantModel):
    STATUS_CHOICES = [
        ("Spare", "Spare"),
        ("On loom", "On loom"),
        ("Finished", "Finished"),
    ]
    beam_no = models.TextField()
    factory = models.ForeignKey(LoomFactory, on_delete=models.CASCADE, related_name="beams")
    quality = models.ForeignKey(LoomQuality, on_delete=models.PROTECT, related_name="beams")
    length = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField(default=timezone.now)
    status = models.TextField(choices=STATUS_CHOICES, default="Spare")
    machine = models.ForeignKey(LoomMachine, null=True, blank=True, on_delete=models.SET_NULL, related_name="beams")

    class Meta:
        db_table = "loom_beams"
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.beam_no} ({self.quality.name}) - {self.status}"


class DailyLoomProduction(TenantModel):
    REASON_CHOICES = [
        ("", "Normal"),
        ("Yarn short", "Yarn short"),
        ("Breakdown", "Breakdown"),
        ("Beam change", "Beam change"),
        ("Power", "Power"),
        ("Labour", "Labour"),
        ("Other", "Other"),
    ]
    factory = models.ForeignKey(LoomFactory, on_delete=models.CASCADE, related_name="daily_productions")
    machine = models.ForeignKey(LoomMachine, on_delete=models.CASCADE, related_name="daily_productions")
    beam = models.ForeignKey(LoomBeam, on_delete=models.CASCADE, related_name="daily_productions")
    date = models.DateField()
    meters = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    rpm = models.DecimalField(max_digits=8, decimal_places=2, default=650)
    hours = models.DecimalField(max_digits=6, decimal_places=2, default=22)
    stop = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    reason = models.TextField(choices=REASON_CHOICES, null=True, blank=True, default="")
    damage = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        db_table = "loom_daily_production"
        ordering = ["-date", "machine__no"]


class FactoryDayLog(TenantModel):
    factory = models.ForeignKey(LoomFactory, on_delete=models.CASCADE, related_name="day_logs")
    date = models.DateField()
    power = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    class Meta:
        db_table = "loom_factory_day_logs"
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "factory", "date"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_loom_factory_day_log",
            )
        ]


class YarnBooking(TenantModel):
    supplier = models.TextField()
    yarn = models.ForeignKey(YarnMaster, on_delete=models.PROTECT, related_name="bookings")
    kgs = models.DecimalField(max_digits=12, decimal_places=2)
    rate = models.DecimalField(max_digits=10, decimal_places=2)
    date = models.DateField(default=timezone.now)
    note = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "loom_yarn_bookings"
        ordering = ["-date"]

    def __str__(self):
        return f"{self.supplier} - {self.yarn.name} ({self.kgs} kg)"


class YarnDelivery(TenantModel):
    STATUS_CHOICES = [
        ("Scheduled", "Scheduled"),
        ("Received", "Received"),
    ]
    booking = models.ForeignKey(YarnBooking, on_delete=models.CASCADE, related_name="deliveries")
    factory = models.ForeignKey(LoomFactory, on_delete=models.CASCADE, related_name="yarn_deliveries")
    kgs = models.DecimalField(max_digits=12, decimal_places=2)
    rate = models.DecimalField(max_digits=10, decimal_places=2)
    due = models.DateField()
    recd_kgs = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    recd_date = models.DateField(null=True, blank=True)
    status = models.TextField(choices=STATUS_CHOICES, default="Scheduled")

    class Meta:
        db_table = "loom_yarn_deliveries"
        ordering = ["due", "-created_at"]


class ProductionProgram(TenantModel):
    STATUS_CHOICES = [
        ("Planned", "Planned"),
        ("Running", "Running"),
        ("Hold", "Hold"),
        ("Closed", "Closed"),
    ]
    name = models.TextField()
    factory = models.ForeignKey(LoomFactory, on_delete=models.CASCADE, related_name="programs")
    quality = models.ForeignKey(LoomQuality, on_delete=models.PROTECT, related_name="programs")
    target = models.DecimalField(max_digits=14, decimal_places=2)
    start = models.DateField()
    deadline = models.DateField()
    t_rpm = models.DecimalField(max_digits=8, decimal_places=2, default=650)
    t_eff = models.DecimalField(max_digits=6, decimal_places=2, default=85)
    t_power = models.DecimalField(max_digits=6, decimal_places=2, default=2)
    status = models.TextField(choices=STATUS_CHOICES, default="Planned")

    class Meta:
        db_table = "loom_programs"
        ordering = ["-start"]

    def __str__(self):
        return self.name


class FabricDispatch(TenantModel):
    DEST_CHOICES = [
        ("Godown", "Godown"),
        ("Dyeing mill", "Dyeing mill"),
        ("Other factory", "Other factory"),
    ]
    factory = models.ForeignKey(LoomFactory, on_delete=models.CASCADE, related_name="fabric_dispatches")
    quality = models.ForeignKey(LoomQuality, on_delete=models.PROTECT, related_name="fabric_dispatches")
    date = models.DateField(default=timezone.now)
    taka = models.IntegerField(default=0)
    meters = models.DecimalField(max_digits=12, decimal_places=2)
    dest = models.TextField(choices=DEST_CHOICES, default="Godown")
    dest_name = models.TextField(null=True, blank=True)
    challan = models.TextField(null=True, blank=True)
    vehicle = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "loom_fabric_dispatches"
        ordering = ["-date", "-created_at"]
