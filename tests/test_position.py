"""Testes do calculo de posicao.

Puros: sem banco, sem HTTP. Todos os numeros esperados foram conferidos a mao --
nao extraidos de uma execucao anterior do proprio codigo, o que so provaria que
ele continua fazendo o que ja fazia, certo ou errado.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from app.models.transaction import TransactionSide
from app.services.position import VendaSemPosicaoError, calcular_posicoes


@dataclass(frozen=True)
class T:
    """Transacao minima que satisfaz o Protocol -- sem ORM, sem banco."""

    ticker: str
    side: TransactionSide
    quantity: Decimal
    price: Decimal
    traded_at: date
    fees: Decimal = Decimal(0)


def compra(ticker: str, qtd: str, preco: str, dia: int, taxas: str = "0") -> T:
    return T(
        ticker,
        TransactionSide.COMPRA,
        Decimal(qtd),
        Decimal(preco),
        date(2026, 1, dia),
        Decimal(taxas),
    )


def venda(ticker: str, qtd: str, preco: str, dia: int, taxas: str = "0") -> T:
    return T(
        ticker,
        TransactionSide.VENDA,
        Decimal(qtd),
        Decimal(preco),
        date(2026, 1, dia),
        Decimal(taxas),
    )


class TestPrecoMedio:
    def test_compra_unica(self) -> None:
        p = calcular_posicoes([compra("PETR4", "100", "20.00", 1)])["PETR4"]
        assert p.quantidade == Decimal(100)
        assert p.preco_medio == Decimal("20.00")
        assert p.custo_total == Decimal("2000.00")

    def test_duas_compras_ponderam_pelo_volume(self) -> None:
        """100 a 20 + 100 a 30 = 5000 / 200 = 25,00. Media simples daria o mesmo
        aqui de proposito; o teste seguinte quebra o empate."""
        p = calcular_posicoes(
            [compra("PETR4", "100", "20.00", 1), compra("PETR4", "100", "30.00", 2)]
        )["PETR4"]
        assert p.preco_medio == Decimal("25.00")

    def test_ponderacao_e_por_quantidade_nao_media_simples(self) -> None:
        """300 a 10 + 100 a 20 = 3000 + 2000 = 5000 / 400 = 12,50.
        Media simples daria 15,00 -- este teste distingue os dois."""
        p = calcular_posicoes(
            [compra("PETR4", "300", "10.00", 1), compra("PETR4", "100", "20.00", 2)]
        )["PETR4"]
        assert p.preco_medio == Decimal("12.50")

    def test_venda_nao_altera_o_preco_medio(self) -> None:
        """A regra brasileira, e o erro mais comum de quem implementa FIFO por
        habito: no Brasil a venda reduz quantidade e custo proporcionalmente, e o
        preco medio fica intacto."""
        p = calcular_posicoes(
            [
                compra("PETR4", "100", "20.00", 1),
                compra("PETR4", "100", "30.00", 2),
                venda("PETR4", "100", "40.00", 3),
            ]
        )["PETR4"]
        assert p.quantidade == Decimal(100)
        assert p.preco_medio == Decimal("25.00")  # inalterado
        assert p.custo_total == Decimal("2500.00")

    def test_resultado_realizado_usa_o_preco_medio(self) -> None:
        """(40 - 25) x 100 = 1500."""
        p = calcular_posicoes(
            [
                compra("PETR4", "100", "20.00", 1),
                compra("PETR4", "100", "30.00", 2),
                venda("PETR4", "100", "40.00", 3),
            ]
        )["PETR4"]
        assert p.resultado_realizado == Decimal("1500.00")

    def test_venda_com_prejuizo_da_resultado_negativo(self) -> None:
        p = calcular_posicoes(
            [compra("PETR4", "100", "30.00", 1), venda("PETR4", "100", "20.00", 2)]
        )["PETR4"]
        assert p.resultado_realizado == Decimal("-1000.00")


class TestTaxas:
    def test_taxa_de_compra_entra_no_custo(self) -> None:
        """(100 x 20) + 10 = 2010 / 100 = 20,10.
        A Receita inclui as despesas de aquisicao no custo."""
        p = calcular_posicoes([compra("PETR4", "100", "20.00", 1, taxas="10.00")])["PETR4"]
        assert p.custo_total == Decimal("2010.00")
        assert p.preco_medio == Decimal("20.10")

    def test_taxa_de_venda_reduz_o_resultado(self) -> None:
        """(100 x 30) - 10 - (100 x 20) = 990."""
        p = calcular_posicoes(
            [compra("PETR4", "100", "20.00", 1), venda("PETR4", "100", "30.00", 2, taxas="10.00")]
        )["PETR4"]
        assert p.resultado_realizado == Decimal("990.00")


class TestOrdemCronologica:
    def test_lancamento_retroativo_e_reordenado(self) -> None:
        """Uma transacao antiga informada depois precisa ser processada no lugar
        cronologico dela. Sem a ordenacao, a venda seria avaliada contra uma
        posicao que ainda nao existia."""
        transacoes = [
            venda("PETR4", "50", "30.00", 10),
            compra("PETR4", "100", "20.00", 1),  # lancada por ultimo, ocorreu antes
        ]
        p = calcular_posicoes(transacoes)["PETR4"]
        assert p.quantidade == Decimal(50)
        assert p.preco_medio == Decimal("20.00")

    def test_resultado_independe_da_ordem_da_lista(self) -> None:
        base = [
            compra("PETR4", "100", "20.00", 1),
            compra("PETR4", "100", "30.00", 2),
            venda("PETR4", "80", "35.00", 3),
        ]
        assert calcular_posicoes(base) == calcular_posicoes(list(reversed(base)))


class TestVendaInvalida:
    def test_vender_mais_do_que_tem_e_rejeitado(self) -> None:
        with pytest.raises(VendaSemPosicaoError):
            calcular_posicoes(
                [compra("PETR4", "100", "20.00", 1), venda("PETR4", "101", "30.00", 2)]
            )

    def test_venda_a_descoberto_sem_compra_anterior_e_rejeitada(self) -> None:
        with pytest.raises(VendaSemPosicaoError):
            calcular_posicoes([venda("PETR4", "10", "30.00", 1)])

    def test_erro_informa_o_que_tinha_e_o_que_tentou(self) -> None:
        """Mensagem util: quem recebe o 422 precisa saber qual era a posicao."""
        with pytest.raises(VendaSemPosicaoError) as exc:
            calcular_posicoes(
                [compra("PETR4", "100", "20.00", 1), venda("PETR4", "150", "30.00", 2)]
            )
        assert exc.value.tinha == Decimal(100)
        assert exc.value.tentou == Decimal(150)


class TestVariosAtivos:
    def test_posicoes_sao_independentes(self) -> None:
        p = calcular_posicoes(
            [
                compra("PETR4", "100", "20.00", 1),
                compra("VALE3", "50", "60.00", 1),
                venda("PETR4", "40", "25.00", 2),
            ]
        )
        assert p["PETR4"].quantidade == Decimal(60)
        assert p["VALE3"].quantidade == Decimal(50)
        assert p["VALE3"].resultado_realizado == Decimal(0)


class TestPosicaoZerada:
    def test_vender_tudo_zera_sem_residuo(self) -> None:
        """Sem o tratamento explicito, o arredondamento deixaria um custo residual
        e a proxima compra herdaria um preco medio absurdo."""
        p = calcular_posicoes(
            [
                compra("PETR4", "3", "10.00", 1),
                compra("PETR4", "3", "20.00", 2),
                venda("PETR4", "6", "18.00", 3),
            ]
        )["PETR4"]
        assert p.quantidade == Decimal(0)
        assert p.custo_total == Decimal(0)
        assert p.preco_medio == Decimal(0)
        assert p.esta_zerada

    def test_recompra_apos_zerar_comeca_do_zero(self) -> None:
        p = calcular_posicoes(
            [
                compra("PETR4", "100", "20.00", 1),
                venda("PETR4", "100", "30.00", 2),
                compra("PETR4", "50", "40.00", 3),
            ]
        )["PETR4"]
        assert p.quantidade == Decimal(50)
        assert p.preco_medio == Decimal("40.00")
        assert p.resultado_realizado == Decimal("1000.00")


class TestFracionario:
    def test_quantidade_fracionaria(self) -> None:
        """Cotas de FII e o mercado fracionario da B3 nao sao inteiros."""
        p = calcular_posicoes(
            [compra("HGLG11", "1.5", "160.00", 1), compra("HGLG11", "0.5", "180.00", 2)]
        )["HGLG11"]
        assert p.quantidade == Decimal("2.0")
        assert p.preco_medio == Decimal("165.00")  # (240 + 90) / 2


class TestPrecisaoDecimal:
    def test_nao_acumula_erro_de_ponto_flutuante(self) -> None:
        """Dez compras de 0.1 tem que dar exatamente 1.0.

        Em float, somar 0.1 dez vezes da 0.9999999999999999. Este teste falha na
        hora se alguem trocar Decimal por float em qualquer ponto do caminho.
        """
        transacoes = [compra("PETR4", "0.1", "10.00", d) for d in range(1, 11)]
        p = calcular_posicoes(transacoes)["PETR4"]
        assert p.quantidade == Decimal("1.0")
        assert p.custo_total == Decimal("10.00")
        assert p.preco_medio == Decimal("10.00")
