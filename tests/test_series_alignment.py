"""Testes do alinhamento de series -- a armadilha mais silenciosa da area."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset, PriceHistory
from app.services.metrics import SeriesAlinhadas
from app.services.series_service import carregar_series
from tests.factories import criar_ativo


def _datas(n: int) -> tuple[date, ...]:
    return tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(n))


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

    series = await carregar_series(db, ["AAAA3", "BBBB3"])

    assert series is not None
    assert len(series) == 3  # dias 3, 4 e 5
    assert len(series.precos["AAAA3"]) == len(series.precos["BBBB3"]) == 3


async def test_todas_as_series_tem_o_mesmo_tamanho(db: AsyncSession) -> None:
    a = await criar_ativo(db, ticker="AAAA3")
    b = await criar_ativo(db, ticker="BBBB3")
    c = await criar_ativo(db, ticker="CCCC3")
    await _pontos(db, a, list(range(1, 21)))
    await _pontos(db, b, list(range(1, 11)))
    await _pontos(db, c, list(range(5, 31)))

    series = await carregar_series(db, ["AAAA3", "BBBB3", "CCCC3"])

    assert series is not None
    assert len({len(s) for s in series.precos.values()}) == 1


async def test_datas_saem_em_ordem_cronologica(db: AsyncSession) -> None:
    """Retorno diario e P_t / P_{t-1}: com as datas fora de ordem o calculo roda
    e devolve ruido."""
    a = await criar_ativo(db, ticker="AAAA3")
    await _pontos(db, a, [5, 1, 3, 2, 4])

    series = await carregar_series(db, ["AAAA3"])
    assert series is not None
    assert list(series.datas) == sorted(series.datas)


async def test_sem_dias_em_comum_devolve_vazio(db: AsyncSession) -> None:
    """Melhor devolver nada do que uma correlacao construida sobre zero
    observacoes coincidentes."""
    a = await criar_ativo(db, ticker="AAAA3")
    b = await criar_ativo(db, ticker="BBBB3")
    await _pontos(db, a, [1, 2, 3])
    await _pontos(db, b, [10, 11, 12])

    assert await carregar_series(db, ["AAAA3", "BBBB3"]) is None


async def test_filtro_por_janela(db: AsyncSession) -> None:
    a = await criar_ativo(db, ticker="AAAA3")
    await _pontos(db, a, list(range(0, 30)))

    series = await carregar_series(db, ["AAAA3"], desde=date(2026, 1, 10), ate=date(2026, 1, 20))
    assert series is not None
    assert series.inicio == date(2026, 1, 10)
    assert series.fim == date(2026, 1, 20)


async def test_ticker_inexistente_e_ignorado(db: AsyncSession) -> None:
    a = await criar_ativo(db, ticker="AAAA3")
    await _pontos(db, a, list(range(1, 30)))

    series = await carregar_series(db, ["AAAA3", "XXXX9"])
    assert series is not None
    assert set(series.precos) == {"AAAA3"}


async def test_lista_vazia_nao_consulta_o_banco(
    db: AsyncSession, contar_queries: list[str]
) -> None:
    contar_queries.clear()
    assert await carregar_series(db, []) is None
    assert contar_queries == []


class TestInvariantesDoTipo:
    """O alinhamento e garantido pela CONSTRUCAO do objeto, nao por convencao.

    Antes, `matriz_correlacao` recebia `dict[str, ndarray]` e a docstring pedia
    series alinhadas. Duas series de mesmo tamanho mas de periodos diferentes
    passavam sem erro e devolviam uma correlacao que era ruido puro -- nada
    estourava. Agora o objeto nem chega a existir.
    """

    def test_series_de_tamanhos_diferentes_e_recusada(self) -> None:
        with pytest.raises(ValueError, match="desalinhada"):
            SeriesAlinhadas(datas=_datas(5), precos={"A": np.ones(5), "B": np.ones(3)})

    def test_mais_precos_que_datas_e_recusado(self) -> None:
        with pytest.raises(ValueError, match="desalinhada"):
            SeriesAlinhadas(datas=_datas(3), precos={"A": np.ones(10)})

    def test_datas_fora_de_ordem_sao_recusadas(self) -> None:
        """Retorno diario e P_t / P_{t-1}: fora de ordem, o calculo roda e
        devolve ruido."""
        with pytest.raises(ValueError, match="fora de ordem"):
            SeriesAlinhadas(datas=(date(2026, 1, 3), date(2026, 1, 1)), precos={"A": np.ones(2)})

    def test_data_repetida_e_recusada(self) -> None:
        """Duas linhas para o mesmo dia significam dado corrompido na origem."""
        with pytest.raises(ValueError, match="fora de ordem ou repetidas"):
            SeriesAlinhadas(datas=(date(2026, 1, 1), date(2026, 1, 1)), precos={"A": np.ones(2)})

    def test_sem_datas_e_recusado(self) -> None:
        with pytest.raises(ValueError, match="pelo menos uma data"):
            SeriesAlinhadas(datas=(), precos={})

    def test_series_validas_sao_aceitas(self) -> None:
        series = SeriesAlinhadas(datas=_datas(5), precos={"A": np.ones(5), "B": np.ones(5)})
        assert len(series) == 5
        assert series.tickers == ["A", "B"]

    def test_tickers_saem_sempre_ordenados(self) -> None:
        """Quem consome a matriz indexa por posicao: ordem instavel trocaria os
        ativos silenciosamente entre duas chamadas."""
        series = SeriesAlinhadas(
            datas=_datas(3), precos={"VALE3": np.ones(3), "ABEV3": np.ones(3), "PETR4": np.ones(3)}
        )
        assert series.tickers == ["ABEV3", "PETR4", "VALE3"]

    def test_subconjunto_preserva_o_alinhamento(self) -> None:
        """Recortar os ativos aptos montando um dicionario a mao seria a brecha
        por onde o desalinhamento voltaria."""
        series = SeriesAlinhadas(
            datas=_datas(4), precos={"A": np.ones(4), "B": np.ones(4), "C": np.ones(4)}
        )
        recorte = series.subconjunto(["A", "C"])
        assert recorte.tickers == ["A", "C"]
        assert recorte.datas == series.datas
        assert len(recorte) == 4

    def test_subconjunto_ignora_ticker_ausente(self) -> None:
        series = SeriesAlinhadas(datas=_datas(3), precos={"A": np.ones(3)})
        assert series.subconjunto(["A", "INEXISTENTE"]).tickers == ["A"]


async def test_carregar_series_devolve_o_tipo_garantido(db: AsyncSession) -> None:
    """A unica porta de entrada do banco para o calculo devolve o tipo com
    invariante -- nao ha caminho que produza series desalinhadas."""
    a = await criar_ativo(db, ticker="AAAA3")
    b = await criar_ativo(db, ticker="BBBB3")
    await _pontos(db, a, list(range(1, 30)))
    await _pontos(db, b, list(range(5, 40)))

    series = await carregar_series(db, ["AAAA3", "BBBB3"])

    assert isinstance(series, SeriesAlinhadas)
    # A invariante ja foi verificada na construcao; reconferimos aqui so para
    # deixar explicito o que o tipo esta prometendo.
    assert all(len(s) == len(series.datas) for s in series.precos.values())
