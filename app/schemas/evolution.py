"""Schemas da evolucao patrimonial comparada."""

from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal

from pydantic import BaseModel, Field, field_serializer

from app.models.benchmark import Indexador
from app.schemas.snapshot import SnapshotRead
from app.schemas.transaction import _dinheiro


class BenchmarkPoint(BaseModel):
    date: date_type
    valor: Decimal

    @field_serializer("valor")
    def _s(self, v: Decimal) -> Decimal:
        return _dinheiro(v)


class BenchmarkSerie(BaseModel):
    indexador: Indexador
    nome: str
    pontos: list[BenchmarkPoint]
    valor_final: Decimal
    # Rentabilidade do indexador NO PERIODO, ja considerando os aportes -- nao a
    # taxa acumulada pura. Ver app/services/benchmark_service.py.
    variacao_percentual: Decimal | None

    @field_serializer("valor_final", "variacao_percentual")
    def _s(self, v: Decimal | None) -> Decimal | None:
        return _dinheiro(v) if v is not None else None


class RentabilidadePoint(BaseModel):
    """Rentabilidade acumulada em FRACAO (0.0415 = +4,15%).

    Fracao e nao percentual, pela mesma convencao das metricas: converter para
    percentual e decisao de apresentacao, e faze-lo na API obrigaria todo cliente
    a saber qual convencao foi usada.
    """

    date: date_type
    carteira: Decimal
    benchmark: Decimal | None


class Comparacao(BaseModel):
    """O numero que o investidor brasileiro de fato quer ver."""

    carteira_percentual: Decimal | None
    benchmark_percentual: Decimal | None
    # Positivo = a carteira bateu o indexador. Negativo = ficou atras -- e nesse
    # caso o dinheiro teria rendido mais no Tesouro Selic, sem risco de bolsa.
    excesso_pontos_percentuais: Decimal | None = Field(
        description="Diferenca em pontos percentuais entre a carteira e o indexador"
    )
    # Quanto a carteira rendeu em relacao ao CDI, a convencao usada por fundos:
    # 120 significa "120% do CDI".
    percentual_do_indexador: Decimal | None

    @field_serializer(
        "carteira_percentual",
        "benchmark_percentual",
        "excesso_pontos_percentuais",
        "percentual_do_indexador",
    )
    def _s(self, v: Decimal | None) -> Decimal | None:
        return _dinheiro(v) if v is not None else None


class EvolutionResponse(BaseModel):
    pontos: list[SnapshotRead]
    # Serie percentual (TWR da carteira x indexador acumulado). E o que o
    # grafico usa por padrao: em reais, uma carteira que cresceu esmaga a
    # escala e o CDI vira uma linha reta, sem informacao.
    rentabilidade: list[RentabilidadePoint]
    benchmark: BenchmarkSerie | None
    comparacao: Comparacao | None
    # Explica ausencia de benchmark, em vez de devolver nulo sem motivo.
    motivo: str | None = None
