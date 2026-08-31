"""Testes da rota de login -- com foco no que ela NAO pode revelar."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from tests.factories import SENHA_PADRAO, criar_usuario, email_unico


async def test_login_bem_sucedido_devolve_token(client: AsyncClient) -> None:
    email, senha = await criar_usuario(client)
    resp = await client.post("/auth/login", data={"username": email, "password": senha})

    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["token_type"] == "bearer"
    assert corpo["expires_in"] > 0
    assert corpo["access_token"].count(".") == 2  # cabecalho.payload.assinatura


async def test_login_aceita_email_em_outra_caixa(client: AsyncClient) -> None:
    email, senha = await criar_usuario(client, email="Maiuscula@Exemplo.com")
    resp = await client.post(
        "/auth/login", data={"username": "MAIUSCULA@exemplo.COM", "password": senha}
    )
    assert resp.status_code == 200


async def test_senha_errada_e_email_inexistente_sao_indistinguiveis(client: AsyncClient) -> None:
    """O teste mais importante deste arquivo.

    Se as duas respostas diferirem em qualquer byte -- mensagem, codigo, formato
    -- o login vira um oraculo de quais emails tem conta no sistema. Um atacante
    varre uma lista vazada de emails e descobre quem e cliente antes mesmo de
    tentar quebrar uma senha.
    """
    email, _ = await criar_usuario(client)

    senha_errada = await client.post(
        "/auth/login", data={"username": email, "password": "senha-completamente-errada"}
    )
    inexistente = await client.post(
        "/auth/login", data={"username": email_unico(), "password": "senha-completamente-errada"}
    )

    assert senha_errada.status_code == inexistente.status_code == 401
    assert senha_errada.json() == inexistente.json()


async def test_conta_desativada_nao_se_revela(client: AsyncClient, db: AsyncSession) -> None:
    """Conta desativada responde igual a senha errada.

    Se respondesse "conta desativada", um atacante saberia que acertou a senha --
    a informacao mais valiosa que ele poderia obter -- e so precisaria esperar a
    conta ser reativada.
    """
    email, senha = await criar_usuario(client)
    usuario = (await db.execute(select(User).where(User.email == email))).scalar_one()
    usuario.is_active = False
    await db.commit()

    resp = await client.post("/auth/login", data={"username": email, "password": senha})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Email ou senha incorretos"


async def test_login_define_cookie_httponly(client: AsyncClient) -> None:
    """O refresh token nao pode ser legivel por JavaScript: uma unica falha de XSS
    entregaria uma sessao de 30 dias."""
    email, senha = await criar_usuario(client)
    resp = await client.post("/auth/login", data={"username": email, "password": senha})

    set_cookie = resp.headers["set-cookie"]
    assert "refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie.replace("samesite", "SameSite")
    assert "Path=/api/v1/auth" in set_cookie


async def test_refresh_token_nao_vai_no_corpo(client: AsyncClient) -> None:
    email, senha = await criar_usuario(client)
    resp = await client.post("/auth/login", data={"username": email, "password": senha})
    assert "refresh" not in resp.json()


async def test_senha_nunca_aparece_na_resposta(client: AsyncClient) -> None:
    email, _ = await criar_usuario(client)
    resp = await client.post("/auth/login", data={"username": email, "password": SENHA_PADRAO})
    assert SENHA_PADRAO not in resp.text


class TestRespostaDeLimite:
    """O 429 precisa dizer a verdade, e dizer quanto esperar.

    Regressao de um problema real encontrado usando o frontend: o cliente
    mostrava "E-mail ou senha incorretos" para QUALQUER falha. Quando o rate
    limit respondeu 429, a tela acusou senha errada -- e a pessoa tentou de
    novo, gastando mais tentativas e prolongando o proprio bloqueio.
    """

    def test_janela_do_limite_vira_segundos(self) -> None:
        from app.core.rate_limit import _segundos_da_janela

        assert _segundos_da_janela("5 per 1 minute") == 60
        assert _segundos_da_janela("10 per 1 hour") == 3600
        assert _segundos_da_janela("2 per 1 second") == 1
        assert _segundos_da_janela("formato inesperado") == 60  # fallback seguro

    async def test_resposta_de_excesso_traz_retry_after(self) -> None:
        """Sem `Retry-After` o cliente so pode chutar quanto esperar -- e chuta
        cedo demais, prolongando o bloqueio. O RFC 6585 define o 429 com esse
        cabecalho justamente por isso."""
        from slowapi.errors import RateLimitExceeded

        from app.core.rate_limit import excesso_de_requisicoes

        class LimiteFalso:
            error_message = None
            limit = None

        excecao = RateLimitExceeded(LimiteFalso())  # type: ignore[arg-type]
        excecao.detail = "5 per 1 minute"

        resposta = await excesso_de_requisicoes(None, excecao)  # type: ignore[arg-type]

        assert resposta.status_code == 429
        assert resposta.headers["Retry-After"] == "60"

    async def test_refresh_tem_limite_proprio_e_mais_folgado(self) -> None:
        """O frontend chama `refresh` a cada carregamento de pagina. Se ele
        dividisse o balde do login, seis F5 num minuto bloqueariam o proprio
        usuario -- e forca bruta contra um token de 384 bits nao e a ameaca aqui.
        """
        from app.core.config import get_settings

        settings = get_settings()
        limite = lambda s: int(s.split("/")[0])  # noqa: E731
        assert limite(settings.RATE_LIMIT_REFRESH) > limite(settings.RATE_LIMIT_LOGIN)
