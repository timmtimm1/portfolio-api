"""Testes do livro de transacoes e da posicao consolidada."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import criar_ativo, criar_historico, op, segunda_conta, usuario_logado


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

    async def test_nao_edita_transacao_alheia(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        outro = await segunda_conta(client)

        criada = (await client.post("/transactions", json=op(), headers=dono)).json()

        resposta = await client.patch(
            f"/transactions/{criada['id']}", json={"price": "999.00"}, headers=outro
        )
        assert resposta.status_code == 404
        # E o preco do dono continua o que era -- o 404 nao pode ser so na
        # resposta, com a escrita acontecendo do mesmo jeito.
        do_dono = (await client.get(f"/transactions/{criada['id']}", headers=dono)).json()
        assert do_dono["price"] == "20"

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
        assert (
            await client.patch(f"/transactions/{fake}", json={"price": "1.00"})
        ).status_code == 401
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


class TestZerarTudo:
    """`DELETE /transactions` -- zera o livro inteiro de uma vez.

    Existe porque a remocao unitaria recusa apagar uma compra que sustenta uma
    venda posterior (`TestRemocao.test_remover_compra_que_sustenta_uma_venda...`).
    Numa carteira com meses de historico, isso obriga a apagar de tras para
    frente, uma linha por vez -- inviavel para quem so quer recomecar a
    simulacao. A REAL fica de fora por um motivo diferente: la a transacao E o
    registro, nao um estado que se reseta.
    """

    async def test_zera_uma_carteira_simulada_mesmo_com_venda_dependente(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O caso que a remocao unitaria nao resolve: uma compra com venda
        posterior. Aqui as duas saem juntas, sem 409."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        sim = (
            await client.post("/portfolios", json={"nome": "Sim", "tipo": "simulada"}, headers=h)
        ).json()

        await client.post(f"/transactions?portfolio_id={sim['id']}", json=op(), headers=h)
        await client.post(
            f"/transactions?portfolio_id={sim['id']}",
            json=op(side="venda", price="30", traded_at="2026-02-10"),
            headers=h,
        )

        resp = await client.delete(f"/transactions?portfolio_id={sim['id']}", headers=h)
        assert resp.status_code == 200
        assert resp.json()["removidas"] == 2

        restante = await client.get(f"/transactions?portfolio_id={sim['id']}", headers=h)
        assert restante.json()["total"] == 0

    async def test_a_carteira_real_e_recusada(self, client: AsyncClient, db: AsyncSession) -> None:
        """A trava mais importante do endpoint: nao existe 'zerar' a carteira
        real. Ledger-as-truth significa que a transacao E o dado."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(), headers=h)  # vai para a REAL

        resp = await client.delete("/transactions", headers=h)

        assert resp.status_code == 409
        assert (await client.get("/transactions", headers=h)).json()["total"] == 1

    async def test_carteira_ja_vazia_devolve_zero(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        _, h = await usuario_logado(client)
        sim = (
            await client.post("/portfolios", json={"nome": "Sim", "tipo": "simulada"}, headers=h)
        ).json()

        resp = await client.delete(f"/transactions?portfolio_id={sim['id']}", headers=h)

        assert resp.status_code == 200
        assert resp.json()["removidas"] == 0

    async def test_nao_zera_carteira_alheia(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        dono, h_dono = await usuario_logado(client)
        h_outro = await segunda_conta(client)
        sim = (
            await client.post(
                "/portfolios", json={"nome": "Sim", "tipo": "simulada"}, headers=h_dono
            )
        ).json()
        await client.post(f"/transactions?portfolio_id={sim['id']}", json=op(), headers=h_dono)

        resp = await client.delete(f"/transactions?portfolio_id={sim['id']}", headers=h_outro)

        # 404, nao 403: `get_carteira` nao acha a carteira de outro usuario --
        # o mesmo portao unico que protege toda leitura e escrita.
        assert resp.status_code == 404
        assert (await client.get(f"/transactions?portfolio_id={sim['id']}", headers=h_dono)).json()[
            "total"
        ] == 1

    async def test_apaga_o_historico_de_snapshots_junto(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Sem transacao nenhuma, nao ha posicao nem valor a fotografar --
        manter snapshots antigos contaria a historia de uma carteira que nao
        existe mais."""
        from sqlalchemy import select

        from app.models.snapshot import PortfolioSnapshot

        ativo = await criar_ativo(db, ticker="PETR4")
        # `backfill` so produz ponto onde ha fechamento em `price_history` -- sem
        # isto, a compra ficaria sem snapshot algum e o teste nao provaria nada.
        await criar_historico(db, ativo, dias=250)
        _, h = await usuario_logado(client)
        sim = (
            await client.post("/portfolios", json={"nome": "Sim", "tipo": "simulada"}, headers=h)
        ).json()
        await client.post(f"/transactions?portfolio_id={sim['id']}", json=op(), headers=h)

        # A propria criacao ja reconstroi o historico a partir da data da compra.
        antes = (
            (
                await db.execute(
                    select(PortfolioSnapshot).where(PortfolioSnapshot.portfolio_id == sim["id"])
                )
            )
            .scalars()
            .all()
        )
        assert antes  # sanity: havia snapshot para apagar

        await client.delete(f"/transactions?portfolio_id={sim['id']}", headers=h)

        depois = (
            (
                await db.execute(
                    select(PortfolioSnapshot).where(PortfolioSnapshot.portfolio_id == sim["id"])
                )
            )
            .scalars()
            .all()
        )
        assert depois == []

    async def test_permite_simular_de_novo_depois_de_zerar(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O motivo de a funcionalidade existir: apagar tudo e lancar operacoes
        novas em seguida, sem nenhum resquicio da simulacao anterior."""
        await criar_ativo(db, ticker="PETR4")
        await criar_ativo(db, ticker="VALE3")
        _, h = await usuario_logado(client)
        sim = (
            await client.post("/portfolios", json={"nome": "Sim", "tipo": "simulada"}, headers=h)
        ).json()
        await client.post(f"/transactions?portfolio_id={sim['id']}", json=op(), headers=h)

        await client.delete(f"/transactions?portfolio_id={sim['id']}", headers=h)
        nova = await client.post(
            f"/transactions?portfolio_id={sim['id']}",
            json=op(ticker="VALE3", quantity="50", price="70.00"),
            headers=h,
        )

        assert nova.status_code == 201
        posicoes = await client.get(f"/portfolio/positions?portfolio_id={sim['id']}", headers=h)
        assert [p["ticker"] for p in posicoes.json()] == ["VALE3"]

    async def test_todas_as_rotas_exigem_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.delete("/transactions")).status_code == 401


class TestEdicao:
    """Correcao de uma operacao ja lancada (`PATCH /transactions/{id}`).

    O que torna a edicao mais perigosa que a criacao: ela mexe no PASSADO. Uma
    compra de 2024 corrigida pode derrubar uma venda de 2025 que dependia dela,
    e o estrago so apareceria na proxima consulta de posicao -- longe do clique
    que o causou. Por isso quase todo teste daqui confere tambem o que NAO
    mudou.
    """

    async def test_corrige_o_preco_e_a_posicao_acompanha(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Ledger-as-truth na pratica: nao existe saldo para atualizar junto.

        Corrigido o livro, o preco medio sai recalculado na consulta seguinte.
        """
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        criada = (await client.post("/transactions", json=op(price="20.00"), headers=h)).json()

        resposta = await client.patch(
            f"/transactions/{criada['id']}", json={"price": "25.00"}, headers=h
        )

        assert resposta.status_code == 200
        assert resposta.json()["price"] == "25"
        p = (await client.get("/portfolio/positions", headers=h)).json()[0]
        # "25" e nao "25.00": `preco_medio` sai pelo `_enxuto`, que corta zeros a
        # direita. So `custo_total` e `resultado_realizado` passam pelo
        # `_dinheiro` e ganham os centavos.
        assert p["preco_medio"] == "25"
        assert p["custo_total"] == "2500.00"

    async def test_campo_ausente_nao_e_apagado(self, client: AsyncClient, db: AsyncSession) -> None:
        """O motivo de ser PATCH e nao PUT.

        Trocar o preco nao pode zerar a corretagem nem sumir com a observacao --
        que e exatamente o que um PUT com corpo incompleto faria, preenchendo os
        campos ausentes com o padrao do schema.
        """
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        corpo = op(price="20.00", fees="10.00") | {"note": "compra inicial"}
        criada = (await client.post("/transactions", json=corpo, headers=h)).json()

        editada = (
            await client.patch(f"/transactions/{criada['id']}", json={"price": "25.00"}, headers=h)
        ).json()

        assert editada["price"] == "25"
        assert editada["fees"] == "10"
        assert editada["quantity"] == "100"
        assert editada["traded_at"] == "2026-01-05"
        assert editada["note"] == "compra inicial"
        assert editada["side"] == "compra"

    async def test_zerar_a_corretagem_funciona(self, client: AsyncClient, db: AsyncSession) -> None:
        """0 e falso em Python, e essa e a armadilha.

        Se o servico escolhesse o valor com `campos.get("fees") or atual`, esta
        correcao seria silenciosamente ignorada e o usuario veria a taxa antiga
        voltar sozinha. O custo total e quem denuncia.
        """
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        criada = (
            await client.post("/transactions", json=op(price="20.00", fees="10.00"), headers=h)
        ).json()

        editada = (
            await client.patch(f"/transactions/{criada['id']}", json={"fees": "0"}, headers=h)
        ).json()

        assert editada["fees"] == "0"
        p = (await client.get("/portfolio/positions", headers=h)).json()[0]
        assert p["custo_total"] == "2000.00"  # 2010,00 antes da correcao

    async def test_note_nula_enviada_limpa_a_observacao(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """`null` enviado apaga; campo ausente preserva.

        Os dois casos no mesmo teste de proposito: e a diferenca entre eles que
        precisa valer, e um teste que olhasse so um dos dois passaria com o
        servico tratando ambos igual.
        """
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        corpo = op() | {"note": "errei o preco"}
        criada = (await client.post("/transactions", json=corpo, headers=h)).json()

        preservou = (
            await client.patch(f"/transactions/{criada['id']}", json={"price": "21.00"}, headers=h)
        ).json()
        assert preservou["note"] == "errei o preco"

        limpou = (
            await client.patch(f"/transactions/{criada['id']}", json={"note": None}, headers=h)
        ).json()
        assert limpou["note"] is None

    async def test_muda_o_ticker_e_a_operacao_troca_de_livro(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Lancou em PETR4 o que era VALE3.

        A posicao antiga tem que DESAPARECER, nao so a nova aparecer -- um
        servico que validasse apenas o ativo de destino deixaria a operacao
        contando nos dois livros.
        """
        await criar_ativo(db, ticker="PETR4")
        await criar_ativo(db, ticker="VALE3")
        _, h = await usuario_logado(client)
        criada = (await client.post("/transactions", json=op(ticker="PETR4"), headers=h)).json()

        editada = (
            await client.patch(f"/transactions/{criada['id']}", json={"ticker": "VALE3"}, headers=h)
        ).json()

        assert editada["ticker"] == "VALE3"
        posicoes = (await client.get("/portfolio/positions", headers=h)).json()
        assert [p["ticker"] for p in posicoes] == ["VALE3"]
        assert posicoes[0]["quantidade"] == "100"

    async def test_recusa_tirar_do_livro_uma_compra_que_a_venda_usa(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A correcao e valida no DESTINO e mesmo assim tem que ser recusada.

        Mover a compra de PETR4 para VALE3 monta um livro impecavel em VALE3 --
        uma compra sozinha. O estrago fica na ORIGEM: PETR4 passa a ter uma
        venda de 100 sem nenhuma compra antes.

        Este teste existe porque a mutacao "validar so o ativo de destino"
        sobreviveu aos outros 16. O caminho feliz da troca de ticker nao prova
        nada sobre a origem: nele o livro antigo fica vazio, e livro vazio
        fecha.
        """
        await criar_ativo(db, ticker="PETR4")
        await criar_ativo(db, ticker="VALE3")
        _, h = await usuario_logado(client)
        compra = (await client.post("/transactions", json=op(ticker="PETR4"), headers=h)).json()
        await client.post(
            "/transactions",
            json=op(ticker="PETR4", side="venda", price="30.00", traded_at="2026-02-10"),
            headers=h,
        )

        resposta = await client.patch(
            f"/transactions/{compra['id']}", json={"ticker": "VALE3"}, headers=h
        )

        assert resposta.status_code == 409
        # E nada se moveu: a compra continua em PETR4.
        intacta = (await client.get(f"/transactions/{compra['id']}", headers=h)).json()
        assert intacta["ticker"] == "PETR4"

    async def test_ticker_minusculo_e_normalizado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="VALE3")
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        criada = (await client.post("/transactions", json=op(ticker="PETR4"), headers=h)).json()

        editada = await client.patch(
            f"/transactions/{criada['id']}", json={"ticker": "vale3"}, headers=h
        )

        assert editada.status_code == 200
        assert editada.json()["ticker"] == "VALE3"

    async def test_recusa_correcao_que_deixaria_venda_a_descoberto(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """409, e o livro fica INTACTO.

        Reduzir a compra para 50 deixaria a venda de 100 sem lastro. Recusar na
        resposta mas gravar assim mesmo seria pior que nao ter a rota.
        """
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        compra = (await client.post("/transactions", json=op(quantity="100"), headers=h)).json()
        await client.post(
            "/transactions",
            json=op(side="venda", quantity="100", price="30.00", traded_at="2026-02-10"),
            headers=h,
        )

        resposta = await client.patch(
            f"/transactions/{compra['id']}", json={"quantity": "50"}, headers=h
        )

        assert resposta.status_code == 409
        intacta = (await client.get(f"/transactions/{compra['id']}", headers=h)).json()
        assert intacta["quantity"] == "100"

    async def test_recusa_venda_movida_para_antes_da_compra(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A validacao e cronologica, nao um conferir de saldo atual.

        Somando tudo, comprou 100 e vendeu 100 -- fecha. So que com a venda em
        janeiro e a compra em fevereiro, ela acontece sobre posicao zero. Um
        servico que olhasse so o total final aceitaria isso.
        """
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(traded_at="2026-02-10"), headers=h)
        venda = (
            await client.post(
                "/transactions",
                json=op(side="venda", price="30.00", traded_at="2026-03-15"),
                headers=h,
            )
        ).json()

        resposta = await client.patch(
            f"/transactions/{venda['id']}", json={"traded_at": "2026-01-05"}, headers=h
        )

        assert resposta.status_code == 409

    async def test_recusa_ticker_fora_do_catalogo(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        criada = (await client.post("/transactions", json=op(), headers=h)).json()

        resposta = await client.patch(
            f"/transactions/{criada['id']}", json={"ticker": "ZZZZ9"}, headers=h
        )

        assert resposta.status_code == 422

    async def test_recusa_data_no_futuro(self, client: AsyncClient, db: AsyncSession) -> None:
        """Mesma recusa da criacao.

        A regra mora numa funcao de modulo justamente para nao existir so no
        schema de criacao -- a edicao e o caminho mais provavel de alguem
        digitar 2027 sem querer.
        """
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        criada = (await client.post("/transactions", json=op(), headers=h)).json()

        resposta = await client.patch(
            f"/transactions/{criada['id']}", json={"traded_at": "2099-01-01"}, headers=h
        )

        assert resposta.status_code == 422

    async def test_recusa_campo_desconhecido(self, client: AsyncClient, db: AsyncSession) -> None:
        """`extra="forbid"` ganha o teste dele.

        Sem isso, mandar `preco` em vez de `price` devolveria 200 sem ter
        corrigido nada -- o pior desfecho possivel, porque parece sucesso.
        """
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        criada = (await client.post("/transactions", json=op(price="20.00"), headers=h)).json()

        resposta = await client.patch(
            f"/transactions/{criada['id']}", json={"preco": "25.00"}, headers=h
        )

        assert resposta.status_code == 422
        inalterada = (await client.get(f"/transactions/{criada['id']}", headers=h)).json()
        assert inalterada["price"] == "20"

    async def test_recusa_quantidade_zero(self, client: AsyncClient, db: AsyncSession) -> None:
        """Os tetos e pisos do schema de criacao valem na edicao tambem."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        criada = (await client.post("/transactions", json=op(), headers=h)).json()

        resposta = await client.patch(
            f"/transactions/{criada['id']}", json={"quantity": "0"}, headers=h
        )

        assert resposta.status_code == 422

    async def test_corpo_vazio_nao_altera_nada(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        criada = (await client.post("/transactions", json=op(), headers=h)).json()

        resposta = await client.patch(f"/transactions/{criada['id']}", json={}, headers=h)

        assert resposta.status_code == 200
        assert resposta.json()["price"] == criada["price"]
        assert resposta.json()["quantity"] == criada["quantity"]

    async def test_id_inexistente_devolve_404(self, client: AsyncClient) -> None:
        import uuid

        _, h = await usuario_logado(client)

        resposta = await client.patch(
            f"/transactions/{uuid.uuid4()}", json={"price": "25.00"}, headers=h
        )

        assert resposta.status_code == 404

    async def test_troca_compra_por_venda(self, client: AsyncClient, db: AsyncSession) -> None:
        """Marcou compra onde era venda -- o erro de clique mais comum.

        Precisa de posicao anterior para a venda ter lastro, senao a correcao
        cairia (corretamente) em 409.
        """
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(quantity="300"), headers=h)
        errada = (
            await client.post(
                "/transactions", json=op(quantity="100", traded_at="2026-02-10"), headers=h
            )
        ).json()

        editada = await client.patch(
            f"/transactions/{errada['id']}", json={"side": "venda"}, headers=h
        )

        assert editada.status_code == 200
        assert editada.json()["side"] == "venda"
        p = (await client.get("/portfolio/positions", headers=h)).json()[0]
        assert p["quantidade"] == "200"
