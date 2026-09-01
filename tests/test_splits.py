"""Testes do ajuste por desdobramento, grupamento e bonificação.

A invariante que governa tudo: **o custo total não muda**. Desdobramento não é
lucro — é o mesmo dinheiro repartido em mais pedaços. Todo valor esperado
abaixo foi conferido à mão.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.yahoo import YahooClient
from app.models.split import Split
from app.models.transaction import TransactionSide
from app.services import split_service
from app.services.position import calcular_posicoes
from app.services.split import ajustar, fator_acumulado
from tests.factories import criar_ativo, op, usuario_logado


@dataclass(frozen=True)
class Op:
    ticker: str
    side: TransactionSide
    quantity: Decimal
    price: Decimal
    traded_at: date
    fees: Decimal = Decimal(0)


@dataclass(frozen=True)
class Evento:
    ticker: str
    data_ex: date
    numerador: Decimal
    denominador: Decimal

    @property
    def fator(self) -> Decimal:
        return self.numerador / self.denominador


def compra(ticker: str, qtd: str, preco: str, dia: date, taxas: str = "0") -> Op:
    return Op(ticker, TransactionSide.COMPRA, Decimal(qtd), Decimal(preco), dia, Decimal(taxas))


def venda(ticker: str, qtd: str, preco: str, dia: date) -> Op:
    return Op(ticker, TransactionSide.VENDA, Decimal(qtd), Decimal(preco), dia)


def evento(ticker: str, dia: date, num: str, den: str) -> Evento:
    return Evento(ticker, dia, Decimal(num), Decimal(den))


D = date  # atalho de leitura


class TestFatorAcumulado:
    def test_sem_eventos_e_um(self) -> None:
        assert fator_acumulado([], "WEGE3", D(2020, 1, 1)) == 1

    def test_evento_posterior_conta(self) -> None:
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]
        assert fator_acumulado(ev, "WEGE3", D(2020, 1, 1)) == 2

    def test_evento_anterior_nao_conta(self) -> None:
        """Quem comprou depois do desdobramento já comprou na quantidade nova."""
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]
        assert fator_acumulado(ev, "WEGE3", D(2022, 1, 1)) == 1

    def test_compra_no_proprio_dia_ex_nao_ajusta(self) -> None:
        """A comparação é estrita: no dia-ex o papel já negocia ajustado."""
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]
        assert fator_acumulado(ev, "WEGE3", D(2021, 4, 28)) == 1

    def test_eventos_compoem_por_multiplicacao(self) -> None:
        """Dois eventos: 2:1 e depois 10:1 = 20x, não 12x. Somar em vez de
        multiplicar só erraria em papel com mais de um evento -- o caso raro
        que ninguém confere à mão."""
        ev = [
            evento("MGLU3", D(2020, 10, 14), "2", "1"),
            evento("MGLU3", D(2021, 10, 14), "10", "1"),
        ]
        assert fator_acumulado(ev, "MGLU3", D(2019, 1, 1)) == 20

    def test_ignora_eventos_de_outros_ativos(self) -> None:
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]
        assert fator_acumulado(ev, "ITUB4", D(2020, 1, 1)) == 1

    def test_grupamento_reduz(self) -> None:
        """MGLU3 agrupou 1:10 em 2024: dez ações viraram uma."""
        ev = [evento("MGLU3", D(2024, 5, 27), "1", "10")]
        assert fator_acumulado(ev, "MGLU3", D(2023, 1, 1)) == Decimal("0.1")


class TestAjuste:
    def test_desdobramento_dobra_quantidade_e_divide_preco(self) -> None:
        livro = [compra("WEGE3", "100", "40.00", D(2021, 1, 10))]
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]

        a = ajustar(livro, ev)[0]

        assert a.quantity == 200
        assert a.price == Decimal("20.00")
        assert a.fator_aplicado == 2
        assert a.foi_ajustada

    def test_o_custo_nao_muda(self) -> None:
        """A invariante central. Se o custo mudar, a carteira "lucrou" com um
        evento que não distribuiu um centavo."""
        livro = [compra("WEGE3", "100", "40.00", D(2021, 1, 10))]
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]

        antes = livro[0].quantity * livro[0].price
        a = ajustar(livro, ev)[0]

        assert a.quantity * a.price == antes == Decimal(4000)

    def test_grupamento_reduz_quantidade_e_multiplica_preco(self) -> None:
        livro = [compra("MGLU3", "1000", "2.00", D(2024, 1, 10))]
        ev = [evento("MGLU3", D(2024, 5, 27), "1", "10")]

        a = ajustar(livro, ev)[0]

        assert a.quantity == 100
        assert a.price == Decimal("20.00")
        assert a.quantity * a.price == Decimal(2000)

    def test_bonificacao_de_3_por_cento(self) -> None:
        """ITUB4, 103:100 em 26/12/2025 -- caso real da série do Yahoo."""
        livro = [compra("ITUB4", "100", "30.90", D(2025, 6, 1))]
        ev = [evento("ITUB4", D(2025, 12, 26), "103", "100")]

        a = ajustar(livro, ev)[0]

        assert a.quantity == 103
        assert a.quantity * a.price == Decimal(3090)

    def test_taxas_nao_sao_ajustadas(self) -> None:
        """A corretagem foi paga em reais, uma vez. Ela não se multiplica
        porque a ação se dividiu."""
        livro = [compra("WEGE3", "100", "40.00", D(2021, 1, 10), taxas="9.90")]
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]

        assert ajustar(livro, ev)[0].fees == Decimal("9.90")

    def test_transacao_posterior_fica_intacta(self) -> None:
        livro = [compra("WEGE3", "100", "20.00", D(2022, 1, 10))]
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]

        a = ajustar(livro, ev)[0]

        assert a.quantity == 100
        assert a.price == Decimal("20.00")
        assert not a.foi_ajustada

    def test_sem_eventos_preserva_tudo(self) -> None:
        livro = [compra("PETR4", "100", "38.50", D(2026, 1, 10), taxas="4.90")]
        a = ajustar(livro, [])[0]

        assert (a.quantity, a.price, a.fees) == (Decimal(100), Decimal("38.50"), Decimal("4.90"))
        assert not a.foi_ajustada

    def test_livro_vazio(self) -> None:
        assert ajustar([], [evento("WEGE3", D(2021, 4, 28), "2", "1")]) == []


class TestPosicaoDepoisDoAjuste:
    """O ponto de tudo: as funções que já existiam continuam corretas, sem
    saber que houve evento."""

    def test_preco_medio_reflete_o_desdobramento(self) -> None:
        livro = [compra("WEGE3", "100", "40.00", D(2021, 1, 10))]
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]

        posicao = calcular_posicoes(ajustar(livro, ev))["WEGE3"]

        assert posicao.quantidade == 200
        assert posicao.preco_medio == Decimal("20.00")
        assert posicao.custo_total == Decimal("4000.00")

    def test_compras_antes_e_depois_convivem(self) -> None:
        """O caso que separa uma implementação certa de uma quase certa: só a
        compra anterior ao evento é ajustada, e o preço médio sai da mistura
        das duas já na mesma unidade.

        100 ações a R$ 40 viram 200 a R$ 20 (custo 4.000).
        Mais 100 a R$ 22 depois (custo 2.200).
        Total: 300 ações, custo 6.200, médio 20,666...
        """
        livro = [
            compra("WEGE3", "100", "40.00", D(2021, 1, 10)),
            compra("WEGE3", "100", "22.00", D(2021, 6, 10)),
        ]
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]

        posicao = calcular_posicoes(ajustar(livro, ev))["WEGE3"]

        assert posicao.quantidade == 300
        assert posicao.custo_total == Decimal("6200.00")
        assert posicao.preco_medio.quantize(Decimal("0.0001")) == Decimal("20.6667")

    def test_venda_posterior_usa_a_quantidade_ajustada(self) -> None:
        """Vender 150 de 200 (pós-desdobramento) deixa 50. Sem o ajuste, o
        livro acharia que só havia 100 e recusaria a venda como descoberta."""
        livro = [
            compra("WEGE3", "100", "40.00", D(2021, 1, 10)),
            venda("WEGE3", "150", "25.00", D(2021, 6, 10)),
        ]
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]

        posicao = calcular_posicoes(ajustar(livro, ev))["WEGE3"]

        assert posicao.quantidade == 50
        # Vendeu 150 a R$ 25 (3.750) com custo médio de R$ 20 (3.000).
        assert posicao.resultado_realizado == Decimal("750.00")

    def test_sem_ajuste_a_posicao_fica_errada(self) -> None:
        """A contraprova, e o motivo deste módulo existir: ignorar o evento
        mostra metade das ações ao dobro do preço. Nada quebra -- o número só
        fica errado."""
        livro = [compra("WEGE3", "100", "40.00", D(2021, 1, 10))]

        sem = calcular_posicoes(livro)["WEGE3"]
        ev = [evento("WEGE3", D(2021, 4, 28), "2", "1")]
        com = calcular_posicoes(ajustar(livro, ev))["WEGE3"]

        assert sem.quantidade == 100 and com.quantidade == 200
        # O custo, esse, tem que bater nos dois -- é o que prova que o ajuste
        # redistribui em vez de criar dinheiro.
        assert sem.custo_total == com.custo_total


# ═══════════════════════════════════════════════════════════════════════
# Integração: fornecedor, banco e rota
# ═══════════════════════════════════════════════════════════════════════


class TestBuscaNoYahoo:
    """O Yahoo devolve desdobramento, grupamento e bonificação todos como
    "split", com numerador e denominador. A distinção é de nome, não de
    matemática."""

    @staticmethod
    def _payload(eventos: list[tuple[int, int, int, float, float]]) -> dict[str, object]:
        return {
            "chart": {
                "result": [
                    {
                        "events": {
                            "splits": {
                                str(i): {
                                    "date": int(datetime(a, m, d, 15, 0, tzinfo=UTC).timestamp()),
                                    "numerator": n,
                                    "denominator": den,
                                }
                                for i, (a, m, d, n, den) in enumerate(eventos)
                            }
                        }
                    }
                ]
            }
        }

    async def _buscar(self, payload: object, ticker: str = "WEGE3") -> list:
        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            return await YahooClient(http).desdobramentos(ticker, D(2015, 1, 1), D(2026, 12, 31))

    async def test_le_numerador_e_denominador(self) -> None:
        """WEGE3, 2:1 em 28/04/2021 -- caso real da série."""
        ds = await self._buscar(self._payload([(2021, 4, 28, 2.0, 1.0)]))

        assert len(ds) == 1
        assert ds[0].data_ex == D(2021, 4, 28)
        assert ds[0].numerador == 2
        assert ds[0].denominador == 1

    async def test_grupamento_vem_com_numerador_menor(self) -> None:
        """MGLU3 agrupou 1:10 em 27/05/2024."""
        ds = await self._buscar(self._payload([(2024, 5, 27, 1.0, 10.0)]), "MGLU3")

        assert ds[0].numerador / ds[0].denominador == Decimal("0.1")

    async def test_pede_os_eventos_certos(self) -> None:
        capturada: list[httpx.Request] = []

        def responder(request: httpx.Request) -> httpx.Response:
            capturada.append(request)
            return httpx.Response(200, json=self._payload([]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            await YahooClient(http).desdobramentos("WEGE3", D(2015, 1, 1), D(2026, 12, 31))

        assert "WEGE3.SA" in str(capturada[0].url)
        assert capturada[0].url.params["events"] == "split"

    async def test_ordem_cronologica(self) -> None:
        ds = await self._buscar(self._payload([(2021, 4, 28, 2.0, 1.0), (2018, 4, 25, 13.0, 10.0)]))
        assert [d.data_ex for d in ds] == sorted(d.data_ex for d in ds)

    async def test_denominador_zero_e_descartado(self) -> None:
        """Fator infinito corromperia toda a posição do ativo em silêncio."""
        assert await self._buscar(self._payload([(2021, 4, 28, 2.0, 0.0)])) == []

    async def test_numerador_zero_e_descartado(self) -> None:
        """Fator zero zeraria a carteira."""
        assert await self._buscar(self._payload([(2021, 4, 28, 0.0, 1.0)])) == []

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            {"chart": {"result": []}},
            {"chart": {"result": [{"events": {}}]}},
            {"chart": {"result": [{"events": {"splits": "nao-e-dict"}}]}},
            {"chart": {"result": [{"events": {"splits": {"0": {"date": "x"}}}}]}},
        ],
    )
    async def test_payload_malformado_devolve_vazio(self, payload: object) -> None:
        assert await self._buscar(payload) == []

    async def test_falha_de_rede_nao_propaga(self) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("estourou", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            ds = await YahooClient(http).desdobramentos("WEGE3", D(2015, 1, 1), D(2026, 12, 31))

        assert ds == []


class TestSincronizacao:
    async def test_grava_e_e_idempotente(self, db: AsyncSession) -> None:
        ativo = await criar_ativo(db, ticker="WEGE3")

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=TestBuscaNoYahoo._payload([(2021, 4, 28, 2.0, 1.0)]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            cliente = YahooClient(http)
            primeira = await split_service.sincronizar(
                db, cliente, ["WEGE3"], D(2015, 1, 1), D(2026, 12, 31)
            )
            segunda = await split_service.sincronizar(
                db, cliente, ["WEGE3"], D(2015, 1, 1), D(2026, 12, 31)
            )

        assert (primeira, segunda) == (1, 0)
        linha = await db.get(Split, (ativo.id, D(2021, 4, 28)))
        assert linha is not None and linha.fator == 2


class TestPosicaoPelaRota:
    async def test_a_posicao_reflete_o_desdobramento(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Ponta a ponta: compra antes do evento, evento no banco, e o endpoint
        de posições devolve a quantidade de hoje."""
        ativo = await criar_ativo(db, ticker="WEGE3")
        _, h = await usuario_logado(client)
        await client.post(
            "/transactions",
            json=op(ticker="WEGE3", quantity="100", price="40.00", traded_at="2026-01-10"),
            headers=h,
        )

        antes = (await client.get("/portfolio/positions", headers=h)).json()[0]
        assert Decimal(antes["quantidade"]) == 100
        assert Decimal(antes["preco_medio"]) == 40

        db.add(
            Split(
                asset_id=ativo.id,
                data_ex=D(2026, 3, 2),
                numerador=Decimal(2),
                denominador=Decimal(1),
            )
        )
        await db.commit()

        depois = (await client.get("/portfolio/positions", headers=h)).json()[0]
        assert Decimal(depois["quantidade"]) == 200
        assert Decimal(depois["preco_medio"]) == 20
        # O que NAO pode mudar: o custo. Ninguém ganhou dinheiro aqui.
        assert depois["custo_total"] == antes["custo_total"]

    async def test_venda_pos_desdobramento_e_aceita(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Sem ajuste na VALIDAÇÃO, o livro acharia que só há 100 ações e
        recusaria esta venda legítima como venda a descoberto."""
        ativo = await criar_ativo(db, ticker="WEGE3")
        _, h = await usuario_logado(client)
        await client.post(
            "/transactions",
            json=op(ticker="WEGE3", quantity="100", price="40.00", traded_at="2026-01-10"),
            headers=h,
        )
        db.add(
            Split(
                asset_id=ativo.id,
                data_ex=D(2026, 3, 2),
                numerador=Decimal(2),
                denominador=Decimal(1),
            )
        )
        await db.commit()

        resp = await client.post(
            "/transactions",
            json=op(
                ticker="WEGE3",
                side="venda",
                quantity="150",
                price="25.00",
                traded_at="2026-04-01",
            ),
            headers=h,
        )

        assert resp.status_code == 201, resp.text
        posicoes = (await client.get("/portfolio/positions", headers=h)).json()
        assert Decimal(posicoes[0]["quantidade"]) == 50
