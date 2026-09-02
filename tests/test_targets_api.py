"""Testes do alvo de preco pela API: definir, remover, e o status embutido
no resumo da carteira.

O calculo em si (limites, ordem gain/loss) ja esta coberto em
`test_target.py`, contra numeros conferidos a mao. Aqui o foco e outro:
persistencia, upsert, isolamento entre contas e entre carteiras, e validacao
na borda.
"""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import ProvedorFake
from tests.factories import criar_ativo, op, segunda_conta, usuario_logado


class TestDefinirAlvo:
    async def test_define_os_dois_lados_e_devolve_o_status_ja_calculado(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """O ponto do PUT devolver o status: poupa um GET extra so para saber
        se o alvo que acabou de ser salvo ja bateu ou nao."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)
        provedor.precos = {"PETR4": "22.00"}  # entre 18,40 (loss) e 23,00 (gain)

        resp = await client.put(
            "/portfolio/targets/PETR4",
            json={
                "stop_gain_tipo": "percentual",
                "stop_gain_valor": "0.15",
                "stop_loss_tipo": "percentual",
                "stop_loss_valor": "0.08",
            },
            headers=h,
        )

        assert resp.status_code == 200
        corpo = resp.json()
        assert corpo["status"] == "dentro"
        assert Decimal(corpo["stop_gain_valor"]) == Decimal("0.15")
        assert Decimal(corpo["stop_loss_valor"]) == Decimal("0.08")

    async def test_status_ja_atingido_no_momento_de_salvar(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)
        provedor.precos = {"PETR4": "30.00"}  # ja acima do gain de 15% (23,00)

        resp = await client.put(
            "/portfolio/targets/PETR4",
            json={"stop_gain_tipo": "percentual", "stop_gain_valor": "0.15"},
            headers=h,
        )
        assert resp.json()["status"] == "gain_atingido"

    async def test_ticker_em_minusculas_funciona(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.put(
            "/portfolio/targets/petr4",
            json={"stop_gain_tipo": "preco", "stop_gain_valor": "45.00"},
            headers=h,
        )
        assert resp.status_code == 200

    async def test_ticker_fora_do_catalogo_devolve_404(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        resp = await client.put("/portfolio/targets/XXXX9", json={}, headers=h)
        assert resp.status_code == 404

    async def test_definir_de_novo_substitui_o_lado_nao_reenviado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Upsert e TOTAL: reenviar so o stop gain apaga o stop loss que
        estava configurado antes -- nao existe atualizacao parcial por fora."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.put(
            "/portfolio/targets/PETR4",
            json={
                "stop_gain_tipo": "percentual",
                "stop_gain_valor": "0.15",
                "stop_loss_tipo": "percentual",
                "stop_loss_valor": "0.08",
            },
            headers=h,
        )

        resp = await client.put(
            "/portfolio/targets/PETR4",
            json={"stop_gain_tipo": "percentual", "stop_gain_valor": "0.20"},
            headers=h,
        )

        corpo = resp.json()
        assert Decimal(corpo["stop_gain_valor"]) == Decimal("0.20")
        assert corpo["stop_loss_tipo"] is None
        assert corpo["stop_loss_valor"] is None


class TestValidacao:
    async def test_tipo_sem_valor_e_recusado(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.put(
            "/portfolio/targets/PETR4", json={"stop_gain_tipo": "percentual"}, headers=h
        )
        assert resp.status_code == 422

    async def test_valor_sem_tipo_e_recusado(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.put(
            "/portfolio/targets/PETR4", json={"stop_gain_valor": "0.10"}, headers=h
        )
        assert resp.status_code == 422

    async def test_stop_loss_percentual_acima_de_cem_por_cento_e_recusado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Uma posicao nao cai mais que 100% do que custou -- perder mais que
        isso so seria possivel numa operacao alavancada, que este app nao
        modela."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.put(
            "/portfolio/targets/PETR4",
            json={"stop_loss_tipo": "percentual", "stop_loss_valor": "1.5"},
            headers=h,
        )
        assert resp.status_code == 422

    async def test_stop_gain_percentual_acima_de_cem_por_cento_e_permitido(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Diferente do loss: uma acao pode multiplicar por 10 -- nao ha teto
        natural para o lado do ganho."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.put(
            "/portfolio/targets/PETR4",
            json={"stop_gain_tipo": "percentual", "stop_gain_valor": "5"},
            headers=h,
        )
        assert resp.status_code == 200

    async def test_valor_zero_ou_negativo_e_recusado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.put(
            "/portfolio/targets/PETR4",
            json={"stop_gain_tipo": "preco", "stop_gain_valor": "0"},
            headers=h,
        )
        assert resp.status_code == 422


class TestRemoverAlvo:
    async def test_remove_o_alvo(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.put(
            "/portfolio/targets/PETR4",
            json={"stop_gain_tipo": "preco", "stop_gain_valor": "45.00"},
            headers=h,
        )

        resp = await client.delete("/portfolio/targets/PETR4", headers=h)
        assert resp.status_code == 204

        await client.post("/transactions", json=op(price="20.00"), headers=h)
        resumo = await client.get("/portfolio/summary", headers=h)
        alvo = next(p["alvo"] for p in resumo.json()["positions"] if p["ticker"] == "PETR4")
        assert alvo["status"] == "sem_alvo"

    async def test_remover_alvo_inexistente_nao_e_erro(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Remover o que ja nao existe chega ao mesmo estado -- nao ha razao
        para a tela tratar isso como falha."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.delete("/portfolio/targets/PETR4", headers=h)
        assert resp.status_code == 204


class TestNoResumoDaCarteira:
    async def test_posicao_sem_alvo_vem_com_status_sem_alvo(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)

        resumo = await client.get("/portfolio/summary", headers=h)
        alvo = resumo.json()["positions"][0]["alvo"]
        assert alvo["status"] == "sem_alvo"
        assert alvo["stop_gain_tipo"] is None

    async def test_alvo_sobrevive_a_zerar_e_reabrir_a_posicao(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O alvo e da CARTEIRA+ATIVO, nao de um lote especifico. Vender tudo
        e comprar de novo depois deve encontrar o mesmo alvo -- e a posicao so
        volta a aparecer no resumo quando a quantidade for maior que zero de
        novo, entao e nesse momento que o alvo reaparece junto."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)
        await client.put(
            "/portfolio/targets/PETR4",
            json={"stop_gain_tipo": "preco", "stop_gain_valor": "45.00"},
            headers=h,
        )

        # Zera a posicao -- ela some do resumo, mas o alvo continua no banco.
        await client.post(
            "/transactions",
            json=op(side="venda", price="25.00", traded_at="2026-02-01"),
            headers=h,
        )
        resumo_zerado = await client.get("/portfolio/summary", headers=h)
        assert resumo_zerado.json()["positions"] == []

        # Recompra: a posicao reaparece, e o alvo com ela.
        await client.post(
            "/transactions", json=op(price="30.00", traded_at="2026-03-01"), headers=h
        )
        resumo = await client.get("/portfolio/summary", headers=h)
        alvo = resumo.json()["positions"][0]["alvo"]
        assert alvo["stop_gain_tipo"] == "preco"
        assert Decimal(alvo["stop_gain_valor"]) == Decimal("45.00")


class TestIsolamento:
    """A classe mais importante do arquivo -- mesmo motivo de
    `test_transactions.py::TestIsolamentoEntreUsuarios`."""

    async def test_alvo_de_um_usuario_nao_aparece_para_outro(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        outro = await segunda_conta(client)

        await client.put(
            "/portfolio/targets/PETR4",
            json={"stop_gain_tipo": "preco", "stop_gain_valor": "45.00"},
            headers=dono,
        )
        await client.post("/transactions", json=op(price="20.00"), headers=outro)

        resumo_de_outro = await client.get("/portfolio/summary", headers=outro)
        alvo = resumo_de_outro.json()["positions"][0]["alvo"]
        assert alvo["status"] == "sem_alvo"

    async def test_nao_define_alvo_na_carteira_alheia(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        outro = await segunda_conta(client)
        carteira_do_dono = (await client.get("/portfolios", headers=dono)).json()[0]["id"]

        resp = await client.put(
            f"/portfolio/targets/PETR4?portfolio_id={carteira_do_dono}",
            json={"stop_gain_tipo": "preco", "stop_gain_valor": "45.00"},
            headers=outro,
        )

        # 404, nao 403: `CarteiraAtual` nao encontra a carteira de outro
        # usuario -- o mesmo portao unico que protege toda leitura e escrita.
        assert resp.status_code == 404

    async def test_alvo_e_por_carteira_nao_por_usuario(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """O mesmo ticker em duas carteiras do MESMO usuario tem alvos
        independentes -- e o ponto de o alvo ser por (carteira, ativo)."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        sim = (
            await client.post("/portfolios", json={"nome": "Sim", "tipo": "simulada"}, headers=h)
        ).json()

        await client.post("/transactions", json=op(price="20.00"), headers=h)  # vai para a real
        await client.post(
            f"/transactions?portfolio_id={sim['id']}", json=op(price="20.00"), headers=h
        )
        await client.put(
            "/portfolio/targets/PETR4",
            json={"stop_gain_tipo": "preco", "stop_gain_valor": "45.00"},
            headers=h,
        )

        resumo_sim = await client.get(f"/portfolio/summary?portfolio_id={sim['id']}", headers=h)
        alvo_sim = resumo_sim.json()["positions"][0]["alvo"]
        assert alvo_sim["status"] == "sem_alvo"


class TestExigeAutenticacao:
    async def test_put_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.put("/portfolio/targets/PETR4", json={})).status_code == 401

    async def test_delete_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.delete("/portfolio/targets/PETR4")).status_code == 401


class TestMetaDeAcumulacao:
    """Meta de tamanho da posicao: quanto se quer TER no papel, em reais.

    Outra pergunta que o stop -- ele olha o preco ("quando sair"), a meta
    olha o tamanho ("quanto ainda comprar"). As duas moram no mesmo alvo,
    mas uma existe sem a outra.
    """

    async def test_define_meta_e_devolve_o_progresso(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)  # 100 x 20 = 2000
        provedor.precos = {"PETR4": "25.00"}  # valor de mercado = 2500

        resp = await client.put("/portfolio/targets/PETR4", json={"meta_valor": "10000"}, headers=h)

        assert resp.status_code == 200
        meta = resp.json()["meta"]
        assert Decimal(meta["atual"]) == Decimal("2500")
        assert Decimal(meta["falta"]) == Decimal("7500")
        assert Decimal(meta["progresso"]) == Decimal("0.25")
        assert meta["atingida"] is False

    async def test_meta_usa_valor_de_MERCADO_nao_o_custo(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """ "Quero ter R$ 10 mil neste papel" e sobre o que a posicao VALE
        hoje, nao sobre quanto foi desembolsado -- quem comprou por 2.000 e
        ja tem 2.500 andou 25% do caminho, nao 20%."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)
        provedor.precos = {"PETR4": "25.00"}
        await client.put("/portfolio/targets/PETR4", json={"meta_valor": "10000"}, headers=h)

        resumo = await client.get("/portfolio/summary", headers=h)
        meta = resumo.json()["positions"][0]["alvo"]["meta"]
        assert Decimal(meta["atual"]) == Decimal("2500")

    async def test_sem_cotacao_a_meta_usa_o_custo_como_reserva(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """Fornecedor fora do ar nao pode zerar a barra de progresso -- o
        custo e o melhor palpite disponivel, mesma regra dos totais."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)
        await client.put("/portfolio/targets/PETR4", json={"meta_valor": "10000"}, headers=h)
        provedor.falha = True

        resumo = await client.get("/portfolio/summary", headers=h)
        meta = resumo.json()["positions"][0]["alvo"]["meta"]
        assert Decimal(meta["atual"]) == Decimal("2000")  # o custo

    async def test_meta_e_stop_convivem_de_forma_independente(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Reenviar so a meta apaga os stops (upsert total) -- e reenviar so
        os stops apaga a meta. Quem edita pelo modal manda os tres juntos."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)

        resp = await client.put(
            "/portfolio/targets/PETR4",
            json={
                "meta_valor": "10000",
                "stop_gain_tipo": "percentual",
                "stop_gain_valor": "0.15",
            },
            headers=h,
        )
        corpo = resp.json()
        assert Decimal(corpo["meta"]["meta"]) == Decimal("10000")
        assert corpo["stop_gain_tipo"] == "percentual"

        so_meta = await client.put(
            "/portfolio/targets/PETR4", json={"meta_valor": "20000"}, headers=h
        )
        assert Decimal(so_meta.json()["meta"]["meta"]) == Decimal("20000")
        assert so_meta.json()["stop_gain_tipo"] is None

    async def test_meta_pode_ser_definida_antes_de_comprar_o_papel(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Planejar antes de comprar e caso legitimo: a meta existe, o
        progresso comeca em zero, e nao ha posicao para dividir."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)

        resp = await client.put("/portfolio/targets/PETR4", json={"meta_valor": "10000"}, headers=h)
        assert resp.status_code == 200
        assert Decimal(resp.json()["meta"]["atual"]) == Decimal(0)

    async def test_meta_zero_e_recusada(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.put("/portfolio/targets/PETR4", json={"meta_valor": "0"}, headers=h)
        assert resp.status_code == 422


class TestMetaDaCarteira:
    async def test_define_a_meta_e_calcula_o_nao_distribuido(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """O numero que justifica ter as duas metas: a diferenca entre o
        objetivo geral e a soma do que ja tem destino definido."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)
        provedor.precos = {"PETR4": "25.00"}
        await client.put("/portfolio/targets/PETR4", json={"meta_valor": "10000"}, headers=h)

        resp = await client.put("/portfolio/goal", json={"valor": "50000"}, headers=h)

        assert resp.status_code == 200
        corpo = resp.json()
        assert Decimal(corpo["progresso"]["meta"]) == Decimal("50000")
        assert Decimal(corpo["progresso"]["atual"]) == Decimal("2500")
        assert Decimal(corpo["soma_das_metas"]) == Decimal("10000")
        assert Decimal(corpo["nao_distribuido"]) == Decimal("40000")

    async def test_soma_conta_metas_de_papeis_ainda_nao_comprados(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Meta de um papel que ainda nao esta na carteira continua sendo
        dinheiro com destino -- some da conta se so olhassemos as posicoes."""
        await criar_ativo(db, ticker="PETR4")
        await criar_ativo(db, ticker="VALE3")
        _, h = await usuario_logado(client)
        await client.put("/portfolio/targets/PETR4", json={"meta_valor": "3000"}, headers=h)
        await client.put("/portfolio/targets/VALE3", json={"meta_valor": "2000"}, headers=h)

        resp = await client.put("/portfolio/goal", json={"valor": "10000"}, headers=h)
        assert Decimal(resp.json()["soma_das_metas"]) == Decimal("5000")
        assert Decimal(resp.json()["nao_distribuido"]) == Decimal("5000")

    async def test_remover_a_meta_da_carteira(self, client: AsyncClient, db: AsyncSession) -> None:
        _, h = await usuario_logado(client)
        await client.put("/portfolio/goal", json={"valor": "50000"}, headers=h)

        resp = await client.put("/portfolio/goal", json={"valor": None}, headers=h)

        assert resp.status_code == 200
        assert resp.json()["progresso"] is None
        assert resp.json()["nao_distribuido"] is None

    async def test_meta_da_carteira_e_por_CARTEIRA(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Cada carteira tem a sua -- definir na real nao mexe na simulada."""
        _, h = await usuario_logado(client)
        sim = (
            await client.post("/portfolios", json={"nome": "Sim", "tipo": "simulada"}, headers=h)
        ).json()
        await client.put("/portfolio/goal", json={"valor": "50000"}, headers=h)

        resumo_sim = await client.get(f"/portfolio/summary?portfolio_id={sim['id']}", headers=h)
        assert resumo_sim.json()["meta"]["progresso"] is None

    async def test_nao_define_meta_na_carteira_alheia(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        _, dono = await usuario_logado(client)
        outro = await segunda_conta(client)
        carteira_do_dono = (await client.get("/portfolios", headers=dono)).json()[0]["id"]

        resp = await client.put(
            f"/portfolio/goal?portfolio_id={carteira_do_dono}",
            json={"valor": "50000"},
            headers=outro,
        )
        assert resp.status_code == 404

    async def test_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.put("/portfolio/goal", json={"valor": "1000"})).status_code == 401
