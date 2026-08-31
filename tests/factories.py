"""Fabricas de dados de teste.

Um teste que precisa de um usuario logado nao deveria repetir seis linhas de
cadastro e login. Alem de ruido, isso espalha o conhecimento de "como se cria um
usuario" por toda a suite -- e quando a rota muda, muda em vinte lugares.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset, AssetType, PriceHistory

_contador = itertools.count()

SENHA_PADRAO = "carteira-b3-2026-forte"


def email_unico(prefixo: str = "user") -> str:
    """Email distinto a cada chamada.

    Necessario porque a constraint UNIQUE nao sabe que estamos em teste: dois
    testes que usassem "a@b.com" colidiriam dependendo da ordem de execucao.
    """
    return f"{prefixo}{next(_contador)}@exemplo.com"


async def criar_usuario(
    client: AsyncClient, email: str | None = None, senha: str = SENHA_PADRAO
) -> tuple[str, str]:
    """Cadastra e devolve (email, senha)."""
    email = email or email_unico()
    resp = await client.post("/auth/register", json={"email": email, "password": senha})
    assert resp.status_code == 201, resp.text
    return email, senha


async def login(client: AsyncClient, email: str, senha: str = SENHA_PADRAO) -> str:
    """Autentica e devolve o access token. O cookie de refresh fica no client."""
    resp = await client.post("/auth/login", data={"username": email, "password": senha})
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["access_token"]
    return token


async def usuario_logado(client: AsyncClient) -> tuple[str, dict[str, str]]:
    """Atalho mais usado: devolve (email, cabecalho de autorizacao pronto)."""
    email, senha = await criar_usuario(client)
    token = await login(client, email, senha)
    return email, {"Authorization": f"Bearer {token}"}


async def criar_ativo(
    db: AsyncSession,
    ticker: str = "PETR4",
    nome: str | None = None,
    setor: str = "Energy",
    tipo: AssetType = AssetType.ACAO,
) -> Asset:
    """O nome padrao deriva do ticker de proposito.

    A primeira versao usava "Petroleo Brasileiro S.A." fixo -- e isso fez um teste
    de busca falhar por motivo errado: um ativo criado como VALE3 herdava esse
    nome e casava com a busca por "PETR". Valor padrao compartilhado entre
    objetos distintos e uma armadilha classica de fabrica de teste.
    """
    ativo = Asset(ticker=ticker, nome=nome or f"Empresa {ticker}", setor=setor, tipo=tipo)
    db.add(ativo)
    await db.commit()
    await db.refresh(ativo)
    return ativo


async def criar_historico(
    db: AsyncSession, ativo: Asset, dias: int = 5, inicial: str = "40.00"
) -> list[PriceHistory]:
    """Serie diaria sintetica, subindo 1% ao dia a partir de `inicial`.

    Valores deterministicos, nao aleatorios: um teste que usa numero aleatorio
    passa ou falha por sorte, e quando falha ninguem consegue reproduzir.
    """
    hoje = date(2026, 8, 26)
    pontos = []
    preco = Decimal(inicial)
    for i in range(dias):
        ponto = PriceHistory(
            asset_id=ativo.id,
            date=hoje - timedelta(days=dias - 1 - i),
            close=preco.quantize(Decimal("0.000001")),
            volume=1_000_000 + i,
        )
        db.add(ponto)
        pontos.append(ponto)
        preco *= Decimal("1.01")
    await db.commit()
    return pontos
