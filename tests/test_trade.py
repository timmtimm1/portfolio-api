"""Testes do trade otimo.

Numeros conferidos a mao com uma posicao real da carteira: TAEE11, 45 acoes
a preco medio de R$ 37,39 (custo R$ 1.682,55), cotada a R$ 41,38.

A conta e bruta -- sem imposto e sem corretagem. O motivo esta no docstring
de `app/services/trade.py`, e nao e simplificacao: modelar IR direito exige
somar as vendas do mes, prejuizo acumulado e o tipo de operacao, e um numero
fiscal quase certo e pior que nenhum.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.trade import planejar, preco_para_residuo

QUANTIDADE = Decimal(45)
PRECO_MEDIO = Decimal("37.39")
PRECO_ATUAL = Decimal("41.38")
CUSTO = Decimal("1682.55")  # 45 x 37,39


class TestRecuperarOCusto:
    """Sem lucro extra: o minimo para tirar o que se pagou."""

    def test_quantas_acoes_vender(self) -> None:
        """1.682,55 / 41,38 = 40,66 -> 41 acoes."""
        plano = planejar(quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, preco_atual=PRECO_ATUAL)
        assert plano is not None
        assert plano.vender == 41
        assert plano.viavel is True

    def test_o_residuo_e_o_que_sobra(self) -> None:
        """45 - 41 = 4 acoes que, dali em diante, nao custaram nada."""
        plano = planejar(quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, preco_atual=PRECO_ATUAL)
        assert plano is not None
        assert plano.residuo == 4
        assert plano.residuo_valor == Decimal("165.52")  # 4 x 41,38

    def test_arredonda_para_CIMA(self) -> None:
        """40,66 vira 41, nao 40.

        Vender 40 deixaria o custo descoberto por alguns centavos -- que e
        exatamente o que a operacao existe para nao acontecer. E fracao de
        acao nao existe na B3.
        """
        plano = planejar(quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, preco_atual=PRECO_ATUAL)
        assert plano is not None
        assert plano.recebe == Decimal("1696.58")  # 41 x 41,38
        assert plano.recebe > CUSTO

    def test_a_sobra_em_caixa_e_o_troco_do_arredondamento(self) -> None:
        """1.696,58 - 1.682,55 = 14,03."""
        plano = planejar(quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, preco_atual=PRECO_ATUAL)
        assert plano is not None
        assert plano.sobra_em_caixa == Decimal("14.03")
        assert plano.custo_recuperado == CUSTO


class TestComLucroDesejado:
    def test_lucro_alcancavel(self) -> None:
        """Tirar R$ 100 alem do custo: (1.682,55 + 100) / 41,38 = 43,08 -> 44."""
        plano = planejar(
            quantidade=QUANTIDADE,
            preco_medio=PRECO_MEDIO,
            preco_atual=PRECO_ATUAL,
            lucro_desejado=Decimal(100),
        )
        assert plano is not None
        assert plano.vender == 44
        assert plano.residuo == 1
        assert plano.viavel is True

    def test_lucro_alto_demais_e_inviavel(self) -> None:
        """R$ 500 exigiria vender 53 de 45. Nao e erro -- e a resposta honesta
        de que o preco ainda nao subiu o bastante."""
        plano = planejar(
            quantidade=QUANTIDADE,
            preco_medio=PRECO_MEDIO,
            preco_atual=PRECO_ATUAL,
            lucro_desejado=Decimal(500),
        )
        assert plano is not None
        assert plano.viavel is False
        assert plano.vender == 53
        # O numero continua sendo mostrado: e ele que permite a tela dizer
        # "faltam 8 acoes" em vez de um "nao da" sem tamanho.
        assert plano.vender - int(QUANTIDADE) == 8

    def test_inviavel_nao_promete_residuo(self) -> None:
        plano = planejar(
            quantidade=QUANTIDADE,
            preco_medio=PRECO_MEDIO,
            preco_atual=PRECO_ATUAL,
            lucro_desejado=Decimal(500),
        )
        assert plano is not None
        assert plano.residuo == 0
        assert plano.residuo_valor == Decimal(0)


class TestPosicaoNoPrejuizo:
    def test_preco_abaixo_do_medio_nunca_recupera(self) -> None:
        """Vender tudo a um preco abaixo do medio nao devolve o que se pagou.
        A conta continua valendo e o veredito e "inviavel"."""
        plano = planejar(quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, preco_atual=Decimal(30))
        assert plano is not None
        assert plano.viavel is False
        assert plano.vender > int(QUANTIDADE)


class TestSemOQuePlanejar:
    def test_sem_posicao(self) -> None:
        assert (
            planejar(quantidade=Decimal(0), preco_medio=PRECO_MEDIO, preco_atual=PRECO_ATUAL)
            is None
        )

    def test_sem_preco(self) -> None:
        """`None`, e nao um plano de vender zero: o chamador nao deveria ter
        que distinguir os dois casos."""
        assert (
            planejar(quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, preco_atual=Decimal(0)) is None
        )


class TestPrecoParaResiduo:
    """O caminho inverso: "quero ficar com N livres -- a que preco?"."""

    def test_dez_acoes_livres(self) -> None:
        """Vendendo 35 das 45: 1.682,55 / 35 = 48,0728... -> R$ 48,08.

        Sobe para o centavo seguinte, nao para o mais proximo: a R$ 48,07 as
        35 acoes rendem R$ 1.682,45, dez centavos a MENOS que o custo. Um
        preco que quase cobre nao cobre.
        """
        preco = preco_para_residuo(
            quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, residuo_desejado=10
        )
        assert preco == Decimal("48.08")

    def test_quanto_mais_residuo_maior_o_preco_necessario(self) -> None:
        """A propriedade que da sentido ao calculo: guardar mais acoes exige
        que as poucas vendidas cubram o custo inteiro."""
        poucas = preco_para_residuo(
            quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, residuo_desejado=5
        )
        muitas = preco_para_residuo(
            quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, residuo_desejado=30
        )
        assert poucas is not None and muitas is not None
        assert muitas > poucas

    def test_com_lucro_desejado_o_preco_sobe(self) -> None:
        sem = preco_para_residuo(
            quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, residuo_desejado=10
        )
        com = preco_para_residuo(
            quantidade=QUANTIDADE,
            preco_medio=PRECO_MEDIO,
            residuo_desejado=10,
            lucro_desejado=Decimal(500),
        )
        assert sem is not None and com is not None
        assert com > sem

    def test_guardar_tudo_nao_tem_solucao(self) -> None:
        """Nao vender nada e tirar o custo de lugar nenhum e um pedido sem
        resposta -- nao um preco muito alto."""
        assert (
            preco_para_residuo(quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, residuo_desejado=45)
            is None
        )

    def test_residuo_maior_que_a_posicao_nao_tem_solucao(self) -> None:
        assert (
            preco_para_residuo(quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, residuo_desejado=60)
            is None
        )


class TestOsDoisSentidosBatem:
    def test_o_preco_calculado_produz_o_residuo_pedido(self) -> None:
        """A prova de que ida e volta sao a mesma conta: pedir o preco para
        sobrarem 10 acoes e, naquele preco, planejar de novo tem que devolver
        exatamente 10 de residuo."""
        preco = preco_para_residuo(
            quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, residuo_desejado=10
        )
        assert preco is not None

        plano = planejar(quantidade=QUANTIDADE, preco_medio=PRECO_MEDIO, preco_atual=preco)
        assert plano is not None
        assert plano.residuo == 10
        assert plano.viavel is True
