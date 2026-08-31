"""Healthcheck -- usado pelo Docker, pelo Render e pelo cron que evita cold start."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db

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


@router.get("/health/ready", response_model=HealthResponse)
async def readiness(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HealthResponse:
    """Readiness: `/health` diz que o processo esta vivo; este diz que ele
    consegue atender -- ou seja, que o banco responde.

    A distincao importa no deploy: um orquestrador que so olha liveness mantem no
    balanceador uma instancia que subiu mas perdeu o banco, e o usuario come 500.

    Em caso de falha devolve 503 **sem detalhe algum**. A mensagem de erro do
    driver traz host, porta e nome do banco -- informacao de graca para quem
    estiver sondando o alvo. O detalhe vai para o log, nao para a resposta.
    """
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="degraded", environment=settings.ENVIRONMENT, version="0.1.0")
    return HealthResponse(status="ok", environment=settings.ENVIRONMENT, version="0.1.0")
