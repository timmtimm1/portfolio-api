"""Testes da rota de otimizacao."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset, PriceHistory
from tests.factories import criar_ativo, op, segunda_conta, usuario_logado


async def _serie(db: AsyncSession, ativo: Asset, dias: int = 200, semente: int = 1) -> None:
    rng = np.random.default_rng(semente)
    precos = np.cumprod(1 + rng.normal(0.0006, 0.013, dias)) * 100
    base = date(2026, 1, 1)
    for i, preco in enumerate(precos):
        db.add(
            PriceHistory(
                asset_id=ativo.id,
                date=base + timedelta(days=i),
                close=Decimal(str(round(float(preco), 6))),
            )
        )
    await db.commit()


async def _carteira_com(client: AsyncClient, db: AsyncSession, *tickers: str) -> dict[str, str]:
    _, h = await usuario_logado(client)
    for i, t in enumerate(tickers):
        await _serie(db, await criar_ativo(db, ticker=t), semente=i + 1)
        await client.post("/transactions", json=op(ticker=t, quantity="10"), headers=h)
    return h


class TestProtecao:
    async def test_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.post("/portfolio/optimize", json={})).status_code == 401

    async def test_nao_usa_a_carteira_de_outro_usuario(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")
        outro = await segunda_conta(client)

        corpo = (await client.post("/portfolio/optimize", json={}, headers=outro)).json()
        assert corpo["tickers"] == []
        assert corpo["fronteira"] == []


class TestResultado:
    async def test_otimiza_a_carteira_do_usuario(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")

        corpo = (await client.post("/portfolio/optimize", json={"pontos": 10}, headers=h)).json()

        assert corpo["tickers"] == ["ITUB4", "PETR4", "VALE3"]
        assert len(corpo["fronteira"]) > 1
        assert corpo["minima_variancia"] is not None
        assert corpo["maximo_sharpe"] is not None

    async def test_pesos_somam_um_e_sao_nomeados(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O otimizador trabalha com indices; a API devolve nomes. Peso certo no
        ativo errado seria o pior bug possivel aqui -- os numeros pareceriam
        plausiveis e ninguem notaria."""
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")

        corpo = (await client.post("/portfolio/optimize", json={"pontos": 8}, headers=h)).json()
        pesos = corpo["minima_variancia"]["pesos"]

        assert set(pesos) == {"ITUB4", "PETR4", "VALE3"}
        assert sum(pesos.values()) == float(1) or abs(sum(pesos.values()) - 1) < 1e-6
        assert all(p >= 0 for p in pesos.values())

    async def test_respeita_o_limite_por_ativo(self, client: AsyncClient, db: AsyncSession) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4", "BBAS3")

        corpo = (
            await client.post(
                "/portfolio/optimize", json={"peso_maximo": 0.30, "pontos": 8}, headers=h
            )
        ).json()

        for carteira in (corpo["minima_variancia"], corpo["maximo_sharpe"], *corpo["fronteira"]):
            assert max(carteira["pesos"].values()) <= 0.30 + 1e-6

    async def test_devolve_a_carteira_atual_para_comparar(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """ "Voce esta aqui, a fronteira esta ali" -- a comparacao que interessa."""
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")

        corpo = (await client.post("/portfolio/optimize", json={"pontos": 8}, headers=h)).json()
        atual = corpo["carteira_atual"]

        assert atual is not None
        assert abs(sum(atual["pesos"].values()) - 1) < 1e-6

    async def test_carteira_atual_e_nula_para_ativos_avulsos(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Pedindo ativos que nao possui, nao ha carteira atual a comparar."""
        _, h = await usuario_logado(client)
        for i, t in enumerate(("PETR4", "VALE3", "ITUB4")):
            await _serie(db, await criar_ativo(db, ticker=t), semente=i + 1)

        corpo = (
            await client.post(
                "/portfolio/optimize",
                json={"tickers": ["PETR4", "VALE3", "ITUB4"], "pontos": 8},
                headers=h,
            )
        ).json()
        assert corpo["carteira_atual"] is None
        assert len(corpo["fronteira"]) > 1

    async def test_minima_variancia_tem_menos_risco_que_maximo_sharpe(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4", "BBAS3")

        corpo = (await client.post("/portfolio/optimize", json={"pontos": 8}, headers=h)).json()
        assert (
            corpo["minima_variancia"]["volatilidade"]
            <= corpo["maximo_sharpe"]["volatilidade"] + 1e-9
        )

    async def test_aviso_sobre_a_limitacao_vem_sempre(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Uma ferramenta que sugere alocacao de dinheiro tem que dizer no que
        ela se baseia -- e no que ela nao se baseia."""
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")
        corpo = (await client.post("/portfolio/optimize", json={"pontos": 6}, headers=h)).json()
        assert "nao e recomendacao de investimento" in corpo["aviso"]


class TestCasosDeBorda:
    async def test_menos_de_dois_ativos_devolve_vazio(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Com um ativo so, a resposta seria '100% nele': correta e inutil."""
        h = await _carteira_com(client, db, "PETR4")
        corpo = (await client.post("/portfolio/optimize", json={}, headers=h)).json()
        assert corpo["fronteira"] == []
        assert corpo["minima_variancia"] is None

    async def test_carteira_vazia_nao_quebra(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        corpo = (await client.post("/portfolio/optimize", json={}, headers=h)).json()
        assert corpo["tickers"] == []

    async def test_limite_impossivel_devolve_vazio_nao_500(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """2 ativos com teto de 20% somam no maximo 40%. A restricao veio do
        usuario, entao a resposta e vazia e explicita -- nunca um 500."""
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        resp = await client.post(
            "/portfolio/optimize", json={"peso_maximo": 0.20, "pontos": 6}, headers=h
        )
        assert resp.status_code == 200
        assert resp.json()["fronteira"] == []

    async def test_ativo_sem_historico_e_reportado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")
        await criar_ativo(db, ticker="XXXX3")
        await client.post("/transactions", json=op(ticker="XXXX3", quantity="1"), headers=h)

        corpo = (await client.post("/portfolio/optimize", json={"pontos": 6}, headers=h)).json()
        assert "XXXX3" in corpo["sem_historico_suficiente"]

    async def test_peso_maximo_fora_da_faixa_e_recusado(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        for valor in (0.01, 1.5, -1):
            resp = await client.post("/portfolio/optimize", json={"peso_maximo": valor}, headers=h)
            assert resp.status_code == 422, valor

    async def test_teto_de_ativos_por_pedido(self, client: AsyncClient) -> None:
        """A otimizacao roda um solver por ponto da fronteira: sem teto, um pedido
        com centenas de ativos prende o worker por minutos."""
        _, h = await usuario_logado(client)
        resp = await client.post(
            "/portfolio/optimize",
            json={"tickers": [f"AAA{i}" for i in range(40)]},
            headers=h,
        )
        assert resp.status_code == 422

    async def test_pontos_fora_da_faixa_e_recusado(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        assert (
            await client.post("/portfolio/optimize", json={"pontos": 5000}, headers=h)
        ).status_code == 422
