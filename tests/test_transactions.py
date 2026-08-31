"""Testes do livro de transacoes e da posicao consolidada."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import criar_ativo, op, segunda_conta, usuario_logado


class TestIsolamentoEntreUsuarios:
    """A classe mais importante do arquivo.

    Falha de autorizacao e a vulnerabilidade numero um do OWASP Top 10, e num
    aplicativo de carteira ela nao vaza "dados": vaza patrimonio. Estes testes
    existem para que a regra "toda consulta filtra por user_id" nunca dependa de
    alguem lembrar dela.
    """

    async def test_extrato_so_mostra_as_proprias_operacoes(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        outro = await segunda_conta(client)

        await client.post("/transactions", json=op(), headers=dono)

        assert (await client.get("/transactions", headers=dono)).json()["total"] == 1
        assert (await client.get("/transactions", headers=outro)).json()["total"] == 0

    async def test_nao_le_transacao_alheia_e_devolve_404_nao_403(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """404, nao 403.

        403 ("existe, mas nao e seu") confirma que aquele id existe -- enumeracao
        de recursos alheios. 404 nao distingue "nao existe" de "nao e seu", que e
        exatamente a ambiguidade desejada.
        """
        await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        outro = await segunda_conta(client)

        criada = (await client.post("/transactions", json=op(), headers=dono)).json()

        do_dono = await client.get(f"/transactions/{criada['id']}", headers=dono)
        do_outro = await client.get(f"/transactions/{criada['id']}", headers=outro)
        assert do_dono.status_code == 200
        assert do_outro.status_code == 404

    async def test_nao_apaga_transacao_alheia(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        outro = await segunda_conta(client)

        criada = (await client.post("/transactions", json=op(), headers=dono)).json()

        assert (
            await client.delete(f"/transactions/{criada['id']}", headers=outro)
        ).status_code == 404
        # E o livro do dono continua intacto.
        assert (await client.get("/transactions", headers=dono)).json()["total"] == 1

    async def test_posicoes_nao_misturam_carteiras(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        await criar_ativo(db, ticker="VALE3")
        _, a = await usuario_logado(client)
        b = await segunda_conta(client)

        await client.post("/transactions", json=op(ticker="PETR4"), headers=a)
        await client.post("/transactions", json=op(ticker="VALE3", price="60.00"), headers=b)

        pos_a = (await client.get("/portfolio/positions", headers=a)).json()
        pos_b = (await client.get("/portfolio/positions", headers=b)).json()
        assert [p["ticker"] for p in pos_a] == ["PETR4"]
        assert [p["ticker"] for p in pos_b] == ["VALE3"]

    async def test_user_id_do_corpo_e_ignorado(self, client: AsyncClient, db: AsyncSession) -> None:
        """Injetar `user_id` no corpo nao lanca na carteira de outro.

        O schema nem tem esse campo, e o `user_id` vem do token. Este teste fixa o
        contrato: se alguem adicionar o campo ao schema por conveniencia, quebra.
        """
        import uuid

        await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        outro = await segunda_conta(client)

        corpo = op() | {"user_id": str(uuid.uuid4())}
        assert (await client.post("/transactions", json=corpo, headers=dono)).status_code == 201
        assert (await client.get("/transactions", headers=outro)).json()["total"] == 0

    async def test_todas_as_rotas_exigem_autenticacao(self, client: AsyncClient) -> None:
        import uuid

        fake = uuid.uuid4()
        assert (await client.post("/transactions", json=op())).status_code == 401
        assert (await client.get("/transactions")).status_code == 401
        assert (await client.get(f"/transactions/{fake}")).status_code == 401
        assert (await client.delete(f"/transactions/{fake}")).status_code == 401
        assert (await client.get("/portfolio/positions")).status_code == 401


class TestCriacao:
    async def test_registra_compra(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)

        resp = await client.post("/transactions", json=op(), headers=h)

        assert resp.status_code == 201
        corpo = resp.json()
        assert corpo["ticker"] == "PETR4"
        assert corpo["side"] == "compra"
        assert "user_id" not in corpo  # nao expomos o id do dono em cada linha

    async def test_ticker_em_minusculas_e_normalizado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.post("/transactions", json=op(ticker="petr4"), headers=h)
        assert resp.json()["ticker"] == "PETR4"

    async def test_ativo_fora_do_catalogo_e_recusado(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        resp = await client.post("/transactions", json=op(ticker="XPTO9"), headers=h)
        assert resp.status_code == 422

    async def test_quantidade_e_preco_precisam_ser_positivos(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        for corpo in (op(quantity="-10"), op(price="0"), op(fees="-1")):
            resp = await client.post("/transactions", json=corpo, headers=h)
            assert resp.status_code == 422, corpo

    async def test_data_futura_e_recusada(self, client: AsyncClient, db: AsyncSession) -> None:
        """Data futura entraria no fim do livro e distorceria o preco medio de
        tudo que viesse depois."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.post("/transactions", json=op(traded_at="2099-01-01"), headers=h)
        assert resp.status_code == 422

    async def test_venda_sem_posicao_e_recusada(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.post("/transactions", json=op(side="venda"), headers=h)
        assert resp.status_code == 422

    async def test_venda_retroativa_e_avaliada_na_data_dela(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O caso que uma checagem do saldo de hoje deixaria passar.

        Compra 100 em janeiro e mais 100 em marco. Uma venda de 150 lancada com
        data de fevereiro e invalida -- naquele momento havia 100 -- mesmo que a
        posicao de hoje seja 200.
        """
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(traded_at="2026-01-05"), headers=h)
        await client.post("/transactions", json=op(traded_at="2026-03-05"), headers=h)

        resp = await client.post(
            "/transactions",
            json=op(side="venda", quantity="150", price="30", traded_at="2026-02-05"),
            headers=h,
        )
        assert resp.status_code == 422


class TestPosicoes:
    async def test_preco_medio_ponderado_com_taxas(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Conferido a mao: (100x20 + 10) + (100x30) = 5010 / 200 = 25,05."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00", fees="10.00"), headers=h)
        await client.post(
            "/transactions", json=op(price="30.00", traded_at="2026-02-10"), headers=h
        )

        p = (await client.get("/portfolio/positions", headers=h)).json()[0]
        assert p["preco_medio"] == "25.05"
        assert p["custo_total"] == "5010.00"

    async def test_venda_preserva_o_preco_medio(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A regra brasileira. Realizado = (40 - 25,05) x 100 = 1495,00."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00", fees="10.00"), headers=h)
        await client.post(
            "/transactions", json=op(price="30.00", traded_at="2026-02-10"), headers=h
        )
        await client.post(
            "/transactions",
            json=op(side="venda", price="40.00", traded_at="2026-03-15"),
            headers=h,
        )

        p = (await client.get("/portfolio/positions", headers=h)).json()[0]
        assert p["quantidade"] == "100"
        assert p["preco_medio"] == "25.05"  # inalterado pela venda
        assert p["resultado_realizado"] == "1495.00"

    async def test_carteira_vazia_devolve_lista_vazia(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        assert (await client.get("/portfolio/positions", headers=h)).json() == []

    async def test_valores_saem_arredondados_para_centavos(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """3 acoes a 10,00 = 30,00 / 3 = 10,00 exato; mas 100/3 nao e exato.
        O arredondamento acontece so na saida, nunca durante o calculo."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(quantity="3", price="10.00"), headers=h)

        p = (await client.get("/portfolio/positions", headers=h)).json()[0]
        assert p["custo_total"] == "30.00"
        assert p["quantidade"] == "3"


class TestRemocao:
    async def test_remove_a_propria_transacao(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        criada = (await client.post("/transactions", json=op(), headers=h)).json()

        resp = await client.delete(f"/transactions/{criada['id']}", headers=h)
        assert resp.status_code == 204
        assert (await client.get("/transactions", headers=h)).json()["total"] == 0

    async def test_remover_compra_que_sustenta_uma_venda_e_recusado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Apagar a compra deixaria a venda posterior sem lastro -- um estado que
        o proprio sistema recusaria criar. Devolve 409 e o livro fica intacto."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        compra = (await client.post("/transactions", json=op(), headers=h)).json()
        await client.post(
            "/transactions",
            json=op(side="venda", price="30", traded_at="2026-02-10"),
            headers=h,
        )

        resp = await client.delete(f"/transactions/{compra['id']}", headers=h)

        assert resp.status_code == 409
        assert (await client.get("/transactions", headers=h)).json()["total"] == 2

    async def test_remover_inexistente_devolve_404(self, client: AsyncClient) -> None:
        import uuid

        _, h = await usuario_logado(client)
        resp = await client.delete(f"/transactions/{uuid.uuid4()}", headers=h)
        assert resp.status_code == 404


class TestListagem:
    async def test_filtra_por_ticker_e_por_lado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        await criar_ativo(db, ticker="VALE3")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(ticker="PETR4"), headers=h)
        await client.post("/transactions", json=op(ticker="VALE3", price="60"), headers=h)
        await client.post(
            "/transactions",
            json=op(ticker="PETR4", side="venda", price="30", traded_at="2026-02-10"),
            headers=h,
        )

        por_ticker = await client.get("/transactions?ticker=PETR4", headers=h)
        por_lado = await client.get("/transactions?side=venda", headers=h)
        assert por_ticker.json()["total"] == 2
        assert por_lado.json()["total"] == 1

    async def test_ordena_do_mais_recente_para_o_mais_antigo(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        for dia in ("2026-01-05", "2026-03-05", "2026-02-05"):
            await client.post("/transactions", json=op(traded_at=dia), headers=h)

        itens = (await client.get("/transactions", headers=h)).json()["items"]
        datas = [t["traded_at"] for t in itens]
        assert datas == sorted(datas, reverse=True)

    async def test_paginacao_tem_teto(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        assert (await client.get("/transactions?limit=1000000", headers=h)).status_code == 422
