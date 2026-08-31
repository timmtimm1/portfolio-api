"""Healthcheck -- usado pelo Docker, pelo Render e pelo cron que evita cold start."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.config import Settings, get_settings

router = APIRouter(tags=["infra"])


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str


@router.get("/health", response_model=HealthResponse)
async def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Deliberadamente nao expoe hostname, versao de biblioteca nem string de
    conexao. Healthcheck e endpoint publico -- tudo que ele devolve e informacao
    de graca para quem esta mapeando o alvo."""
    return HealthResponse(status="ok", environment=settings.ENVIRONMENT, version="0.1.0")
