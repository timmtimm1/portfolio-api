"""Schemas da projecao por Monte Carlo."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, field_serializer

from app.schemas.transaction import _dinheiro


class SimulationRequest(BaseModel):
    """Retorno e volatilidade vem do CLIENTE, como no rebalanceamento.

    A tela ja calculou a fronteira e mostrou tres carteiras. Mandar os numeros
    de volta deixa a pessoa projetar a carteira que ELA escolheu -- a atual, a
    de minima variancia ou a de maximo Sharpe. Recalcular no servidor
    projetaria uma carteira possivelmente diferente da que esta na tela.

    O valor INICIAL nao vem daqui: e lido da carteira no servidor. Ele nao e
    premissa, e fato.
    """

    retorno_esperado: Annotated[float, Field(gt=-1, le=10)]
    volatilidade: Annotated[float, Field(ge=0, le=10)]
    anos: Annotated[int, Field(ge=1, le=40)]
    aporte_mensal: Annotated[Decimal, Field(ge=0, le=Decimal("1e7"))] = Decimal(0)
    # Dez mil e suficiente: o erro do percentil cai com a raiz de n, e a partir
    # dai a mudanca fica na terceira casa -- invisivel no grafico e cara na CPU.
    cenarios: Annotated[int, Field(ge=100, le=50_000)] = 10_000


class ProjecaoPonto(BaseModel):
    mes: int
    p5: Decimal
    p25: Decimal
    p50: Decimal
    p75: Decimal
    p95: Decimal

    @field_serializer("p5", "p25", "p50", "p75", "p95")
    def _s(self, v: Decimal) -> Decimal:
        return _dinheiro(v)


class SimulationResponse(BaseModel):
    pontos: list[ProjecaoPonto]
    valor_inicial: Decimal
    total_aportado: Decimal = Field(description="Valor inicial mais todos os aportes")
    prob_acima_do_aportado: float = Field(
        description="Fracao de cenarios que terminam acima do que foi colocado"
    )
    cenarios: int
    # Dito na resposta, e nao so na tela: quem consumir a API direto tambem
    # precisa saber que o modelo subestima crise.
    ressalva: str = Field(
        default=(
            "Cenarios sorteados de uma distribuicao normal, que nao tem cauda gorda: "
            "crises reais sao mais frequentes e mais profundas do que este modelo "
            "preve. O percentil 5 aqui e otimista para o pior caso. O retorno "
            "esperado vem de estimativa historica, que e instavel. Isto nao e "
            "recomendacao de investimento."
        )
    )

    @field_serializer("valor_inicial", "total_aportado")
    def _s(self, v: Decimal) -> Decimal:
        return _dinheiro(v)
