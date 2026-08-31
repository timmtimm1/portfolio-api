"""Testes da rota de cadastro."""

from __future__ import annotations

from httpx import AsyncClient

from tests.factories import SENHA_PADRAO, email_unico


async def test_cadastro_bem_sucedido(client: AsyncClient) -> None:
    email = email_unico()
    resp = await client.post("/auth/register", json={"email": email, "password": SENHA_PADRAO})

    assert resp.status_code == 201
    corpo = resp.json()
    assert corpo["email"] == email
    assert corpo["is_active"] is True


async def test_resposta_nunca_expoe_o_hash_da_senha(client: AsyncClient) -> None:
    """Regressao deliberada: se alguem trocar `UserRead` pelo model do ORM, este
    teste falha. E o unico guarda-corpo contra vazar coluna nova sem querer."""
    resp = await client.post(
        "/auth/register", json={"email": email_unico(), "password": SENHA_PADRAO}
    )
    corpo = resp.json()
    assert "hashed_password" not in corpo
    assert "password" not in corpo
    assert SENHA_PADRAO not in resp.text


async def test_email_e_normalizado_para_minusculas(client: AsyncClient) -> None:
    resp = await client.post(
        "/auth/register", json={"email": "  Bernardo.Timm@Exemplo.COM ", "password": SENHA_PADRAO}
    )
    assert resp.json()["email"] == "bernardo.timm@exemplo.com"


async def test_email_duplicado_em_outra_caixa_e_rejeitado(client: AsyncClient) -> None:
    """Sem a normalizacao, este cadastro passaria e a conta ficaria duplicada."""
    await client.post("/auth/register", json={"email": "dup@exemplo.com", "password": SENHA_PADRAO})
    resp = await client.post(
        "/auth/register", json={"email": "DUP@Exemplo.com", "password": SENHA_PADRAO}
    )
    assert resp.status_code == 409


async def test_senha_curta_e_rejeitada(client: AsyncClient) -> None:
    resp = await client.post("/auth/register", json={"email": email_unico(), "password": "curta12"})
    assert resp.status_code == 422


async def test_senha_gigante_e_rejeitada_antes_do_argon2(client: AsyncClient) -> None:
    """Sem o teto de 128 caracteres, um POST com senha de megabytes faria o argon2
    consumir CPU e memoria por segundos -- negacao de servico de graca."""
    resp = await client.post(
        "/auth/register", json={"email": email_unico(), "password": "a" * 200_000}
    )
    assert resp.status_code == 422


async def test_senha_derivada_do_email_e_rejeitada(client: AsyncClient) -> None:
    resp = await client.post(
        "/auth/register", json={"email": "bernardo@exemplo.com", "password": "bernardo123456"}
    )
    assert resp.status_code == 422


async def test_email_invalido_e_rejeitado(client: AsyncClient) -> None:
    resp = await client.post(
        "/auth/register", json={"email": "nao-e-email", "password": SENHA_PADRAO}
    )
    assert resp.status_code == 422
