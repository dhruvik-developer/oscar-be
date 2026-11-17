"""
OSCAR ERP Reprocess Views.

API endpoints for sending QC-rejected fabric for reprocess, tracking outstanding
quantities, and receiving vendor returns.
"""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import HasModulePermission
from . import reprocess_services


class ReprocessListView(APIView):
    """
    GET  /api/v1/inventory/reprocess/        -- list reprocess records
    POST /api/v1/inventory/reprocess/send/   -- send batch for reprocess
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]
    permission_map = {"POST": ["adjust_stock", "approve_qc"]}

    def get(self, request):
        records = reprocess_services.list_reprocess_records(request.client_id, request.query_params)
        return Response({"results": records, "count": len(records)})

    def post(self, request):
        data = request.data or {}
        batch_code = data.get("batchCode") or data.get("batch_code")
        sent_qty = data.get("sentQty") or data.get("sent_quantity") or data.get("quantity")
        outward_challan = data.get("outwardChallan") or data.get("outward_challan")
        vendor_id = data.get("vendorId") or data.get("vendor_id")
        po_id = data.get("poId") or data.get("po_id")
        reason = data.get("reason")
        expected_return_date = data.get("expectedReturnDate") or data.get("expected_return_date")

        record = reprocess_services.send_batch_for_reprocess(
            request.client_id,
            batch_code,
            sent_qty=sent_qty,
            outward_challan=outward_challan,
            vendor_id=vendor_id,
            po_id=po_id,
            reason=reason,
            expected_return_date=expected_return_date,
            user=request.user,
        )
        return Response(record, status=status.HTTP_201_CREATED)


class ReprocessDetailView(APIView):
    """
    GET /api/v1/inventory/reprocess/<id>/   -- retrieve reprocess record detail
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]

    def get(self, request, pk):
        record = reprocess_services.get_reprocess_record_detail(request.client_id, pk)
        if not record:
            return Response({"detail": f"Reprocess record '{pk}' not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(record)


class ReprocessReceiveReturnView(APIView):
    """
    POST /api/v1/inventory/reprocess/<id>/receive-return/  -- record vendor return receipt
    """
    permission_classes = [HasModulePermission]
    required_permissions = ["view_inventory"]
    permission_map = {"POST": ["adjust_stock", "enter_goods_receipt"]}

    def post(self, request, pk):
        data = request.data or {}
        return_qty = data.get("returnQty") or data.get("return_quantity") or data.get("quantity")
        return_date = data.get("returnDate") or data.get("return_date")
        vendor_challan = (
            data.get("vendorReturnChallan")
            or data.get("vendor_return_challan")
            or data.get("vendorChallan")
            or data.get("challan")
        )
        reprocess_charges = data.get("reprocessCharges") or data.get("reprocess_charges") or 0
        remarks = data.get("remarks")
        idempotency_key = (
            data.get("idempotencyKey")
            or data.get("idempotency_key")
            or request.headers.get("X-Idempotency-Key")
        )

        record = reprocess_services.receive_reprocess_return(
            request.client_id,
            pk,
            return_qty=return_qty,
            return_date=return_date,
            vendor_return_challan=vendor_challan,
            reprocess_charges=reprocess_charges,
            remarks=remarks,
            idempotency_key=idempotency_key,
            user=request.user,
        )
        return Response(record, status=status.HTTP_200_OK)
