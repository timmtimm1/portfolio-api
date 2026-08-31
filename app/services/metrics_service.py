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
    datas, series = await series_service.carregar_series(db, pedidos, desde=desde, ate=ate)

    if not series or len(datas) <= MINIMO_OBSERVACOES:
        return PortfolioMetrics(
            inicio=datas[0] if datas else None,
            fim=datas[-1] if datas else None,
            pregoes=len(datas),
            taxa_livre_risco=taxa_livre_risco,
            ativos=[],
            correlacao=None,
            sem_historico_suficiente=pedidos,
        )

    calculadas: list[AssetMetrics] = []
    aptos: dict[str, object] = {}

    for ticker in sorted(series):
        m = metricas_do_ativo(ticker, series[ticker], taxa_livre_risco)
        if m is None:
            continue
        aptos[ticker] = series[ticker]
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
        nomes, matriz = matriz_correlacao(aptos)  # type: ignore[arg-type]
        correlacao = CorrelationMatrix(tickers=nomes, matriz=matriz.tolist())

    return PortfolioMetrics(
        inicio=datas[0],
        fim=datas[-1],
        pregoes=len(datas),
        taxa_livre_risco=taxa_livre_risco,
        ativos=calculadas,
        correlacao=correlacao,
        sem_historico_suficiente=[t for t in pedidos if t not in aptos],
    )


async def metricas_da_carteira(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    taxa_livre_risco: float,
    desde: date_type | None = None,
    ate: date_type | None = None,
) -> PortfolioMetrics:
    """Metricas dos ativos que o usuario tem em carteira.

    So posicoes abertas: nao faz sentido medir o risco de um papel que ele nao
    tem mais. O resultado ja realizado nele esta em /portfolio/summary.
    """
    posicoes = await transaction_service.posicoes(db, user_id)
    abertos = [p.ticker for p in posicoes if not p.esta_zerada]
    return await metricas(db, abertos, taxa_livre_risco=taxa_livre_risco, desde=desde, ate=ate)
