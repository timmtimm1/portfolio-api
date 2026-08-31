"""Schemas de carteira."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.portfolio import TipoCarteira


class PortfolioCreate(BaseModel):
    nome: Annotated[str, Field(min_length=1, max_length=60)]
    tipo: TipoCarteira = TipoCarteira.SIMULADA

    @field_validator("nome")
    @classmethod
    def _limpa(cls, v: str) -> str:
        """Espaço nas pontas faria "Real" e "Real " passarem pela constraint de
        unicidade como nomes distintos -- e o seletor mostraria duas entradas
        visualmente idênticas."""
        limpo = " ".join(v.split())
        if not limpo:
            raise ValueError("o nome nao pode ser vazio")
        return limpo


class PortfolioRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    nome: str
    tipo: TipoCarteira
    created_at: datetime
