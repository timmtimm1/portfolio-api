"""Reconstroi o historico de snapshots a partir dos fechamentos ja no banco.

    uv run python -m scripts.backfill_snapshots --email voce@exemplo.com --desde 2026-01-01

Util depois de cadastrar transacoes antigas: em vez de esperar meses para o
grafico de evolucao ganhar forma, o historico e reconstruido de uma vez.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date

from sqlalchemy import select

from app.core.db import dispose_engine, get_sessionmaker
from app.models.portfolio import Portfolio
from app.models.user import User
from app.services import snapshot_service


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--desde", type=date.fromisoformat, default=date(2020, 1, 1))
    parser.add_argument("--ate", type=date.fromisoformat, default=None)
    args = parser.parse_args()

    async with get_sessionmaker()() as db:
        usuario = (
            await db.execute(select(User).where(User.email == args.email.strip().lower()))
        ).scalar_one_or_none()
        if usuario is None:
            print(f"[backfill] usuario {args.email} nao encontrado", file=sys.stderr)
            return 1

        carteiras = (
            (await db.execute(select(Portfolio).where(Portfolio.user_id == usuario.id)))
            .scalars()
            .all()
        )
        if not carteiras:
            print(f"[backfill] {args.email} nao tem carteiras", file=sys.stderr)
            return 1

        total = 0
        for carteira in carteiras:
            gravados = await snapshot_service.backfill(db, carteira, desde=args.desde, ate=args.ate)
            total += gravados
            print(f"[backfill] {carteira.nome}: {gravados} snapshots")
        print(f"[backfill] {total} no total para {args.email}")

    await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
