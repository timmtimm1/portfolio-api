"""Testes da comparação com CDI / Selic.

O cálculo central é a "curva equivalente": quanto o MESMO dinheiro, aplicado nos
MESMOS dias, renderia na renda fixa. Todos os valores esperados abaixo foram
conferidos à mão.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.bcb import BcbClient
from app.clients.ibov import IbovClient
from app.models.benchmark import BenchmarkRate, Indexador
from app.services.benchmark_service import curva_equivalente, curva_rentabilidade
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


class TestClienteBcbIpca:
    """O IPCA (série 433) sai uma vez por MÊS; CDI e Selic saem por dia útil.
    `_espalhar_mensal_em_diario` converte um número no outro na borda, para que
    `curva_equivalente` e `taxas_do_periodo` não precisem saber da diferença.
    """

    async def test_usa_a_serie_433(self) -> None:
        capturada: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            capturada.append(request)
            return httpx.Response(200, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            await BcbClient(http).taxas(Indexador.IPCA, date(2026, 7, 1), date(2026, 7, 31))

        assert "bcdata.sgs.433" in str(capturada[0].url)

    async def test_arredonda_a_janela_para_o_dia_1_do_mes(self) -> None:
        """O SGS data cada valor de IPCA no dia 1 do mês de referência. Pedir a
        partir do dia 20 sem arredondar excluiria o valor do próprio mês
        corrente -- ele está datado "01/08", antes do início pedido."""
        capturada: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            capturada.append(request)
            return httpx.Response(200, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            await BcbClient(http).taxas(Indexador.IPCA, date(2026, 8, 20), date(2026, 8, 31))

        assert capturada[0].url.params["dataInicial"] == "01/08/2026"

    async def test_taxa_mensal_vira_a_mesma_taxa_diaria_em_todo_dia_do_mes(self) -> None:
        """0,07% em julho (31 dias) vira uma taxa diária repetida 31 vezes --
        um valor por dia de CALENDÁRIO, não por dia útil."""

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"data": "01/07/2026", "valor": "0.07"}])

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            taxas = await BcbClient(http).taxas(Indexador.IPCA, date(2026, 7, 1), date(2026, 7, 31))

        assert set(taxas) == {date(2026, 7, dia) for dia in range(1, 32)}
        assert len({round(v, 12) for v in taxas.values()}) == 1

    async def test_a_composicao_diaria_reproduz_a_taxa_mensal(self) -> None:
        """A prova de que o espalhamento é matematicamente correto: compor a
        taxa diária pelos dias do mês tem que devolver a taxa mensal original
        -- não uma aproximação grosseira como dividir por 28."""

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"data": "01/02/2026", "valor": "0.70"}])

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            taxas = await BcbClient(http).taxas(Indexador.IPCA, date(2026, 2, 1), date(2026, 2, 28))

        taxa_diaria = next(iter(taxas.values()))
        acumulado = (1 + taxa_diaria) ** 28  # fevereiro de 2026 -- ano não bissexto
        assert abs(acumulado - Decimal("1.0070")) < Decimal("0.0000001")

    async def test_ano_bissexto_usa_29_dias_em_fevereiro(self) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"data": "01/02/2024", "valor": "0.50"}])

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            taxas = await BcbClient(http).taxas(Indexador.IPCA, date(2024, 2, 1), date(2024, 2, 29))

        assert len(taxas) == 29
        assert date(2024, 2, 29) in taxas

    async def test_deflacao_impossivel_e_ignorada_sem_lancar(self) -> None:
        """-150% ao mês tornaria o fator (1 + taxa) negativo, e `ln()` de um
        número não positivo lança `InvalidOperation`. Um valor absurdo do
        fornecedor não pode derrubar a resposta inteira -- mesma regra do
        parser de payload malformado logo acima."""

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"data": "01/07/2026", "valor": "-150"}])

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            assert await BcbClient(http).taxas(Indexador.IPCA, d(1), d(31)) == {}

    async def test_a_gravacao_no_banco_aceita_ipca(self, db: AsyncSession) -> None:
        """`indexador` tem um CHECK no banco que so aceitava 'cdi' e 'selic'
        (migration `ed28216483b2`). Sem a migration `091c1a39886d`, esta
        gravacao falharia com uma violacao de constraint -- em producao, nao
        aqui: os testes recriam o schema rodando as migrations de verdade
        (ver `tests/conftest.py`), entao um erro na migration derruba a
        suite inteira antes mesmo deste teste rodar."""
        from functools import partial

        from app.services.benchmark_service import taxas_do_periodo

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"data": "01/07/2026", "valor": "0.07"}])

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            cliente = BcbClient(http)
            taxas = await taxas_do_periodo(
                db,
                Indexador.IPCA,
                date(2026, 7, 1),
                date(2026, 7, 31),
                buscar=partial(cliente.taxas, Indexador.IPCA),
            )

        assert len(taxas) == 31

        linha = await db.get(BenchmarkRate, (Indexador.IPCA, date(2026, 7, 15)))
        assert linha is not None
        # A coluna e Numeric(14,10); o valor em memoria, vindo de ln()/exp(),
        # tem mais casas do que isso guarda. Comparar exigiria o mesmo
        # arredondamento que a coluna ja aplicou sozinha ao gravar.
        esperado = taxas[date(2026, 7, 15)].quantize(Decimal("0.0000000001"))
        assert linha.rate == esperado


class TestClienteIbov:
    """Ibovespa nao vem do BCB -- a serie 7 do SGS foi descontinuada em 2019
    (pedir dados de hoje devolve "Value(s) not found", conferido a mao). Vem do
    Yahoo Finance como fechamento diario do indice, convertido aqui em variacao
    percentual dia a dia -- a mesma forma de taxa que CDI/Selic/IPCA, so que
    calculada a partir de um NIVEL em vez de publicada pronta."""

    @staticmethod
    def _ts(ano: int, mes: int, dia: int) -> int:
        # 15h UTC = meio-dia em Brasilia, bem dentro do pregao (13h-21h UTC) --
        # nunca cruza a meia-noite UTC, entao a data extraida e sempre esta
        # mesma independente de fuso.
        return int(datetime(ano, mes, dia, 15, 0, tzinfo=UTC).timestamp())

    def _payload(self, pares: list[tuple[int, int, int, float | None]]) -> dict[str, object]:
        return {
            "chart": {
                "result": [
                    {
                        "timestamp": [self._ts(a, m, d) for a, m, d, _ in pares],
                        "indicators": {"quote": [{"close": [c for *_, c in pares]}]},
                    }
                ]
            }
        }

    async def test_variacao_e_o_fechamento_de_hoje_sobre_o_de_ontem(self) -> None:
        payload = self._payload([(2026, 8, 3, 100000.0), (2026, 8, 4, 101000.0)])

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            variacoes = await IbovClient(http).variacoes_diarias(date(2026, 8, 3), date(2026, 8, 4))

        assert variacoes[date(2026, 8, 4)] == Decimal("101000.0") / Decimal("100000.0") - 1

    async def test_usa_o_ticker_do_ibovespa(self) -> None:
        capturada: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            capturada.append(request)
            return httpx.Response(200, json=self._payload([]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            await IbovClient(http).variacoes_diarias(date(2026, 8, 1), date(2026, 8, 31))

        assert "%5EBVSP" in str(capturada[0].url)

    async def test_pula_fechamento_nulo(self) -> None:
        """O Yahoo intercala `None` num pregao sem fechamento registrado."""
        payload = self._payload(
            [(2026, 8, 3, 100000.0), (2026, 8, 4, None), (2026, 8, 5, 102000.0)]
        )

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            variacoes = await IbovClient(http).variacoes_diarias(date(2026, 8, 3), date(2026, 8, 5))

        # Sem o dia 4, a variacao do dia 5 e sobre o ultimo fechamento REAL,
        # o do dia 3 -- nao um salto contra `None`, nem um dia perdido.
        assert date(2026, 8, 4) not in variacoes
        assert variacoes[date(2026, 8, 5)] == Decimal("102000.0") / Decimal("100000.0") - 1

    async def test_usa_o_fechamento_anterior_a_janela_pedida(self) -> None:
        """Pedir so o dia 4 ainda precisa do fechamento do dia 3 para calcular
        a variacao -- por isso o cliente busca uma janela maior que a pedida
        (`RANGE`) e so FILTRA o resultado, sem descartar o ponto anterior."""
        payload = self._payload([(2026, 8, 3, 100000.0), (2026, 8, 4, 105000.0)])

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            variacoes = await IbovClient(http).variacoes_diarias(date(2026, 8, 4), date(2026, 8, 4))

        assert date(2026, 8, 3) not in variacoes  # fora da janela PEDIDA
        assert variacoes[date(2026, 8, 4)] == Decimal("105000.0") / Decimal("100000.0") - 1

    async def test_falha_de_rede_nao_propaga(self) -> None:
        """Comparar com um indice e um extra: a carteira continua sendo exibida
        sem ele. O Yahoo fora do ar nao pode derrubar o grafico."""

        def responder(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("estourou", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            resultado = await IbovClient(http).variacoes_diarias(
                date(2026, 8, 1), date(2026, 8, 31)
            )
        assert resultado == {}

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            {"chart": {}},
            {"chart": {"result": []}},
            {
                "chart": {
                    "result": [
                        {"timestamp": "nao-e-lista", "indicators": {"quote": [{"close": []}]}}
                    ]
                }
            },
        ],
    )
    async def test_ignora_payload_malformado(self, payload: object) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            assert (
                await IbovClient(http).variacoes_diarias(date(2026, 8, 1), date(2026, 8, 31)) == {}
            )

    async def test_a_gravacao_no_banco_aceita_ibov(self, db: AsyncSession) -> None:
        """Mesma prova da migration que `TestClienteBcbIpca` fez para 'ipca':
        sem a migration `bc35731d22ea`, esta gravacao violaria o CHECK de
        `indexador` -- e os testes rodam as migrations de verdade, entao um
        erro nela derrubaria a suite inteira antes deste teste."""
        from app.services.benchmark_service import taxas_do_periodo

        payload = self._payload([(2026, 7, 31, 100000.0), (2026, 8, 3, 101000.0)])

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            taxas = await taxas_do_periodo(
                db,
                Indexador.IBOV,
                date(2026, 8, 3),
                date(2026, 8, 3),
                buscar=IbovClient(http).variacoes_diarias,
            )

        assert len(taxas) == 1
        linha = await db.get(BenchmarkRate, (Indexador.IBOV, date(2026, 8, 3)))
        assert linha is not None
        assert linha.rate == taxas[date(2026, 8, 3)].quantize(Decimal("0.0000000001"))


class TestRotaDeEvolucao:
    async def test_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.get("/portfolio/evolution")).status_code == 401

    async def test_sem_historico_explica(self, client: AsyncClient) -> None:
        """Quem PEDIU a comparacao e nao a recebeu merece saber por que.

        O indexador vai explicito: desde que a comparacao virou opt-in, omiti-lo
        significa "nao quero comparar" -- e ai nao ha nada a explicar.
        """
        _, h = await usuario_logado(client)
        corpo = (await client.get("/portfolio/evolution?indexador=cdi", headers=h)).json()
        assert corpo["benchmark"] is None
        assert corpo["motivo"] is not None

    async def test_sem_indexador_nao_compara_com_nada(self, client: AsyncClient) -> None:
        """Regressao: "sem comparacao" mostrava a linha do CDI assim mesmo.

        O parametro era `Indexador` com default `CDI`, entao o cliente nao tinha
        como dizer "nenhum": omitir significava "use o default". A opcao existia
        na tela e nao existia na API.
        """
        _, h = await usuario_logado(client)
        corpo = (await client.get("/portfolio/evolution", headers=h)).json()
        assert corpo["benchmark"] is None
        assert corpo["comparacao"] is None
        # Sem motivo: nao houve falha nenhuma a justificar.
        assert corpo["motivo"] is None

    async def test_sem_indexador_ainda_devolve_a_carteira(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Tirar a comparacao nao pode tirar o grafico junto: a serie da propria
        carteira continua inteira, so a linha do indexador some."""
        from app.models.snapshot import PortfolioSnapshot
        from tests.factories import carteira_de

        email, h = await usuario_logado(client)
        carteira = await carteira_de(db, email)
        for dia, valor in ((date(2026, 8, 10), 1000), (date(2026, 8, 11), 1100)):
            db.add(
                PortfolioSnapshot(
                    portfolio_id=carteira.id,
                    user_id=carteira.user_id,
                    date=dia,
                    custo_total=1000,
                    valor_mercado=valor,
                    resultado_nao_realizado=valor - 1000,
                    resultado_realizado=0,
                    ativos=1,
                    ativos_sem_cotacao=0,
                )
            )
        await db.commit()

        corpo = (await client.get("/portfolio/evolution", headers=h)).json()
        assert corpo["benchmark"] is None
        assert len(corpo["pontos"]) == 2
        assert len(corpo["rentabilidade"]) == 2
        assert all(p["benchmark"] is None for p in corpo["rentabilidade"])

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


class TestProventosNoRetorno:
    """Retorno TOTAL, não só valorização.

    Na data-com o preço cai aproximadamente o valor do provento -- a empresa
    distribuiu caixa, então vale menos. O snapshot registra essa queda. Se o
    provento não voltar ao numerador, a carteira aparece perdendo exatamente o
    que ganhou, e uma carteira de dividendo fica cronicamente subestimada.
    """

    @staticmethod
    def _snaps() -> list[Snap]:
        """Dois dias, sem aporte novo. O valor de mercado cai de 1000 para 990
        -- exatamente o provento de R$ 10 que foi distribuído."""
        return [
            Snap(d(1), Decimal(1000), valor_mercado=Decimal(1000)),
            Snap(d(2), Decimal(1000), valor_mercado=Decimal(990)),
        ]

    def test_sem_proventos_a_queda_da_data_com_vira_prejuizo(self) -> None:
        """O comportamento ANTIGO, que era errado para quem recebe dividendo:
        -1% de retorno num dia em que o investidor não perdeu nada."""
        curva = curva_rentabilidade(self._snaps(), {})  # type: ignore[arg-type]
        assert curva[-1].carteira == Decimal("-0.01")

    def test_com_provento_o_retorno_volta_a_zero(self) -> None:
        """(990 + 10 - 0) / 1000 - 1 = 0. O dinheiro saiu da cotação e entrou
        na conta -- não evaporou."""
        curva = curva_rentabilidade(
            self._snaps(),  # type: ignore[arg-type]
            {},
            {d(2): Decimal(10)},
        )
        assert curva[-1].carteira == Decimal(0)

    def test_provento_em_dia_sem_queda_e_ganho_real(self) -> None:
        """Preço estável e provento recebido = retorno positivo, e não zero."""
        snaps = [
            Snap(d(1), Decimal(1000), valor_mercado=Decimal(1000)),
            Snap(d(2), Decimal(1000), valor_mercado=Decimal(1000)),
        ]
        curva = curva_rentabilidade(snaps, {}, {d(2): Decimal(50)})  # type: ignore[arg-type]
        assert curva[-1].carteira == Decimal("0.05")

    def test_proventos_de_dias_diferentes_compoem(self) -> None:
        """Retorno acumulado é produto, não soma: 1,01 × 1,01 = 1,0201."""
        snaps = [
            Snap(d(1), Decimal(1000), valor_mercado=Decimal(1000)),
            Snap(d(2), Decimal(1000), valor_mercado=Decimal(1000)),
            Snap(d(3), Decimal(1000), valor_mercado=Decimal(1000)),
        ]
        curva = curva_rentabilidade(
            snaps,  # type: ignore[arg-type]
            {},
            {d(2): Decimal(10), d(3): Decimal(10)},
        )
        assert curva[-1].carteira == Decimal("0.0201")

    def test_dia_sem_provento_nao_e_afetado(self) -> None:
        """Só a data-com recebe o crédito. Um dicionário de proventos não pode
        vazar para os outros dias."""
        snaps = [
            Snap(d(1), Decimal(1000), valor_mercado=Decimal(1000)),
            Snap(d(2), Decimal(1000), valor_mercado=Decimal(1000)),
            Snap(d(3), Decimal(1000), valor_mercado=Decimal(1000)),
        ]
        curva = curva_rentabilidade(snaps, {}, {d(2): Decimal(10)})  # type: ignore[arg-type]
        assert curva[1].carteira == Decimal("0.01")
        assert curva[2].carteira == Decimal("0.01")  # inalterado no dia 3

    def test_omitir_proventos_mantem_o_comportamento_anterior(self) -> None:
        """O parâmetro é opcional: quem chama sem ele mede só valorização, como
        antes. Isso protege os outros chamadores de mudarem de significado sem
        ninguém perceber."""
        snaps = self._snaps()
        assert curva_rentabilidade(snaps, {}) == curva_rentabilidade(snaps, {}, None)  # type: ignore[arg-type]

    def test_provento_e_somado_junto_com_o_aporte_do_dia(self) -> None:
        """Aporte e provento no mesmo dia não podem se anular. O aporte SAI do
        numerador (dinheiro novo não é lucro); o provento ENTRA (é lucro)."""
        snaps = [
            Snap(d(1), Decimal(1000), valor_mercado=Decimal(1000)),
            # Aportou 500, e o valor foi para 1510: 1000 + 500 de aporte + 10
            # de mercado. Recebeu ainda R$ 10 de provento.
            Snap(d(2), Decimal(1500), valor_mercado=Decimal(1510)),
        ]
        curva = curva_rentabilidade(snaps, {}, {d(2): Decimal(10)})  # type: ignore[arg-type]
        # (1510 + 10 - 500) / 1000 - 1 = 0,02
        assert curva[-1].carteira == Decimal("0.02")
