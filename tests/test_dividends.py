"""Testes do cálculo de proventos.

A regra central é a **data-com**: recebe quem tinha o ativo no fechamento
daquele dia, não quem tem hoje. Todos os valores esperados foram conferidos à
mão.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.yahoo import YahooClient
from app.models.dividend import Dividend, TipoProvento
from app.models.transaction import TransactionSide
from app.services import dividend_service
from app.services.dividend import (
    quantidade_em,
    recebidos,
    total_liquido,
    yield_on_cost,
)
from tests.factories import criar_ativo, op, usuario_logado


@dataclass(frozen=True)
class Op:
    """Transação mínima — só o que o cálculo de proventos precisa."""

    ticker: str
    side: TransactionSide
    quantity: Decimal
    traded_at: date
    price: Decimal = Decimal(10)
    fees: Decimal = Decimal(0)


@dataclass(frozen=True)
class Prov:
    ticker: str
    data_com: date
    valor_por_cota: Decimal
    tipo: TipoProvento = TipoProvento.DIVIDENDO


def compra(ticker: str, qtd: int, dia: int, mes: int = 8) -> Op:
    return Op(ticker, TransactionSide.COMPRA, Decimal(qtd), date(2026, mes, dia))


def venda(ticker: str, qtd: int, dia: int, mes: int = 8) -> Op:
    return Op(ticker, TransactionSide.VENDA, Decimal(qtd), date(2026, mes, dia))


class TestQuantidadeNaData:
    def test_compra_no_proprio_dia_conta(self) -> None:
        """Quem compra NA data-com aparece na posição do fechamento e recebe."""
        livro = [compra("TAEE11", 45, 17)]
        assert quantidade_em(livro, "TAEE11", date(2026, 8, 17)) == 45

    def test_compra_depois_da_data_nao_conta(self) -> None:
        """O caso real desta carteira: TAEE11 comprada em 20/08, provento com
        data-com em 17/08. Três dias de diferença, e o provento não é seu."""
        livro = [compra("TAEE11", 45, 20)]
        assert quantidade_em(livro, "TAEE11", date(2026, 8, 17)) == 0

    def test_vendas_reduzem(self) -> None:
        livro = [compra("PETR4", 100, 1), venda("PETR4", 40, 10)]
        assert quantidade_em(livro, "PETR4", date(2026, 8, 15)) == 60

    def test_venda_posterior_nao_afeta_a_data_anterior(self) -> None:
        """A posição de 10/08 não muda porque você vendeu em 20/08. Usar o livro
        inteiro em vez do livro ATÉ a data apagaria proventos já recebidos."""
        livro = [compra("PETR4", 100, 1), venda("PETR4", 100, 20)]
        assert quantidade_em(livro, "PETR4", date(2026, 8, 10)) == 100
        assert quantidade_em(livro, "PETR4", date(2026, 8, 25)) == 0

    def test_ignora_outros_tickers(self) -> None:
        livro = [compra("PETR4", 100, 1), compra("VALE3", 10, 1)]
        assert quantidade_em(livro, "VALE3", date(2026, 8, 5)) == 10

    def test_ticker_ausente_do_livro(self) -> None:
        assert quantidade_em([compra("PETR4", 100, 1)], "ITUB4", date(2026, 8, 5)) == 0


class TestRecebidos:
    def test_multiplica_quantidade_pelo_valor_por_cota(self) -> None:
        """20 cotas × R$ 1,348143 = R$ 26,96 (arredondado ao centavo)."""
        livro = [compra("PETR4", 20, 20)]
        provs = [Prov("PETR4", date(2026, 8, 24), Decimal("1.348143"))]

        r = recebidos(livro, provs)

        assert len(r) == 1
        assert r[0].quantidade == 20
        assert r[0].valor_bruto == Decimal("26.96")

    def test_provento_antes_da_compra_nao_entra(self) -> None:
        """O caso que motivou esta fase inteira. A VALE3 teve data-com em 12/08
        e a compra foi em 20/08 -- somar por ticker inventaria dinheiro."""
        livro = [compra("VALE3", 10, 20)]
        provs = [Prov("VALE3", date(2026, 8, 12), Decimal("2.030722"))]

        assert recebidos(livro, provs) == []

    def test_ativo_que_a_carteira_nao_tem_e_ignorado(self) -> None:
        """Não é erro: é um evento de mercado que não diz respeito a esta
        carteira. A tabela de proventos é do ATIVO, não do usuário."""
        livro = [compra("PETR4", 100, 1)]
        provs = [Prov("ITUB4", date(2026, 8, 3), Decimal("0.018182"))]

        assert recebidos(livro, provs) == []

    def test_jcp_retem_15_por_cento_na_fonte(self) -> None:
        """R$ 100 de JCP viram R$ 85 na conta. Somar o bruto superestimaria o
        retorno -- trocar um número subestimado por um superestimado não é
        conserto."""
        livro = [compra("ITUB4", 100, 1)]
        provs = [Prov("ITUB4", date(2026, 8, 10), Decimal("1.00"), TipoProvento.JCP)]

        r = recebidos(livro, provs)

        assert r[0].valor_bruto == Decimal("100.00")
        assert r[0].valor_liquido == Decimal("85.00")
        assert r[0].imposto_retido == Decimal("15.00")

    def test_dividendo_nao_tem_retencao(self) -> None:
        livro = [compra("ITUB4", 100, 1)]
        provs = [Prov("ITUB4", date(2026, 8, 10), Decimal("1.00"))]

        r = recebidos(livro, provs)
        assert r[0].valor_bruto == r[0].valor_liquido == Decimal("100.00")

    def test_indefinido_nao_desconta_nada(self) -> None:
        """O Yahoo não classifica o provento. Aplicar 15% "por via das dúvidas"
        inventaria um imposto que pode não existir -- e errar para menos parece
        certo e ninguém percebe. Errar para o bruto é visível e corrigível."""
        livro = [compra("TAEE11", 45, 1)]
        provs = [Prov("TAEE11", date(2026, 8, 17), Decimal("1.00"), TipoProvento.INDEFINIDO)]

        r = recebidos(livro, provs)
        assert r[0].valor_liquido == Decimal("45.00")

    def test_ordem_cronologica_na_saida(self) -> None:
        livro = [compra("PETR4", 100, 1), compra("VALE3", 100, 1)]
        provs = [
            Prov("VALE3", date(2026, 8, 20), Decimal("1.00")),
            Prov("PETR4", date(2026, 8, 5), Decimal("1.00")),
            Prov("PETR4", date(2026, 8, 12), Decimal("1.00")),
        ]

        datas = [r.data_com for r in recebidos(livro, provs)]
        assert datas == sorted(datas)

    def test_posicao_zerada_na_data_com_nao_recebe(self) -> None:
        """Vendeu tudo antes da data-com: não recebe, mesmo tendo comprado de
        volta depois."""
        livro = [compra("PETR4", 100, 1), venda("PETR4", 100, 5), compra("PETR4", 100, 25)]
        provs = [Prov("PETR4", date(2026, 8, 12), Decimal("1.00"))]

        assert recebidos(livro, provs) == []

    def test_posicao_parcial_recebe_proporcional(self) -> None:
        livro = [compra("PETR4", 100, 1), venda("PETR4", 70, 5)]
        provs = [Prov("PETR4", date(2026, 8, 12), Decimal("2.00"))]

        r = recebidos(livro, provs)
        assert r[0].quantidade == 30
        assert r[0].valor_bruto == Decimal("60.00")


class TestTotais:
    def test_total_soma_o_liquido_e_nao_o_bruto(self) -> None:
        livro = [compra("ITUB4", 100, 1)]
        provs = [
            Prov("ITUB4", date(2026, 8, 5), Decimal("1.00"), TipoProvento.JCP),
            Prov("ITUB4", date(2026, 8, 15), Decimal("1.00"), TipoProvento.DIVIDENDO),
        ]

        # 85 (JCP líquido) + 100 (dividendo) = 185, não 200.
        assert total_liquido(recebidos(livro, provs)) == Decimal("185.00")

    def test_total_de_lista_vazia_e_zero(self) -> None:
        assert total_liquido([]) == Decimal(0)

    def test_yield_on_cost(self) -> None:
        """R$ 100 recebidos sobre R$ 2.000 de custo = 5%."""
        livro = [compra("ITUB4", 100, 1)]
        provs = [Prov("ITUB4", date(2026, 8, 5), Decimal("1.00"))]

        y = yield_on_cost(recebidos(livro, provs), Decimal("2000"))
        assert y == Decimal("0.05")

    def test_yield_sem_custo_devolve_none(self) -> None:
        """Carteira vazia não tem yield indefinido -- tem yield que não faz
        sentido perguntar. Devolver None diz isso; levantar exceção obrigaria
        todo chamador a tratar um caso normal como erro."""
        assert yield_on_cost([], Decimal(0)) is None


# ═══════════════════════════════════════════════════════════════════════
# Testes de integração: banco e rotas
# ═══════════════════════════════════════════════════════════════════════


class TestRotaDeProventos:
    async def test_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.get("/portfolio/dividends")).status_code == 401

    async def test_carteira_sem_transacoes_devolve_vazio(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        corpo = (await client.get("/portfolio/dividends", headers=h)).json()

        assert corpo["proventos"] == []
        assert corpo["total_liquido"] == "0.00"
        # Sem custo, o yield não é zero -- é uma pergunta sem sentido.
        assert corpo["yield_on_cost"] is None

    async def test_cruza_provento_com_o_livro(self, client: AsyncClient, db: AsyncSession) -> None:
        """O caminho completo: ativo, compra, provento no banco, endpoint."""
        ativo = await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post(
            "/transactions",
            json=op(ticker="PETR4", quantity="100", traded_at="2026-01-05"),
            headers=h,
        )

        db.add(
            Dividend(
                asset_id=ativo.id,
                data_com=date(2026, 1, 20),
                tipo=TipoProvento.DIVIDENDO,
                valor_por_cota=Decimal("0.50"),
                fonte="yahoo",
            )
        )
        await db.commit()

        corpo = (await client.get("/portfolio/dividends", headers=h)).json()

        assert len(corpo["proventos"]) == 1
        assert corpo["proventos"][0]["ticker"] == "PETR4"
        assert corpo["total_liquido"] == "50.00"

    async def test_provento_anterior_a_compra_nao_aparece(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A regra da data-com, agora ponta a ponta e não só no módulo puro."""
        ativo = await criar_ativo(db, ticker="VALE3")
        _, h = await usuario_logado(client)
        await client.post(
            "/transactions",
            json=op(ticker="VALE3", quantity="10", traded_at="2026-01-20"),
            headers=h,
        )

        db.add(
            Dividend(
                asset_id=ativo.id,
                data_com=date(2026, 1, 5),
                tipo=TipoProvento.DIVIDENDO,
                valor_por_cota=Decimal("2.00"),
                fonte="yahoo",
            )
        )
        await db.commit()

        corpo = (await client.get("/portfolio/dividends", headers=h)).json()
        assert corpo["proventos"] == []

    async def test_nao_mistura_carteiras(self, client: AsyncClient, db: AsyncSession) -> None:
        """Proventos são derivados do livro, e o livro é por carteira. Um
        usuário não pode ver o provento que o outro recebeu."""
        from tests.factories import segunda_conta

        ativo = await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        await client.post("/transactions", json=op(ticker="PETR4"), headers=dono)
        outro = await segunda_conta(client)

        db.add(
            Dividend(
                asset_id=ativo.id,
                data_com=date(2026, 2, 10),
                tipo=TipoProvento.DIVIDENDO,
                valor_por_cota=Decimal("1.00"),
                fonte="yahoo",
            )
        )
        await db.commit()

        assert (await client.get("/portfolio/dividends", headers=dono)).json()["proventos"]
        assert (await client.get("/portfolio/dividends", headers=outro)).json()["proventos"] == []

    async def test_conta_os_sem_classificacao(self, client: AsyncClient, db: AsyncSession) -> None:
        """A interface precisa poder avisar: enquanto for INDEFINIDO, o líquido
        pode estar até 15% acima do real."""
        ativo = await criar_ativo(db, ticker="ITUB4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(ticker="ITUB4"), headers=h)

        db.add(
            Dividend(
                asset_id=ativo.id,
                data_com=date(2026, 2, 10),
                tipo=TipoProvento.INDEFINIDO,
                valor_por_cota=Decimal("1.00"),
                fonte="yahoo",
            )
        )
        await db.commit()

        corpo = (await client.get("/portfolio/dividends", headers=h)).json()
        assert corpo["sem_classificacao"] == 1


class TestReclassificacao:
    async def test_troca_indefinido_por_jcp_e_desconta_o_imposto(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        ativo = await criar_ativo(db, ticker="ITUB4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(ticker="ITUB4", quantity="100"), headers=h)
        db.add(
            Dividend(
                asset_id=ativo.id,
                data_com=date(2026, 2, 10),
                tipo=TipoProvento.INDEFINIDO,
                valor_por_cota=Decimal("1.00"),
                fonte="yahoo",
            )
        )
        await db.commit()

        antes = (await client.get("/portfolio/dividends", headers=h)).json()
        assert antes["total_liquido"] == "100.00"

        resp = await client.post(
            "/portfolio/dividends/reclassificar",
            json={"ticker": "ITUB4", "data_com": "2026-02-10", "tipo": "jcp"},
            headers=h,
        )
        assert resp.status_code == 204

        depois = (await client.get("/portfolio/dividends", headers=h)).json()
        assert depois["total_liquido"] == "85.00"
        assert depois["imposto_retido"] == "15.00"
        assert depois["sem_classificacao"] == 0

    async def test_recusa_reclassificar_para_indefinido(self, client: AsyncClient) -> None:
        """Desfazer não é caso de uso, e deixaria uma linha órfã que colidiria
        com a chave primária na próxima sincronização."""
        _, h = await usuario_logado(client)
        resp = await client.post(
            "/portfolio/dividends/reclassificar",
            json={"ticker": "ITUB4", "data_com": "2026-02-10", "tipo": "indefinido"},
            headers=h,
        )
        assert resp.status_code == 422

    async def test_ativo_fora_da_carteira_e_404(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="WEGE3")
        _, h = await usuario_logado(client)
        resp = await client.post(
            "/portfolio/dividends/reclassificar",
            json={"ticker": "WEGE3", "data_com": "2026-02-10", "tipo": "jcp"},
            headers=h,
        )
        assert resp.status_code == 404

    async def test_provento_inexistente_e_404(self, client: AsyncClient, db: AsyncSession) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(ticker="PETR4"), headers=h)

        resp = await client.post(
            "/portfolio/dividends/reclassificar",
            json={"ticker": "PETR4", "data_com": "2026-02-10", "tipo": "jcp"},
            headers=h,
        )
        assert resp.status_code == 404


class TestBuscaNoYahoo:
    """O endpoint de eventos do Yahoo (`events=div`) devolve os proventos junto
    com a série de preços, num dicionário com o timestamp como chave."""

    @staticmethod
    def _payload(eventos: list[tuple[int, int, int, float]]) -> dict[str, object]:
        return {
            "chart": {
                "result": [
                    {
                        "events": {
                            "dividends": {
                                str(i): {
                                    "date": int(datetime(a, m, d, 15, 0, tzinfo=UTC).timestamp()),
                                    "amount": v,
                                }
                                for i, (a, m, d, v) in enumerate(eventos)
                            }
                        }
                    }
                ]
            }
        }

    async def test_converte_timestamp_em_data_com(self) -> None:
        payload = self._payload([(2026, 8, 17, 0.600238)])

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            ps = await YahooClient(http).proventos("TAEE11", date(2026, 1, 1), date(2026, 12, 31))

        assert len(ps) == 1
        assert ps[0].data_com == date(2026, 8, 17)
        assert ps[0].valor_por_cota == Decimal("0.600238")

    async def test_pede_os_eventos_e_usa_o_sufixo_da_bolsa(self) -> None:
        """`.SA` é convenção do Yahoo para a B3 e é adicionada no adaptador --
        nunca guardada no banco."""
        capturada: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            capturada.append(request)
            return httpx.Response(200, json=self._payload([]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            await YahooClient(http).proventos("PETR4", date(2026, 1, 1), date(2026, 12, 31))

        assert "PETR4.SA" in str(capturada[0].url)
        assert capturada[0].url.params["events"] == "div"

    async def test_filtra_pela_janela_pedida(self) -> None:
        """A janela vai larga ao fornecedor (`JANELA_PROVENTOS`) e o recorte é
        feito aqui -- o endpoint não aceita data inicial e final."""
        payload = self._payload([(2024, 5, 10, 1.0), (2026, 8, 17, 2.0)])

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            ps = await YahooClient(http).proventos("TAEE11", date(2026, 1, 1), date(2026, 12, 31))

        assert [p.data_com for p in ps] == [date(2026, 8, 17)]

    async def test_ordem_cronologica(self) -> None:
        payload = self._payload([(2026, 8, 17, 1.0), (2026, 2, 10, 1.0), (2026, 5, 12, 1.0)])

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            ps = await YahooClient(http).proventos("TAEE11", date(2026, 1, 1), date(2026, 12, 31))

        assert [p.data_com for p in ps] == sorted(p.data_com for p in ps)

    async def test_valor_zero_ou_negativo_e_descartado(self) -> None:
        """Provento de R$ 0,00 é dado corrompido, não um pagamento."""
        payload = self._payload([(2026, 8, 17, 0.0), (2026, 8, 18, -1.0)])

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            ps = await YahooClient(http).proventos("TAEE11", date(2026, 1, 1), date(2026, 12, 31))

        assert ps == []

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            {"chart": {"result": []}},
            {"chart": {"result": [{}]}},
            {"chart": {"result": [{"events": {}}]}},
            {"chart": {"result": [{"events": {"dividends": "nao-e-dict"}}]}},
            {"chart": {"result": [{"events": {"dividends": {"0": {"date": "x", "amount": 1}}}}]}},
        ],
    )
    async def test_payload_malformado_devolve_vazio(self, payload: object) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            ps = await YahooClient(http).proventos("TAEE11", date(2026, 1, 1), date(2026, 12, 31))

        assert ps == []

    async def test_falha_de_rede_nao_propaga(self) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("estourou", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            ps = await YahooClient(http).proventos("TAEE11", date(2026, 1, 1), date(2026, 12, 31))

        assert ps == []


class TestSincronizacao:
    async def test_grava_como_indefinido(self, db: AsyncSession) -> None:
        """O Yahoo não classifica. Gravar como DIVIDENDO embutiria um erro de
        15% toda vez que o provento fosse JCP."""
        ativo = await criar_ativo(db, ticker="TAEE11")

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=TestBuscaNoYahoo._payload([(2026, 8, 17, 0.6)]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            n = await dividend_service.sincronizar(
                db, YahooClient(http), ["TAEE11"], date(2026, 1, 1), date(2026, 12, 31)
            )

        assert n == 1
        linha = await db.get(Dividend, (ativo.id, date(2026, 8, 17), TipoProvento.INDEFINIDO))
        assert linha is not None
        assert linha.fonte == "yahoo"

    async def test_e_idempotente(self, db: AsyncSession) -> None:
        """Rodar duas vezes não duplica e não falha -- um sincronizador que só
        pode rodar uma vez é um sincronizador que ninguém se atreve a rodar."""
        await criar_ativo(db, ticker="ITUB4")

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=TestBuscaNoYahoo._payload([(2026, 8, 3, 0.018)]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            cliente = YahooClient(http)
            primeira = await dividend_service.sincronizar(
                db, cliente, ["ITUB4"], date(2026, 1, 1), date(2026, 12, 31)
            )
            segunda = await dividend_service.sincronizar(
                db, cliente, ["ITUB4"], date(2026, 1, 1), date(2026, 12, 31)
            )

        assert primeira == 1
        assert segunda == 0

    async def test_nao_duplica_apos_reclassificacao(self, db: AsyncSession) -> None:
        """Regressão de um bug real, achado com dados de verdade.

        A chave primária inclui `tipo`. Reclassificar de INDEFINIDO para JCP
        deixa a vaga do INDEFINIDO livre, e `ON CONFLICT DO NOTHING` -- que
        olha a chave inteira -- não impede a reinserção. A segunda
        sincronização criava uma SEGUNDA linha para a mesma data-com, e o
        provento passava a ser contado duas vezes.

        Na carteira real deste projeto isso inflou o total de R$ 75,44 para
        R$ 111,63 sem nenhum erro aparecer.
        """
        ativo = await criar_ativo(db, ticker="ITUB4")

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=TestBuscaNoYahoo._payload([(2026, 6, 19, 0.36188)]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            cliente = YahooClient(http)
            await dividend_service.sincronizar(
                db, cliente, ["ITUB4"], date(2026, 1, 1), date(2026, 12, 31)
            )
            await dividend_service.reclassificar(db, ativo.id, date(2026, 6, 19), TipoProvento.JCP)
            reinseridos = await dividend_service.sincronizar(
                db, cliente, ["ITUB4"], date(2026, 1, 1), date(2026, 12, 31)
            )

        assert reinseridos == 0

        linhas = (
            (
                await db.execute(
                    select(Dividend).where(
                        Dividend.asset_id == ativo.id, Dividend.data_com == date(2026, 6, 19)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(linhas) == 1, f"{len(linhas)} linhas para a mesma data-com"
        assert linhas[0].tipo is TipoProvento.JCP

    async def test_nao_desfaz_reclassificacao_manual(self, db: AsyncSession) -> None:
        """O caso que justifica `DO NOTHING` em vez de `DO UPDATE`: depois de o
        usuário marcar um provento como JCP, a próxima sincronização não pode
        reintroduzir a versão INDEFINIDO por cima."""
        ativo = await criar_ativo(db, ticker="ITUB4")

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=TestBuscaNoYahoo._payload([(2026, 8, 3, 1.0)]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            cliente = YahooClient(http)
            await dividend_service.sincronizar(
                db, cliente, ["ITUB4"], date(2026, 1, 1), date(2026, 12, 31)
            )
            await dividend_service.reclassificar(db, ativo.id, date(2026, 8, 3), TipoProvento.JCP)
            await dividend_service.sincronizar(
                db, cliente, ["ITUB4"], date(2026, 1, 1), date(2026, 12, 31)
            )

        jcp = await db.get(Dividend, (ativo.id, date(2026, 8, 3), TipoProvento.JCP))
        assert jcp is not None
        assert jcp.fonte == "manual"

    async def test_ticker_desconhecido_e_ignorado(self, db: AsyncSession) -> None:
        """Ativo que não está no catálogo não tem `asset_id` -- pular é melhor
        que estourar, porque a carteira pode ter um papel recém-listado."""

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=TestBuscaNoYahoo._payload([(2026, 8, 3, 1.0)]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            n = await dividend_service.sincronizar(
                db, YahooClient(http), ["NAOEXISTE11"], date(2026, 1, 1), date(2026, 12, 31)
            )

        assert n == 0
