"""Ponto de entrada da aplicacao FastAPI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.core.config import Settings, get_settings
from app.routers import health


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Ciclo de vida: o que abre no boot e fecha no shutdown.

    Por enquanto so valida a configuracao. A partir da Etapa 1 e aqui que o pool
    de conexoes do banco e descartado no shutdown.
    """
    get_settings()  # falha ja no boot se faltar segredo, nao no primeiro request
    yield


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
    return app


app = create_app()
