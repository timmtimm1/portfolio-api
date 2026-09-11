"""Orquestracao da otimizacao: carrega series, monta mu e Sigma, otimiza.

O `mu` vem de Black-Litterman (`app/services/black_litterman.py`), nao da media
historica. Este modulo e quem busca os ingredientes que o modelo puro nao pode
buscar sozinho -- valor de mercado no catalogo, series de preco -- e quem traduz
opiniao por ticker em matriz posicional.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.schemas.optimization import (
    CarteiraSugerida,
    EquilibrioResumo,
    OptimizationRequest,
    OptimizationResponse,
    Visao,
)
from app.services import black_litterman, series_service, transaction_service
from app.services.metrics import (
    MINIMO_OBSERVACOES,
    SeriesAlinhadas,
    matriz_covariancia,
    retorno_anualizado,
)
from app.services.optimizer import (
    Carteira,
    OtimizacaoInviavelError,
    carteira_para,
    fronteira_eficiente,
    maximo_sharpe,
    minima_variancia,
)
from app.services.position import Posicao

# Minimo de ativos para haver o que otimizar. Com um so, o resultado e "100%
# nele" -- resposta correta e inutil.
MINIMO_ATIVOS = 2


def _sugerida(carteira: Carteira, tickers: list[str]) -> CarteiraSugerida:
    """Converte pesos posicionais em pesos NOMEADOS.

    O otimizador trabalha com indices; a API devolve nomes. Fazer essa traducao
    num unico lugar evita o pior bug possivel aqui: pesos certos atribuidos aos
    ativos errados, que ninguem percebe porque os numeros parecem plausiveis.
    """
    return CarteiraSugerida(
        pesos={t: float(p) for t, p in zip(tickers, carteira.pesos, strict=True)},
        retorno_esperado=carteira.retorno_esperado,
        volatilidade=carteira.volatilidade,
        indice_sharpe=carteira.indice_sharpe,
    )


def _retornos_historicos(series: SeriesAlinhadas, tickers: list[str]) -> np.ndarray:
    """Retorno anualizado observado, por ativo.

    ATENCAO ao que este vetor virou. Ele JA FOI o `mu` entregue ao otimizador --
    o Markowitz classico, com a fraqueza conhecida de que pequenas mudancas na
    janela produzem carteiras bem diferentes. Hoje ele nao chega perto do
    otimizador: serve apenas para calcular UM numero, o retorno da carteira de
    mercado, que por sua vez alimenta a aversao ao risco.

    A diferenca importa. Uma media por ativo tem erro-padrao enorme e o
    otimizador amplifica esse erro; a media da carteira de mercado inteira e
    uma estimativa muito mais estavel, e entra numa divisao que so calibra a
    escala do premio de risco. Reintroduzir este vetor como `mu` desfaria a
    troca inteira.
    """
    return np.array([retorno_anualizado(series.precos[t]) for t in tickers], dtype=np.float64)


async def _valores_de_mercado(db: AsyncSession, tickers: list[str]) -> list[float | None]:
    """Valor de mercado de cada ticker, na ORDEM recebida.

    A ordem e o contrato: o vetor de pesos que sai disto multiplica a matriz de
    covariancia posicionalmente. Devolver um dicionario e deixar o chamador
    reordenar abriria a mesma porta que `_sugerida` fecha -- numero certo no
    ativo errado, plausivel demais para alguem notar.

    Decimal vira float aqui, e este e o lugar certo: a fronteira entre o
    dinheiro guardado no banco e a estatistica do modelo.
    """
    linhas = (
        await db.execute(select(Asset.ticker, Asset.market_cap).where(Asset.ticker.in_(tickers)))
    ).all()
    por_ticker = {ticker: valor for ticker, valor in linhas}
    return [None if por_ticker.get(t) is None else float(por_ticker[t]) for t in tickers]


def _matriz_de_visoes(
    visoes: Sequence[Visao], tickers: list[str]
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Traduz opinioes por ticker em (P, Q) posicionais.

    Opiniao que cita ativo fora do calculo e DESCARTADA com motivo, nunca
    aplicada pela metade. Zerar so o coeficiente do ativo ausente mudaria o
    sentido da opiniao sem avisar: "PETR4 supera VALE3 em 3 pontos" viraria
    "PETR4 rende 3%", que e uma afirmacao completamente diferente e que o
    usuario nunca fez.
    """
    indice = {t: i for i, t in enumerate(tickers)}
    linhas: list[list[float]] = []
    retornos: list[float] = []
    ignoradas: list[str] = []

    for visao in visoes:
        citados = sorted(visao.ativos)
        fora = [t for t in citados if t not in indice]
        if fora:
            ignoradas.append(
                f"Opiniao sobre {', '.join(citados)} descartada: "
                f"{', '.join(fora)} nao entrou no calculo."
            )
            continue
        linha = [0.0] * len(tickers)
        for ticker, coeficiente in visao.ativos.items():
            linha[indice[ticker]] = float(coeficiente)
        linhas.append(linha)
        retornos.append(visao.retorno)

    if not linhas:
        return np.zeros((0, len(tickers))), np.zeros(0), ignoradas
    return (
        np.array(linhas, dtype=np.float64),
        np.array(retornos, dtype=np.float64),
        ignoradas,
    )


async def otimizar(
    db: AsyncSession,
    portfolio_id: uuid.UUID,
    pedido: OptimizationRequest,
    *,
    taxa_livre_risco: float,
) -> OptimizationResponse:
    """Fronteira eficiente, minima variancia e maximo Sharpe.

    Quando `tickers` nao e informado, usa os ativos em carteira e devolve
    tambem a carteira atual do usuario avaliada com os mesmos parametros --
    que e o que permite ao grafico mostrar "voce esta aqui, a fronteira esta ali".
    """
    da_carteira = pedido.tickers is None
    posicoes = await transaction_service.posicoes(db, portfolio_id)
    abertas = [p for p in posicoes if not p.esta_zerada]

    pedidos = (
        sorted({t.strip().upper() for t in pedido.tickers if t.strip()})
        if pedido.tickers is not None
        else sorted(p.ticker for p in abertas)
    )

    def vazio(motivo: str) -> OptimizationResponse:
        return OptimizationResponse(
            inicio=None,
            fim=None,
            pregoes=0,
            taxa_livre_risco=taxa_livre_risco,
            peso_maximo=pedido.peso_maximo,
            tickers=[],
            fronteira=[],
            minima_variancia=None,
            maximo_sharpe=None,
            carteira_atual=None,
            sem_historico_suficiente=pedidos,
            motivo=motivo,
        )

    if len(pedidos) < MINIMO_ATIVOS:
        return vazio(
            "A otimizacao precisa de pelo menos dois ativos. Com um so, a resposta "
            "seria '100% nele' -- correta e inutil."
        )

    # Verificacao antecipada da restricao, para explicar em vez de so falhar.
    # N ativos com teto de p cada somam no maximo N x p; se isso for menor que
    # 100%, nao existe carteira valida.
    if len(pedidos) * pedido.peso_maximo < 1.0 - 1e-9:
        minimo = 1.0 / len(pedidos)
        return vazio(
            f"{len(pedidos)} ativos com limite de {pedido.peso_maximo:.0%} cada somam "
            f"no maximo {len(pedidos) * pedido.peso_maximo:.0%} -- impossivel investir "
            f"100%. Aumente o limite para pelo menos {minimo:.0%} ou inclua mais ativos."
        )

    series = await series_service.carregar_series(db, pedidos, desde=pedido.desde, ate=pedido.ate)
    if series is None or len(series) <= MINIMO_OBSERVACOES:
        return vazio(
            "Nao ha historico de precos suficiente em comum entre estes ativos. "
            f"Sao necessarios mais de {MINIMO_OBSERVACOES} pregoes com todos negociando."
        )

    aptos = [t for t in series.tickers if len(series.precos[t]) > MINIMO_OBSERVACOES]
    if len(aptos) < MINIMO_ATIVOS:
        return vazio("Menos de dois ativos tem historico suficiente para entrar no calculo.")

    # `subconjunto` preserva o alinhamento -- remontar o dicionario a mao seria a
    # brecha por onde series desalinhadas voltariam a virar covariancia errada.
    usadas = series.subconjunto(aptos)
    tickers, cov_amostral = matriz_covariancia(usadas)

    # --- Black-Litterman -----------------------------------------------------
    #
    # O retorno da CARTEIRA DE MERCADO e a unica media historica que sobrevive,
    # e ela entra so no denominador da aversao ao risco. Ver `_retornos_historicos`.
    valores = await _valores_de_mercado(db, tickers)
    pesos_mercado, _ = black_litterman.pesos_de_mercado(valores)
    retorno_mercado = float(pesos_mercado @ _retornos_historicos(usadas, tickers))

    p_visoes, q_visoes, visoes_ignoradas = _matriz_de_visoes(pedido.visoes, tickers)
    posterior = black_litterman.black_litterman(
        cov_amostral,
        valores,
        retorno_mercado=retorno_mercado,
        taxa_livre_risco=taxa_livre_risco,
        p=p_visoes,
        q_total=q_visoes,
    )

    # `mu` volta a ser retorno TOTAL aqui, que e a convencao do otimizador e de
    # `carteira_para`. A travessia acontece uma vez so, com nome.
    mu = black_litterman.retorno_total(posterior.mu, taxa_livre_risco)
    # A covariancia do posterior, nao a amostral: ela carrega a incerteza sobre a
    # propria media (o termo tau*Sigma) e, com opinioes, a reducao de incerteza
    # que elas trazem. Otimizar o mu do modelo contra o Sigma cru misturaria duas
    # visoes de mundo.
    cov = posterior.cov

    equilibrio = EquilibrioResumo(
        pesos_mercado={t: float(w) for t, w in zip(tickers, pesos_mercado, strict=True)},
        aversao_ao_risco=posterior.equilibrio.delta,
        usou_peso_igual=posterior.equilibrio.usou_peso_igual,
        usou_delta_padrao=posterior.equilibrio.usou_delta_padrao,
        sem_valor_de_mercado=[tickers[i] for i in posterior.equilibrio.sem_valor_de_mercado],
    )

    try:
        min_var = carteira_para(
            minima_variancia(cov, pedido.peso_maximo), mu, cov, taxa_livre_risco
        )
        max_sharpe = carteira_para(
            maximo_sharpe(mu, cov, taxa_livre_risco, pedido.peso_maximo), mu, cov, taxa_livre_risco
        )
        fronteira = fronteira_eficiente(
            mu, cov, taxa_livre_risco, pontos=pedido.pontos, peso_maximo=pedido.peso_maximo
        )
    except OtimizacaoInviavelError as exc:
        # Rede de seguranca: a checagem antecipada acima cobre o caso previsivel,
        # mas o solver pode nao convergir por outros motivos. A restricao veio do
        # usuario, entao a resposta e vazia e explicita, nunca um 500.
        return vazio(f"Nao foi possivel otimizar com estas restricoes. {exc}")

    atual = None
    if da_carteira:
        pesos_atuais = _pesos_da_carteira(abertas, tickers)
        if pesos_atuais is not None:
            atual = _sugerida(carteira_para(pesos_atuais, mu, cov, taxa_livre_risco), tickers)

    return OptimizationResponse(
        inicio=usadas.inicio,
        fim=usadas.fim,
        pregoes=len(usadas),
        taxa_livre_risco=taxa_livre_risco,
        peso_maximo=pedido.peso_maximo,
        tickers=tickers,
        fronteira=[_sugerida(c, tickers) for c in fronteira],
        minima_variancia=_sugerida(min_var, tickers),
        maximo_sharpe=_sugerida(max_sharpe, tickers),
        carteira_atual=atual,
        sem_historico_suficiente=[t for t in pedidos if t not in tickers],
        equilibrio=equilibrio,
        visoes_aplicadas=posterior.visoes_aplicadas,
        visoes_ignoradas=visoes_ignoradas,
    )


def _pesos_da_carteira(posicoes: Sequence[Posicao], tickers: list[str]) -> np.ndarray | None:
    """Pesos atuais pelo CUSTO das posicoes.

    Custo, e nao valor de mercado, deliberadamente: usar valor de mercado
    exigiria buscar cotacao aqui, acoplando a otimizacao a disponibilidade de um
    fornecedor externo. O custo ja esta no banco, sempre existe, e para "quanto
    do meu capital esta em cada ativo" e uma leitura defensavel.
    """
    por_ticker = {p.ticker: float(p.custo_total) for p in posicoes}
    valores = np.array([por_ticker.get(t, 0.0) for t in tickers], dtype=np.float64)
    total = valores.sum()
    if total <= 0:
        return None
    return valores / total
