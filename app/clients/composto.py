"""Encadeamento de fornecedores com degradacao gradual."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.clients.base import Cotacao, ProvedorDeCotacoes

logger = logging.getLogger(__name__)


class ProvedorEncadeado:
    """Tenta os fornecedores em ordem, pedindo ao seguinte so o que faltou.

    Nao e "se o primeiro falhar, use o segundo": e "o segundo completa as lacunas
    do primeiro". A diferenca importa numa carteira mista -- a brapi pode
    responder 28 dos 30 ativos e nao conhecer os outros 2. Repetir os 30 no
    fallback gastaria requisicoes a toa; pedir so os 2 que faltam e o correto.

    Se todos falharem, devolve o que tiver -- possivelmente nada. Quem chama
    trata a ausencia; nunca ha excecao borbulhando para o usuario porque um
    fornecedor externo saiu do ar.
    """

    def __init__(self, *provedores: ProvedorDeCotacoes) -> None:
        self._provedores = provedores

    @property
    def nome(self) -> str:
        return "+".join(p.nome for p in self._provedores)

    async def cotacoes(self, tickers: Sequence[str]) -> dict[str, Cotacao]:
        encontradas: dict[str, Cotacao] = {}
        pendentes = list(tickers)

        for provedor in self._provedores:
            if not pendentes:
                break
            try:
                novas = await provedor.cotacoes(pendentes)
            except Exception:
                # Rede de seguranca: mesmo que um adaptador deixe escapar algo
                # inesperado, a falha de UM fornecedor nao pode impedir que os
                # seguintes sejam tentados.
                logger.exception("[cotacoes] provedor %s falhou", provedor.nome)
                continue
            encontradas.update(novas)
            pendentes = [t for t in pendentes if t not in encontradas]

        if pendentes:
            logger.info("[cotacoes] sem cotacao para: %s", pendentes)
        return encontradas
