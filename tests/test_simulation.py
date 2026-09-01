"""Testes da projeção por Monte Carlo.

Simulação é código que sempre "funciona": ela devolve números plausíveis para
qualquer entrada, certa ou errada. Por isso os testes aqui atacam **propriedades
que a matemática obriga**, e não valores sorteados:

- sem volatilidade, o resultado é determinístico e conferível na mão
- os percentis nunca se cruzam
- a média dos cenários bate com o retorno pedido (é o que a correção de Itô faz)
- a mesma semente devolve o mesmo resultado
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from app.services.simulation import CENARIOS_MAXIMO, Projecao, projetar


class TestSemVolatilidade:
    """Volatilidade zero elimina o sorteio: todo cenário é o mesmo caminho, e
    o resultado pode ser conferido com uma calculadora."""

    def test_juro_composto_puro(self) -> None:
        """10.000 a 10% ao ano, um ano, sem aporte = 11.000."""
        p = projetar(Decimal(10000), retorno_anual=0.10, volatilidade_anual=0.0, anos=1, semente=1)
        assert p.pontos[-1].p50 == Decimal("11000.00")

    def test_todos_os_percentis_coincidem(self) -> None:
        """Sem incerteza não há faixa: p5 e p95 são o mesmo número."""
        p = projetar(Decimal(10000), retorno_anual=0.10, volatilidade_anual=0.0, anos=1, semente=1)
        ponto = p.pontos[-1]
        assert ponto.p5 == ponto.p25 == ponto.p50 == ponto.p75 == ponto.p95

    def test_aporte_mensal_entra_todo_mes(self) -> None:
        """Sem rendimento nenhum, 12 aportes de 100 sobre 1.000 dão 2.200."""
        p = projetar(
            Decimal(1000),
            retorno_anual=0.0,
            volatilidade_anual=0.0,
            anos=1,
            aporte_mensal=Decimal(100),
            semente=1,
        )
        assert p.pontos[-1].p50 == Decimal("2200.00")
        assert p.total_aportado == Decimal(2200)

    def test_o_aporte_entra_depois_do_rendimento(self) -> None:
        """Dinheiro que entra no mês não rendeu aquele mês inteiro.

        A ordem importa e a diferença é silenciosa: 1.000 a 10% ao ano com
        aportes de 100 dão R$ 2.354,05 se o aporte entra DEPOIS do rendimento,
        e R$ 2.364,05 se entra antes. Dez reais em um ano, e crescendo -- um
        rendimento sobre dinheiro que ainda não estava aplicado.

        É a mesma regra que `curva_equivalente` já segue na comparação com o
        indexador; aqui ela precisava valer também para o futuro.
        """
        p = projetar(
            Decimal(1000),
            retorno_anual=0.10,
            volatilidade_anual=0.0,
            anos=1,
            aporte_mensal=Decimal(100),
            semente=1,
        )
        assert p.pontos[-1].p50 == Decimal("2354.05")

    def test_o_primeiro_ponto_e_o_valor_de_hoje(self) -> None:
        """O gráfico precisa começar no que a pessoa tem agora, não no mês 1."""
        p = projetar(
            Decimal("11502.40"), retorno_anual=0.30, volatilidade_anual=0.2, anos=3, semente=1
        )
        assert p.pontos[0].mes == 0
        assert p.pontos[0].p50 == Decimal("11502.40")

    def test_um_ponto_por_mes_mais_o_inicial(self) -> None:
        p = projetar(Decimal(1000), retorno_anual=0.1, volatilidade_anual=0.1, anos=6, semente=1)
        assert len(p.pontos) == 6 * 12 + 1
        assert [x.mes for x in p.pontos] == list(range(73))


class TestComVolatilidade:
    def test_os_percentis_nunca_se_cruzam(self) -> None:
        """p5 <= p25 <= p50 <= p75 <= p95, em TODOS os meses. Se cruzarem, a
        faixa desenhada no gráfico vira um nó."""
        p = projetar(
            Decimal(10000),
            retorno_anual=0.30,
            volatilidade_anual=0.25,
            anos=5,
            aporte_mensal=Decimal(500),
            semente=42,
        )
        for x in p.pontos:
            assert x.p5 <= x.p25 <= x.p50 <= x.p75 <= x.p95, f"cruzaram no mês {x.mes}"

    def test_mais_volatilidade_abre_a_faixa(self) -> None:
        """É a propriedade que dá sentido ao gráfico: risco maior, incerteza
        maior. Se não abrisse, a simulação não estaria medindo nada."""
        comum = dict(retorno_anual=0.15, anos=5, semente=7, cenarios=5000)
        calma = projetar(Decimal(10000), volatilidade_anual=0.10, **comum)  # type: ignore[arg-type]
        agitada = projetar(Decimal(10000), volatilidade_anual=0.40, **comum)  # type: ignore[arg-type]

        def faixa(projecao: Projecao) -> Decimal:
            return projecao.pontos[-1].p95 - projecao.pontos[-1].p5

        assert faixa(agitada) > faixa(calma)

    def test_o_valor_nunca_fica_negativo(self) -> None:
        """Uma carteira pode ir a zero, não a menos de zero. É o motivo de o
        modelo sortear retorno em LOG: com retorno aritmético e volatilidade
        alta, cenários negativos aparecem em horizonte longo."""
        p = projetar(
            Decimal(1000), retorno_anual=-0.20, volatilidade_anual=0.90, anos=10, semente=3
        )
        assert all(x.p5 >= 0 for x in p.pontos)

    def test_a_media_dos_cenarios_bate_com_o_retorno_pedido(self) -> None:
        """O que a correção de Itô garante.

        Sem o `-s²/2`, a média dos cenários ficaria ACIMA do retorno esperado:
        a simulação renderia mais que a premissa, sozinha, e o gráfico
        prometeria um ganho que ninguém pediu.

        Com vol de 20% e retorno de 10% ao ano, a mediana em 1 ano deve ficar
        perto de 10.000 × 1,10 dividido pelo viés da lognormal -- na prática,
        a MEDIA dos cenários é que fica em ~11.000.
        """
        p = projetar(
            Decimal(10000),
            retorno_anual=0.10,
            volatilidade_anual=0.20,
            anos=1,
            cenarios=40000,
            semente=11,
        )
        # A mediana da lognormal fica abaixo da média por exp(-s²·12/2).
        s_anual = 0.20
        esperado_mediana = 10000 * 1.10 * math.exp(-(s_anual**2) / 2)
        # Tolerância de 1%, não 3%: sem a correção de Itô a mediana sobe
        # 2,02%, e 3% deixava a mutação passar. O ruído de Monte Carlo com
        # 40 mil cenários fica na terceira casa, bem abaixo de 1%.
        assert abs(float(p.pontos[-1].p50) - esperado_mediana) < esperado_mediana * 0.01


class TestReprodutibilidade:
    def test_a_mesma_semente_da_o_mesmo_resultado(self) -> None:
        """Sem semente fixa, dois cliques no mesmo botão dariam números
        diferentes e o usuário não saberia se algo mudou ou se é o sorteio."""
        args = dict(retorno_anual=0.2, volatilidade_anual=0.3, anos=3, semente=99)
        a = projetar(Decimal(5000), **args)  # type: ignore[arg-type]
        b = projetar(Decimal(5000), **args)  # type: ignore[arg-type]
        assert [x.p50 for x in a.pontos] == [x.p50 for x in b.pontos]

    def test_sementes_diferentes_dao_resultados_diferentes(self) -> None:
        a = projetar(Decimal(5000), retorno_anual=0.2, volatilidade_anual=0.3, anos=3, semente=1)
        b = projetar(Decimal(5000), retorno_anual=0.2, volatilidade_anual=0.3, anos=3, semente=2)
        assert [x.p50 for x in a.pontos] != [x.p50 for x in b.pontos]


class TestProbabilidade:
    def test_sem_risco_e_com_retorno_positivo_a_chance_e_total(self) -> None:
        p = projetar(Decimal(10000), retorno_anual=0.10, volatilidade_anual=0.0, anos=5, semente=1)
        assert p.prob_acima_do_aportado == 1.0

    def test_com_retorno_zero_e_sem_risco_ninguem_supera_o_aportado(self) -> None:
        """Guardar embaixo do colchão dá exatamente o mesmo resultado."""
        p = projetar(
            Decimal(10000),
            retorno_anual=0.0,
            volatilidade_anual=0.0,
            anos=5,
            aporte_mensal=Decimal(100),
            semente=1,
        )
        assert p.prob_acima_do_aportado == 0.0

    def test_fica_entre_zero_e_um(self) -> None:
        p = projetar(Decimal(10000), retorno_anual=0.15, volatilidade_anual=0.35, anos=8, semente=5)
        assert 0.0 <= p.prob_acima_do_aportado <= 1.0


class TestEntradasInvalidas:
    """Cada uma destas produziria um gráfico plausível e sem sentido."""

    def test_horizonte_zero(self) -> None:
        with pytest.raises(ValueError, match="pelo menos um ano"):
            projetar(Decimal(1000), retorno_anual=0.1, volatilidade_anual=0.1, anos=0)

    def test_volatilidade_negativa(self) -> None:
        with pytest.raises(ValueError, match="negativa"):
            projetar(Decimal(1000), retorno_anual=0.1, volatilidade_anual=-0.1, anos=1)

    def test_retorno_de_menos_cem_por_cento(self) -> None:
        """`log1p(-1)` é -infinito: a conta explodiria em vez de recusar."""
        with pytest.raises(ValueError, match="zera a carteira"):
            projetar(Decimal(1000), retorno_anual=-1.0, volatilidade_anual=0.1, anos=1)

    def test_cenarios_acima_do_teto(self) -> None:
        with pytest.raises(ValueError, match="entre 1 e"):
            projetar(
                Decimal(1000),
                retorno_anual=0.1,
                volatilidade_anual=0.1,
                anos=1,
                cenarios=CENARIOS_MAXIMO + 1,
            )
