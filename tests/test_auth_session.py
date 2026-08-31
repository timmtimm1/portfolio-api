"""Testes de sessao: rota protegida, rotacao, deteccao de reuso e logout."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_token
from app.models.refresh_token import RefreshToken
from tests.factories import criar_usuario, login, usuario_logado


async def _envelhecer_revogacoes(db: AsyncSession, *, segundos: int) -> None:
    """Recua o `revoked_at` das linhas revogadas, simulando tempo passado.

    Permite testar a deteccao de roubo sem `sleep` na suite: teste que dorme e
    teste que ninguem roda.
    """
    from sqlalchemy import update

    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.revoked_at.is_not(None))
        .values(revoked_at=datetime.now(UTC) - timedelta(seconds=segundos))
    )
    await db.commit()


class TestRotaProtegida:
    async def test_sem_token_e_recusado(self, client: AsyncClient) -> None:
        assert (await client.get("/auth/me")).status_code == 401

    async def test_token_malformado_e_recusado(self, client: AsyncClient) -> None:
        resp = await client.get("/auth/me", headers={"Authorization": "Bearer nao.e.jwt"})
        assert resp.status_code == 401

    async def test_token_de_usuario_inexistente_e_recusado(self, client: AsyncClient) -> None:
        """Token com assinatura VALIDA, mas cujo `sub` aponta para ninguem.

        E o cenario de uma conta apagada depois da emissao do token. So e barrado
        porque `get_current_user` rele o usuario no banco em vez de confiar nas
        claims -- se confiasse, a conta apagada continuaria acessando.
        """
        import uuid

        token, _ = create_token(uuid.uuid4(), "access", timedelta(minutes=5))
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    async def test_token_expirado_e_recusado(self, client: AsyncClient) -> None:
        import uuid

        token, _ = create_token(uuid.uuid4(), "access", timedelta(seconds=-1))
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    async def test_refresh_token_nao_serve_como_access_token(self, client: AsyncClient) -> None:
        """Confusao de tipo: sem a claim `typ`, um token de 30 dias passaria como
        token de acesso e a expiracao curta viraria decoracao."""
        import uuid

        token, _ = create_token(uuid.uuid4(), "refresh", timedelta(minutes=5))
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    async def test_token_valido_devolve_o_proprio_usuario(self, client: AsyncClient) -> None:
        email, headers = await usuario_logado(client)
        resp = await client.get("/auth/me", headers=headers)

        assert resp.status_code == 200
        assert resp.json()["email"] == email
        assert "hashed_password" not in resp.json()

    async def test_um_usuario_nao_ve_os_dados_do_outro(self, client: AsyncClient) -> None:
        """`/auth/me` nao aceita identificador nenhum: a identidade vem do token.
        Este teste fixa esse contrato -- se alguem adicionar um parametro de id
        aceito do cliente, ele quebra."""
        _, headers_a = await usuario_logado(client)
        email_b, headers_b = await usuario_logado(client)

        assert (await client.get("/auth/me", headers=headers_a)).json()["email"] != email_b
        assert (await client.get("/auth/me", headers=headers_b)).json()["email"] == email_b


class TestRotacao:
    async def test_refresh_emite_par_novo(self, client: AsyncClient) -> None:
        email, senha = await criar_usuario(client)
        await login(client, email, senha)
        antigo = client.cookies["refresh_token"]

        resp = await client.post("/auth/refresh")

        assert resp.status_code == 200
        assert resp.json()["access_token"]
        assert client.cookies["refresh_token"] != antigo

    async def test_refresh_sem_cookie_e_recusado(self, client: AsyncClient) -> None:
        assert (await client.post("/auth/refresh")).status_code == 401

    async def test_token_antigo_nao_funciona_apos_rotacao(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        email, senha = await criar_usuario(client)
        await login(client, email, senha)
        antigo = client.cookies["refresh_token"]
        await client.post("/auth/refresh")
        await _envelhecer_revogacoes(db, segundos=120)

        client.cookies.set("refresh_token", antigo, path="/api/v1/auth")
        resp = await client.post("/auth/refresh")
        assert resp.status_code == 401

    async def test_duas_abas_abrindo_juntas_nao_derrubam_a_sessao(
        self, client: AsyncClient
    ) -> None:
        """Regressao de um problema encontrado usando o frontend de verdade.

        Duas abas abrindo ao mesmo tempo chamam /auth/refresh com o MESMO cookie.
        A segunda apresenta o token que a primeira acabou de rotacionar -- e sem
        a janela de tolerancia isso era interpretado como roubo, derrubando todas
        as sessoes. O usuario era deslogado por abrir duas abas.
        """
        email, senha = await criar_usuario(client)
        await login(client, email, senha)
        compartilhado = client.cookies["refresh_token"]

        # Aba 1 renova.
        assert (await client.post("/auth/refresh")).status_code == 200
        depois_da_aba1 = client.cookies["refresh_token"]

        # Aba 2 chega com o cookie antigo, milissegundos depois.
        client.cookies.set("refresh_token", compartilhado, path="/api/v1/auth")
        assert (await client.post("/auth/refresh")).status_code == 200

        # E a sessao da aba 1 continua viva.
        client.cookies.set("refresh_token", depois_da_aba1, path="/api/v1/auth")
        assert (await client.post("/auth/refresh")).status_code == 200

    async def test_reuso_derruba_todas_as_sessoes(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O teste central da etapa.

        Cenario: o atacante copia o refresh token; o usuario renova normalmente
        (o token copiado e revogado); o atacante entao usa a copia. Isso so pode
        significar que alguem tem uma credencial que nao deveria -- e a resposta
        correta (RFC 9700) e derrubar TUDO, inclusive a sessao legitima em curso.
        """
        email, senha = await criar_usuario(client)
        await login(client, email, senha)
        roubado = client.cookies["refresh_token"]

        await client.post("/auth/refresh")  # usuario renova; `roubado` e revogado
        atual = client.cookies["refresh_token"]

        # Envelhece a revogacao para alem da janela de tolerancia: sem isso, a
        # reapresentacao imediata seria tratada como corrida entre abas, nao
        # como roubo. Um token realmente roubado e usado muito depois.
        await _envelhecer_revogacoes(db, segundos=120)

        # O atacante usa a copia.
        client.cookies.set("refresh_token", roubado, path="/api/v1/auth")
        assert (await client.post("/auth/refresh")).status_code == 401

        # E a sessao legitima cai junto -- este e o ponto.
        client.cookies.set("refresh_token", atual, path="/api/v1/auth")
        assert (await client.post("/auth/refresh")).status_code == 401

        ativos = await db.scalar(
            select(func.count()).select_from(RefreshToken).where(RefreshToken.revoked_at.is_(None))
        )
        assert ativos == 0

    async def test_token_expirado_e_recusado(self, client: AsyncClient, db: AsyncSession) -> None:
        email, senha = await criar_usuario(client)
        await login(client, email, senha)

        registro = (
            await db.execute(select(RefreshToken).where(RefreshToken.revoked_at.is_(None)))
        ).scalar_one()
        registro.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

        assert (await client.post("/auth/refresh")).status_code == 401

    async def test_banco_nunca_guarda_o_token_em_texto_puro(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Um dump vazado nao pode conter sessao utilizavel."""
        email, senha = await criar_usuario(client)
        await login(client, email, senha)
        bruto = client.cookies["refresh_token"]

        hashes = (await db.execute(select(RefreshToken.token_hash))).scalars().all()
        assert bruto not in hashes
        assert all(len(h) == 64 for h in hashes)  # SHA-256 em hexadecimal


class TestLogout:
    async def test_logout_revoga_no_servidor(self, client: AsyncClient) -> None:
        """Apagar o cookie nao basta: uma copia do token seguiria valida."""
        email, senha = await criar_usuario(client)
        await login(client, email, senha)
        token = client.cookies["refresh_token"]

        assert (await client.post("/auth/logout")).status_code == 204
        client.cookies.set("refresh_token", token, path="/api/v1/auth")
        assert (await client.post("/auth/refresh")).status_code == 401

    async def test_logout_e_idempotente(self, client: AsyncClient) -> None:
        """Um cliente que desloga duas vezes deve terminar deslogado, nao em
        duvida sobre o proprio estado."""
        email, senha = await criar_usuario(client)
        await login(client, email, senha)

        assert (await client.post("/auth/logout")).status_code == 204
        assert (await client.post("/auth/logout")).status_code == 204

    async def test_logout_sem_sessao_nao_falha(self, client: AsyncClient) -> None:
        assert (await client.post("/auth/logout")).status_code == 204


class TestJanelaDeTolerancia:
    """A tolerância vale só para revogação por ROTAÇÃO.

    Estes testes existem porque a primeira versão aplicava a janela a qualquer
    revogação -- e um teste flagrou que, com isso, o token voltava a funcionar
    por dez segundos DEPOIS do logout. Logout que não desloga na hora não é
    logout.
    """

    async def test_logout_nao_ganha_tolerancia(self, client: AsyncClient) -> None:
        email, senha = await criar_usuario(client)
        await login(client, email, senha)
        token = client.cookies["refresh_token"]

        await client.post("/auth/logout")

        # Imediatamente após o logout — dentro da janela, se ela valesse aqui.
        client.cookies.set("refresh_token", token, path="/api/v1/auth")
        assert (await client.post("/auth/refresh")).status_code == 401

    async def test_revogacao_por_seguranca_nao_ganha_tolerancia(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Tolerar aqui anularia a defesa que acabou de disparar."""
        from app.models.user import User
        from app.services.token_service import revogar_todos_do_usuario

        email, senha = await criar_usuario(client)
        await login(client, email, senha)
        token = client.cookies["refresh_token"]

        usuario = (await db.execute(select(User).where(User.email == email))).scalar_one()
        await revogar_todos_do_usuario(db, usuario.id)

        client.cookies.set("refresh_token", token, path="/api/v1/auth")
        assert (await client.post("/auth/refresh")).status_code == 401

    async def test_motivo_da_revogacao_e_registrado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        from app.models.refresh_token import MotivoRevogacao

        email, senha = await criar_usuario(client)
        await login(client, email, senha)
        await client.post("/auth/refresh")  # rotação
        await client.post("/auth/logout")  # logout

        motivos = (await db.execute(select(RefreshToken.revoked_reason))).scalars().all()
        assert MotivoRevogacao.ROTACAO in motivos
        assert MotivoRevogacao.LOGOUT in motivos
