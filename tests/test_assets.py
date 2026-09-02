"""Testes do catalogo de ativos."""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import AssetType
from tests.conftest import ProvedorFake
from tests.factories import criar_ativo, criar_historico, op, usuario_logado


class TestProtecao:
    async def test_listagem_exige_autenticacao(self, client: AsyncClient) -> None:
        """O router inteiro e protegido por omissao. Se alguem adicionar uma rota
        nova aqui, ela ja nasce fechada -- este teste fixa esse contrato."""
        assert (await client.get("/assets")).status_code == 401

    async def test_detalhe_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.get("/assets/PETR4")).status_code == 401

    async def test_historico_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.get("/assets/PETR4/history")).status_code == 401


class TestPaginacao:
    async def test_envelope_traz_total_e_janela(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        _, headers = await usuario_logado(client)
        for t in ("AAAA3", "BBBB3", "CCCC3"):
            await criar_ativo(db, ticker=t)

        corpo = (await client.get("/assets?limit=2", headers=headers)).json()

        assert corpo["total"] == 3
        assert corpo["limit"] == 2
        assert len(corpo["items"]) == 2

    async def test_paginas_nao_se_sobrepoem(self, client: AsyncClient, db: AsyncSession) -> None:
        """Sem `order_by` explicito o Postgres nao garante ordem estavel entre
        consultas, e a pagina 2 poderia repetir ou pular itens da pagina 1."""
        _, headers = await usuario_logado(client)
        for t in ("AAAA3", "BBBB3", "CCCC3", "DDDD3"):
            await criar_ativo(db, ticker=t)

        p1 = (await client.get("/assets?limit=2&offset=0", headers=headers)).json()["items"]
        p2 = (await client.get("/assets?limit=2&offset=2", headers=headers)).json()["items"]

        tickers = [a["ticker"] for a in p1 + p2]
        assert tickers == sorted(tickers)
        assert len(set(tickers)) == 4

    async def test_limit_acima_do_teto_e_recusado(self, client: AsyncClient) -> None:
        """Sem teto, `?limit=1000000` derruba a API por memoria -- sem ataque
        nenhum, so um cliente distraido."""
        _, headers = await usuario_logado(client)
        assert (await client.get("/assets?limit=1000000", headers=headers)).status_code == 422

    async def test_limit_zero_ou_negativo_e_recusado(self, client: AsyncClient) -> None:
        _, headers = await usuario_logado(client)
        assert (await client.get("/assets?limit=0", headers=headers)).status_code == 422
        assert (await client.get("/assets?offset=-1", headers=headers)).status_code == 422


class TestBuscaEFiltro:
    async def test_busca_por_ticker_e_por_nome(self, client: AsyncClient, db: AsyncSession) -> None:
        _, headers = await usuario_logado(client)
        await criar_ativo(db, ticker="PETR4", nome="Petroleo Brasileiro S.A.")
        await criar_ativo(db, ticker="VALE3", nome="Vale S.A.")

        por_ticker = (await client.get("/assets?busca=PETR", headers=headers)).json()
        por_nome = (await client.get("/assets?busca=brasileiro", headers=headers)).json()

        assert [a["ticker"] for a in por_ticker["items"]] == ["PETR4"]
        assert [a["ticker"] for a in por_nome["items"]] == ["PETR4"]

    async def test_busca_ignora_caixa(self, client: AsyncClient, db: AsyncSession) -> None:
        _, headers = await usuario_logado(client)
        await criar_ativo(db, ticker="PETR4")
        assert (await client.get("/assets?busca=petr", headers=headers)).json()["total"] == 1

    async def test_filtro_por_tipo(self, client: AsyncClient, db: AsyncSession) -> None:
        _, headers = await usuario_logado(client)
        await criar_ativo(db, ticker="PETR4", tipo=AssetType.ACAO)
        await criar_ativo(db, ticker="HGLG11", tipo=AssetType.FII, setor="Real Estate")

        corpo = (await client.get("/assets?tipo=fii", headers=headers)).json()
        assert [a["ticker"] for a in corpo["items"]] == ["HGLG11"]

    async def test_tipo_invalido_e_recusado(self, client: AsyncClient) -> None:
        _, headers = await usuario_logado(client)
        assert (await client.get("/assets?tipo=cripto", headers=headers)).status_code == 422

    async def test_busca_nao_permite_injecao_de_sql(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A entrada e vinculada como parametro, nunca concatenada no SQL. Se
        fosse interpolada, esta busca devolveria a tabela inteira."""
        _, headers = await usuario_logado(client)
        await criar_ativo(db, ticker="PETR4")

        corpo = (await client.get("/assets?busca=' OR 1=1 --", headers=headers)).json()
        assert corpo["total"] == 0

    async def test_total_corresponde_ao_filtro_aplicado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A contagem e a listagem tem que usar exatamente os mesmos filtros --
        senao o `total` mente sobre quantas paginas existem."""
        _, headers = await usuario_logado(client)
        await criar_ativo(db, ticker="PETR4")
        await criar_ativo(db, ticker="PETR3")
        await criar_ativo(db, ticker="VALE3")

        corpo = (await client.get("/assets?busca=PETR&limit=1", headers=headers)).json()
        assert corpo["total"] == 2
        assert len(corpo["items"]) == 1


class TestDetalheEHistorico:
    async def test_detalhe_por_ticker(self, client: AsyncClient, db: AsyncSession) -> None:
        _, headers = await usuario_logado(client)
        await criar_ativo(db, ticker="PETR4")

        corpo = (await client.get("/assets/PETR4", headers=headers)).json()
        assert corpo["ticker"] == "PETR4"
        assert corpo["tipo"] == "acao"

    async def test_ticker_em_minusculas_funciona(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        _, headers = await usuario_logado(client)
        await criar_ativo(db, ticker="PETR4")
        assert (await client.get("/assets/petr4", headers=headers)).status_code == 200

    async def test_ativo_inexistente_devolve_404(self, client: AsyncClient) -> None:
        _, headers = await usuario_logado(client)
        assert (await client.get("/assets/XXXX9", headers=headers)).status_code == 404

    async def test_ticker_fora_do_formato_e_barrado_na_borda(self, client: AsyncClient) -> None:
        """422 pelo `pattern` do path: a requisicao nem chega ao banco."""
        _, headers = await usuario_logado(client)
        assert (await client.get("/assets/'; DROP--", headers=headers)).status_code == 422

    async def test_historico_vem_do_mais_recente_para_o_mais_antigo(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        _, headers = await usuario_logado(client)
        ativo = await criar_ativo(db, ticker="PETR4")
        await criar_historico(db, ativo, dias=5)

        pontos = (await client.get("/assets/PETR4/history", headers=headers)).json()

        assert len(pontos) == 5
        datas = [p["date"] for p in pontos]
        assert datas == sorted(datas, reverse=True)

    async def test_historico_respeita_o_limite(self, client: AsyncClient, db: AsyncSession) -> None:
        _, headers = await usuario_logado(client)
        ativo = await criar_ativo(db, ticker="PETR4")
        await criar_historico(db, ativo, dias=10)

        pontos = (await client.get("/assets/PETR4/history?limit=3", headers=headers)).json()
        assert len(pontos) == 3

    async def test_preco_mantem_precisao_decimal(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O preco atravessa banco, ORM e JSON sem virar float.

        Se em algum ponto houvesse conversao para ponto flutuante, o valor voltaria
        como 40.000000000000004 ou parecido -- e centavo errado numa aplicacao
        financeira e defeito, nao arredondamento.
        """
        _, headers = await usuario_logado(client)
        ativo = await criar_ativo(db, ticker="PETR4")
        await criar_historico(db, ativo, dias=1, inicial="40.10")

        ponto = (await client.get("/assets/PETR4/history", headers=headers)).json()[0]
        assert ponto["close"] == "40.100000"


class TestCotacao:
    """`GET /assets/{ticker}/quote` -- pre-preenche o preco de uma operacao nova.

    Passa pelo mesmo `quote_service` usado no resumo da carteira: o cache aqui
    e o mesmo cache de la, e o mesmo teste de `test_quotes.py` que prova que a
    segunda chamada nao toca o fornecedor vale tambem para esta rota.
    """

    async def test_devolve_a_cotacao_do_fornecedor(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, headers = await usuario_logado(client)
        provedor.precos = {"PETR4": "37.45"}

        resp = await client.get("/assets/PETR4/quote", headers=headers)

        assert resp.status_code == 200
        corpo = resp.json()
        assert corpo["preco"] == "37.45"
        assert corpo["fonte"] == "fake"

    async def test_ticker_em_minusculas_funciona(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, headers = await usuario_logado(client)
        provedor.precos = {"PETR4": "37.45"}

        resp = await client.get("/assets/petr4/quote", headers=headers)
        assert resp.status_code == 200

    async def test_ativo_fora_do_catalogo_devolve_404(self, client: AsyncClient) -> None:
        _, headers = await usuario_logado(client)
        assert (await client.get("/assets/XXXX9/quote", headers=headers)).status_code == 404

    async def test_sem_cotacao_do_fornecedor_devolve_404(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """Ativo existe no catalogo, mas nenhum fornecedor devolveu preco --
        mesma consequencia pratica para quem preenche o formulario: sem
        sugestao, digite o seu."""
        await criar_ativo(db, ticker="PETR4")
        _, headers = await usuario_logado(client)
        provedor.falha = True

        resp = await client.get("/assets/PETR4/quote", headers=headers)

        assert resp.status_code == 404
        assert "PETR4" in resp.json()["detail"]

    async def test_reusa_o_cache_do_resto_do_app(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """A cotacao usada aqui e a MESMA do resumo da carteira -- pedir o
        preco para pre-preencher o formulario nao pode custar uma segunda
        chamada ao fornecedor."""
        await criar_ativo(db, ticker="PETR4")
        _, headers = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=headers)
        provedor.precos = {"PETR4": "25.00"}

        await client.get("/portfolio/summary", headers=headers)
        provedor.chamadas.clear()

        resp = await client.get("/assets/PETR4/quote", headers=headers)

        assert resp.status_code == 200
        # Decimal, nao string exata: um HIT de cache devolve o valor com a
        # precisao da coluna NUMERIC(18,6) da tabela de cache (25.000000), nao
        # a precisao original do fornecedor (25.00) -- o mesmo numero, duas
        # representacoes.
        assert Decimal(resp.json()["preco"]) == Decimal("25.00")
        assert provedor.chamadas == []

    async def test_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.get("/assets/PETR4/quote")).status_code == 401
