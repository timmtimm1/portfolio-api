"""Testes do frontend servido pela propria API."""

from __future__ import annotations

import re
from pathlib import Path

from httpx import AsyncClient

ESTATICOS = Path(__file__).parent.parent / "app" / "static"


def _sem_comentarios(js: str) -> str:
    """Remove comentarios antes de procurar padroes proibidos.

    A primeira versao destes testes falhou pela razao mais irônica possivel: os
    comentarios do proprio app.js explicam POR QUE nao usamos `localStorage` nem
    `innerHTML` -- e o teste encontrava as palavras ali. Verificacao de codigo
    tem que olhar codigo; prosa sobre o codigo nao e codigo.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    # `(?<!:)` preserva "https://": sem isso, o stripper cortaria a URL do CDN
    # ao meio e o teste passaria a examinar um arquivo mutilado.
    return re.sub(r"(?<!:)//.*$", "", js, flags=re.MULTILINE)


class TestServico:
    async def test_raiz_redireciona_para_o_app(self, client: AsyncClient) -> None:
        resp = await client.get("http://test/", follow_redirects=False)
        assert resp.status_code in (307, 308)
        assert resp.headers["location"] == "/app/"

    async def test_pagina_e_servida(self, client: AsyncClient) -> None:
        resp = await client.get("http://test/app/")
        assert resp.status_code == 200
        assert "Portfolio Tracker" in resp.text

    async def test_css_e_js_sao_servidos(self, client: AsyncClient) -> None:
        assert (await client.get("http://test/app/style.css")).status_code == 200
        assert (await client.get("http://test/app/app.js")).status_code == 200

    async def test_cabecalhos_de_seguranca_valem_para_o_frontend(self, client: AsyncClient) -> None:
        """O middleware cobre os estaticos tambem -- e no frontend que CSP e
        X-Frame-Options de fato protegem o usuario."""
        h = (await client.get("http://test/app/")).headers
        assert h["x-frame-options"] == "DENY"
        assert "default-src 'self'" in h["content-security-policy"]


class TestCompatibilidadeComACSP:
    """A CSP proibe script inline. Se o HTML tiver um `<script>` sem `src` ou um
    `onclick=`, a pagina carrega mas nao FUNCIONA -- e o navegador reclama no
    console, onde ninguem olha. Estes testes pegam isso no CI.
    """

    def test_sem_script_inline(self) -> None:
        html = (ESTATICOS / "index.html").read_text()
        assert not re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html)

    def test_sem_handlers_inline(self) -> None:
        html = (ESTATICOS / "index.html").read_text()
        assert not re.findall(r"\son(click|load|error|submit|change)\s*=", html)

    def test_scripts_externos_vem_de_origem_permitida(self) -> None:
        """A CSP libera 'self' e cdn.jsdelivr.net. Qualquer outro host carrega
        sem erro visivel e simplesmente nao executa."""
        html = (ESTATICOS / "index.html").read_text()
        for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', html):
            assert src.startswith("/") or src.startswith("https://cdn.jsdelivr.net/"), src

    def test_sem_folha_de_estilo_externa(self) -> None:
        """A CSP nao libera fonts.googleapis: uma fonte externa falharia em
        silencio e a pagina cairia na fonte de sistema sem aviso."""
        html = (ESTATICOS / "index.html").read_text()
        for href in re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', html):
            assert href.startswith("/"), href


class TestPosturaDoCliente:
    """Verificacoes sobre o codigo do cliente que valem manter no CI."""

    def test_token_nunca_vai_para_localstorage(self) -> None:
        """localStorage e legivel por qualquer script da pagina: um XSS ou uma
        dependencia comprometida levaria a sessao. O access token fica em
        variavel, e a sessao e retomada pelo cookie httpOnly."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "localStorage" not in js
        assert "sessionStorage" not in js

    def test_nao_monta_html_com_dado_da_api(self) -> None:
        """Concatenar HTML com dado e como o XSS entra, mesmo quando o dado 'e do
        proprio banco' -- ele foi digitado por alguem."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "innerHTML" not in js
        assert "outerHTML" not in js
        assert "document.write" not in js
