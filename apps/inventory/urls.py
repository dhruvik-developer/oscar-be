"""
Inventory routes (api.md §4.2, §4.3, §7).

The item, category, unit and location masters live in ``apps.masters`` but are
routed under ``/inventory/`` because that is where the frontend calls them
(api.md §4.2 -- ``GET /inventory/items/``).
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.masters.views import (
    AgencyViewSet,
    BrokerViewSet,
    DefectMasterViewSet,
    DesignViewSet,
    FabricQualityViewSet,
    FabricTypeViewSet,
    FabricVariantViewSet,
    FabricViewSet,
    ItemCategoryViewSet,
    ItemViewSet,
    JobWorkRateMasterViewSet,
    LocationViewSet,
    LossReasonMasterViewSet,
    ProcessMasterViewSet,
    ReturnReasonMasterViewSet,
    ShadeViewSet,
    UnitViewSet,
)

from . import views, warehouse_views

router = DefaultRouter(trailing_slash=True)
router.register("items", ItemViewSet, basename="inventory-items")
router.register("categories", ItemCategoryViewSet, basename="inventory-categories")
router.register("units", UnitViewSet, basename="inventory-units")
router.register("locations", LocationViewSet, basename="inventory-locations")
router.register("fabrics", FabricViewSet, basename="inventory-fabrics")
router.register("fabric-qualities", FabricQualityViewSet, basename="inventory-fabric-qualities")
router.register("fabric-types", FabricTypeViewSet, basename="inventory-fabric-types")
router.register("designs", DesignViewSet, basename="inventory-designs")
router.register("shades", ShadeViewSet, basename="inventory-shades")
router.register("fabric-variants", FabricVariantViewSet, basename="inventory-fabric-variants")
router.register("agencies", AgencyViewSet, basename="inventory-agencies")
router.register("brokers", BrokerViewSet, basename="inventory-brokers")
router.register("processes", ProcessMasterViewSet, basename="inventory-processes")
router.register("jobwork-rates", JobWorkRateMasterViewSet, basename="inventory-jobwork-rates")
router.register("defects", DefectMasterViewSet, basename="inventory-defects")
router.register("loss-reasons", LossReasonMasterViewSet, basename="inventory-loss-reasons")
router.register("return-reasons", ReturnReasonMasterViewSet, basename="inventory-return-reasons")
router.register("movements", views.StockMovementViewSet, basename="inventory-movements")
router.register("transfers", views.StockTransferViewSet, basename="inventory-transfers")
router.register("faulty-parts", views.FaultyPartViewSet, basename="inventory-faulty-parts")
router.register("service-usage", views.ServiceUsageViewSet, basename="inventory-service-usage")
router.register("zone-requests", views.ZoneRequestViewSet, basename="inventory-zone-requests")
router.register("item-parts", views.ItemPartViewSet, basename="inventory-item-parts")
router.register("audits", views.StockAuditViewSet, basename="inventory-audits")
router.register("qc-inspections", views.QCInspectionViewSet, basename="inventory-qc-inspections")
router.register("lot-cost-entries", views.LotCostEntryViewSet, basename="inventory-lot-cost-entries")
router.register("samples", views.SampleViewSet, basename="inventory-samples")
router.register("trips", views.TripViewSet, basename="inventory-trips")

urlpatterns = [
    path("dashboard/", views.InventoryDashboardView.as_view(), name="inventory-dashboard"),
    path("traceability/", views.LotTraceabilityView.as_view(), name="inventory-traceability"),
    path("batches/", views.BatchListView.as_view(), name="inventory-batches"),
    path("warehouses/", warehouse_views.WarehouseListView.as_view(), name="inventory-warehouses"),
    path("warehouses/dashboard/", warehouse_views.WarehouseDashboardView.as_view(), name="inventory-warehouse-dashboard"),
    path("warehouses/stock/", warehouse_views.WarehouseStockView.as_view(), name="inventory-warehouse-stock"),
    path("warehouses/availability/", warehouse_views.WarehouseAvailabilityView.as_view(), name="inventory-warehouse-availability"),
    path("warehouses/shortage-transfer/", warehouse_views.WarehouseShortageTransferView.as_view(), name="inventory-warehouse-shortage-transfer"),
    path("warehouses/stock-levels/", warehouse_views.WarehouseStockLevelView.as_view(), name="inventory-warehouse-stock-levels"),
    path("warehouses/ledger/", warehouse_views.WarehouseLedgerView.as_view(), name="inventory-warehouse-ledger"),
    path("warehouses/<uuid:pk>/", warehouse_views.WarehouseDetailView.as_view(), name="inventory-warehouse-detail"),
    path("production-runs/", views.ProductionRunListView.as_view(), name="inventory-production-runs"),
    path("production-runs/<uuid:pk>/", views.ProductionRunDetailView.as_view(), name="inventory-production-run"),
    path("production-runs/<uuid:pk>/<str:action_name>/", views.ProductionRunDetailView.as_view(),
         name="inventory-production-run-action"),
    path("qc/purchase-return/", views.QCPurchaseReturnView.as_view(), name="inventory-qc-purchase-return"),
    path("stock-position/<str:screen>/", views.StockPositionScreenView.as_view(), name="inventory-stock-position"),
    path("stock-position/<str:screen>/<str:action_name>/", views.StockPositionScreenView.as_view(),
         name="inventory-stock-position-action"),
    path("batches/eligible/", views.BatchEligibleStockView.as_view(), name="inventory-batch-eligible"),
    path("batches/customer-receipt/", views.CustomerMaterialView.as_view(), name="inventory-customer-receipt"),
    path("batches/physical-stock/", views.CustomerMaterialView.as_view(), name="inventory-physical-stock"),
    path("batches/<str:code>/deliver/", views.CustomerMaterialView.as_view(), name="inventory-batch-deliver"),
    path("batches/<str:code>/register-takas/", views.BatchTakaRegistrationView.as_view(),
         name="inventory-batch-register-takas"),
    path("batches/allocate-charge/", views.BatchChargeView.as_view(), name="inventory-batch-allocate-charge"),
    path("batches/<str:code>/losses/", views.BatchLossView.as_view(), name="inventory-batch-losses"),
    path("batch-losses/<uuid:loss_id>/", views.BatchLossView.as_view(), name="inventory-batch-loss"),
    path("batches/<str:code>/", views.BatchDetailView.as_view(), name="inventory-batch-detail"),
    path("lots/<str:lot_id>/traceability/", views.LotTraceabilityView.as_view(), name="inventory-lot-traceability"),
    path("stock/ageing/", views.StockAgeingView.as_view(), name="inventory-stock-ageing"),
    path("stock/slow-moving/", views.SlowMovingStockView.as_view(), name="inventory-slow-moving"),
    path("qc/dashboard/", views.QCDashboardView.as_view(), name="inventory-qc-dashboard"),
    path("qc/pending/", views.QCPendingInspectionsView.as_view(), name="inventory-qc-pending"),
    path("qc/pending/bulk-assign/", views.QCBulkAssignView.as_view(), name="inventory-qc-bulk-assign"),
    path("qc/complete/", views.QCCompleteInspectionView.as_view(), name="inventory-qc-complete"),
    path("qc/register/", views.QCRegisterView.as_view(), name="inventory-qc-register"),
    path("qc/register/manual/", views.QCRegisterView.as_view(), name="inventory-qc-register-manual"),
    path("qc/disposition/", views.QCDispositionView.as_view(), name="inventory-qc-disposition"),
    path("qc/reinspection/", views.QCReinspectionView.as_view(), name="inventory-qc-reinspection"),
    path("qc/reports/", views.QCReportsView.as_view(), name="inventory-qc-reports"),
    path("valuation/", views.ValuationView.as_view(), name="inventory-valuation"),
    path("stock/summary/", views.StockSummaryView.as_view(), name="inventory-stock-summary"),
    path("stock/", views.StockPositionView.as_view(), name="inventory-stock"),
    path("combined-grey-stock/", views.CombinedGreyStockView.as_view(), name="inventory-combined-grey-stock"),
    path("combined-grey-stock/setting/", views.CombinedGreyStockSettingView.as_view(), name="inventory-combined-grey-stock-setting"),
    path("adjustments/", views.StockAdjustmentView.as_view(), name="inventory-adjustments"),
    path("lot-register/", views.LotTakaRegisterView.as_view(), name="inventory-lot-register"),
    path("lot-register/merge/", views.LotMergeView.as_view(), name="inventory-lot-merge"),
    path("lot-register/<uuid:pk>/", views.LotTakaRegisterView.as_view(), name="inventory-lot-register-detail"),
    path("taka-tags/", views.TakaTagPrintView.as_view(), name="inventory-taka-tags"),
    path("taka-tags/print/", views.TakaTagPrintView.as_view(), name="inventory-taka-tags-print"),
    path("taka-tags/scan/", views.TakaTagScanView.as_view(), name="inventory-taka-tags-scan"),
    path("taka-tags/pdf/", views.TakaTagPdfView.as_view(), name="inventory-taka-tags-pdf"),
    path("taka-tags/history/", views.TakaTagHistoryView.as_view(), name="inventory-taka-tags-history-query"),
    path("taka-tags/<str:pk>/history/", views.TakaTagHistoryView.as_view(), name="inventory-taka-tags-history"),
    path("", include(router.urls)),
]
