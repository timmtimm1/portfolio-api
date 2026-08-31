"""Testes das rotas de metricas."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset, PriceHistory
from tests.factories import criar_ativo, op, usuario_logado


async def _serie(db: AsyncSession, ativo: Asset, dias: int = 120, semente: int = 1) -> None:
    """Serie sintetica com semente fixa -- reprodutivel, nunca 'passa por sorte'."""
    rng = np.random.default_rng(semente)
    precos = np.cumprod(1 + rng.normal(0.0005, 0.012, dias)) * 100
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


class TestProtecao:
    async def test_metricas_da_carteira_exigem_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.get("/portfolio/metrics")).status_code == 401

    async def test_metricas_avulsas_exigem_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.get("/metrics?tickers=PETR4")).status_code == 401

    async def test_carteira_alheia_nao_aparece(self, client: AsyncClient, db: AsyncSession) -> None:
        from tests.factories import segunda_conta

        ativo = await criar_ativo(db, ticker="PETR4")
        await _serie(db, ativo)
        _, dono = await usuario_logado(client)
        await client.post("/transactions", json=op(), headers=dono)
        outro = await segunda_conta(client)

        assert (await client.get("/portfolio/metrics", headers=dono)).json()["ativos"] != []
        assert (await client.get("/portfolio/metrics", headers=outro)).json()["ativos"] == []


class TestCalculo:
    async def test_metricas_da_carteira(self, client: AsyncClient, db: AsyncSession) -> None:
        ativo = await criar_ativo(db, ticker="PETR4")
        await _serie(db, ativo)
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(), headers=h)

        corpo = (await client.get("/portfolio/metrics", headers=h)).json()

        assert len(corpo["ativos"]) == 1
        m = corpo["ativos"][0]
        assert m["ticker"] == "PETR4"
        assert m["volatilidade_anualizada"] > 0
        assert m["maior_queda"] <= 0
        assert corpo["taxa_livre_risco"] == 0.10

    async def test_correlacao_exige_dois_ativos(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A correlacao de um ativo consigo mesmo e 1 e nao informa nada.
        Devolver uma matriz 1x1 seria ruido com aparencia de resultado."""
        ativo = await criar_ativo(db, ticker="PETR4")
        await _serie(db, ativo)
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(), headers=h)

        assert (await client.get("/portfolio/metrics", headers=h)).json()["correlacao"] is None

    async def test_matriz_de_correlacao_com_varios_ativos(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        for i, t in enumerate(("PETR4", "VALE3", "ITUB4")):
            await _serie(db, await criar_ativo(db, ticker=t), semente=i + 1)
        _, h = await usuario_logado(client)

        corpo = (
            await client.get("/metrics?tickers=PETR4&tickers=VALE3&tickers=ITUB4", headers=h)
        ).json()
        c = corpo["correlacao"]

        assert c["tickers"] == ["ITUB4", "PETR4", "VALE3"]  # ordenados
        assert len(c["matriz"]) == 3
        assert all(len(linha) == 3 for linha in c["matriz"])
        assert all(abs(c["matriz"][i][i] - 1.0) < 1e-9 for i in range(3))

    async def test_ativo_sem_historico_e_reportado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Silenciar isso faria o usuario acreditar que a analise cobriu a
        carteira inteira."""
        await _serie(db, await criar_ativo(db, ticker="PETR4"))
        await criar_ativo(db, ticker="VALE3")  # sem historico
        _, h = await usuario_logado(client)

        corpo = (await client.get("/metrics?tickers=PETR4&tickers=VALE3", headers=h)).json()
        assert "VALE3" in corpo["sem_historico_suficiente"]

    async def test_carteira_vazia_nao_quebra(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        corpo = (await client.get("/portfolio/metrics", headers=h)).json()
        assert corpo["ativos"] == []
        assert corpo["correlacao"] is None

    async def test_teto_de_ativos_por_analise(self, client: AsyncClient) -> None:
        """A matriz cresce com o QUADRADO do numero de ativos: 500 seriam 250 mil
        celulas."""
        _, h = await usuario_logado(client)
        muitos = "&".join(f"tickers=AAA{i}" for i in range(60))
        assert (await client.get(f"/metrics?{muitos}", headers=h)).status_code == 422

    async def test_posicao_zerada_fica_de_fora(self, client: AsyncClient, db: AsyncSession) -> None:
        """Nao faz sentido medir o risco de um papel que o usuario nao tem mais."""
        ativo = await criar_ativo(db, ticker="PETR4")
        await _serie(db, ativo)
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(), headers=h)
        await client.post(
            "/transactions",
            json=op(side="venda", price="30", traded_at="2026-02-10"),
            headers=h,
        )

        assert (await client.get("/portfolio/metrics", headers=h)).json()["ativos"] == []
