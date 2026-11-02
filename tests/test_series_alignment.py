"""Testes do alinhamento de series -- a armadilha mais silenciosa da area."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset, PriceHistory
from app.services.metrics import (
    SeriesAlinhadas,
    alinhar_com_indice,
    covariancia_com_indice,
    matriz_covariancia,
)
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


class TestAlinhamentoComIndice:
    """O Ibovespa chega como variacao diaria e precisa casar com os precos.

    O beta de cada ativo sai de `cov(ativo, indice)`; se o retorno do indice
    de um dia for comparado com o retorno do ativo de outro, o beta mede ruido
    -- e com ele o retorno de equilibrio inteiro.
    """

    @staticmethod
    def _series(precos: dict[str, list[float]], datas: tuple[date, ...]) -> SeriesAlinhadas:
        return SeriesAlinhadas(datas=datas, precos={t: np.array(p) for t, p in precos.items()})

    def test_o_nivel_acumula_as_variacoes(self) -> None:
        datas = _datas(4)
        series = self._series({"A": [10, 11, 12, 13]}, datas)

        resultado = alinhar_com_indice(series, dict.fromkeys(datas, 0.10))

        assert resultado is not None
        _, nivel = resultado
        assert nivel == pytest.approx([1.1, 1.21, 1.331, 1.4641])

    def test_dia_em_que_o_ativo_nao_negociou_entra_composto_no_indice(self) -> None:
        """O ativo pulou o dia 1; o indice andou nele. O retorno do indice entre
        os dias 0 e 2 tem que incluir o dia 1 (1,1 x 1,1 - 1 = 21%), senao o
        ativo estaria sendo comparado com metade do movimento do mercado."""
        todas = _datas(4)
        series = self._series({"A": [10, 12, 13]}, (todas[0], todas[2], todas[3]))

        resultado = alinhar_com_indice(series, dict.fromkeys(todas, 0.10))

        assert resultado is not None
        _, nivel = resultado
        assert nivel[1] / nivel[0] - 1 == pytest.approx(0.21)

    def test_dia_sem_indice_sai_da_amostra_dos_dois_lados(self) -> None:
        datas = _datas(40)
        series = self._series({"A": list(np.linspace(10, 20, 40))}, datas)
        variacoes = {d: 0.001 for d in datas if d != datas[20]}

        resultado = alinhar_com_indice(series, variacoes)

        assert resultado is not None
        recortadas, nivel = resultado
        assert datas[20] not in recortadas.datas
        assert len(recortadas) == len(nivel) == 39

    def test_cobertura_insuficiente_devolve_none_em_vez_de_beta_torto(self) -> None:
        datas = _datas(40)
        series = self._series({"A": list(np.linspace(10, 20, 40))}, datas)

        assert alinhar_com_indice(series, {d: 0.001 for d in datas[:20]}) is None
        assert alinhar_com_indice(series, {}) is None

    def test_variacao_fora_da_janela_nao_entra(self) -> None:
        datas = _datas(5)
        series = self._series({"A": [10, 11, 12, 13, 14]}, datas)
        variacoes = dict.fromkeys(datas, 0.0)
        variacoes[datas[0] - timedelta(days=1)] = 0.50

        resultado = alinhar_com_indice(series, variacoes)

        assert resultado is not None
        assert resultado[1] == pytest.approx([1.0] * 5)

    def test_indice_que_e_carteira_dos_ativos_tem_covariancia_sigma_w(self) -> None:
        """Se o indice rende exatamente 60/40 dos dois ativos todo dia, a
        covariancia de cada ativo com ele e Sigma w -- a identidade que liga a
        conta pelo indice a formula classica de Black-Litterman."""
        rng = np.random.default_rng(7)
        datas = _datas(120)
        retornos = rng.normal(0.0005, 0.015, (119, 2))
        precos = 100 * np.vstack([np.ones(2), np.cumprod(1 + retornos, axis=0)])
        series = self._series({"A": list(precos[:, 0]), "B": list(precos[:, 1])}, datas)
        pesos = np.array([0.6, 0.4])
        variacoes = {datas[0]: 0.0} | {
            d: float(r @ pesos) for d, r in zip(datas[1:], retornos, strict=True)
        }

        resultado = alinhar_com_indice(series, variacoes)
        assert resultado is not None
        tickers, sigma, cov_mercado, var_mercado = covariancia_com_indice(*resultado)

        assert tickers == ["A", "B"]
        assert sigma == pytest.approx(matriz_covariancia(resultado[0])[1])
        assert cov_mercado == pytest.approx(sigma @ pesos)
        assert var_mercado == pytest.approx(float(pesos @ sigma @ pesos))

    def test_indice_de_tamanho_errado_e_recusado(self) -> None:
        datas = _datas(5)
        series = self._series({"A": [10, 11, 12, 13, 14]}, datas)

        with pytest.raises(ValueError, match="desalinhado"):
            covariancia_com_indice(series, np.ones(4))
