"""Pydantic DTOs for the api-keys endpoints. Response keys are camelCase to match
the FE types (frontend/src/types/index.ts) and the SP1 profile endpoint."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    label: str = ""
    api_key: str = Field(alias="apiKey")
    api_secret: str = Field(alias="apiSecret")


class ApiKeyResponse(BaseModel):
    id: str
    label: str
    api_key: str = Field(serialization_alias="apiKey")
    api_secret: str = Field(default="****", serialization_alias="apiSecret")
    exchange_status: str = Field(serialization_alias="exchangeStatus")
    created_at: str = Field(serialization_alias="createdAt")
    verified_at: str | None = Field(default=None, serialization_alias="verifiedAt")
    last_verify_error: str | None = Field(default=None, serialization_alias="lastVerifyError")


class VerifyResultResponse(BaseModel):
    status: str
    error: str | None = None
