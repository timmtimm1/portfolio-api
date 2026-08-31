"""Schemas dos snapshots."""

from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_serializer

from app.schemas.transaction import _dinheiro


class SnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: date_type
    custo_total: Decimal
    valor_mercado: Decimal
    resultado_nao_realizado: Decimal
    resultado_realizado: Decimal
    ativos: int
    ativos_sem_cotacao: int

    @field_serializer(
        "custo_total", "valor_mercado", "resultado_nao_realizado", "resultado_realizado"
    )
    def _s(self, v: Decimal) -> Decimal:
        return _dinheiro(v)


class SnapshotRunResult(BaseModel):
    """Relatorio do job. Devolvido ao cron para que a falha apareca no log do
    GitHub Actions em vez de sumir em silencio."""

    date: date_type
    usuarios_processados: int
    snapshots_gravados: int
    tickers_consultados: int
