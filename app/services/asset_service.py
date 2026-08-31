"""Consultas ao catalogo de ativos."""

from __future__ import annotations

from datetime import date as date_type

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset, AssetType, PriceHistory


def _filtrar(
    stmt: Select[tuple[Asset]], busca: str | None, tipo: AssetType | None, setor: str | None
) -> Select[tuple[Asset]]:
    """Aplica os filtros a uma consulta ja iniciada.

    Fica separado para que a contagem e a listagem usem exatamente os mesmos
    filtros. Duplicar as condicoes nos dois lugares e como o `total` de uma
    paginacao passa a nao corresponder aos itens devolvidos.
    """
    if busca:
        # `ilike` com parametro vinculado -- a string do usuario nunca e
        # concatenada no SQL. Interpolar aqui (f"... LIKE '%{busca}%'") seria
        # injecao de SQL de manual.
        padrao = f"%{busca.strip()}%"
        stmt = stmt.where(or_(Asset.ticker.ilike(padrao), Asset.nome.ilike(padrao)))
    if tipo is not None:
        stmt = stmt.where(Asset.tipo == tipo)
    if setor:
        stmt = stmt.where(Asset.setor == setor)
    return stmt


async def listar(
    db: AsyncSession,
    *,
    busca: str | None = None,
    tipo: AssetType | None = None,
    setor: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[Asset], int]:
    """Devolve (pagina, total).

    Duas consultas: uma conta, outra pagina. A alternativa -- trazer tudo e
    contar em Python com `len()` -- e exatamente o que se quer evitar: derrota o
    proposito da paginacao, porque o banco materializa a tabela inteira mesmo
    assim.
    """
    base = _filtrar(select(Asset), busca, tipo, setor)

    total = await db.scalar(
        select(func.count()).select_from(_filtrar(select(Asset.id), busca, tipo, setor).subquery())
    )
    # `order_by` explicito: sem ordenacao, o Postgres nao garante ordem estavel
    # entre consultas -- a pagina 2 poderia repetir ou pular linhas da pagina 1.
    itens = (
        (await db.execute(base.order_by(Asset.ticker).limit(limit).offset(offset))).scalars().all()
    )
    return list(itens), int(total or 0)


async def buscar_por_ticker(db: AsyncSession, ticker: str) -> Asset | None:
    return (
        await db.execute(select(Asset).where(Asset.ticker == ticker.strip().upper()))
    ).scalar_one_or_none()


async def historico(
    db: AsyncSession, asset_id: object, *, desde: date_type | None = None, limit: int
) -> list[PriceHistory]:
    """Fechamentos mais recentes primeiro.

    Serve o indice da chave primaria composta (asset_id, date) -- por isso e uma
    varredura curta e ordenada, nao um sort de tabela inteira.
    """
    stmt = select(PriceHistory).where(PriceHistory.asset_id == asset_id)
    if desde is not None:
        stmt = stmt.where(PriceHistory.date >= desde)
    stmt = stmt.order_by(PriceHistory.date.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())
