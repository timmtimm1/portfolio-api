"""Gravacao e leitura dos snapshots diarios."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clients.base import ProvedorDeCotacoes
from app.models.asset import Asset, PriceHistory
from app.models.snapshot import PortfolioSnapshot
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.snapshot import SnapshotRunResult
from app.services import quote_service, transaction_service
from app.services.position import calcular_posicoes

logger = logging.getLogger(__name__)
ZERO = Decimal(0)


async def gravar_de_todos(
    db: AsyncSession,
    provedor: ProvedorDeCotacoes,
    *,
    ttl_segundos: int,
    dia: date_type | None = None,
) -> SnapshotRunResult:
    """Fotografa a carteira de todos os usuarios ativos.

    ## A ordem das operacoes e o ponto

    Primeiro levantamos TODOS os tickers de TODAS as carteiras, buscamos as
    cotacoes numa unica passada, e so entao calculamos usuario por usuario.

    A alternativa obvia -- para cada usuario, buscar as cotacoes dele -- e um
    N+1 contra a API externa: com 100 usuarios que tem PETR4, seriam 100
    consultas da mesma cotacao. Com a cota gratuita de 15 mil chamadas/mes, esse
    desenho estoura o limite em poucos dias. Aqui, PETR4 e buscado uma vez e
    serve a todos.

    Usuarios inativos ficam de fora: fotografar carteira de conta desativada
    gasta processamento e cota para produzir dado que ninguem vai olhar.
    """
    hoje = dia or datetime.now(UTC).date()

    usuarios = (await db.execute(select(User).where(User.is_active.is_(True)))).scalars().all()
    if not usuarios:
        return SnapshotRunResult(
            date=hoje, usuarios_processados=0, snapshots_gravados=0, tickers_consultados=0
        )

    posicoes_por_usuario = {}
    tickers: set[str] = set()
    for usuario in usuarios:
        abertas = [
            p for p in await transaction_service.posicoes(db, usuario.id) if not p.esta_zerada
        ]
        realizado = sum(
            (p.resultado_realizado for p in await transaction_service.posicoes(db, usuario.id)),
            ZERO,
        )
        posicoes_por_usuario[usuario.id] = (abertas, realizado)
        tickers.update(p.ticker for p in abertas)

    cotacoes = await quote_service.cotacoes_atuais(
        db, provedor, sorted(tickers), ttl_segundos=ttl_segundos
    )

    linhas = []
    for user_id, (abertas, realizado) in posicoes_por_usuario.items():
        if not abertas and realizado == ZERO:
            # Carteira nunca usada: um ponto de valor zero por dia so poluiria o
            # historico de quem ainda nao comecou.
            continue

        custo = valor = ZERO
        sem_cotacao = 0
        for posicao in abertas:
            custo += posicao.custo_total
            cotacao = cotacoes.get(posicao.ticker)
            if cotacao is None:
                sem_cotacao += 1
                valor += posicao.custo_total  # sem preco, entra pelo custo
            else:
                valor += posicao.quantidade * cotacao.preco

        linhas.append(
            {
                "user_id": user_id,
                "date": hoje,
                "custo_total": custo,
                "valor_mercado": valor,
                "resultado_nao_realizado": valor - custo,
                "resultado_realizado": realizado,
                "ativos": len(abertas),
                "ativos_sem_cotacao": sem_cotacao,
            }
        )

    if linhas:
        await _gravar(db, linhas)

    if any(linha["ativos_sem_cotacao"] for linha in linhas):
        logger.warning("[snapshots] %s: houve ativos sem cotacao na foto do dia", hoje)

    return SnapshotRunResult(
        date=hoje,
        usuarios_processados=len(usuarios),
        snapshots_gravados=len(linhas),
        tickers_consultados=len(tickers),
    )


async def _gravar(db: AsyncSession, linhas: list[dict[str, object]]) -> None:
    """Upsert por (user_id, date).

    DO UPDATE, nao DO NOTHING: rodar de novo no mesmo dia deve ATUALIZAR a foto
    com a cotacao mais recente, nao ignorar. Isso torna o job seguro de repetir
    -- por nova tentativa, por disparo manual ou porque o primeiro rodou antes
    do fechamento.
    """
    stmt = insert(PortfolioSnapshot).values(linhas)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[PortfolioSnapshot.user_id, PortfolioSnapshot.date],
            set_={
                c: stmt.excluded[c]
                for c in (
                    "custo_total",
                    "valor_mercado",
                    "resultado_nao_realizado",
                    "resultado_realizado",
                    "ativos",
                    "ativos_sem_cotacao",
                )
            },
        )
    )
    await db.commit()


async def historico(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    desde: date_type | None = None,
    ate: date_type | None = None,
    limit: int,
) -> list[PortfolioSnapshot]:
    """Historico do usuario, do mais recente para o mais antigo."""
    stmt = select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user_id)
    if desde is not None:
        stmt = stmt.where(PortfolioSnapshot.date >= desde)
    if ate is not None:
        stmt = stmt.where(PortfolioSnapshot.date <= ate)
    stmt = stmt.order_by(PortfolioSnapshot.date.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def backfill(
    db: AsyncSession, user_id: uuid.UUID, *, desde: date_type, ate: date_type | None = None
) -> int:
    """Reconstroi snapshots passados a partir do historico de fechamentos.

    Nao contradiz o que esta escrito no model. O snapshot existe porque a cotacao
    ATUAL e sobrescrita no cache -- mas o FECHAMENTO de cada dia esta guardado em
    `price_history`. Onde ha fechamento, a foto daquele dia e reconstruivel; o
    que nao volta e o preco intradiario do momento em que o job rodaria.

    Serve para dar historico a quem acabou de cadastrar transacoes antigas, em vez
    de esperar meses para o grafico ter forma.

    Cada dia usa a posicao vigente NAQUELE dia -- transacoes posteriores nao
    contam. Ignorar isso produziria um grafico em que a carteira sempre teve o
    tamanho de hoje, que e a forma mais convincente de mentir com um grafico.
    """
    todas = await transaction_service.posicoes(db, user_id)
    if not todas:
        return 0

    transacoes = (
        (
            await db.execute(
                select(Transaction)
                .where(Transaction.user_id == user_id)
                .options(selectinload(Transaction.asset))
            )
        )
        .scalars()
        .all()
    )
    if not transacoes:
        return 0

    fim = ate or datetime.now(UTC).date()
    tickers = sorted({t.asset.ticker for t in transacoes})

    fechamentos: dict[tuple[str, date_type], Decimal] = {
        (ticker, dia): preco
        for ticker, dia, preco in (
            await db.execute(
                select(Asset.ticker, PriceHistory.date, PriceHistory.close)
                .join(PriceHistory, PriceHistory.asset_id == Asset.id)
                .where(Asset.ticker.in_(tickers), PriceHistory.date.between(desde, fim))
            )
        ).all()
    }
    if not fechamentos:
        return 0

    dias = sorted({dia for _, dia in fechamentos})
    linhas = []

    for dia in dias:
        # Recalcula a posicao com o livro ATE aquele dia, nao o livro inteiro.
        ate_o_dia = [t for t in transacoes if t.traded_at <= dia]
        if not ate_o_dia:
            continue
        posicoes = calcular_posicoes(ate_o_dia)
        abertas = [p for p in posicoes.values() if not p.esta_zerada]
        if not abertas:
            continue

        custo = valor = ZERO
        sem_cotacao = 0
        for posicao in abertas:
            custo += posicao.custo_total
            preco = fechamentos.get((posicao.ticker, dia))
            if preco is None:
                sem_cotacao += 1
                valor += posicao.custo_total
            else:
                valor += posicao.quantidade * preco

        linhas.append(
            {
                "user_id": user_id,
                "date": dia,
                "custo_total": custo,
                "valor_mercado": valor,
                "resultado_nao_realizado": valor - custo,
                "resultado_realizado": sum(
                    (p.resultado_realizado for p in posicoes.values()), ZERO
                ),
                "ativos": len(abertas),
                "ativos_sem_cotacao": sem_cotacao,
            }
        )

    if linhas:
        await _gravar(db, linhas)
    return len(linhas)
