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


class TestAvisoDeComparacaoFalha:
    """A API manda `motivo` quando o indexador foi pedido e a comparacao falhou
    (BCB fora do ar, ou o mes do IPCA ainda nao publicado). O campo existia na
    resposta e nunca era lido: quem escolhia "vs IPCA" numa carteira recente via
    a carteira sozinha, sem nenhuma explicacao de por que a linha de comparacao
    nao apareceu -- e isso parece defeito do sistema, nao uma situacao normal.
    """

    def test_o_campo_motivo_da_evolucao_e_lido(self) -> None:
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "evolucao.motivo" in js, (
            "`EvolutionResponse.motivo` existe na API e precisa ser mostrado "
            "quando a comparacao pedida falhar -- sem isso a tela fica muda"
        )

    def test_existe_um_elemento_para_mostrar_o_aviso(self) -> None:
        html = (ESTATICOS / "index.html").read_text()
        assert 'id="evolucao-aviso"' in html

    def test_o_aviso_fica_escondido_quando_nao_ha_motivo(self) -> None:
        """Sem motivo, sem aviso -- inclusive quando `motivo` é string vazia.

        A garantia mora em `mostrarSe()`, que centraliza o par "preenche e
        mostra, senão esconde". O teste verifica o CONTRATO do helper e o valor
        que a chamada passa, e não uma linha específica: assim ele sobrevive a
        refatoração e continua caindo se o comportamento mudar.
        """
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "elemento.hidden = !texto" in js, "mostrarSe() precisa esconder sem texto"
        assert 'mostrarSe($("#evolucao-aviso"), evolucao.motivo)' in js


class TestCartaoDeProventos:
    """O cálculo de proventos existia sem aparecer em lugar nenhum.

    Um endpoint que ninguém consome é trabalho que não serve para nada -- e
    a Fase 1 só fecha quando o número chega à tela.
    """

    def test_o_cartao_existe_no_html(self) -> None:
        html = (ESTATICOS / "index.html").read_text()
        assert 'id="tabela-proventos"' in html
        assert 'id="btn-sync-proventos"' in html

    def test_a_visao_geral_carrega_os_proventos(self) -> None:
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "carregarProventos()" in js
        assert "/portfolio/dividends" in js

    def test_avisa_sobre_proventos_sem_classificacao(self) -> None:
        """O Yahoo não distingue dividendo de JCP, e JCP tem 15% retidos. Um
        total exibido sem essa ressalva tem falsa precisão -- pode estar até
        15% acima do que caiu na conta."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "sem_classificacao" in js, (
            "o total pode estar 15% acima do real enquanto houver provento "
            "sem tipo; a tela precisa dizer isso"
        )

    def test_sincronizar_recarrega_o_grafico(self) -> None:
        """Provento entra no retorno TOTAL, então buscar proventos novos muda o
        gráfico de evolução -- não só a tabela de proventos."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        trecho = js[js.find("btn-sync-proventos") :]
        assert "invalidar()" in trecho[:1200]


class TestMarcaDePosicaoAjustada:
    """Sem isto, o usuário vê "200 cotas" na posição e "comprei 100" no
    extrato, e conclui -- com razão -- que um dos dois números está errado.
    A matemática do desdobramento estava certa; faltava a comunicação.
    """

    def test_o_marcador_existe(self) -> None:
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "marcaDeEvento" in js
        assert "p.eventos" in js, "a marca precisa vir do campo `eventos` da API"

    def test_so_marca_quando_ha_evento(self) -> None:
        """Quem nunca teve desdobramento não pode ver nada -- é informação de
        exceção, não um alerta permanente."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "if (!p.eventos?.length) return null;" in js

    def test_os_dois_lugares_que_mostram_posicao_usam_a_marca(self) -> None:
        """Resumo da visão geral E tabela completa. Marcar só um lugar deixaria
        o outro contando a mesma história pela metade."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        # Duas CHAMADAS, sem depender do nome da variavel que recebe cada uma.
        chamadas = js.count("marcaDeEvento(p)") - js.count("function marcaDeEvento(p)")
        assert chamadas == 2, f"esperava 2 chamadas, achei {chamadas}"

    def test_o_estilo_do_marcador_existe(self) -> None:
        css = (ESTATICOS / "style.css").read_text()
        assert ".marca-evento" in css


class TestBotaoDeApagarCarteira:
    """A exclusão de carteira existia na API e não existia na tela.

    É destrutiva de verdade -- leva o livro e todo o histórico --, então os
    testes aqui guardam as duas decisões que impedem o pior desfecho: não
    oferecer o botão onde ele não deve existir, e não perguntar de forma vaga.
    """

    def test_o_botao_existe(self) -> None:
        html = (ESTATICOS / "index.html").read_text()
        assert 'id="btn-apagar-carteira"' in html

    def test_nasce_escondido(self) -> None:
        """A primeira carteira que abre é a real, onde o botão não vale. Nascer
        visível o mostraria por um instante antes de o JS corrigir."""
        html = (ESTATICOS / "index.html").read_text()
        trecho = html[html.index('id="btn-apagar-carteira"') :][:300]
        assert "hidden" in trecho

    def test_so_aparece_em_carteira_simulada(self) -> None:
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert '$("#btn-apagar-carteira").hidden = !atual || atual.tipo !== "simulada"' in js

    def test_a_tela_tambem_recusa_apagar_a_real(self) -> None:
        """Defesa em profundidade: o backend devolve 409, e o handler nem
        chega a chamar. Uma das duas falhando, a outra segura."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        trecho = js[js.index('$("#btn-apagar-carteira").addEventListener') :][:400]
        assert 'atual.tipo !== "simulada"' in trecho and "return" in trecho

    def test_confirma_antes_de_apagar(self) -> None:
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        trecho = js[js.index('$("#btn-apagar-carteira").addEventListener') :]
        fim = trecho.index('$("#btn-nova-carteira")')
        handler = trecho[:fim]
        assert "confirm(" in handler, "exclusao destrutiva sem confirmacao"
        # A confirmação precisa vir ANTES do DELETE, não depois.
        assert handler.index("confirm(") < handler.index('method: "DELETE"')

    def test_a_confirmacao_diz_o_que_sera_perdido(self) -> None:
        """ "Tem certeza?" genérico não informa nada -- a pessoa clica em OK por
        reflexo. A mensagem precisa contar quantas operações e quantos dias de
        histórico somem."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        trecho = js[js.index('$("#btn-apagar-carteira").addEventListener') :][:2000]
        assert "operação" in trecho and "histórico" in trecho
        assert "desfazer" in trecho


class TestClassificarProvento:
    """O Yahoo não distingue dividendo de JCP, e a diferença vale 15% de
    imposto retido. O aviso de que o número pode estar errado já existia; a
    correção só existia na API."""

    def test_a_pilula_indefinida_vira_seletor(self) -> None:
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "seletorDeTipo" in js
        assert 'if (p.tipo === "indefinido")' in js

    def test_so_o_indefinido_e_editavel(self) -> None:
        """Provento já classificado virou fato. Reabrir a escolha convidaria a
        mexer no que está certo."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        trecho = js[js.index("const tdTipo = el(") :][:400]
        assert "else {" in trecho and "pilula-tipo" in trecho

    def test_reclassificar_recarrega_o_grafico(self) -> None:
        """Mudar o tipo muda o valor líquido, que entra no retorno total."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        trecho = js[js.index("function seletorDeTipo") :][:1600]
        assert "invalidar()" in trecho


class TestRotuloDaSincronizacao:
    """Regressão de uma ponta solta: o botão dizia "Buscar proventos" depois de
    passar a sincronizar desdobramentos também. Rótulo que mente sobre o que o
    botão faz é pior que rótulo genérico."""

    def test_o_rotulo_cobre_os_dois_eventos(self) -> None:
        html = (ESTATICOS / "index.html").read_text()
        assert "Buscar proventos" not in html
        assert "Atualizar eventos" in html

    def test_desdobramento_novo_tambem_recarrega_a_visao(self) -> None:
        """Desdobramento muda a QUANTIDADE das posições. Recarregar só quando
        vem provento deixaria a tela desatualizada no caso mais grave."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "r.gravados > 0 || r.desdobramentos > 0" in js

    def test_o_aviso_de_jcp_nao_e_sobrescrito(self) -> None:
        """O aviso de imposto retido é permanente enquanto houver provento sem
        classificação. A mensagem da sincronização soma, não substitui -- senão
        o alerta some no primeiro clique e não volta."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert 'aviso.hidden ? "" : aviso.textContent' in js


class TestTelaDeRebalanceamento:
    """A tela que traduz peso em ordem.

    Os testes guardam as decisões que impedem os dois piores desfechos: pedir
    um plano sem ter fronteira calculada (pesos de onde?) e o usuário achar
    que o app executou a ordem por ele.
    """

    def test_o_cartao_existe(self) -> None:
        html = (ESTATICOS / "index.html").read_text()
        assert 'id="btn-rebalancear"' in html
        assert 'id="tabela-ordens"' in html
        assert 'id="tabela-desvios"' in html

    def test_os_dois_modos_estao_na_tela(self) -> None:
        html = (ESTATICOS / "index.html").read_text()
        assert 'value="aporte"' in html and 'value="completo"' in html

    def test_deixa_claro_que_nao_executa(self) -> None:
        """Uma lista de ordens numa tela de investimento parece uma ordem
        enviada. Se a pessoa achar que o app comprou por ela, o dano não é de
        interface -- é de dinheiro."""
        html = (ESTATICOS / "index.html").read_text()
        assert "não é uma ordem enviada" in html or "sugestão, não uma ordem" in html
        assert "Nada é gravado no seu livro" in html

    def test_usa_os_pesos_da_fronteira_ja_calculada(self) -> None:
        """Mandar os pesos que a tela mostrou garante que o plano corresponde à
        carteira que a pessoa viu no gráfico. Recalcular no servidor poderia
        devolver outra."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "ultimaOtimizacao" in js
        assert "pesos: alvo.pesos" in js

    def test_recalcular_a_fronteira_invalida_o_plano(self) -> None:
        """Pesos novos, plano velho: deixar o anterior na tela mostraria ordens
        que não levam mais à carteira selecionada."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        trecho = js[js.index("ultimaOtimizacao = r;") :][:400]
        assert '$("#reb-resultado").hidden = true' in trecho

    def test_recusa_calcular_sem_fronteira(self) -> None:
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        trecho = js[js.index('$("#btn-rebalancear").addEventListener') :][:600]
        assert "if (!ultimaOtimizacao)" in trecho

    def test_aceita_virgula_no_aporte(self) -> None:
        """A API fala ponto decimal; a pessoa digita vírgula. Mandar "1.000,00"
        cru daria 422 num valor que ela escreveu certo."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "paraNumeroDaApi" in js

    def test_avisa_sobre_ativo_sem_cotacao(self) -> None:
        """Omitir em silêncio faria a pessoa achar que o plano cobre a carteira
        inteira."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "plano.sem_preco.length" in js

    def test_o_desvio_nao_usa_cor_de_lucro_e_prejuizo(self) -> None:
        """`sinal()` pinta lucro de verde e prejuízo de vermelho. A primeira
        versão desta tabela reusou isso, e ITUB4 -- 34,8 pontos ACIMA do alvo --
        aparecia em verde, como se estivesse indo bem. Estar acima é tão fora do
        lugar quanto estar abaixo."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        trecho = js[js.index("const desvios = $(") :]
        fim = trecho.index('const aviso = $("#reb-aviso")')
        assert "sinal(" not in trecho[:fim], "desvio nao pode usar a cor de lucro/prejuizo"

    def test_peso_e_proporcao_e_nao_variacao(self) -> None:
        """`pct()` prefixa "+" para valores não negativos. Num peso, "+35,0%"
        sugere um ganho de 35%."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "proporcao(d.peso_atual)" in js and "proporcao(d.peso_alvo)" in js


class TestTelaDeProjecao:
    """A projeção por Monte Carlo.

    O risco aqui não é a matemática — é a leitura. Um gráfico de projeção
    parece uma previsão, e este projeto vende faixa de probabilidade, não
    promessa.
    """

    def test_o_cartao_existe(self) -> None:
        html = (ESTATICOS / "index.html").read_text()
        assert 'id="btn-projetar"' in html and 'id="g-projecao"' in html

    def test_o_retorno_esperado_e_editavel(self) -> None:
        """O retorno vem de estimativa histórica, e uma carteira que subiu numa
        janela curta produz uma expectativa alta demais para projetar dez anos.
        Campo travado transformaria premissa discutível em promessa."""
        html = (ESTATICOS / "index.html").read_text()
        trecho = html[html.index('id="proj-retorno"') :][:200]
        assert "readonly" not in trecho and "disabled" not in trecho

    def test_a_ressalva_do_modelo_chega_a_tela(self) -> None:
        """A API manda `ressalva` dizendo que o modelo subestima crise. Não
        exibir seria esconder a limitação de quem mais precisa dela."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "r.ressalva" in js

    def test_mostra_o_percentil_5_e_nao_so_a_mediana(self) -> None:
        """Reportar só a mediana esconderia exatamente a informação que motiva
        a simulação: o cenário ruim."""
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        trecho = js[js.index("function renderProjecao") :]
        assert "final.p5" in trecho and "final.p50" in trecho

    def test_recalcular_a_fronteira_invalida_a_projecao(self) -> None:
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        trecho = js[js.index("ultimaOtimizacao = r;") :][:500]
        assert '$("#proj-resultado").hidden = true' in trecho

    def test_aceita_percentual_com_virgula(self) -> None:
        js = _sem_comentarios((ESTATICOS / "app.js").read_text())
        assert "percentualParaFracao" in js
