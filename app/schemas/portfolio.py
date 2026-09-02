"""Schemas do resumo da carteira."""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_serializer

from app.schemas.target import AlvoResumo
from app.schemas.transaction import _dinheiro, _enxuto


class EventoAplicado(BaseModel):
    """Um evento corporativo que mexeu nesta posição.

    Existe para a tela poder explicar a diferença entre o extrato ("comprei
    100") e a posição ("tenho 200"). Sem a explicação, o usuário vê os dois
    números e conclui, com razão, que um dos dois está errado.
    """

    data_ex: date_type
    proporcao: str = Field(description='Como a empresa anunciou: "2:1", "1:10", "103:100"')
    fator: Decimal = Field(description="Por quanto a quantidade foi multiplicada")

    @field_serializer("fator")
    def _s_fator(self, v: Decimal) -> Decimal:
        return _enxuto(v)


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

    # Vazio na esmagadora maioria dos casos -- so tem conteudo quando houve
    # desdobramento, grupamento ou bonificacao DEPOIS da primeira compra.
    eventos: list[EventoAplicado] = Field(default_factory=list)

    # Sempre presente (nunca null), mesmo sem alvo configurado -- `status`
    # comeca em SEM_ALVO e os demais campos em None. Poupa a tela de checar
    # nulidade do objeto inteiro antes de olhar o status.
    alvo: AlvoResumo = Field(default_factory=AlvoResumo)

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
