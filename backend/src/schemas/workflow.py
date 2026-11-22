from typing import Any

from pydantic import BaseModel, Field


class LoginForm(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class CaseForm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class ReviewForm(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class BlockForm(BaseModel):
    address: str
    bytes: int


class BillingRecordForm(BaseModel):
    month: str
    production_group: str
    record_key: str
    blocks: list[BlockForm] | None = None
    run_id: int | None = None
    note: str | None = None


class ReverseForm(BaseModel):
    note: str
