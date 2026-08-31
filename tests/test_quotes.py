"""Testes do cache de cotacoes e do resumo da carteira."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quote import PriceQuote
from tests.conftest import ProvedorFake
from tests.factories import criar_ativo, op, segunda_conta, usuario_logado


async def _carteira(client: AsyncClient, db: AsyncSession) -> dict[str, str]:
    """Usuario com 100 PETR4 a R$ 20,00 (custo 2000)."""
    await criar_ativo(db, ticker="PETR4")
    _, h = await usuario_logado(client)
    await client.post("/transactions", json=op(price="20.00"), headers=h)
    return h


class TestCache:
    async def test_primeira_chamada_busca_no_fornecedor(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        h = await _carteira(client, db)
        provedor.precos = {"PETR4": "25.00"}

        resp = await client.get("/portfolio/summary", headers=h)

        assert resp.status_code == 200
        assert provedor.chamadas == [["PETR4"]]

    async def test_segunda_chamada_nao_toca_o_fornecedor(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """O teste central desta etapa.

        Sem cache, cada carregamento de tela viraria uma chamada externa: a cota
        gratuita de 15 mil requisicoes/mes evaporaria com poucos usuarios, e o
        tempo de resposta passaria a depender de um terceiro.
        """
        h = await _carteira(client, db)
        provedor.precos = {"PETR4": "25.00"}

        await client.get("/portfolio/summary", headers=h)
        provedor.chamadas.clear()
        await client.get("/portfolio/summary", headers=h)

        assert provedor.chamadas == []

    async def test_cache_vencido_dispara_nova_busca(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        h = await _carteira(client, db)
        provedor.precos = {"PETR4": "25.00"}
        await client.get("/portfolio/summary", headers=h)

        # Envelhece a entrada para alem do TTL (900s).
        linha = (await db.execute(select(PriceQuote))).scalar_one()
        linha.fetched_at = datetime.now(UTC) - timedelta(seconds=1000)
        await db.commit()

        provedor.chamadas.clear()
        provedor.precos = {"PETR4": "30.00"}
        corpo = (await client.get("/portfolio/summary", headers=h)).json()

        assert provedor.chamadas == [["PETR4"]]
        assert corpo["positions"][0]["preco_atual"] == "30"

    async def test_cache_e_compartilhado_entre_usuarios(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """A cotacao de PETR4 e a mesma para todo mundo.

        Cache por usuario multiplicaria as chamadas externas pelo numero de
        usuarios sem nenhum ganho -- e por isso a chave do cache e o ativo, nao
        o par (usuario, ativo).
        """
        h1 = await _carteira(client, db)
        provedor.precos = {"PETR4": "25.00"}
        await client.get("/portfolio/summary", headers=h1)

        h2 = await segunda_conta(client)
        await client.post("/transactions", json=op(price="10.00"), headers=h2)
        provedor.chamadas.clear()
        await client.get("/portfolio/summary", headers=h2)

        assert provedor.chamadas == []


class TestDegradacao:
    async def test_fornecedor_fora_do_ar_devolve_cache_vencido(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """Preco de horas atras e melhor que nenhum preco.

        A resposta informa `cotacao_em`, entao quem consome decide se aceita a
        idade. Zerar a carteira porque o fornecedor caiu seria um defeito muito
        pior que um numero defasado.
        """
        h = await _carteira(client, db)
        provedor.precos = {"PETR4": "25.00"}
        await client.get("/portfolio/summary", headers=h)

        linha = (await db.execute(select(PriceQuote))).scalar_one()
        linha.fetched_at = datetime.now(UTC) - timedelta(hours=5)
        await db.commit()
        provedor.falha = True

        corpo = (await client.get("/portfolio/summary", headers=h)).json()

        assert corpo["positions"][0]["preco_atual"] == "25"
        assert corpo["sem_cotacao"] == []

    async def test_sem_cache_e_sem_fornecedor_a_carteira_ainda_responde(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """Degradar, nunca falhar: custo e quantidade continuam corretos, e os
        tickers afetados sao listados explicitamente."""
        h = await _carteira(client, db)
        provedor.falha = True

        resp = await client.get("/portfolio/summary", headers=h)
        corpo = resp.json()

        assert resp.status_code == 200
        assert corpo["sem_cotacao"] == ["PETR4"]
        assert corpo["positions"][0]["preco_atual"] is None
        assert corpo["positions"][0]["custo_total"] == "2000.00"
        # Sem preco, o ativo entra no total pelo custo: os totais fecham com a
        # soma das linhas em vez de a carteira parecer ter derretido.
        assert corpo["totals"]["valor_mercado"] == "2000.00"
        assert corpo["totals"]["resultado_nao_realizado"] == "0.00"


class TestCalculoDeMercado:
    async def test_valor_de_mercado_e_rentabilidade(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """100 x 25 = 2500 de valor; custo 2000; ganho 500; variacao 25%."""
        h = await _carteira(client, db)
        provedor.precos = {"PETR4": "25.00"}

        corpo = (await client.get("/portfolio/summary", headers=h)).json()
        linha = corpo["positions"][0]

        assert linha["valor_mercado"] == "2500.00"
        assert linha["resultado_nao_realizado"] == "500.00"
        assert linha["variacao_percentual"] == "25.00"

    async def test_prejuizo_da_variacao_negativa(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        h = await _carteira(client, db)
        provedor.precos = {"PETR4": "15.00"}

        linha = (await client.get("/portfolio/summary", headers=h)).json()["positions"][0]
        assert linha["resultado_nao_realizado"] == "-500.00"
        assert linha["variacao_percentual"] == "-25.00"

    async def test_posicao_zerada_nao_consome_cota_do_fornecedor(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """Nao ha o que valorizar numa posicao zerada -- buscar cotacao para ela
        seria gastar requisicao a toa. Mas o resultado realizado continua no
        total, porque e dinheiro que o usuario de fato ganhou."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)
        await client.post(
            "/transactions",
            json=op(side="venda", price="30.00", traded_at="2026-02-10"),
            headers=h,
        )

        corpo = (await client.get("/portfolio/summary", headers=h)).json()

        assert corpo["positions"] == []
        assert provedor.chamadas == []
        assert corpo["totals"]["resultado_realizado"] == "1000.00"

    async def test_resumo_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.get("/portfolio/summary")).status_code == 401

    async def test_resumo_nao_mistura_carteiras(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        h1 = await _carteira(client, db)
        provedor.precos = {"PETR4": "25.00"}
        h2 = await segunda_conta(client)

        assert len((await client.get("/portfolio/summary", headers=h1)).json()["positions"]) == 1
        assert (await client.get("/portfolio/summary", headers=h2)).json()["positions"] == []


class TestParsingDefensivo:
    """A resposta de uma API externa nunca e confiavel.

    Um campo que some numa atualizacao do fornecedor viraria KeyError e um 500
    para o usuario. Estes testes fixam que o cliente ignora o que nao entende em
    vez de estourar.
    """

    def test_brapi_ignora_payload_malformado(self) -> None:
        from app.clients.brapi import BrapiClient

        for lixo in [
            None,
            [],
            {},
            {"results": None},
            {"results": [{}]},
            {"results": [{"symbol": "PETR4"}]},  # sem preco
            {"results": [{"regularMarketPrice": 10}]},  # sem simbolo
            {"results": [{"symbol": "PETR4", "regularMarketPrice": "nao-e-numero"}]},
            {"results": ["string solta"]},
        ]:
            assert BrapiClient._extrair(lixo) == {}, lixo

    def test_brapi_rejeita_preco_nao_positivo(self) -> None:
        """Preco zero ou negativo e dado corrompido, nao cotacao -- aceitar
        zeraria a carteira do usuario silenciosamente."""
        from app.clients.brapi import BrapiClient

        for preco in (0, -5):
            payload = {"results": [{"symbol": "PETR4", "regularMarketPrice": preco}]}
            assert BrapiClient._extrair(payload) == {}, preco

    def test_brapi_le_payload_valido(self) -> None:
        from app.clients.brapi import BrapiClient

        cotacoes = BrapiClient._extrair(
            {"results": [{"symbol": "petr4", "regularMarketPrice": 41.45}]}
        )
        assert cotacoes["PETR4"].preco == Decimal("41.45")


class TestEncadeamento:
    async def test_segundo_provedor_completa_as_lacunas(self) -> None:
        """O fallback pede ao segundo SO o que o primeiro nao respondeu.

        Repetir a lista inteira gastaria cota a toa -- e numa carteira mista o
        primario costuma responder quase tudo.
        """
        from app.clients.composto import ProvedorEncadeado

        primario = ProvedorFake()
        primario.precos = {"PETR4": "40.00"}
        reserva = ProvedorFake()
        reserva.precos = {"VALE3": "78.00"}

        encadeado = ProvedorEncadeado(primario, reserva)
        cotacoes = await encadeado.cotacoes(["PETR4", "VALE3"])

        assert set(cotacoes) == {"PETR4", "VALE3"}
        assert primario.chamadas == [["PETR4", "VALE3"]]
        assert reserva.chamadas == [["VALE3"]]  # so a lacuna

    async def test_falha_do_primeiro_nao_impede_o_segundo(self) -> None:
        from app.clients.composto import ProvedorEncadeado

        class Explode:
            nome = "explode"

            async def cotacoes(self, tickers):  # type: ignore[no-untyped-def]
                raise RuntimeError("fornecedor fora do ar")

        reserva = ProvedorFake()
        reserva.precos = {"PETR4": "40.00"}

        cotacoes = await ProvedorEncadeado(Explode(), reserva).cotacoes(["PETR4"])
        assert cotacoes["PETR4"].preco == Decimal("40.00")


class TestClienteHttp:
    def test_todo_timeout_esta_configurado(self) -> None:
        """Sem timeout, um fornecedor que aceita a conexao e nunca responde
        prende o worker para sempre -- a forma mais comum de uma API cair por
        causa de um terceiro."""
        from app.clients import get_http_client

        timeout = get_http_client().timeout
        assert timeout.connect is not None
        assert timeout.read is not None
        assert timeout.write is not None
        assert timeout.pool is not None

    def test_nao_segue_redirecionamento(self) -> None:
        """Um 3xx inesperado de uma API de dados e sinal de problema (portal
        cativo, bloqueio, dominio sequestrado), nao algo a obedecer."""
        from app.clients import get_http_client

        assert get_http_client().follow_redirects is False


class TestCamadaHttp:
    """Testes do transporte HTTP real, com respostas simuladas.

    `MockTransport` do httpx intercepta no nivel do transporte: o cliente monta a
    URL de verdade, serializa parametros de verdade e processa a resposta de
    verdade -- so o socket e substituido. Assim se testa o que uma dublagem do
    metodo inteiro nao alcanca: a URL construida, o tratamento de 4xx/5xx e o
    comportamento diante de timeout.
    """

    async def test_brapi_monta_a_url_em_lote(self) -> None:
        import httpx

        from app.clients.brapi import BrapiClient

        chamadas: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            chamadas.append(request)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"symbol": "PETR4", "regularMarketPrice": 41.45},
                        {"symbol": "VALE3", "regularMarketPrice": 78.30},
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            cotacoes = await BrapiClient(http).cotacoes(["PETR4", "VALE3"])

        # Uma requisicao para os dois tickers, nao duas.
        assert len(chamadas) == 1
        assert chamadas[0].url.path.endswith("/PETR4,VALE3")
        assert cotacoes["PETR4"].preco == Decimal("41.45")

    async def test_brapi_envia_o_token_quando_configurado(self) -> None:
        import httpx

        from app.clients.brapi import BrapiClient

        capturada: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            capturada.append(request)
            return httpx.Response(200, json={"results": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            await BrapiClient(http, token="segredo").cotacoes(["PETR4"])

        assert capturada[0].url.params["token"] == "segredo"

    async def test_brapi_divide_carteira_grande_em_lotes(self) -> None:
        """O plano gratuito recusa lotes grandes por inteiro -- perder a resposta
        toda por excesso e pior que fazer duas chamadas."""
        import httpx

        from app.clients.brapi import BrapiClient

        chamadas: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            chamadas.append(request)
            return httpx.Response(200, json={"results": []})

        tickers = [f"AAA{i}" for i in range(25)]
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            await BrapiClient(http).cotacoes(tickers)

        assert len(chamadas) == 3  # 10 + 10 + 5

    async def test_erro_http_nao_propaga(self) -> None:
        """500 do fornecedor vira "sem cotacao", nunca 500 para o usuario."""
        import httpx

        from app.clients.brapi import BrapiClient

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="erro interno do fornecedor")

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            assert await BrapiClient(http).cotacoes(["PETR4"]) == {}

    async def test_timeout_nao_propaga(self) -> None:
        import httpx

        from app.clients.brapi import BrapiClient

        def responder(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("estourou", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            assert await BrapiClient(http).cotacoes(["PETR4"]) == {}

    async def test_resposta_nao_json_nao_propaga(self) -> None:
        """Portal cativo, pagina de bloqueio, HTML de erro -- tudo isso chega
        como 200 com corpo que nao e JSON."""
        import httpx

        from app.clients.brapi import BrapiClient

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>bloqueado</html>")

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            assert await BrapiClient(http).cotacoes(["PETR4"]) == {}

    async def test_yahoo_adiciona_o_sufixo_sa_no_adaptador(self) -> None:
        """O ".SA" e convencao do Yahoo, nao parte do ativo -- e por isso ele
        aparece aqui e nunca no banco."""
        import httpx

        from app.clients.yahoo import YahooClient

        chamadas: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            chamadas.append(request)
            return httpx.Response(
                200, json={"chart": {"result": [{"meta": {"regularMarketPrice": 41.45}}]}}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            cotacoes = await YahooClient(http).cotacoes(["PETR4"])

        assert chamadas[0].url.path.endswith("/PETR4.SA")
        assert cotacoes["PETR4"].preco == Decimal("41.45")

    async def test_yahoo_usa_o_fechamento_anterior_como_alternativa(self) -> None:
        """Fora do pregao, `regularMarketPrice` pode nao vir."""
        import httpx

        from app.clients.yahoo import YahooClient

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"chart": {"result": [{"meta": {"chartPreviousClose": 40.00}}]}}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            cotacoes = await YahooClient(http).cotacoes(["PETR4"])

        assert cotacoes["PETR4"].preco == Decimal("40.00")

    async def test_yahoo_ignora_estrutura_inesperada(self) -> None:
        import httpx

        from app.clients.yahoo import YahooClient

        for payload in ({}, {"chart": {}}, {"chart": {"result": []}}, {"chart": {"result": [{}]}}):

            def responder(request: httpx.Request, p: dict = payload) -> httpx.Response:
                return httpx.Response(200, json=p)

            async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
                assert await YahooClient(http).cotacoes(["PETR4"]) == {}, payload
