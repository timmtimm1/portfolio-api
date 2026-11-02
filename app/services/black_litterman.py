"""Black-Litterman: retorno esperado a partir do equilibrio de mercado e das opinioes.

## O problema que este modulo resolve

A fronteira eficiente de Markowitz e sensivel ao `mu` que recebe. Usar a media
historica como estimativa do retorno esperado tem uma falha conhecida e grave:
pequenas mudancas na janela produzem carteiras completamente diferentes, e o
otimizador concentra tudo no papel que por acaso mais subiu na amostra. A media
historica de um ativo e uma estimativa com erro-padrao enorme; a covariancia,
nao. Como o otimizador amplifica erro no retorno muito mais que erro na
covariancia, o resultado e uma carteira que parece otima e e, na pratica, ruido.

Black-Litterman ataca exatamente isso. Em vez de perguntar "quanto este ativo
rendeu?", ele pergunta "que retorno o mercado PRECISA estar esperando para que
os precos de hoje facam sentido?". Esse e o retorno de equilibrio, e ele nasce
de uma otimizacao reversa: se a carteira de mercado M e otima, entao cada ativo
promete um excedente proporcional ao risco que ele soma a M:

    pi_i = delta * cov(r_i, r_M)          delta = premio / var(r_M)
         = beta_i * premio                beta_i = cov(r_i, r_M) / var(r_M)

Sobre esse ponto de partida o investidor sobrepoe opinioes ("acho que PETR4
rende 15% ao ano", "acho que bancos superam mineracao em 3 pontos"), e o modelo
combina as duas fontes ponderando pela incerteza de cada uma. Sem opiniao
nenhuma, o resultado E o equilibrio.

## O mercado e o Ibovespa, nao a carteira do usuario

A primeira versao deste modulo usava como "mercado" os proprios ativos da
carteira, pesados pelo valor de mercado, e estimava `delta` pelo retorno
passado desse conjunto. As duas escolhas estavam erradas, e o erro era grande:

  - **O retorno de um ativo dependia dos vizinhos.** Com o mercado igual a
    carteira, `pi` de ITUB4 saia 16% ao lado de WEGE3 e 52% ao lado de VALE3 e
    PETR4 (precos reais, 2025-2026). O equilibrio e do mercado inteiro; o que a
    pessoa tem na carteira nao muda o que o mercado espera de um papel.
  - **A media historica voltava pela porta dos fundos.** Um ano em que a
    carteira subiu 43% dava delta 8,9 e um premio de 33% ao ano acima do CDI --
    retorno esperado de 25% a 64% para acoes brasileiras. Exatamente o ruido que
    o modelo existe para tirar.
  - **Classes da mesma empresa contavam duas vezes.** O valor de mercado do
    Yahoo e o da empresa inteira em cada classe; PETR3 + PETR4 viravam 70% do
    "mercado".

Agora o mercado e o Ibovespa, medido na MESMA janela da covariancia, e o premio
de risco e uma PREMISSA declarada (5% ao ano sobre o CDI por padrao), nao uma
media do passado. O valor de mercado deixou de ser necessario.

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

import math
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

# Quanto a bolsa rende acima do CDI, por ano, no longo prazo. E PREMISSA, nao
# medida -- e por isso fica declarada e editavel em vez de sair do historico.
#
# 5% fica no meio do que se usa para o Brasil (4% a 6% sobre o CDI). Com a
# volatilidade do Ibovespa perto de 22% ao ano, da delta ~1,0. O 2,5 da
# literatura foi calibrado para a bolsa americana (vol ~15%); aplicado aqui,
# implicaria um premio de ~12,5% ao ano, alto demais.
PREMIO_DE_RISCO_PADRAO = 0.05


@dataclass(frozen=True)
class Equilibrio:
    """O prior: o que o mercado esta implicitamente esperando de cada ativo."""

    premio_de_risco: float
    """Premio anual da carteira de mercado sobre a taxa livre de risco."""
    variancia_mercado: float
    """Variancia anualizada do indice de mercado, na janela da covariancia."""
    delta: float
    """Aversao ao risco implicita: premio / variancia do mercado."""
    betas: Vetor
    """cov(r_i, r_M) / var(r_M): quanto cada ativo acompanha o mercado."""
    pi: Vetor
    """Retorno EXCEDENTE de equilibrio (beta * premio), anualizado."""


@dataclass(frozen=True)
class Posterior:
    """O resultado: retorno esperado e covariancia depois de combinar tudo."""

    mu: Vetor
    """Retorno EXCEDENTE esperado, anualizado. Ver `retorno_total`."""
    cov: Matriz
    equilibrio: Equilibrio
    visoes_aplicadas: int


def aversao_ao_risco(premio_de_risco: float, variancia_mercado: float) -> float:
    """delta = premio / var(r_M).

    E a otimizacao reversa aplicada a carteira de mercado inteira: se ela e
    otima para um investidor de aversao `delta`, entao esse delta e o preco do
    risco que o mercado esta cobrando.

    Recusa entrada impossivel em vez de inventar um numero. Variancia zero (ou
    negativa, ou NaN) so aparece com dado quebrado, e premio nao positivo faria
    o equilibrio dizer que risco nao paga nada -- ou que paga para perder. A
    versao antiga caia num delta padrao nesses casos e seguia com cara de
    normal; aqui quem chama decide o que dizer ao usuario.
    """
    if not math.isfinite(variancia_mercado) or variancia_mercado <= 0.0:
        raise ValueError(f"variancia do mercado invalida: {variancia_mercado}")
    if not math.isfinite(premio_de_risco) or premio_de_risco <= 0.0:
        raise ValueError(f"premio de risco precisa ser positivo: {premio_de_risco}")
    return premio_de_risco / variancia_mercado


def retornos_de_equilibrio(delta: float, cov_com_mercado: Vetor) -> Vetor:
    """pi = delta * cov(r_i, r_M) -- a otimizacao reversa, linha a linha.

    E o mesmo `delta * Sigma * w_mercado` da formulacao classica, escrito sem
    precisar da matriz do mercado inteiro: `Sigma * w_mercado` E a covariancia
    de cada ativo com a carteira de mercado. Medindo essa covariancia direto
    contra o indice, o `pi` de um ativo passa a depender so dele e do mercado
    -- nao do que mais estiver na carteira.

    Repare que nao ha nenhum retorno historico nesta conta: so covariancia, que
    e o que se estima bem, e o premio, que e premissa declarada.
    """
    return np.asarray(delta * np.asarray(cov_com_mercado, dtype=np.float64), dtype=np.float64)


def equilibrio_de_mercado(
    cov_com_mercado: Vetor,
    variancia_mercado: float,
    *,
    premio_de_risco: float = PREMIO_DE_RISCO_PADRAO,
) -> Equilibrio:
    """Monta o prior inteiro: delta, betas e pi.

    `cov_com_mercado[i]` e a covariancia anualizada do ativo i com o indice de
    mercado e `variancia_mercado` a do indice, as duas na MESMA janela e na
    mesma unidade da covariancia entre os ativos. Misturar janelas aqui daria
    betas de um periodo aplicados a riscos de outro.
    """
    delta = aversao_ao_risco(premio_de_risco, variancia_mercado)
    c = np.asarray(cov_com_mercado, dtype=np.float64)
    return Equilibrio(
        premio_de_risco=premio_de_risco,
        variancia_mercado=variancia_mercado,
        delta=delta,
        betas=np.asarray(c / variancia_mercado, dtype=np.float64),
        pi=retornos_de_equilibrio(delta, c),
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
    cov_com_mercado: Vetor,
    variancia_mercado: float,
    *,
    taxa_livre_risco: float,
    premio_de_risco: float = PREMIO_DE_RISCO_PADRAO,
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
        cov_com_mercado, variancia_mercado, premio_de_risco=premio_de_risco
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
