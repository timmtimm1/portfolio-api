"""Fabricas de dados de teste.

Um teste que precisa de um usuario logado nao deveria repetir seis linhas de
cadastro e login. Alem de ruido, isso espalha o conhecimento de "como se cria um
usuario" por toda a suite -- e quando a rota muda, muda em vinte lugares.
"""

from __future__ import annotations

import itertools

from httpx import AsyncClient

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
