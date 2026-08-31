"""Dependencias compartilhadas entre rotas."""

from __future__ import annotations

import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import get_provedor_de_cotacoes
from app.clients.base import ProvedorDeCotacoes
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.security import decode_token
from app.models.user import User

DbDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# `tokenUrl` nao muda o comportamento do servidor -- e so o que o Swagger le para
# saber onde fazer login quando voce clica em Authorize.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

_NAO_AUTORIZADO = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    # Uma unica mensagem para todos os motivos: token expirado, assinatura
    # invalida, tipo errado, usuario apagado, conta desativada. Distinguir seria
    # dizer ao atacante exatamente o que corrigir na proxima tentativa.
    detail="Nao autenticado",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: DbDep,
) -> User:
    """Resolve o usuario do access token, ou responde 401.

    Esta funcao e o unico lugar do sistema que transforma um token em identidade.
    Toda rota protegida depende dela -- e nenhuma rota deve aceitar um user_id
    vindo do corpo ou da query string. Confiar num `user_id` enviado pelo cliente
    e literalmente deixar o usuario escolher de quem ele quer ser: e a falha de
    autorizacao mais comum que existe, e a razao de a carteira alheia vazar.

    O usuario e relido do banco a cada request, nao reconstruido a partir das
    claims. Custa um SELECT por chave primaria (indexado, barato) e garante que
    desativar uma conta tenha efeito imediato -- se confiassemos so no token, a
    conta banida continuaria acessando ate o token expirar.
    """
    try:
        payload = decode_token(token, "access")
        user_id = uuid.UUID(payload["sub"])
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise _NAO_AUTORIZADO from None

    usuario = await db.get(User, user_id)
    if usuario is None or not usuario.is_active:
        raise _NAO_AUTORIZADO

    return usuario


# Alias usado nas assinaturas das rotas. Declarar `usuario: CurrentUser` e o que
# torna uma rota protegida -- e o que torna obvio, na leitura, quais nao sao.
CurrentUser = Annotated[User, Depends(get_current_user)]


# Injetado como dependencia, nao importado direto na rota: e o que permite ao
# teste substituir o fornecedor por um duble e rodar sem tocar a rede. Suite que
# depende de API externa e suite que falha quando a internet oscila.
ProvedorDep = Annotated[ProvedorDeCotacoes, Depends(get_provedor_de_cotacoes)]
