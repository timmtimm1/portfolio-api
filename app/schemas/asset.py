"""Schemas do catalogo de ativos."""

from __future__ import annotations

import uuid
from datetime import date as date_type
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
