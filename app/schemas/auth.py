"""Schemas de token."""

from __future__ import annotations

from pydantic import BaseModel


class TokenResponse(BaseModel):
    """Formato de resposta do OAuth2 (RFC 6749), que e o que o botao Authorize do
    Swagger e a maioria dos clientes HTTP ja sabem consumir."""

    access_token: str
    # S105 e falso positivo: "bearer" e o esquema do RFC 6750, nao um segredo.
    token_type: str = "bearer"  # noqa: S105
    expires_in: int  # segundos ate expirar -- o cliente nao deveria ter que
    # decodificar o JWT para saber quando renovar
