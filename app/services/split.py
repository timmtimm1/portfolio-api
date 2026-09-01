"""Ajuste do livro de transacoes por desdobramento, grupamento e bonificacao.

## A ideia central

Em vez de ensinar `calcular_posicoes()`, `curva_equivalente()` e todo o resto
a lidar com eventos corporativos, o livro e NORMALIZADO para os termos de hoje
antes de qualquer conta. Uma compra de 100 acoes a R$ 40, seguida de um
desdobramento 2:1, passa a ser lida como 200 acoes a R$ 20 -- que e
exatamente o que a posicao vale hoje.

Assim nenhuma funcao adiante precisa saber que houve evento: preco medio,
custo, resultado realizado e proventos continuam usando a mesma matematica.

## Por que o custo nao muda

Desdobramento nao e lucro. O investidor tinha R$ 4.000 em 100 acoes e passou a
ter R$ 4.000 em 200 -- mesmo bolo, mais fatias. Por isso quantidade e preco se
movem em sentidos opostos e o produto fica constante. Qualquer implementacao
em que o custo mude esta errada, e e facil errar: multiplicar a quantidade e
esquecer o preco produz uma carteira que dobrou de valor sozinha.

## Compatibilidade com os proventos

O Yahoo ja ajusta o historico de proventos por desdobramento. Como o livro
tambem passa a estar em termos de hoje, os dois lados falam a mesma unidade:
quantidade ajustada x provento ajustado da o valor correto. Misturar um lado
ajustado com outro nao ajustado erraria pelo fator do evento.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from typing import Protocol

from app.models.transaction import TransactionSide
from app.services.position import TransacaoLike

UM = Decimal(1)


class EventoLike(Protocol):
    """O minimo que o ajuste precisa saber de um evento corporativo."""

    @property
    def ticker(self) -> str: ...
    @property
    def data_ex(self) -> date_type: ...
    @property
    def fator(self) -> Decimal: ...


@dataclass(frozen=True)
class TransacaoAjustada:
    """Uma transacao relida nos termos de hoje.

    Implementa `TransacaoLike`, entao entra em `calcular_posicoes()` e em
    `dividend.quantidade_em()` sem que nenhuma das duas saiba que houve ajuste.
    `fator_aplicado` existe para a interface poder explicar a diferenca entre o
    que a pessoa digitou e o que ela ve.
    """

    ticker: str
    side: TransactionSide
    quantity: Decimal
    price: Decimal
    fees: Decimal
    traded_at: date_type
    fator_aplicado: Decimal = UM

    @property
    def foi_ajustada(self) -> bool:
        return self.fator_aplicado != UM


def fator_acumulado(eventos: Sequence[EventoLike], ticker: str, depois_de: date_type) -> Decimal:
    """Produto dos fatores de todos os eventos POSTERIORES a `depois_de`.

    Eventos compoem: uma acao comprada antes de um 2:1 e de um 10:1 vale 20
    vezes mais cotas hoje. Somar os fatores em vez de multiplicar daria 12, e o
    erro so apareceria em papeis com mais de um evento -- o caso raro que
    ninguem testa a mao.

    A comparacao e ESTRITA (`>`): quem comprou no proprio dia-ex ja comprou na
    quantidade nova, entao nao ha o que ajustar.
    """
    fator = UM
    for evento in eventos:
        if evento.ticker == ticker and evento.data_ex > depois_de:
            fator *= evento.fator
    return fator


def ajustar(
    transacoes: Sequence[TransacaoLike], eventos: Sequence[EventoLike]
) -> list[TransacaoAjustada]:
    """Reescreve o livro nos termos de hoje. Nao toca no banco.

    Sem eventos, devolve as mesmas transacoes com fator 1 -- o caminho comum
    e o mais barato, e continua passando por aqui de proposito: um caminho
    alternativo "sem ajuste" seria um segundo comportamento para manter.
    """
    ajustadas: list[TransacaoAjustada] = []
    for t in transacoes:
        fator = fator_acumulado(eventos, t.ticker, t.traded_at)
        ajustadas.append(
            TransacaoAjustada(
                ticker=t.ticker,
                side=t.side,
                quantity=t.quantity * fator,
                # O preco anda ao contrario da quantidade: o produto (o custo)
                # tem que ficar identico ao que foi pago de verdade.
                price=t.price / fator,
                # Taxas NAO sao ajustadas: a corretagem foi paga em reais, uma
                # vez, e nao se multiplica porque a acao se dividiu.
                fees=t.fees,
                traded_at=t.traded_at,
                fator_aplicado=fator,
            )
        )
    return ajustadas
