"""Testes da rota de otimizacao."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.asset import Asset, PriceHistory
from app.models.benchmark import BenchmarkRate, Indexador
from app.schemas.optimization import MAXIMO_VISOES
from tests.conftest import CDI_DIARIO_FAKE, BcbFake, IbovFake
from tests.factories import criar_ativo, op, segunda_conta, usuario_logado, variacao_do_ibov

INICIO = date(2026, 1, 1)


def _beta_da_semente(semente: int) -> float:
    """0,85; 1,10; 1,35... -- betas distintos e plausiveis para acao brasileira."""
    return 0.6 + 0.25 * semente


async def _serie(db: AsyncSession, ativo: Asset, dias: int = 200, semente: int = 1) -> list[float]:
    """Precos que andam com o Ibovespa sintetico: beta x mercado + ruido proprio.

    O retorno do dia d usa a variacao do Ibovespa do MESMO dia d, a mesma que
    o `IbovFake` devolve. Com isso o beta medido pela rota tem que voltar perto
    do beta de construcao -- e se o alinhamento por data escorregar um dia, o
    beta cai para perto de zero e o teste de beta denuncia.

    Devolve os precos como gravados (6 casas), para quem precisar recalcular
    retornos exatos.
    """
    rng = np.random.default_rng(semente)
    beta = _beta_da_semente(semente)
    datas = [INICIO + timedelta(days=i) for i in range(dias)]
    retornos = np.array([beta * variacao_do_ibov(d) for d in datas]) + rng.normal(
        0.0002, 0.010, dias
    )
    precos = np.cumprod(1 + retornos) * 100
    gravados: list[float] = []
    for dia, preco in zip(datas, precos, strict=True):
        fechamento = Decimal(str(round(float(preco), 6)))
        db.add(PriceHistory(asset_id=ativo.id, date=dia, close=fechamento))
        gravados.append(float(fechamento))
    await db.commit()
    return gravados


async def _carteira_com(client: AsyncClient, db: AsyncSession, *tickers: str) -> dict[str, str]:
    _, h = await usuario_logado(client)
    for i, t in enumerate(tickers):
        await _serie(db, await criar_ativo(db, ticker=t), semente=i + 1)
        await client.post("/transactions", json=op(ticker=t, quantity="10"), headers=h)
    return h


class TestProtecao:
    async def test_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.post("/portfolio/optimize", json={})).status_code == 401

    async def test_nao_usa_a_carteira_de_outro_usuario(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")
        outro = await segunda_conta(client)

        corpo = (await client.post("/portfolio/optimize", json={}, headers=outro)).json()
        assert corpo["tickers"] == []
        assert corpo["fronteira"] == []


class TestResultado:
    async def test_otimiza_a_carteira_do_usuario(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")

        corpo = (await client.post("/portfolio/optimize", json={"pontos": 10}, headers=h)).json()

        assert corpo["tickers"] == ["ITUB4", "PETR4", "VALE3"]
        assert len(corpo["fronteira"]) > 1
        assert corpo["minima_variancia"] is not None
        assert corpo["maximo_sharpe"] is not None

    async def test_pesos_somam_um_e_sao_nomeados(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O otimizador trabalha com indices; a API devolve nomes. Peso certo no
        ativo errado seria o pior bug possivel aqui -- os numeros pareceriam
        plausiveis e ninguem notaria."""
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")

        corpo = (await client.post("/portfolio/optimize", json={"pontos": 8}, headers=h)).json()
        pesos = corpo["minima_variancia"]["pesos"]

        assert set(pesos) == {"ITUB4", "PETR4", "VALE3"}
        assert sum(pesos.values()) == float(1) or abs(sum(pesos.values()) - 1) < 1e-6
        assert all(p >= 0 for p in pesos.values())

    async def test_respeita_o_limite_por_ativo(self, client: AsyncClient, db: AsyncSession) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4", "BBAS3")

        corpo = (
            await client.post(
                "/portfolio/optimize", json={"peso_maximo": 0.30, "pontos": 8}, headers=h
            )
        ).json()

        for carteira in (corpo["minima_variancia"], corpo["maximo_sharpe"], *corpo["fronteira"]):
            assert max(carteira["pesos"].values()) <= 0.30 + 1e-6

    async def test_devolve_a_carteira_atual_para_comparar(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """ "Voce esta aqui, a fronteira esta ali" -- a comparacao que interessa."""
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")

        corpo = (await client.post("/portfolio/optimize", json={"pontos": 8}, headers=h)).json()
        atual = corpo["carteira_atual"]

        assert atual is not None
        assert abs(sum(atual["pesos"].values()) - 1) < 1e-6

    async def test_carteira_atual_e_nula_para_ativos_avulsos(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Pedindo ativos que nao possui, nao ha carteira atual a comparar."""
        _, h = await usuario_logado(client)
        for i, t in enumerate(("PETR4", "VALE3", "ITUB4")):
            await _serie(db, await criar_ativo(db, ticker=t), semente=i + 1)

        corpo = (
            await client.post(
                "/portfolio/optimize",
                json={"tickers": ["PETR4", "VALE3", "ITUB4"], "pontos": 8},
                headers=h,
            )
        ).json()
        assert corpo["carteira_atual"] is None
        assert len(corpo["fronteira"]) > 1

    async def test_minima_variancia_tem_menos_risco_que_maximo_sharpe(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4", "BBAS3")

        corpo = (await client.post("/portfolio/optimize", json={"pontos": 8}, headers=h)).json()
        assert (
            corpo["minima_variancia"]["volatilidade"]
            <= corpo["maximo_sharpe"]["volatilidade"] + 1e-9
        )

    async def test_aviso_sobre_a_limitacao_vem_sempre(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Uma ferramenta que sugere alocacao de dinheiro tem que dizer no que
        ela se baseia -- e no que ela nao se baseia."""
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")
        corpo = (await client.post("/portfolio/optimize", json={"pontos": 6}, headers=h)).json()
        assert "não é recomendação de investimento" in corpo["aviso"]


class TestCasosDeBorda:
    async def test_menos_de_dois_ativos_devolve_vazio(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Com um ativo so, a resposta seria '100% nele': correta e inutil."""
        h = await _carteira_com(client, db, "PETR4")
        corpo = (await client.post("/portfolio/optimize", json={}, headers=h)).json()
        assert corpo["fronteira"] == []
        assert corpo["minima_variancia"] is None

    async def test_carteira_vazia_nao_quebra(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        corpo = (await client.post("/portfolio/optimize", json={}, headers=h)).json()
        assert corpo["tickers"] == []

    async def test_limite_impossivel_devolve_vazio_nao_500(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """2 ativos com teto de 20% somam no maximo 40%. A restricao veio do
        usuario, entao a resposta e vazia e explicita -- nunca um 500."""
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        resp = await client.post(
            "/portfolio/optimize", json={"peso_maximo": 0.20, "pontos": 6}, headers=h
        )
        assert resp.status_code == 200
        assert resp.json()["fronteira"] == []

    async def test_ativo_sem_historico_e_reportado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")
        await criar_ativo(db, ticker="XXXX3")
        await client.post("/transactions", json=op(ticker="XXXX3", quantity="1"), headers=h)

        corpo = (await client.post("/portfolio/optimize", json={"pontos": 6}, headers=h)).json()
        assert "XXXX3" in corpo["sem_historico_suficiente"]

    async def test_peso_maximo_fora_da_faixa_e_recusado(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        for valor in (0.01, 1.5, -1):
            resp = await client.post("/portfolio/optimize", json={"peso_maximo": valor}, headers=h)
            assert resp.status_code == 422, valor

    async def test_teto_de_ativos_por_pedido(self, client: AsyncClient) -> None:
        """A otimizacao roda um solver por ponto da fronteira: sem teto, um pedido
        com centenas de ativos prende o worker por minutos."""
        _, h = await usuario_logado(client)
        resp = await client.post(
            "/portfolio/optimize",
            json={"tickers": [f"AAA{i}" for i in range(40)]},
            headers=h,
        )
        assert resp.status_code == 422

    async def test_pontos_fora_da_faixa_e_recusado(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        assert (
            await client.post("/portfolio/optimize", json={"pontos": 5000}, headers=h)
        ).status_code == 422


class TestMotivoDaRespostaVazia:
    """Uma resposta 200 com listas vazias é ambígua.

    O cliente não sabe se o cálculo não encontrou nada, se faltou dado ou se a
    restrição era impossível — e acaba mostrando um gráfico em branco sem
    explicação, que é o pior tipo de erro porque parece defeito do sistema.
    Encontrado usando o frontend de verdade.
    """

    async def test_limite_impossivel_explica_e_sugere_o_ajuste(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = (
            await client.post(
                "/portfolio/optimize", json={"peso_maximo": 0.35, "pontos": 6}, headers=h
            )
        ).json()

        assert corpo["fronteira"] == []
        assert corpo["motivo"] is not None
        assert "70%" in corpo["motivo"]  # o que a restrição de fato permite
        assert "50%" in corpo["motivo"]  # o mínimo que resolveria

    async def test_menos_de_dois_ativos_explica(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        h = await _carteira_com(client, db, "PETR4")
        corpo = (await client.post("/portfolio/optimize", json={}, headers=h)).json()
        assert "dois ativos" in corpo["motivo"]

    async def test_sem_historico_explica(self, client: AsyncClient, db: AsyncSession) -> None:
        _, h = await usuario_logado(client)
        for t in ("PETR4", "VALE3"):
            await criar_ativo(db, ticker=t)  # sem série de preços
            await client.post("/transactions", json=op(ticker=t, quantity="10"), headers=h)

        # peso_maximo=1.0 para NAO cair na checagem de restricao, que vem antes:
        # com 2 ativos e o padrao de 40%, o motivo seria o limite, nao o historico.
        corpo = (
            await client.post("/portfolio/optimize", json={"peso_maximo": 1.0}, headers=h)
        ).json()
        assert corpo["motivo"] is not None
        assert "historico" in corpo["motivo"].lower()

    async def test_sucesso_nao_traz_motivo(self, client: AsyncClient, db: AsyncSession) -> None:
        """`motivo` só existe para explicar ausência de resultado."""
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")
        corpo = (await client.post("/portfolio/optimize", json={"pontos": 6}, headers=h)).json()
        assert corpo["fronteira"] != []
        assert corpo["motivo"] is None


class TestBlackLitterman:
    """A rota estima retorno por Black-Litterman, com o Ibovespa como mercado.

    O que precisa ficar visivel na resposta: de onde veio o prior (CDI, beta e
    premio), e o que aconteceu com as opinioes que o usuario mandou. Um modelo
    de retorno esperado devolve numeros plausiveis de qualquer jeito -- a
    versao anterior devolvia 25% a 64% ao ano para acoes brasileiras --, entao
    aqui ha testes de identidade E testes de plausibilidade.
    """

    async def _otimizar(
        self, client: AsyncClient, h: dict[str, str], **corpo: object
    ) -> dict[str, object]:
        pedido = {"peso_maximo": 1.0} | corpo
        resposta: dict[str, object] = (
            await client.post("/portfolio/optimize", json=pedido, headers=h)
        ).json()
        return resposta

    async def test_equilibrio_e_cdi_mais_beta_vezes_premio_de_5(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A conta inteira do prior, com os numeros que a propria resposta traz."""
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = await self._otimizar(client, h)

        eq = corpo["equilibrio"]
        assert isinstance(eq, dict)
        rf = corpo["taxa_livre_risco"]
        assert isinstance(rf, float)
        assert eq["referencia"] == "Ibovespa"
        assert eq["premio_de_risco"] == 0.05
        assert set(eq["betas"]) == {"PETR4", "VALE3"}
        for ticker, beta in eq["betas"].items():
            assert eq["retorno_de_equilibrio"][ticker] == pytest.approx(rf + beta * 0.05)
        assert eq["aversao_ao_risco"] == pytest.approx(0.05 / eq["volatilidade_mercado"] ** 2)

    async def test_o_beta_medido_e_o_da_construcao_da_serie(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Prova o alinhamento por data de ponta a ponta.

        As series foram feitas com beta 0,85 e 1,10 contra o Ibovespa do mesmo
        dia. Se precos e indice escorregassem um dia, o beta medido cairia para
        perto de zero. A folga de 0,2 e mais de tres erros-padrao.
        """
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        eq = (await self._otimizar(client, h))["equilibrio"]

        assert isinstance(eq, dict)
        assert eq["betas"]["PETR4"] == pytest.approx(_beta_da_semente(1), abs=0.2)
        assert eq["betas"]["VALE3"] == pytest.approx(_beta_da_semente(2), abs=0.2)

    async def test_retorno_de_equilibrio_fica_entre_o_cdi_e_cdi_mais_15(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O teste de plausibilidade que teria pego a versao anterior.

        Acao brasileira comum (beta entre 0,5 e 2) com premio de 5% tem que
        esperar entre o CDI e CDI + 15% ao ano. A versao antiga devolvia ate
        CDI + 54% com precos reais.
        """
        h = await _carteira_com(client, db, "PETR4", "VALE3", "ITUB4")

        corpo = await self._otimizar(client, h)

        rf = corpo["taxa_livre_risco"]
        eq = corpo["equilibrio"]
        assert isinstance(rf, float)
        assert isinstance(eq, dict)
        for ticker, retorno in eq["retorno_de_equilibrio"].items():
            assert rf < retorno < rf + 0.15, f"{ticker}: {retorno:.1%}"

    async def test_o_retorno_de_um_ativo_nao_depende_do_resto_da_carteira(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A regressao principal.

        Antes o "mercado" era a propria carteira, pesada por valor de mercado:
        ITUB4 saia 16% ao lado de WEGE3 e 52% ao lado de VALE3 e PETR4, e PETR3
        mais PETR4 contavam a Petrobras duas vezes. Agora o valor de mercado nem
        entra, e o equilibrio de ITUB4 e o mesmo nos dois pedidos.
        """
        _, h = await usuario_logado(client)
        for i, t in enumerate(("ITUB4", "WEGE3", "VALE3", "PETR4", "PETR3")):
            capitalizacao = Decimal("700e9") if t.startswith("PETR") else None
            await _serie(
                db, await criar_ativo(db, ticker=t, market_cap=capitalizacao), semente=i + 1
            )

        a = await self._otimizar(client, h, tickers=["ITUB4", "WEGE3"])
        b = await self._otimizar(client, h, tickers=["ITUB4", "VALE3", "PETR4", "PETR3"])

        assert isinstance(a["equilibrio"], dict)
        assert isinstance(b["equilibrio"], dict)
        assert a["equilibrio"]["retorno_de_equilibrio"]["ITUB4"] == pytest.approx(
            b["equilibrio"]["retorno_de_equilibrio"]["ITUB4"], abs=1e-12
        )

    async def test_sem_opiniao_a_carteira_otima_e_a_que_imita_o_ibovespa(
        self, client: AsyncClient, db: AsyncSession, ibov: IbovFake
    ) -> None:
        """A identidade que define o modelo, e o teste mais forte do arquivo.

        Aqui o Ibovespa E uma carteira 50/30/20 dos tres ativos, todo dia. Sem
        opiniao, mu - rf = delta*cov(r, r_M) = delta*Sigma*w, e o maximo Sharpe
        resolve w* ~ Sigma^-1 (mu - rf) = delta * w: normalizado, o proprio w.
        Vale tambem com a covariancia posterior, que sem opiniao e (1+tau)*Sigma.

        Uma assercao cobre a cadeia inteira: Ibovespa no banco -> alinhamento
        -> cov com o indice -> delta -> pi -> mu -> otimizador. Errar data,
        ordem dos tickers, transposicao, sinal ou escala em qualquer elo quebra
        isto. Teto folgado (1.0) porque a restricao, se apertar, e quem manda.
        """
        _, h = await usuario_logado(client)
        pesos = {"ITUB4": 0.5, "PETR4": 0.3, "VALE3": 0.2}
        precos: dict[str, np.ndarray] = {}
        for i, t in enumerate(pesos):
            precos[t] = np.array(await _serie(db, await criar_ativo(db, ticker=t), semente=i + 1))
            await client.post("/transactions", json=op(ticker=t, quantity="10"), headers=h)

        retornos = {t: p[1:] / p[:-1] - 1 for t, p in precos.items()}
        datas = [INICIO + timedelta(days=i) for i in range(len(precos["ITUB4"]))]
        db.add(BenchmarkRate(indexador=Indexador.IBOV, date=datas[0], rate=Decimal(0)))
        for k, dia in enumerate(datas[1:]):
            r = sum(w * retornos[t][k] for t, w in pesos.items())
            db.add(BenchmarkRate(indexador=Indexador.IBOV, date=dia, rate=Decimal(f"{r:.10f}")))
        await db.commit()

        corpo = await self._otimizar(client, h)

        assert ibov.chamadas == [], "o Ibovespa do banco cobria a janela"
        otima = corpo["maximo_sharpe"]
        assert isinstance(otima, dict)
        for ticker, peso in pesos.items():
            assert otima["pesos"][ticker] == pytest.approx(peso, abs=1e-4), (
                f"{ticker}: otima {otima['pesos'][ticker]:.4f} != indice {peso:.4f}"
            )

    async def test_premio_de_risco_e_editavel(self, client: AsyncClient, db: AsyncSession) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = await self._otimizar(client, h, premio_de_risco=0.08)

        rf = corpo["taxa_livre_risco"]
        eq = corpo["equilibrio"]
        assert isinstance(rf, float)
        assert isinstance(eq, dict)
        assert eq["premio_de_risco"] == 0.08
        for ticker, beta in eq["betas"].items():
            assert eq["retorno_de_equilibrio"][ticker] == pytest.approx(rf + beta * 0.08)

    async def test_premio_fora_da_faixa_e_recusado(self, client: AsyncClient) -> None:
        """Zero zera todo excedente; acima de 15% a premissa vira aposta."""
        _, h = await usuario_logado(client)
        for valor in (0, -0.01, 0.20):
            resp = await client.post(
                "/portfolio/optimize", json={"premio_de_risco": valor}, headers=h
            )
            assert resp.status_code == 422, valor

    async def test_sem_ibovespa_explica_em_vez_de_inventar(
        self, client: AsyncClient, db: AsyncSession, ibov: IbovFake
    ) -> None:
        """Sem o indice nao ha equilibrio. A versao antiga caia em "peso
        igual" e seguia; aqui a resposta diz o que faltou."""
        ibov.falha = True
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = await self._otimizar(client, h)

        assert corpo["fronteira"] == []
        assert corpo["equilibrio"] is None
        assert "Ibovespa" in str(corpo["motivo"])

    async def test_a_taxa_livre_de_risco_e_o_cdi_do_banco_central(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = await self._otimizar(client, h)

        esperado = (1 + float(CDI_DIARIO_FAKE)) ** 252 - 1
        assert corpo["taxa_livre_risco"] == pytest.approx(esperado)
        assert "CDI de" in str(corpo["fonte_taxa_livre_risco"])

    async def test_bcb_fora_usa_a_reserva_e_diz_isso(
        self, client: AsyncClient, db: AsyncSession, bcb: BcbFake
    ) -> None:
        bcb.falha = True
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = await self._otimizar(client, h)

        assert corpo["taxa_livre_risco"] == get_settings().RISK_FREE_RATE
        assert "reserva" in str(corpo["fonte_taxa_livre_risco"])

    async def test_opiniao_otimista_puxa_o_peso_do_ativo_para_cima(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O teste que prova que a opiniao chega ate a carteira.

        Sem ele, todo o resto poderia passar com as visoes sendo lidas,
        validadas, contadas -- e descartadas antes do otimizador.
        """
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        sem = (
            await client.post("/portfolio/optimize", json={"peso_maximo": 1.0}, headers=h)
        ).json()
        com = (
            await client.post(
                "/portfolio/optimize",
                json={"peso_maximo": 1.0, "visoes": [{"ativos": {"PETR4": 1}, "retorno": 0.60}]},
                headers=h,
            )
        ).json()

        assert com["visoes_aplicadas"] == 1
        assert com["maximo_sharpe"]["pesos"]["PETR4"] > sem["maximo_sharpe"]["pesos"]["PETR4"]

    async def test_opiniao_pessimista_empurra_o_peso_para_baixo(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O par do teste acima. Se o codigo somasse o valor absoluto da
        discordancia em vez de respeitar o sinal, so um dos dois falharia."""
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        sem = (
            await client.post("/portfolio/optimize", json={"peso_maximo": 1.0}, headers=h)
        ).json()
        com = (
            await client.post(
                "/portfolio/optimize",
                json={"peso_maximo": 1.0, "visoes": [{"ativos": {"PETR4": 1}, "retorno": -0.30}]},
                headers=h,
            )
        ).json()

        assert com["maximo_sharpe"]["pesos"]["PETR4"] < sem["maximo_sharpe"]["pesos"]["PETR4"]

    async def test_opiniao_relativa_e_aceita(self, client: AsyncClient, db: AsyncSession) -> None:
        """ "PETR4 supera VALE3 em 10 pontos" -- coeficientes somando zero."""
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = (
            await client.post(
                "/portfolio/optimize",
                json={
                    "peso_maximo": 1.0,
                    "visoes": [{"ativos": {"PETR4": 1, "VALE3": -1}, "retorno": 0.10}],
                },
                headers=h,
            )
        ).json()

        assert corpo["visoes_aplicadas"] == 1
        assert corpo["visoes_ignoradas"] == []
        assert corpo["maximo_sharpe"]["pesos"]["PETR4"] > corpo["maximo_sharpe"]["pesos"]["VALE3"]

    async def test_opiniao_sobre_ativo_fora_do_calculo_e_descartada_COM_motivo(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Descartar em silencio faria o usuario achar que a opiniao pesou.

        E aplicar pela metade seria pior: zerar o coeficiente do ausente
        transforma "PETR4 supera BBAS3 em 10 pontos" em "PETR4 rende 10%", uma
        afirmacao que ele nunca fez.
        """
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = (
            await client.post(
                "/portfolio/optimize",
                json={
                    "peso_maximo": 1.0,
                    "visoes": [{"ativos": {"PETR4": 1, "BBAS3": -1}, "retorno": 0.10}],
                },
                headers=h,
            )
        ).json()

        assert corpo["visoes_aplicadas"] == 0
        assert len(corpo["visoes_ignoradas"]) == 1
        assert "BBAS3" in corpo["visoes_ignoradas"][0]

    async def test_ticker_da_opiniao_e_normalizado(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = (
            await client.post(
                "/portfolio/optimize",
                json={"peso_maximo": 1.0, "visoes": [{"ativos": {" petr4 ": 1}, "retorno": 0.30}]},
                headers=h,
            )
        ).json()

        assert corpo["visoes_aplicadas"] == 1
        assert corpo["visoes_ignoradas"] == []

    async def test_sem_opiniao_a_fronteira_continua_saindo(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Lista vazia e o caso comum: o resultado e o equilibrio puro."""
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = (
            await client.post("/portfolio/optimize", json={"peso_maximo": 1.0}, headers=h)
        ).json()

        assert corpo["visoes_aplicadas"] == 0
        assert len(corpo["fronteira"]) > 0
        assert corpo["maximo_sharpe"] is not None

    async def test_retorno_esperado_sai_em_retorno_TOTAL(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O erro de unidade que o modelo torna facil de cometer.

        Black-Litterman trabalha em retorno EXCEDENTE; o resto do sistema, em
        retorno total. Esquecer de somar a taxa livre de risco de volta nao
        estoura em lugar nenhum -- a fronteira sai, os pesos somam 1, tudo tem
        cara de certo. O que denuncia e o Sharpe: com o mu em excedente, o
        (retorno - rf) do numerador fica negativo e a carteira de MAXIMO Sharpe
        passa a ter Sharpe negativo, que e uma contradicao em termos.
        """
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = (
            await client.post("/portfolio/optimize", json={"peso_maximo": 1.0}, headers=h)
        ).json()

        melhor = corpo["maximo_sharpe"]
        assert melhor["retorno_esperado"] > corpo["taxa_livre_risco"]
        assert melhor["indice_sharpe"] is not None
        assert melhor["indice_sharpe"] > 0

    async def test_a_opiniao_muda_tambem_a_minima_variancia(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Prova que a covariancia usada e a POSTERIOR, nao a amostral.

        A carteira de minima variancia nem olha para o mu -- so para Sigma.
        Entao se opinar mudasse so o retorno esperado, ela ficaria parada. Ela
        se mexe porque Sigma_posterior = Sigma + M, e M depende de P: a opiniao
        reduz a incerteza justamente na direcao sobre a qual se opinou.

        Com a covariancia amostral no lugar da posterior, este teste falha e
        nenhum outro percebe -- o efeito e de poucos por cento e nao aparece em
        assercao de estrutura.
        """
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        sem = (
            await client.post("/portfolio/optimize", json={"peso_maximo": 1.0}, headers=h)
        ).json()
        com = (
            await client.post(
                "/portfolio/optimize",
                json={
                    "peso_maximo": 1.0,
                    "visoes": [{"ativos": {"PETR4": 1, "VALE3": -1}, "retorno": 0.10}],
                },
                headers=h,
            )
        ).json()

        assert com["visoes_aplicadas"] == 1
        peso_sem = sem["minima_variancia"]["pesos"]["PETR4"]
        peso_com = com["minima_variancia"]["pesos"]["PETR4"]
        assert peso_sem != pytest.approx(peso_com, abs=1e-9)

    async def test_o_aviso_deixou_de_falar_em_media_historica(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O texto e a unica coisa que o usuario le sobre o modelo.

        Manter o aviso antigo depois da troca seria descrever um modelo que nao
        roda mais -- e, pior, omitir que as opinioes dele entram na conta sem
        ninguem conferir se fazem sentido.
        """
        h = await _carteira_com(client, db, "PETR4", "VALE3")

        corpo = (
            await client.post("/portfolio/optimize", json={"peso_maximo": 1.0}, headers=h)
        ).json()

        assert "Black-Litterman" in corpo["aviso"]
        assert "opiniões são suas" in corpo["aviso"]


class TestValidacaoDasVisoes:
    async def test_coeficientes_todos_zero_sao_recusados(self, client: AsyncClient) -> None:
        """Linha nula em P deixa Omega zerado naquela posicao -- divisao por
        zero disfarcada de "confianca infinita"."""
        _, h = await usuario_logado(client)

        resposta = await client.post(
            "/portfolio/optimize",
            json={
                "peso_maximo": 1.0,
                "visoes": [{"ativos": {"PETR4": 0, "VALE3": 0}, "retorno": 0.10}],
            },
            headers=h,
        )

        assert resposta.status_code == 422

    async def test_opiniao_sem_ativo_algum_e_recusada(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)

        resposta = await client.post(
            "/portfolio/optimize",
            json={"peso_maximo": 1.0, "visoes": [{"ativos": {}, "retorno": 0.10}]},
            headers=h,
        )

        assert resposta.status_code == 422

    async def test_retorno_em_percentual_em_vez_de_fracao_e_recusado(
        self, client: AsyncClient
    ) -> None:
        """15 no lugar de 0,15 e o erro de unidade mais provavel do formulario.

        Aceito, ele viraria uma expectativa de 1500% ao ano, dominaria o
        equilibrio inteiro e devolveria uma carteira sem sentido -- com cara de
        resultado.
        """
        _, h = await usuario_logado(client)

        resposta = await client.post(
            "/portfolio/optimize",
            json={"peso_maximo": 1.0, "visoes": [{"ativos": {"PETR4": 1}, "retorno": 15}]},
            headers=h,
        )

        assert resposta.status_code == 422

    async def test_perda_maior_que_o_capital_e_recusada(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)

        resposta = await client.post(
            "/portfolio/optimize",
            json={"peso_maximo": 1.0, "visoes": [{"ativos": {"PETR4": 1}, "retorno": -1.5}]},
            headers=h,
        )

        assert resposta.status_code == 422

    async def test_teto_de_opinioes_por_pedido(self, client: AsyncClient) -> None:
        """Acima do teto o usuario nao esta opinando, esta reescrevendo o mu na
        mao -- e ai Black-Litterman nao esta fazendo nada por ele."""
        _, h = await usuario_logado(client)

        resposta = await client.post(
            "/portfolio/optimize",
            json={
                "visoes": [
                    {"ativos": {f"AAA{i}": 1}, "retorno": 0.10} for i in range(MAXIMO_VISOES + 1)
                ]
            },
            headers=h,
        )

        assert resposta.status_code == 422
