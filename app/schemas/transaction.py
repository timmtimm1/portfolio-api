"""Schemas de transacao e posicao."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.models.transaction import TransactionSide

# Tetos deliberados. Nao existe pessoa fisica comprando 10^12 acoes; um numero
# assim e erro de digitacao ou tentativa de estourar a aritmetica decimal. Barrar
# na borda e mais barato que descobrir depois por que o custo total ficou absurdo.
Quantidade = Annotated[Decimal, Field(gt=0, le=Decimal("1e9"), decimal_places=8)]
Preco = Annotated[Decimal, Field(gt=0, le=Decimal("1e9"), decimal_places=6)]
Taxas = Annotated[Decimal, Field(ge=0, le=Decimal("1e7"), decimal_places=6)]
Ticker = Annotated[str, Field(min_length=4, max_length=12, pattern=r"^[A-Za-z0-9]{4,6}$")]
Nota = Annotated[str | None, Field(max_length=200)]


def _recusa_data_futura(v: date_type) -> date_type:
    """Data futura nao e erro de digitacao inofensivo: ela entraria no fim do
    livro e distorceria o preco medio de tudo que viesse depois.

    Funcao de modulo, nao metodo: corrigir uma operacao precisa da mesma recusa
    que lanca-la. Com a regra escrita duas vezes, um dia so uma das duas seria
    corrigida -- e a brecha estaria justamente na edicao, que e o caminho por
    onde se conserta erro de digitacao.
    """
    from datetime import UTC, datetime

    if v > datetime.now(UTC).date():
        raise ValueError("a data da operacao nao pode estar no futuro")
    return v


class TransactionCreate(BaseModel):
    ticker: Ticker
    side: TransactionSide
    quantity: Quantidade
    price: Preco
    fees: Taxas = Decimal(0)
    traded_at: date_type
    note: Nota = None

    @field_validator("ticker")
    @classmethod
    def _normaliza(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("traded_at")
    @classmethod
    def _nao_pode_ser_no_futuro(cls, v: date_type) -> date_type:
        return _recusa_data_futura(v)


class TransactionUpdate(BaseModel):
    """Correcao de uma operacao ja lancada. Campo ausente nao muda nada.

    Parcial de proposito. Corrigir o preco de uma compra nao deveria obrigar o
    cliente a reenviar quantidade, data e taxas -- e um reenvio ao qual faltasse
    um campo sobrescreveria dado bom com o padrao do schema (`fees` viraria 0,
    `note` viraria nulo). Num livro contabil isso e corrupcao silenciosa.

    `note` e o campo que exige cuidado: `null` ENVIADO limpa a observacao,
    ausente a preserva. Quem separa os dois casos e `model_fields_set` (via
    `exclude_unset`), nunca uma comparacao com None -- com ela, apagar uma nota
    seria impossivel.

    `extra="forbid"` porque num PATCH um nome de campo errado e indistinguivel
    de "nao quis mudar": o cliente manda `preco` em vez de `price`, recebe 200 e
    a correcao simplesmente nao aconteceu. Melhor recusar na borda.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: Ticker | None = None
    side: TransactionSide | None = None
    quantity: Quantidade | None = None
    price: Preco | None = None
    fees: Taxas | None = None
    traded_at: date_type | None = None
    note: Nota = None

    @field_validator("ticker")
    @classmethod
    def _normaliza(cls, v: str | None) -> str | None:
        return v.strip().upper() if v is not None else None

    @field_validator("traded_at")
    @classmethod
    def _nao_pode_ser_no_futuro(cls, v: date_type | None) -> date_type | None:
        return _recusa_data_futura(v) if v is not None else None


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticker: str
    side: TransactionSide
    quantity: Decimal
    price: Decimal
    fees: Decimal
    traded_at: date_type
    note: str | None

    @field_serializer("quantity", "price", "fees")
    def _serializa(self, v: Decimal) -> Decimal:
        return _enxuto(v)

    # `user_id` NAO esta aqui, de proposito. O usuario ja sabe quem e; devolver o
    # id dele em cada linha so aumenta a superficie exposta sem servir a nada.


CENTAVOS = Decimal("0.01")


def _dinheiro(valor: Decimal) -> Decimal:
    """Arredonda para centavos, meio para cima.

    O arredondamento acontece SO na saida. Arredondar durante o calculo -- a cada
    compra, a cada venda -- faria o erro se acumular ao longo do livro; e por isso
    que o preco medio interno guarda 8 casas.

    ROUND_HALF_UP, nao o padrao do Python (ROUND_HALF_EVEN, "arredondamento
    bancario"): o padrao transformaria 0,005 em 0,00, o que contraria a convencao
    financeira brasileira e o que qualquer usuario espera ver.
    """
    return valor.quantize(CENTAVOS, rounding=ROUND_HALF_UP)


def _enxuto(valor: Decimal) -> Decimal:
    """Remove zeros a direita sem virar notacao cientifica.

    `Decimal("100.00").normalize()` devolve `1E+2`, que serializa como "1E+2" no
    JSON e quebra qualquer cliente. O ramo do inteiro evita isso.
    """
    if valor == valor.to_integral_value():
        return valor.quantize(Decimal(1))
    return valor.normalize()


class PositionRead(BaseModel):
    """Posicao consolidada num ativo.

    Valores derivados do livro a cada consulta -- nao ha coluna de posicao no
    banco. Ver app/services/position.py.
    """

    ticker: str
    nome: str | None = None
    quantidade: Decimal
    preco_medio: Decimal
    custo_total: Decimal
    resultado_realizado: Decimal

    @field_serializer("quantidade", "preco_medio")
    def _serializa_quantidade(self, v: Decimal) -> Decimal:
        return _enxuto(v)

    @field_serializer("custo_total", "resultado_realizado")
    def _serializa_dinheiro(self, v: Decimal) -> Decimal:
        return _dinheiro(v)


class TransactionsClearedResult(BaseModel):
    """Resultado de zerar o livro de uma carteira simulada de uma vez."""

    removidas: int
