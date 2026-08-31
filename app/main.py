"""Ponto de entrada da aplicacao FastAPI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from starlette.types import ExceptionHandler

from app.clients import fechar_http_client
from app.core.config import Settings, get_settings
from app.core.db import dispose_engine
from app.core.middleware import SecurityHeadersMiddleware
from app.core.rate_limit import excesso_de_requisicoes, limiter
from app.routers import assets, auth, health, metrics, snapshots, transactions


class _EstaticosRevalidados(StaticFiles):
    """StaticFiles que obriga o navegador a revalidar a cada visita.

    Sem isso, o navegador serve o JS e o CSS do proprio cache sem sequer
    perguntar ao servidor -- e uma correcao publicada nao chega ao usuario, que
    continua vendo o comportamento antigo e reportando um bug que ja nao existe.
    Foi exatamente o que aconteceu durante o desenvolvimento deste frontend.

    `no-cache` NAO significa "nao guarde": significa "guarde, mas confirme antes
    de usar". O navegador manda o ETag, o servidor responde 304 quando nada
    mudou, e a resposta vazia custa alguns bytes. E o ajuste certo para arquivos
    que mudam junto com a aplicacao e nao tem hash no nome.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        resposta: Response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        resposta.headers["Cache-Control"] = "no-cache"
        return resposta


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
    # Handler proprio em vez do padrao do slowapi: o dele nao inclui
    # `Retry-After`, e sem esse cabecalho o cliente so pode chutar quanto
    # esperar -- normalmente tentando cedo demais e prolongando o bloqueio.
    app.add_exception_handler(RateLimitExceeded, cast(ExceptionHandler, excesso_de_requisicoes))

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
    app.include_router(metrics.router, prefix=settings.API_V1_PREFIX)
    app.include_router(snapshots.router, prefix=settings.API_V1_PREFIX)

    # Frontend servido pela PROPRIA API.
    #
    # Sem deploy separado, sem build, e -- o que mais importa -- sem CORS: a
    # pagina e a API compartilham a origem, entao o cookie httpOnly do refresh
    # token viaja normalmente. Um frontend em outro dominio exigiria
    # `SameSite=None`, que enfraquece justamente a protecao contra CSRF que
    # escolhemos na Etapa 3.
    #
    # `check_dir=False` evita quebrar o boot num ambiente onde a pasta nao foi
    # empacotada (um container mal montado sobe sem frontend, mas com a API viva).
    estaticos = Path(__file__).parent / "static"
    # Montado em dois caminhos.
    #
    # `/painel` e o oficial. `/app` fica como apelido porque navegadores que
    # visitaram a versao antiga guardaram os arquivos em cache SEM revalidar --
    # e um arquivo ja cacheado nao e afetado por um `Cache-Control` que so
    # passou a ser enviado depois. Um caminho novo nao tem entrada no cache de
    # ninguem, entao a busca e obrigatoriamente fresca.
    for caminho in ("/painel", "/app"):
        app.mount(
            caminho,
            _EstaticosRevalidados(directory=estaticos, html=True, check_dir=False),
            name=f"frontend{caminho.replace('/', '_')}",
        )

    @app.get("/", include_in_schema=False)
    async def raiz() -> RedirectResponse:
        return RedirectResponse(url="/painel/")

    return app


app = create_app()
