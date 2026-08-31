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
const dataBR = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString("pt-BR");
const sinal = (v) => (Number(v) >= 0 ? "pos" : "neg");

function el(tag, classe, texto) {
  const e = document.createElement(tag);
  if (classe) e.className = classe;
  if (texto !== undefined) e.textContent = texto;   // textContent, nunca innerHTML
  return e;
}
const $ = (s) => document.querySelector(s);
function limpar(node) { while (node.firstChild) node.removeChild(node.firstChild); }

/* ═══ Login ═══ */

function mostrarLogin() {
  $("#login").hidden = false;
  $("#app").hidden = true;
  token = null;
}

async function entrarNoApp() {
  // Carrega os dados ANTES de trocar de tela.
  //
  // A ordem inversa deixa um estado quebrado quando algo falha: a tela de login
  // já saiu, a aplicação aparece vazia, e a mensagem de erro é escrita num
  // elemento que não está mais visível. O usuário fica olhando uma página em
  // branco sem nenhuma explicação.
  const eu = await api("/auth/me");
  usuarioEmail = eu.email;
  $("#usuario-email").textContent = eu.email;
  $("#avatar").textContent = eu.email.slice(0, 2).toUpperCase();
  await carregarVisao();

  $("#login").hidden = true;
  $("#app").hidden = false;
}

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
  const [resumo, snapshots] = await Promise.all([
    api("/portfolio/summary"),
    api("/portfolio/snapshots?limit=250"),
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

  desenharEvolucao(snapshots.slice().reverse());
  renderPosicoesResumo(resumo.positions);
  renderOperacoesResumo(await api("/transactions?limit=5"));
}

function renderPosicoesResumo(posicoes) {
  const lista = $("#lista-posicoes");
  limpar(lista);
  if (!posicoes.length) { lista.append(el("p", "vazio", "Nenhuma posição aberta.")); return; }

  for (const p of posicoes.slice(0, 5)) {
    const linha = el("div", "linha");
    linha.append(el("div", "ficha", p.ticker.slice(0, 4)));

    const txt = el("div", "linha-txt");
    txt.append(el("strong", null, p.ticker));
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

function desenharEvolucao(pontos) {
  const canvas = $("#g-evolucao");
  const ctx = canvas.getContext("2d");
  graficos.evolucao?.destroy();

  if (!pontos.length) return;

  graficos.evolucao = new Chart(ctx, {
    type: "line",
    data: {
      labels: pontos.map((p) => dataBR(p.date)),
      datasets: [
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
      ],
    },
    options: {
      ...base({ ticks: { color: EIXO, callback: (v) => brl.format(v).replace(/\s/g, "") } }),
    },
  });

  const legenda = $("#legenda-evolucao");
  limpar(legenda);
  for (const [cor, rotulo] of [["#ff4fa3", "Valor de mercado"], ["#35d6e8", "Custo"]]) {
    const s = el("span", "lg");
    const i = el("i");
    i.style.background = cor;
    s.append(i, document.createTextNode(rotulo));
    legenda.append(s);
  }
}

/* ═══ Posições ═══ */

async function carregarPosicoes() {
  carregado.posicoes = true;
  const [resumo, metricas] = await Promise.all([
    api("/portfolio/summary"),
    api("/portfolio/metrics"),
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
    tr.append(el("td", null, p.ticker));
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
    const r = await api("/portfolio/optimize", {
      method: "POST",
      body: JSON.stringify({ peso_maximo: Number($("#peso-maximo").value), pontos: 40 }),
    });
    desenharFronteira(r);
    renderCarteiras(r);
    $("#aviso-modelo").textContent = r.aviso;
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
  graficos.fronteira?.destroy();

  if (!r.fronteira.length) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }

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
    ["Máximo Sharpe", "#ff4fa3", r.maximo_sharpe, "Melhor retorno por unidade de risco, acima do CDI."],
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

/* ═══ Transações ═══ */

async function carregarTransacoes() {
  carregado.transacoes = true;
  const pagina = await api("/transactions?limit=100");
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
        await api(`/transactions/${t.id}`, { method: "DELETE" });
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
    $("#op-data").value = new Date().toISOString().slice(0, 10);
    $("#op-ticker").focus();
  }
});
$("#op-cancelar").addEventListener("click", () => { $("#form-op").hidden = true; });

$("#form-op").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const erro = $("#op-erro");
  erro.hidden = true;
  try {
    await api("/transactions", {
      method: "POST",
      body: JSON.stringify({
        ticker: $("#op-ticker").value.trim().toUpperCase(),
        side: $("#op-lado").value,
        quantity: $("#op-qtd").value,
        price: $("#op-preco").value,
        fees: $("#op-taxas").value || "0",
        traded_at: $("#op-data").value,
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
          $("#op-data").value = new Date().toISOString().slice(0, 10);
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
