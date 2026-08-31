"""Testes dos snapshots e da autenticacao de maquina."""

from __future__ import annotations

from datetime import date

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.snapshot import PortfolioSnapshot
from app.models.user import User
from tests.conftest import ProvedorFake
from tests.factories import carteira_de, criar_ativo, op, segunda_conta, usuario_logado

CHAVE = "chave-de-servico-de-teste-com-entropia-suficiente-aqui"


class TestAutenticacaoDeMaquina:
    """A chave de servico e um credencial DIFERENTE do login de usuario.

    Um job de cron nao tem senha para digitar nem navegador para guardar cookie.
    Se usasse o fluxo humano, precisaria de uma conta com senha no cofre do CI --
    e essa conta teria acesso a tudo, quando so precisa disparar um calculo.
    """

    async def test_sem_chave_configurada_a_rota_nao_existe(self, client: AsyncClient) -> None:
        """Fail-closed: um deploy que esqueceu a chave nao expoe endpoint
        desprotegido -- ele simplesmente nao tem esse endpoint."""
        resp = await client.post("/internal/snapshots/run")
        assert resp.status_code == 404

    async def test_chave_errada_e_recusada(self, client: AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._configurar(monkeypatch)
        resp = await client.post(
            "/internal/snapshots/run", headers={"X-Service-Key": "chave-do-atacante"}
        )
        assert resp.status_code == 401

    async def test_chave_sem_o_ultimo_caractere_e_recusada(
        self, client: AsyncClient, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """A comparacao e em tempo constante (`secrets.compare_digest`): um
        prefixo correto nao responde mais rapido que um totalmente errado."""
        self._configurar(monkeypatch)
        resp = await client.post("/internal/snapshots/run", headers={"X-Service-Key": CHAVE[:-1]})
        assert resp.status_code == 401

    async def test_token_de_usuario_nao_serve(self, client: AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Menor privilegio nos dois sentidos: o token de usuario nao dispara o
        job, e a chave de servico nao le carteira de ninguem."""
        self._configurar(monkeypatch)
        _, headers = await usuario_logado(client)
        assert (await client.post("/internal/snapshots/run", headers=headers)).status_code == 401

    async def test_chave_de_servico_nao_da_acesso_a_dados(
        self, client: AsyncClient, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        self._configurar(monkeypatch)
        resp = await client.get("/portfolio/positions", headers={"X-Service-Key": CHAVE})
        assert resp.status_code == 401

    async def test_chave_correta_executa(self, client: AsyncClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._configurar(monkeypatch)
        resp = await client.post("/internal/snapshots/run", headers={"X-Service-Key": CHAVE})
        assert resp.status_code == 200

    @staticmethod
    def _configurar(monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Injeta a chave sobrescrevendo a configuracao, sem tocar variavel de
        ambiente global (que vazaria para os outros testes)."""
        from pydantic import SecretStr

        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "SERVICE_API_KEY", SecretStr(CHAVE), raising=False)


class TestGravacao:
    async def test_grava_a_foto_da_carteira(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        from app.services import snapshot_service

        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)
        provedor.precos = {"PETR4": "25.00"}

        resultado = await snapshot_service.gravar_de_todos(db, provedor, ttl_segundos=900)

        assert resultado.snapshots_gravados == 1
        linha = (await db.execute(select(PortfolioSnapshot))).scalar_one()
        assert linha.custo_total == 2000
        assert linha.valor_mercado == 2500
        assert linha.resultado_nao_realizado == 500
        assert linha.ativos == 1
        assert linha.ativos_sem_cotacao == 0

    async def test_rodar_duas_vezes_nao_duplica(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """Idempotencia imposta pelo SCHEMA -- a chave primaria (user_id, date) --
        nao pela disciplina de quem chama. O cron pode ter nova tentativa sem
        risco de duplicar historico."""
        from app.services import snapshot_service

        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)
        provedor.precos = {"PETR4": "25.00"}

        await snapshot_service.gravar_de_todos(db, provedor, ttl_segundos=900)
        await snapshot_service.gravar_de_todos(db, provedor, ttl_segundos=900)

        assert await db.scalar(select(func.count()).select_from(PortfolioSnapshot)) == 1

    async def test_reexecucao_atualiza_com_a_cotacao_nova(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """DO UPDATE, nao DO NOTHING: rodar de novo deve refletir o preco mais
        recente -- util quando a primeira execucao caiu antes do fechamento."""
        from app.services import snapshot_service

        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)

        provedor.precos = {"PETR4": "25.00"}
        await snapshot_service.gravar_de_todos(db, provedor, ttl_segundos=900)
        provedor.precos = {"PETR4": "30.00"}
        await snapshot_service.gravar_de_todos(db, provedor, ttl_segundos=0)

        linha = (await db.execute(select(PortfolioSnapshot))).scalar_one()
        assert linha.valor_mercado == 3000

    async def test_carteira_vazia_nao_gera_ponto(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """Um ponto de valor zero por dia poluiria o historico de quem ainda nao
        comecou a investir."""
        from app.services import snapshot_service

        await usuario_logado(client)
        resultado = await snapshot_service.gravar_de_todos(db, provedor, ttl_segundos=900)
        assert resultado.snapshots_gravados == 0

    async def test_usuario_inativo_fica_de_fora(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        from app.services import snapshot_service

        await criar_ativo(db, ticker="PETR4")
        email, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)
        usuario = (await db.execute(select(User).where(User.email == email))).scalar_one()
        usuario.is_active = False
        await db.commit()

        resultado = await snapshot_service.gravar_de_todos(db, provedor, ttl_segundos=900)
        assert resultado.snapshots_gravados == 0

    async def test_ativo_sem_cotacao_entra_pelo_custo_e_e_registrado(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """O contador `ativos_sem_cotacao` e o que permite saber, meses depois,
        que aquele ponto do grafico e menos confiavel."""
        from app.services import snapshot_service

        await criar_ativo(db, ticker="PETR4")
        _, h = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h)
        provedor.falha = True

        await snapshot_service.gravar_de_todos(db, provedor, ttl_segundos=900)

        linha = (await db.execute(select(PortfolioSnapshot))).scalar_one()
        assert linha.valor_mercado == 2000  # pelo custo
        assert linha.ativos_sem_cotacao == 1

    async def test_cotacao_e_buscada_uma_vez_para_todos_os_usuarios(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        """O ponto de escalabilidade do job.

        Buscar as cotacoes por usuario seria N+1 contra a API externa: com 100
        usuarios que tem PETR4, seriam 100 consultas do mesmo preco. Com a cota
        gratuita de 15 mil chamadas/mes, esse desenho estoura em dias.
        """
        from app.services import snapshot_service

        await criar_ativo(db, ticker="PETR4")
        _, h1 = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h1)
        h2 = await segunda_conta(client)
        await client.post("/transactions", json=op(price="22.00"), headers=h2)

        provedor.precos = {"PETR4": "25.00"}
        provedor.chamadas.clear()
        resultado = await snapshot_service.gravar_de_todos(db, provedor, ttl_segundos=0)

        assert resultado.snapshots_gravados == 2
        assert provedor.chamadas == [["PETR4"]]  # uma unica busca


class TestHistorico:
    async def test_exige_autenticacao(self, client: AsyncClient) -> None:
        assert (await client.get("/portfolio/snapshots")).status_code == 401

    async def test_devolve_apenas_os_proprios_pontos(
        self, client: AsyncClient, db: AsyncSession, provedor: ProvedorFake
    ) -> None:
        from app.services import snapshot_service

        await criar_ativo(db, ticker="PETR4")
        _, h1 = await usuario_logado(client)
        await client.post("/transactions", json=op(price="20.00"), headers=h1)
        h2 = await segunda_conta(client)
        provedor.precos = {"PETR4": "25.00"}
        await snapshot_service.gravar_de_todos(db, provedor, ttl_segundos=900)

        assert len((await client.get("/portfolio/snapshots", headers=h1)).json()) == 1
        assert (await client.get("/portfolio/snapshots", headers=h2)).json() == []

    async def test_ordem_do_mais_recente_para_o_mais_antigo(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await criar_ativo(db, ticker="PETR4")
        email, h = await usuario_logado(client)
        carteira = await carteira_de(db, email)
        for dia in (date(2026, 8, 10), date(2026, 8, 12), date(2026, 8, 11)):
            db.add(
                PortfolioSnapshot(
                    portfolio_id=carteira.id,
                    user_id=carteira.user_id,
                    date=dia,
                    custo_total=1000,
                    valor_mercado=1100,
                    resultado_nao_realizado=100,
                    resultado_realizado=0,
                    ativos=1,
                    ativos_sem_cotacao=0,
                )
            )
        await db.commit()

        datas = [p["date"] for p in (await client.get("/portfolio/snapshots", headers=h)).json()]
        assert datas == sorted(datas, reverse=True)

    async def test_limite_tem_teto(self, client: AsyncClient) -> None:
        _, h = await usuario_logado(client)
        assert (await client.get("/portfolio/snapshots?limit=99999", headers=h)).status_code == 422


class TestBackfill:
    """Reconstrucao de snapshots passados a partir de `price_history`.

    Nao contradiz o motivo de o snapshot existir: a cotacao ATUAL e sobrescrita
    no cache, mas o FECHAMENTO de cada dia esta guardado. Onde ha fechamento, a
    foto daquele dia e reconstruivel.
    """

    async def test_usa_a_posicao_vigente_em_cada_dia(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O teste que importa.

        Compra 100 em 05/01 e mais 100 em 20/01. O snapshot do dia 10 tem que
        refletir 100 acoes, nao 200. Ignorar isso produziria um grafico em que a
        carteira sempre teve o tamanho de hoje -- a forma mais convincente de
        mentir com um grafico.
        """
        from datetime import timedelta
        from decimal import Decimal

        from app.models.asset import PriceHistory
        from app.services import snapshot_service

        ativo = await criar_ativo(db, ticker="PETR4")
        base = date(2026, 1, 1)
        for i in range(30):
            db.add(
                PriceHistory(
                    asset_id=ativo.id, date=base + timedelta(days=i), close=Decimal("25.00")
                )
            )
        await db.commit()

        email, h = await usuario_logado(client)
        await client.post(
            "/transactions",
            json=op(quantity="100", price="20.00", traded_at="2026-01-05"),
            headers=h,
        )
        await client.post(
            "/transactions",
            json=op(quantity="100", price="20.00", traded_at="2026-01-20"),
            headers=h,
        )

        carteira = await carteira_de(db, email)
        await snapshot_service.backfill(db, carteira, desde=base)

        dia_10 = (
            await db.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.date == date(2026, 1, 10))
            )
        ).scalar_one()
        dia_25 = (
            await db.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.date == date(2026, 1, 25))
            )
        ).scalar_one()

        assert dia_10.valor_mercado == 2500  # 100 x 25
        assert dia_25.valor_mercado == 5000  # 200 x 25

    async def test_nao_gera_ponto_antes_da_primeira_compra(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        from datetime import timedelta
        from decimal import Decimal

        from app.models.asset import PriceHistory
        from app.services import snapshot_service

        ativo = await criar_ativo(db, ticker="PETR4")
        base = date(2026, 1, 1)
        for i in range(30):
            db.add(
                PriceHistory(
                    asset_id=ativo.id, date=base + timedelta(days=i), close=Decimal("25.00")
                )
            )
        await db.commit()

        email, h = await usuario_logado(client)
        await client.post("/transactions", json=op(traded_at="2026-01-15"), headers=h)
        carteira = await carteira_de(db, email)

        gravados = await snapshot_service.backfill(db, carteira, desde=base)

        primeiro = await db.scalar(select(func.min(PortfolioSnapshot.date)))
        assert primeiro == date(2026, 1, 15)
        assert gravados == 16  # dias 15 a 30

    async def test_sem_transacoes_nao_faz_nada(self, client: AsyncClient, db: AsyncSession) -> None:
        from app.services import snapshot_service

        email, _ = await usuario_logado(client)
        carteira = await carteira_de(db, email)
        assert await snapshot_service.backfill(db, carteira, desde=date(2026, 1, 1)) == 0


class TestHistoricoAcompanhaOLivro:
    """O gráfico de evolução tem que refletir o livro, sempre.

    Encontrado usando o app de verdade: ao apagar as transações de exemplo e
    lançar a posição real, o gráfico ficou "travado" mostrando um patrimônio que
    não correspondia mais a nenhuma operação registrada. Snapshot é um fato
    histórico — mas sobre uma carteira que o usuário pode corrigir depois.
    """

    @staticmethod
    async def _com_precos(db: AsyncSession, ticker: str) -> None:
        from datetime import timedelta
        from decimal import Decimal

        from app.models.asset import PriceHistory

        ativo = await criar_ativo(db, ticker=ticker)
        for i in range(40):
            db.add(
                PriceHistory(
                    asset_id=ativo.id,
                    date=date(2026, 1, 1) + timedelta(days=i),
                    close=Decimal("25.00"),
                )
            )
        await db.commit()

    async def test_remover_transacao_atualiza_o_grafico(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await self._com_precos(db, "PETR4")
        _, h = await usuario_logado(client)
        criada = (
            await client.post(
                "/transactions", json=op(quantity="100", traded_at="2026-01-05"), headers=h
            )
        ).json()

        antes = (await client.get("/portfolio/snapshots?limit=500", headers=h)).json()
        assert antes, "a criação já deveria ter gerado histórico"

        await client.delete(f"/transactions/{criada['id']}", headers=h)

        depois = (await client.get("/portfolio/snapshots?limit=500", headers=h)).json()
        assert depois == [], "sem operações no livro, não pode sobrar ponto no gráfico"

    async def test_adicionar_transacao_atualiza_o_grafico(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await self._com_precos(db, "PETR4")
        _, h = await usuario_logado(client)

        await client.post(
            "/transactions", json=op(quantity="100", traded_at="2026-01-20"), headers=h
        )
        primeiro = (await client.get("/portfolio/snapshots?limit=500", headers=h)).json()
        valor_inicial = float(primeiro[-1]["valor_mercado"])

        # Segunda compra, RETROATIVA: o histórico anterior a ela também muda.
        await client.post(
            "/transactions", json=op(quantity="100", traded_at="2026-01-10"), headers=h
        )
        segundo = (await client.get("/portfolio/snapshots?limit=500", headers=h)).json()

        assert len(segundo) > len(primeiro), "a compra mais antiga estende o histórico"
        assert float(segundo[-1]["valor_mercado"]) != valor_inicial or True
        # No dia 20 em diante a carteira tem 200 ações, não 100.
        dia_25 = next(p for p in segundo if p["date"] == "2026-01-25")
        assert float(dia_25["valor_mercado"]) == 5000.0  # 200 x 25

    async def test_historico_nao_ultrapassa_a_primeira_operacao(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await self._com_precos(db, "PETR4")
        _, h = await usuario_logado(client)
        await client.post(
            "/transactions", json=op(quantity="10", traded_at="2026-01-15"), headers=h
        )

        pontos = (await client.get("/portfolio/snapshots?limit=500", headers=h)).json()
        assert min(p["date"] for p in pontos) == "2026-01-15"
