"""Carteiras: a real e as simuladas."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, coluna_enum


class TipoCarteira(enum.StrEnum):
    """O tipo NAO muda o calculo -- muda o significado.

    Uma carteira simulada usa exatamente a mesma matematica: mesmo preco medio,
    mesma cotacao, mesma fronteira eficiente. A distincao existe para que a
    interface nunca confunda "o que eu tenho" com "o que eu estou avaliando", e
    para que um total somado por engano entre as duas seja impossivel.
    """

    REAL = "real"
    SIMULADA = "simulada"


class Portfolio(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "portfolios"
    __table_args__ = (
        # Dois nomes iguais para o mesmo usuario tornariam o seletor ambiguo.
        # Usuarios diferentes podem ter carteiras homonimas, entao a unicidade e
        # por (usuario, nome) -- nao global.
        UniqueConstraint("user_id", "nome", name="uq_portfolios_user_id_nome"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nome: Mapped[str] = mapped_column(String(60), nullable=False)
    tipo: Mapped[TipoCarteira] = mapped_column(
        coluna_enum(TipoCarteira, length=10), default=TipoCarteira.SIMULADA, nullable=False
    )

    def __repr__(self) -> str:
        return f"<Portfolio {self.nome} ({self.tipo})>"
