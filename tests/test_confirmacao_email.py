"""Confirmacao de e-mail no cadastro.

O fluxo tem tres promessas, e cada classe abaixo guarda uma:

1. **Ninguem entra sem provar que o e-mail e seu** -- e isso nao pode virar um
   oraculo de quem tem conta.
2. **O link e uma credencial**: uso unico, prazo, hash no banco, fora dos logs,
   e o reenvio aposenta o anterior.
3. **O envio nunca derruba o cadastro**, e em producao o app nao sobe sem ter
   como enviar.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import correio
from app.clients.correio import Email, EnviadorEmArquivo, EnviadorSmtp
from app.core.config import Settings
from app.models.email_confirmation import EmailConfirmationToken
from app.models.user import User
from app.services import confirmacao_service
from tests.factories import SENHA_PADRAO, criar_usuario, email_unico


def _caixa(client: AsyncClient):  # type: ignore[no-untyped-def]
    return client.caixa  # type: ignore[attr-defined]


async def _login(client: AsyncClient, email: str, senha: str = SENHA_PADRAO) -> int:
    resp = await client.post("/auth/login", data={"username": email, "password": senha})
    return resp.status_code


def _conteudos_eml(pasta: Path) -> list[bytes]:
    """Le os .eml fora da corrotina.

    Disco dentro de `async def` bloqueia o event loop -- o mesmo defeito que tirou
    o argon2 do caminho do login. Nao importa num teste, mas o ruff barra
    (ASYNC240), e um teste nao deveria ensinar o padrao que a aplicacao proibe.
    """
    return [arquivo.read_bytes() for arquivo in pasta.glob("*.eml")]


class _ColetorDeLog(logging.Handler):
    """Guarda as mensagens do logger em que for pendurado, sem depender da raiz."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.mensagens: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.mensagens.append(record.getMessage())


class TestNinguemEntraSemConfirmar:
    async def test_cadastro_manda_o_link_e_a_conta_nasce_nao_confirmada(
        self, client: AsyncClient
    ) -> None:
        email = email_unico()

        resp = await client.post("/auth/register", json={"email": email, "password": SENHA_PADRAO})

        assert resp.status_code == 201
        assert resp.json()["email_confirmado"] is False
        enviados = _caixa(client).para(email)
        assert len(enviados) == 1
        assert "Confirme seu e-mail" in enviados[0].assunto
        assert "#confirmar=" in enviados[0].texto

    async def test_login_antes_de_confirmar_responde_403(self, client: AsyncClient) -> None:
        """403 e nao 401: a senha estava certa, e o dono precisa saber que falta o
        e-mail -- nao ficar tentando a senha de novo e esbarrar no rate limit."""
        email, _ = await criar_usuario(client, confirmar=False)

        resp = await client.post("/auth/login", data={"username": email, "password": SENHA_PADRAO})

        assert resp.status_code == 403
        assert "Confirme seu e-mail" in resp.json()["detail"]

    async def test_senha_errada_em_conta_nao_confirmada_continua_401(
        self, client: AsyncClient
    ) -> None:
        """O teste que impede o 403 de virar oraculo.

        Se a checagem de confirmacao viesse ANTES da senha, qualquer um descobriria
        que existe uma conta pendente com esse e-mail sem saber a senha dela.
        """
        email, _ = await criar_usuario(client, confirmar=False)

        assert await _login(client, email, "senha-completamente-errada") == 401

    async def test_email_inexistente_continua_401(self, client: AsyncClient) -> None:
        assert await _login(client, email_unico("ninguem")) == 401

    async def test_confirmar_libera_o_login(self, client: AsyncClient) -> None:
        email, _ = await criar_usuario(client, confirmar=False)
        token = _caixa(client).token_para(email)

        conf = await client.post("/auth/confirmar", json={"token": token})

        assert conf.status_code == 200
        assert conf.json()["email"] == email
        assert await _login(client, email) == 200

    async def test_confirmar_nao_entrega_sessao(self, client: AsyncClient) -> None:
        """Quem le a caixa de outra pessoa nao pode ganhar a sessao dela de brinde:
        confirmar exige entrar depois, com a senha."""
        email, _ = await criar_usuario(client, confirmar=False)
        token = _caixa(client).token_para(email)

        conf = await client.post("/auth/confirmar", json={"token": token})

        assert "access_token" not in conf.json()
        assert "refresh_token" not in conf.cookies

    async def test_me_mostra_a_confirmacao(self, client: AsyncClient) -> None:
        email, _ = await criar_usuario(client)
        token = (
            await client.post("/auth/login", data={"username": email, "password": SENHA_PADRAO})
        ).json()["access_token"]

        me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert me.json()["email_confirmado"] is True

    async def test_demo_nasce_confirmada(self, client: AsyncClient) -> None:
        token = (await client.post("/auth/demo")).json()["access_token"]

        me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert me.json()["email_confirmado"] is True


class TestOLinkEUmaCredencial:
    async def test_o_token_vai_no_fragmento_e_nao_na_query(self, client: AsyncClient) -> None:
        """O navegador nunca manda o fragmento ao servidor. Com `?confirmar=`, o token
        cairia no log de acesso do uvicorn e no do proxy."""
        email, _ = await criar_usuario(client, confirmar=False)
        texto = _caixa(client).para(email)[-1].texto

        assert "/painel/#confirmar=" in texto
        assert "?confirmar=" not in texto

    async def test_o_banco_guarda_o_hash_e_nao_o_token(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        email, _ = await criar_usuario(client, confirmar=False)
        token = _caixa(client).token_para(email)

        registros = (await db.execute(select(EmailConfirmationToken.token_hash))).scalars().all()

        assert token not in registros
        assert confirmacao_service.hash_confirmation_token(token) in registros

    async def test_segundo_clique_no_mesmo_link_nao_assusta(self, client: AsyncClient) -> None:
        """Abrir o mesmo e-mail duas vezes nao e erro. Responder "link invalido"
        ali assustaria sem proteger nada -- quem tem o link tem o e-mail."""
        email, _ = await criar_usuario(client, confirmar=False)
        token = _caixa(client).token_para(email)

        primeira = await client.post("/auth/confirmar", json={"token": token})
        segunda = await client.post("/auth/confirmar", json={"token": token})

        assert primeira.status_code == 200
        assert segunda.status_code == 200

    async def test_link_expirado_e_recusado_e_a_conta_segue_bloqueada(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        email, _ = await criar_usuario(client, confirmar=False)
        token = _caixa(client).token_para(email)
        await db.execute(
            update(EmailConfirmationToken)
            .where(
                EmailConfirmationToken.user_id
                == select(User.id).where(User.email == email).scalar_subquery()
            )
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await db.commit()

        resp = await client.post("/auth/confirmar", json={"token": token})

        assert resp.status_code == 400
        assert await _login(client, email) == 403

    async def test_token_desconhecido_e_recusado(self, client: AsyncClient) -> None:
        resp = await client.post("/auth/confirmar", json={"token": "x" * 64})

        assert resp.status_code == 400

    async def test_token_gigante_e_recusado_antes_do_hash(self, client: AsyncClient) -> None:
        resp = await client.post("/auth/confirmar", json={"token": "x" * 100_000})

        assert resp.status_code == 422

    async def test_reenvio_aposenta_o_link_anterior(self, client: AsyncClient) -> None:
        """Dois links validos para a mesma conta sao dois alvos em vez de um."""
        email, _ = await criar_usuario(client, confirmar=False)
        antigo = _caixa(client).token_para(email)

        await client.post("/auth/confirmacao/reenviar", json={"email": email})
        novo = _caixa(client).token_para(email)

        assert novo != antigo
        assert (await client.post("/auth/confirmar", json={"token": antigo})).status_code == 400
        assert (await client.post("/auth/confirmar", json={"token": novo})).status_code == 200

    async def test_reenvio_nao_revela_se_a_conta_existe(self, client: AsyncClient) -> None:
        """Mesma resposta, mesmo status, para e-mail cadastrado e para e-mail que
        nunca existiu -- e so o primeiro recebe alguma coisa."""
        email, _ = await criar_usuario(client, confirmar=False)
        inexistente = email_unico("fantasma")
        antes = len(_caixa(client).enviados)

        com_conta = await client.post("/auth/confirmacao/reenviar", json={"email": email})
        sem_conta = await client.post("/auth/confirmacao/reenviar", json={"email": inexistente})

        assert com_conta.status_code == sem_conta.status_code == 202
        assert com_conta.json() == sem_conta.json()
        assert len(_caixa(client).enviados) == antes + 1
        assert _caixa(client).para(inexistente) == []

    async def test_reenvio_para_conta_ja_confirmada_nao_manda_nada(
        self, client: AsyncClient
    ) -> None:
        email, _ = await criar_usuario(client)
        antes = len(_caixa(client).para(email))

        resp = await client.post("/auth/confirmacao/reenviar", json={"email": email})

        assert resp.status_code == 202
        assert len(_caixa(client).para(email)) == antes

    async def test_reenvio_normaliza_o_email(self, client: AsyncClient) -> None:
        email, _ = await criar_usuario(client, confirmar=False)
        antes = len(_caixa(client).para(email))

        await client.post("/auth/confirmacao/reenviar", json={"email": f"  {email.upper()} "})

        assert len(_caixa(client).para(email)) == antes + 1


class TestOEnvioNaoDerrubaNada:
    async def test_smtp_fora_do_ar_nao_transforma_cadastro_em_500(
        self, client: AsyncClient
    ) -> None:
        """A conta ja existe quando o e-mail sai. Falha no envio vira log, e a
        pessoa pede o reenvio na tela de entrada."""
        _caixa(client).falhar = True

        resp = await client.post(
            "/auth/register", json={"email": email_unico(), "password": SENHA_PADRAO}
        )

        assert resp.status_code == 201

    async def test_smtp_faz_starttls_antes_do_login(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Autenticar antes do STARTTLS mandaria a senha do SMTP em texto puro."""
        passos: list[str] = []

        class SmtpFake:
            def __init__(self, host: str, porta: int, timeout: float) -> None:
                passos.append(f"conectar {host}:{porta}")

            def __enter__(self) -> SmtpFake:
                return self

            def __exit__(self, *_: object) -> None:
                passos.append("fechar")

            def starttls(self, context: object = None) -> None:
                passos.append("starttls")

            def login(self, usuario: str, senha: str) -> None:
                passos.append(f"login {usuario}")

            def send_message(self, msg: object) -> None:
                passos.append("enviar")

        monkeypatch.setattr(correio.smtplib, "SMTP", SmtpFake)
        settings = Settings(
            SMTP_HOST="smtp.exemplo.com", SMTP_USER="conta", SMTP_PASSWORD="segredo"
        )

        await EnviadorSmtp(settings).enviar(Email(para="a@exemplo.com", assunto="x", texto="y"))

        assert passos == [
            "conectar smtp.exemplo.com:587",
            "starttls",
            "login conta",
            "enviar",
            "fechar",
        ]

    async def test_sem_smtp_o_email_vira_arquivo_e_o_link_nao_vai_para_o_log(
        self, tmp_path: Path
    ) -> None:
        """Token de confirmacao em log e credencial em log.

        O arquivo e lido como e-mail de verdade, e nao como texto cru: a linha do
        link passa de 78 colunas, o corpo sai em quoted-printable, e o token aparece
        quebrado por `=` dentro do `.eml`. Procura-lo como substring falharia por um
        motivo que nao tem nada a ver com o que o teste quer provar.

        O log e coletado DIRETO no logger do modulo, e nao pelo `caplog`: o
        `create_app` chama `configurar()`, que SUBSTITUI os handlers da raiz e leva
        junto o handler que o `caplog` pendura ali. Mas isso nao bastava -- este
        teste passava sozinho e falhava na suite porque as migrations desligavam o
        proprio logger. Ver `test_as_migrations_nao_desligam_os_loggers_do_app`.
        """
        settings = Settings()
        token = "token-que-nao-pode-aparecer-no-log-0123456789"
        email = confirmacao_service.email_de_confirmacao("pessoa@exemplo.com", token, settings)

        coletor = _ColetorDeLog()
        logger = logging.getLogger("app.clients.correio")
        nivel_anterior = logger.level
        logger.addHandler(coletor)
        logger.setLevel(logging.WARNING)
        try:
            await EnviadorEmArquivo(tmp_path, settings.SMTP_REMETENTE).enviar(email)
        finally:
            logger.removeHandler(coletor)
            logger.setLevel(nivel_anterior)

        conteudos = _conteudos_eml(tmp_path)
        assert len(conteudos) == 1
        mensagem = message_from_bytes(conteudos[0], policy=policy.default)
        corpo = mensagem.get_body(preferencelist=("plain",))
        assert corpo is not None
        assert token in corpo.get_content()
        # O log existe -- senao a assercao seguinte seria vacua -- e nao traz o token.
        texto_do_log = "\n".join(coletor.mensagens)
        assert "gravado em" in texto_do_log
        assert token not in texto_do_log

    def test_as_migrations_nao_desligam_os_loggers_do_app(self, postgres: object) -> None:
        """A causa real do teste de log que passava sozinho e falhava na suite.

        O conftest roda as migrations no MESMO processo, e `migrations/env.py` chamava
        `fileConfig` com o padrao `disable_existing_loggers=True`: todo logger ja
        importado -- os do app inclusive -- era desligado ali, e qualquer log emitido
        depois sumia sem erro nenhum. Pedir o fixture `postgres` garante que as
        migrations ja rodaram quando este teste confere o logger.
        """
        assert logging.getLogger("app.clients.correio").disabled is False


class TestProducaoNaoSobeSemComoEnviar:
    """A trava existe porque o sintoma de SMTP ausente em producao seria "o cadastro
    nao funciona" -- os links iriam para um arquivo no servidor, longe da causa."""

    def test_sem_smtp_host(self) -> None:
        with pytest.raises(ValidationError, match="SMTP_HOST"):
            Settings(ENVIRONMENT="production", APP_URL="https://carteira.exemplo.com")

    def test_com_link_apontando_para_localhost(self) -> None:
        with pytest.raises(ValidationError, match="APP_URL"):
            Settings(ENVIRONMENT="production", SMTP_HOST="smtp.exemplo.com")

    def test_sem_criptografia(self) -> None:
        with pytest.raises(ValidationError, match="SMTP_TLS"):
            Settings(
                ENVIRONMENT="production",
                SMTP_HOST="smtp.exemplo.com",
                SMTP_TLS="nenhum",
                APP_URL="https://carteira.exemplo.com",
            )

    def test_fora_de_producao_sobe_sem_smtp(self) -> None:
        assert Settings(ENVIRONMENT="local").SMTP_HOST is None
