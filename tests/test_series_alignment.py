"""Testes do alinhamento de series -- a armadilha mais silenciosa da area."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset, PriceHistory
from app.services.series_service import carregar_series
from tests.factories import criar_ativo


async def _pontos(db: AsyncSession, ativo: Asset, dias: list[int], preco: str = "10.00") -> None:
    """Grava fechamentos nos dias indicados (offset a partir de 2026-01-01)."""
    base = date(2026, 1, 1)
    for i, dia in enumerate(dias):
        db.add(
            PriceHistory(
                asset_id=ativo.id,
                date=base + timedelta(days=dia),
                close=Decimal(preco) + Decimal(i),
            )
        )
    await db.commit()


async def test_intersecao_das_datas(db: AsyncSession) -> None:
    """So entram os dias em que TODOS os ativos negociaram.

    Sem isso, a linha 5 de uma serie seria comparada com a linha 5 de outra
    mesmo sendo dias diferentes -- e a correlacao resultante descreveria uma
    realidade que nao existe. Nada estoura; o numero so fica errado.
    """
    a = await criar_ativo(db, ticker="AAAA3")
    b = await criar_ativo(db, ticker="BBBB3")
    await _pontos(db, a, [1, 2, 3, 4, 5])
    await _pontos(db, b, [3, 4, 5, 6, 7])

    datas, series = await carregar_series(db, ["AAAA3", "BBBB3"])

    assert len(datas) == 3  # dias 3, 4 e 5
    assert len(series["AAAA3"]) == len(series["BBBB3"]) == 3


async def test_todas_as_series_tem_o_mesmo_tamanho(db: AsyncSession) -> None:
    a = await criar_ativo(db, ticker="AAAA3")
    b = await criar_ativo(db, ticker="BBBB3")
    c = await criar_ativo(db, ticker="CCCC3")
    await _pontos(db, a, list(range(1, 21)))
    await _pontos(db, b, list(range(1, 11)))
    await _pontos(db, c, list(range(5, 31)))

    _, series = await carregar_series(db, ["AAAA3", "BBBB3", "CCCC3"])

    assert len({len(s) for s in series.values()}) == 1


async def test_datas_saem_em_ordem_cronologica(db: AsyncSession) -> None:
    """Retorno diario e P_t / P_{t-1}: com as datas fora de ordem o calculo roda
    e devolve ruido."""
    a = await criar_ativo(db, ticker="AAAA3")
    await _pontos(db, a, [5, 1, 3, 2, 4])

    datas, _ = await carregar_series(db, ["AAAA3"])
    assert datas == sorted(datas)


async def test_sem_dias_em_comum_devolve_vazio(db: AsyncSession) -> None:
    """Melhor devolver nada do que uma correlacao construida sobre zero
    observacoes coincidentes."""
    a = await criar_ativo(db, ticker="AAAA3")
    b = await criar_ativo(db, ticker="BBBB3")
    await _pontos(db, a, [1, 2, 3])
    await _pontos(db, b, [10, 11, 12])

    datas, series = await carregar_series(db, ["AAAA3", "BBBB3"])
    assert datas == [] and series == {}


async def test_filtro_por_janela(db: AsyncSession) -> None:
    a = await criar_ativo(db, ticker="AAAA3")
    await _pontos(db, a, list(range(0, 30)))

    datas, _ = await carregar_series(db, ["AAAA3"], desde=date(2026, 1, 10), ate=date(2026, 1, 20))
    assert datas[0] == date(2026, 1, 10)
    assert datas[-1] == date(2026, 1, 20)


async def test_ticker_inexistente_e_ignorado(db: AsyncSession) -> None:
    a = await criar_ativo(db, ticker="AAAA3")
    await _pontos(db, a, list(range(1, 30)))

    _, series = await carregar_series(db, ["AAAA3", "XXXX9"])
    assert set(series) == {"AAAA3"}


async def test_lista_vazia_nao_consulta_o_banco(
    db: AsyncSession, contar_queries: list[str]
) -> None:
    contar_queries.clear()
    datas, series = await carregar_series(db, [])
    assert datas == [] and series == {}
    assert contar_queries == []
