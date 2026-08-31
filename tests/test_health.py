"""Testes dos endpoints de infraestrutura."""

from __future__ import annotations

from httpx import AsyncClient


async def test_liveness(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_readiness_consulta_o_banco(client: AsyncClient) -> None:
    assert (await client.get("/health/ready")).status_code == 200


async def test_health_nao_vaza_infraestrutura(client: AsyncClient) -> None:
    """Endpoint publico: nao pode devolver host, porta, versao de biblioteca nem
    string de conexao. Tudo isso e informacao de graca para quem mapeia o alvo."""
    corpo = (await client.get("/health")).text.lower()
    for vazamento in ("postgres", "asyncpg", "localhost", "5432", "password", "uvicorn"):
        assert vazamento not in corpo


async def test_cabecalhos_de_seguranca_em_toda_resposta(client: AsyncClient) -> None:
    h = (await client.get("/health")).headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    # 'unsafe-inline' em script anularia a protecao contra XSS -- o ponto do CSP.
    csp = h["content-security-policy"]
    script_src = next(d for d in csp.split(";") if "script-src" in d)
    assert "unsafe-inline" not in script_src
    assert "unsafe-eval" not in script_src
