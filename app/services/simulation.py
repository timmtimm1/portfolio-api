"""Projecao da carteira por simulacao de Monte Carlo.

## A pergunta que isto responde

Nao e "quanto vou ter em 2032?" -- ninguem sabe. E **"de todos os futuros
plausiveis, como eles se distribuem?"**.

Uma projecao de linha unica responde a primeira pergunta, e responde errado:
ela promete um numero que nao vai acontecer. Sorteando milhares de caminhos a
partir da volatilidade da propria carteira, sai uma faixa -- e a frase que
sobra e de outra natureza: "em 5% dos cenarios voce fica abaixo de X".

Isso mostra RISCO, e nao so esperanca. Numa ferramenta sobre dinheiro, e a
diferenca entre informar e vender otimismo.

## O modelo

Movimento browniano geometrico: o retorno mensal em LOG e sorteado de uma
normal, e o valor evolui multiplicando pelo exponencial.

    log_r ~ Normal(m, s)     s = vol_anual / sqrt(12)
                             m = ln(1 + retorno_anual)/12 - s^2/2
    valor[t+1] = valor[t] * exp(log_r) + aporte

Usar log em vez de retorno aritmetico nao e capricho: garante que o valor
nunca fica negativo. Uma carteira pode ir a zero, nao a menos de zero -- e uma
normal sobre retornos aritmeticos produz valores negativos com folga em
horizontes longos.

O `- s^2/2` e a correcao de Ito. Sem ela, a MEDIA dos cenarios ficaria acima
do retorno esperado que se pediu -- a simulacao renderia mais que a premissa,
sozinha.

## O que este modelo NAO captura

Normal nao tem cauda gorda. Crises reais sao mais frequentes e mais profundas
do que uma normal preve, entao o percentil 5 aqui e **otimista** demais para
o pior caso. E `retorno_anual` vem de estimativa historica, que e instavel.

Nada disso e conselho de investimento; e engenharia honesta sobre o que o
numero significa.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

MESES_POR_ANO = 12

# Percentis reportados. Nao e uma escolha estetica: p5 e p95 delimitam onde
# caem 90% dos cenarios, e p25/p75 fecham a metade central. Reportar so a
# mediana esconderia exatamente a informacao que motiva a simulacao.
PERCENTIS = (5, 25, 50, 75, 95)

# Teto de cenarios. Acima disso o ganho de precisao e imperceptivel e o custo
# de CPU (e de memoria: cenarios x meses de float64) deixa de ser.
CENARIOS_MAXIMO = 50_000


@dataclass(frozen=True)
class PontoProjecao:
    """A distribuicao dos cenarios num mes."""

    mes: int
    p5: Decimal
    p25: Decimal
    p50: Decimal
    p75: Decimal
    p95: Decimal


@dataclass(frozen=True)
class Projecao:
    pontos: list[PontoProjecao]
    total_aportado: Decimal
    # Fracao de cenarios que terminam acima do que foi COLOCADO. Nao e
    # "chance de lucro" no sentido contabil, e "chance de ter mais dinheiro do
    # que se tivesse guardado embaixo do colchao" -- que e a pergunta que uma
    # pessoa realmente faz.
    prob_acima_do_aportado: float
    cenarios: int


def _quantizar(valores: np.ndarray) -> list[Decimal]:
    """Do mundo estatistico (float) para o mundo do dinheiro (Decimal).

    A fronteira entre os dois e explicita neste projeto: `float` para
    estimativa com incerteza na terceira casa, `Decimal` para valor que alguem
    le como reais. A conversao acontece aqui, uma vez.
    """
    return [Decimal(f"{v:.2f}") for v in valores]


def projetar(
    valor_inicial: Decimal,
    *,
    retorno_anual: float,
    volatilidade_anual: float,
    anos: int,
    aporte_mensal: Decimal = Decimal(0),
    cenarios: int = 10_000,
    semente: int | None = None,
) -> Projecao:
    """Sorteia `cenarios` caminhos e devolve a distribuicao mes a mes.

    `semente` fixa o gerador: sem ela, dois cliques seguidos no mesmo botao
    devolveriam numeros diferentes e o usuario nao saberia se algo mudou ou se
    e so o sorteio. Com ela, o resultado e reproduzivel -- e testavel.
    """
    if anos <= 0:
        raise ValueError("o horizonte precisa ser de pelo menos um ano")
    if cenarios <= 0 or cenarios > CENARIOS_MAXIMO:
        raise ValueError(f"cenarios precisa estar entre 1 e {CENARIOS_MAXIMO}")
    if volatilidade_anual < 0:
        raise ValueError("volatilidade negativa nao existe")
    if retorno_anual <= -1:
        raise ValueError("retorno anual de -100% ou pior zera a carteira por definicao")

    meses = anos * MESES_POR_ANO
    s = volatilidade_anual / math.sqrt(MESES_POR_ANO)
    # Correcao de Ito: sem o -s^2/2, a media dos cenarios superaria o retorno
    # esperado pedido, e a simulacao renderia mais que a premissa.
    m = math.log1p(retorno_anual) / MESES_POR_ANO - (s**2) / 2

    gerador = np.random.default_rng(semente)
    # Uma matriz de choques de uma vez, em vez de sortear dentro do laco: com
    # 10 mil cenarios e 72 meses sao 720 mil numeros, e o numpy faz isso numa
    # chamada. Sortear um a um em Python levaria segundos.
    choques = np.exp(gerador.normal(m, s, size=(cenarios, meses)))

    inicial = float(valor_inicial)
    aporte = float(aporte_mensal)

    valores = np.full(cenarios, inicial, dtype=np.float64)
    pontos = [PontoProjecao(0, *_quantizar(np.full(len(PERCENTIS), inicial)))]

    # O laco percorre MESES (dezenas), nao cenarios (milhares): cada iteracao
    # move os 10 mil caminhos de uma vez.
    for t in range(meses):
        valores = valores * choques[:, t] + aporte
        faixas = np.percentile(valores, PERCENTIS)
        pontos.append(PontoProjecao(t + 1, *_quantizar(faixas)))

    total_aportado = valor_inicial + aporte_mensal * meses
    acima = float(np.mean(valores > float(total_aportado)))

    return Projecao(
        pontos=pontos,
        total_aportado=total_aportado,
        prob_acima_do_aportado=acima,
        cenarios=cenarios,
    )
