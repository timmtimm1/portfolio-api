"""Testes de log estruturado, ID de correlação e métricas.

Observabilidade é o tipo de código que ninguém percebe quebrado até precisar
dele -- e precisar dele é sempre no meio de um incidente. Por isso ela tem
teste como o resto.
"""

from __future__ import annotations

import json
import logging

from httpx import AsyncClient

from app.core.logging import FormatadorJson, id_da_requisicao


def _registro(mensagem: str = "teste", **extras: object) -> logging.LogRecord:
    r = logging.LogRecord("app.teste", logging.INFO, __file__, 1, mensagem, None, None)
    for chave, valor in extras.items():
        setattr(r, chave, valor)
    return r


class TestFormatadorJson:
    def test_produz_json_valido(self) -> None:
        d = json.loads(FormatadorJson().format(_registro("oi")))
        assert d["mensagem"] == "oi"
        assert d["nivel"] == "INFO"
        assert d["logger"] == "app.teste"

    def test_a_hora_vai_em_utc(self) -> None:
        """Fuso do servidor é detalhe de infraestrutura. Comparar horários entre
        máquinas com fusos diferentes é fonte clássica de investigação perdida."""
        d = json.loads(FormatadorJson().format(_registro()))
        assert d["hora"].endswith("+00:00")

    def test_campos_extras_entram_sozinhos(self) -> None:
        """Adicionar um campo novo não pode exigir mexer no formatador."""
        d = json.loads(FormatadorJson().format(_registro(rota="/x", status=200)))
        assert d["rota"] == "/x" and d["status"] == 200

    def test_inclui_o_id_de_correlacao_quando_existe(self) -> None:
        token = id_da_requisicao.set("abc123")
        try:
            d = json.loads(FormatadorJson().format(_registro()))
            assert d["request_id"] == "abc123"
        finally:
            id_da_requisicao.reset(token)

    def test_omite_o_id_fora_de_uma_requisicao(self) -> None:
        """Startup, job de snapshot e migration também logam, e ali não há
        correlação. Um campo vazio poluiria toda linha."""
        assert "request_id" not in json.loads(FormatadorJson().format(_registro()))

    def test_objeto_estranho_nao_derruba_o_log(self) -> None:
        """O log nunca pode ser a causa da falha: um objeto não serializável
        viraria exceção DENTRO do tratamento de erro, escondendo o problema
        original."""
        d = json.loads(FormatadorJson().format(_registro(bicho=object())))
        assert "bicho" in d


class TestIdDeCorrelacao:
    async def test_toda_resposta_traz_o_id(self, client: AsyncClient) -> None:
        """ "Me manda o X-Request-ID" transforma uma investigação numa consulta."""
        resp = await client.get("/health")
        assert resp.headers["X-Request-ID"]

    async def test_o_id_do_cliente_e_reaproveitado(self, client: AsyncClient) -> None:
        """É o que permite seguir um pedido atravessando vários serviços. Gerar
        um novo aqui quebraria a corrente exatamente onde ela importa."""
        resp = await client.get("/health", headers={"X-Request-ID": "de-fora-123"})
        assert resp.headers["X-Request-ID"] == "de-fora-123"

    async def test_cada_requisicao_tem_o_seu(self, client: AsyncClient) -> None:
        a = (await client.get("/health")).headers["X-Request-ID"]
        b = (await client.get("/health")).headers["X-Request-ID"]
        assert a != b


class TestMetricas:
    async def test_expoe_o_formato_do_prometheus(self, client: AsyncClient) -> None:
        resp = await client.get("http://test/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "http_requisicoes_total" in resp.text

    async def test_conta_a_requisicao(self, client: AsyncClient) -> None:
        await client.get("/health")
        corpo = (await client.get("http://test/metrics")).text
        assert 'rota="/health"' in corpo

    async def test_o_rotulo_e_a_rota_e_nao_a_url(self, client: AsyncClient, db: object) -> None:
        """O teste que protege contra a explosão de cardinalidade.

        Cada combinação de rótulos vira uma série temporal em memória. Usar a
        URL (`/transactions/9f2c...`) criaria uma série POR TRANSAÇÃO --
        milhares de séries de um ponto cada, e o processo cresce até morrer.

        O rótulo tem que ser `/transactions/{transacao_id}`, com o parâmetro
        ainda entre chaves.
        """
        from tests.factories import usuario_logado

        _, h = await usuario_logado(client)
        await client.get("/transactions/00000000-0000-0000-0000-000000000000", headers=h)

        corpo = (await client.get("http://test/metrics")).text
        assert "00000000-0000-0000-0000-000000000000" not in corpo
        assert "{transacao_id}" in corpo

    async def test_erro_tambem_e_contado(self, client: AsyncClient) -> None:
        """Registrar só o caminho feliz produz um painel que fica verde
        justamente durante o incidente."""
        await client.get("/portfolio/summary")  # sem token: 401
        corpo = (await client.get("http://test/metrics")).text
        assert 'status="401"' in corpo

    async def test_fora_do_schema_publico(self, client: AsyncClient) -> None:
        """Não é rota de produto, é infraestrutura. Na documentação pública só
        confundiria quem lê a API para integrar."""
        schema = (await client.get("http://test/api/v1/openapi.json")).json()
        assert "/metrics" not in schema["paths"]

    async def test_rota_que_levanta_ainda_e_registrada(self) -> None:
        """O `try/finally` do middleware, testado no único caminho que o exige.

        Uma rota que levanta não devolve resposta -- se a métrica só fosse
        registrada depois do `return`, a exceção pularia por cima dela. O
        painel ficaria verde exatamente durante o incidente, que é quando
        alguém finalmente vai olhar para ele.
        """
        from prometheus_client import REGISTRY
        from starlette.requests import Request

        from app.core.middleware import ObservabilidadeMiddleware

        def contagem() -> float:
            return (
                REGISTRY.get_sample_value(
                    "http_requisicoes_total",
                    {"metodo": "GET", "rota": "desconhecida", "status": "500"},
                )
                or 0.0
            )

        antes = contagem()

        async def explode(_: Request) -> None:
            raise RuntimeError("falha simulada dentro da rota")

        mw = ObservabilidadeMiddleware(app=lambda *a, **k: None)  # type: ignore[arg-type]
        pedido = Request({"type": "http", "method": "GET", "path": "/x", "headers": []})

        try:
            await mw.dispatch(pedido, explode)  # type: ignore[arg-type]
        except RuntimeError:
            pass  # a exceção DEVE continuar subindo; o middleware não a engole

        assert contagem() == antes + 1, "erro nao foi contado"
