"""Trade otimo: vender o suficiente para tirar o custo e ficar com o resto.

## A ideia

Vender parte da posicao ate recuperar o que se pagou (mais um lucro escolhido,
se houver), e ficar com o RESIDUO -- acoes da mesma empresa que, dali em
diante, nao custaram nada. O dinheiro original saiu, o papel ficou.

    vender x acoes a P  ->  x . P = custo_total + lucro_desejado

    x = (custo_total + lucro_desejado) / P
    residuo = quantidade - x

## Bruto, nao liquido

A conta ignora imposto e corretagem de proposito. Modelar IR direito exigiria
somar as vendas do mes (a isencao de R$ 20 mil vale por mes, nao por
operacao), saber se ha prejuizo acumulado a compensar e distinguir day trade
de swing trade -- tres coisas que este app nao rastreia. Um numero fiscal
"quase certo" e pior que nenhum: ele parece confiavel.

O nome disso na tela e "bruto", e o aviso fica junto.

## Arredondamento

`x` sobe para o inteiro seguinte (`ceil`). Vender a fracao exata nao existe na
B3, e arredondar para baixo deixaria o custo sem cobrir por alguns centavos --
que e justamente o que a operacao existe para nao acontecer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

ZERO = Decimal(0)
CENTAVOS = Decimal("0.01")


def _dinheiro(v: Decimal) -> Decimal:
    return v.quantize(CENTAVOS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PlanoDeTrade:
    """Quanto vender, quanto sobra, e se a conta fecha."""

    vender: int
    residuo: int
    recebe: Decimal
    """Bruto da venda, antes de imposto e corretagem."""
    custo_recuperado: Decimal
    sobra_em_caixa: Decimal
    """O que fica em dinheiro depois de repor o custo. Nunca negativo num
    plano viavel -- se fosse, o custo nao teria sido recuperado."""
    residuo_valor: Decimal
    viavel: bool
    """False quando seria preciso vender mais acoes do que se tem. Nao e erro:
    e a resposta honesta de que o preco ainda nao subiu o bastante."""


def planejar(
    *,
    quantidade: Decimal,
    preco_medio: Decimal,
    preco_atual: Decimal,
    lucro_desejado: Decimal = ZERO,
) -> PlanoDeTrade | None:
    """Quantas acoes vender para tirar o custo (mais o lucro pedido).

    `None` quando nao ha o que planejar: sem posicao, sem preco, ou preco
    zerado. Devolver um plano vazio obrigaria todo chamador a distinguir
    "plano de vender zero" de "nao da para planejar".
    """
    if quantidade <= ZERO or preco_atual <= ZERO:
        return None

    custo = quantidade * preco_medio
    alvo = custo + lucro_desejado

    exato = alvo / preco_atual
    vender = math.ceil(exato)
    viavel = vender <= quantidade

    # Num plano inviavel os numeros seguintes descrevem o cenario que NAO cabe.
    # Mostra-los mesmo assim e o que permite a tela dizer "faltariam 2 acoes"
    # em vez de um "nao da" sem tamanho.
    recebe = Decimal(vender) * preco_atual
    residuo = quantidade - vender

    return PlanoDeTrade(
        vender=vender,
        residuo=int(residuo) if viavel else 0,
        recebe=_dinheiro(recebe),
        custo_recuperado=_dinheiro(min(recebe, custo)),
        sobra_em_caixa=_dinheiro(max(ZERO, recebe - custo)),
        residuo_valor=_dinheiro(residuo * preco_atual) if viavel else ZERO,
        viavel=viavel,
    )


def preco_para_residuo(
    *,
    quantidade: Decimal,
    preco_medio: Decimal,
    residuo_desejado: int,
    lucro_desejado: Decimal = ZERO,
) -> Decimal | None:
    """O caminho inverso: a que preco sobrariam N acoes livres.

    Vendendo `quantidade - residuo` acoes, o preco precisa cobrir custo mais
    lucro:

        (quantidade - residuo) . P = custo + lucro
        P = (custo + lucro) / (quantidade - residuo)

    `None` quando o residuo pedido nao deixa nada para vender -- guardar TODAS
    as acoes e nao tirar o custo de lugar nenhum e um pedido sem solucao, nao
    um preco muito alto.

    O preco sobe para o centavo seguinte (`ROUND_CEILING`), nao para o mais
    proximo. Pelo mesmo motivo do `ceil` nas acoes: um preco que "quase" cobre
    o custo nao cobre. Arredondar 48,0728 para 48,07 devolvia um preco em que
    as 35 acoes rendem R$ 1.682,45 -- dez centavos a menos que o custo -- e
    `planejar()` naquele preco exigia 36 acoes, deixando 9 de residuo em vez
    das 10 pedidas. Os dois sentidos da conta tem que fechar.
    """
    if quantidade <= ZERO or residuo_desejado < 0:
        return None
    vendaveis = quantidade - residuo_desejado
    if vendaveis <= ZERO:
        return None

    custo = quantidade * preco_medio
    return ((custo + lucro_desejado) / vendaveis).quantize(CENTAVOS, rounding=ROUND_CEILING)
