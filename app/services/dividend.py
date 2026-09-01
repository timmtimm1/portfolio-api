"""Quanto a carteira recebeu de proventos, derivado do livro de transacoes.

## A regra que este modulo existe para respeitar

Provento nao se paga a quem tem o ativo hoje -- paga-se a quem tinha o ativo no
fechamento da DATA-COM. Comprar no dia seguinte nao da direito a nada.

Isso nao e detalhe burocratico: e a diferenca entre um numero certo e um numero
inventado. Na carteira real deste projeto, a TAEE11 foi comprada em 20/08/2026 e
teve provento com data-com em 17/08 -- tres dias antes. Somar proventos por
ticker, sem olhar a data, creditaria R$ 27,00 que nunca entraram na conta.

## Modulo puro

Entram transacoes e proventos, sai o que foi recebido. Sem banco, sem HTTP, sem
ORM -- mesma escolha de `position.py` e `optimizer.py`. O teste passa objetos
triviais; a producao passa linhas do banco.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from app.models.dividend import RETENCAO_NA_FONTE, TipoProvento
from app.models.transaction import TransactionSide
from app.services.position import TransacaoLike

ZERO = Decimal(0)
CENTAVOS = Decimal("0.01")


class ProventoLike(Protocol):
    """O minimo que o calculo precisa saber de um provento.

    `ticker`, e nao `asset_id`: o casamento com o livro de transacoes e por
    ticker, e um Protocol com `asset_id` obrigaria todo teste a inventar UUIDs
    para nada.
    """

    @property
    def ticker(self) -> str: ...
    @property
    def data_com(self) -> date_type: ...
    @property
    def tipo(self) -> TipoProvento: ...
    @property
    def valor_por_cota(self) -> Decimal: ...


@dataclass(frozen=True)
class ProventoRecebido:
    """Um provento ja cruzado com a posicao de quem o recebeu."""

    ticker: str
    data_com: date_type
    tipo: TipoProvento
    quantidade: Decimal
    valor_por_cota: Decimal
    valor_bruto: Decimal
    valor_liquido: Decimal

    @property
    def imposto_retido(self) -> Decimal:
        return self.valor_bruto - self.valor_liquido


def quantidade_em(transacoes: Sequence[TransacaoLike], ticker: str, dia: date_type) -> Decimal:
    """Quantas cotas a carteira tinha no FECHAMENTO de `dia`.

    Compra no proprio dia conta (`<=`), porque quem compra na data-com ainda
    aparece na posicao do fechamento. Venda no proprio dia tambem conta, pelo
    mesmo motivo, e ai a pessoa nao recebe -- foi exatamente esse o efeito de
    vender "na data-com".

    Nao reaproveita `calcular_posicoes()` de proposito: aquilo calcula preco
    medio e resultado realizado, que aqui nao servem para nada. Provento depende
    so de QUANTAS cotas existiam, nunca de quanto elas custaram.
    """
    quantidade = ZERO
    for t in transacoes:
        if t.ticker != ticker or t.traded_at > dia:
            continue
        quantidade += t.quantity if t.side is TransactionSide.COMPRA else -t.quantity
    return quantidade


def recebidos(
    transacoes: Sequence[TransacaoLike], proventos: Sequence[ProventoLike]
) -> list[ProventoRecebido]:
    """Cruza o livro com os proventos anunciados. Ordem cronologica na saida.

    Proventos de ativos que a carteira nao tinha na data-com sao descartados
    silenciosamente -- nao sao erro, sao simplesmente eventos de mercado que nao
    dizem respeito a esta carteira.
    """
    recebidos_: list[ProventoRecebido] = []

    for provento in sorted(proventos, key=lambda p: (p.data_com, p.ticker)):
        quantidade = quantidade_em(transacoes, provento.ticker, provento.data_com)
        if quantidade <= ZERO:
            continue

        bruto = (quantidade * provento.valor_por_cota).quantize(CENTAVOS, rounding=ROUND_HALF_UP)
        retencao = RETENCAO_NA_FONTE[provento.tipo]
        liquido = (bruto * (1 - retencao)).quantize(CENTAVOS, rounding=ROUND_HALF_UP)

        recebidos_.append(
            ProventoRecebido(
                ticker=provento.ticker,
                data_com=provento.data_com,
                tipo=provento.tipo,
                quantidade=quantidade,
                valor_por_cota=provento.valor_por_cota,
                valor_bruto=bruto,
                valor_liquido=liquido,
            )
        )

    return recebidos_


def total_liquido(recebidos_: Sequence[ProventoRecebido]) -> Decimal:
    """Soma do que efetivamente caiu na conta.

    Liquido, nao bruto: JCP tem 15% retidos na fonte, entao o valor anunciado
    nao e o valor recebido. Somar o bruto superestimaria o retorno da carteira
    -- e o objetivo desta fase inteira foi corrigir um retorno subestimado, nao
    trocar por um superestimado.
    """
    return sum((r.valor_liquido for r in recebidos_), ZERO)


def yield_on_cost(recebidos_: Sequence[ProventoRecebido], custo_total: Decimal) -> Decimal | None:
    """Proventos recebidos sobre o custo da posicao, em fracao.

    Devolve `None` com custo zero em vez de levantar: uma carteira vazia nao
    tem yield indefinido, tem yield que nao faz sentido perguntar.
    """
    if custo_total <= ZERO:
        return None
    return total_liquido(recebidos_) / custo_total
