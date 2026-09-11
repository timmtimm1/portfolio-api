"""Testes do modelo de Black-Litterman.

Modulo puro: entra numero, sai numero. Os casos foram montados para fechar na
calculadora -- covariancia diagonal de 0,04 com pesos 50/50 da variancia de
mercado 0,02, e com premio de 5% ao ano o delta sai 2,5 exato, que por acaso e
o valor de referencia da literatura.

Nao ha teste de "o resultado parece razoavel" aqui de proposito. Um modelo de
retorno esperado produz numeros plausiveis mesmo quando esta invertido -- foi
isso que motivou a faixa aceitavel do delta -- entao todo teste confere uma
identidade que so vale se a conta estiver certa.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services import black_litterman as bl

# Sem correlacao: as contas saem redondas e cada ativo se move sozinho.
COV_DIAGONAL = np.array([[0.04, 0.0], [0.0, 0.04]])
# Com correlacao: e onde se ve uma opiniao contaminar o vizinho.
COV_CORRELACIONADA = np.array([[0.04, 0.02], [0.02, 0.04]])

TAXA_LIVRE = 0.10


class TestPesosDeMercado:
    def test_peso_e_proporcional_ao_valor_de_mercado(self) -> None:
        pesos, faltantes = bl.pesos_de_mercado([300e9, 100e9])

        assert pesos == pytest.approx([0.75, 0.25])
        assert faltantes == []
        assert pesos.sum() == pytest.approx(1.0)

    def test_falta_um_valor_e_o_vetor_INTEIRO_vira_peso_igual(self) -> None:
        """Nao e "peso igual so no faltante" -- e no vetor todo.

        Completar so o buraco produziria um vetor que parece o mercado e nao e,
        porque o peso e fracao do total: o valor inventado para um ativo muda o
        peso de todos os outros. O teste fixa a escolha de nao fazer isso.
        """
        pesos, faltantes = bl.pesos_de_mercado([300e9, None, 100e9])

        assert pesos == pytest.approx([1 / 3, 1 / 3, 1 / 3])
        assert faltantes == [1]

    def test_valor_zero_ou_negativo_conta_como_ausencia(self) -> None:
        """Zero nao e "empresa pequena", e dado sujo.

        Tratado como numero, daria peso 0 com cara de legitimo e o ativo sumiria
        do equilibrio sem ninguem ser avisado.
        """
        _, faltantes = bl.pesos_de_mercado([300e9, 0.0, -5.0])

        assert faltantes == [1, 2]

    def test_lista_vazia_nao_estoura(self) -> None:
        pesos, faltantes = bl.pesos_de_mercado([])

        assert pesos.shape == (0,)
        assert faltantes == []


class TestAversaoAoRisco:
    def test_delta_e_o_premio_dividido_pela_variancia(self) -> None:
        """(0,15 - 0,10) / 0,02 = 2,5."""
        delta, padrao = bl.aversao_ao_risco(0.15, 0.02, TAXA_LIVRE)

        assert delta == pytest.approx(2.5)
        assert padrao is False

    def test_mercado_que_caiu_nao_inverte_o_equilibrio(self) -> None:
        """O caso perigoso, e a razao da faixa aceitavel existir.

        Numa janela de queda o premio fica negativo e delta tambem. Com delta
        negativo, pi = delta*Sigma*w troca de sinal e o equilibrio passa a
        afirmar que o ativo mais arriscado rende MENOS -- a otimizacao entao
        corre para ele. Sai sem erro, com numeros de aparencia normal.
        """
        delta, padrao = bl.aversao_ao_risco(-0.20, 0.02, TAXA_LIVRE)

        assert delta == bl.DELTA_PADRAO
        assert delta > 0
        assert padrao is True

    def test_variancia_zero_cai_no_padrao_em_vez_de_dividir_por_zero(self) -> None:
        delta, padrao = bl.aversao_ao_risco(0.15, 0.0, TAXA_LIVRE)

        assert delta == bl.DELTA_PADRAO
        assert padrao is True

    def test_delta_absurdamente_alto_cai_no_padrao(self) -> None:
        """Premio de 50% sobre variancia minuscula da delta ~500.

        Um investidor com essa aversao so aceitaria o ativo de menor variancia
        da amostra. E sintoma de janela ruim, nao de preferencia.
        """
        delta, padrao = bl.aversao_ao_risco(0.60, 0.001, TAXA_LIVRE)

        assert delta == bl.DELTA_PADRAO
        assert padrao is True


class TestRetornosDeEquilibrio:
    def test_pi_conferido_na_mao(self) -> None:
        """pi = 2,5 * diag(0,04) @ [0,5, 0,5] = [0,05, 0,05]."""
        pi = bl.retornos_de_equilibrio(2.5, COV_DIAGONAL, np.array([0.5, 0.5]))

        assert pi == pytest.approx([0.05, 0.05])

    def test_a_identidade_que_prova_a_otimizacao_reversa(self) -> None:
        """w' pi tem que ser exatamente o premio de risco do mercado.

        E a identidade que sustenta o modelo inteiro: se pi = delta*Sigma*w,
        entao w'pi = delta*w'Sigma*w = delta * variancia = premio. Um erro de
        sinal, de transposicao ou de escala em qualquer ponto quebra isto, e
        praticamente nada mais quebraria de forma visivel.
        """
        pesos = np.array([0.75, 0.25])
        variancia = float(pesos @ COV_CORRELACIONADA @ pesos)
        delta, _ = bl.aversao_ao_risco(0.18, variancia, TAXA_LIVRE)

        pi = bl.retornos_de_equilibrio(delta, COV_CORRELACIONADA, pesos)

        assert float(pesos @ pi) == pytest.approx(0.18 - TAXA_LIVRE)

    def test_quem_carrega_mais_risco_promete_mais(self) -> None:
        """Sem retorno historico nenhum na conta -- so covariancia e pesos."""
        cov = np.array([[0.01, 0.0], [0.0, 0.09]])

        pi = bl.retornos_de_equilibrio(2.5, cov, np.array([0.5, 0.5]))

        assert pi[1] > pi[0]


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
    def test_do_valor_de_mercado_ate_o_mu(self) -> None:
        """Tudo conferido na mao: w=[0,5;0,5], var=0,02, delta=2,5, pi=[0,05;0,05]."""
        posterior = bl.black_litterman(
            COV_DIAGONAL,
            [100e9, 100e9],
            retorno_mercado=0.15,
            taxa_livre_risco=TAXA_LIVRE,
        )

        assert posterior.equilibrio.pesos_mercado == pytest.approx([0.5, 0.5])
        assert posterior.equilibrio.delta == pytest.approx(2.5)
        assert posterior.equilibrio.pi == pytest.approx([0.05, 0.05])
        assert posterior.mu == pytest.approx([0.05, 0.05])
        assert posterior.visoes_aplicadas == 0

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
            [100e9, 100e9],
            retorno_mercado=0.15,
            taxa_livre_risco=TAXA_LIVRE,
            p=p,
            q_total=np.array([0.15]),
        )

        assert posterior.mu[0] == pytest.approx(0.05)
        assert posterior.visoes_aplicadas == 1

    def test_retorno_total_devolve_a_taxa_livre_de_risco(self) -> None:
        excedente = np.array([0.05, 0.03])

        assert bl.retorno_total(excedente, TAXA_LIVRE) == pytest.approx([0.15, 0.13])

    def test_sem_valor_de_mercado_o_prior_avisa_que_nao_e_o_mercado(self) -> None:
        posterior = bl.black_litterman(
            COV_DIAGONAL,
            [100e9, None],
            retorno_mercado=0.15,
            taxa_livre_risco=TAXA_LIVRE,
        )

        assert posterior.equilibrio.usou_peso_igual is True
        assert posterior.equilibrio.sem_valor_de_mercado == [1]
