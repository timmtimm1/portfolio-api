"""Schemas de proventos."""

from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.models.dividend import TipoProvento
from app.schemas.transaction import _dinheiro, _enxuto


class DividendRead(BaseModel):
    """Um provento recebido por ESTA carteira.

    Nao e a linha da tabela `dividends` -- aquela e o evento de mercado, igual
    para todo mundo. Isto e o cruzamento dela com o livro: quantas cotas voce
    tinha na data-com e quanto isso virou.
    """

    ticker: str
    data_com: date_type = Field(description="Quem tinha o ativo neste fechamento recebe")
    tipo: TipoProvento
    quantidade: Decimal
    valor_por_cota: Decimal
    valor_bruto: Decimal
    valor_liquido: Decimal = Field(description="Ja descontada a retencao na fonte (JCP: 15%)")

    @field_serializer("valor_bruto", "valor_liquido")
    def _serializa_dinheiro(self, v: Decimal) -> Decimal:
        return _dinheiro(v)

    @field_serializer("quantidade", "valor_por_cota")
    def _serializa_enxuto(self, v: Decimal) -> Decimal:
        return _enxuto(v)


class DividendSummary(BaseModel):
    """Total recebido no periodo, com o detalhamento."""

    total_liquido: Decimal
    total_bruto: Decimal
    imposto_retido: Decimal

    # Fracao (0.0415 = 4,15%), nao percentual: converter e decisao de
    # apresentacao, e fazer isso na API obrigaria todo cliente a saber qual
    # convencao foi usada. Mesma regra dos schemas de metricas.
    yield_on_cost: Decimal | None = Field(
        default=None, description="Proventos sobre o custo da posicao, em fracao"
    )

    # Quantos proventos ainda estao sem classificacao. Existe para a interface
    # poder avisar: enquanto for INDEFINIDO, o liquido pode estar ate 15% acima
    # do real, caso tenha sido JCP.
    sem_classificacao: int = 0

    proventos: list[DividendRead]

    @field_serializer("total_liquido", "total_bruto", "imposto_retido")
    def _serializa_dinheiro(self, v: Decimal) -> Decimal:
        return _dinheiro(v)

    @field_serializer("yield_on_cost")
    def _serializa_yield(self, v: Decimal | None) -> Decimal | None:
        """Sem isto a divisao de Decimal vaza a precisao interna: uma carteira
        sem proventos devolvia `0E+14` e uma com proventos devolvia 28 casas
        decimais. Nenhum dos dois esta errado -- os dois estao ilegiveis, e
        precisao falsa e uma forma de mentira."""
        return None if v is None else v.quantize(Decimal("0.000001"))


class DividendSyncResult(BaseModel):
    """Resultado da sincronização com o fornecedor."""

    tickers_consultados: list[str]
    gravados: int = Field(description="Proventos novos; repetir a sincronizacao devolve 0")


class DividendReclassify(BaseModel):
    """Correção manual do tipo de um provento importado.

    O Yahoo não distingue dividendo de JCP, e a diferença vale 15% de retenção
    na fonte. Isto é a porta para o usuário corrigir o que o fornecedor não sabe.
    """

    ticker: Annotated[str, Field(min_length=4, max_length=12, pattern=r"^[A-Za-z0-9]{4,6}$")]
    data_com: date_type
    tipo: TipoProvento

    @field_validator("ticker")
    @classmethod
    def _normaliza(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("tipo")
    @classmethod
    def _recusa_indefinido(cls, v: TipoProvento) -> TipoProvento:
        """Reclassificar PARA indefinido não é correção, é desfazer -- e não há
        caso de uso para isso. Barrar aqui evita uma linha órfã que colidiria
        com a chave primária na próxima sincronização."""
        if v is TipoProvento.INDEFINIDO:
            raise ValueError("escolha dividendo, jcp ou rendimento")
        return v
