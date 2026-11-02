"""Orquestracao da otimizacao: carrega series, monta mu e Sigma, otimiza.

O `mu` vem de Black-Litterman (`app/services/black_litterman.py`), nao da media
historica. Este modulo e quem busca os ingredientes que o modelo puro nao pode
buscar sozinho -- series de preco, o Ibovespa da mesma janela, a taxa livre de
risco -- e quem traduz opiniao por ticker em matriz posicional.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.benchmark import Indexador
from app.schemas.optimization import (
    CarteiraSugerida,
    EquilibrioResumo,
    OptimizationRequest,
    OptimizationResponse,
    Visao,
)
from app.services import black_litterman, series_service, transaction_service
from app.services.benchmark_service import BuscarTaxas, TaxaLivreRisco, taxas_do_periodo
from app.services.metrics import (
    MINIMO_OBSERVACOES,
    alinhar_com_indice,
    covariancia_com_indice,
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
    taxa: TaxaLivreRisco,
    buscar_ibov: BuscarTaxas,
) -> OptimizationResponse:
    """Fronteira eficiente, minima variancia e maximo Sharpe.

    Quando `tickers` nao e informado, usa os ativos em carteira e devolve
    tambem a carteira atual do usuario avaliada com os mesmos parametros --
    que e o que permite ao grafico mostrar "voce esta aqui, a fronteira esta ali".

    `buscar_ibov` e quem sabe trazer o Ibovespa do fornecedor quando o banco
    nao cobre a janela -- injetado, como nas outras rotas, para o teste rodar
    sem rede.
    """
    taxa_livre_risco = taxa.valor
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
            fonte_taxa_livre_risco=taxa.fonte,
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

    # --- Black-Litterman -----------------------------------------------------
    #
    # O mercado e o Ibovespa, na MESMA janela dos precos. A covariancia dos
    # ativos e a de cada um com o indice saem de uma unica amostra: os dias em
    # que existem as duas coisas.
    variacoes = await taxas_do_periodo(
        db, Indexador.IBOV, usadas.inicio, usadas.fim, buscar=buscar_ibov
    )
    alinhadas = alinhar_com_indice(usadas, {d: float(v) for d, v in variacoes.items()})
    if alinhadas is None or len(alinhadas[0]) <= MINIMO_OBSERVACOES:
        return vazio(
            "Sem a serie do Ibovespa para este periodo, nao ha como calcular o "
            "equilibrio de mercado. Tente de novo em alguns minutos."
        )
    usadas, nivel_ibov = alinhadas
    tickers, cov_amostral, cov_com_ibov, var_ibov = covariancia_com_indice(usadas, nivel_ibov)

    p_visoes, q_visoes, visoes_ignoradas = _matriz_de_visoes(pedido.visoes, tickers)
    try:
        posterior = black_litterman.black_litterman(
            cov_amostral,
            cov_com_ibov,
            var_ibov,
            taxa_livre_risco=taxa_livre_risco,
            premio_de_risco=pedido.premio_de_risco,
            p=p_visoes,
            q_total=q_visoes,
        )
    except ValueError:
        # So acontece com o Ibovespa parado a janela inteira -- dado quebrado.
        return vazio("A serie do Ibovespa neste periodo nao tem variacao. Confira os dados.")

    # `mu` volta a ser retorno TOTAL aqui, que e a convencao do otimizador e de
    # `carteira_para`. A travessia acontece uma vez so, com nome.
    mu = black_litterman.retorno_total(posterior.mu, taxa_livre_risco)
    # A covariancia do posterior, nao a amostral: ela carrega a incerteza sobre a
    # propria media (o termo tau*Sigma) e, com opinioes, a reducao de incerteza
    # que elas trazem. Otimizar o mu do modelo contra o Sigma cru misturaria duas
    # visoes de mundo.
    cov = posterior.cov

    eq = posterior.equilibrio
    pi_total = black_litterman.retorno_total(eq.pi, taxa_livre_risco)
    equilibrio = EquilibrioResumo(
        premio_de_risco=eq.premio_de_risco,
        aversao_ao_risco=eq.delta,
        volatilidade_mercado=float(np.sqrt(eq.variancia_mercado)),
        betas={t: float(b) for t, b in zip(tickers, eq.betas, strict=True)},
        retorno_de_equilibrio={t: float(r) for t, r in zip(tickers, pi_total, strict=True)},
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
        fonte_taxa_livre_risco=taxa.fonte,
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
