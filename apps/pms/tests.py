from decimal import Decimal
from django.test import TestCase
from apps.accounts.models import Client, User
from apps.pms.models import Project, ProjectStage, Department
from apps.pms import services


class StagePercentageCalculationTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="test-tenant", defaults={"name": "Test Tenant"}
        )
        self.creator = User.objects.filter(email="creator@example.com").first()
        if not self.creator:
            self.creator = User.objects.create_user(
                email="creator@example.com",
                password="pass",
                client=self.client_obj,
            )
        self.dept, _ = Department.objects.get_or_create(
            client=self.client_obj,
            name="Engineering",
        )

    def test_serializer_percentage_validation(self):
        from apps.pms.serializers import StagePercentagesSerializer
        from rest_framework.exceptions import ValidationError

        # Invalid: 80% total
        invalid_data = {
            "stages": [
                {"id": "1", "percentage": 30},
                {"id": "2", "percentage": 50},
            ]
        }
        s1 = StagePercentagesSerializer(data=invalid_data)
        self.assertFalse(s1.is_valid())
        self.assertIn("stages", s1.errors)

        # Valid: 100% total
        valid_data = {
            "stages": [
                {"id": "1", "percentage": 30},
                {"id": "2", "percentage": 70},
            ]
        }
        s2 = StagePercentagesSerializer(data=valid_data)
        self.assertTrue(s2.is_valid())

    def test_weighted_progress_calculation(self):
        import uuid
        project = Project.objects.create(
            client=self.client_obj,
            code=f"PRJ-2026-WEIGHTED-{uuid.uuid4().hex[:6]}",
            created_by=self.creator,
            status="In Progress",
        )
        specs = [
            ("Design", 15, 100),
            ("Fabrication", 30, 50),
            ("QC", 15, 0),
            ("Coating", 15, 0),
            ("Packaging", 10, 0),
            ("Dispatch", 15, 0),
        ]
        for idx, (name, weight, completion) in enumerate(specs, start=1):
            ProjectStage.objects.create(
                client=self.client_obj,
                project=project,
                name=name,
                sequence=idx,
                department=self.dept,
                weight_pct=Decimal(str(weight)),
                completion_pct=completion,
            )

        services.recalculate_project(project)
        project.refresh_from_db()
        self.assertEqual(project.overall_completion_pct, 30)

    def test_unweighted_fallback_for_legacy_projects(self):
        import uuid
        project = Project.objects.create(
            client=self.client_obj,
            code=f"PRJ-2026-LEGACY-{uuid.uuid4().hex[:6]}",
            created_by=self.creator,
            status="In Progress",
        )
        specs = [
            ("Stage 1", 0, 100),
            ("Stage 2", 0, 50),
            ("Stage 3", 0, 0),
        ]
        for idx, (name, weight, completion) in enumerate(specs, start=1):
            ProjectStage.objects.create(
                client=self.client_obj,
                project=project,
                name=name,
                sequence=idx,
                department=self.dept,
                weight_pct=Decimal(str(weight)),
                completion_pct=completion,
            )

        services.recalculate_project(project)
        project.refresh_from_db()
        # (100 + 50 + 0) / 3 = 50%
        self.assertEqual(project.overall_completion_pct, 50)

    def test_stage_percentages_rebalance(self):
        import uuid
        # Create project with 2 stages: 50% / 50%
        project = Project.objects.create(
            client=self.client_obj,
            code=f"PRJ-2026-REBALANCE-{uuid.uuid4().hex[:6]}",
            created_by=self.creator,
            status="In Progress",
        )
        s1 = ProjectStage.objects.create(
            client=self.client_obj,
            project=project,
            name="Stage 1",
            sequence=1,
            department=self.dept,
            weight_pct=Decimal("50"),
            completion_pct=100,
        )
        s2 = ProjectStage.objects.create(
            client=self.client_obj,
            project=project,
            name="Stage 2",
            sequence=2,
            department=self.dept,
            weight_pct=Decimal("50"),
            completion_pct=0,
        )
        services.recalculate_project(project)
        project.refresh_from_db()
        # 100 * 0.5 = 50%
        self.assertEqual(project.overall_completion_pct, 50)

        # Rebalance: Stage 1 -> 80%, Stage 2 -> 20%
        s1.weight_pct = Decimal("80")
        s1.save()
        s2.weight_pct = Decimal("20")
        s2.save()
        services.recalculate_project(project)
        project.refresh_from_db()
        # 100 * 0.8 = 80%
        self.assertEqual(project.overall_completion_pct, 80)
