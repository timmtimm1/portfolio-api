"""Middlewares proprios."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings


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
