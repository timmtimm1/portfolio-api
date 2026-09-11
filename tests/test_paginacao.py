"""Paginacao: o envelope `Page` e a estabilidade da ordem entre paginas.

Arquivo proprio, e nao dentro de `test_transactions.py`, porque o assunto e o
contrato de paginacao -- que vale para toda rota que devolve `Page` --, nao o
livro de transacoes em si.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction, TransactionSide
from app.schemas.common import Page
from tests.factories import carteira_de, criar_ativo, usuario_logado


class TestEnvelope:
    """`tem_proxima` existia no codigo e nunca chegou a cliente nenhum.

    Era `@property`, e o Pydantic nao serializa property: o valor funcionava em
    Python, nao aparecia no JSON e nao constava no OpenAPI. Um recurso morto que
    parecia vivo para quem lia o schema.
    """

    def test_tem_proxima_chega_ao_json(self) -> None:
        pagina = Page[int](items=[1, 2], total=10, limit=2, offset=0)

        assert pagina.model_dump()["tem_proxima"] is True

    def test_ultima_pagina_nao_tem_proxima(self) -> None:
        pagina = Page[int](items=[9, 10], total=10, limit=2, offset=8)

        assert pagina.model_dump()["tem_proxima"] is False

    def test_pagina_vazia_alem_do_fim_nao_tem_proxima(self) -> None:
        """offset alem do total: lista vazia, e nao ha para onde avancar."""
        pagina = Page[int](items=[], total=10, limit=2, offset=50)

        assert pagina.model_dump()["tem_proxima"] is False

    def test_tem_proxima_consta_no_contrato_openapi(self) -> None:
        """Estar no JSON e nao estar no schema e meio recurso: gerador de
        cliente (openapi-generator, orval) so enxerga o que o schema declara."""
        schema = Page[int].model_json_schema(mode="serialization")

        assert "tem_proxima" in schema["properties"]

    async def test_a_rota_de_transacoes_devolve_tem_proxima(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        ativo = await criar_ativo(db, ticker="PETR4")
        email, h = await usuario_logado(client)
        carteira = await carteira_de(db, email)
        for dia in range(1, 4):
            db.add(_compra(carteira.user_id, carteira.id, ativo.id, date(2026, 3, dia)))
        await db.commit()

        primeira = (await client.get("/transactions?limit=2&offset=0", headers=h)).json()
        ultima = (await client.get("/transactions?limit=2&offset=2", headers=h)).json()

        assert primeira["tem_proxima"] is True
        assert ultima["tem_proxima"] is False


class TestOrdemEstavel:
    """Paginas por OFFSET so fazem sentido se a ordem for TOTAL.

    O extrato ordenava por (traded_at, created_at). Os dois empatam com
    facilidade: `created_at` vem de `now()` do Postgres, que devolve o INICIO da
    transacao -- toda linha gravada no mesmo commit recebe o mesmo carimbo. A
    conta de demonstracao grava o livro inteiro num commit so, com PETR4, VALE3
    e TAEE11 no mesmo dia: empate total em tres das cinco linhas.

    Com empate, a ordem entre as linhas empatadas e indefinida, e pode mudar
    entre `OFFSET 0` e `OFFSET 7`: o Postgres escolhe o algoritmo de ordenacao
    conforme LIMIT+OFFSET, e o heapsort de top-N nao e estavel. Resultado:
    linha repetida numa pagina e linha que nunca aparece em pagina nenhuma.
    """

    async def test_percorrer_todas_as_paginas_ve_cada_linha_uma_vez(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        total = 60
        ativo = await criar_ativo(db, ticker="PETR4")
        email, h = await usuario_logado(client)
        carteira = await carteira_de(db, email)
        # Empate total de proposito: mesma data e um commit so, logo o mesmo
        # created_at. E o pior caso, e e exatamente o que o demo produz.
        for _ in range(total):
            db.add(_compra(carteira.user_id, carteira.id, ativo.id, date(2026, 3, 2)))
        await db.commit()

        vistos: list[str] = []
        tamanho = 7
        for offset in range(0, total + tamanho, tamanho):
            pagina = (
                await client.get(f"/transactions?limit={tamanho}&offset={offset}", headers=h)
            ).json()
            vistos.extend(item["id"] for item in pagina["items"])

        repetidos = len(vistos) - len(set(vistos))
        assert repetidos == 0, f"{repetidos} linha(s) apareceram em mais de uma pagina"
        assert len(set(vistos)) == total, f"{total - len(set(vistos))} linha(s) nunca apareceram"


def _compra(user_id: object, portfolio_id: object, asset_id: object, dia: date) -> Transaction:
    return Transaction(
        user_id=user_id,
        portfolio_id=portfolio_id,
        asset_id=asset_id,
        side=TransactionSide.COMPRA,
        quantity=Decimal(1),
        price=Decimal("10.00"),
        fees=Decimal(0),
        traded_at=dia,
    )
