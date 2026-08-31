"""Schemas do resumo da carteira."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, field_serializer

from app.schemas.transaction import _dinheiro, _enxuto


class PositionSummary(BaseModel):
    """Posicao com valor de mercado.

    Todo campo derivado de cotacao e opcional: quando nenhum fornecedor responde,
    a carteira continua sendo devolvida com custo e quantidade -- so os campos de
    mercado vem nulos. Uma carteira que aparece zerada porque a brapi caiu seria
    um defeito muito pior que campos ausentes.
    """

    ticker: str
    quantidade: Decimal
    preco_medio: Decimal
    custo_total: Decimal
    resultado_realizado: Decimal

    preco_atual: Decimal | None = None
    valor_mercado: Decimal | None = None
    resultado_nao_realizado: Decimal | None = None
    variacao_percentual: Decimal | None = None
    cotacao_em: datetime | None = None
    cotacao_fonte: str | None = None

    @field_serializer("quantidade", "preco_medio", "preco_atual")
    def _s_quantidade(self, v: Decimal | None) -> Decimal | None:
        return _enxuto(v) if v is not None else None

    @field_serializer(
        "custo_total", "resultado_realizado", "valor_mercado", "resultado_nao_realizado"
    )
    def _s_dinheiro(self, v: Decimal | None) -> Decimal | None:
        return _dinheiro(v) if v is not None else None

    @field_serializer("variacao_percentual")
    def _s_percentual(self, v: Decimal | None) -> Decimal | None:
        return _dinheiro(v) if v is not None else None


class PortfolioTotals(BaseModel):
    custo_total: Decimal
    valor_mercado: Decimal
    resultado_nao_realizado: Decimal
    resultado_realizado: Decimal
    variacao_percentual: Decimal | None = None

    @field_serializer(
        "custo_total",
        "valor_mercado",
        "resultado_nao_realizado",
        "resultado_realizado",
        "variacao_percentual",
    )
    def _s_dinheiro(self, v: Decimal | None) -> Decimal | None:
        return _dinheiro(v) if v is not None else None


class PortfolioSummary(BaseModel):
    positions: list[PositionSummary]
    totals: PortfolioTotals
    # Transparencia deliberada: o cliente precisa saber que estes papeis entraram
    # nos totais apenas pelo custo, sem preco de mercado. Esconder isso faria a
    # rentabilidade parecer pior do que e, sem explicacao.
    sem_cotacao: list[str]
