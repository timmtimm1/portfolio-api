"""Cache de cotacao atual."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PriceQuote(Base):
    """Ultima cotacao conhecida de cada ativo.

    O cache vive no BANCO, nao em memoria do processo. Com dois workers, um cache
    em memoria e cacheado duas vezes, expira em momentos diferentes e dobra as
    chamadas ao fornecedor -- justamente o que se quer evitar quando a cota
    gratuita e de 15 mil requisicoes por mes. No banco, todos os workers
    compartilham a mesma entrada.

    Uma linha por ativo (`asset_id` e a chave primaria): nao guardamos historico
    intradiario aqui. O historico de fechamentos e outra tabela, com outro
    proposito -- misturar os dois faria esta crescer sem limite.
    """

    __tablename__ = "price_quotes"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)

    # Quando o dado foi obtido do fornecedor -- nao quando a linha foi gravada.
    # E o que define se o cache ainda vale, e o que a API devolve ao cliente para
    # que ele saiba a idade do numero que esta vendo.
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Qual fornecedor respondeu. Serve para depurar divergencia de preco e para
    # saber, em producao, com que frequencia o fallback esta sendo acionado.
    source: Mapped[str] = mapped_column(String(20), nullable=False)

    def __repr__(self) -> str:
        return f"<PriceQuote {self.asset_id} {self.price} ({self.source})>"
