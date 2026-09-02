"""Testes do calculo de stop gain / stop loss.

Modulo puro: os limites sao conferidos a mao, sem depender de banco. A conta e
simples de proposito -- se ela precisar de calculadora para conferir, algo no
desenho esta errado.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.target import Alvo, StatusAlvo, TipoAlvo, avaliar


class TestSemAlvo:
    def test_sem_nenhum_lado_configurado(self) -> None:
        assert (
            avaliar(None, preco_medio=Decimal(20), preco_atual=Decimal(25)) is StatusAlvo.SEM_ALVO
        )

    def test_alvo_com_os_dois_lados_nulos_tambem_e_sem_alvo(self) -> None:
        """`Alvo()` vazio e o mesmo que nao ter registro nenhum -- o status nao
        pode depender de qual das duas formas o chamador usou."""
        assert (
            avaliar(Alvo(), preco_medio=Decimal(20), preco_atual=Decimal(25)) is StatusAlvo.SEM_ALVO
        )


class TestStopGainPercentual:
    """preco_medio = 20,00. Stop gain em 15% -> limite = 20 x 1,15 = 23,00."""

    ALVO = Alvo(stop_gain_tipo=TipoAlvo.PERCENTUAL, stop_gain_valor=Decimal("0.15"))

    def test_abaixo_do_limite_fica_dentro(self) -> None:
        r = avaliar(self.ALVO, preco_medio=Decimal(20), preco_atual=Decimal("22.99"))
        assert r is StatusAlvo.DENTRO

    def test_exatamente_no_limite_ja_bateu(self) -> None:
        """>=, nao >: quem definiu "15%" quer ser avisado QUANDO chegar la, nao
        só depois de passar."""
        r = avaliar(self.ALVO, preco_medio=Decimal(20), preco_atual=Decimal("23.00"))
        assert r is StatusAlvo.GAIN_ATINGIDO

    def test_acima_do_limite_bateu(self) -> None:
        r = avaliar(self.ALVO, preco_medio=Decimal(20), preco_atual=Decimal("30.00"))
        assert r is StatusAlvo.GAIN_ATINGIDO


class TestStopLossPercentual:
    """preco_medio = 20,00. Stop loss em 8% -> limite = 20 x 0,92 = 18,40."""

    ALVO = Alvo(stop_loss_tipo=TipoAlvo.PERCENTUAL, stop_loss_valor=Decimal("0.08"))

    def test_acima_do_limite_fica_dentro(self) -> None:
        r = avaliar(self.ALVO, preco_medio=Decimal(20), preco_atual=Decimal("18.41"))
        assert r is StatusAlvo.DENTRO

    def test_exatamente_no_limite_ja_bateu(self) -> None:
        r = avaliar(self.ALVO, preco_medio=Decimal(20), preco_atual=Decimal("18.40"))
        assert r is StatusAlvo.LOSS_ATINGIDO

    def test_abaixo_do_limite_bateu(self) -> None:
        r = avaliar(self.ALVO, preco_medio=Decimal(20), preco_atual=Decimal("10.00"))
        assert r is StatusAlvo.LOSS_ATINGIDO


class TestTipoPreco:
    """Preco fixo NAO depende do preco medio -- e o ponto do tipo."""

    def test_stop_gain_preco_fixo_ignora_o_preco_medio(self) -> None:
        alvo = Alvo(stop_gain_tipo=TipoAlvo.PRECO, stop_gain_valor=Decimal("45.00"))
        # preco_medio bem diferente do alvo: se a conta usasse preco_medio por
        # engano, o limite sairia errado e o teste pegaria.
        assert avaliar(alvo, preco_medio=Decimal(1000), preco_atual=Decimal("45.00")) is (
            StatusAlvo.GAIN_ATINGIDO
        )
        assert avaliar(alvo, preco_medio=Decimal(1000), preco_atual=Decimal("44.99")) is (
            StatusAlvo.DENTRO
        )

    def test_stop_loss_preco_fixo_ignora_o_preco_medio(self) -> None:
        alvo = Alvo(stop_loss_tipo=TipoAlvo.PRECO, stop_loss_valor=Decimal("18.00"))
        assert avaliar(alvo, preco_medio=Decimal(5), preco_atual=Decimal("18.00")) is (
            StatusAlvo.LOSS_ATINGIDO
        )
        assert (
            avaliar(alvo, preco_medio=Decimal(5), preco_atual=Decimal("18.01")) is StatusAlvo.DENTRO
        )


class TestSemCotacao:
    def test_preco_atual_nulo_nunca_dispara_atingido(self) -> None:
        """Sem cotacao nao ha o que comparar. A alternativa -- presumir que o
        alvo bateu -- e o falso positivo que faz alguem vender confiando num
        numero que o app inventou."""
        alvo = Alvo(stop_gain_tipo=TipoAlvo.PERCENTUAL, stop_gain_valor=Decimal("0.01"))
        assert avaliar(alvo, preco_medio=Decimal(20), preco_atual=None) is StatusAlvo.DENTRO


class TestOsDoisLadosJuntos:
    def test_gain_e_checado_antes_do_loss(self) -> None:
        """O caso do docstring: um stop loss em PRECO FIXO configurado ACIMA do
        preco medio por engano (25, com preco medio 20). Se o preco atual bate
        os dois limites ao mesmo tempo, mostrar GAIN e a leitura mais honesta
        do que aconteceu -- o preco SUBIU, nao caiu."""
        alvo = Alvo(
            stop_gain_tipo=TipoAlvo.PERCENTUAL,
            stop_gain_valor=Decimal("0.10"),  # limite = 22
            stop_loss_tipo=TipoAlvo.PRECO,
            stop_loss_valor=Decimal("25.00"),  # acima do preco medio, por engano
        )
        r = avaliar(alvo, preco_medio=Decimal(20), preco_atual=Decimal(26))
        assert r is StatusAlvo.GAIN_ATINGIDO

    def test_nenhum_dos_dois_bate_fica_dentro(self) -> None:
        alvo = Alvo(
            stop_gain_tipo=TipoAlvo.PERCENTUAL,
            stop_gain_valor=Decimal("0.15"),
            stop_loss_tipo=TipoAlvo.PERCENTUAL,
            stop_loss_valor=Decimal("0.08"),
        )
        r = avaliar(alvo, preco_medio=Decimal(20), preco_atual=Decimal("20.50"))
        assert r is StatusAlvo.DENTRO
