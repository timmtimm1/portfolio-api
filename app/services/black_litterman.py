"""Black-Litterman: retorno esperado a partir do equilibrio de mercado e das opinioes.

## O problema que este modulo resolve

A fronteira eficiente de Markowitz e sensivel ao `mu` que recebe. Usar a media
historica como estimativa do retorno esperado -- o que `optimizer_service` fazia
-- tem uma falha conhecida e grave: pequenas mudancas na janela produzem
carteiras completamente diferentes, e o otimizador concentra tudo no papel que
por acaso mais subiu na amostra. A media historica de um ativo e uma estimativa
com erro-padrao enorme; a covariancia, nao. Como o otimizador amplifica erro no
retorno muito mais que erro na covariancia, o resultado e uma carteira que
parece otima e e, na pratica, ruido.

Black-Litterman ataca exatamente isso. Em vez de perguntar "quanto este ativo
rendeu?", ele pergunta "que retorno o mercado PRECISA estar esperando para que
os precos de hoje facam sentido?". Esse e o retorno de equilibrio, e ele nasce
de uma otimizacao reversa: se a carteira de mercado e otima, entao

    pi = delta * Sigma * w_mercado

Sobre esse ponto de partida o investidor sobrepoe opinioes ("acho que PETR4
rende 15% ao ano", "acho que bancos superam mineracao em 3 pontos"), e o modelo
combina as duas fontes ponderando pela incerteza de cada uma. Sem opiniao
nenhuma, o resultado E o equilibrio -- uma carteira parecida com o mercado, que
e um default muito mais defensavel que "tudo no que mais subiu".

## Qual variante

A de He & Litterman (1999), com `Omega = diag(P tau Sigma P')` -- a formulacao
mais usada, e a que Idzorek popularizou. A escolha de Omega nao e detalhe: ela
diz o quanto confiar em cada opiniao. Com esta, a incerteza da opiniao fica
proporcional a incerteza que o proprio mercado tem naquela combinacao de
ativos, e o efeito pratico e que cada opiniao entra com peso ~50% contra o
equilibrio. Arbitrar Omega na mao daria ao usuario um botao que ele nao tem como
calibrar.

## Unidades

Tudo aqui e ANUALIZADO, e `pi` e `mu` sao retorno EXCEDENTE (acima da taxa livre
de risco), nao retorno total. Isso importa: o resto do sistema trabalha com
retorno total, e somar a taxa livre de risco de volta e responsabilidade de quem
chama (`retorno_total`). Misturar as duas convencoes nao estoura em lugar nenhum
-- as contas fecham e os pesos saem errados, que e o pior tipo de erro. A
conversao acontece numa fronteira explicita, como a de Decimal para float.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

Vetor = np.ndarray[Any, np.dtype[np.float64]]
Matriz = np.ndarray[Any, np.dtype[np.float64]]

# Incerteza do proprio equilibrio, como fracao da covariancia.
#
# 0,025 e o valor de He & Litterman e o mais usado na literatura. A intuicao:
# `tau * Sigma` e a incerteza sobre a MEDIA dos retornos, que e muito menor que
# a variancia dos retornos em si -- dai um numero pequeno. O resultado final e
# pouco sensivel a ele dentro da faixa usual (0,01 a 0,05), porque tau aparece
# no numerador e no denominador da combinacao; o que ele de fato controla e o
# peso relativo entre equilibrio e opiniao.
TAU_PADRAO = 0.025

# Aversao ao risco quando nao da para estimar do mercado.
#
# 2,5 e o valor que He & Litterman usaram para o mercado global e virou o
# default de facto. Serve de rede: a estimativa amostral vira lixo numa janela
# em que o mercado caiu (delta sairia negativo, e o equilibrio mandaria vender
# tudo -- resposta absurda que o modelo daria com cara de seria).
DELTA_PADRAO = 2.5

# Faixa aceitavel para o delta estimado. Fora dela, cai para o padrao.
#
# Abaixo de 0,5 o investidor seria quase indiferente ao risco e o equilibrio
# viraria uma aposta alavancada; acima de 10 ele seria tao avesso que so o ativo
# de menor variancia sobreviveria. Os dois extremos sao sintoma de amostra ruim,
# nao de preferencia real.
DELTA_MINIMO = 0.5
DELTA_MAXIMO = 10.0


@dataclass(frozen=True)
class Equilibrio:
    """O prior: o que o mercado esta implicitamente esperando.

    `usou_peso_igual` e `usou_delta_padrao` existem para a interface poder
    dizer ao usuario em que terreno ele esta pisando. Um resultado calculado
    sobre peso igual porque faltou valor de mercado nao e errado, mas tambem
    nao e "o equilibrio do mercado" -- e chamar os dois pela mesma coisa
    enganaria quem le o grafico.
    """

    pesos_mercado: Vetor
    delta: float
    pi: Vetor
    usou_peso_igual: bool
    usou_delta_padrao: bool
    sem_valor_de_mercado: list[int]
    """Indices dos ativos sem valor de mercado, na ordem recebida."""


@dataclass(frozen=True)
class Posterior:
    """O resultado: retorno esperado e covariancia depois de combinar tudo."""

    mu: Vetor
    """Retorno EXCEDENTE esperado, anualizado. Ver `retorno_total`."""
    cov: Matriz
    equilibrio: Equilibrio
    visoes_aplicadas: int


def pesos_de_mercado(valores: Sequence[float | None]) -> tuple[Vetor, list[int]]:
    """Peso de cada ativo na carteira de mercado, pelo valor de mercado.

    Devolve tambem os indices sem valor. Quando FALTA QUALQUER UM, a funcao cai
    para peso igual no vetor INTEIRO, e nao so no ativo faltante.

    Isso e deliberado e vale explicar. A alternativa tentadora -- dar ao ativo
    sem dado algum valor plausivel, ou distribuir o residual -- produz um vetor
    que parece o mercado mas nao e, e o erro se propaga silenciosamente para o
    `pi` de TODOS os ativos, porque o peso e uma fracao do total. Peso igual e
    obviamente uma aproximacao; um mercado meio real e meio inventado nao e
    obviamente nada, e e por isso que e pior.
    """
    faltantes: list[int] = []
    capitalizacoes: list[float] = []
    for i, valor in enumerate(valores):
        # Zero e negativo entram como ausencia, e nao como numero pequeno: um
        # valor de mercado nulo nao existe no mundo real, entao e dado sujo --
        # e dado sujo dividido pelo total daria peso 0 com cara de legitimo.
        if valor is None or valor <= 0:
            faltantes.append(i)
            capitalizacoes.append(0.0)
        else:
            capitalizacoes.append(float(valor))

    n = len(capitalizacoes)
    if n == 0:
        return np.zeros(0, dtype=np.float64), faltantes
    if faltantes:
        return np.full(n, 1.0 / n, dtype=np.float64), faltantes

    total = np.array(capitalizacoes, dtype=np.float64)
    return np.asarray(total / total.sum(), dtype=np.float64), faltantes


def aversao_ao_risco(
    retorno_mercado: float, variancia_mercado: float, taxa_livre_risco: float
) -> tuple[float, bool]:
    """delta = (E[Rm] - rf) / var(Rm). Devolve (delta, caiu_no_padrao).

    E a otimizacao reversa aplicada a carteira de mercado inteira: se ela e
    otima para um investidor de aversao `delta`, entao esse delta e o preco do
    risco que o mercado esta cobrando.

    A estimativa amostral falha de um jeito especifico e perigoso: numa janela
    em que o mercado caiu, o numerador fica negativo e delta tambem. Com delta
    negativo, `pi = delta * Sigma * w` inverte de sinal e o equilibrio passa a
    dizer que os ativos mais arriscados sao os que rendem MENOS -- a otimizacao
    entao empurra a carteira para eles. O resultado sai sem erro, com numeros
    de aparencia normal, e esta invertido. Dai a faixa aceitavel: fora dela, o
    problema e a amostra, e o padrao da literatura e menos errado.
    """
    if variancia_mercado <= 0.0:
        return DELTA_PADRAO, True
    delta = (retorno_mercado - taxa_livre_risco) / variancia_mercado
    if not np.isfinite(delta) or delta < DELTA_MINIMO or delta > DELTA_MAXIMO:
        return DELTA_PADRAO, True
    return float(delta), False


def retornos_de_equilibrio(delta: float, cov: Matriz, pesos_mercado: Vetor) -> Vetor:
    """pi = delta * Sigma * w -- a otimizacao reversa.

    Le-se: o retorno excedente que cada ativo precisa estar prometendo para que
    ninguem queira mudar a carteira de mercado. Ativo que contribui muito para o
    risco do conjunto (linha de Sigma grande contra os pesos) precisa prometer
    mais. Repare que nao ha nenhum retorno historico nesta conta -- so
    covariancia e pesos, que sao as duas coisas que se estima bem.
    """
    return np.asarray(delta * cov @ pesos_mercado, dtype=np.float64)


def equilibrio_de_mercado(
    cov: Matriz,
    valores_de_mercado: Sequence[float | None],
    *,
    retorno_mercado: float,
    taxa_livre_risco: float,
) -> Equilibrio:
    """Monta o prior inteiro: pesos, delta e pi.

    `retorno_mercado` e o retorno anualizado da carteira de mercado na mesma
    janela de `cov`. A variancia dela sai da propria `cov` (w' Sigma w), e nao
    de uma segunda estimativa -- usar duas fontes para o mesmo numero e como
    ficam as inconsistencias que ninguem consegue rastrear depois.
    """
    pesos, faltantes = pesos_de_mercado(valores_de_mercado)
    variancia = float(pesos @ cov @ pesos) if pesos.size else 0.0
    delta, padrao = aversao_ao_risco(retorno_mercado, variancia, taxa_livre_risco)
    return Equilibrio(
        pesos_mercado=pesos,
        delta=delta,
        pi=retornos_de_equilibrio(delta, cov, pesos),
        usou_peso_igual=bool(faltantes),
        usou_delta_padrao=padrao,
        sem_valor_de_mercado=faltantes,
    )


def para_excesso(p: Matriz, q: Vetor, taxa_livre_risco: float) -> Vetor:
    """Converte opinioes de retorno TOTAL para retorno EXCEDENTE.

    A conta e `q - rf * soma_da_linha`, e ela trata os dois tipos de opiniao de
    uma vez so, sem precisar saber qual e qual:

    - Absoluta ("PETR4 rende 15%"): a linha e [1, 0, ...], soma 1, e sai
      `15% - rf`. Correto: o modelo trabalha em excedente.
    - Relativa ("PETR4 supera VALE3 em 3 pontos"): a linha e [1, -1, 0, ...],
      soma 0, e o valor passa intacto. Tambem correto -- numa diferenca entre
      dois ativos a taxa livre de risco se cancela, e subtrai-la seria contar
      duas vezes.

    Escrever isso como uma formula so, em vez de um `if` sobre o tipo da
    opiniao, e o que garante que uma opiniao mista (meia carteira contra um
    ativo) tambem saia certa.
    """
    if p.size == 0:
        return np.zeros(0, dtype=np.float64)
    return np.asarray(q - taxa_livre_risco * p.sum(axis=1), dtype=np.float64)


def omega_he_litterman(p: Matriz, cov: Matriz, tau: float) -> Matriz:
    """Omega = diag(P tau Sigma P').

    A incerteza de cada opiniao fica proporcional a incerteza que o proprio
    mercado ja tem sobre aquela combinacao de ativos. Opiniao sobre um par
    volatil entra com menos forca que a mesma opiniao sobre um par estavel, o
    que e o comportamento desejado e sai de graca.

    Diagonal, e nao a matriz cheia: assume que os erros das opinioes sao
    independentes entre si. E o padrao da literatura, e a alternativa exigiria
    do usuario dizer a correlacao entre os erros das proprias opinioes -- um
    numero que ninguem sabe.
    """
    return np.asarray(np.diag(np.diag(p @ (tau * cov) @ p.T)), dtype=np.float64)


def combinar(
    pi: Vetor, cov: Matriz, p: Matriz, q_excesso: Vetor, *, tau: float = TAU_PADRAO
) -> tuple[Vetor, Matriz]:
    """A formula de Black-Litterman. Devolve (mu posterior, Sigma posterior).

        mu = pi + tau*S*P' (P tau*S P' + Omega)^-1 (q - P pi)
        Sigma_post = S + tau*S - tau*S*P' (P tau*S P' + Omega)^-1 P tau*S

    Esta forma e algebricamente identica a versao com `(tau*Sigma)^-1` que
    aparece no artigo original, e numericamente melhor: ela inverte uma matriz
    k x k (uma linha por opiniao, tipicamente 1 a 3) em vez de uma n x n que
    fica mal condicionada quando ha poucos ativos ou ativos muito
    correlacionados -- caso comum numa carteira concentrada em bancos.

    O termo `(q - P pi)` e a leitura do modelo: o quanto a opiniao DISCORDA do
    equilibrio. Opiniao que apenas repete o que o mercado ja diz nao move nada,
    e e assim que deve ser.

    Sem opiniao alguma o posterior e o proprio equilibrio, com a covariancia
    inflada em (1 + tau) -- a incerteza sobre a media entra na conta de risco.
    """
    tau_sigma = tau * cov

    if p.size == 0 or p.shape[0] == 0:
        return np.asarray(pi, dtype=np.float64), np.asarray(cov + tau_sigma, dtype=np.float64)

    omega = omega_he_litterman(p, cov, tau)
    meio = p @ tau_sigma @ p.T + omega

    # `solve` e nao `inv`: resolver o sistema e mais estavel e mais barato que
    # inverter e multiplicar. `pinv` como rede para o caso degenerado de duas
    # opinioes identicas, que deixam `meio` singular -- situacao que o usuario
    # cria sem perceber ao repetir a mesma opiniao com outras palavras.
    try:
        ajuste = np.linalg.solve(meio, q_excesso - p @ pi)
        correcao = np.linalg.solve(meio, p @ tau_sigma)
    except np.linalg.LinAlgError:
        inversa = np.linalg.pinv(meio)
        ajuste = inversa @ (q_excesso - p @ pi)
        correcao = inversa @ (p @ tau_sigma)

    mu = pi + tau_sigma @ p.T @ ajuste
    m = tau_sigma - tau_sigma @ p.T @ correcao
    cov_post = cov + m

    # Simetriza: o produto de matrizes acumula assimetria da ordem de 1e-17, e
    # um otimizador que confere simetria recusaria a matriz por causa disso.
    cov_post = (cov_post + cov_post.T) / 2.0
    return np.asarray(mu, dtype=np.float64), np.asarray(cov_post, dtype=np.float64)


def retorno_total(mu_excesso: Vetor, taxa_livre_risco: float) -> Vetor:
    """Soma a taxa livre de risco de volta.

    Existe como funcao nomeada, e nao como um `+ rf` solto no meio do servico,
    porque e a fronteira entre a convencao deste modulo (excedente) e a do resto
    do sistema (total). Fronteira que tem nome e uma fronteira que alguem lembra
    de atravessar.
    """
    return np.asarray(mu_excesso + taxa_livre_risco, dtype=np.float64)


def black_litterman(
    cov: Matriz,
    valores_de_mercado: Sequence[float | None],
    *,
    retorno_mercado: float,
    taxa_livre_risco: float,
    p: Matriz | None = None,
    q_total: Vetor | None = None,
    tau: float = TAU_PADRAO,
) -> Posterior:
    """Caminho completo: equilibrio, opinioes em excedente, combinacao.

    `q_total` vem em retorno TOTAL, que e como uma pessoa expressa opiniao
    ("acho que rende 15% ao ano"). A conversao para excedente acontece aqui
    dentro, uma vez so.
    """
    equilibrio = equilibrio_de_mercado(
        cov,
        valores_de_mercado,
        retorno_mercado=retorno_mercado,
        taxa_livre_risco=taxa_livre_risco,
    )

    n = cov.shape[0]
    if p is None or q_total is None or p.size == 0:
        p_efetivo = np.zeros((0, n), dtype=np.float64)
        q_efetivo = np.zeros(0, dtype=np.float64)
    else:
        p_efetivo = np.asarray(p, dtype=np.float64)
        q_efetivo = para_excesso(p_efetivo, np.asarray(q_total, dtype=np.float64), taxa_livre_risco)

    mu, cov_post = combinar(equilibrio.pi, cov, p_efetivo, q_efetivo, tau=tau)
    return Posterior(
        mu=mu,
        cov=cov_post,
        equilibrio=equilibrio,
        visoes_aplicadas=int(p_efetivo.shape[0]),
    )
