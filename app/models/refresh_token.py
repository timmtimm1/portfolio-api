"""Refresh token persistido.

Por que o refresh token NAO e um JWT, sendo que o access token e:

O valor do JWT e poder validar sem consultar nada -- assinatura confere, token
vale. Isso e otimo para um token de 15 minutos: se vazar, o estrago tem prazo.

Mas um refresh token vive 30 dias e PRECISA ser revogavel -- no logout, na troca
de senha, na deteccao de roubo. Revogar exige consultar um registro. Ou seja: o
unico beneficio do JWT (nao consultar nada) e exatamente o que nao podemos ter
aqui. Sobraria so o custo: um token maior e um segundo formato parecido com o
primeiro, convidando a confundir os dois.

Entao o refresh token e um valor aleatorio opaco de 256 bits, e a fonte da
verdade e esta tabela.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MotivoRevogacao(enum.StrEnum):
    """Por que o token foi revogado.

    Nao e metadado decorativo: e o que decide se uma reapresentacao merece a
    janela de tolerancia.

    ROTACAO  -- o proprio usuario renovou. Reapresentar logo em seguida quase
                sempre e corrida entre abas, nao roubo. Ganha tolerancia.
    LOGOUT   -- o usuario saiu. Nenhuma tolerancia: se o token voltasse a
                funcionar por dez segundos depois do logout, o logout seria
                mentira. Foi exatamente isso que um teste pegou quando a janela
                de tolerancia foi aplicada a todos os casos.
    SEGURANCA -- derrubado por deteccao de reuso. Tolerar aqui anularia a
                defesa que acabou de disparar.
    """

    ROTACAO = "rotacao"
    LOGOUT = "logout"
    SEGURANCA = "seguranca"


class RefreshToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        # ondelete="CASCADE": se a conta for apagada de fato, os tokens vao junto.
        # Deixar token orfao apontando para usuario inexistente e como se descobre,
        # tarde, que "a conta foi excluida mas a sessao continuou valida".
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,  # toda revogacao em massa filtra por user_id
    )

    # SHA-256 do token, em hexadecimal -- NUNCA o token em si.
    #
    # Mesma logica da senha: um dump do banco nao pode conter credencial usavel.
    # Mas aqui usamos SHA-256, nao argon2, e a diferenca e proposital: argon2 e
    # caro para compensar a baixa entropia de uma senha humana. Um token de 256
    # bits aleatorios nao tem o que adivinhar -- forca bruta e inviavel por
    # construcao. Pagar 200ms de argon2 a cada refresh seria custo puro, sem
    # ganho de seguranca.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Nulo enquanto valido. Preenchido no logout, na rotacao e na revogacao em
    # massa. Marcamos em vez de deletar de proposito: e a linha revogada que
    # permite detectar reuso -- se o registro sumisse, um token roubado seria
    # apenas "desconhecido", indistinguivel de lixo, e o roubo passaria batido.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_reason: Mapped[MotivoRevogacao | None] = mapped_column(
        Enum(MotivoRevogacao, native_enum=False, length=12, validate_strings=True),
        default=None,
    )

    def __repr__(self) -> str:
        # Sem o hash: repr de model vaza em log com frequencia surpreendente.
        revogado = self.revoked_at is not None
        return f"<RefreshToken id={self.id} user_id={self.user_id} revogado={revogado}>"
