"""Schemas do catalogo de ativos."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.asset import AssetType


class AssetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticker: str
    nome: str | None
    setor: str | None
    tipo: AssetType


class PricePoint(BaseModel):
    """Um fechamento. `Decimal` serializa como numero JSON preservando as casas
    decimais -- converter para float aqui reintroduziria o erro binario que o
    `Numeric` do banco existe para evitar."""

    model_config = ConfigDict(from_attributes=True)

    date: date_type
    close: Decimal
    volume: int | None


class QuoteRead(BaseModel):
    """A cotacao usada para pre-preencher o preco de uma nova operacao.

    Mostra a origem e a idade de proposito: quem esta lancando uma compra
    precisa saber que R$ 37,45 e "o Yahoo, ha 4 minutos", nao o preco exato do
    fechamento de hoje -- ele so preenche o campo, nunca o substitui em silencio.
    """

    preco: Decimal
    obtida_em: datetime
    fonte: str
