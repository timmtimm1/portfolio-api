"""Testes do modelo de Black-Litterman.

Modulo puro: entra numero, sai numero. Os casos foram montados para fechar na
calculadora -- covariancia diagonal de 0,04 e um mercado meio a meio entre os
dois ativos, o que da covariancia de cada um com o mercado de 0,02 e variancia
do mercado de 0,02. Com premio de 5% ao ano o delta sai 2,5 e o beta de cada
ativo sai 1.

Nao ha teste de "o resultado parece razoavel" aqui de proposito. Um modelo de
retorno esperado produz numeros plausiveis mesmo quando esta errado -- a versao
anterior devolvia 25% a 64% ao ano para acoes brasileiras com cara de normal --
entao todo teste confere uma identidade que so vale se a conta estiver certa. A
checagem de plausibilidade com dado de mercado fica nos testes da rota.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.services import black_litterman as bl

# Sem correlacao: as contas saem redondas e cada ativo se move sozinho.
COV_DIAGONAL = np.array([[0.04, 0.0], [0.0, 0.04]])
# Com correlacao: e onde se ve uma opiniao contaminar o vizinho.
COV_CORRELACIONADA = np.array([[0.04, 0.02], [0.02, 0.04]])

# Mercado = 50/50 dos dois ativos de COV_DIAGONAL: cov com o mercado = Sigma w.
COV_COM_MERCADO = np.array([0.02, 0.02])
VARIANCIA_MERCADO = 0.02

TAXA_LIVRE = 0.10


class TestAversaoAoRisco:
    def test_delta_e_o_premio_dividido_pela_variancia(self) -> None:
        assert bl.aversao_ao_risco(0.05, 0.02) == pytest.approx(2.5)

    def test_com_a_volatilidade_do_ibovespa_o_delta_fica_perto_de_um(self) -> None:
        """A calibracao que motivou a troca, em numero.

        Com o Ibovespa a ~22,6% de vol ao ano (mar/2025-out/2026) e premio de
        5%, delta ~1,0. O 2,5 da literatura, calibrado na bolsa americana,
        implicaria aqui um premio de 12,8% ao ano sobre o CDI.
        """
        delta = bl.aversao_ao_risco(bl.PREMIO_DE_RISCO_PADRAO, 0.226**2)

        assert delta == pytest.approx(0.98, abs=0.01)
        assert 2.5 * 0.226**2 == pytest.approx(0.128, abs=0.001)

    @pytest.mark.parametrize("variancia", [0.0, -0.01, math.nan, math.inf])
    def test_variancia_impossivel_e_recusada(self, variancia: float) -> None:
        """A versao antiga caia num delta padrao e seguia com cara de normal.
        Variancia assim so sai de dado quebrado, e quem chama precisa saber."""
        with pytest.raises(ValueError, match="variancia"):
            bl.aversao_ao_risco(0.05, variancia)

    @pytest.mark.parametrize("premio", [0.0, -0.02, math.nan])
    def test_premio_que_nao_paga_risco_e_recusado(self, premio: float) -> None:
        """Premio zero zera todo excedente; negativo diria que risco paga para
        perder. Os dois inverteriam ou anulariam o equilibrio sem erro."""
        with pytest.raises(ValueError, match="premio"):
            bl.aversao_ao_risco(premio, 0.02)


class TestEquilibrio:
    def test_pi_e_beta_vezes_premio(self) -> None:
        """beta = [0,02; 0,03] / 0,02 = [1; 1,5]; pi = beta x 5% = [5%; 7,5%]."""
        eq = bl.equilibrio_de_mercado(np.array([0.02, 0.03]), 0.02, premio_de_risco=0.05)

        assert eq.delta == pytest.approx(2.5)
        assert eq.betas == pytest.approx([1.0, 1.5])
        assert eq.pi == pytest.approx([0.05, 0.075])
        assert eq.pi == pytest.approx(eq.betas * eq.premio_de_risco)

    def test_e_a_mesma_otimizacao_reversa_da_formula_classica(self) -> None:
        """Quando o mercado E uma carteira w dos ativos, pi = delta*Sigma*w.

        E a prova de que medir a covariancia contra o indice nao mudou o
        modelo, so a fonte do mercado: com c = Sigma w e var = w'Sigma w, sai
        o mesmo pi da formulacao com pesos -- e w'pi fecha no premio.
        """
        pesos = np.array([0.75, 0.25])
        c = COV_CORRELACIONADA @ pesos
        variancia = float(pesos @ COV_CORRELACIONADA @ pesos)

        eq = bl.equilibrio_de_mercado(c, variancia, premio_de_risco=0.06)

        assert eq.pi == pytest.approx(eq.delta * COV_CORRELACIONADA @ pesos)
        assert float(pesos @ eq.pi) == pytest.approx(0.06)

    def test_o_retorno_de_um_ativo_nao_depende_dos_vizinhos(self) -> None:
        """A regressao principal: o mercado era a carteira do usuario.

        Antes, o pi de ITUB4 ia de 16% a 52% ao ano conforme os outros papeis
        da carteira. Agora cada pi depende so da covariancia do ativo com o
        indice -- acrescentar ou tirar vizinho nao o move.
        """
        sozinho = bl.equilibrio_de_mercado(np.array([0.03, 0.01]), 0.02)
        com_outros = bl.equilibrio_de_mercado(np.array([0.03, 0.05, 0.02, 0.04]), 0.02)

        assert sozinho.pi[0] == com_outros.pi[0]

    def test_ativo_que_anda_contra_o_mercado_promete_menos_que_a_taxa_livre(self) -> None:
        """Beta negativo (ouro, dolar em crise) da excedente negativo: o ativo
        paga para proteger. E o comportamento correto, nao um erro de sinal."""
        eq = bl.equilibrio_de_mercado(np.array([-0.004]), 0.02)

        assert eq.betas[0] == pytest.approx(-0.2)
        assert eq.pi[0] == pytest.approx(-0.01)


class TestParaExcesso:
    def test_opiniao_absoluta_desconta_a_taxa_livre_de_risco(self) -> None:
        """ "PETR4 rende 15%" vira 5% de excedente com rf de 10%."""
        p = np.array([[1.0, 0.0]])

        assert bl.para_excesso(p, np.array([0.15]), TAXA_LIVRE) == pytest.approx([0.05])

    def test_opiniao_relativa_passa_intacta(self) -> None:
        """Numa diferenca entre dois ativos a taxa livre de risco se cancela.

        Descontar aqui contaria rf duas vezes e encolheria toda opiniao relativa
        pelo mesmo tanto -- um vies sistematico e silencioso.
        """
        p = np.array([[1.0, -1.0]])

        assert bl.para_excesso(p, np.array([0.03]), TAXA_LIVRE) == pytest.approx([0.03])

    def test_opiniao_mista_desconta_na_proporcao_da_linha(self) -> None:
        """Meia exposicao liquida desconta meia taxa.

        E o caso que um `if absoluta/relativa` erraria: a formula unica
        (q - rf * soma_da_linha) acerta os tres.
        """
        p = np.array([[1.0, -0.5]])

        assert bl.para_excesso(p, np.array([0.20]), TAXA_LIVRE) == pytest.approx([0.15])


class TestCombinar:
    def test_sem_opiniao_o_posterior_e_o_equilibrio(self) -> None:
        pi = np.array([0.05, 0.05])

        mu, cov = bl.combinar(pi, COV_DIAGONAL, np.zeros((0, 2)), np.zeros(0))

        assert mu == pytest.approx(pi)
        # A covariancia infla em (1 + tau): a incerteza sobre a MEDIA entra na
        # conta de risco. Nao e arredondamento, e parte do modelo.
        assert cov == pytest.approx(COV_DIAGONAL * (1 + bl.TAU_PADRAO))

    def test_opiniao_que_repete_o_equilibrio_nao_move_nada(self) -> None:
        """O modelo reage a DISCORDANCIA, nao a opiniao.

        O termo (q - P pi) e zero aqui. Uma implementacao que somasse a opiniao
        em vez de combinar com ela passaria em quase tudo e falharia neste.
        """
        pi = np.array([0.05, 0.05])
        p = np.array([[1.0, 0.0]])

        mu, _ = bl.combinar(pi, COV_DIAGONAL, p, np.array([0.05]))

        assert mu == pytest.approx(pi)

    def test_opiniao_discordante_anda_metade_do_caminho(self) -> None:
        """A assinatura de Omega = diag(P tau Sigma P').

        Com uma opiniao, `P tau Sigma P' + Omega` e o dobro de `P tau Sigma P'`,
        entao o posterior fica exatamente no meio entre equilibrio e opiniao.
        Esse 0,5 e o teste que distingue esta variante de qualquer outra escolha
        de Omega -- e o que faz o titulo "He-Litterman" ser verdade.
        """
        pi = np.array([0.05, 0.05])
        p = np.array([[1.0, 0.0]])
        opiniao = 0.09

        mu, _ = bl.combinar(pi, COV_DIAGONAL, p, np.array([opiniao]))

        assert mu[0] == pytest.approx((0.05 + opiniao) / 2)
        # Sem correlacao, o vizinho nao se mexe.
        assert mu[1] == pytest.approx(0.05)

    def test_opiniao_contamina_o_ativo_correlacionado(self) -> None:
        """Opinar sobre um ativo e opinar um pouco sobre quem anda junto.

        Se o modelo so mexesse no ativo citado, teria virado "substituir a
        celula do mu", que e exatamente o que Black-Litterman NAO e.
        """
        pi = np.array([0.06, 0.06])
        p = np.array([[1.0, 0.0]])

        mu, _ = bl.combinar(pi, COV_CORRELACIONADA, p, np.array([0.10]))

        assert mu[0] > pi[0]
        assert mu[1] > pi[1]
        # Metade da correlacao (0,02/0,04), entao metade do deslocamento.
        assert (mu[1] - pi[1]) == pytest.approx((mu[0] - pi[0]) / 2)

    def test_opiniao_pessimista_puxa_para_baixo(self) -> None:
        pi = np.array([0.05, 0.05])
        p = np.array([[1.0, 0.0]])

        mu, _ = bl.combinar(pi, COV_DIAGONAL, p, np.array([0.01]))

        assert mu[0] == pytest.approx(0.03)
        assert mu[0] < pi[0]

    def test_duas_opinioes_identicas_nao_quebram(self) -> None:
        """O usuario repete a mesma ideia com outras palavras e o sistema cai.

        Duas linhas iguais em P deixam a matriz do meio singular. Sem a rede do
        pinv isto levanta LinAlgError e a rota devolve 500 por causa de um
        preenchimento de formulario.
        """
        pi = np.array([0.05, 0.05])
        p = np.array([[1.0, 0.0], [1.0, 0.0]])

        mu, cov = bl.combinar(pi, COV_DIAGONAL, p, np.array([0.09, 0.09]))

        assert np.all(np.isfinite(mu))
        assert np.all(np.isfinite(cov))
        assert mu[0] > pi[0]

    def test_a_covariancia_posterior_sai_simetrica(self) -> None:
        """Assimetria de 1e-17 do produto de matrizes faz um solver recusar tudo."""
        pi = np.array([0.06, 0.06])
        p = np.array([[1.0, -1.0]])

        _, cov = bl.combinar(pi, COV_CORRELACIONADA, p, np.array([0.02]))

        assert cov == pytest.approx(cov.T)
        assert np.all(np.linalg.eigvalsh(cov) > 0)


class TestCaminhoCompleto:
    def test_da_covariancia_com_o_mercado_ate_o_mu(self) -> None:
        """Tudo conferido na mao: delta=2,5, beta=[1;1], pi=[0,05;0,05]."""
        posterior = bl.black_litterman(
            COV_DIAGONAL, COV_COM_MERCADO, VARIANCIA_MERCADO, taxa_livre_risco=TAXA_LIVRE
        )

        assert posterior.equilibrio.delta == pytest.approx(2.5)
        assert posterior.equilibrio.betas == pytest.approx([1.0, 1.0])
        assert posterior.equilibrio.pi == pytest.approx([0.05, 0.05])
        assert posterior.mu == pytest.approx([0.05, 0.05])
        assert posterior.visoes_aplicadas == 0

    def test_o_premio_padrao_e_cinco_por_cento(self) -> None:
        assert bl.PREMIO_DE_RISCO_PADRAO == 0.05

    def test_premio_maior_escala_o_equilibrio_na_mesma_proporcao(self) -> None:
        posterior = bl.black_litterman(
            COV_DIAGONAL,
            COV_COM_MERCADO,
            VARIANCIA_MERCADO,
            taxa_livre_risco=TAXA_LIVRE,
            premio_de_risco=0.08,
        )

        assert posterior.equilibrio.pi == pytest.approx([0.08, 0.08])

    def test_a_opiniao_chega_em_retorno_TOTAL_e_e_convertida(self) -> None:
        """O erro de unidade que nao estoura em lugar nenhum.

        Com rf de 10%, a opiniao "rende 15%" e 5% de excedente -- que por acaso
        e o proprio equilibrio, entao o mu nao pode se mexer. Se o codigo
        passasse os 15% direto como excedente, ele veria uma discordancia de 10
        pontos e empurraria o mu para 0,10. O teste separa os dois casos.
        """
        p = np.array([[1.0, 0.0]])

        posterior = bl.black_litterman(
            COV_DIAGONAL,
            COV_COM_MERCADO,
            VARIANCIA_MERCADO,
            taxa_livre_risco=TAXA_LIVRE,
            p=p,
            q_total=np.array([0.15]),
        )

        assert posterior.mu[0] == pytest.approx(0.05)
        assert posterior.visoes_aplicadas == 1

    def test_retorno_total_devolve_a_taxa_livre_de_risco(self) -> None:
        excedente = np.array([0.05, 0.03])

        assert bl.retorno_total(excedente, TAXA_LIVRE) == pytest.approx([0.15, 0.13])
