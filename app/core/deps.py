"""Dependencias compartilhadas entre rotas."""

from __future__ import annotations

import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import get_bcb_client, get_ibov_client, get_provedor_de_cotacoes
from app.clients.base import ProvedorDeCotacoes
from app.clients.bcb import BcbClient
from app.clients.ibov import IbovClient
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.security import decode_token
from app.models.portfolio import Portfolio
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

BcbDep = Annotated[BcbClient, Depends(get_bcb_client)]
IbovDep = Annotated[IbovClient, Depends(get_ibov_client)]


async def get_carteira(
    usuario: Annotated[User, Depends(get_current_user)],
    db: DbDep,
    portfolio_id: Annotated[
        uuid.UUID | None, Query(description="Carteira. Omitido = a carteira padrao.")
    ] = None,
) -> Portfolio:
    """Resolve a carteira do request, verificando que ela e do usuario.

    ## O novo ponto unico de autorizacao

    Com varias carteiras, o cliente passa a informar um `portfolio_id` -- e ai
    mora a falha classica: aceitar esse id sem conferir de quem ele e. Bastaria
    trocar um UUID na URL para ler a carteira alheia.

    Esta dependencia e o unico caminho pelo qual um `portfolio_id` entra no
    sistema. Ela busca por id E por dono na mesma consulta; carteira de outro
    usuario simplesmente nao existe daqui para dentro. Por isso os servicos
    adiante podem filtrar so por `portfolio_id`, sem repetir a checagem.

    404, nao 403: dizer "existe, mas nao e sua" confirmaria a existencia daquele
    id -- enumeracao de recursos alheios.
    """
    from app.services import portfolio_crud

    if portfolio_id is None:
        return await portfolio_crud.obter_padrao(db, usuario.id)

    carteira = await portfolio_crud.obter(db, usuario.id, portfolio_id)
    if carteira is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Carteira nao encontrada")
    return carteira


CarteiraAtual = Annotated[Portfolio, Depends(get_carteira)]
