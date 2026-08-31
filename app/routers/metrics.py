"""Rotas de metricas de risco e retorno."""

from __future__ import annotations

from datetime import date as date_type
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, DbDep, SettingsDep
from app.schemas.metrics import PortfolioMetrics
from app.services import metrics_service

router = APIRouter(tags=["metricas"])

# Teto de ativos por analise. A matriz de correlacao cresce com o QUADRADO do
# numero de ativos: 50 ativos sao 2.500 celulas, 500 seriam 250 mil. O limite
# protege memoria e tempo de resposta -- e nenhuma carteira de pessoa fisica
# passa disso.
MAXIMO_ATIVOS = 50


@router.get(
    "/portfolio/metrics",
    response_model=PortfolioMetrics,
    summary="Risco e retorno da carteira",
)
async def metricas_da_carteira(
    usuario: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    desde: Annotated[date_type | None, Query(description="Inicio da janela (AAAA-MM-DD)")] = None,
    ate: Annotated[date_type | None, Query()] = None,
) -> PortfolioMetrics:
    """Retorno, volatilidade, Sharpe, maior queda e correlacao dos ativos em carteira.

    As series sao alinhadas pela intersecao das datas antes de qualquer calculo:
    correlacionar historicos de tamanhos diferentes produz um numero com a forma
    certa e o significado errado.
    """
    return await metrics_service.metricas_da_carteira(
        db, usuario.id, taxa_livre_risco=settings.RISK_FREE_RATE, desde=desde, ate=ate
    )


@router.get(
    "/metrics",
    response_model=PortfolioMetrics,
    summary="Risco e retorno de ativos avulsos",
)
async def metricas_de_ativos(
    _: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    tickers: Annotated[
        list[str],
        Query(min_length=1, max_length=MAXIMO_ATIVOS, description="Repita o parametro por ativo"),
    ],
    desde: Annotated[date_type | None, Query()] = None,
    ate: Annotated[date_type | None, Query()] = None,
) -> PortfolioMetrics:
    """Mesma analise para ativos que o usuario nao possui -- para avaliar antes
    de comprar. E a rota que o simulador da Etapa 9 vai consumir."""
    return await metrics_service.metricas(
        db, tickers, taxa_livre_risco=settings.RISK_FREE_RATE, desde=desde, ate=ate
    )
