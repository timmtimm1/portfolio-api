"""Otimizacao de carteira pelo modelo de Markowitz (media-variancia).

Modulo puro: entra vetor de retornos esperados e matriz de covariancia, sai um
vetor de pesos. Sem banco, sem HTTP, sem ORM.

## O que o modelo faz

Markowitz (1952) formalizou uma intuicao: o risco de uma carteira NAO e a media
dos riscos dos ativos. Depende de como eles se movem juntos. Dois ativos de
volatilidade 30% cada, com correlacao negativa, formam uma carteira de
volatilidade bem MENOR que 30% -- quando um cai, o outro tende a subir.

E por isso que a covariancia (Etapa 8) e a entrada central aqui, e por que
alinhar as series importava tanto: uma correlacao errada vira peso errado, e
peso errado vira dinheiro real alocado no lugar errado.

O problema resolvido, para um retorno-alvo mu*:

    minimizar    w' Sigma w          (variancia da carteira)
    sujeito a    w' mu  = mu*        (atinge o retorno desejado)
                 soma(w) = 1         (investe todo o capital)
                 0 <= w_i <= limite  (sem venda a descoberto, sem concentrar)

Varrendo mu* entre o minimo e o maximo possiveis, obtem-se a **fronteira
eficiente**: para cada nivel de retorno, a menor variancia alcancavel.

## Limitacoes que este codigo NAO esconde

O modelo assume que o passado estima o futuro. Nao assume. Retorno esperado
calculado sobre historico e notoriamente instavel -- pequenas mudancas na
amostra produzem carteiras completamente diferentes. Por isso:

  - a carteira de minima variancia e mais confiavel que a de maximo Sharpe: ela
    NAO usa retorno esperado, so covariancia (que e bem mais estavel);
  - existe um limite maximo por ativo, para o otimizador nao devolver 97% num
    unico papel so porque ele subiu muito na janela observada.

Nada disso e conselho de investimento; e engenharia honesta sobre o que o
numero significa.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.optimize import minimize

# Apelidos com a FORMA do array, nao so o tipo. `np.ndarray` cru aceita
# qualquer dimensao: uma matriz passada onde se espera um vetor compilaria e
# quebraria em producao. Aqui o mypy barra antes.
Vetor = np.ndarray[tuple[int], np.dtype[np.float64]]
Matriz = np.ndarray[tuple[int, int], np.dtype[np.float64]]

# Limite maximo por ativo. Sem ele, o otimizador rotineiramente concentra quase
# tudo no papel que mais subiu na amostra -- matematicamente otimo para o passado,
# e exatamente o oposto de diversificar.
PESO_MAXIMO_PADRAO = 0.40

# Peso abaixo disso e ruido numerico do solver, nao alocacao. Zerar evita
# devolver "0,0000001% em ABEV3" numa carteira sugerida.
PESO_MINIMO_RELEVANTE = 1e-4

# Numero de pontos da fronteira. 50 desenha uma curva suave sem custo relevante.
PONTOS_FRONTEIRA = 50


class OtimizacaoInviavelError(Exception):
    """Nao existe carteira que satisfaca todas as restricoes.

    Acontece, por exemplo, com 2 ativos e limite de 40% por ativo: o maximo
    alocavel seria 80%, e a restricao de somar 100% torna o problema impossivel.
    """


@dataclass(frozen=True)
class Carteira:
    pesos: Vetor
    retorno_esperado: float
    volatilidade: float
    indice_sharpe: float | None


def _volatilidade(pesos: Vetor, cov: Matriz) -> float:
    """sqrt(w' Sigma w).

    O `max(0, ...)` nao e paranoia: por erro de ponto flutuante, uma variancia
    que deveria ser exatamente zero pode sair como -1e-18, e a raiz de um numero
    negativo devolve NaN que se propaga silenciosamente por todo o resto.
    """
    variancia = float(pesos @ cov @ pesos)
    return float(np.sqrt(max(variancia, 0.0)))


def _restricoes(
    n: int, peso_maximo: float
) -> tuple[list[dict[str, Any]], list[tuple[float, float]]]:
    """Restricoes comuns a todos os problemas.

    `soma(w) = 1` -- investe exatamente o capital, nao mais (alavancagem) nem
    menos (caixa parado).

    `0 <= w <= peso_maximo` -- o limite inferior zero proibe venda a descoberto.
    Nao e preferencia estetica: pessoa fisica na B3 nao vende a descoberto sem
    conta margem, e uma carteira sugerida com peso negativo seria inexecutavel
    para o usuario deste sistema.
    """
    if n * peso_maximo < 1.0 - 1e-9:
        raise OtimizacaoInviavelError(
            f"{n} ativos com limite de {peso_maximo:.0%} cada somam no maximo "
            f"{n * peso_maximo:.0%} -- impossivel investir 100%"
        )
    restricoes: list[dict[str, Any]] = [{"type": "eq", "fun": lambda w: float(np.sum(w) - 1.0)}]
    limites = [(0.0, peso_maximo)] * n
    return restricoes, limites


def _resolver(
    objetivo: Callable[[Vetor], float],
    n: int,
    restricoes: list[dict[str, Any]],
    limites: list[tuple[float, float]],
) -> Vetor:
    """Roda o SLSQP a partir de pesos iguais.

    SLSQP resolve problemas com restricoes de igualdade e desigualdade. O chute
    inicial e a carteira igualmente ponderada -- ponto viavel por construcao
    (soma 1, todos dentro dos limites), o que evita o solver comecar fora da
    regiao factivel e nao convergir.

    Minimizar variancia com essas restricoes e um problema CONVEXO: o minimo
    local encontrado e o global. Para o maximo Sharpe isso nao vale em geral,
    mas na pratica com poucos ativos o resultado e estavel -- e a carteira de
    minima variancia, que nao depende de retorno esperado, continua disponivel
    como alternativa mais robusta.
    """
    inicial: Vetor = np.full(n, 1.0 / n, dtype=np.float64)
    # O `scipy-stubs` declara `minimize` com sobrecargas que exigem a funcao
    # objetivo aceitando (*args, **kwargs) alem do vetor -- a nossa recebe so o
    # vetor, que e a forma correta e mais restrita. Nenhuma sobrecarga casa, e o
    # ignore fica aqui, na linha exata, em vez de afrouxar o mypy no projeto.
    resultado = minimize(  # type: ignore[call-overload]
        objetivo,
        inicial,
        method="SLSQP",
        bounds=limites,
        constraints=restricoes,
        options={"maxiter": 500, "ftol": 1e-12},
    )
    if not resultado.success:
        raise OtimizacaoInviavelError(f"o otimizador nao convergiu: {resultado.message}")

    pesos: Vetor = np.asarray(resultado.x, dtype=np.float64)
    pesos[pesos < PESO_MINIMO_RELEVANTE] = 0.0
    soma = pesos.sum()
    # Zerar os residuos desbalanceia a soma; renormalizamos para fechar em 100%.
    return pesos / soma if soma > 0 else pesos


def minima_variancia(cov: Matriz, peso_maximo: float = PESO_MAXIMO_PADRAO) -> Vetor:
    """A carteira de menor risco possivel.

    A mais confiavel do modelo, porque NAO usa retorno esperado -- so
    covariancia. Retorno esperado estimado a partir do historico e instavel;
    covariancia e bem mais estavel. Toda a fragilidade famosa de Markowitz vem
    da estimativa de retorno, nao da de risco.
    """
    restricoes, limites = _restricoes(len(cov), peso_maximo)
    return _resolver(lambda w: float(w @ cov @ w), len(cov), restricoes, limites)


def maximo_sharpe(
    mu: Vetor,
    cov: Matriz,
    taxa_livre_risco: float,
    peso_maximo: float = PESO_MAXIMO_PADRAO,
) -> Vetor:
    """A carteira de melhor retorno por unidade de risco.

    Maximizamos o Sharpe minimizando o seu negativo -- o `scipy.optimize` so
    minimiza. E a razao de o objetivo abaixo ter um sinal invertido.
    """

    def negativo_do_sharpe(w: Vetor) -> float:
        vol = _volatilidade(w, cov)
        if vol <= 0:
            # Penalidade em vez de divisao por zero: devolver `inf` faria o
            # solver perder a direcao do gradiente e parar ali.
            return 1e6
        return -float((w @ mu - taxa_livre_risco) / vol)

    restricoes, limites = _restricoes(len(mu), peso_maximo)
    return _resolver(negativo_do_sharpe, len(mu), restricoes, limites)


def carteira_para(pesos: Vetor, mu: Vetor, cov: Matriz, taxa_livre_risco: float) -> Carteira:
    """Avalia retorno, risco e Sharpe de um vetor de pesos qualquer.

    Usada tambem para medir a carteira ATUAL do usuario e coloca-la no mesmo
    grafico da fronteira -- que e a comparacao que de fato interessa a ele: "a
    minha carteira esta longe da fronteira?".
    """
    retorno = float(pesos @ mu)
    vol = _volatilidade(pesos, cov)
    return Carteira(
        pesos=pesos,
        retorno_esperado=retorno,
        volatilidade=vol,
        indice_sharpe=(retorno - taxa_livre_risco) / vol if vol > 0 else None,
    )


def fronteira_eficiente(
    mu: Vetor,
    cov: Matriz,
    taxa_livre_risco: float,
    *,
    pontos: int = PONTOS_FRONTEIRA,
    peso_maximo: float = PESO_MAXIMO_PADRAO,
) -> list[Carteira]:
    """A curva de menor risco para cada nivel de retorno.

    O alvo varre de MIN a MAX, onde:
      - MIN e o retorno da carteira de minima variancia (nada abaixo disso e
        eficiente: haveria outra carteira com mais retorno e menos risco);
      - MAX e o maior retorno alcancavel respeitando o limite por ativo -- que
        NAO e simplesmente o retorno do melhor ativo, porque com limite de 40%
        e impossivel colocar 100% nele.

    Alvos infactiveis sao ignorados em vez de derrubar a chamada: perto dos
    extremos o solver as vezes nao converge, e uma fronteira com 47 dos 50
    pontos e perfeitamente util.
    """
    n = len(mu)
    restricoes_base, limites = _restricoes(n, peso_maximo)

    piso = carteira_para(minima_variancia(cov, peso_maximo), mu, cov, taxa_livre_risco)

    # Retorno maximo respeitando o limite por ativo: aloca o maximo nos melhores
    # ativos ate completar 100%. Fecha o intervalo de busca no ponto certo.
    ordenados = np.sort(mu)[::-1]
    restante, teto = 1.0, 0.0
    for retorno_ativo in ordenados:
        parcela = min(peso_maximo, restante)
        teto += parcela * float(retorno_ativo)
        restante -= parcela
        if restante <= 1e-12:
            break

    if teto <= piso.retorno_esperado:
        return [piso]

    fronteira: list[Carteira] = [piso]
    for alvo in np.linspace(piso.retorno_esperado, teto, pontos)[1:]:
        restricoes = [
            *restricoes_base,
            {"type": "eq", "fun": lambda w, a=float(alvo): float(w @ mu - a)},
        ]
        try:
            pesos = _resolver(lambda w: float(w @ cov @ w), n, restricoes, limites)
        except OtimizacaoInviavelError:
            continue
        fronteira.append(carteira_para(pesos, mu, cov, taxa_livre_risco))

    return fronteira
