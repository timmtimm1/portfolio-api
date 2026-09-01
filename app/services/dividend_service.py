"""Persistencia e sincronizacao de proventos.

Camada que fala com banco e fornecedor. A regra de negocio -- quem recebeu o
que, e quanto -- fica em `app/services/dividend.py`, que e puro. Mesma divisao
de `optimizer.py` / `optimizer_service.py`.
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
from sqlalchemy.orm import selectinload

from app.clients.yahoo import YahooClient
from app.models.asset import Asset
from app.models.dividend import Dividend, TipoProvento
from app.models.transaction import Transaction
from app.services import dividend, split, split_service
from app.services.dividend import ProventoRecebido

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ProventoComTicker:
    """Adapta a linha do banco ao Protocol `ProventoLike` do modulo puro.

    O banco guarda `asset_id`; o calculo casa por `ticker`. A traducao acontece
    aqui, uma vez, em vez de o modulo puro precisar conhecer o ORM.
    """

    ticker: str
    data_com: date_type
    tipo: TipoProvento
    valor_por_cota: Decimal


async def sincronizar(
    db: AsyncSession,
    cliente: YahooClient,
    tickers: list[str],
    desde: date_type,
    ate: date_type,
) -> int:
    """Busca proventos no fornecedor e grava os que faltam. Devolve quantos
    entraram.

    Uma requisicao por ticker: o endpoint do Yahoo nao aceita lote para eventos.
    Por isso esta funcao NAO e chamada dentro do request do usuario -- ela e
    disparada explicitamente, como o job de snapshot.
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
        for bruto in await cliente.proventos(ticker, desde, ate):
            linhas.append(
                {
                    "asset_id": asset_id,
                    "data_com": bruto.data_com,
                    # INDEFINIDO, nao DIVIDENDO: o Yahoo nao classifica, e chutar
                    # o tipo comum embutiria um erro de 15% sempre que for JCP.
                    "tipo": TipoProvento.INDEFINIDO,
                    "valor_por_cota": bruto.valor_por_cota,
                    "fonte": "yahoo",
                }
            )

    if not linhas:
        return 0

    stmt = insert(Dividend).values(linhas)
    # DO NOTHING, e nao DO UPDATE: provento anunciado nao muda depois. Mais
    # importante, isto protege a correcao MANUAL -- se o usuario reclassificou
    # um provento como JCP, a proxima sincronizacao nao pode desfazer isso.
    #
    # RETURNING para contar: com DO NOTHING, ele devolve APENAS as linhas que
    # realmente entraram. Contar `linhas` diria quantas foram tentadas, e numa
    # sincronizacao repetida isso seria sempre o total, sugerindo trabalho que
    # nao aconteceu.
    inseridas = (
        await db.execute(
            stmt.on_conflict_do_nothing(
                index_elements=[Dividend.asset_id, Dividend.data_com, Dividend.tipo]
            ).returning(Dividend.asset_id)
        )
    ).all()
    await db.commit()
    return len(inseridas)


async def da_carteira(
    db: AsyncSession,
    portfolio_id: uuid.UUID,
    *,
    desde: date_type | None = None,
    ate: date_type | None = None,
) -> list[ProventoRecebido]:
    """Proventos que ESTA carteira recebeu, derivados do livro.

    Duas consultas, nao N: uma traz o livro inteiro, outra traz os proventos dos
    ativos que aparecem nele. O cruzamento acontece em memoria, no modulo puro.
    Consultar provento por transacao seria o N+1 classico.
    """
    transacoes = list(
        (
            await db.execute(
                select(Transaction)
                # `selectinload` NAO e opcional aqui: `Transaction.ticker` e uma
                # property que le `self.asset.ticker`, e o relacionamento esta
                # com `lazy="raise"`. Sem esta linha o endpoint levanta na
                # primeira transacao -- que foi exatamente o que aconteceu na
                # primeira chamada real. O `raise` e proposital: sem ele, o
                # acesso viraria uma query POR TRANSACAO, em silencio.
                .options(selectinload(Transaction.asset))
                .where(Transaction.portfolio_id == portfolio_id)
                .order_by(Transaction.traded_at)
            )
        )
        .scalars()
        .all()
    )
    if not transacoes:
        return []

    ids_ativos = {t.asset_id for t in transacoes}

    stmt = (
        select(Dividend, Asset.ticker)
        .join(Asset, Asset.id == Dividend.asset_id)
        .where(Dividend.asset_id.in_(ids_ativos))
    )
    if desde is not None:
        stmt = stmt.where(Dividend.data_com >= desde)
    if ate is not None:
        stmt = stmt.where(Dividend.data_com <= ate)

    proventos = [
        _ProventoComTicker(ticker, linha.data_com, linha.tipo, linha.valor_por_cota)
        for linha, ticker in (await db.execute(stmt)).all()
    ]

    # O livro vai AJUSTADO por desdobramento. O Yahoo ja ajusta o historico de
    # proventos, entao os dois lados precisam falar a mesma unidade: quantidade
    # ajustada x provento ajustado. Cruzar um lado ajustado com outro cru
    # erraria exatamente pelo fator do evento.
    eventos = await split_service.dos_ativos(db, ids_ativos)
    return dividend.recebidos(split.ajustar(transacoes, eventos), proventos)


async def reclassificar(
    db: AsyncSession, asset_id: uuid.UUID, data_com: date_type, tipo_novo: TipoProvento
) -> bool:
    """Troca o tipo de um provento importado como INDEFINIDO.

    O tipo faz parte da chave primaria, entao "reclassificar" e apagar e
    reinserir -- nao um UPDATE simples. Devolve False se nao havia nada para
    reclassificar.
    """
    linha = await db.get(Dividend, (asset_id, data_com, TipoProvento.INDEFINIDO))
    if linha is None:
        return False

    valor, pagamento = linha.valor_por_cota, linha.data_pagamento
    await db.delete(linha)
    await db.flush()
    db.add(
        Dividend(
            asset_id=asset_id,
            data_com=data_com,
            tipo=tipo_novo,
            valor_por_cota=valor,
            data_pagamento=pagamento,
            # "manual": marca a linha como corrigida por gente, para a proxima
            # sincronizacao automatica nao ter chance de reintroduzir a versao
            # INDEFINIDO por cima.
            fonte="manual",
        )
    )
    await db.commit()
    return True
