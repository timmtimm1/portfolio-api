"""Confirmacao de e-mail: emitir o link, confirmar e reenviar.

Camada que persiste. O envio em si fica em `app/clients/correio.py`, e a rota e
quem junta os dois -- assim este modulo nao sabe se o e-mail sai por SMTP, por
arquivo ou para a caixa em memoria dos testes.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.correio import Email
from app.core.config import Settings
from app.core.security import generate_confirmation_token, hash_confirmation_token
from app.models.email_confirmation import EmailConfirmationToken
from app.models.user import User
from app.services.exceptions import DomainError


class LinkDeConfirmacaoInvalidoError(DomainError):
    """Token desconhecido, expirado, ou ja substituido numa conta nao confirmada.

    Uma excecao so para os tres casos, de proposito: a tela diz a mesma coisa em
    todos -- "peca um link novo" --, e separar "expirado" de "nunca existiu" so
    ajudaria quem esta chutando tokens.
    """


async def emitir(db: AsyncSession, usuario: User, settings: Settings) -> str:
    """Gera um link novo e aposenta os anteriores ainda nao usados.

    Aposentar importa: quem pede reenvio porque nao achou o primeiro e-mail pode
    acha-lo depois, e dois links validos para a mesma conta sao dois alvos em vez
    de um. Devolve o token em texto puro -- a unica vez que ele existe fora do
    e-mail.
    """
    agora = datetime.now(UTC)
    await db.execute(
        update(EmailConfirmationToken)
        .where(
            EmailConfirmationToken.user_id == usuario.id,
            EmailConfirmationToken.used_at.is_(None),
        )
        .values(used_at=agora)
    )
    token, token_hash = generate_confirmation_token()
    db.add(
        EmailConfirmationToken(
            user_id=usuario.id,
            token_hash=token_hash,
            expires_at=agora + timedelta(hours=settings.EMAIL_CONFIRMACAO_VALIDADE_HORAS),
        )
    )
    await db.commit()
    return token


def link_de_confirmacao(token: str, settings: Settings) -> str:
    """O token vai no FRAGMENTO (`#`), nao na query (`?`).

    O navegador nunca manda o fragmento ao servidor. Com `?confirmar=`, o token
    apareceria no log de acesso do uvicorn, no do proxy e no do provedor de
    hospedagem -- tres lugares onde uma credencial nao deveria estar. Com `#`, so
    o JavaScript da pagina o ve, e ele o apaga da barra de endereco na hora.
    """
    return f"{settings.APP_URL.rstrip('/')}/painel/#confirmar={token}"


def email_de_confirmacao(destinatario: str, token: str, settings: Settings) -> Email:
    link = link_de_confirmacao(token, settings)
    horas = settings.EMAIL_CONFIRMACAO_VALIDADE_HORAS
    texto = (
        "Olá,\n\n"
        "Para ativar sua conta no Portfolio Tracker, confirme este e-mail abrindo o link:\n\n"
        f"{link}\n\n"
        f"O link vale por {horas} horas e funciona uma vez só.\n\n"
        "Se você não criou esta conta, ignore esta mensagem: nada acontece sem a confirmação.\n"
    )
    seguro = html.escape(link, quote=True)
    discreto = 'style="color:#5b5870;font-size:13px"'
    botao = (
        'style="display:inline-block;padding:10px 18px;border-radius:8px;'
        'background:#7b5cff;color:#fff;text-decoration:none;font-weight:600"'
    )
    corpo_html = (
        '<div style="font-family:system-ui,sans-serif;font-size:15px;color:#1d1b26">'
        "<p>Olá,</p>"
        "<p>Para ativar sua conta no <strong>Portfolio Tracker</strong>, "
        "confirme este e-mail:</p>"
        f'<p><a href="{seguro}" {botao}>Confirmar e-mail</a></p>'
        f"<p {discreto}>Ou copie o endereço: {seguro}</p>"
        f"<p {discreto}>O link vale por {horas} horas e funciona uma vez só. "
        "Se você não criou esta conta, ignore esta mensagem.</p>"
        "</div>"
    )
    return Email(
        para=destinatario,
        assunto="Confirme seu e-mail no Portfolio Tracker",
        texto=texto,
        html=corpo_html,
    )


async def confirmar(db: AsyncSession, token_bruto: str) -> User:
    """Consome o link e marca a conta como confirmada. Devolve o usuario.

    Clicar de novo num link ja usado, com a conta ja confirmada, NAO e erro: e a
    pessoa abrindo o mesmo e-mail duas vezes. Responder "link invalido" ali
    assustaria sem motivo -- e nao protege nada, porque quem tem o link tem o
    e-mail.
    """
    registro = (
        await db.execute(
            select(EmailConfirmationToken).where(
                EmailConfirmationToken.token_hash == hash_confirmation_token(token_bruto)
            )
        )
    ).scalar_one_or_none()
    if registro is None:
        raise LinkDeConfirmacaoInvalidoError

    usuario = await db.get(User, registro.user_id)
    if usuario is None:
        raise LinkDeConfirmacaoInvalidoError
    if usuario.email_confirmado:
        return usuario

    agora = datetime.now(UTC)
    if registro.used_at is not None or registro.expires_at <= agora:
        raise LinkDeConfirmacaoInvalidoError

    registro.used_at = agora
    usuario.email_confirmado_em = agora
    await db.commit()
    return usuario


async def reenviar(db: AsyncSession, email: str, settings: Settings) -> str | None:
    """Emite um link novo se houver conta esperando confirmacao; senao, nada.

    Devolve o token, para a rota montar o e-mail, ou None. A ROTA responde a
    mesma coisa nos dois casos: dizer "este e-mail nao tem conta" transformaria o
    reenvio num oraculo de quem esta cadastrado. Demo e conta desativada ficam de
    fora -- nenhuma das duas entra por login.
    """
    usuario = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if usuario is None or usuario.email_confirmado or usuario.is_demo or not usuario.is_active:
        return None
    return await emitir(db, usuario, settings)
