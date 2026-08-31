"""Cotacao atual com cache -- a camada que protege a cota do fornecedor."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.base import Cotacao, ProvedorDeCotacoes
from app.models.asset import Asset
from app.models.quote import PriceQuote


@dataclass(frozen=True)
class CotacaoAtual:
    preco: Decimal
    obtida_em: datetime
    fonte: str
    do_cache: bool

    def idade_segundos(self, agora: datetime | None = None) -> int:
        return int(((agora or datetime.now(UTC)) - self.obtida_em).total_seconds())


async def cotacoes_atuais(
    db: AsyncSession,
    provedor: ProvedorDeCotacoes,
    tickers: list[str],
    *,
    ttl_segundos: int,
) -> dict[str, CotacaoAtual]:
    """Devolve a cotacao de cada ticker, buscando so o que estiver vencido.

    O fluxo, e o motivo de cada passo:

    1. Le o cache inteiro numa consulta. Nao uma por ticker -- isso seria N+1
       contra o proprio banco.
    2. Separa o que ainda esta dentro do TTL. Esses nao viram requisicao externa
       nenhuma: com 15 mil chamadas por mes no plano gratuito, cada chamada
       evitada conta.
    3. Busca os vencidos EM LOTE, uma vez so.
    4. Grava o que veio, com upsert.
    5. **Para o que o fornecedor nao respondeu, devolve o valor vencido do
       cache.** Preco de dez minutos atras e melhor que nenhum preco -- e a
       resposta informa a idade, entao quem consome decide se aceita. Uma
       carteira que aparece zerada porque a brapi caiu seria um defeito muito
       pior que um numero levemente defasado.
    """
    if not tickers:
        return {}

    ativos = {
        t: i
        for t, i in (
            await db.execute(select(Asset.ticker, Asset.id).where(Asset.ticker.in_(tickers)))
        ).all()
    }
    if not ativos:
        return {}

    cache = {
        linha.asset_id: linha
        for linha in (
            await db.execute(
                select(PriceQuote).where(PriceQuote.asset_id.in_(list(ativos.values())))
            )
        )
        .scalars()
        .all()
    }

    agora = datetime.now(UTC)
    limite = agora - timedelta(seconds=ttl_segundos)

    resultado: dict[str, CotacaoAtual] = {}
    vencidos: list[str] = []

    for ticker, asset_id in ativos.items():
        linha = cache.get(asset_id)
        if linha is not None and linha.fetched_at > limite:
            resultado[ticker] = CotacaoAtual(linha.price, linha.fetched_at, linha.source, True)
        else:
            vencidos.append(ticker)

    if not vencidos:
        return resultado

    frescas = await provedor.cotacoes(vencidos)

    if frescas:
        await _gravar(db, {ativos[t]: c for t, c in frescas.items() if t in ativos}, agora)

    for ticker in vencidos:
        nova = frescas.get(ticker)
        if nova is not None:
            resultado[ticker] = CotacaoAtual(nova.preco, agora, nova.fonte, False)
            continue
        # Fornecedor nao respondeu: serve o cache vencido, se houver.
        antiga = cache.get(ativos[ticker])
        if antiga is not None:
            resultado[ticker] = CotacaoAtual(antiga.price, antiga.fetched_at, antiga.source, True)

    return resultado


async def _gravar(db: AsyncSession, cotacoes: dict[uuid.UUID, Cotacao], agora: datetime) -> None:
    """Upsert em lote.

    ON CONFLICT DO UPDATE porque a chave e o ativo: existe no maximo uma cotacao
    corrente por papel. E em lote porque um UPDATE por ticker seria uma ida ao
    banco por linha -- o mesmo N+1, agora na escrita.
    """
    valores = [
        {
            "asset_id": asset_id,
            "price": cotacao.preco,
            "fetched_at": agora,
            "source": cotacao.fonte,
        }
        for asset_id, cotacao in cotacoes.items()
    ]
    if not valores:
        return

    stmt = insert(PriceQuote).values(valores)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[PriceQuote.asset_id],
            set_={
                "price": stmt.excluded.price,
                "fetched_at": stmt.excluded.fetched_at,
                "source": stmt.excluded.source,
            },
        )
    )
    await db.commit()
