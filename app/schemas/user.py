"""Schemas de entrada e saida de usuario.

A separacao entre model (ORM) e schema (Pydantic) nao e burocracia: e o que
impede que uma coluna nova no banco vaze automaticamente na resposta da API.
Se `UserRead` fosse o proprio model, adicionar `hashed_password` -- ou depois um
campo `cpf` -- exporia o dado sem ninguem decidir isso.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# Minimo de 12: a recomendacao atual (NIST SP 800-63B) e priorizar comprimento
# sobre regras de "1 maiuscula, 1 simbolo", que so produzem "Senha@123".
SENHA_MIN = 12
# Maximo de 128: sem teto, um POST com uma senha de 10 MB faz o argon2 consumir
# CPU e memoria por segundos. Repetido algumas vezes, e negacao de servico de
# graca. O limite tem que existir *antes* da funcao de hash.
SENHA_MAX = 128


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=SENHA_MIN, max_length=SENHA_MAX)

    @field_validator("email")
    @classmethod
    def _normaliza_email(cls, v: str) -> str:
        """Sem isto, "Bernardo@x.com" e "bernardo@x.com" criam duas contas e a
        constraint de unicidade vira ficcao. Normalizar na entrada e mais barato
        e mais previsivel que um indice funcional em lower(email)."""
        return v.strip().lower()

    @model_validator(mode="after")
    def _senha_nao_contem_email(self) -> UserCreate:
        """Bloqueia o padrao mais previsivel que existe: a senha ser derivada do
        proprio email (bernardo@x.com -> "bernardo2024"). E a primeira coisa que
        um ataque de dicionario direcionado tenta."""
        local = self.email.split("@", 1)[0]
        if len(local) >= 4 and local in self.password.lower():
            raise ValueError("a senha nao pode conter o seu email")
        return self


class UserRead(BaseModel):
    """Resposta publica. Note o que NAO esta aqui: hashed_password."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    is_active: bool
    created_at: datetime
