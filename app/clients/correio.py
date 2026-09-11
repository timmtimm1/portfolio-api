"""Envio de e-mail: SMTP de verdade, ou arquivo local quando nao ha SMTP.

Mesmo papel de `get_provedor_de_cotacoes`: a rota recebe o enviador por
dependencia, e o teste o troca por uma caixa em memoria. Nenhum teste toca a
rede, e nenhuma rota sabe se o e-mail saiu por SMTP ou foi para um arquivo.
"""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Um servidor que aceita a conexao e nunca responde prenderia a thread para
# sempre. 10 s cobre um provedor lento; passado disso, o link ja chega tarde.
TIMEOUT_SMTP_SEGUNDOS = 10.0


@dataclass(frozen=True)
class Email:
    para: str
    assunto: str
    texto: str
    html: str | None = None


class EnviadorDeEmail(Protocol):
    async def enviar(self, email: Email) -> None: ...


def _mensagem(email: Email, remetente: str) -> EmailMessage:
    """Texto puro sempre, HTML como alternativa.

    Cliente de e-mail que nao renderiza HTML -- ou que bloqueia por politica --
    mostra a parte de texto. Um e-mail so em HTML chegaria em branco justamente
    para quem mais precisa ler o link.
    """
    msg = EmailMessage()
    msg["From"] = remetente
    msg["To"] = email.para
    msg["Subject"] = email.assunto
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="portfolio-tracker")
    msg.set_content(email.texto)
    if email.html is not None:
        msg.add_alternative(email.html, subtype="html")
    return msg


class EnviadorSmtp:
    """SMTP da biblioteca padrao, numa thread.

    Thread comum, e nao o pool do argon2: aqui a espera e de REDE, nao de CPU. A
    thread passa quase todo o tempo parada no socket, com o GIL solto, e nao
    disputa nucleo com ninguem -- alem de o volume ja ser limitado pelo rate
    limit das rotas que mandam e-mail.

    Biblioteca padrao, e nao um cliente async de terceiros: sao uma ou duas
    mensagens por conta, e cada dependencia a mais no lockfile e superficie a
    mais para manter.
    """

    def __init__(self, settings: Settings) -> None:
        self._host = settings.SMTP_HOST or ""
        self._porta = settings.SMTP_PORT
        self._usuario = settings.SMTP_USER
        self._senha = settings.SMTP_PASSWORD
        self._tls = settings.SMTP_TLS
        self._remetente = settings.SMTP_REMETENTE

    async def enviar(self, email: Email) -> None:
        await asyncio.to_thread(self._enviar, _mensagem(email, self._remetente))

    def _enviar(self, msg: EmailMessage) -> None:
        contexto = ssl.create_default_context()
        conexao: smtplib.SMTP
        if self._tls == "ssl":
            conexao = smtplib.SMTP_SSL(
                self._host, self._porta, timeout=TIMEOUT_SMTP_SEGUNDOS, context=contexto
            )
        else:
            conexao = smtplib.SMTP(self._host, self._porta, timeout=TIMEOUT_SMTP_SEGUNDOS)
        with conexao:
            # STARTTLS ANTES do login: autenticar em texto puro e entregar a
            # senha do SMTP a qualquer um no caminho da rede.
            if self._tls == "starttls":
                conexao.starttls(context=contexto)
            if self._usuario and self._senha is not None:
                conexao.login(self._usuario, self._senha.get_secret_value())
            conexao.send_message(msg)


class EnviadorEmArquivo:
    """Sem SMTP configurado: grava o e-mail como `.eml` numa pasta local.

    So existe fora de producao -- a configuracao se recusa a subir em producao
    sem SMTP. O `.eml` abre em qualquer cliente de e-mail, e o fluxo inteiro de
    confirmacao pode ser exercitado sem conta em provedor nenhum.

    O link NAO vai para o log, so o caminho do arquivo. Token de confirmacao em
    log e credencial em log: quem le o log confirma a conta de outra pessoa.
    """

    def __init__(self, pasta: Path, remetente: str) -> None:
        self._pasta = pasta
        self._remetente = remetente

    async def enviar(self, email: Email) -> None:
        caminho = await asyncio.to_thread(
            self._gravar, _mensagem(email, self._remetente), email.para
        )
        logger.warning(
            "[email] SMTP nao configurado: e-mail para %s gravado em %s", email.para, caminho
        )

    def _gravar(self, msg: EmailMessage, para: str) -> Path:
        self._pasta.mkdir(parents=True, exist_ok=True)
        carimbo = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        seguro = re.sub(r"[^A-Za-z0-9._-]", "_", para)
        caminho = self._pasta / f"{carimbo}-{seguro}.eml"
        caminho.write_bytes(msg.as_bytes())
        return caminho


@lru_cache
def get_enviador_de_email() -> EnviadorDeEmail:
    """SMTP quando configurado; arquivo local quando nao."""
    settings = get_settings()
    if settings.SMTP_HOST:
        return EnviadorSmtp(settings)
    return EnviadorEmArquivo(Path(settings.EMAIL_PASTA_LOCAL), settings.SMTP_REMETENTE)


async def enviar_sem_derrubar(enviador: EnviadorDeEmail, email: Email) -> None:
    """Envia em segundo plano; falha vira log, nunca erro para o usuario.

    Roda DEPOIS da resposta (BackgroundTasks). A conta ja foi criada quando isto
    executa: um SMTP fora do ar nao pode transformar um cadastro bem-sucedido em
    500 -- a pessoa pede o reenvio na tela de entrada.
    """
    try:
        await enviador.enviar(email)
    except Exception:  # noqa: BLE001
        logger.exception("[email] falha ao enviar para %s", email.para)
