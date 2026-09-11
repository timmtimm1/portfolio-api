"""Schemas de token."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, field_validator


class TokenResponse(BaseModel):
    """Formato de resposta do OAuth2 (RFC 6749), que e o que o botao Authorize do
    Swagger e a maioria dos clientes HTTP ja sabem consumir."""

    access_token: str
    # S105 e falso positivo: "bearer" e o esquema do RFC 6750, nao um segredo.
    token_type: str = "bearer"  # noqa: S105
    expires_in: int  # segundos ate expirar -- o cliente nao deveria ter que
    # decodificar o JWT para saber quando renovar


class ConfirmarEmail(BaseModel):
    # Teto de tamanho ANTES do hash: sem ele, um corpo de megabytes seria
    # hasheado so para descobrir que nao e token nenhum.
    token: Annotated[str, Field(min_length=20, max_length=128)]


class ReenviarConfirmacao(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normaliza(cls, v: str) -> str:
        """Mesma normalizacao do cadastro. Sem ela, "Bernardo@x.com" nunca acharia
        a conta gravada como "bernardo@x.com", e o reenvio falharia em silencio."""
        return v.strip().lower()


class EmailConfirmado(BaseModel):
    email: EmailStr


class Mensagem(BaseModel):
    mensagem: str
