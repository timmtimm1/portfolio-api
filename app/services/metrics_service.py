"""Orquestracao das metricas: carrega series, calcula, monta a resposta."""

from __future__ import annotations

import uuid
from datetime import date as date_type

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.metrics import AssetMetrics, CorrelationMatrix, PortfolioMetrics
from app.services import series_service, transaction_service
from app.services.metrics import MINIMO_OBSERVACOES, matriz_correlacao, metricas_do_ativo


async def metricas(
    db: AsyncSession,
    tickers: list[str],
    *,
    taxa_livre_risco: float,
    desde: date_type | None = None,
    ate: date_type | None = None,
) -> PortfolioMetrics:
    """Metricas de cada ativo e a matriz de correlacao entre eles."""
    pedidos = sorted({t.strip().upper() for t in tickers if t.strip()})
    series = await series_service.carregar_series(db, pedidos, desde=desde, ate=ate)

    if series is None or len(series) <= MINIMO_OBSERVACOES:
        return PortfolioMetrics(
            inicio=series.inicio if series else None,
            fim=series.fim if series else None,
            pregoes=len(series) if series else 0,
            taxa_livre_risco=taxa_livre_risco,
            ativos=[],
            correlacao=None,
            sem_historico_suficiente=pedidos,
        )

    calculadas: list[AssetMetrics] = []
    aptos: list[str] = []

    for ticker in series.tickers:
        m = metricas_do_ativo(ticker, series.precos[ticker], taxa_livre_risco)
        if m is None:
            continue
        aptos.append(ticker)
        calculadas.append(
            AssetMetrics(
                ticker=m.ticker,
                observacoes=m.observacoes,
                retorno_periodo=m.retorno_periodo,
                retorno_anualizado=m.retorno_anualizado,
                volatilidade_anualizada=m.volatilidade_anualizada,
                indice_sharpe=m.indice_sharpe,
                maior_queda=m.maior_queda,
            )
        )

    correlacao = None
    # Correlacao exige pelo menos dois ativos: a de um ativo consigo mesmo e 1,
    # que nao informa nada. Devolver uma matriz 1x1 seria ruido com aparencia
    # de resultado.
    if len(aptos) >= 2:
        # `subconjunto` preserva as datas: recortar os ativos aptos montando um
        # dicionario a mao seria exatamente a brecha por onde o desalinhamento
        # voltaria depois de todo esse cuidado.
        nomes, matriz = matriz_correlacao(series.subconjunto(aptos))
        correlacao = CorrelationMatrix(tickers=nomes, matriz=matriz.tolist())

    return PortfolioMetrics(
        inicio=series.inicio,
        fim=series.fim,
        pregoes=len(series),
        taxa_livre_risco=taxa_livre_risco,
        ativos=calculadas,
        correlacao=correlacao,
        sem_historico_suficiente=[t for t in pedidos if t not in aptos],
    )


async def metricas_da_carteira(
    db: AsyncSession,
    portfolio_id: uuid.UUID,
    *,
    taxa_livre_risco: float,
    desde: date_type | None = None,
    ate: date_type | None = None,
) -> PortfolioMetrics:
    """Metricas dos ativos que o usuario tem em carteira.

    So posicoes abertas: nao faz sentido medir o risco de um papel que ele nao
    tem mais. O resultado ja realizado nele esta em /portfolio/summary.
    """
    posicoes = await transaction_service.posicoes(db, portfolio_id)
    abertos = [p.ticker for p in posicoes if not p.esta_zerada]
    return await metricas(db, abertos, taxa_livre_risco=taxa_livre_risco, desde=desde, ate=ate)
