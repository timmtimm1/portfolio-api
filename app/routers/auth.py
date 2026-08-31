"""Rotas de autenticacao."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.security import create_token
from app.schemas.auth import TokenResponse
from app.schemas.user import UserCreate, UserRead
from app.services import auth_service
from app.services.exceptions import (
    ContaInativaError,
    CredenciaisInvalidasError,
    EmailJaCadastradoError,
)

router = APIRouter(prefix="/auth", tags=["auth"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Cria uma conta",
)
async def register(dados: UserCreate, db: DbDep) -> UserRead:
    """Cadastro com email e senha.

    Compromisso assumido conscientemente: devolver 409 revela que aquele email ja
    tem conta -- e enumeracao de usuarios. Esconder isso exigiria responder 201
    sempre e mandar a informacao real por email, o que so faz sentido com um fluxo
    de verificacao por email (fora do escopo desta v1). A mitigacao aqui e limitar
    a taxa de requisicoes nesta rota (Etapa 3). O login, esse sim, nao vaza nada.
    """
    try:
        usuario = await auth_service.criar_usuario(db, dados)
    except EmailJaCadastradoError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email ja cadastrado",
        ) from None
    return UserRead.model_validate(usuario)


@router.post("/login", response_model=TokenResponse, summary="Autentica e emite o token")
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: DbDep,
    settings: SettingsDep,
) -> TokenResponse:
    """Login via formulario OAuth2 (`username` = email).

    Usamos o formulario padrao em vez de um corpo JSON proprio por um motivo
    pratico: e o que faz o botao **Authorize** do `/docs` funcionar sozinho --
    quem abrir a documentacao consegue autenticar e testar as rotas protegidas
    sem colar token na mao.

    Email inexistente, senha errada e conta desativada devolvem a MESMA resposta.
    Qualquer diferenca -- na mensagem, no codigo ou no tempo -- transforma o login
    num oraculo de quais emails tem conta no sistema.
    """
    try:
        usuario = await auth_service.autenticar(db, form.username.strip().lower(), form.password)
    except (CredenciaisInvalidasError, ContaInativaError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou senha incorretos",
            # Exigido pelo RFC 7235 num 401; e o que sinaliza ao cliente qual
            # esquema de autenticacao usar.
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    expira_em = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    token, _ = create_token(
        subject=usuario.id,
        # noqa abaixo: a regra S106 do bandit dispara em qualquer argumento cujo
        # nome contenha "token"/"password". Aqui e o tipo do token, nao um segredo.
        token_type="access",  # noqa: S106
        expires_delta=timedelta(seconds=expira_em),
    )
    return TokenResponse(access_token=token, expires_in=expira_em)
