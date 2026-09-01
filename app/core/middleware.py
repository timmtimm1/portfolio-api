"""Middlewares proprios."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core import metrics
from app.core.config import get_settings
from app.core.logging import id_da_requisicao

logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Cabecalhos de seguranca em toda resposta.

    Sao instrucoes ao navegador. Nao protegem a API contra `curl` -- protegem o
    usuario que abre o frontend (Etapa 11) contra ataques que acontecem DENTRO do
    navegador dele.

    Cada um fecha uma porta especifica:

    X-Content-Type-Options: nosniff
        Impede o navegador de "adivinhar" o tipo do conteudo. Sem isso, um arquivo
        que o usuario suba e que o navegador resolva interpretar como HTML executa
        script na sua origem.

    X-Frame-Options: DENY
        Impede que a pagina seja embutida num iframe em outro site. Bloqueia
        clickjacking -- o site do atacante empilha um botao invisivel sobre o seu
        e o usuario clica em "vender tudo" achando que clicou noutra coisa.

    Referrer-Policy: no-referrer
        Nao vaza a URL da sua pagina para sites externos. Uma URL pode conter id
        de carteira, filtro, token em query string.

    Content-Security-Policy
        A defesa mais forte contra XSS: declara de onde script pode vir. Mesmo
        que um script estranho seja injetado na pagina, o navegador se recusa a
        executa-lo.

    Strict-Transport-Security
        Obriga HTTPS nos acessos seguintes. So em producao: em localhost isso
        travaria o navegador em https:// e o desenvolvimento pararia.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        settings = get_settings()

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            # 'unsafe-inline' em style e um compromisso para o Swagger funcionar.
            # Em script NAO abrimos excecao -- e justamente ai que o XSS mora.
            "script-src 'self' https://cdn.jsdelivr.net; "
            "frame-ancestors 'none'; "
            "base-uri 'self'"
        )
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


class ObservabilidadeMiddleware(BaseHTTPMiddleware):
    """Da a cada requisicao um ID, um tempo medido e uma linha de log.

    ## O ID vem de fora, quando vem

    Se o cliente (ou um proxy na frente) mandou `X-Request-ID`, ele e
    reaproveitado. Isso e o que permite seguir um pedido atravessando varios
    servicos: gerar um novo aqui quebraria a corrente exatamente onde ela
    importa.

    O ID volta no cabecalho da resposta de proposito. Quando alguem reclama de
    um erro, "me manda o X-Request-ID" transforma uma investigacao em uma
    consulta.

    ## Por que medir aqui

    Este middleware envolve TODOS os outros, entao o tempo registrado e o que o
    usuario sentiu -- incluindo serializacao, CORS e cabecalhos de seguranca.
    Medir dentro da rota daria um numero menor e mais bonito, e menos util.

    ## Erro tambem e resposta

    O `try/finally` garante a linha de log e a metrica mesmo quando a rota
    levanta. Registrar so o caminho feliz produz um painel que fica verde
    justamente durante o incidente.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlacao = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = id_da_requisicao.set(correlacao)
        inicio = time.perf_counter()
        status = 500  # se a rota levantar, foi 500 -- assumimos o pior ate saber

        try:
            resposta = await call_next(request)
            status = resposta.status_code
            resposta.headers["X-Request-ID"] = correlacao
            return resposta
        finally:
            duracao = time.perf_counter() - inicio
            # A ROTA, nao a URL: `/transactions/{id}` e uma serie temporal;
            # `/transactions/9f2c...` seriam milhares de series de um ponto
            # cada -- o erro de cardinalidade que derruba um Prometheus.
            rota = request.scope.get("route")
            caminho = getattr(rota, "path", None) or "desconhecida"

            metrics.registrar(request.method, caminho, status, duracao)
            logger.info(
                "requisicao",
                extra={
                    "metodo": request.method,
                    "rota": caminho,
                    "status": status,
                    "duracao_ms": round(duracao * 1000, 2),
                },
            )
            id_da_requisicao.reset(token)
