"""Carga inicial do catalogo de ativos e do historico de fechamentos.

Le os CSVs produzidos pela pipeline do projeto `mercado_financeiro`
(https://github.com/timmtimm1/mercado_financeiro) e os carrega no banco.

    uv run python -m scripts.seed_b3 --origem ~/Projects/mercado_financeiro

Por que carregar historico em vez de comecar com o banco vazio: o calculo de
volatilidade e de covariancia (Etapas 8 e 9) precisa de uma serie de retornos.
Sem historico, a API sobe, responde 200 e devolve metricas sem sentido -- ou um
erro -- ate acumular meses de dados. Com a carga, ela e util no primeiro minuto.

O script e **idempotente**: rodar duas vezes nao duplica nada e nao falha. Isso
nao e um detalhe -- um seed que so pode rodar uma vez e um seed que ninguem se
atreve a rodar.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections.abc import Iterator
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import dispose_engine, get_sessionmaker
from app.models.asset import Asset, PriceHistory
from app.services.b3 import classificar, normalizar_ticker, ticker_valido

# Insercao em lotes, nao linha a linha.
#
# 37 mil `session.add()` seguidos de um commit sao 37 mil INSERTs individuais --
# cada um com sua ida e volta ao banco. Leva minutos. Um INSERT com 5 mil linhas
# de uma vez leva milissegundos. E a mesma logica do N+1 de leitura, do lado da
# escrita: o custo esta no numero de viagens, nao no volume de dados.
LOTE = 5_000


def _ler_csv(caminho: Path) -> Iterator[dict[str, str]]:
    with caminho.open(encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def _decimal_ou_none(valor: str) -> Decimal | None:
    try:
        return Decimal(valor)
    except (InvalidOperation, ValueError):
        return None


async def importar_ativos(db: AsyncSession, origem: Path) -> int:
    """Insere ou atualiza o catalogo a partir de fundamentals_latest.csv."""
    fundamentals = origem / "data" / "fundamentals_latest.csv"
    if not fundamentals.exists():
        raise FileNotFoundError(f"nao encontrei {fundamentals}")

    linhas: dict[str, dict[str, Any]] = {}
    for linha in _ler_csv(fundamentals):
        ticker = normalizar_ticker(linha["ticker"])
        if not ticker_valido(ticker):
            print(f"  [aviso] ticker fora do formato da B3, pulando: {ticker}")
            continue
        setor = (linha.get("setor") or "").strip() or None
        linhas[ticker] = {
            "ticker": ticker,
            "nome": (linha.get("nome") or "").strip()[:120] or None,
            "setor": setor,
            "tipo": classificar(ticker, setor),
        }

    if not linhas:
        return 0

    # ON CONFLICT DO UPDATE (upsert): o `ticker` e a chave natural. Rodar de novo
    # atualiza nome e setor em vez de estourar violacao de unicidade -- e o que
    # torna o script seguro de repetir depois que a pipeline atualizar os dados.
    stmt = insert(Asset).values(list(linhas.values()))
    stmt = stmt.on_conflict_do_update(
        index_elements=[Asset.ticker],
        set_={"nome": stmt.excluded.nome, "setor": stmt.excluded.setor, "tipo": stmt.excluded.tipo},
    )
    await db.execute(stmt)
    await db.commit()
    return len(linhas)


async def importar_cotacoes(db: AsyncSession, origem: Path) -> tuple[int, int]:
    """Carrega quotes_history.csv. Devolve (inseridas, ignoradas)."""
    quotes = origem / "data" / "quotes_history.csv"
    if not quotes.exists():
        raise FileNotFoundError(f"nao encontrei {quotes}")

    # Um unico SELECT resolve ticker -> id para todos os ativos.
    #
    # A alternativa ingenua -- consultar o Asset a cada linha do CSV -- seriam
    # 37 mil SELECTs. E o N+1 classico, e a forma mais comum de um endpoint que
    # "funciona" levar 40 segundos.
    ids: dict[str, Any] = {
        t: i for t, i in (await db.execute(select(Asset.ticker, Asset.id))).all()
    }

    lote: list[dict[str, Any]] = []
    inseridas = ignoradas = 0

    async def descarregar() -> None:
        nonlocal inseridas
        if not lote:
            return
        # DO NOTHING, nao DO UPDATE: fechamento de um dia passado nao muda. Se
        # a linha ja existe, e a mesma -- reescrever seria trabalho sem efeito.
        stmt = (
            insert(PriceHistory)
            .values(lote)
            .on_conflict_do_nothing(index_elements=[PriceHistory.asset_id, PriceHistory.date])
        )
        resultado = await db.execute(stmt)
        await db.commit()
        inseridas += resultado.rowcount or 0
        lote.clear()

    for linha in _ler_csv(quotes):
        ticker = normalizar_ticker(linha["ticker"])
        asset_id = ids.get(ticker)
        fechamento = _decimal_ou_none(linha["fechamento"])
        if asset_id is None or fechamento is None:
            ignoradas += 1
            continue

        volume = linha.get("volume") or ""
        lote.append(
            {
                "asset_id": asset_id,
                "date": date.fromisoformat(linha["data"][:10]),
                "close": fechamento,
                "volume": int(float(volume)) if volume else None,
            }
        )
        if len(lote) >= LOTE:
            await descarregar()

    await descarregar()
    return inseridas, ignoradas


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--origem",
        type=Path,
        default=Path.home() / "Projects" / "mercado_financeiro",
        help="raiz do repositorio mercado_financeiro",
    )
    args = parser.parse_args()
    origem = args.origem.expanduser()

    async with get_sessionmaker()() as db:
        print(f"[seed] lendo de {origem}")
        ativos = await importar_ativos(db, origem)
        print(f"[seed] {ativos} ativos no catalogo")
        inseridas, ignoradas = await importar_cotacoes(db, origem)
        print(f"[seed] {inseridas} cotacoes inseridas, {ignoradas} ignoradas")

    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
