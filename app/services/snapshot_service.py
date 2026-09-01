"""Gravacao e leitura dos snapshots diarios."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from datetime import date as date_type
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.clients.base import ProvedorDeCotacoes
from app.models.asset import Asset, PriceHistory
from app.models.portfolio import Portfolio
from app.models.snapshot import PortfolioSnapshot
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.snapshot import SnapshotRunResult
from app.services import quote_service, split, split_service, transaction_service
from app.services.position import Posicao, calcular_posicoes

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

    # Percorre CARTEIRAS de usuarios ativos -- inclusive as simuladas. Uma
    # simulacao so tem valor se acompanhar o mercado junto com a real; congelar
    # o historico dela a tornaria inutil como comparacao.
    carteiras = list(
        (await db.execute(select(Portfolio).join(User).where(User.is_active.is_(True))))
        .scalars()
        .all()
    )
    if not carteiras:
        return SnapshotRunResult(
            date=hoje, usuarios_processados=0, snapshots_gravados=0, tickers_consultados=0
        )

    por_carteira: list[tuple[Portfolio, list[Posicao], Decimal]] = []
    tickers: set[str] = set()
    for carteira in carteiras:
        # UMA leitura do livro por carteira. Chamar `posicoes()` duas vezes --
        # uma para as abertas, outra para somar o realizado -- dobraria a
        # consulta ao banco para obter o mesmo dado.
        todas = await transaction_service.posicoes(db, carteira.id)
        abertas = [p for p in todas if not p.esta_zerada]
        # O realizado soma TODAS as posicoes, inclusive as zeradas: o lucro de um
        # papel ja vendido continua sendo dinheiro que o usuario ganhou.
        realizado = sum((p.resultado_realizado for p in todas), ZERO)
        por_carteira.append((carteira, abertas, realizado))
        tickers.update(p.ticker for p in abertas)

    cotacoes = await quote_service.cotacoes_atuais(
        db, provedor, sorted(tickers), ttl_segundos=ttl_segundos
    )

    linhas = []
    for carteira, abertas, realizado in por_carteira:
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
                "portfolio_id": carteira.id,
                "user_id": carteira.user_id,
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
        usuarios_processados=len(carteiras),
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
            index_elements=[PortfolioSnapshot.portfolio_id, PortfolioSnapshot.date],
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
    portfolio_id: uuid.UUID,
    *,
    desde: date_type | None = None,
    ate: date_type | None = None,
    limit: int,
) -> list[PortfolioSnapshot]:
    """Historico do usuario, do mais recente para o mais antigo."""
    stmt = select(PortfolioSnapshot).where(PortfolioSnapshot.portfolio_id == portfolio_id)
    if desde is not None:
        stmt = stmt.where(PortfolioSnapshot.date >= desde)
    if ate is not None:
        stmt = stmt.where(PortfolioSnapshot.date <= ate)
    stmt = stmt.order_by(PortfolioSnapshot.date.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def backfill(
    db: AsyncSession, carteira: Portfolio, *, desde: date_type, ate: date_type | None = None
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
    todas = await transaction_service.posicoes(db, carteira.id)
    if not todas:
        return 0

    transacoes = (
        (
            await db.execute(
                select(Transaction)
                .where(Transaction.portfolio_id == carteira.id)
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

    # Eventos corporativos do periodo, aplicados ao livro antes de qualquer
    # conta.
    #
    # ## A premissa, dita em voz alta
    #
    # O ajuste poe a quantidade em termos de HOJE, e a serie de `price_history`
    # precisa estar na mesma unidade -- ou seja, com os fechamentos historicos
    # tambem restados pelo desdobramento, que e o que "preco ajustado" significa
    # e o que todo fornecedor de dado faz por padrao.
    #
    # Se a serie fosse crua, quantidade de hoje vezes preco de ontem erraria
    # pelo fator do evento. Vale conferir a convencao da origem antes de
    # confiar num historico que atravesse um desdobramento grande.
    eventos = await split_service.dos_ativos(db, {t.asset_id for t in transacoes})
    ajustadas = split.ajustar(transacoes, eventos)

    for dia in dias:
        # Recalcula a posicao com o livro ATE aquele dia, nao o livro inteiro.
        ate_o_dia = [t for t in ajustadas if t.traded_at <= dia]
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
                "portfolio_id": carteira.id,
                "user_id": carteira.user_id,
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


async def reconstruir_desde(db: AsyncSession, carteira: Portfolio, dia: date_type) -> int:
    """Invalida e refaz o historico a partir de um dia.

    ## Por que isso precisa existir

    Snapshot e um fato historico -- mas um fato historico sobre uma carteira que
    o usuario pode corrigir depois. Se ele apaga uma compra lancada por engano,
    ou registra uma operacao antiga que tinha esquecido, todos os snapshots
    daquele dia em diante passam a descrever uma carteira que nunca existiu.

    Aconteceu de verdade: apagar as transacoes de exemplo deixou o grafico de
    evolucao "travado" mostrando um patrimonio que nao correspondia mais a
    nenhuma operacao no livro. O grafico mentia, e nada no sistema percebia.

    Apagamos em vez de recalcular em cima: se a carteira ficou vazia num periodo,
    o certo e nao haver ponto nenhum, e um UPDATE nunca removeria linhas.
    """
    # `CursorResult` expoe rowcount; o `Result` generico do stub nao. O cast
    # documenta que um DELETE sempre devolve o primeiro.
    resultado = cast(
        CursorResult[Any],
        await db.execute(
            delete(PortfolioSnapshot).where(
                PortfolioSnapshot.portfolio_id == carteira.id, PortfolioSnapshot.date >= dia
            )
        ),
    )
    apagados = resultado.rowcount or 0
    await db.commit()

    refeitos = await backfill(db, carteira, desde=dia)
    logger.info(
        "[snapshots] historico refeito para a carteira %s desde %s: %d apagados, %d recriados",
        carteira.id,
        dia,
        apagados,
        refeitos,
    )
    return refeitos
