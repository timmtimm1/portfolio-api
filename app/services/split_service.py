"""Persistencia e sincronizacao de eventos corporativos.

Camada que fala com banco e fornecedor. A matematica do ajuste fica em
`app/services/split.py`, que e puro -- mesma divisao de `dividend.py` /
`dividend_service.py`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.yahoo import YahooClient
from app.models.asset import Asset
from app.models.split import Split

logger = logging.getLogger(__name__)

UM = Decimal(1)


@dataclass(frozen=True)
class EventoComTicker:
    """Adapta a linha do banco ao Protocol `EventoLike` do modulo puro.

    O banco guarda `asset_id`; o ajuste casa por `ticker`. A traducao acontece
    aqui, uma vez, para o modulo puro nao precisar conhecer o ORM.
    """

    ticker: str
    data_ex: date_type
    numerador: Decimal
    denominador: Decimal

    @property
    def fator(self) -> Decimal:
        return self.numerador / self.denominador


async def sincronizar(
    db: AsyncSession,
    cliente: YahooClient,
    tickers: list[str],
    desde: date_type,
    ate: date_type,
) -> int:
    """Busca eventos no fornecedor e grava os que faltam. Devolve quantos
    entraram.

    Uma requisicao por ticker: o endpoint de eventos do Yahoo nao aceita lote.
    Por isso nao roda dentro do request comum -- e disparada explicitamente.
    """
    ids = {
        ticker: asset_id
        for ticker, asset_id in (
            await db.execute(select(Asset.ticker, Asset.id).where(Asset.ticker.in_(tickers)))
        ).all()
    }

    linhas: list[dict[str, object]] = []
    for ticker in tickers:
        asset_id = ids.get(ticker)
        if asset_id is None:
            continue
        for bruto in await cliente.desdobramentos(ticker, desde, ate):
            linhas.append(
                {
                    "asset_id": asset_id,
                    "data_ex": bruto.data_ex,
                    "numerador": bruto.numerador,
                    "denominador": bruto.denominador,
                }
            )

    if not linhas:
        return 0

    stmt = insert(Split).values(linhas)
    # DO NOTHING: evento anunciado nao muda depois. RETURNING para contar so o
    # que realmente entrou -- contar `linhas` diria quantas foram tentadas, e
    # numa segunda execucao isso seria sempre o total.
    inseridas = (
        await db.execute(
            stmt.on_conflict_do_nothing(index_elements=[Split.asset_id, Split.data_ex]).returning(
                Split.asset_id
            )
        )
    ).all()
    await db.commit()
    return len(inseridas)


async def dos_ativos(db: AsyncSession, asset_ids: set[uuid.UUID]) -> list[EventoComTicker]:
    """Eventos dos ativos indicados, prontos para o modulo puro.

    Uma consulta so, com join no catalogo para trazer o ticker junto --
    consultar evento por transacao seria o N+1 classico.
    """
    if not asset_ids:
        return []

    linhas = (
        await db.execute(
            select(Split, Asset.ticker)
            .join(Asset, Asset.id == Split.asset_id)
            .where(Split.asset_id.in_(asset_ids))
            .order_by(Split.data_ex)
        )
    ).all()
    return [EventoComTicker(ticker, s.data_ex, s.numerador, s.denominador) for s, ticker in linhas]
