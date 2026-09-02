/* Portfolio Tracker — cliente da API.
 *
 * ## Duas decisões de segurança governam este arquivo
 *
 * 1. O access token vive numa VARIÁVEL, nunca em localStorage.
 *    localStorage é legível por qualquer script da página — uma dependência
 *    comprometida ou um XSS entrega a sessão. Aqui o token some ao fechar a aba,
 *    e a sessão é retomada pelo cookie httpOnly do refresh, que o JavaScript não
 *    consegue ler nem vazar. É o desenho da Etapa 3 sendo usado como planejado.
 *
 * 2. Nada de innerHTML com dado vindo da API.
 *    Todo texto entra por textContent. Um nome de ativo com "<img onerror=...>"
 *    vira texto, não script. Concatenar HTML com dado é como o XSS entra, mesmo
 *    quando o dado "é do próprio banco" — ele foi digitado por alguém.
 */
"use strict";

const API = "/api/v1";
let token = null;         // só em memória, de propósito
let usuarioEmail = "";
const graficos = {};
let ultimaEvolucao = null;
// A otimizacao mais recente. O rebalanceamento manda os pesos DELA de volta
// ao servidor, em vez de o servidor recalcular: assim o plano corresponde
// exatamente a carteira que a pessoa esta vendo no grafico.
let ultimaOtimizacao = null;
// Percentual por padrao: em reais, uma carteira que cresceu esmaga a escala e
// o CDI vira uma linha reta, sem informacao nenhuma.
let escala = "pct";
// Carteira ativa. Vai como `portfolio_id` em toda chamada de carteira --
// omitir usaria a real, e o usuario veria dados de outra carteira sem entender.
let carteiraAtiva = null;
let carteiras = [];

/* ═══ HTTP ═══ */

async function renovar() {
  // O cookie httpOnly viaja sozinho; não há nada para o JS anexar.
  const r = await fetch(`${API}/auth/refresh`, { method: "POST" });
  if (!r.ok) return false;
  token = (await r.json()).access_token;
  return true;
}

async function api(caminho, opcoes = {}, jaRenovou = false) {
  const cabecalhos = { ...(opcoes.headers || {}) };
  if (token) cabecalhos.Authorization = `Bearer ${token}`;
  if (opcoes.body) cabecalhos["Content-Type"] = "application/json";

  const r = await fetch(`${API}${caminho}`, { ...opcoes, headers: cabecalhos });

  // Access token expira em 15 min. Em vez de deslogar o usuário no meio do uso,
  // renovamos uma vez e repetimos a chamada. Uma vez só: se a renovação também
  // falhar, insistir viraria laço infinito.
  if (r.status === 401 && !jaRenovou && (await renovar())) {
    return api(caminho, opcoes, true);
  }
  if (r.status === 401) { mostrarLogin(); throw new Error("sessão expirada"); }
  if (!r.ok) {
    const corpo = await r.json().catch(() => ({}));
    throw new Error(detalhe(corpo) || `Erro ${r.status}`);
  }
  return r.status === 204 ? null : r.json();
}

async function mensagemDeFalha(r) {
  // Traduz a resposta do servidor em mensagem honesta.
  //
  // A primeira versao mostrava "E-mail ou senha incorretos" para QUALQUER falha.
  // Resultado: quando o rate limit da Etapa 3 respondeu 429, a tela acusou senha
  // errada -- e o usuario ficou tentando de novo, gastando mais tentativas e
  // prolongando o bloqueio. Mensagem de erro que mente e pior que mensagem
  // generica: ela manda a pessoa para o lado errado.
  //
  // O 401 continua generico DE PROPOSITO (nao distingue email inexistente de
  // senha errada, para nao virar oraculo de quem tem conta). Ja o 429 nao e
  // segredo nenhum -- esconde-lo so prejudica quem esta do lado certo.
  if (r.status === 429) {
    const segundos = Number(r.headers.get("Retry-After")) || 60;
    return `Muitas tentativas de login. Aguarde ${segundos}s e tente de novo.`;
  }
  if (r.status === 401) return "E-mail ou senha incorretos";
  const corpo = await r.json().catch(() => ({}));
  return detalhe(corpo) || `Não foi possível entrar (erro ${r.status})`;
}

function detalhe(corpo) {
  const d = corpo?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => e.msg).join("; ");
  return null;
}

/* ═══ Formatação ═══ */

const brl = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const pct = (v, casas = 2) =>
  `${v >= 0 ? "+" : ""}${(v * 100).toLocaleString("pt-BR", { minimumFractionDigits: casas, maximumFractionDigits: casas })}%`;
const num = (v) => Number(v).toLocaleString("pt-BR", { maximumFractionDigits: 8 });
/** Proporção (0.35 -> "35,0%"). Sem sinal: peso não é variação. */
const proporcao = (v, casas = 1) =>
  `${(Number(v) * 100).toLocaleString("pt-BR", { minimumFractionDigits: casas, maximumFractionDigits: casas })}%`;
const dataBR = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString("pt-BR");

/* ═══ Datas ═══
   A API fala ISO (aaaa-mm-dd) porque e o unico formato sem ambiguidade:
   03/04 e 3 de abril aqui e 4 de marco nos EUA. O usuario nunca ve ISO --
   a conversao acontece na fronteira, nestas quatro funcoes, e em lugar nenhum
   mais. Quando entrar o proximo campo de data (proventos, por exemplo), ele
   reusa isto em vez de reinventar a regra. */

/** A data de hoje no fuso de quem esta olhando a tela. */
const hojeISO = () => {
  const d = new Date();
  // Nao usar toISOString(): ela converte para UTC, e as 21h de Brasilia la ja
  // e o dia seguinte. O campo abriria preenchido com amanha -- e como o
  // backend tambem compara em UTC, ele aceitaria a operacao no futuro.
  const dd = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${dd(d.getMonth() + 1)}-${dd(d.getDate())}`;
};

/** `hojeISO()` menos `dias` dias corridos -- usada pelo seletor de período do
 * gráfico de evolução (30/90/180/365 dias). `Date` já resolve a virada de
 * mês/ano sozinho (30/set − 5 dias = 25/set, 3/jan − 5 dias = 29/dez). */
const isoMenosDias = (dias) => {
  const d = new Date();
  d.setDate(d.getDate() - dias);
  const dd = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${dd(d.getMonth() + 1)}-${dd(d.getDate())}`;
};

const isoParaBR = (iso) => {
  const [a, m, d] = iso.split("-");
  return `${d}/${m}/${a}`;
};

/** dd/mm/aaaa -> aaaa-mm-dd. Devolve "" se a data nao existe no calendario. */
const brParaISO = (br) => {
  const p = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(br.trim());
  if (!p) return "";
  const dia = Number(p[1]), mes = Number(p[2]), ano = Number(p[3]);
  // 31/02/2026 passa no regex, e o Date "conserta" para 03/03/2026 em silencio.
  // Remontar e conferir os componentes e a unica checagem que pega isso.
  const d = new Date(ano, mes - 1, dia);
  if (d.getFullYear() !== ano || d.getMonth() !== mes - 1 || d.getDate() !== dia) return "";
  return `${p[3]}-${p[2]}-${p[1]}`;
};

/** Insere as barras enquanto o usuario digita, sem exigir que ele as digite. */
function aplicarMascaraData(input) {
  input.addEventListener("input", () => {
    // Colar aaaa-mm-dd e comum (vem de planilha, do extrato, da propria API).
    // Sem este caso, os digitos seriam remontados na ordem errada e virariam
    // uma data invalida silenciosa: 2026-08-20 sairia como 20/26/0820.
    const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(input.value.trim());
    if (iso) {
      input.value = isoParaBR(input.value.trim());
      input.setSelectionRange(input.value.length, input.value.length);
      return;
    }

    const cursor = input.selectionStart ?? input.value.length;
    const digitosAntesDoCursor = input.value.slice(0, cursor).replace(/\D/g, "").length;

    const d = input.value.replace(/\D/g, "").slice(0, 8);
    input.value = [d.slice(0, 2), d.slice(2, 4), d.slice(4, 8)].filter(Boolean).join("/");

    // Reposicionar o cursor contando digitos, nao caracteres: reatribuir .value
    // joga o cursor para o fim, o que torna impossivel corrigir o meio da data.
    let pos = 0, vistos = 0;
    while (pos < input.value.length && vistos < digitosAntesDoCursor) {
      if (/\d/.test(input.value[pos])) vistos++;
      pos++;
    }
    input.setSelectionRange(pos, pos);
  });
}
const sinal = (v) => (Number(v) >= 0 ? "pos" : "neg");

/* Mostra o elemento com o texto, ou esconde quando não há texto.

   Este par -- "preenche e mostra, senão esconde" -- aparecia quatro vezes no
   arquivo, sempre com o mesmo if/else de quatro linhas. Além da repetição, ele
   centraliza o contrato do `hidden`, que já nos custou caro uma vez: a tela de
   login não sumia porque `display: grid` no CSS vence o atributo. Com um ponto
   só, uma correção desse tipo acontece em um lugar. */
function mostrarSe(elemento, texto) {
  elemento.textContent = texto || "";
  elemento.hidden = !texto;
}

function el(tag, classe, texto) {
  const e = document.createElement(tag);
  if (classe) e.className = classe;
  if (texto !== undefined) e.textContent = texto;   // textContent, nunca innerHTML
  return e;
}
const $ = (s) => document.querySelector(s);
function limpar(node) { while (node.firstChild) node.removeChild(node.firstChild); }

/* ═══ Login ═══ */

let relogioDaDemo = null;

function mostrarLogin() {
  $("#login").hidden = false;
  $("#app").hidden = true;
  token = null;
  // Para o contador da demonstração. Sem isto ele continuaria rodando sobre um
  // elemento escondido e, ao vencer, chamaria esta mesma função de novo --
  // derrubando para a tela de login alguém que já entrou em outra conta.
  clearInterval(relogioDaDemo);
  $("#aviso-demo").hidden = true;
}

function comCarteira(caminho) {
  // Anexa a carteira ativa a uma rota de carteira.
  //
  // Omitir o parametro faria a API usar a carteira REAL por padrao -- e o
  // usuario veria os dados de uma carteira diferente da que ele selecionou,
  // sem nenhum aviso. Por isso o parametro e sempre explicito no cliente.
  if (!carteiraAtiva) return caminho;
  const separador = caminho.includes("?") ? "&" : "?";
  return `${caminho}${separador}portfolio_id=${carteiraAtiva}`;
}

async function carregarCarteiras() {
  carteiras = await api("/portfolios");
  if (!carteiras.some((c) => c.id === carteiraAtiva)) {
    carteiraAtiva = carteiras[0]?.id ?? null;
  }

  const select = $("#carteira");
  limpar(select);
  for (const c of carteiras) {
    const opcao = el("option", null, c.nome);
    opcao.value = c.id;
    if (c.id === carteiraAtiva) opcao.selected = true;
    select.append(opcao);
  }

  const atual = carteiras.find((c) => c.id === carteiraAtiva);
  const pilula = $("#pilula-tipo");
  pilula.className = "pilula pilula--simulada";
  mostrarSe(pilula, atual?.tipo === "simulada" ? "simulação" : "");

  // A lixeira some na carteira real -- ela não pode ser apagada. Some, e não
  // fica desabilitada: um botão cinza convida a tentar e depois frustra. A
  // recusa de verdade está no backend (409); isto aqui é só não oferecer.
  $("#btn-apagar-carteira").hidden = !atual || atual.tipo !== "simulada";
  $("#btn-zerar-transacoes").hidden = !atual || atual.tipo !== "simulada";
}

async function entrarNoApp() {
  // Carrega os dados ANTES de trocar de tela.
  //
  // A ordem inversa deixa um estado quebrado quando algo falha: a tela de login
  // já saiu, a aplicação aparece vazia, e a mensagem de erro é escrita num
  // elemento que não está mais visível. O usuário fica olhando uma página em
  // branco sem nenhuma explicação.
  const eu = await api("/auth/me");
  await carregarCarteiras();
  usuarioEmail = eu.email;
  $("#usuario-email").textContent = eu.email;
  $("#avatar").textContent = eu.email.slice(0, 2).toUpperCase();
  await carregarVisao();

  marcarDemo(eu);

  $("#login").hidden = true;
  $("#app").hidden = false;
}

// ══ MODO DEMONSTRAÇÃO ══

function restanteEmTexto(fim) {
  const segundos = Math.max(0, Math.floor((fim - Date.now()) / 1000));
  const h = Math.floor(segundos / 3600);
  const m = Math.floor((segundos % 3600) / 60);
  // Sem segundos acima de um minuto: um contador piscando a cada segundo puxa
  // o olho para o relógio em vez da carteira, que é o que a pessoa veio ver.
  if (h > 0) return `${h}h${String(m).padStart(2, "0")}`;
  if (m > 0) return `${m} min`;
  return `${segundos}s`;
}

function marcarDemo(usuario) {
  clearInterval(relogioDaDemo);
  const aviso = $("#aviso-demo");
  aviso.hidden = !usuario.is_demo;
  if (!usuario.is_demo || !usuario.expires_at) return;

  // O e-mail sintético (demo-a1f3…@demo.invalid) não diz nada a ninguém e
  // ocupa o lugar onde a pessoa espera se reconhecer.
  $("#usuario-email").textContent = "Visitante";
  $("#avatar").textContent = "VC";

  const fim = new Date(usuario.expires_at).getTime();
  const tique = () => {
    $("#demo-restante").textContent = restanteEmTexto(fim);
    // Quando vence, o backend já recusa tudo com 401. Avisar aqui evita que a
    // pessoa descubra pelo erro de uma ação que ela tentou fazer.
    if (fim <= Date.now()) {
      clearInterval(relogioDaDemo);
      mostrarLogin();
    }
  };
  tique();
  relogioDaDemo = setInterval(tique, 30_000);
}

$("#btn-demo").addEventListener("click", async () => {
  const erro = $("#login-erro");
  erro.hidden = true;
  const botao = $("#btn-demo");
  botao.disabled = true;
  botao.textContent = "Preparando a carteira…";
  try {
    // Sem corpo e sem credencial: a conta é criada e autenticada no mesmo passo.
    const r = await fetch(`${API}/auth/demo`, { method: "POST" });
    if (!r.ok) {
      if (r.status === 429) throw new Error("Muitas demonstrações agora há pouco. Tente em um minuto.");
      throw new Error(await mensagemDeFalha(r));
    }
    token = (await r.json()).access_token;
    await entrarNoApp();
  } catch (e) {
    erro.textContent = e.message;
    erro.hidden = false;
  } finally {
    botao.disabled = false;
    botao.textContent = "Ver demonstração";
  }
});

$("#form-login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const erro = $("#login-erro");
  erro.hidden = true;
  const botao = $("#btn-entrar");
  botao.disabled = true;
  botao.textContent = "Entrando…";
  try {
    const corpo = new URLSearchParams({ username: $("#email").value, password: $("#senha").value });
    const r = await fetch(`${API}/auth/login`, { method: "POST", body: corpo });
    if (!r.ok) throw new Error(await mensagemDeFalha(r));
    token = (await r.json()).access_token;
    await entrarNoApp();
  } catch (e) {
    erro.textContent = e.message;
    erro.hidden = false;
  } finally {
    botao.disabled = false;
    botao.textContent = "Entrar";
  }
});

$("#btn-ver-senha").addEventListener("click", () => {
  const campo = $("#senha");
  const visivel = campo.type === "text";
  campo.type = visivel ? "password" : "text";
  $("#btn-ver-senha").textContent = visivel ? "Mostrar" : "Ocultar";
  campo.focus();
});

$("#btn-criar").addEventListener("click", async () => {
  const erro = $("#login-erro");
  erro.hidden = true;
  try {
    const r = await fetch(`${API}/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("#email").value, password: $("#senha").value }),
    });
    if (!r.ok) {
      if (r.status === 409) throw new Error("Este e-mail já tem conta. Basta entrar.");
      throw new Error(await mensagemDeFalha(r));
    }
    $("#form-login").requestSubmit();
  } catch (e) {
    erro.textContent = e.message;
    erro.hidden = false;
  }
});

$("#btn-sair").addEventListener("click", async () => {
  // Revoga no servidor, não só limpa o cookie: uma cópia do refresh token
  // continuaria valendo por 30 dias.
  await fetch(`${API}/auth/logout`, { method: "POST" }).catch(() => {});
  mostrarLogin();
});

/* ═══ Navegação ═══ */

const carregado = {};
document.addEventListener("click", (ev) => {
  const alvo = ev.target.closest("[data-vista]");
  if (!alvo || alvo.tagName === "SECTION") return;
  const vista = alvo.dataset.vista;

  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("is-ativo", b.dataset.vista === vista));
  document.querySelectorAll("section.vista").forEach((s) => { s.hidden = s.dataset.vista !== vista; });

  if (vista === "fronteira" && !carregado.fronteira) otimizar();
  if (vista === "posicoes" && !carregado.posicoes) carregarPosicoes();
  if (vista === "transacoes" && !carregado.transacoes) carregarTransacoes();
});

/* ═══ Visão geral ═══ */

async function carregarVisao() {
  const indexador = $("#indexador").value;
  // "Tudo" (value="") não manda `desde` -- a API já limita em `limit`. Um
  // período em dias vira uma data de corte; a partir dela o backend filtra
  // ANTES de contar o limite, então "1 ano" não trunca no meio do ano por
  // causa de `limit`.
  const dias = $("#periodo").value;
  const filtroPeriodo = dias ? `&desde=${isoMenosDias(Number(dias))}` : "";
  const [resumo, evolucao] = await Promise.all([
    api(comCarteira("/portfolio/summary")),
    api(
      comCarteira(
        `/portfolio/evolution?limit=500${indexador ? `&indexador=${indexador}` : ""}${filtroPeriodo}`
      )
    ),
  ]);

  const t = resumo.totals;
  $("#kpi-mercado").textContent = brl.format(t.valor_mercado);
  $("#kpi-custo").textContent = brl.format(t.custo_total);
  $("#kpi-nao-realizado").textContent = brl.format(t.resultado_nao_realizado);
  $("#kpi-realizado").textContent = brl.format(t.resultado_realizado);

  $("#destaque-total").textContent = brl.format(t.valor_mercado);
  const variacao = t.variacao_percentual;
  $("#destaque-var").textContent =
    variacao === null ? "sem posições" : `${pct(variacao / 100)} sobre o custo`;
  $("#destaque-ativos").textContent = `${resumo.positions.length} ativo(s)`;

  const fonte = resumo.positions.find((p) => p.cotacao_fonte)?.cotacao_fonte;
  $("#destaque-fonte").textContent = fonte ? `cotação via ${fonte}` : "sem cotação";
  const quando = resumo.positions.find((p) => p.cotacao_em)?.cotacao_em;
  $("#selo-atualizado").textContent = quando
    ? `atualizado ${new Date(quando).toLocaleString("pt-BR")}`
    : "sem cotação disponível";

  ultimaEvolucao = evolucao;
  desenharEvolucao(evolucao);
  carregarProventos().catch(() => {});
  renderPosicoesResumo(resumo.positions);
  renderOperacoesResumo(await api(comCarteira("/transactions?limit=5")));
}

/* Um marcador na posição cuja quantidade não bate com o extrato.

   Sem isto o usuário vê "200 cotas" na posição e "comprei 100" nas
   transações, e conclui -- com razão -- que um dos dois números está errado.
   O texto vai no `title` porque é explicação sob demanda: quem nunca teve
   desdobramento nunca precisa ler nada. */
function marcaDeEvento(p) {
  if (!p.eventos?.length) return null;

  const marca = el("span", "marca-evento", "ajustada");
  const detalhe = p.eventos
    .map((e) => `${dataBR(e.data_ex)}: ${e.proporcao} (×${num(e.fator)})`)
    .join("\n");
  const total = p.eventos.reduce((acc, e) => acc * Number(e.fator), 1);
  marca.title =
    `Quantidade ajustada por ${p.eventos.length} evento` +
    `${p.eventos.length > 1 ? "s" : ""} desde a primeira compra:\n${detalhe}\n\n` +
    `Suas cotas foram multiplicadas por ${num(total)}. O custo total não muda ` +
    `-- desdobramento reparte o mesmo dinheiro em mais pedaços.`;
  return marca;
}

function renderPosicoesResumo(posicoes) {
  const lista = $("#lista-posicoes");
  limpar(lista);
  if (!posicoes.length) { lista.append(el("p", "vazio", "Nenhuma posição aberta.")); return; }

  for (const p of posicoes.slice(0, 5)) {
    const linha = el("div", "linha");
    linha.append(el("div", "ficha", p.ticker.slice(0, 4)));

    const txt = el("div", "linha-txt");
    const nome = el("strong", null, p.ticker);
    const marca = marcaDeEvento(p);
    if (marca) nome.append(marca);
    txt.append(nome);
    txt.append(el("span", null, `${num(p.quantidade)} × ${brl.format(p.preco_medio)}`));
    linha.append(txt);

    const dir = el("div", "linha-num");
    dir.append(el("strong", null, brl.format(p.valor_mercado ?? p.custo_total)));
    const r = p.resultado_nao_realizado;
    dir.append(el("span", r === null ? "" : sinal(r), r === null ? "sem cotação" : `${brl.format(r)} (${pct((p.variacao_percentual ?? 0) / 100)})`));
    linha.append(dir);

    lista.append(linha);
  }
}

function renderOperacoesResumo(pagina) {
  const lista = $("#lista-operacoes");
  limpar(lista);
  if (!pagina.items.length) { lista.append(el("p", "vazio", "Nenhuma operação registrada.")); return; }

  for (const t of pagina.items) {
    const linha = el("div", "linha");
    linha.append(el("div", "ficha", t.ticker.slice(0, 4)));

    const txt = el("div", "linha-txt");
    txt.append(el("strong", null, t.ticker));
    txt.append(el("span", null, dataBR(t.traded_at)));
    linha.append(txt);

    linha.append(el("span", `pilula pilula--${t.side}`, t.side === "compra" ? "Compra" : "Venda"));

    const dir = el("div", "linha-num");
    dir.append(el("strong", null, brl.format(Number(t.quantity) * Number(t.price))));
    dir.append(el("span", null, `${num(t.quantity)} × ${brl.format(t.price)}`));
    linha.append(dir);

    lista.append(linha);
  }
}

/* ═══ Gráficos ═══ */

const GRADE = "rgba(255,255,255,.055)";
const EIXO = "#6f6a8c";

function base(escalaY = {}) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: "#16142699", borderColor: "rgba(255,255,255,.14)", borderWidth: 1,
        padding: 11, cornerRadius: 10, titleColor: "#e9e7f3", bodyColor: "#a9a4c2",
      },
    },
    scales: {
      x: { grid: { color: GRADE, drawTicks: false }, border: { display: false }, ticks: { color: EIXO, maxRotation: 0, autoSkipPadding: 24 } },
      y: { grid: { color: GRADE, drawTicks: false }, border: { display: false }, ticks: { color: EIXO }, ...escalaY },
    },
  };
}

function gradiente(ctx, cor) {
  const g = ctx.createLinearGradient(0, 0, 0, 320);
  g.addColorStop(0, `${cor}44`);
  g.addColorStop(1, `${cor}00`);
  return g;
}

function desenharEvolucao(evolucao) {
  const canvas = $("#g-evolucao");
  const ctx = canvas.getContext("2d");
  const selo = $("#selo-cdi");
  graficos.evolucao?.destroy();

  const pontos = evolucao.pontos;
  if (!pontos.length) {
    selo.hidden = true;
    $("#evolucao-aviso").hidden = true;
    return;
  }

  const emPct = escala === "pct";
  const nomeBench = evolucao.benchmark?.nome;
  $("#sub-evolucao").textContent = emPct
    ? `Rentabilidade acumulada${nomeBench ? ` × ${nomeBench}` : ""}`
    : `Valor de mercado × custo${nomeBench ? ` × ${nomeBench}` : ""}`;

  // `motivo` só vem preenchido quando um indexador FOI pedido e a comparação
  // falhou (BCB fora do ar, ou -- caso comum do IPCA -- o mês ainda não foi
  // publicado). Gráfico sem a linha de comparação e sem explicação nenhuma
  // parece defeito do sistema; a API já manda o motivo, faltava a tela ler.
  mostrarSe($("#evolucao-aviso"), evolucao.motivo);

  const conjuntos = [];
  const rotulos = [];

  if (emPct) {
    // Retorno ponderado pelo tempo: isola o efeito do mercado dos aportes.
    // O percentual ingênuo (valor/custo − 1) despencaria a cada aporte, sem o
    // mercado ter mexido.
    conjuntos.push({
      label: "Carteira",
      data: evolucao.rentabilidade.map((p) => Number(p.carteira) * 100),
      borderColor: "#ff4fa3", backgroundColor: gradiente(ctx, "#ff4fa3"),
      borderWidth: 2.4, fill: true, tension: .35, pointRadius: 0, pointHoverRadius: 5,
    });
    rotulos.push(["#ff4fa3", "Carteira"]);

    if (nomeBench) {
      conjuntos.push({
        label: nomeBench,
        data: evolucao.rentabilidade.map((p) =>
          p.benchmark === null ? null : Number(p.benchmark) * 100
        ),
        borderColor: "#f5b54a", borderWidth: 2, fill: false, tension: .35,
        pointRadius: 0, pointHoverRadius: 5, spanGaps: true,
      });
      rotulos.push(["#f5b54a", nomeBench]);
    }
  } else {
    conjuntos.push(
      {
        label: "Valor de mercado", data: pontos.map((p) => Number(p.valor_mercado)),
        borderColor: "#ff4fa3", backgroundColor: gradiente(ctx, "#ff4fa3"),
        borderWidth: 2.4, fill: true, tension: .35, pointRadius: 0, pointHoverRadius: 5,
      },
      {
        label: "Custo", data: pontos.map((p) => Number(p.custo_total)),
        borderColor: "#35d6e8", borderWidth: 1.8, borderDash: [5, 5],
        fill: false, tension: .35, pointRadius: 0, pointHoverRadius: 5,
      },
    );
    rotulos.push(["#ff4fa3", "Valor de mercado"], ["#35d6e8", "Custo"]);

    if (evolucao.benchmark) {
      // Alinhamos por DATA, não por posição: o indexador não rende em feriado,
      // então as séries podem ter comprimentos diferentes. Casar por índice
      // deslocaria a curva inteira sem nenhum erro aparente.
      const porData = new Map(evolucao.benchmark.pontos.map((p) => [p.date, Number(p.valor)]));
      conjuntos.push({
        label: nomeBench,
        data: pontos.map((p) => porData.get(p.date) ?? null),
        borderColor: "#f5b54a", borderWidth: 2, fill: false, tension: .35,
        pointRadius: 0, pointHoverRadius: 5, spanGaps: true,
      });
      rotulos.push(["#f5b54a", nomeBench]);
    }
  }

  const formatarEixo = emPct
    ? (v) => `${v.toFixed(2)}%`
    : (v) => brl.format(v).replace(/\s/g, "");

  graficos.evolucao = new Chart(ctx, {
    type: "line",
    data: { labels: pontos.map((p) => dataBR(p.date)), datasets: conjuntos },
    options: {
      ...base({ ticks: { color: EIXO, callback: formatarEixo } }),
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#16142699", borderColor: "rgba(255,255,255,.14)", borderWidth: 1,
          padding: 11, cornerRadius: 10, titleColor: "#e9e7f3", bodyColor: "#a9a4c2",
          callbacks: {
            label: (c) =>
              `${c.dataset.label}: ${
                emPct ? `${c.parsed.y >= 0 ? "+" : ""}${c.parsed.y.toFixed(2)}%` : brl.format(c.parsed.y)
              }`,
          },
        },
      },
    },
  });

  const legenda = $("#legenda-evolucao");
  limpar(legenda);
  for (const [cor, rotulo] of rotulos) {
    const sp = el("span", "lg");
    const i = el("i");
    i.style.background = cor;
    sp.append(i, document.createTextNode(rotulo));
    legenda.append(sp);
  }

  const c = evolucao.comparacao;
  if (c && c.excesso_pontos_percentuais !== null) {
    const excesso = Number(c.excesso_pontos_percentuais);
    // Uma palavra muda entre os dois casos, não a frase inteira. Duplicar o
    // texto convidava as duas versões a divergirem na próxima edição.
    const lado = excesso >= 0 ? "acima" : "abaixo";
    selo.className = `selo-comparacao ${lado}`;
    selo.title = `Carteira ${c.carteira_percentual}% · ${nomeBench} ${c.benchmark_percentual}%`;
    mostrarSe(selo, `${pct(excesso / 100, 2)} ${lado} ${artigo(nomeBench)} ${nomeBench}`);
  } else {
    selo.hidden = true;
  }
}

document.querySelectorAll("[data-escala]").forEach((botao) => {
  botao.addEventListener("click", () => {
    escala = botao.dataset.escala;
    document
      .querySelectorAll("[data-escala]")
      .forEach((b) => b.classList.toggle("is-ativo", b === botao));
    // Redesenha com os dados que já estão em memória: trocar a unidade do
    // gráfico não é motivo para uma requisição nova.
    if (ultimaEvolucao) desenharEvolucao(ultimaEvolucao);
  });
});

$("#indexador").addEventListener("change", () => {
  carregarVisao().catch(() => {});
});
$("#periodo").addEventListener("change", () => {
  carregarVisao().catch(() => {});
});

/* ═══ Proventos ═══ */

/* O artigo é dado, não condicional.

   O texto dizia "acima do Selic" -- a Selic é feminina. Um `if nome ===
   "Selic"` resolveria hoje e seria esquecido no próximo indexador; uma tabela
   diz o que fazer com cada nome, e o que não estiver nela cai no masculino,
   que é o caso comum. */
const ARTIGO_DO_INDEXADOR = { Selic: "da" };
const artigo = (nome) => ARTIGO_DO_INDEXADOR[nome] ?? "do";

const ROTULO_TIPO = {
  dividendo: "Dividendo",
  jcp: "JCP",
  rendimento: "Rendimento",
  indefinido: "A classificar",
};

/* A pílula "A classificar" vira um seletor ao ser clicada.

   O Yahoo não diz se o provento foi dividendo ou JCP, e a diferença vale 15%
   de imposto retido. O aviso de que o número pode estar errado já existia; o
   que faltava era a pessoa poder consertar sem sair da tela.

   Só proventos indefinidos ganham o seletor: os já classificados viraram fato,
   e reabrir a escolha convidaria a mexer no que está certo. */
function seletorDeTipo(p) {
  const select = el("select", "seletor-tipo");
  select.setAttribute("aria-label", `Classificar o provento de ${p.ticker} de ${dataBR(p.data_com)}`);

  const inicial = el("option", null, "A classificar");
  inicial.value = "";
  select.append(inicial);
  for (const [valor, rotulo] of [
    ["dividendo", "Dividendo"],
    ["jcp", "JCP (−15%)"],
    ["rendimento", "Rendimento"],
  ]) {
    const o = el("option", null, rotulo);
    o.value = valor;
    select.append(o);
  }

  select.addEventListener("change", async () => {
    if (!select.value) return;
    select.disabled = true;
    try {
      await api(comCarteira("/portfolio/dividends/reclassificar"), {
        method: "POST",
        body: JSON.stringify({
          ticker: p.ticker,
          data_com: p.data_com,
          tipo: select.value,
        }),
      });
      // Reclassificar muda o valor líquido, que entra no retorno total --
      // então o gráfico muda junto, não só esta tabela.
      await carregarProventos();
      invalidar();
    } catch (e) {
      const aviso = $("#proventos-aviso");
      aviso.textContent = e.message;
      aviso.hidden = false;
      select.disabled = false;
      select.value = "";
    }
  });
  return select;
}

async function carregarProventos() {
  const dados = await api(comCarteira("/portfolio/dividends"));
  const corpo = $("#tabela-proventos tbody");
  const vazio = $("#proventos-vazio");
  const selo = $("#selo-proventos");
  const aviso = $("#proventos-aviso");
  limpar(corpo);

  if (!dados.proventos.length) {
    $("#tabela-proventos").hidden = true;
    selo.hidden = true;
    aviso.hidden = true;
    vazio.textContent =
      "Nenhum provento registrado. Clique em “Atualizar eventos” para buscar " +
      "proventos e desdobramentos dos ativos desta carteira.";
    vazio.hidden = false;
    return;
  }

  $("#tabela-proventos").hidden = false;
  vazio.hidden = true;

  selo.textContent = `${brl.format(dados.total_liquido)} recebidos`;
  selo.hidden = false;

  const y = dados.yield_on_cost;
  $("#sub-proventos").textContent =
    y === null
      ? "Dividendos, JCP e rendimentos recebidos"
      : `Dividendos, JCP e rendimentos — ${pct(Number(y))} sobre o custo`;

  // O aviso existe porque o número pode estar até 15% acima do real: o Yahoo
  // não distingue dividendo de JCP, e JCP tem retenção na fonte. Exibir um
  // total com falsa precisão seria pior do que exibir a ressalva.
  const n = dados.sem_classificacao;
  mostrarSe(
    aviso,
    n > 0 &&
      `${n} provento${n > 1 ? "s" : ""} sem classificação: o fornecedor não ` +
        "informa se foi dividendo ou JCP. Se for JCP, há 15% de imposto retido " +
        "que ainda não está descontado."
  );

  for (const p of dados.proventos) {
    const tr = el("tr");
    tr.append(el("td", null, dataBR(p.data_com)));
    tr.append(el("td", null, p.ticker));

    const tdTipo = el("td");
    if (p.tipo === "indefinido") {
      tdTipo.append(seletorDeTipo(p));
    } else {
      const pilula = el("span", "pilula-tipo", ROTULO_TIPO[p.tipo] || p.tipo);
      pilula.dataset.tipo = p.tipo;
      tdTipo.append(pilula);
    }
    tr.append(tdTipo);

    tr.append(el("td", "num", num(p.quantidade)));
    tr.append(el("td", "num", brl.format(p.valor_por_cota)));
    tr.append(el("td", "num", brl.format(p.valor_bruto)));
    tr.append(el("td", "num", brl.format(p.valor_liquido)));
    corpo.append(tr);
  }
}

$("#btn-sync-proventos").addEventListener("click", async (ev) => {
  const botao = ev.currentTarget;
  const rotulo = botao.textContent;
  botao.disabled = true;
  botao.textContent = "Buscando…";
  try {
    // Uma requisição externa POR ATIVO no servidor: pode demorar. Desabilitar o
    // botão evita a fila de cliques que estouraria o rate limit de 10/hora.
    const r = await api(comCarteira("/portfolio/dividends/sync"), { method: "POST" });
    await carregarProventos();
    // Recarrega a visão inteira: provento entra no retorno total e
    // desdobramento muda a quantidade, então o gráfico e as posições mudam
    // junto.
    if (r.gravados > 0 || r.desdobramentos > 0) invalidar();

    // Dizer o que aconteceu, inclusive quando nada aconteceu.
    //
    // Um botão que recarrega em silêncio deixa a pessoa sem saber se funcionou
    // ou se o clique se perdeu -- e "0 novos" é uma resposta legítima, não uma
    // falha: significa que já estava tudo lá.
    const partes = [];
    if (r.gravados) partes.push(`${r.gravados} provento${r.gravados > 1 ? "s" : ""}`);
    if (r.desdobramentos) {
      partes.push(`${r.desdobramentos} desdobramento${r.desdobramentos > 1 ? "s" : ""}`);
    }
    const resumo = partes.length
      ? `${partes.join(" e ")} encontrado${partes.length > 1 || r.gravados > 1 ? "s" : ""}.`
      : `Nada novo em ${r.tickers_consultados.length} ativo(s) — já estava atualizado.`;

    // `carregarProventos()` acabou de escrever aqui o alerta de provento sem
    // classificação. Ele CONTINUA valendo -- somamos a mensagem em vez de
    // sobrescrever, senão o aviso de 15% de imposto some no primeiro clique
    // e não volta mais.
    const aviso = $("#proventos-aviso");
    const alerta = aviso.hidden ? "" : aviso.textContent;
    aviso.textContent = alerta ? `${resumo} ${alerta}` : resumo;
    aviso.hidden = false;
  } catch (e) {
    const aviso = $("#proventos-aviso");
    aviso.textContent = e.message;
    aviso.hidden = false;
  } finally {
    botao.disabled = false;
    botao.textContent = rotulo;
  }
});

/* ═══ Posições ═══ */

async function carregarPosicoes() {
  carregado.posicoes = true;
  const [resumo, metricas] = await Promise.all([
    api(comCarteira("/portfolio/summary")),
    api(comCarteira("/portfolio/metrics")),
  ]);

  const corpo = $("#tabela-posicoes tbody");
  limpar(corpo);
  if (!resumo.positions.length) {
    const tr = el("tr");
    const td = el("td", "vazio", "Nenhuma posição aberta.");
    td.colSpan = 8;
    tr.append(td);
    corpo.append(tr);
  }
  for (const p of resumo.positions) {
    const tr = el("tr");
    const tdTicker = el("td", null, p.ticker);
    const marcaT = marcaDeEvento(p);
    if (marcaT) tdTicker.append(marcaT);
    tr.append(tdTicker);
    tr.append(el("td", "num", num(p.quantidade)));
    tr.append(el("td", "num", brl.format(p.preco_medio)));
    tr.append(el("td", "num", p.preco_atual ? brl.format(p.preco_atual) : "—"));
    tr.append(el("td", "num", brl.format(p.custo_total)));
    tr.append(el("td", "num", p.valor_mercado ? brl.format(p.valor_mercado) : "—"));
    const r = el("td", `num ${p.resultado_nao_realizado === null ? "" : sinal(p.resultado_nao_realizado)}`,
      p.resultado_nao_realizado === null ? "—" : brl.format(p.resultado_nao_realizado));
    tr.append(r);
    tr.append(el("td", `num ${p.variacao_percentual === null ? "" : sinal(p.variacao_percentual)}`,
      p.variacao_percentual === null ? "—" : pct(p.variacao_percentual / 100)));
    corpo.append(tr);
  }

  desenharMatriz(metricas.correlacao);
}

function desenharMatriz(correlacao) {
  const alvo = $("#matriz-correlacao");
  limpar(alvo);
  if (!correlacao) {
    alvo.append(el("p", "vazio", "A correlação precisa de pelo menos dois ativos com histórico."));
    return;
  }

  const tabela = el("table");
  const cabeca = el("tr");
  cabeca.append(el("th", null, ""));
  correlacao.tickers.forEach((t) => cabeca.append(el("th", null, t)));
  tabela.append(cabeca);

  correlacao.matriz.forEach((linha, i) => {
    const tr = el("tr");
    tr.append(el("th", null, correlacao.tickers[i]));
    linha.forEach((v) => {
      const td = el("td", null, v.toFixed(2));
      // Verde = move junto, magenta = move ao contrário. A intensidade é o
      // módulo: correlação -0,8 diversifica tanto quanto +0,8 concentra.
      const cor = v >= 0 ? "52,211,153" : "255,79,163";
      td.style.background = `rgba(${cor},${Math.min(Math.abs(v), 1) * 0.32 + 0.04})`;
      td.style.color = Math.abs(v) > 0.55 ? "#fff" : "#a9a4c2";
      tr.append(td);
    });
    tabela.append(tr);
  });
  alvo.append(tabela);
}

/* ═══ Fronteira eficiente ═══ */

$("#btn-otimizar").addEventListener("click", otimizar);

async function otimizar() {
  carregado.fronteira = true;
  const botao = $("#btn-otimizar");
  botao.disabled = true;
  botao.textContent = "Calculando…";
  try {
    const r = await api(comCarteira("/portfolio/optimize"), {
      method: "POST",
      body: JSON.stringify({ peso_maximo: Number($("#peso-maximo").value), pontos: 40 }),
    });
    ultimaOtimizacao = r;
    desenharFronteira(r);
    renderCarteiras(r);
    $("#aviso-modelo").textContent = r.aviso;
    // Recalcular a fronteira invalida o plano anterior: os pesos mudaram.
    $("#reb-resultado").hidden = true;
    $("#proj-resultado").hidden = true;
    preencherRetornoEsperado();
  } finally {
    botao.disabled = false;
    botao.textContent = "Recalcular";
  }
}

function ponto(carteira, cor, rotulo) {
  return {
    label: rotulo,
    data: [{ x: carteira.volatilidade * 100, y: carteira.retorno_esperado * 100 }],
    backgroundColor: cor, borderColor: "#0c0b16", borderWidth: 2,
    pointRadius: 8, pointHoverRadius: 10, showLine: false,
  };
}

function desenharFronteira(r) {
  const canvas = $("#g-fronteira");
  const aviso = $("#fronteira-vazia");
  graficos.fronteira?.destroy();

  if (!r.fronteira.length) {
    // Gráfico em branco sem explicação é o pior tipo de erro: parece defeito do
    // sistema. A API diz o motivo; a tela mostra o motivo.
    canvas.hidden = true;
    aviso.textContent =
      r.motivo || "Não há dados suficientes para calcular a fronteira.";
    aviso.hidden = false;
    return;
  }

  canvas.hidden = false;
  aviso.hidden = true;

  const conjuntos = [
    {
      label: "Fronteira eficiente",
      data: r.fronteira.map((c) => ({ x: c.volatilidade * 100, y: c.retorno_esperado * 100 })),
      borderColor: "#7b5cff", backgroundColor: "rgba(123,92,255,.12)",
      borderWidth: 2.6, fill: false, tension: .3, pointRadius: 0, showLine: true,
    },
  ];
  if (r.minima_variancia) conjuntos.push(ponto(r.minima_variancia, "#35d6e8", "Mínima variância"));
  if (r.maximo_sharpe) conjuntos.push(ponto(r.maximo_sharpe, "#ff4fa3", "Máximo Sharpe"));
  if (r.carteira_atual) conjuntos.push(ponto(r.carteira_atual, "#f5b54a", "Sua carteira"));

  graficos.fronteira = new Chart(canvas, {
    type: "scatter",
    data: { datasets: conjuntos },
    options: {
      ...base(),
      interaction: { mode: "nearest", intersect: true },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#16142699", borderColor: "rgba(255,255,255,.14)", borderWidth: 1,
          padding: 11, cornerRadius: 10, titleColor: "#e9e7f3", bodyColor: "#a9a4c2",
          callbacks: {
            label: (c) => `${c.dataset.label}: risco ${c.parsed.x.toFixed(1)}% · retorno ${c.parsed.y.toFixed(1)}%`,
          },
        },
      },
      scales: {
        x: {
          grid: { color: GRADE, drawTicks: false }, border: { display: false },
          ticks: { color: EIXO, callback: (v) => `${v.toFixed(0)}%` },
          title: { display: true, text: "Risco — volatilidade anualizada", color: EIXO, font: { size: 11 } },
        },
        y: {
          grid: { color: GRADE, drawTicks: false }, border: { display: false },
          ticks: { color: EIXO, callback: (v) => `${v.toFixed(0)}%` },
          title: { display: true, text: "Retorno esperado anualizado", color: EIXO, font: { size: 11 } },
        },
      },
    },
  });
}

function renderCarteiras(r) {
  const alvo = $("#carteiras-sugeridas");
  limpar(alvo);

  const cartoes = [
    ["Mínima variância", "#35d6e8", r.minima_variancia, "Menor risco possível. Não usa retorno esperado — só covariância, que é bem mais estável."],
    ["Máximo Sharpe", "#ff4fa3", r.maximo_sharpe, "Melhor retorno por unidade de risco, acima da taxa livre de risco."],
    ["Sua carteira hoje", "#f5b54a", r.carteira_atual, "Pesos atuais pelo custo, avaliados com os mesmos parâmetros."],
  ];

  for (const [nome, cor, carteira, explicacao] of cartoes) {
    if (!carteira) continue;
    const cartao = el("article", "cartao");

    const cab = el("div", "carteira-cab");
    const i = el("i");
    i.style.background = cor;
    cab.append(i, el("h3", null, nome));
    cartao.append(cab);

    const metricas = el("div", "metricas");
    for (const [rot, val] of [
      ["Retorno", pct(carteira.retorno_esperado, 1)],
      ["Risco", pct(carteira.volatilidade, 1).replace("+", "")],
      ["Sharpe", carteira.indice_sharpe === null ? "—" : carteira.indice_sharpe.toFixed(2)],
    ]) {
      const m = el("div", "metrica");
      m.append(el("span", null, rot), el("strong", null, val));
      metricas.append(m);
    }
    cartao.append(metricas);

    const barras = el("div", "barra");
    const pesos = Object.entries(carteira.pesos).filter(([, p]) => p > 0.001).sort((a, b) => b[1] - a[1]);
    for (const [ticker, peso] of pesos) {
      const item = el("div", "barra-item");
      item.append(el("span", null, ticker));
      const trilho = el("div", "barra-trilho");
      const preenche = el("div", "barra-preenche");
      preenche.style.width = `${peso * 100}%`;
      preenche.style.background = cor;
      trilho.append(preenche);
      item.append(trilho, el("em", null, pct(peso, 1).replace("+", "")));
      barras.append(item);
    }
    cartao.append(barras);
    cartao.append(el("p", "sub", explicacao));
    alvo.append(cartao);
  }
}

/* ═══ Rebalanceamento ═══ */

/** "1.234,56" -> "1234.56". A API fala ponto decimal; a pessoa digita vírgula. */
function paraNumeroDaApi(texto) {
  const limpo = texto.trim().replace(/\./g, "").replace(",", ".");
  return /^\d+(\.\d{1,2})?$/.test(limpo) ? limpo : null;
}

// O campo de aporte só faz sentido nos modos que usam dinheiro novo. No modo
// completo sem aporte ele fica visível mas opcional -- vender e comprar já
// financia o rebalanceamento sozinho.
$("#reb-modo").addEventListener("change", () => {
  const completo = $("#reb-modo").value === "completo";
  $("#campo-aporte").querySelector("span").textContent = completo
    ? "Aporte (opcional)"
    : "Aporte";
});

$("#btn-rebalancear").addEventListener("click", async () => {
  const aviso = $("#reb-aviso");
  aviso.hidden = true;

  if (!ultimaOtimizacao) {
    aviso.textContent = "Calcule a fronteira primeiro — o plano usa os pesos dela.";
    aviso.hidden = false;
    return;
  }

  const alvo = ultimaOtimizacao[$("#reb-alvo").value];
  if (!alvo) {
    aviso.textContent =
      "Esta carteira-alvo não foi calculada. Recalcule a fronteira ou escolha a outra.";
    aviso.hidden = false;
    return;
  }

  const completo = $("#reb-modo").value === "completo";
  const bruto = $("#reb-aporte").value.trim();
  const aporte = bruto === "" ? "0" : paraNumeroDaApi(bruto);
  if (aporte === null) {
    aviso.textContent = "Valor do aporte inválido. Use algo como 1.000,00.";
    aviso.hidden = false;
    $("#reb-aporte").focus();
    return;
  }
  if (!completo && Number(aporte) <= 0) {
    aviso.textContent =
      "Sem aporte e sem vender não há o que rebalancear. Informe um valor ou troque o modo.";
    aviso.hidden = false;
    $("#reb-aporte").focus();
    return;
  }

  const botao = $("#btn-rebalancear");
  botao.disabled = true;
  botao.textContent = "Calculando…";
  try {
    const plano = await api(comCarteira("/portfolio/rebalance"), {
      method: "POST",
      body: JSON.stringify({
        pesos: alvo.pesos,
        aporte,
        permitir_venda: completo,
      }),
    });
    renderPlano(plano);
  } catch (e) {
    aviso.textContent = e.message;
    aviso.hidden = false;
    $("#reb-resultado").hidden = true;
  } finally {
    botao.disabled = false;
    botao.textContent = "Calcular plano";
  }
});

function renderPlano(plano) {
  $("#reb-resultado").hidden = false;

  const resumo = $("#reb-resumo");
  limpar(resumo);
  for (const [classe, valor, rotulo] of [
    ["venda", plano.total_vendas, "a vender"],
    ["compra", plano.total_compras, "a comprar"],
    ["", plano.sobra, "sobra em caixa"],
  ]) {
    const bloco = el("div", classe);
    bloco.append(el("b", null, brl.format(valor)), el("span", null, rotulo));
    resumo.append(bloco);
  }

  const corpo = $("#tabela-ordens tbody");
  limpar(corpo);
  if (!plano.ordens.length) {
    const tr = el("tr");
    const td = el("td", "vazio", "Nenhuma ordem: a carteira já está no alvo, ou o valor não compra uma ação inteira.");
    td.colSpan = 5;
    tr.append(td);
    corpo.append(tr);
  }
  for (const o of plano.ordens) {
    const tr = el("tr");
    const td = el("td");
    const pilula = el("span", "pilula-ordem", o.side === "compra" ? "Comprar" : "Vender");
    pilula.dataset.side = o.side;
    td.append(pilula);
    tr.append(td);
    tr.append(el("td", null, o.ticker));
    tr.append(el("td", "num", num(o.quantidade)));
    tr.append(el("td", "num", brl.format(o.preco)));
    tr.append(el("td", "num", brl.format(o.valor)));
    corpo.append(tr);
  }

  const desvios = $("#tabela-desvios tbody");
  limpar(desvios);
  // Do mais distante para o mais próximo: quem está mais fora do lugar é o
  // que interessa, e ordenar por ticker esconderia isso no meio da lista.
  const ordenados = [...plano.desvios].sort(
    (a, b) => Math.abs(Number(b.diferenca)) - Math.abs(Number(a.diferenca))
  );
  for (const d of ordenados) {
    const tr = el("tr");
    tr.append(el("td", null, d.ticker));
    // Peso é proporção, não variação: "+35,0%" sugeriria um ganho de 35%.
    tr.append(el("td", "num", proporcao(d.peso_atual)));
    tr.append(el("td", "num", proporcao(d.peso_alvo)));
    // Desvio NÃO usa verde e vermelho.
    //
    // `sinal()` pinta lucro de verde e prejuízo de vermelho, e a primeira
    // versão desta tabela reusou isso -- fazendo ITUB4, 34,8 pontos ACIMA do
    // alvo, aparecer em verde como se estivesse indo bem. Estar acima é tão
    // fora do lugar quanto estar abaixo; o que importa é a distância, e o
    // sinal já diz a direção.
    const td = el("td", "num desvio", `${pct(Number(d.diferenca), 1)} p.p.`);
    if (Math.abs(Number(d.diferenca)) >= 0.1) td.classList.add("desvio--grande");
    tr.append(td);
    tr.append(el("td", "num", brl.format(d.valor_atual)));
    desvios.append(tr);
  }

  const aviso = $("#reb-aviso");
  // Omitir em silêncio seria pior: a pessoa acharia que o plano cobre a
  // carteira inteira, e ela não cobre.
  mostrarSe(
    aviso,
    plano.sem_preco.length &&
      `Fora do plano por falta de cotação: ${plano.sem_preco.join(", ")}. ` +
        "Sem preço não dá para saber quantas ações comprar."
  );
}

/* ═══ Projeção (Monte Carlo) ═══ */

// Ao trocar a carteira-alvo, o campo de retorno se preenche com o número dela
// -- mas continua EDITÁVEL. Isso importa: o retorno esperado vem de estimativa
// histórica, e uma carteira que subiu numa janela curta produz uma expectativa
// alta demais para projetar dez anos. Deixar o campo travado transformaria uma
// premissa discutível numa promessa.
function preencherRetornoEsperado() {
  const alvo = ultimaOtimizacao?.[$("#proj-carteira").value];
  if (alvo) $("#proj-retorno").value = proporcao(alvo.retorno_esperado);
}
$("#proj-carteira").addEventListener("change", preencherRetornoEsperado);

/** "12,5%" ou "12,5" -> 0.125. Devolve null se não for um número. */
function percentualParaFracao(texto) {
  const limpo = texto.trim().replace("%", "").replace(/\./g, "").replace(",", ".");
  if (!/^-?\d+(\.\d+)?$/.test(limpo)) return null;
  return Number(limpo) / 100;
}

$("#btn-projetar").addEventListener("click", async () => {
  const aviso = $("#proj-aviso");
  aviso.hidden = true;

  if (!ultimaOtimizacao) {
    aviso.textContent = "Calcule a fronteira primeiro — a projeção usa a volatilidade dela.";
    aviso.hidden = false;
    return;
  }
  const alvo = ultimaOtimizacao[$("#proj-carteira").value];
  if (!alvo) {
    aviso.textContent = "Esta carteira não foi calculada. Recalcule a fronteira.";
    aviso.hidden = false;
    return;
  }

  if (!$("#proj-retorno").value.trim()) preencherRetornoEsperado();
  const retorno = percentualParaFracao($("#proj-retorno").value);
  if (retorno === null || retorno <= -1) {
    aviso.textContent = "Retorno esperado inválido. Use algo como 10,5%.";
    aviso.hidden = false;
    $("#proj-retorno").focus();
    return;
  }

  const bruto = $("#proj-aporte").value.trim();
  const aporte = bruto === "" ? "0" : paraNumeroDaApi(bruto);
  if (aporte === null) {
    aviso.textContent = "Aporte inválido. Use algo como 500,00.";
    aviso.hidden = false;
    $("#proj-aporte").focus();
    return;
  }

  const botao = $("#btn-projetar");
  botao.disabled = true;
  botao.textContent = "Simulando…";
  try {
    const r = await api(comCarteira("/portfolio/simulate"), {
      method: "POST",
      body: JSON.stringify({
        retorno_esperado: retorno,
        volatilidade: alvo.volatilidade,
        anos: Number($("#proj-anos").value),
        aporte_mensal: aporte,
      }),
    });
    renderProjecao(r, alvo.volatilidade);
  } catch (e) {
    aviso.textContent = e.message;
    aviso.hidden = false;
    $("#proj-resultado").hidden = true;
  } finally {
    botao.disabled = false;
    botao.textContent = "Simular";
  }
});

function renderProjecao(r, volatilidade) {
  $("#proj-resultado").hidden = false;

  const ctx = $("#g-projecao").getContext("2d");
  graficos.projecao?.destroy();

  const anos = (p) => (p.mes / 12).toFixed(1);
  // Um rótulo por ANO, não por mês: 240 rótulos num eixo de 20 anos viram
  // uma tarja preta.
  const rotulos = r.pontos.map((p) => (p.mes % 12 === 0 ? `${p.mes / 12}a` : ""));

  const faixa = (chave, cor, preenche) => ({
    label: chave,
    data: r.pontos.map((p) => Number(p[chave])),
    borderColor: cor,
    backgroundColor: cor,
    borderWidth: chave === "p50" ? 2.4 : 0,
    fill: preenche,
    tension: .2, pointRadius: 0, pointHoverRadius: 4,
  });

  graficos.projecao = new Chart(ctx, {
    type: "line",
    data: {
      labels: rotulos,
      // A ordem importa: cada faixa se preenche até a ANTERIOR (`fill: "-1"`),
      // então as bandas saem empilhadas de baixo para cima.
      datasets: [
        faixa("p5", "rgba(123,92,255,.18)", false),
        faixa("p25", "rgba(123,92,255,.18)", "-1"),
        faixa("p50", "#7b5cff", "-1"),
        faixa("p75", "rgba(123,92,255,.45)", "-1"),
        faixa("p95", "rgba(123,92,255,.18)", "-1"),
      ],
    },
    options: {
      ...base({ ticks: { color: EIXO, callback: (v) => brl.format(v).replace(/\s/g, "") } }),
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "#16142699", borderColor: "rgba(255,255,255,.14)", borderWidth: 1,
          padding: 11, cornerRadius: 10, titleColor: "#e9e7f3", bodyColor: "#a9a4c2",
          callbacks: {
            title: (itens) => `Ano ${anos(r.pontos[itens[0].dataIndex])}`,
            label: (c) => `${c.dataset.label}: ${brl.format(c.parsed.y)}`,
          },
        },
      },
    },
  });

  const final = r.pontos[r.pontos.length - 1];
  const resumo = $("#proj-resumo");
  limpar(resumo);
  for (const [valor, rotulo] of [
    [r.total_aportado, "você terá colocado"],
    [final.p50, "mediana dos cenários"],
    [final.p5, "5% ficam abaixo de"],
  ]) {
    const bloco = el("div");
    bloco.append(el("b", null, brl.format(valor)), el("span", null, rotulo));
    resumo.append(bloco);
  }

  // A frase é a entrega: o gráfico mostra a faixa, ela diz o que significa.
  const frase = $("#proj-frase");
  limpar(frase);
  frase.append(document.createTextNode("Em metade dos cenários você passa de "));
  frase.append(el("b", null, brl.format(final.p50)));
  frase.append(document.createTextNode(". Em 5% deles, fica abaixo de "));
  frase.append(el("b", null, brl.format(final.p5)));
  frase.append(document.createTextNode(
    `, tendo colocado ${brl.format(r.total_aportado)}. ` +
    `A chance de terminar acima do que você colocou é de ` +
    `${(r.prob_acima_do_aportado * 100).toFixed(1)}%.`
  ));

  $("#proj-ressalva").textContent =
    `${r.ressalva} Volatilidade usada: ${proporcao(volatilidade)} ao ano, ` +
    `de ${r.cenarios.toLocaleString("pt-BR")} cenários.`;
}

/* ═══ Transações ═══ */

async function carregarTransacoes() {
  carregado.transacoes = true;
  const pagina = await api(comCarteira("/transactions?limit=100"));
  const corpo = $("#tabela-operacoes tbody");
  limpar(corpo);

  if (!pagina.items.length) {
    const tr = el("tr");
    const td = el("td", "vazio", "Nenhuma operação registrada.");
    td.colSpan = 7;
    tr.append(td);
    corpo.append(tr);
    return;
  }

  for (const t of pagina.items) {
    const tr = el("tr");
    tr.append(el("td", null, dataBR(t.traded_at)));
    tr.append(el("td", null, t.ticker));
    const lado = el("td");
    lado.append(el("span", `pilula pilula--${t.side}`, t.side === "compra" ? "Compra" : "Venda"));
    tr.append(lado);
    tr.append(el("td", "num", num(t.quantity)));
    tr.append(el("td", "num", brl.format(t.price)));
    tr.append(el("td", "num", brl.format(t.fees)));

    const acao = el("td", "num");
    const apagar = el("button", "icone-lixeira", "✕");
    apagar.title = "Remover operação";
    apagar.addEventListener("click", async () => {
      if (!confirm(`Remover a ${t.side} de ${num(t.quantity)} ${t.ticker}?`)) return;
      try {
        await api(comCarteira(`/transactions/${t.id}`), { method: "DELETE" });
        invalidar();
        await carregarTransacoes();
      } catch (e) {
        alert(e.message);
      }
    });
    acao.append(apagar);
    tr.append(acao);
    corpo.append(tr);
  }
}

function invalidar() {
  carregado.posicoes = carregado.fronteira = carregado.transacoes = false;
  carregarVisao().catch(() => {});
}

$("#btn-nova").addEventListener("click", () => {
  const form = $("#form-op");
  form.hidden = !form.hidden;
  if (!form.hidden) {
    $("#op-data").value = isoParaBR(hojeISO());
    $("#op-ticker").focus();
  }
});
$("#op-cancelar").addEventListener("click", () => { $("#form-op").hidden = true; });

/* O campo de data: texto mascarado na frente, calendario nativo atras.
   O nativo existe so como seletor -- quem guarda o valor e o campo de texto. */
aplicarMascaraData($("#op-data"));
$("#op-data-nativo").max = hojeISO();
$("#op-data-calendario").addEventListener("click", () => {
  const nativo = $("#op-data-nativo");
  nativo.value = brParaISO($("#op-data").value) || hojeISO();
  try {
    nativo.showPicker();
  } catch {
    // showPicker() e recente e exige gesto do usuario. Se falhar, o campo de
    // texto continua funcionando -- o calendario e conveniencia, nao caminho
    // unico. Falhar aqui nao pode travar o registro da operacao.
  }
});
$("#op-data-nativo").addEventListener("change", (ev) => {
  if (ev.target.value) $("#op-data").value = isoParaBR(ev.target.value);
});

$("#form-op").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const erro = $("#op-erro");
  erro.hidden = true;

  const traded_at = brParaISO($("#op-data").value);
  if (!traded_at) {
    erro.textContent = "Data invalida. Use o formato dd/mm/aaaa.";
    erro.hidden = false;
    $("#op-data").focus();
    return;
  }
  if (traded_at > hojeISO()) {
    // Comparacao de texto funciona aqui: em ISO, ordem alfabetica e ordem
    // cronologica. E o mesmo limite que o backend aplica -- checar aqui so
    // troca um 422 seco por uma frase que explica.
    erro.textContent = "A data da operacao nao pode estar no futuro.";
    erro.hidden = false;
    $("#op-data").focus();
    return;
  }

  try {
    await api(comCarteira("/transactions"), {
      method: "POST",
      body: JSON.stringify({
        ticker: $("#op-ticker").value.trim().toUpperCase(),
        side: $("#op-lado").value,
        quantity: $("#op-qtd").value,
        price: $("#op-preco").value,
        fees: $("#op-taxas").value || "0",
        traded_at,
      }),
    });
    $("#form-op").reset();
    $("#form-op").hidden = true;
    invalidar();
    await carregarTransacoes();
  } catch (e) {
    erro.textContent = e.message;
    erro.hidden = false;
  }
});

/* ═══ Busca de ativos ═══ */

let buscaTimer;
$("#busca").addEventListener("input", (ev) => {
  clearTimeout(buscaTimer);
  const termo = ev.target.value.trim();
  const caixa = $("#busca-resultados");
  if (termo.length < 2) { caixa.hidden = true; return; }

  // Debounce: sem ele, digitar "PETR4" dispara 5 requisições, das quais 4 são
  // descartadas. Com 250 ms, dispara uma.
  buscaTimer = setTimeout(async () => {
    try {
      const r = await api(`/assets?busca=${encodeURIComponent(termo)}&limit=8`);
      limpar(caixa);
      if (!r.items.length) { caixa.hidden = true; return; }
      for (const a of r.items) {
        const b = el("button");
        b.append(el("b", null, a.ticker), el("span", null, a.nome || a.tipo));
        b.addEventListener("click", () => {
          document.querySelector('.nav-item[data-vista="transacoes"]').click();
          $("#form-op").hidden = false;
          $("#op-ticker").value = a.ticker;
          $("#op-data").value = isoParaBR(hojeISO());
          $("#op-qtd").focus();
          caixa.hidden = true;
          ev.target.value = "";
        });
        caixa.append(b);
      }
      caixa.hidden = false;
    } catch { caixa.hidden = true; }
  }, 250);
});
document.addEventListener("click", (ev) => {
  if (!ev.target.closest(".busca")) $("#busca-resultados").hidden = true;
});

/* ═══ Início ═══ */

// Retoma a sessão pelo cookie httpOnly: se ele existir e for válido, o usuário
// entra direto. É o que torna possível não guardar nada em localStorage.
renovar()
  .then((ok) => (ok ? entrarNoApp() : mostrarLogin()))
  .catch(mostrarLogin);


/* ═══ Troca de carteira ═══ */

$("#carteira").addEventListener("change", async (ev) => {
  carteiraAtiva = ev.target.value;
  invalidar();
  await carregarCarteiras();
});

$("#btn-zerar-transacoes").addEventListener("click", async () => {
  const atual = carteiras.find((c) => c.id === carteiraAtiva);
  if (!atual || atual.tipo !== "simulada") return;

  // Mesma logica do "apagar carteira": a confirmação diz quantas operações
  // vão sumir, em vez de um "tem certeza?" genérico que se clica no reflexo.
  let detalhe = "";
  try {
    const ops = await api(`/transactions?portfolio_id=${atual.id}&limit=1`);
    const n = ops.total;
    detalhe = `\n\nVocê perde ${n} operação${n === 1 ? "" : "ões"} e todo o histórico do gráfico.`;
  } catch {
    detalhe = "\n\nIsso apaga todas as operações e todo o histórico dela.";
  }

  if (!confirm(`Zerar as transações de "${atual.nome}"?${detalhe}\n\nNão dá para desfazer.`)) return;

  try {
    await api(comCarteira("/transactions"), { method: "DELETE" });
    invalidar();
    await carregarTransacoes();
  } catch (e) {
    alert(e.message);
  }
});

$("#btn-apagar-carteira").addEventListener("click", async () => {
  const atual = carteiras.find((c) => c.id === carteiraAtiva);
  if (!atual || atual.tipo !== "simulada") return;

  // A confirmação diz o que vai sumir, com números.
  //
  // "Tem certeza?" genérico não informa nada: a pessoa clica em OK por reflexo.
  // Aqui ela lê quantas operações e quantos dias de histórico está destruindo
  // antes de decidir -- e a exclusão é definitiva, sem lixeira nem desfazer.
  let detalhe = "";
  try {
    const [ops, snaps] = await Promise.all([
      api(`/transactions?portfolio_id=${atual.id}&limit=1`),
      api(`/portfolio/snapshots?portfolio_id=${atual.id}&limit=500`),
    ]);
    const n = ops.total;
    const d = snaps.length;
    detalhe =
      `\n\nVocê perde ${n} operação${n === 1 ? "" : "ões"} e ${d} dia${d === 1 ? "" : "s"} ` +
      "de histórico.";
  } catch {
    // Falhar ao contar não pode impedir a exclusão -- só deixa o aviso
    // genérico. O que não pode é apagar sem perguntar.
    detalhe = "\n\nIsso apaga todas as operações e todo o histórico dela.";
  }

  if (!confirm(`Apagar a carteira "${atual.nome}"?${detalhe}\n\nNão dá para desfazer.`)) return;

  try {
    await api(`/portfolios/${atual.id}`, { method: "DELETE" });
    carteiraAtiva = null;      // força cair na primeira da lista (a real)
    await carregarCarteiras();
    invalidar();
    await carregarTransacoes();
  } catch (e) {
    alert(e.message);
  }
});

$("#btn-nova-carteira").addEventListener("click", async () => {
  const nome = prompt("Nome da carteira simulada:");
  if (!nome?.trim()) return;
  try {
    const nova = await api("/portfolios", {
      method: "POST",
      body: JSON.stringify({ nome: nome.trim(), tipo: "simulada" }),
    });
    carteiraAtiva = nova.id;
    invalidar();
    await carregarCarteiras();
  } catch (e) {
    alert(e.message);
  }
});
