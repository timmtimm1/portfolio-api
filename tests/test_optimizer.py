"""Testes do otimizador de Markowitz.

Validados por tres caminhos independentes:
  1. propriedades que a solucao tem que ter (somar 1, respeitar limites);
  2. a formula fechada analitica, quando ela e aplicavel;
  3. o `skfolio`, uma implementacao madura e independente.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.optimizer import (
    OtimizacaoInviavelError,
    carteira_para,
    fronteira_eficiente,
    maximo_sharpe,
    minima_variancia,
)

RF = 0.10


def _cov(n: int, semente: int = 42) -> np.ndarray:
    """Covariancia sintetica positiva-definida (A A' e sempre PSD; a diagonal
    extra garante que seja definida, evitando matriz singular)."""
    rng = np.random.default_rng(semente)
    a = rng.normal(0, 1, (n, n))
    return (a @ a.T) / 40 + np.eye(n) * 0.01


class TestRestricoes:
    def test_pesos_somam_um(self) -> None:
        """Investe exatamente o capital: nem alavancagem, nem caixa parado."""
        pesos = minima_variancia(_cov(5))
        assert pesos.sum() == pytest.approx(1.0)

    def test_nenhum_peso_negativo(self) -> None:
        """Peso negativo e venda a descoberto -- inexecutavel para a pessoa
        fisica a quem esta carteira seria sugerida."""
        mu = np.array([0.15, 0.22, 0.09, 0.30, 0.05])
        for pesos in (minima_variancia(_cov(5)), maximo_sharpe(mu, _cov(5), RF)):
            assert (pesos >= -1e-9).all(), pesos

    def test_respeita_o_limite_por_ativo(self) -> None:
        """Sem limite, o otimizador aloca quase tudo no papel que mais subiu na
        amostra -- otimo para o passado, o oposto de diversificar."""
        mu = np.array([0.05, 0.08, 0.60, 0.07])  # o terceiro domina
        pesos = maximo_sharpe(mu, _cov(4), RF, peso_maximo=0.25)
        assert pesos.max() <= 0.25 + 1e-9

    def test_limite_impossivel_e_recusado(self) -> None:
        """2 ativos com teto de 40% somam no maximo 80%: nao ha carteira que
        invista 100%. Erro claro, nao um resultado silenciosamente errado."""
        with pytest.raises(OtimizacaoInviavelError, match="impossivel investir 100%"):
            minima_variancia(_cov(2), peso_maximo=0.40)

    def test_residuos_numericos_sao_zerados(self) -> None:
        """O solver devolve 1e-17 em ativos que deveriam ficar de fora. Devolver
        '0,0000001% em ABEV3' numa carteira sugerida seria ruido."""
        mu = np.array([0.05, 0.50, 0.06, 0.07])
        pesos = maximo_sharpe(mu, _cov(4), RF, peso_maximo=1.0)
        assert not ((pesos > 0) & (pesos < 1e-4)).any()


class TestFormulaFechada:
    """A minima variancia sem restricoes tem solucao analitica exata:

        w = Sigma^-1 1 / (1' Sigma^-1 1)

    Comparar com ela e mais forte que comparar com outra biblioteca: nao ha
    duvida sobre qual das duas esta certa.
    """

    def test_minima_variancia_bate_com_a_analitica(self) -> None:
        cov = _cov(4)
        inv = np.linalg.inv(cov)
        uns = np.ones(4)
        exato = inv @ uns / (uns @ inv @ uns)

        # A formula fechada nao restringe sinal; so comparamos quando ela ja
        # devolve pesos nao-negativos, que e quando os dois problemas coincidem.
        if (exato >= 0).all():
            numerico = minima_variancia(cov, peso_maximo=1.0)
            assert numerico == pytest.approx(exato, abs=1e-5)
            assert numerico @ cov @ numerico == pytest.approx(float(exato @ cov @ exato), rel=1e-9)

    def test_variancia_encontrada_e_a_menor(self) -> None:
        """Propriedade que define a solucao: nenhuma outra carteira valida tem
        variancia menor. Testado contra 500 carteiras aleatorias."""
        cov = _cov(5)
        otima = minima_variancia(cov, peso_maximo=1.0)
        var_otima = float(otima @ cov @ otima)

        rng = np.random.default_rng(1)
        for _ in range(500):
            w = rng.dirichlet(np.ones(5))  # aleatoria, soma 1, nao-negativa
            assert float(w @ cov @ w) >= var_otima - 1e-9


class TestDiversificacao:
    def test_carteira_e_menos_volatil_que_seus_ativos(self) -> None:
        """O ponto inteiro de Markowitz.

        Dois ativos de mesma volatilidade e correlacao NEGATIVA formam uma
        carteira com volatilidade menor que a de qualquer um deles: quando um
        cai, o outro tende a subir.
        """
        vol = 0.30
        cov = np.array([[vol**2, -0.8 * vol * vol], [-0.8 * vol * vol, vol**2]])
        pesos = minima_variancia(cov, peso_maximo=1.0)
        vol_carteira = float(np.sqrt(pesos @ cov @ pesos))
        assert vol_carteira < vol

    def test_ativos_perfeitamente_correlacionados_nao_diversificam(self) -> None:
        """Correlacao 1: a carteira herda a volatilidade dos ativos. Carteira que
        cai junta nao e carteira diversificada, por mais nomes que tenha."""
        vol = 0.30
        cov = np.full((3, 3), vol**2)
        pesos = minima_variancia(cov, peso_maximo=1.0)
        assert float(np.sqrt(pesos @ cov @ pesos)) == pytest.approx(vol, rel=1e-6)


class TestMaximoSharpe:
    def test_supera_a_carteira_igualmente_ponderada(self) -> None:
        n = 5
        mu = np.array([0.15, 0.22, 0.09, 0.30, 0.05])
        cov = _cov(n)
        otima = carteira_para(maximo_sharpe(mu, cov, RF, peso_maximo=1.0), mu, cov, RF)
        igual = carteira_para(np.full(n, 1 / n), mu, cov, RF)
        assert otima.indice_sharpe is not None and igual.indice_sharpe is not None
        assert otima.indice_sharpe >= igual.indice_sharpe - 1e-9

    def test_supera_carteiras_aleatorias(self) -> None:
        mu = np.array([0.15, 0.22, 0.09, 0.30])
        cov = _cov(4)
        otima = carteira_para(maximo_sharpe(mu, cov, RF, peso_maximo=1.0), mu, cov, RF)
        assert otima.indice_sharpe is not None

        rng = np.random.default_rng(2)
        for _ in range(300):
            w = rng.dirichlet(np.ones(4))
            c = carteira_para(w, mu, cov, RF)
            assert c.indice_sharpe is not None
            assert c.indice_sharpe <= otima.indice_sharpe + 1e-6


class TestFronteira:
    def test_comeca_na_minima_variancia(self) -> None:
        """Nada abaixo da minima variancia e eficiente: haveria outra carteira
        com mais retorno e menos risco."""
        mu = np.array([0.15, 0.22, 0.09, 0.30])
        cov = _cov(4)
        fronteira = fronteira_eficiente(mu, cov, RF, pontos=10, peso_maximo=1.0)
        piso = carteira_para(minima_variancia(cov, peso_maximo=1.0), mu, cov, RF)
        assert fronteira[0].volatilidade == pytest.approx(piso.volatilidade, abs=1e-6)

    def test_retorno_cresce_ao_longo_da_curva(self) -> None:
        mu = np.array([0.15, 0.22, 0.09, 0.30])
        fronteira = fronteira_eficiente(mu, _cov(4), RF, pontos=15, peso_maximo=1.0)
        retornos = [c.retorno_esperado for c in fronteira]
        assert retornos == sorted(retornos)

    def test_nenhum_ponto_tem_risco_menor_que_o_minimo(self) -> None:
        """Se algum tivesse, a 'minima variancia' nao seria minima."""
        mu = np.array([0.15, 0.22, 0.09, 0.30])
        cov = _cov(4)
        minimo = carteira_para(minima_variancia(cov, peso_maximo=1.0), mu, cov, RF).volatilidade
        for c in fronteira_eficiente(mu, cov, RF, pontos=15, peso_maximo=1.0):
            assert c.volatilidade >= minimo - 1e-6

    def test_todos_os_pontos_respeitam_as_restricoes(self) -> None:
        mu = np.array([0.15, 0.22, 0.09, 0.30, 0.11])
        for c in fronteira_eficiente(mu, _cov(5), RF, pontos=12, peso_maximo=0.5):
            assert c.pesos.sum() == pytest.approx(1.0)
            assert (c.pesos >= -1e-9).all()
            assert c.pesos.max() <= 0.5 + 1e-6

    def test_maximo_sharpe_esta_sobre_a_fronteira(self) -> None:
        """A carteira de tangencia e um ponto da fronteira: nenhum ponto dela
        deve ter Sharpe materialmente maior."""
        mu = np.array([0.15, 0.22, 0.09, 0.30])
        cov = _cov(4)
        melhor = carteira_para(maximo_sharpe(mu, cov, RF, peso_maximo=1.0), mu, cov, RF)
        assert melhor.indice_sharpe is not None
        for c in fronteira_eficiente(mu, cov, RF, pontos=30, peso_maximo=1.0):
            assert c.indice_sharpe is not None
            assert c.indice_sharpe <= melhor.indice_sharpe + 1e-4


class TestContraSkfolio:
    """Conferencia cruzada com o `skfolio`, implementacao independente e madura.

    Usamos os mesmos estimadores que ele usa por padrao (media e covariancia
    amostrais) para que a comparacao seja da OTIMIZACAO, nao da estimativa.
    """

    @staticmethod
    def _dados() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        rng = np.random.default_rng(7)
        retornos = rng.normal(0.0006, 0.014, (500, 5))
        retornos[:, 1] += retornos[:, 0] * 0.6
        retornos[:, 3] -= retornos[:, 2] * 0.4
        return retornos, retornos.mean(axis=0), np.cov(retornos, rowvar=False, ddof=1)

    def test_minima_variancia(self) -> None:
        from skfolio.optimization import MeanRisk, ObjectiveFunction

        retornos, _, cov = self._dados()
        meu = minima_variancia(cov, peso_maximo=1.0)
        deles = np.asarray(
            MeanRisk(objective_function=ObjectiveFunction.MINIMIZE_RISK).fit(retornos).weights_
        )

        assert meu == pytest.approx(deles, abs=2e-3)
        # A variancia atingida importa mais que o vetor: perto do otimo, pesos
        # ligeiramente diferentes dao praticamente a mesma variancia.
        assert float(meu @ cov @ meu) == pytest.approx(float(deles @ cov @ deles), rel=1e-4)

    def test_maximo_sharpe(self) -> None:
        from skfolio.optimization import MeanRisk, ObjectiveFunction

        retornos, mu, cov = self._dados()
        rf = 0.10 / 252  # diario, mesma unidade dos retornos
        meu = maximo_sharpe(mu, cov, rf, peso_maximo=1.0)
        deles = np.asarray(
            MeanRisk(objective_function=ObjectiveFunction.MAXIMIZE_RATIO, risk_free_rate=rf)
            .fit(retornos)
            .weights_
        )

        assert meu == pytest.approx(deles, abs=2e-3)
        c_meu = carteira_para(meu, mu, cov, rf)
        c_deles = carteira_para(deles, mu, cov, rf)
        assert c_meu.indice_sharpe is not None and c_deles.indice_sharpe is not None
        assert c_meu.indice_sharpe == pytest.approx(c_deles.indice_sharpe, rel=1e-5)
