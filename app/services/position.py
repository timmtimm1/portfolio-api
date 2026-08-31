"""Calculo de posicao a partir do livro de transacoes.

Modulo puro: sem banco, sem HTTP, sem ORM. Recebe transacoes, devolve posicoes.
E deliberado -- e o codigo que decide quanto dinheiro o usuario tem, entao
precisa ser testavel contra numeros conferidos a mao, sem subir infraestrutura.

## A regra brasileira: custo medio ponderado

O Brasil usa **preco medio ponderado**, nao FIFO nem LIFO. A consequencia que
mais confunde:

    **Venda NAO altera o preco medio.**

Ela reduz a quantidade e reduz o custo total proporcionalmente, ao preco medio
vigente -- entao a divisao custo/quantidade continua dando o mesmo numero. Quem
implementa FIFO por habito de mercado estrangeiro produz um preco medio errado, e
com ele um imposto errado.

Exemplo conferido a mao:

    compra 100 a R$ 20,00  -> qtd 100, custo 2000, medio 20,00
    compra 100 a R$ 30,00  -> qtd 200, custo 5000, medio 25,00
    vende  100 a R$ 40,00  -> qtd 100, custo 2500, medio 25,00  (medio intacto)
                              resultado realizado = (40 - 25) x 100 = 1500

## Taxas

Corretagem e emolumentos na COMPRA entram no custo (aumentam o preco medio); na
VENDA saem do resultado. E o tratamento correto pela Receita: o custo de
aquisicao inclui as despesas necessarias a aquisicao.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from app.models.transaction import TransactionSide
from app.services.exceptions import DomainError

ZERO = Decimal(0)

# Casas decimais internas do preco medio. Oito e folgado para que o erro de
# arredondamento nao se propague ao longo de centenas de transacoes; a
# apresentacao arredonda para 2 na hora de mostrar, nao antes.
CASAS_PRECO_MEDIO = Decimal("0.00000001")


class VendaSemPosicaoError(DomainError):
    """Venda de mais unidades do que a posicao tinha naquela data."""

    def __init__(self, ticker: str, quando: date, tinha: Decimal, tentou: Decimal) -> None:
        super().__init__(
            f"{ticker}: tentativa de vender {tentou} em {quando}, mas a posicao era {tinha}"
        )
        self.ticker = ticker
        self.quando = quando
        self.tinha = tinha
        self.tentou = tentou


class TransacaoLike(Protocol):
    """O minimo que o calculo precisa saber de uma transacao.

    Um Protocol em vez do model do ORM: assim o teste passa um objeto trivial e
    o codigo de producao passa a linha do banco, sem que este modulo dependa do
    SQLAlchemy.
    """

    @property
    def ticker(self) -> str: ...
    @property
    def side(self) -> TransactionSide: ...
    @property
    def quantity(self) -> Decimal: ...
    @property
    def price(self) -> Decimal: ...
    @property
    def fees(self) -> Decimal: ...
    @property
    def traded_at(self) -> date: ...


@dataclass(frozen=True)
class Posicao:
    """Posicao consolidada num ativo.

    `frozen=True`: o resultado de um calculo nao deve ser alteravel depois de
    pronto. Se algo precisa mudar, recalcula-se a partir do livro -- que e a
    unica fonte da verdade.
    """

    ticker: str
    quantidade: Decimal
    preco_medio: Decimal
    custo_total: Decimal
    resultado_realizado: Decimal
    quantidade_comprada: Decimal
    quantidade_vendida: Decimal

    @property
    def esta_zerada(self) -> bool:
        return self.quantidade == ZERO


@dataclass
class _Acumulador:
    quantidade: Decimal = ZERO
    custo_total: Decimal = ZERO
    realizado: Decimal = ZERO
    comprada: Decimal = ZERO
    vendida: Decimal = ZERO

    @property
    def preco_medio(self) -> Decimal:
        if self.quantidade == ZERO:
            return ZERO
        return (self.custo_total / self.quantidade).quantize(CASAS_PRECO_MEDIO)


def calcular_posicoes(transacoes: Sequence[TransacaoLike]) -> dict[str, Posicao]:
    """Reconstroi as posicoes percorrendo o livro em ordem cronologica.

    Recebe `Sequence`, nao `list`: `list` e invariante em Python, entao uma
    `list[Transaction]` nao seria aceita onde se pede `list[TransacaoLike]`,
    mesmo com Transaction satisfazendo o Protocol. `Sequence` e covariante e
    aceita qualquer sequencia de subtipos -- que e o comportamento desejado
    para um parametro so de leitura.

    A ordenacao e feita aqui, nao presumida do chamador: uma transacao lancada
    com data retroativa precisa ser processada no lugar cronologico dela, senao
    o preco medio sai errado. O desempate por ticker mantem o resultado
    deterministico quando ha varias operacoes no mesmo dia.

    Percorrer o livro inteiro a cada consulta e uma escolha: para as centenas de
    transacoes de um investidor pessoa fisica e instantaneo, e mantem uma unica
    fonte da verdade. Guardar a posicao numa coluna seria mais rapido e abriria
    a porta para o pior tipo de bug -- o saldo que diverge do extrato e ninguem
    sabe qual dos dois esta certo. Se um dia o volume exigir, a resposta e cache
    ou snapshot (Etapa 10), nunca duplicar a verdade.
    """
    acumuladores: dict[str, _Acumulador] = {}

    for t in sorted(transacoes, key=lambda x: (x.traded_at, x.ticker)):
        acc = acumuladores.setdefault(t.ticker, _Acumulador())

        if t.side is TransactionSide.COMPRA:
            # Taxas de compra entram no custo de aquisicao.
            acc.custo_total += t.quantity * t.price + t.fees
            acc.quantidade += t.quantity
            acc.comprada += t.quantity
            continue

        if t.quantity > acc.quantidade:
            raise VendaSemPosicaoError(t.ticker, t.traded_at, acc.quantidade, t.quantity)

        medio = acc.preco_medio
        custo_baixado = t.quantity * medio
        # Taxas de venda reduzem o resultado.
        acc.realizado += t.quantity * t.price - t.fees - custo_baixado
        acc.custo_total -= custo_baixado
        acc.quantidade -= t.quantity
        acc.vendida += t.quantity

        # Zera residuo de arredondamento: sem isto, vender toda a posicao pode
        # deixar um custo de 0.00000001 e um preco medio absurdo na proxima
        # compra.
        if acc.quantidade == ZERO:
            acc.custo_total = ZERO

    return {
        ticker: Posicao(
            ticker=ticker,
            quantidade=acc.quantidade,
            preco_medio=acc.preco_medio,
            custo_total=acc.custo_total,
            resultado_realizado=acc.realizado,
            quantidade_comprada=acc.comprada,
            quantidade_vendida=acc.vendida,
        )
        for ticker, acc in acumuladores.items()
    }
