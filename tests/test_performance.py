"""Testes de forma das consultas.

Um endpoint com N+1 devolve a resposta correta -- ele so e lento. Nenhum teste
funcional pega isso; so contar as consultas pega.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import criar_ativo, op, usuario_logado


async def test_listagem_nao_faz_n_mais_1(
    client: AsyncClient, db: AsyncSession, contar_queries: list[str]
) -> None:
    """O numero de consultas nao pode crescer com a quantidade de transacoes.

    Sem `selectinload`, cada linha do extrato dispararia um SELECT para buscar o
    ticker do ativo: 20 transacoes = 21 consultas. Com ele, sao duas -- uma para
    as transacoes, uma para os ativos -- independentemente do volume.
    """
    await criar_ativo(db, ticker="PETR4")
    await criar_ativo(db, ticker="VALE3")
    _, h = await usuario_logado(client)
    for i in range(10):
        ticker = "PETR4" if i % 2 else "VALE3"
        await client.post("/transactions", json=op(ticker=ticker, quantity="1"), headers=h)

    contar_queries.clear()
    resp = await client.get("/transactions?limit=20", headers=h)

    assert resp.json()["total"] == 10
    # 1 usuario (get_current_user) + 1 contagem + 1 transacoes + 1 ativos = 4.
    # A margem existe para nao amarrar o teste a detalhes internos; o que importa
    # e ser CONSTANTE, nao crescer com as 10 linhas.
    assert len(contar_queries) <= 5, "\n".join(contar_queries)


async def test_posicoes_nao_fazem_n_mais_1(
    client: AsyncClient, db: AsyncSession, contar_queries: list[str]
) -> None:
    """O calculo de posicao le o livro inteiro. Precisa ler em duas consultas,
    nao numa por ativo."""
    for t in ("PETR4", "VALE3", "ITUB4", "BBAS3"):
        await criar_ativo(db, ticker=t)
    _, h = await usuario_logado(client)
    for t in ("PETR4", "VALE3", "ITUB4", "BBAS3"):
        await client.post("/transactions", json=op(ticker=t, quantity="10"), headers=h)

    contar_queries.clear()
    resp = await client.get("/portfolio/positions", headers=h)

    assert len(resp.json()) == 4
    assert len(contar_queries) <= 4, "\n".join(contar_queries)


async def test_acesso_nao_carregado_ao_ativo_levanta_excecao(
    client: AsyncClient, db: AsyncSession
) -> None:
    """`lazy="raise"` no relacionamento faz o N+1 falhar alto.

    Sem isso, esquecer o `selectinload` produz codigo que funciona e e lento --
    o pior defeito possivel, porque nada denuncia. Com "raise", o esquecimento
    quebra na hora, em desenvolvimento.
    """
    import pytest
    from sqlalchemy import select

    from app.models.transaction import Transaction

    await criar_ativo(db, ticker="PETR4")
    _, h = await usuario_logado(client)
    await client.post("/transactions", json=op(), headers=h)

    # Consulta SEM selectinload -- exatamente o esquecimento que causa N+1.
    transacao = (await db.execute(select(Transaction))).scalars().first()
    assert transacao is not None
    with pytest.raises(Exception, match=r"lazy='raise'"):
        _ = transacao.asset.ticker
