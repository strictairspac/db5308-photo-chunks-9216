from fastapi import APIRouter, Depends, Query

from src.schemas.workflow import BillingRecordForm, ReverseForm
from src.services import billing
from src.services.security import current_user, operator

router = APIRouter()


@router.get('/billing/records')
def list_records(month: str | None = Query(default=None), _=Depends(current_user)):
    return billing.list_records(month)


@router.post('/billing/records', status_code=201)
def create_record(form: BillingRecordForm, actor=Depends(operator)):
    return billing.add_record(form, actor)


@router.post('/billing/records/{identity}/reverse')
def reverse_record(identity: int, form: ReverseForm, actor=Depends(operator)):
    return billing.reverse_record(identity, form.note, actor)


@router.get('/billing/periods')
def list_periods(_=Depends(current_user)):
    return billing.list_periods()


@router.post('/billing/periods/{month}/close', status_code=201)
def close_period(month: str, actor=Depends(operator)):
    return billing.close_month(month, actor)


@router.post('/billing/periods/{month}/settle', status_code=201)
def settle_period(month: str, actor=Depends(operator)):
    return billing.settle_month(month, actor)


@router.get('/billing/settlements')
def list_settlements(month: str | None = Query(default=None), _=Depends(current_user)):
    return billing.list_settlements(month)


@router.get('/billing/settlements/{identity}')
def get_settlement(identity: int, _=Depends(current_user)):
    return billing.settlement_detail(identity)


@router.get('/billing/bills')
def list_bills(month: str | None = Query(default=None),
               production_group: str | None = Query(default=None), _=Depends(current_user)):
    return billing.list_bills(month, production_group)


@router.get('/billing/bills/{identity}')
def get_bill(identity: int, _=Depends(current_user)):
    return billing.bill_detail(identity)


@router.get('/billing/summary')
def get_summary(month: str = Query(...), _=Depends(current_user)):
    return billing.month_summary(month)


@router.get('/billing/overview')
def get_overview(_=Depends(current_user)):
    return billing.overview()
