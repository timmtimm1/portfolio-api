"""Emissao, rotacao e revogacao de tokens de sessao."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import create_token, generate_refresh_token, hash_refresh_token
from app.models.refresh_token import MotivoRevogacao, RefreshToken
from app.models.user import User
from app.services.exceptions import DomainError

logger = logging.getLogger(__name__)


class RefreshTokenInvalidoError(DomainError):
    """Token desconhecido, expirado ou revogado."""


class ReusoDeTokenDetectadoError(DomainError):
    """Um token ja rotacionado foi apresentado de novo -- indicio forte de roubo."""


async def emitir_par(db: AsyncSession, usuario: User, settings: Settings) -> tuple[str, str, int]:
    """Emite (access_token, refresh_token, segundos_ate_expirar_o_access)."""
    expira_em = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    access, _ = create_token(
        subject=usuario.id,
        token_type="access",  # noqa: S106  (tipo do token, nao um segredo)
        expires_delta=timedelta(seconds=expira_em),
    )

    bruto, digest = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=usuario.id,
            token_hash=digest,
            expires_at=datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
    )
    await db.commit()
    return access, bruto, expira_em


async def rotacionar(
    db: AsyncSession, refresh_bruto: str, settings: Settings
) -> tuple[str, str, int]:
    """Troca um refresh token por um par novo, invalidando o antigo.

    Rotacao: cada refresh token vale UMA vez so. Sem isso, um token vazado serve
    pelos 30 dias inteiros e nada denuncia o vazamento.

    Deteccao de reuso -- o ponto central desta funcao. Se um token JA rotacionado
    reaparece, so ha duas explicacoes:
      a) o atacante roubou o token e o usou depois de voce; ou
      b) voce o usou depois do atacante.
    Em ambos os casos alguem tem uma copia que nao deveria. A resposta correta,
    recomendada pelo RFC 9700 (OAuth 2.0 Security BCP), e revogar TODAS as sessoes
    do usuario: derruba o atacante, ao custo de um login a mais para o dono.

    E por isso que a linha revogada e mantida em vez de deletada -- se ela sumisse,
    o token roubado seria apenas "desconhecido", indistinguivel de lixo aleatorio,
    e o roubo passaria despercebido.
    """
    digest = hash_refresh_token(refresh_bruto)
    registro = (
        await db.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    ).scalar_one_or_none()

    if registro is None:
        raise RefreshTokenInvalidoError

    if registro.revoked_at is not None:
        # Corrida (duas abas, nova tentativa apos falha de rede) ou roubo?
        # A janela de tolerancia separa os dois: uma corrida acontece em
        # milissegundos, um token roubado e usado muito depois.
        idade = (datetime.now(UTC) - registro.revoked_at).total_seconds()
        # A tolerancia vale SO para revogacao por rotacao. Aplicar a logout faria
        # o token continuar valendo dez segundos depois de sair -- e um logout que
        # nao desloga na hora nao e logout.
        rotacionado = registro.revoked_reason is MotivoRevogacao.ROTACAO
        if not rotacionado or idade > settings.REFRESH_REUSE_GRACE_SECONDS:
            await revogar_todos_do_usuario(db, registro.user_id)
            raise ReusoDeTokenDetectadoError(str(registro.user_id))
        logger.info(
            "[tokens] reapresentacao dentro da janela de tolerancia (%.1fs) "
            "para o usuario %s -- tratado como corrida, nao como roubo",
            idade,
            registro.user_id,
        )

    # `expires_at` vem do banco com timezone; comparamos sempre em UTC ciente.
    # Comparar datetime naive com aware estoura TypeError -- em producao, as 3h.
    if registro.expires_at <= datetime.now(UTC):
        raise RefreshTokenInvalidoError

    usuario = await db.get(User, registro.user_id)
    if usuario is None or not usuario.is_active:
        raise RefreshTokenInvalidoError

    registro.revoked_at = datetime.now(UTC)
    registro.revoked_reason = MotivoRevogacao.ROTACAO
    # Sem commit aqui: `emitir_par` fecha a transacao. Revogar o antigo e emitir o
    # novo precisam ser atomicos -- uma falha no meio deixaria o usuario sem
    # nenhum token valido, deslogado sem ter feito nada.
    return await emitir_par(db, usuario, settings)


async def revogar(db: AsyncSession, refresh_bruto: str) -> None:
    """Logout. Silencioso se o token nao existir.

    Nao levanta erro de proposito: logout precisa ser idempotente. Um cliente que
    tenta deslogar duas vezes, ou com um token ja expirado, deve terminar
    deslogado -- nao receber um 4xx que o deixa em duvida sobre o proprio estado.
    """
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == hash_refresh_token(refresh_bruto),
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC), revoked_reason=MotivoRevogacao.LOGOUT)
    )
    await db.commit()


async def revogar_todos_do_usuario(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Derruba todas as sessoes ativas. Usado na deteccao de reuso -- e, no
    futuro, na troca de senha (trocar a senha sem derrubar as sessoes abertas
    deixa o invasor logado exatamente quando a vitima achou que o expulsou)."""
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC), revoked_reason=MotivoRevogacao.SEGURANCA)
    )
    await db.commit()
