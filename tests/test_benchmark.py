"""Testes da comparação com CDI / Selic.

O cálculo central é a "curva equivalente": quanto o MESMO dinheiro, aplicado nos
MESMOS dias, renderia na renda fixa. Todos os valores esperados abaixo foram
conferidos à mão.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.bcb import BcbClient
from app.models.benchmark import Indexador
from app.services.benchmark_service import curva_equivalente
from tests.factories import criar_ativo, op, usuario_logado


@dataclass(frozen=True)
class Snap:
    """Snapshot mínimo — só o que a curva precisa."""

    date: date
    custo_total: Decimal
    valor_mercado: Decimal = Decimal(0)


def d(dia: int) -> date:
    return date(2026, 1, dia)


class TestCurvaEquivalente:
    def test_sem_aporte_e_juro_composto_simples(self) -> None:
        """1000 rendendo 1% ao dia por 2 dias = 1000 × 1.01² = 1020,10."""
        snaps = [Snap(d(1), Decimal(1000)), Snap(d(2), Decimal(1000)), Snap(d(3), Decimal(1000))]
        taxas = {d(2): Decimal("0.01"), d(3): Decimal("0.01")}

        curva = curva_equivalente(snaps, taxas)  # type: ignore[arg-type]

        assert curva[0].valor == Decimal(1000)
        assert curva[1].valor == Decimal("1010.00")
        assert curva[2].valor == Decimal("1020.1000")

    def test_aporte_entra_na_curva_no_dia_em_que_aconteceu(self) -> None:
        """O teste que define se a comparação é honesta.

        Aplicar a taxa só sobre o valor inicial subestimaria o benchmark e faria
        a carteira parecer melhor do que foi. Aqui: 1000 rende 1% (→1010), no
        mesmo dia entram mais 1000 de aporte (→2010), e no dia seguinte os 2010
        rendem 1% (→2030,10).
        """
        snaps = [Snap(d(1), Decimal(1000)), Snap(d(2), Decimal(2000)), Snap(d(3), Decimal(2000))]
        taxas = {d(2): Decimal("0.01"), d(3): Decimal("0.01")}

        curva = curva_equivalente(snaps, taxas)  # type: ignore[arg-type]

        assert curva[1].valor == Decimal("2010.00")
        assert curva[2].valor == Decimal("2030.1000")

    def test_aporte_nao_rende_no_proprio_dia(self) -> None:
        """Dinheiro que entrou hoje não estava aplicado ontem. Somar antes de
        aplicar a taxa daria ao benchmark um rendimento que não existiu."""
        snaps = [Snap(d(1), Decimal(1000)), Snap(d(2), Decimal(2000))]
        taxas = {d(2): Decimal("0.10")}

        curva = curva_equivalente(snaps, taxas)  # type: ignore[arg-type]

        # 1000 × 1.10 + 1000 = 2100 (e não (1000+1000) × 1.10 = 2200)
        assert curva[1].valor == Decimal("2100.0")

    def test_retirada_reduz_a_curva(self) -> None:
        """Vender parte da carteira reduz o capital comparado — senão o CDI
        continuaria rendendo sobre dinheiro que o investidor já sacou."""
        snaps = [Snap(d(1), Decimal(2000)), Snap(d(2), Decimal(1000))]
        taxas = {d(2): Decimal("0.01")}

        curva = curva_equivalente(snaps, taxas)  # type: ignore[arg-type]
        assert curva[1].valor == Decimal("1020.00")  # 2000×1.01 − 1000

    def test_dia_sem_taxa_publicada_nao_rende(self) -> None:
        """Fim de semana e feriado não têm CDI. Ausência da data significa
        'não rendeu' — é o comportamento real, não uma aproximação."""
        snaps = [Snap(d(1), Decimal(1000)), Snap(d(2), Decimal(1000))]
        curva = curva_equivalente(snaps, {})  # type: ignore[arg-type]
        assert curva[1].valor == Decimal(1000)

    def test_lista_vazia_devolve_vazio(self) -> None:
        assert curva_equivalente([], {}) == []

    def test_comeca_no_custo_e_nao_no_valor_de_mercado(self) -> None:
        """A comparação parte do que foi INVESTIDO. Partir do valor de mercado
        daria de graça ao benchmark o lucro que a carteira já tinha."""
        snaps = [Snap(d(1), Decimal(1000), valor_mercado=Decimal(1500))]
        curva = curva_equivalente(snaps, {})  # type: ignore[arg-type]
        assert curva[0].valor == Decimal(1000)


class TestClienteBcb:
    async def test_converte_percentual_em_fracao(self) -> None:
        """O BCB publica 0.051660 (% ao dia); guardamos 0.00051660.

        A conversão acontece uma vez, na fronteira. Guardar a unidade do
        fornecedor obrigaria todo cálculo adiante a lembrar de dividir por 100 --
        e um dia alguém esquece, errando por 100×  sem estourar nada.
        """

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"data": "20/08/2026", "valor": "0.051660"}])

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            taxas = await BcbClient(http).taxas(Indexador.CDI, d(1), d(31))

        assert taxas[date(2026, 8, 20)] == Decimal("0.00051660")

    async def test_envia_data_no_formato_do_sgs(self) -> None:
        """O SGS só aceita dd/MM/yyyy. Mandar ISO devolve a série inteira desde
        1986, em silêncio — alguns megabytes por um formato errado."""
        capturada: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            capturada.append(request)
            return httpx.Response(200, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            await BcbClient(http).taxas(Indexador.CDI, date(2026, 8, 1), date(2026, 8, 31))

        assert capturada[0].url.params["dataInicial"] == "01/08/2026"
        assert capturada[0].url.params["dataFinal"] == "31/08/2026"

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            [{"data": "20/08/2026"}],
            [{"valor": "0.05"}],
            [{"data": "nao-e-data", "valor": "0.05"}],
            [{"data": "20/08/2026", "valor": "nao-e-numero"}],
            ["string solta"],
        ],
    )
    async def test_ignora_payload_malformado(self, payload: object) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            assert await BcbClient(http).taxas(Indexador.CDI, d(1), d(31)) == {}

    async def test_falha_de_rede_nao_propaga(self) -> None:
        """Comparar com o CDI é um extra: a carteira continua sendo exibida sem
        ele. O BCB fora do ar não pode derrubar o gráfico."""

        def responder(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("estourou", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            assert await BcbClient(http).taxas(Indexador.CDI, d(1), d(31)) == {}


class TestRotaDeEvolucao:
    async def test_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.get("/portfolio/evolution")).status_code == 401

    async def test_sem_historico_explica(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        corpo = (await client.get("/portfolio/evolution", headers=h)).json()
        assert corpo["benchmark"] is None
        assert corpo["motivo"] is not None

    async def test_indexador_invalido_e_recusado(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        resp = await client.get("/portfolio/evolution?indexador=poupanca", headers=h)
        assert resp.status_code == 422

    async def test_nao_mistura_carteiras(self, client: AsyncClient, db: AsyncSession) -> None:
        from tests.factories import segunda_conta

        await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        await client.post("/transactions", json=op(), headers=dono)
        outro = await segunda_conta(client)

        assert (await client.get("/portfolio/evolution", headers=outro)).json()["pontos"] == []


class TestCurvaDeRentabilidade:
    """Retorno ponderado pelo tempo (TWR) — o número que fundos reportam.

    É a única medida comparável com o CDI acumulado: ela isola o efeito do
    mercado dos aportes.
    """

    def test_comeca_em_zero(self) -> None:
        """As duas curvas partem do mesmo ponto, senão a comparação visual
        mediria a diferença de partida, não a de desempenho."""
        from app.services.benchmark_service import curva_rentabilidade

        snaps = [Snap(d(1), Decimal(1000), Decimal(1500))]
        curva = curva_rentabilidade(snaps, {d(1): Decimal("0.01")})  # type: ignore[arg-type]
        assert curva[0].carteira == Decimal(0)
        assert curva[0].benchmark == Decimal(0)

    def test_aporte_nao_dilui_a_rentabilidade(self) -> None:
        """O teste que justifica o TWR.

        Dia 1: investe 1.000, vale 1.100 → +10%.
        Dia 2: aporta 1.000 e o mercado não mexe: vale 2.100, custo 2.000.

        O percentual ingênuo (valor/custo − 1) diria +5% — a rentabilidade teria
        "caído pela metade" sem o mercado ter mexido. O TWR mantém +10%.
        """
        from app.services.benchmark_service import curva_rentabilidade

        snaps = [
            Snap(d(1), Decimal(1000), Decimal(1100)),
            Snap(d(2), Decimal(2000), Decimal(2100)),
        ]
        curva = curva_rentabilidade(snaps, {})  # type: ignore[arg-type]

        ingenuo = Decimal(2100) / Decimal(2000) - 1
        assert ingenuo == Decimal("0.05")  # o que NÃO queremos
        assert curva[1].carteira == Decimal(0)  # nenhum ganho novo no dia 2

    def test_ganho_de_mercado_aparece(self) -> None:
        """1.000 → 1.100 sem aporte = +10%."""
        from app.services.benchmark_service import curva_rentabilidade

        snaps = [
            Snap(d(1), Decimal(1000), Decimal(1000)),
            Snap(d(2), Decimal(1000), Decimal(1100)),
        ]
        curva = curva_rentabilidade(snaps, {})  # type: ignore[arg-type]
        assert curva[1].carteira == pytest.approx(Decimal("0.10"))

    def test_retornos_compoem(self) -> None:
        """+10% e depois +10% dão +21%, não +20%."""
        from app.services.benchmark_service import curva_rentabilidade

        snaps = [
            Snap(d(1), Decimal(1000), Decimal(1000)),
            Snap(d(2), Decimal(1000), Decimal(1100)),
            Snap(d(3), Decimal(1000), Decimal(1210)),
        ]
        curva = curva_rentabilidade(snaps, {})  # type: ignore[arg-type]
        assert curva[2].carteira == pytest.approx(Decimal("0.21"))

    def test_prejuizo_da_rentabilidade_negativa(self) -> None:
        from app.services.benchmark_service import curva_rentabilidade

        snaps = [
            Snap(d(1), Decimal(1000), Decimal(1000)),
            Snap(d(2), Decimal(1000), Decimal(900)),
        ]
        curva = curva_rentabilidade(snaps, {})  # type: ignore[arg-type]
        assert curva[1].carteira == pytest.approx(Decimal("-0.10"))

    def test_benchmark_e_a_taxa_acumulada_pura(self) -> None:
        """O CDI não tem aporte: a curva percentual dele é só o produto das
        taxas diárias. 1% e 1% dão 2,01%."""
        from app.services.benchmark_service import curva_rentabilidade

        snaps = [
            Snap(d(1), Decimal(1000), Decimal(1000)),
            Snap(d(2), Decimal(1000), Decimal(1000)),
            Snap(d(3), Decimal(1000), Decimal(1000)),
        ]
        taxas = {d(2): Decimal("0.01"), d(3): Decimal("0.01")}
        curva = curva_rentabilidade(snaps, taxas)  # type: ignore[arg-type]

        assert curva[2].benchmark == pytest.approx(Decimal("0.0201"))

    def test_carteira_zerada_e_reaberta_nao_divide_por_zero(self) -> None:
        """Acontece de verdade: vender tudo e comprar de novo depois. Sem base
        para calcular retorno, o dia não rende — em vez de estourar."""
        from app.services.benchmark_service import curva_rentabilidade

        snaps = [
            Snap(d(1), Decimal(1000), Decimal(1000)),
            Snap(d(2), Decimal(0), Decimal(0)),
            Snap(d(3), Decimal(500), Decimal(500)),
        ]
        curva = curva_rentabilidade(snaps, {})  # type: ignore[arg-type]
        assert len(curva) == 3
        assert all(p.carteira is not None for p in curva)

    def test_sem_taxas_o_benchmark_e_nulo(self) -> None:
        """Nulo, não zero: "não sei" e "rendeu 0%" são coisas diferentes, e um
        gráfico que desenha zero onde não há dado mente."""
        from app.services.benchmark_service import curva_rentabilidade

        snaps = [Snap(d(1), Decimal(1000), Decimal(1000)), Snap(d(2), Decimal(1000), Decimal(1100))]
        curva = curva_rentabilidade(snaps, {})  # type: ignore[arg-type]
        assert all(p.benchmark is None for p in curva)
