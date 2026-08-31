"""Rotas de autenticacao."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.core.config import get_settings
from app.core.deps import CurrentUser, DbDep, SettingsDep
from app.core.rate_limit import limiter
from app.schemas.auth import TokenResponse
from app.schemas.user import UserCreate, UserRead
from app.services import auth_service, token_service
from app.services.exceptions import (
    ContaInativaError,
    CredenciaisInvalidasError,
    EmailJaCadastradoError,
)
from app.services.token_service import (
    RefreshTokenInvalidoError,
    ReusoDeTokenDetectadoError,
)

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"  # noqa: S105  (nome do cookie, nao um segredo)

_CREDENCIAIS_INVALIDAS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Email ou senha incorretos",
    headers={"WWW-Authenticate": "Bearer"},
)
_SESSAO_INVALIDA = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Sessao invalida ou expirada",
    headers={"WWW-Authenticate": "Bearer"},
)


def _set_refresh_cookie(response: Response, token: str, settings: SettingsDep) -> None:
    """Entrega o refresh token num cookie, nao no corpo da resposta.

    Motivo: o corpo obriga o cliente a guardar o token em algum lugar do
    JavaScript -- e `localStorage` e legivel por qualquer script que rode na
    pagina. Uma unica falha de XSS (uma dependencia comprometida, um campo
    renderizado sem escape) entrega uma sessao de 30 dias.

    `httponly=True` torna o cookie invisivel para o JavaScript: o navegador o
    envia, o script nao o le. XSS deixa de virar roubo de sessao permanente.

    As outras tres flags fecham o resto:
      secure   -- so trafega em HTTPS (desligado em local, senao nao funciona
                  em http://127.0.0.1)
      samesite -- "strict": o navegador nao envia o cookie em request originado
                  de outro site. E o que barra CSRF nesta rota sem precisar de
                  token anti-CSRF separado.
      path     -- escopo restrito a /auth: o cookie nao acompanha os requests de
                  carteira, cotacao etc. Menos exposicao, menos bytes.
    """
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        path=f"{settings.API_V1_PREFIX}/auth",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
    )


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Cria uma conta",
)
@limiter.limit(get_settings().RATE_LIMIT_REGISTER)
async def register(request: Request, dados: UserCreate, db: DbDep) -> UserRead:
    """Cadastro com email e senha.

    Compromisso assumido conscientemente: devolver 409 revela que aquele email ja
    tem conta -- e enumeracao de usuarios. Esconder isso exigiria responder 201
    sempre e mandar a informacao real por email, o que so faz sentido com um fluxo
    de verificacao por email (fora do escopo desta v1). A mitigacao aqui e o rate
    limit aplicado a esta rota. O login, esse sim, nao vaza nada.
    """
    try:
        usuario = await auth_service.criar_usuario(db, dados)
    except EmailJaCadastradoError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email ja cadastrado"
        ) from None
    return UserRead.model_validate(usuario)


@router.post("/login", response_model=TokenResponse, summary="Autentica e emite os tokens")
@limiter.limit(get_settings().RATE_LIMIT_LOGIN)
async def login(
    request: Request,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> TokenResponse:
    """Login via formulario OAuth2 (`username` = email).

    Devolve o access token no corpo (o cliente guarda em memoria, nao em disco) e
    o refresh token num cookie httpOnly.

    Email inexistente, senha errada e conta desativada produzem a MESMA resposta,
    no MESMO tempo. Qualquer diferenca transforma o login num oraculo de quais
    emails tem conta.
    """
    try:
        usuario = await auth_service.autenticar(db, form.username.strip().lower(), form.password)
    except (CredenciaisInvalidasError, ContaInativaError):
        raise _CREDENCIAIS_INVALIDAS from None

    access, refresh, expira_em = await token_service.emitir_par(db, usuario, settings)
    _set_refresh_cookie(response, refresh, settings)
    return TokenResponse(access_token=access, expires_in=expira_em)


@router.post("/refresh", response_model=TokenResponse, summary="Renova o access token")
@limiter.limit(get_settings().RATE_LIMIT_LOGIN)
async def refresh(
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> TokenResponse:
    """Troca o refresh token por um par novo (rotacao).

    Reapresentar um token ja rotacionado derruba TODAS as sessoes do usuario --
    ver `token_service.rotacionar`. A resposta e a mesma de um token invalido
    qualquer: dizer "reuso detectado" avisaria o atacante de que ele foi visto.
    """
    if refresh_token is None:
        raise _SESSAO_INVALIDA
    try:
        access, novo_refresh, expira_em = await token_service.rotacionar(
            db, refresh_token, settings
        )
    except (RefreshTokenInvalidoError, ReusoDeTokenDetectadoError):
        response.delete_cookie(REFRESH_COOKIE, path=f"{settings.API_V1_PREFIX}/auth")
        raise _SESSAO_INVALIDA from None

    _set_refresh_cookie(response, novo_refresh, settings)
    return TokenResponse(access_token=access, expires_in=expira_em)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Encerra a sessao")
async def logout(
    response: Response,
    db: DbDep,
    settings: SettingsDep,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> None:
    """Revoga o refresh token no servidor e apaga o cookie.

    Nao basta apagar o cookie: o token continuaria valido no banco e uma copia
    (feita por um proxy, por um script, por quem tirou o notebook da mesa)
    seguiria funcionando. Logout de verdade e revogacao no servidor -- o cookie
    e so a limpeza do lado do cliente.

    O access token ainda vale ate expirar (no maximo 15 minutos). Esse e o preco
    do JWT sem consulta a banco; a mitigacao e a vida util curta. Revogar o
    access token na hora exigiria uma lista de bloqueio consultada a cada request,
    que e exatamente o custo que o JWT existe para evitar.
    """
    if refresh_token is not None:
        await token_service.revogar(db, refresh_token)
    response.delete_cookie(REFRESH_COOKIE, path=f"{settings.API_V1_PREFIX}/auth")


@router.get("/me", response_model=UserRead, summary="Dados do usuario autenticado")
async def me(usuario: CurrentUser) -> UserRead:
    """Rota protegida de referencia.

    Repare que ela nao recebe nenhum identificador. A identidade vem do token,
    resolvida por `get_current_user`. Uma rota `/users/{id}` que devolvesse os
    dados sem conferir se `id` e o do requisitante seria a falha de autorizacao
    classica -- aqui ela e impossivel por construcao.
    """
    return UserRead.model_validate(usuario)
