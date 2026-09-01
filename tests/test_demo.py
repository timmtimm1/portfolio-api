"""Testes da conta de demonstracao.

A demo e uma conta como qualquer outra -- essa e a tese do desenho, e e
justamente ela que precisa ser provada. Se em algum ponto a demo escapar do
`get_current_user` ou do `get_carteira`, o isolamento do app inteiro passa a ter
uma excecao, e excecao em regra de autorizacao e onde vaza carteira alheia.

Por isso os testes aqui atacam, em ordem: que ela e isolada como qualquer conta,
que a validade e mesmo aplicada (e nao so gravada), e que ela nasce utilizavel.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.portfolio import Portfolio, TipoCarteira
from app.models.user import User
from app.services import demo_service
from tests.factories import criar_ativo, usuario_logado


async def _abrir_demo(client: AsyncClient) -> dict[str, str]:
    resp = await client.post("/auth/demo")
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestCriacao:
    async def test_devolve_token_utilizavel_sem_nenhuma_credencial(
        self, client: AsyncClient
    ) -> None:
        """O ponto da funcionalidade: nada de email, senha ou cadastro."""
        headers = await _abrir_demo(client)
        eu = await client.get("/auth/me", headers=headers)
        assert eu.status_code == 200
        assert eu.json()["is_demo"] is True

    async def test_a_carteira_e_simulada_e_nunca_real(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Confundir demonstracao com posicao de verdade e o unico erro que este
        projeto nao pode cometer: o tipo governa o rotulo na tela."""
        await _abrir_demo(client)
        carteiras = (
            (await db.execute(select(Portfolio).join(User).where(User.is_demo.is_(True))))
            .scalars()
            .all()
        )
        assert carteiras
        assert all(c.tipo is TipoCarteira.SIMULADA for c in carteiras)

    async def test_a_carteira_semeada_e_a_padrao(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Sem isto a demo abre VAZIA -- e foi o que aconteceu na primeira versao.

        `obter_padrao` criava uma "Carteira real" no primeiro request que nao
        informava carteira, e ela virava a padrao por ser do tipo REAL. Os dados
        semeados continuavam la, escondidos atras de um seletor que o visitante
        nao sabe que precisa mexer. A demonstracao abria na tela vazia que ela
        existe para evitar.
        """
        await criar_ativo(db, "WEGE3")
        headers = await _abrir_demo(client)

        resp = await client.get("/portfolios", headers=headers)
        nomes = [c["nome"] for c in resp.json()]
        assert nomes == [demo_service.NOME_DA_CARTEIRA]
        assert "Carteira real" not in nomes

    async def test_nasce_com_posicoes(self, client: AsyncClient, db: AsyncSession) -> None:
        """Carteira vazia nao demonstra nada."""
        for ticker, *_ in demo_service.SEMENTE:
            await criar_ativo(db, ticker)

        headers = await _abrir_demo(client)
        resp = await client.get("/portfolio/positions", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == len(demo_service.SEMENTE)

    async def test_ticker_ausente_do_catalogo_e_pulado_sem_quebrar(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Base recem-criada pode nao ter todos os papeis. Uma demo com menos
        ativos e melhor que uma demo que devolve 500."""
        await criar_ativo(db, demo_service.SEMENTE[0][0])
        headers = await _abrir_demo(client)
        resp = await client.get("/portfolio/positions", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_cada_chamada_cria_uma_conta_separada(self, client: AsyncClient) -> None:
        """Duas pessoas no mesmo link nao podem cair na mesma carteira: uma
        lancaria uma operacao e a outra veria aparecer na tela."""
        a = await _abrir_demo(client)
        b = await _abrir_demo(client)
        id_a = (await client.get("/auth/me", headers=a)).json()["id"]
        id_b = (await client.get("/auth/me", headers=b)).json()["id"]
        assert id_a != id_b


class TestIsolamento:
    async def test_a_demo_nao_enxerga_a_carteira_de_outra_conta(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A garantia que sustenta o desenho inteiro."""
        await criar_ativo(db, "PETR4")
        _, headers_real = await usuario_logado(client)
        carteira_real = (await client.get("/portfolios", headers=headers_real)).json()

        headers_demo = await _abrir_demo(client)
        alvo = carteira_real[0]["id"]
        resp = await client.get(f"/portfolio/positions?portfolio_id={alvo}", headers=headers_demo)
        # 404, nao 403: dizer "existe, mas nao e sua" confirmaria o id.
        assert resp.status_code == 404

    async def test_outra_conta_nao_enxerga_a_carteira_da_demo(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        headers_demo = await _abrir_demo(client)
        carteira_demo = (await client.get("/portfolios", headers=headers_demo)).json()[0]["id"]

        _, headers_real = await usuario_logado(client)
        resp = await client.get(
            f"/portfolio/positions?portfolio_id={carteira_demo}", headers=headers_real
        )
        assert resp.status_code == 404


class TestValidade:
    async def test_a_conta_nasce_com_prazo(self, client: AsyncClient, db: AsyncSession) -> None:
        await _abrir_demo(client)
        usuario = (await db.execute(select(User).where(User.is_demo.is_(True)))).scalars().one()
        assert usuario.expires_at is not None
        assert usuario.expires_at > datetime.now(UTC)

    async def test_conta_vencida_e_recusada_no_request_seguinte(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A validade vale de imediato, sem depender da faxina.

        Se dependesse, o refresh token renovaria a demo indefinidamente ate
        alguem criar a proxima demo -- e as "2 horas" seriam enfeite. Vale
        porque `get_current_user` rele o usuario do banco a cada request.
        """
        headers = await _abrir_demo(client)
        assert (await client.get("/auth/me", headers=headers)).status_code == 200

        usuario = (await db.execute(select(User).where(User.is_demo.is_(True)))).scalars().one()
        usuario.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

        assert (await client.get("/auth/me", headers=headers)).status_code == 401

    async def test_conta_normal_nunca_expira(self, client: AsyncClient, db: AsyncSession) -> None:
        """`expires_at` nulo precisa significar "para sempre". Se `expirou`
        devolvesse True para nulo, TODA conta do sistema seria deslogada."""
        email, headers = await usuario_logado(client)
        usuario = (await db.execute(select(User).where(User.email == email))).scalars().one()
        assert usuario.expires_at is None
        assert usuario.expirou is False
        assert (await client.get("/auth/me", headers=headers)).status_code == 200


class TestFaxina:
    async def test_remove_a_vencida_e_preserva_a_valida(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A faxina separa vencida de viva -- e nao apaga as duas.

        Sem a condicao de data, cada visitante novo derrubaria a sessao de quem
        estivesse com a demo aberta no mesmo momento.
        """
        headers_vencida = await _abrir_demo(client)
        id_vencida = (await client.get("/auth/me", headers=headers_vencida)).json()["id"]
        headers_viva = await _abrir_demo(client)
        id_viva = (await client.get("/auth/me", headers=headers_viva)).json()["id"]

        usuario = await db.get(User, id_vencida)
        assert usuario is not None
        usuario.expires_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()

        assert await demo_service.limpar_expiradas(db) == 1
        assert await db.get(User, id_vencida) is None
        assert await db.get(User, id_viva) is not None

    async def test_a_rota_faz_a_faxina_sozinha(self, client: AsyncClient, db: AsyncSession) -> None:
        """A limpeza roda na criacao da proxima demo, e nao num agendador: e o
        que faz o lixo ser recolhido sem depender de cron configurado no deploy.
        """
        headers = await _abrir_demo(client)
        id_vencida = (await client.get("/auth/me", headers=headers)).json()["id"]

        usuario = await db.get(User, id_vencida)
        assert usuario is not None
        usuario.expires_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()

        await _abrir_demo(client)  # sem chamar limpar_expiradas a mao

        db.expire_all()
        assert await db.get(User, id_vencida) is None

    async def test_nunca_toca_em_conta_de_verdade(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A protecao mais importante do modulo. O mesmo `ON DELETE CASCADE` que
        limpa a demo apagaria o historico inteiro de uma conta real."""
        email, _ = await usuario_logado(client)
        usuario = (await db.execute(select(User).where(User.email == email))).scalars().one()
        # Mesmo com um prazo VENCIDO gravado, uma conta nao-demo esta fora do
        # alcance da faxina: o filtro e `is_demo`, nao a data.
        usuario.expires_at = datetime.now(UTC) - timedelta(days=30)
        await db.commit()

        await demo_service.limpar_expiradas(db)
        assert (
            await db.execute(select(User).where(User.email == email))
        ).scalars().one_or_none() is not None

    async def test_leva_junto_carteiras_e_transacoes(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """O cascade e do BANCO, nao do ORM -- e `limpar_expiradas` usa um
        DELETE em massa, que nao carrega objeto nenhum e por isso ignoraria um
        cascade declarado so no `relationship`. Se alguem trocar a foreign key,
        este teste cai e as linhas ficariam orfas em silencio."""
        await criar_ativo(db, demo_service.SEMENTE[0][0])
        headers = await _abrir_demo(client)
        id_demo = (await client.get("/auth/me", headers=headers)).json()["id"]

        usuario = await db.get(User, id_demo)
        assert usuario is not None
        usuario.expires_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()

        await demo_service.limpar_expiradas(db)
        sobraram = (
            (await db.execute(select(Portfolio).where(Portfolio.user_id == id_demo)))
            .scalars()
            .all()
        )
        assert sobraram == []


class TestLimitesDaDemo:
    async def test_nao_gasta_cota_do_fornecedor(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A rota de proventos dispara N chamadas ao Yahoo com cota compartilhada.
        Aberta numa conta gratuita e anonima, qualquer visitante esgotaria a cota
        e derrubaria a sincronizacao da carteira de verdade junto."""
        await criar_ativo(db, "PETR4")
        headers = await _abrir_demo(client)
        resp = await client.post("/portfolio/dividends/sync", headers=headers)
        assert resp.status_code == 403

    async def test_a_conta_normal_continua_podendo(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """A guarda precisa barrar a demo SEM barrar quem paga a conta."""
        await criar_ativo(db, "PETR4")
        _, headers = await usuario_logado(client)
        resp = await client.post("/portfolio/dividends/sync", headers=headers)
        assert resp.status_code != 403

    async def test_a_demo_pode_editar_a_propria_carteira(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        """Foi a escolha explicita: populada E editavel. Se a demo fosse somente
        leitura, simular um aporte -- o motivo de alguem abrir isto -- nao
        funcionaria."""
        await criar_ativo(db, "PETR4")
        headers = await _abrir_demo(client)
        resp = await client.post(
            "/transactions",
            headers=headers,
            json={
                "ticker": "PETR4",
                "side": "compra",
                "quantity": "10",
                "price": "40.00",
                "traded_at": "2026-08-26",
            },
        )
        assert resp.status_code == 201, resp.text


class TestSenha:
    async def test_a_senha_nao_e_previsivel(self, client: AsyncClient, db: AsyncSession) -> None:
        """A conta so e acessivel pelos tokens devolvidos na criacao.

        Se a senha fosse fixa ou derivavel do email, qualquer um faria login
        numa demo alheia e leria o que aquele visitante lancou.
        """
        await _abrir_demo(client)
        await _abrir_demo(client)
        usuarios = (await db.execute(select(User).where(User.is_demo.is_(True)))).scalars().all()
        assert len(usuarios) == 2
        hashes = {u.hashed_password for u in usuarios}
        assert len(hashes) == 2
        emails = {u.email for u in usuarios}
        assert len(emails) == 2

    @pytest.mark.parametrize("senha", ["demo", "demo123", "portfolio", "example.com"])
    async def test_login_com_senha_obvia_nao_entra(
        self, client: AsyncClient, db: AsyncSession, senha: str
    ) -> None:
        await _abrir_demo(client)
        usuario = (await db.execute(select(User).where(User.is_demo.is_(True)))).scalars().one()
        resp = await client.post("/auth/login", data={"username": usuario.email, "password": senha})
        assert resp.status_code == 401
