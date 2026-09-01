"""Testes de carteiras múltiplas: a real e as simuladas."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import criar_ativo, op, segunda_conta, usuario_logado


async def _nova(client: AsyncClient, headers: dict[str, str], nome: str) -> str:
    resp = await client.post(
        "/portfolios", json={"nome": nome, "tipo": "simulada"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


class TestGestao:
    async def test_usuario_novo_ja_tem_a_carteira_real(self, client: AsyncClient) -> None:
        """Sem isso, ele precisaria criar uma carteira antes de conseguir lançar
        a primeira compra — uma etapa a mais para quem só quer registrar."""
        _, h = await usuario_logado(client)
        carteiras = (await client.get("/portfolios", headers=h)).json()
        assert len(carteiras) == 1
        assert carteiras[0]["tipo"] == "real"

    async def test_a_real_vem_sempre_primeiro(self, client: AsyncClient) -> None:
        """Regressão de um bug real.

        A ordenação usava `tipo.desc()` presumindo que "real" viria antes de
        "simulada". Quando o banco passou a guardar o VALOR minúsculo,
        'simulada' > 'real' alfabeticamente e o desc() inverteu tudo: a carteira
        PADRÃO virou a simulada, e uma transação lançada sem `portfolio_id` iria
        para a carteira errada, sem erro nenhum.
        """
        _, h = await usuario_logado(client)
        await _nova(client, h, "AAA simulada")  # nome que viria antes alfabeticamente

        carteiras = (await client.get("/portfolios", headers=h)).json()
        assert carteiras[0]["tipo"] == "real"

    async def test_nome_duplicado_e_recusado(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        await _nova(client, h, "Minha simulação")
        resp = await client.post("/portfolios", json={"nome": "Minha simulação"}, headers=h)
        assert resp.status_code == 409

    async def test_espacos_nas_pontas_sao_removidos(self, client: AsyncClient) -> None:
        """Sem isso, "Real" e "Real " passariam pela constraint de unicidade como
        nomes distintos, e o seletor mostraria duas entradas idênticas."""
        _, h = await usuario_logado(client)
        resp = await client.post("/portfolios", json={"nome": "  Teste  "}, headers=h)
        assert resp.json()["nome"] == "Teste"

    async def test_nome_vazio_e_recusado(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        assert (
            await client.post("/portfolios", json={"nome": "   "}, headers=h)
        ).status_code == 422

    async def test_teto_de_carteiras(self, client: AsyncClient) -> None:
        """Sem limite, um POST em laço criaria milhões de linhas."""
        _, h = await usuario_logado(client)
        await client.get("/portfolios", headers=h)  # garante a carteira real
        for i in range(19):  # 19 simuladas + 1 real = 20
            await _nova(client, h, f"Simulação {i}")
        resp = await client.post("/portfolios", json={"nome": "Mais uma"}, headers=h)
        assert resp.status_code == 422

    async def test_apagar_leva_junto_as_transacoes(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        sim = await _nova(client, h, "Descartável")
        await client.post(f"/transactions?portfolio_id={sim}", json=op(), headers=h)

        assert (await client.delete(f"/portfolios/{sim}", headers=h)).status_code == 204
        # A transação some com a carteira (CASCADE) e a carteira real fica intacta.
        assert (await client.get("/transactions", headers=h)).json()["total"] == 0


class TestIsolamento:
    """Uma carteira nunca pode ver a outra — nem a simulada do mesmo usuário."""

    async def test_transacoes_ficam_na_carteira_indicada(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        await criar_ativo(db, ticker="VALE3")
        _, h = await usuario_logado(client)
        sim = await _nova(client, h, "Simulada")

        await client.post("/transactions", json=op(ticker="PETR4"), headers=h)
        await client.post(
            f"/transactions?portfolio_id={sim}", json=op(ticker="VALE3", price="60"), headers=h
        )

        real = (await client.get("/portfolio/positions", headers=h)).json()
        simulada = (await client.get(f"/portfolio/positions?portfolio_id={sim}", headers=h)).json()

        assert [p["ticker"] for p in real] == ["PETR4"]
        assert [p["ticker"] for p in simulada] == ["VALE3"]

    async def test_sem_portfolio_id_usa_a_real(self, client: AsyncClient, db: AsyncSession) -> None:
        """O padrão é a REAL, escolhida explicitamente pelo tipo — não "a
        primeira da lista". Onde uma transação sem carteira é lançada não pode
        depender de ordenação."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await _nova(client, h, "AAA")  # ordenaria antes, se a ordem mandasse

        await client.post("/transactions", json=op(), headers=h)

        carteiras = (await client.get("/portfolios", headers=h)).json()
        real = next(c for c in carteiras if c["tipo"] == "real")
        na_real = (await client.get(f"/transactions?portfolio_id={real['id']}", headers=h)).json()
        assert na_real["total"] == 1

    async def test_snapshots_nao_colidem_entre_carteiras(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A chave do snapshot é (carteira, dia). Fosse (usuário, dia), a
        simulada sobrescreveria a real silenciosamente."""
        from datetime import date, timedelta
        from decimal import Decimal

        from app.models.asset import PriceHistory

        ativo = await criar_ativo(db, ticker="PETR4")
        for i in range(30):
            db.add(
                PriceHistory(
                    asset_id=ativo.id, date=date(2026, 1, 1) + timedelta(days=i), close=Decimal(25)
                )
            )
        await db.commit()

        _, h = await usuario_logado(client)
        sim = await _nova(client, h, "Simulada")
        await client.post(
            "/transactions", json=op(quantity="100", traded_at="2026-01-05"), headers=h
        )
        await client.post(
            f"/transactions?portfolio_id={sim}",
            json=op(quantity="10", traded_at="2026-01-05"),
            headers=h,
        )

        real = (await client.get("/portfolio/snapshots", headers=h)).json()
        simulada = (await client.get(f"/portfolio/snapshots?portfolio_id={sim}", headers=h)).json()

        assert real and simulada
        assert real[0]["valor_mercado"] != simulada[0]["valor_mercado"]

    async def test_metricas_e_otimizacao_respeitam_a_carteira(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        sim = await _nova(client, h, "Simulada")
        await client.post("/transactions", json=op(), headers=h)

        metricas = (await client.get(f"/portfolio/metrics?portfolio_id={sim}", headers=h)).json()
        otimizacao = (
            await client.post(f"/portfolio/optimize?portfolio_id={sim}", json={}, headers=h)
        ).json()

        assert metricas["ativos"] == []
        assert otimizacao["tickers"] == []


class TestAutorizacao:
    """Com `portfolio_id` vindo do cliente, a falha seria aceitá-lo sem conferir
    de quem é. Bastaria trocar um UUID na URL.
    """

    async def test_nao_le_carteira_alheia(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        sim = await _nova(client, dono, "Privada")
        await client.post(f"/transactions?portfolio_id={sim}", json=op(), headers=dono)

        outro = await segunda_conta(client)
        for rota in (
            f"/portfolio/positions?portfolio_id={sim}",
            f"/transactions?portfolio_id={sim}",
            f"/portfolio/summary?portfolio_id={sim}",
            f"/portfolio/metrics?portfolio_id={sim}",
            f"/portfolio/snapshots?portfolio_id={sim}",
            f"/portfolio/evolution?portfolio_id={sim}",
        ):
            resp = await client.get(rota, headers=outro)
            assert resp.status_code == 404, rota

    async def test_nao_escreve_em_carteira_alheia(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        sim = await _nova(client, dono, "Privada")
        outro = await segunda_conta(client)

        resp = await client.post(f"/transactions?portfolio_id={sim}", json=op(), headers=outro)
        assert resp.status_code == 404

    async def test_nao_apaga_carteira_alheia(self, client: AsyncClient) -> None:
        _, dono = await usuario_logado(client)
        sim = await _nova(client, dono, "Privada")
        outro = await segunda_conta(client)

        assert (await client.delete(f"/portfolios/{sim}", headers=outro)).status_code == 404
        # E ela continua lá para o dono.
        assert len((await client.get("/portfolios", headers=dono)).json()) == 2

    async def test_carteira_inexistente_devolve_404(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        resp = await client.get(f"/portfolio/positions?portfolio_id={uuid.uuid4()}", headers=h)
        assert resp.status_code == 404

    async def test_rotas_de_carteira_exigem_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.get("/portfolios")).status_code == 401
        assert (await client.post("/portfolios", json={"nome": "X"})).status_code == 401
        assert (await client.delete(f"/portfolios/{uuid.uuid4()}")).status_code == 401


class TestProtecaoDaCarteiraReal:
    """A carteira real não pode ser apagada.

    Ela é criada sozinha, é o padrão do sistema e é a que abre selecionada --
    ou seja, a mais fácil de apagar sem querer. A exclusão é destrutiva de
    verdade: leva o livro inteiro e todo o histórico de snapshots.

    A regra vive no domínio, não no botão. Estes testes batem na API direto,
    que é exatamente por onde um guard só de interface seria contornado.
    """

    async def test_apagar_a_real_devolve_409(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        carteiras = (await client.get("/portfolios", headers=h)).json()
        real = next(c for c in carteiras if c["tipo"] == "real")

        resp = await client.delete(f"/portfolios/{real['id']}", headers=h)

        # 409, e não 404 nem 403: ela existe, é sua, e mesmo assim é recusada.
        assert resp.status_code == 409
        assert "real" in resp.json()["detail"].lower()

    async def test_a_real_continua_la_depois_da_tentativa(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        real = next(
            c for c in (await client.get("/portfolios", headers=h)).json() if c["tipo"] == "real"
        )
        await client.delete(f"/portfolios/{real['id']}", headers=h)

        depois = (await client.get("/portfolios", headers=h)).json()
        assert any(c["id"] == real["id"] for c in depois)

    async def test_as_transacoes_da_real_sobrevivem(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O que a proteção existe para salvar: o livro de verdade."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(ticker="PETR4"), headers=h)
        real = next(
            c for c in (await client.get("/portfolios", headers=h)).json() if c["tipo"] == "real"
        )

        await client.delete(f"/portfolios/{real['id']}", headers=h)

        assert (await client.get("/transactions", headers=h)).json()["total"] == 1

    async def test_simulada_continua_apagavel(self, client: AsyncClient) -> None:
        """A proteção não pode virar uma trava geral: simulação descartada é
        justamente o caso de uso do botão."""
        _, h = await usuario_logado(client)
        simulada = await _nova(client, h, "Descartavel")

        assert (await client.delete(f"/portfolios/{simulada}", headers=h)).status_code == 204
        assert not any(
            c["id"] == simulada for c in (await client.get("/portfolios", headers=h)).json()
        )
