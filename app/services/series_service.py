"""Carregamento de series historicas alinhadas por data."""

from __future__ import annotations

from collections import defaultdict
from datetime import date as date_type
from decimal import Decimal

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset, PriceHistory
from app.services.metrics import para_float


async def carregar_series(
    db: AsyncSession,
    tickers: list[str],
    *,
    desde: date_type | None = None,
    ate: date_type | None = None,
) -> tuple[list[date_type], dict[str, np.ndarray]]:
    """Devolve (datas comuns, {ticker: precos}) -- todas as series do mesmo tamanho.

    ## O alinhamento e o ponto deste modulo

    Ativos tem historicos diferentes: um IPO recente tem 3 meses, a Petrobras tem
    decadas; um papel nao negocia num dia de leilao; um ticker pode ter sido
    suspenso por uma semana.

    Correlacionar series de tamanhos diferentes -- ou de mesmo tamanho mas datas
    diferentes -- produz um numero com a forma certa e o significado errado. E o
    erro mais silencioso desta area inteira: nada estoura, nada avisa, e a
    matriz de correlacao simplesmente descreve uma realidade que nao existe.
    Pior, esse numero vira peso de carteira na Etapa 9.

    A solucao adotada e a **intersecao das datas**: so entram os dias em que
    TODOS os ativos pedidos negociaram. O custo e perder alguns dias; o
    beneficio e que cada linha compara o mesmo dia em todos os ativos.

    Uma consulta so para todos os tickers -- nao uma por ativo. Com 30 papeis e
    um ano de historico, sao 7.500 linhas: trivial numa consulta, e N+1 em trinta.
    """
    if not tickers:
        return [], {}

    stmt = (
        select(Asset.ticker, PriceHistory.date, PriceHistory.close)
        .join(PriceHistory, PriceHistory.asset_id == Asset.id)
        .where(Asset.ticker.in_(tickers))
    )
    if desde is not None:
        stmt = stmt.where(PriceHistory.date >= desde)
    if ate is not None:
        stmt = stmt.where(PriceHistory.date <= ate)

    por_ticker: dict[str, dict[date_type, Decimal]] = defaultdict(dict)
    for ticker, dia, fechamento in (await db.execute(stmt)).all():
        por_ticker[ticker][dia] = fechamento

    if not por_ticker:
        return [], {}

    datas_comuns = set.intersection(*(set(d) for d in por_ticker.values()))
    if not datas_comuns:
        return [], {}

    # Ordem cronologica e obrigatoria: retorno diario e P_t / P_{t-1}. Com as
    # datas fora de ordem o calculo roda e devolve ruido.
    datas = sorted(datas_comuns)
    series = {t: para_float([por_ticker[t][d] for d in datas]) for t in por_ticker}
    return datas, series
