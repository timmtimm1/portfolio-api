"""Testes do frontend servido pela propria API."""

from __future__ import annotations

import re
from pathlib import Path

from httpx import AsyncClient

ESTATICOS = Path(__file__).parent.parent / "app" / "static"


def _mesma_origem(url: str) -> bool:
    """Caminho relativo ou absoluto no proprio host.

    Os links viraram RELATIVOS ("app.js" em vez de "/app/app.js") para a pagina
    funcionar tanto em /painel/ quanto em /app/. Relativo tambem e mesma origem,
    entao a intencao do teste -- nenhuma origem externa nao autorizada -- segue
    valendo; so a forma de escrever o caminho mudou.
    """
    return not url.startswith(("http://", "https://", "//"))


def _mesma_origem_ou_cdn(url: str) -> bool:
    return _mesma_origem(url) or url.startswith("https://cdn.jsdelivr.net/")


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
    async def test_raiz_redireciona_para_o_painel(self, client: AsyncClient) -> None:
        resp = await client.get("http://test/", follow_redirects=False)
        assert resp.status_code in (307, 308)
        assert resp.headers["location"] == "/painel/"

    async def test_pagina_e_servida_nos_dois_caminhos(self, client: AsyncClient) -> None:
        """`/app` continua valendo como apelido.

        O caminho mudou para `/painel` porque navegadores que visitaram a versao
        antiga guardaram os arquivos em cache sem revalidar -- e um arquivo ja
        cacheado nao e afetado por um Cache-Control que so passou a ser enviado
        depois. Quebrar o endereco antigo, porem, seria trocar um problema por
        outro.
        """
        for base in ("/painel/", "/app/"):
            resp = await client.get(f"http://test{base}")
            assert resp.status_code == 200, base
            assert "Portfolio Tracker" in resp.text

    async def test_css_e_js_sao_servidos(self, client: AsyncClient) -> None:
        for base in ("/painel", "/app"):
            assert (await client.get(f"http://test{base}/style.css")).status_code == 200
            assert (await client.get(f"http://test{base}/app.js")).status_code == 200

    async def test_estaticos_pedem_revalidacao(self, client: AsyncClient) -> None:
        """`no-cache` nao e "nao guarde", e sim "guarde, mas confirme antes de
        usar". Sem isso, uma correcao publicada nao chega ao usuario -- ele
        continua vendo o comportamento antigo e reportando um bug que ja nao
        existe. Aconteceu de verdade durante o desenvolvimento."""
        for caminho in ("/painel/app.js", "/painel/style.css"):
            resp = await client.get(f"http://test{caminho}")
            assert resp.headers.get("cache-control") == "no-cache", caminho

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
            assert _mesma_origem_ou_cdn(src), src

    def test_sem_folha_de_estilo_externa(self) -> None:
        """A CSP nao libera fonts.googleapis: uma fonte externa falharia em
        silencio e a pagina cairia na fonte de sistema sem aviso."""
        html = (ESTATICOS / "index.html").read_text()
        for href in re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', html):
            assert _mesma_origem(href), href


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


class TestAtributoHidden:
    """Regressão de um bug que quebrou o login inteiro.

    O JavaScript alterna telas com `el.hidden = true/false`. Mas `hidden` só
    aplica `display: none` pela folha de estilo do NAVEGADOR, e qualquer regra
    do autor com `display` vence. Como `.login-tela`, `.shell` e `.form-op`
    declaram `display: grid`, o `hidden` não surtia efeito nenhum: o login
    funcionava (o servidor respondia 200 e os dados carregavam), mas a tela de
    login continuava na frente -- e parecia que entrar não funcionava.
    """

    def test_css_restabelece_o_contrato_do_hidden(self) -> None:
        css = (ESTATICOS / "style.css").read_text()
        assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css), (
            "sem `[hidden] { display: none !important }`, qualquer regra com "
            "`display` anula o atributo hidden e as telas não trocam"
        )

    def test_toda_classe_alternada_por_hidden_esta_coberta(self) -> None:
        """Se alguém adicionar `display` a um elemento que o JS esconde, a regra
        acima continua cobrindo -- este teste garante que a cobertura existe
        ANTES de qualquer declaração de display."""
        css = (ESTATICOS / "style.css").read_text()
        pos_regra = css.find("[hidden]")
        pos_primeiro_display = css.find("display:")
        assert pos_regra != -1
        assert pos_regra < pos_primeiro_display or "!important" in css[pos_regra : pos_regra + 80]


class TestFormatoDeData:
    """Datas na tela em dd/mm/aaaa, e nao no formato do navegador.

    O `<input type="date">` nativo desenha a data no idioma da INTERFACE do
    navegador, nao no da pagina: com o Chrome em ingles ele mostra mm/dd/aaaa
    mesmo sob `lang="pt-BR"`, e nao existe atributo nem CSS que mude isso. Por
    isso o campo visivel virou texto com mascara, com o calendario nativo
    escondido atras do botao -- e este teste existe para que ninguem "simplifique"
    de volta para o input nativo sem perceber o que perde.

    Estes testes olham o codigo-fonte, entao provam que a conversao ESTA no
    lugar certo; nao provam que ela esta correta. A correcao de `brParaISO`
    (31/02 recusado, 29/02 so em ano bissexto) e verificada rodando a funcao.
    """

    def test_o_campo_de_data_visivel_nao_e_o_input_nativo(self) -> None:
        html = (ESTATICOS / "index.html").read_text()
        assert 'id="op-data" type="text"' in html, (
            "o campo visivel voltou a ser type=date: ele renderiza no idioma do "
            "navegador, entao um usuario com Chrome em ingles ve mm/dd/aaaa"
        )
        assert 'placeholder="dd/mm/aaaa"' in html

    def test_o_calendario_nativo_continua_disponivel(self) -> None:
        """Trocar o input por texto nao pode custar o seletor de datas: sem ele,
        registrar uma operacao antiga vira digitacao as cegas."""
        html = (ESTATICOS / "index.html").read_text()
        assert 'id="op-data-nativo" type="date"' in html
        assert 'id="op-data-calendario"' in html

    def test_o_nativo_fica_invisivel_mas_renderizado(self) -> None:
        """`display: none` faria `showPicker()` lancar InvalidStateError: o
        elemento precisa estar renderizado. Por isso opacity, nao display."""
        css = (ESTATICOS / "style.css").read_text()
        regra = re.search(r'\.campo-data > input\[type="date"\]\s*\{([^}]*)\}', css)
        assert regra is not None
        assert "opacity: 0" in regra.group(1)
        assert "display: none" not in regra.group(1)

    def test_a_data_de_hoje_nao_vem_de_toisostring(self) -> None:
        """`new Date().toISOString()` converte para UTC. As 21h de Brasilia la
        ja e o dia seguinte, entao o campo abriria preenchido com AMANHA -- e o
        backend, que tambem compara em UTC, aceitaria a operacao no futuro.
        Um erro que nao lanca nada e produz um numero plausivel."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "toISOString" not in js, (
            "toISOString() devolve a data em UTC; use hojeISO(), que usa o fuso "
            "de quem esta olhando a tela"
        )

    def test_o_envio_converte_para_iso(self) -> None:
        """A API so aceita aaaa-mm-dd. Se o valor do campo fosse enviado cru, o
        backend receberia 20/08/2026 e devolveria 422."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert 'brParaISO($("#op-data").value)' in js
        assert 'traded_at: $("#op-data").value' not in js
