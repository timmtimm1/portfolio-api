"""Token de confirmacao de e-mail."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class EmailConfirmationToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Um link de confirmacao enviado.

    Mesmo desenho do refresh token: o banco guarda o SHA-256, nunca o token. Um
    dump vazado nao confirma a conta de ninguem.
    """

    __tablename__ = "email_confirmation_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        # CASCADE: conta apagada leva os links junto. Link orfao apontando para
        # usuario inexistente nao serve para nada alem de ocupar o indice.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,  # invalidar os links anteriores filtra por user_id
    )

    # SHA-256 em hexadecimal. SHA e nao argon2 pelo mesmo motivo do refresh
    # token: 384 bits aleatorios nao tem o que adivinhar.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Preenchido quando o link e usado OU substituido por um reenvio. Marcado, e
    # nao apagado: e a linha que permite responder "este e-mail ja foi
    # confirmado" a quem clica pela segunda vez, em vez de um "link invalido"
    # que assusta sem motivo.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    def __repr__(self) -> str:
        # Sem o hash: repr de model vaza em log com frequencia surpreendente.
        usado = self.used_at is not None
        return f"<EmailConfirmationToken id={self.id} user_id={self.user_id} usado={usado}>"
