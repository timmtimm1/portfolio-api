"""Testes do rebalanceamento.

Duas invariantes governam tudo:

1. **Nunca gastar mais do que existe.** Comprar a descoberto não é opção.
2. **Ações são inteiras.** Não existe comprar 12,4 ações na B3.

Todos os valores esperados foram conferidos à mão.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import TransactionSide
from app.services.rebalance import planejar
from tests.conftest import ProvedorFake
from tests.factories import criar_ativo, op, usuario_logado


@dataclass(frozen=True)
class Pos:
    ticker: str
    quantidade: Decimal
    preco_atual: Decimal | None


def pos(ticker: str, qtd: str, preco: str | None) -> Pos:
    return Pos(ticker, Decimal(qtd), Decimal(preco) if preco is not None else None)


def alvo(**pesos: str) -> dict[str, Decimal]:
    return {t: Decimal(p) for t, p in pesos.items()}


class TestDesvio:
    def test_mostra_a_distancia_antes_de_qualquer_ordem(self) -> None:
        """Ver a distância é útil mesmo para quem não vai rebalancear agora."""
        carteira = [pos("A", "100", "10.00"), pos("B", "100", "30.00")]

        plano = planejar(carteira, alvo(A="0.5", B="0.5"))

        d = {x.ticker: x for x in plano.desvios}
        # A vale 1.000 de 4.000 = 25%; B vale 3.000 = 75%.
        assert d["A"].peso_atual == Decimal("0.25")
        assert d["B"].peso_atual == Decimal("0.75")
        assert d["A"].diferenca == Decimal("-0.25")  # 25 p.p. abaixo
        assert d["B"].diferenca == Decimal("0.25")  # 25 p.p. acima

    def test_ativo_alvo_que_nao_esta_na_carteira_aparece(self) -> None:
        """Comprar algo novo é rebalanceamento também."""
        plano = planejar([pos("A", "100", "10.00")], alvo(A="0.5", B="0.5"))

        assert {d.ticker for d in plano.desvios} == {"A", "B"}
        assert next(d for d in plano.desvios if d.ticker == "B").peso_atual == 0


class TestModoAporte:
    """Sem venda: só distribui dinheiro novo."""

    def test_compra_quem_esta_abaixo_do_alvo(self) -> None:
        """A vale 1.000 e B vale 3.000. Com R$ 1.000 de aporte, a base vira
        5.000 e o alvo de A é 2.500 -- faltam 1.500, mas só há 1.000."""
        carteira = [pos("A", "100", "10.00"), pos("B", "100", "30.00")]

        plano = planejar(carteira, alvo(A="0.5", B="0.5"), aporte=Decimal(1000))

        assert [o.ticker for o in plano.ordens] == ["A"]
        assert plano.ordens[0].side is TransactionSide.COMPRA
        assert plano.ordens[0].quantidade == 100  # 1.000 / 10
        assert plano.sobra == 0

    def test_nunca_vende(self) -> None:
        """Mesmo com um ativo 25 pontos acima do alvo, o modo aporte não emite
        ordem de venda -- é o que evita imposto e realização de prejuízo."""
        carteira = [pos("A", "100", "10.00"), pos("B", "100", "30.00")]

        plano = planejar(carteira, alvo(A="0.5", B="0.5"), aporte=Decimal(500))

        assert all(o.side is TransactionSide.COMPRA for o in plano.ordens)

    def test_nao_gasta_mais_do_que_o_aporte(self) -> None:
        """A invariante mais importante: o total comprado nunca passa do que
        entrou. Comprar a descoberto não é uma opção."""
        carteira = [pos("A", "10", "10.00"), pos("B", "10", "10.00")]

        plano = planejar(carteira, alvo(A="0.5", B="0.5"), aporte=Decimal("97"))

        assert plano.total_compras <= Decimal("97")

    def test_reparte_entre_varios_ativos(self) -> None:
        carteira = [pos("A", "10", "10.00"), pos("B", "10", "10.00"), pos("C", "10", "10.00")]

        plano = planejar(carteira, alvo(A="0.34", B="0.33", C="0.33"), aporte=Decimal(300))

        assert plano.total_compras + plano.sobra == Decimal(300)

    def test_sem_aporte_nao_faz_nada(self) -> None:
        """Sem dinheiro novo e sem poder vender, não há o que rebalancear."""
        carteira = [pos("A", "100", "10.00"), pos("B", "100", "30.00")]

        plano = planejar(carteira, alvo(A="0.5", B="0.5"))

        assert plano.ordens == []


class TestModoCompleto:
    """Com venda: chega exatamente no alvo."""

    def test_vende_o_que_esta_acima_e_compra_o_que_falta(self) -> None:
        """A vale 1.000, B vale 3.000, alvo 50/50 sobre 4.000 = 2.000 cada.
        Vende 33 de B (990) e compra 99 de A (990)."""
        carteira = [pos("A", "100", "10.00"), pos("B", "100", "30.00")]

        plano = planejar(carteira, alvo(A="0.5", B="0.5"), permitir_venda=True)

        ordens = {o.ticker: o for o in plano.ordens}
        assert ordens["B"].side is TransactionSide.VENDA
        assert ordens["B"].quantidade == 33
        assert ordens["A"].side is TransactionSide.COMPRA
        assert ordens["A"].quantidade == 99

    def test_o_dinheiro_da_venda_financia_a_compra(self) -> None:
        """Sem aporte, o total comprado não pode passar do total vendido."""
        carteira = [pos("A", "100", "10.00"), pos("B", "100", "30.00")]

        plano = planejar(carteira, alvo(A="0.5", B="0.5"), permitir_venda=True)

        assert plano.total_compras <= plano.total_vendas

    def test_zera_ativo_fora_do_alvo(self) -> None:
        """Peso-alvo zero significa sair do papel."""
        carteira = [pos("A", "100", "10.00"), pos("B", "50", "20.00")]

        plano = planejar(carteira, alvo(A="1.0"), permitir_venda=True)

        venda = next(o for o in plano.ordens if o.ticker == "B")
        assert venda.side is TransactionSide.VENDA
        assert venda.quantidade == 50

    def test_venda_arredonda_para_baixo(self) -> None:
        """Vender uma ação a mais deixaria o ativo ABAIXO do alvo. O objetivo é
        chegar nele, não passar dele."""
        carteira = [pos("A", "0", "7.00"), pos("B", "100", "7.00")]

        plano = planejar(carteira, alvo(A="0.5", B="0.5"), permitir_venda=True)

        venda = next(o for o in plano.ordens if o.ticker == "B")
        # Excedente de 350; 350/7 = 50 exatas.
        assert venda.quantidade == 50


class TestArredondamento:
    def test_so_compra_acoes_inteiras(self) -> None:
        carteira = [pos("A", "0", "33.33")]

        plano = planejar(carteira, alvo(A="1.0"), aporte=Decimal(100))

        assert plano.ordens[0].quantidade == 3  # 99,99, não 3,0003
        assert plano.sobra == Decimal("0.01")

    def test_a_sobra_do_arredondamento_e_reaproveitada(self) -> None:
        """A segunda passada existe para isto: sem ela, cada ativo deixa uma
        fração parada e o plano diz "sobrou muito" com dinheiro que compraria.

        Três ativos a R$ 70, aporte de R$ 300, pesos iguais. Falta R$ 100 de
        cada, e cada um compra 1 ação (R$ 70): gastou 210 e sobraram 90 -- que
        ainda compram mais uma. Com a segunda passada são 4 ações e sobra 20;
        sem ela seriam 3 e sobrariam 90 parados.
        """
        carteira = [pos(t, "0", "70.00") for t in ("A", "B", "C")]

        plano = planejar(carteira, alvo(A="0.34", B="0.33", C="0.33"), aporte=Decimal(300))

        assert sum(o.quantidade for o in plano.ordens) == 4
        assert plano.sobra == Decimal("20.00")
        # A sobra tem que ser menor que a ação mais barata: se não for, ficou
        # dinheiro na mesa que dava para usar.
        assert plano.sobra < Decimal("70.00")

    def test_aporte_menor_que_uma_acao_nao_gera_ordem(self) -> None:
        """R$ 10 não compram uma ação de R$ 40. O plano diz isso em vez de
        arredondar para cima e sugerir uma compra impossível."""
        plano = planejar([pos("A", "0", "40.00")], alvo(A="1.0"), aporte=Decimal(10))

        assert plano.ordens == []
        assert plano.sobra == Decimal(10)


class TestCasosDeBorda:
    def test_ativo_sem_cotacao_fica_de_fora_e_e_avisado(self) -> None:
        """Sem preço não dá para decidir quantas ações comprar. Entrar pelo
        custo -- como o snapshot faz para EXIBIR um total -- aqui viraria ordem
        errada, com dinheiro real."""
        carteira = [pos("A", "100", "10.00"), pos("B", "100", None)]

        plano = planejar(carteira, alvo(A="0.5", B="0.5"), aporte=Decimal(1000))

        assert plano.sem_preco == ["B"]
        assert all(o.ticker != "B" for o in plano.ordens)

    def test_carteira_vazia_sem_aporte(self) -> None:
        plano = planejar([], alvo(A="1.0"))
        assert plano.ordens == [] and plano.sobra == 0

    def test_carteira_vazia_com_aporte_nao_tem_preco_para_comprar(self) -> None:
        """Sem posição e sem cotação de nada, não há como transformar dinheiro
        em ordem -- o aporte volta inteiro como sobra."""
        plano = planejar([], alvo(A="1.0"), aporte=Decimal(1000))
        assert plano.ordens == [] and plano.sobra == Decimal(1000)

    def test_carteira_ja_no_alvo_nao_gera_ordem(self) -> None:
        carteira = [pos("A", "100", "10.00"), pos("B", "100", "10.00")]

        plano = planejar(carteira, alvo(A="0.5", B="0.5"), permitir_venda=True)

        assert plano.ordens == []

    def test_preco_zero_e_tratado_como_sem_preco(self) -> None:
        """Preço zero é dado corrompido, não uma ação de graça -- dividir por
        ele produziria uma quantidade infinita."""
        plano = planejar([pos("A", "100", "0")], alvo(A="1.0"), aporte=Decimal(100))
        assert plano.sem_preco == ["A"]


# ═══════════════════════════════════════════════════════════════════════
# Integração: a rota
# ═══════════════════════════════════════════════════════════════════════


class TestRota:
    async def test_exige_autenticacao(self, client: AsyncClient) -> None:
        resp = await client.post("/portfolio/rebalance", json={"pesos": {"PETR4": "1.0"}})
        assert resp.status_code == 401

    async def test_pesos_que_nao_somam_cem_sao_recusados(self, client: AsyncClient) -> None:
        """Um pedido com 30% deixaria 70% do dinheiro sem destino, e o plano
        sairia sem que ninguém percebesse o que faltou."""
        _, h = await usuario_logado(client)
        resp = await client.post(
            "/portfolio/rebalance",
            json={"pesos": {"PETR4": "0.3"}, "aporte": "1000"},
            headers=h,
        )
        assert resp.status_code == 422
        assert "100%" in resp.text

    async def test_tolera_soma_quase_exata(self, client: AsyncClient, db: AsyncSession) -> None:
        """Os pesos vêm de uma otimização numérica e somam 0,9999... com
        frequência. Exigir soma exata recusaria pedidos corretos."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        resp = await client.post(
            "/portfolio/rebalance",
            json={"pesos": {"PETR4": "0.999999"}, "aporte": "1000"},
            headers=h,
        )
        assert resp.status_code == 200

    async def test_sem_venda_e_sem_aporte_e_recusado(self, client: AsyncClient) -> None:
        """Não há o que rebalancear: nenhum dinheiro novo e nenhuma venda
        permitida. Devolver um plano vazio esconderia o pedido sem sentido."""
        _, h = await usuario_logado(client)
        resp = await client.post(
            "/portfolio/rebalance",
            json={"pesos": {"PETR4": "1.0"}, "aporte": "0", "permitir_venda": False},
            headers=h,
        )
        assert resp.status_code == 422

    async def test_peso_negativo_e_recusado(self, client: AsyncClient) -> None:
        """Peso negativo seria operar vendido, que este projeto não faz."""
        _, h = await usuario_logado(client)
        resp = await client.post(
            "/portfolio/rebalance",
            json={"pesos": {"PETR4": "1.3", "VALE3": "-0.3"}, "aporte": "1000"},
            headers=h,
        )
        assert resp.status_code == 422

    async def test_nao_grava_transacao_nenhuma(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """O plano é uma sugestão. Gravar as ordens sozinho tiraria do usuário a
        decisão sobre o próprio dinheiro."""
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(ticker="PETR4"), headers=h)
        provedor.precos = {"PETR4": "20.00"}

        antes = (await client.get("/transactions", headers=h)).json()["total"]
        await client.post(
            "/portfolio/rebalance",
            json={"pesos": {"PETR4": "1.0"}, "aporte": "1000"},
            headers=h,
        )
        depois = (await client.get("/transactions", headers=h)).json()["total"]

        assert antes == depois == 1

    async def test_devolve_ordens_com_a_cotacao_de_mercado(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(ticker="PETR4", quantity="10"), headers=h)
        provedor.precos = {"PETR4": "50.00"}

        corpo = (
            await client.post(
                "/portfolio/rebalance",
                json={"pesos": {"PETR4": "1.0"}, "aporte": "1000"},
                headers=h,
            )
        ).json()

        assert len(corpo["ordens"]) == 1
        ordem = corpo["ordens"][0]
        assert ordem["side"] == "compra"
        assert ordem["quantidade"] == 20  # 1.000 / 50
        assert corpo["sobra"] == "0.00"

    async def test_nao_mistura_carteiras(self, client: AsyncClient, db: AsyncSession) -> None:
        from tests.factories import segunda_conta

        await criar_ativo(db, ticker="PETR4")
        _, dono = await usuario_logado(client)
        await client.post("/transactions", json=op(ticker="PETR4"), headers=dono)
        outro = await segunda_conta(client)

        corpo = (
            await client.post(
                "/portfolio/rebalance",
                json={"pesos": {"PETR4": "1.0"}, "aporte": "1000"},
                headers=outro,
            )
        ).json()

        # A carteira do outro está vazia: nenhum desvio com valor.
        assert all(d["valor_atual"] == "0.00" for d in corpo["desvios"])
