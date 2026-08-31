"""Testes das metricas de risco.

Puros, com valores conferidos a mao ou construidos para ter resposta exata.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from app.services.metrics import (
    PREGOES_POR_ANO,
    SeriesAlinhadas,
    indice_sharpe,
    maior_queda,
    matriz_correlacao,
    matriz_covariancia,
    metricas_do_ativo,
    retorno_anualizado,
    retornos_diarios,
    volatilidade_anualizada,
)


def alinhadas(**series: np.ndarray) -> SeriesAlinhadas:
    """Monta SeriesAlinhadas a partir de arrays de mesmo tamanho, com datas
    sinteticas consecutivas."""
    n = len(next(iter(series.values())))
    datas = tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(n))
    return SeriesAlinhadas(datas=datas, precos=dict(series))


class TestRetornos:
    def test_retorno_simples(self) -> None:
        precos = np.array([100.0, 110.0, 99.0])
        r = retornos_diarios(precos)
        assert r[0] == pytest.approx(0.10)
        assert r[1] == pytest.approx(-0.10)

    def test_n_precos_geram_n_menos_1_retornos(self) -> None:
        """Obvio, e a origem de um erro comum: usar `len(precos)` em vez de
        `len(retornos)` ao anualizar erra o expoente por um dia."""
        assert len(retornos_diarios(np.arange(1.0, 11.0))) == 9

    def test_anualizacao_e_geometrica_nao_aritmetica(self) -> None:
        """Cai 50%, sobe 50%: media aritmetica = 0%, resultado real = -25%.

        A media aritmetica mente sistematicamente para cima, e mente mais quanto
        mais volatil o ativo. Este teste distingue as duas formulas.
        """
        precos = np.array([100.0, 50.0, 75.0])
        assert float(np.mean(retornos_diarios(precos))) == pytest.approx(0.0)
        assert precos[-1] / precos[0] - 1 == pytest.approx(-0.25)
        assert retorno_anualizado(precos) < -0.9  # -25% em 2 dias, anualizado

    def test_preco_constante_da_retorno_zero(self) -> None:
        assert retorno_anualizado(np.full(100, 42.0)) == pytest.approx(0.0)

    def test_dobrar_em_exatamente_um_ano_da_100_por_cento(self) -> None:
        """252 pregoes = 1 ano, entao 253 precos. Dobrar o valor deve dar
        exatamente +100% anualizado -- e o teste que pega a raiz de 252 trocada
        por multiplicacao, ou o expoente errado por um dia."""
        precos = np.geomspace(100.0, 200.0, PREGOES_POR_ANO + 1)
        assert retorno_anualizado(precos) == pytest.approx(1.0, rel=1e-9)


class TestVolatilidade:
    def test_preco_constante_tem_volatilidade_zero(self) -> None:
        assert volatilidade_anualizada(retornos_diarios(np.full(50, 10.0))) == 0.0

    def test_anualizacao_usa_raiz_de_252(self) -> None:
        """Multiplicar por 252 em vez da raiz infla o numero em ~15,9 vezes --
        um erro que passa despercebido porque o resultado ainda 'parece' um
        percentual."""
        retornos = np.array([0.01, -0.01] * 50)
        diaria = float(np.std(retornos, ddof=1))
        assert volatilidade_anualizada(retornos) == pytest.approx(diaria * np.sqrt(252))

    def test_usa_desvio_amostral_e_nao_populacional(self) -> None:
        """ddof=1, nao ddof=0. Com poucos dados a diferenca e grande, e ddof=0
        subestima o risco de forma sistematica."""
        retornos = np.array([0.02, -0.01, 0.03, -0.02, 0.01])
        populacional = float(np.std(retornos, ddof=0) * np.sqrt(252))
        assert volatilidade_anualizada(retornos) > populacional

    def test_mais_volatil_tem_numero_maior(self) -> None:
        calmo = np.array([0.001, -0.001] * 50)
        agitado = np.array([0.05, -0.05] * 50)
        assert volatilidade_anualizada(agitado) > volatilidade_anualizada(calmo)


class TestSharpe:
    def test_taxa_livre_de_risco_nao_e_zero_no_brasil(self) -> None:
        """Uma acao que rendeu 8% com o CDI a 10% tem Sharpe NEGATIVO: entregou
        menos que o Tesouro Selic assumindo risco de renda variavel. Com rf=0
        (a convencao de exemplos americanos) o mesmo caso viraria positivo."""
        assert indice_sharpe(0.08, 0.20, 0.10) is not None
        sharpe = indice_sharpe(0.08, 0.20, 0.10)
        assert sharpe is not None and sharpe < 0
        sem_rf = indice_sharpe(0.08, 0.20, 0.0)
        assert sem_rf is not None and sem_rf > 0

    def test_volatilidade_zero_devolve_none(self) -> None:
        """Dividir por zero daria infinito, e 'Sharpe infinito' nao significa nada."""
        assert indice_sharpe(0.15, 0.0, 0.10) is None

    def test_valor_conferido_a_mao(self) -> None:
        """(0,25 - 0,10) / 0,20 = 0,75."""
        assert indice_sharpe(0.25, 0.20, 0.10) == pytest.approx(0.75)


class TestMaiorQueda:
    def test_serie_so_de_alta_nao_tem_queda(self) -> None:
        assert maior_queda(np.array([10.0, 11.0, 12.0, 13.0])) == pytest.approx(0.0)

    def test_queda_do_topo_ate_o_fundo(self) -> None:
        """Sobe a 200, cai a 120: -40% do topo. A recuperacao posterior nao apaga
        a queda -- e justamente a maior perda vivida que interessa."""
        precos = np.array([100.0, 200.0, 120.0, 180.0])
        assert maior_queda(precos) == pytest.approx(-0.40)

    def test_mede_do_topo_anterior_nao_do_inicio(self) -> None:
        precos = np.array([100.0, 50.0, 300.0, 150.0])
        assert maior_queda(precos) == pytest.approx(-0.50)


class TestCorrelacao:
    def test_series_identicas_tem_correlacao_1(self) -> None:
        base = np.cumprod(np.append(1.0, 1 + np.array([0.01, -0.02, 0.03] * 15))) * 100
        _, matriz = matriz_correlacao(alinhadas(A=base, B=base * 3))
        assert matriz[0][1] == pytest.approx(1.0)

    def test_series_espelhadas_tem_correlacao_menos_1(self) -> None:
        retornos = np.array([0.01, -0.02, 0.03] * 15)
        a = np.cumprod(np.append(1.0, 1 + retornos)) * 100
        b = np.cumprod(np.append(1.0, 1 - retornos)) * 100
        _, matriz = matriz_correlacao(alinhadas(A=a, B=b))
        assert matriz[0][1] == pytest.approx(-1.0)

    def test_diagonal_e_sempre_1(self) -> None:
        rng = np.random.default_rng(42)  # semente fixa: teste reprodutivel
        series = {t: np.cumprod(1 + rng.normal(0, 0.01, 100)) * 100 for t in "ABC"}
        tickers, matriz = matriz_correlacao(alinhadas(**series))
        for i in range(len(tickers)):
            assert matriz[i][i] == pytest.approx(1.0)

    def test_e_simetrica(self) -> None:
        rng = np.random.default_rng(7)
        series = {t: np.cumprod(1 + rng.normal(0, 0.01, 100)) * 100 for t in "ABC"}
        _, matriz = matriz_correlacao(alinhadas(**series))
        assert np.allclose(matriz, matriz.T)

    def test_tickers_vem_ordenados(self) -> None:
        """A ordem da matriz precisa ser deterministica: quem consome indexa por
        posicao, e ordem instavel trocaria os ativos silenciosamente."""
        rng = np.random.default_rng(1)
        nomes = ("VALE3", "ABEV3", "PETR4")
        series = {t: np.cumprod(1 + rng.normal(0, 0.01, 60)) * 100 for t in nomes}
        tickers, _ = matriz_correlacao(alinhadas(**series))
        assert tickers == ["ABEV3", "PETR4", "VALE3"]


class TestCovariancia:
    def test_diagonal_e_a_variancia_anualizada(self) -> None:
        """A diagonal da matriz de covariancia e a variancia de cada ativo, que
        e o quadrado da volatilidade. Este teste amarra os dois calculos: se um
        deles mudar de convencao, o outro denuncia."""
        rng = np.random.default_rng(3)
        precos = np.cumprod(1 + rng.normal(0, 0.015, 300)) * 100
        _, cov = matriz_covariancia(alinhadas(A=precos))
        vol = volatilidade_anualizada(retornos_diarios(precos))
        assert cov[0][0] == pytest.approx(vol**2, rel=1e-9)

    def test_anualizacao_multiplica_por_252(self) -> None:
        """Covariancia diaria misturada com retorno anual erra por um fator de
        252 -- e as contas fecham, entao nada denuncia."""
        rng = np.random.default_rng(5)
        series = {t: np.cumprod(1 + rng.normal(0, 0.01, 200)) * 100 for t in "AB"}
        _, anual = matriz_covariancia(alinhadas(**series), anualizar=True)
        _, diaria = matriz_covariancia(alinhadas(**series), anualizar=False)
        assert np.allclose(anual, diaria * PREGOES_POR_ANO)


class TestHistoricoInsuficiente:
    def test_poucos_dados_devolve_none(self) -> None:
        """Com 5 observacoes o desvio-padrao e ruido. Devolver esse numero como
        'risco' seria pior que devolver nada, porque parece informacao."""
        assert metricas_do_ativo("X", np.array([10.0, 11.0, 12.0]), 0.10) is None

    def test_dados_suficientes_devolve_metricas(self) -> None:
        rng = np.random.default_rng(11)
        precos = np.cumprod(1 + rng.normal(0.0005, 0.01, 100)) * 100
        m = metricas_do_ativo("X", precos, 0.10)
        assert m is not None
        assert m.observacoes == 100
