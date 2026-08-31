"""Ponto de entrada da aplicacao FastAPI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ExceptionHandler

from app.clients import fechar_http_client
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine
from app.core.middleware import SecurityHeadersMiddleware
from app.core.rate_limit import limiter
from app.routers import assets, auth, health, transactions


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Ciclo de vida: o que abre no boot e fecha no shutdown.

    Valida a configuracao no boot (falha cedo, nao no primeiro request) e fecha o
    pool de conexoes no shutdown -- sem isso o processo pode encerrar deixando
    conexoes penduradas no Postgres ate o timeout do servidor.
    """
    get_settings()
    yield
    await dispose_engine()
    await fechar_http_client()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory.

    Nao criamos o `app` como variavel de modulo direto porque isso amarra a
    aplicacao a uma unica configuracao no momento do import. Com a factory, o
    teste monta uma instancia isolada com outra config -- e e o que permite a
    suite rodar contra um Postgres efemero sem contaminar nada.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    )

    # Rate limiting: o limiter fica no state porque os decoradores das rotas
    # o buscam ali. O handler traduz o estouro em 429 com Retry-After.
    app.state.limiter = limiter
    # O handler do slowapi e tipado para RateLimitExceeded, mas o Starlette
    # declara o parametro como Exception. O cast documenta que a incompatibilidade
    # e so de assinatura -- o Starlette so chama este handler para essa excecao.
    app.add_exception_handler(
        RateLimitExceeded, cast(ExceptionHandler, _rate_limit_exceeded_handler)
    )

    app.add_middleware(SecurityHeadersMiddleware)

    # CORS so entra se houver origem configurada. `allow_credentials=True` com
    # `allow_origins=["*"]` e proibido pelo proprio navegador e e o erro classico:
    # a lista aqui e sempre explicita, vinda do ambiente.
    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    app.include_router(health.router, prefix=settings.API_V1_PREFIX)
    app.include_router(auth.router, prefix=settings.API_V1_PREFIX)
    app.include_router(assets.router, prefix=settings.API_V1_PREFIX)
    app.include_router(transactions.router, prefix=settings.API_V1_PREFIX)
    return app


app = create_app()
