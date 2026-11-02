"""Metricas de risco e retorno.

Modulo puro: recebe series de precos, devolve numeros. Sem banco, sem HTTP.

## Por que aqui usamos float, e nao Decimal

O resto da aplicacao usa `Decimal` porque dinheiro exige aritmetica exata: 0,01
que vira 0,009999 e centavo perdido, e centavo perdido e defeito.

Estatistica e outra coisa. Desvio-padrao envolve raiz quadrada, que e irracional
-- nao existe representacao decimal exata de sqrt(2), com nenhum numero de casas.
Insistir em Decimal aqui traria lentidao (ordens de magnitude) sem trazer exatidao,
porque a exatidao ja e impossivel na primeira raiz. E volatilidade de 23,4% ou
23,4000001% e a mesma decisao de investimento; R$ 1000,00 ou R$ 999,99 nao e.

A fronteira e explicita: `Decimal` entra, `float` circula aqui dentro, e o que
sai e rotulado como estatistica -- nunca volta a ser tratado como dinheiro.

## Convencoes

- **252 pregoes por ano**, nao 365. A B3 nao negocia fim de semana nem feriado;
  anualizar com 365 infla a volatilidade em cerca de 20%.
- **Retornos, nao precos.** Correlacao entre series de PRECOS de duas acoes
  quase sempre da alto, porque ambas sobem com a inflacao e com o mercado --
  e uma correlacao espuria. A relacao que interessa e entre as VARIACOES.
- **Desvio-padrao amostral (ddof=1)**, nao populacional. Temos uma amostra do
  historico, nao a populacao de todos os retornos possiveis; ddof=0 subestima
  o risco sistematicamente.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal

import numpy as np

# Pregoes por ano na B3. O numero exato varia (250 a 253 conforme os feriados);
# 252 e a convencao de mercado.
PREGOES_POR_ANO = 252

# Minimo de retornos para uma estatistica significar alguma coisa. Com 5
# observacoes, o desvio-padrao e ruido -- devolver esse numero como "risco"
# seria pior que devolver nada, porque parece informacao.
MINIMO_OBSERVACOES = 20


@dataclass(frozen=True)
class SeriesAlinhadas:
    """Series de precos com a garantia ESTRUTURAL de estarem alinhadas.

    ## Por que um tipo, e nao um comentario

    A versao anterior deste modulo recebia `dict[str, np.ndarray]` e a docstring
    dizia "pressupoe series ja alinhadas". Isso nao e uma garantia -- e um pedido.
    Duas series de mesmo tamanho mas de periodos diferentes passavam sem erro e
    devolviam uma correlacao que era puro ruido. Nada estourava.

    Com este tipo, a validacao acontece na CONSTRUCAO: se o objeto existe, ele
    esta alinhado. Nao ha caminho no codigo que produza uma instancia invalida, e
    nenhuma funcao adiante precisa reconferir. E a diferenca entre "confie em
    mim" e "e impossivel errar".

    Invariantes garantidas:
      - toda serie tem exatamente um preco por data;
      - as datas estao em ordem cronologica estrita, sem repeticao;
      - ha pelo menos uma data.
    """

    datas: tuple[date_type, ...]
    precos: Mapping[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.datas:
            raise ValueError("series alinhadas precisam de pelo menos uma data")

        # Ordem cronologica estrita. Retorno diario e P_t / P_{t-1}: com as datas
        # fora de ordem o calculo roda e devolve ruido. Data repetida significa
        # dois precos para o mesmo dia -- dado corrompido na origem.
        for anterior, seguinte in zip(self.datas, self.datas[1:], strict=False):
            if seguinte <= anterior:
                raise ValueError(f"datas fora de ordem ou repetidas: {anterior} -> {seguinte}")

        for ticker, serie in self.precos.items():
            if len(serie) != len(self.datas):
                raise ValueError(
                    f"{ticker}: {len(serie)} precos para {len(self.datas)} datas "
                    "-- serie desalinhada"
                )

    @property
    def tickers(self) -> list[str]:
        """Sempre ordenados: quem consome a matriz indexa por posicao, e ordem
        instavel trocaria os ativos silenciosamente entre duas chamadas."""
        return sorted(self.precos)

    @property
    def inicio(self) -> date_type:
        return self.datas[0]

    @property
    def fim(self) -> date_type:
        return self.datas[-1]

    def __len__(self) -> int:
        return len(self.datas)

    def subconjunto(self, tickers: Sequence[str]) -> SeriesAlinhadas:
        """Recorta alguns ativos preservando o alinhamento.

        Necessario porque o filtro de "historico suficiente" descarta ativos
        depois do carregamento -- e reconstruir um dicionario a mao ali seria
        justamente a brecha por onde o desalinhamento voltaria.
        """
        return SeriesAlinhadas(
            datas=self.datas,
            precos={t: self.precos[t] for t in tickers if t in self.precos},
        )


VAZIO = SeriesAlinhadas(datas=(date_type(1970, 1, 1),), precos={})


@dataclass(frozen=True)
class MetricasAtivo:
    ticker: str
    observacoes: int
    retorno_periodo: float
    retorno_anualizado: float
    volatilidade_anualizada: float
    indice_sharpe: float | None
    maior_queda: float


def retornos_diarios(precos: np.ndarray) -> np.ndarray:
    """Retorno simples: r_t = P_t / P_{t-1} - 1.

    Simples, nao logaritmico. Retorno logaritmico e aditivo no tempo (soma-se ao
    longo dos dias), o que e conveniente; o simples e aditivo na CARTEIRA (a
    media ponderada dos retornos dos ativos e o retorno da carteira). Como o
    proximo passo e otimizacao de carteira (Etapa 9), a segunda propriedade e a
    que importa -- e usar log ali produziria pesos sutilmente errados.
    """
    resultado: np.ndarray = precos[1:] / precos[:-1] - 1.0
    return resultado


def volatilidade_anualizada(retornos: np.ndarray) -> float:
    """Desvio-padrao dos retornos diarios, escalado para o ano.

    A raiz de 252 vem da propriedade de que a variancia de retornos independentes
    cresce linearmente com o tempo -- entao o desvio-padrao cresce com a raiz.
    Multiplicar por 252 (em vez da raiz) e um erro comum e infla o numero em
    quinze vezes.
    """
    if len(retornos) < 2:
        return 0.0
    return float(np.std(retornos, ddof=1) * np.sqrt(PREGOES_POR_ANO))


def retorno_anualizado(precos: np.ndarray) -> float:
    """Retorno geometrico anualizado (CAGR sobre o periodo observado).

    Geometrico, nao a media aritmetica dos retornos diarios. A diferenca nao e
    academica: uma acao que cai 50% e depois sobe 50% tem media aritmetica de 0%
    e resultado real de -25%. A media aritmetica mente sistematicamente para
    cima, e quanto mais volatil o ativo, mais ela mente.
    """
    if len(precos) < 2:
        return 0.0
    periodos = len(precos) - 1
    total = float(precos[-1] / precos[0])
    if total <= 0:
        return -1.0
    return float(total ** (PREGOES_POR_ANO / periodos) - 1.0)


def indice_sharpe(
    retorno_anual: float, volatilidade: float, taxa_livre_risco: float
) -> float | None:
    """(retorno - taxa livre de risco) / volatilidade.

    A taxa livre de risco NAO e zero no Brasil, e essa e a diferenca em relacao a
    maior parte do material estrangeiro. Com o CDI perto de dois digitos, uma
    acao que rendeu 8% no ano teve Sharpe NEGATIVO -- entregou menos que o
    Tesouro Selic assumindo risco de renda variavel. Usar rf=0, como se faz em
    exemplos americanos, transformaria esse caso em "Sharpe positivo, bom
    investimento".

    Devolve None com volatilidade zero: dividir por zero daria infinito, e
    "Sharpe infinito" e um numero que nao significa nada.
    """
    if volatilidade <= 0:
        return None
    return (retorno_anual - taxa_livre_risco) / volatilidade


def maior_queda(precos: np.ndarray) -> float:
    """Maximum drawdown: a maior perda do topo ate o fundo seguinte.

    Complementa a volatilidade, que trata alta e baixa como o mesmo "risco".
    Nenhum investidor perde o sono porque o ativo subiu demais; o que doi e a
    queda -- e este numero mede exatamente isso.
    """
    if len(precos) < 2:
        return 0.0
    maximos = np.maximum.accumulate(precos)
    return float(np.min(precos / maximos - 1.0))


def metricas_do_ativo(
    ticker: str, precos: np.ndarray, taxa_livre_risco: float
) -> MetricasAtivo | None:
    """Devolve None se nao houver historico suficiente."""
    retornos = retornos_diarios(precos)
    if len(retornos) < MINIMO_OBSERVACOES:
        return None

    anual = retorno_anualizado(precos)
    vol = volatilidade_anualizada(retornos)
    return MetricasAtivo(
        ticker=ticker,
        observacoes=len(precos),
        retorno_periodo=float(precos[-1] / precos[0] - 1.0),
        retorno_anualizado=anual,
        volatilidade_anualizada=vol,
        indice_sharpe=indice_sharpe(anual, vol, taxa_livre_risco),
        maior_queda=maior_queda(precos),
    )


def matriz_de_retornos(series: SeriesAlinhadas) -> tuple[list[str], np.ndarray]:
    """Empilha os retornos num array (dias x ativos), na ordem dos tickers.

    Recebe `SeriesAlinhadas`, nao um dicionario: o alinhamento ja foi verificado
    na construcao do objeto, entao esta funcao nao precisa confiar em ninguem nem
    reconferir nada. Um `dict[str, ndarray]` sequer compila aqui.
    """
    tickers = series.tickers
    retornos = np.column_stack([retornos_diarios(series.precos[t]) for t in tickers])
    return tickers, retornos


def matriz_correlacao(series: SeriesAlinhadas) -> tuple[list[str], np.ndarray]:
    """Correlacao de Pearson entre os RETORNOS.

    O numero que responde "diversificar nestes dois ativos adianta?". Correlacao
    perto de 1 significa que eles caem juntos -- e carteira que cai junta nao e
    carteira diversificada, por mais nomes que tenha.
    """
    tickers, retornos = matriz_de_retornos(series)
    # rowvar=False: cada COLUNA e uma variavel (um ativo), cada linha um dia.
    # O padrao do numpy e o contrario, e trocar isso devolve uma matriz de
    # correlacao entre DIAS -- com a forma certa e o significado errado.
    return tickers, np.corrcoef(retornos, rowvar=False)


def matriz_covariancia(
    series: SeriesAlinhadas, anualizar: bool = True
) -> tuple[list[str], np.ndarray]:
    """Covariancia dos retornos -- a entrada do otimizador de Markowitz.

    Anualizada por padrao para ficar na mesma unidade do retorno anual. Misturar
    covariancia diaria com retorno anual e um erro que passa despercebido: as
    contas fecham, os pesos saem, e estao errados por um fator de 252.
    """
    tickers, retornos = matriz_de_retornos(series)
    # Com um unico ativo, np.cov devolve um escalar 0-d; atleast_2d normaliza
    # para (1, 1) e mantem o resto do codigo indiferente ao tamanho.
    cov = np.atleast_2d(np.cov(retornos, rowvar=False, ddof=1))
    if anualizar:
        cov = cov * PREGOES_POR_ANO
    return tickers, cov


# Fracao minima dos pregoes da janela que precisa ter a variacao do indice.
#
# Todo dia em que os ativos negociaram, a B3 tambem calculou o Ibovespa; se o
# indice falta em mais de 5% deles, o problema e a serie (fornecedor fora do ar,
# cache incompleto), e um beta medido sobre esse buraco nao descreve a janela
# que o resto do calculo usa.
COBERTURA_MINIMA_INDICE = 0.95


def alinhar_com_indice(
    series: SeriesAlinhadas,
    variacoes: Mapping[date_type, float],
    *,
    cobertura_minima: float = COBERTURA_MINIMA_INDICE,
) -> tuple[SeriesAlinhadas, np.ndarray] | None:
    """Recorta as series aos dias que tambem tem indice, e devolve o NIVEL dele.

    O indice chega como variacao diaria (como o CDI), nao como preco. Para
    compara-lo com os ativos ele vira um nivel acumulado -- o preco de uma cota
    que rende exatamente o indice -- e o retorno sai do mesmo jeito que o de um
    ativo, entre as MESMAS duas datas. Se um ativo nao negociou num dia em que o
    indice andou, o retorno do indice entre as duas datas restantes ja inclui
    aquele dia, porque o nivel acumula tudo.

    Devolve None quando a cobertura e insuficiente: melhor dizer "faltou o
    Ibovespa" do que medir beta sobre metade da janela.
    """
    datas_do_indice = sorted(d for d in variacoes if series.inicio <= d <= series.fim)
    nivel: dict[date_type, float] = {}
    acumulado = 1.0
    for dia in datas_do_indice:
        acumulado *= 1.0 + float(variacoes[dia])
        nivel[dia] = acumulado

    manter = [i for i, dia in enumerate(series.datas) if dia in nivel]
    if len(manter) < 2 or len(manter) < cobertura_minima * len(series.datas):
        return None

    indices = np.array(manter)
    datas = tuple(series.datas[i] for i in manter)
    recortadas = SeriesAlinhadas(
        datas=datas, precos={t: series.precos[t][indices] for t in series.tickers}
    )
    return recortadas, np.array([nivel[d] for d in datas], dtype=np.float64)


def covariancia_com_indice(
    series: SeriesAlinhadas, nivel_do_indice: np.ndarray, anualizar: bool = True
) -> tuple[list[str], np.ndarray, np.ndarray, float]:
    """(tickers, Sigma dos ativos, cov de cada ativo com o indice, var do indice).

    As tres saem de UMA matriz de covariancia conjunta, sobre os mesmos dias.
    Calcular cada pedaco numa amostra diferente daria um beta de um periodo
    aplicado ao risco de outro -- os numeros fecham e o equilibrio sai torto.
    """
    if len(nivel_do_indice) != len(series):
        raise ValueError(
            f"indice com {len(nivel_do_indice)} pontos para {len(series)} datas -- desalinhado"
        )
    tickers, retornos = matriz_de_retornos(series)
    conjunta = np.cov(
        np.column_stack([retornos, retornos_diarios(nivel_do_indice)]), rowvar=False, ddof=1
    )
    if anualizar:
        conjunta = conjunta * PREGOES_POR_ANO
    n = len(tickers)
    return tickers, conjunta[:n, :n], conjunta[:n, n], float(conjunta[n, n])


def para_float(valores: list[Decimal]) -> np.ndarray:
    """Fronteira explicita entre o mundo Decimal e o mundo estatistico.

    Uma unica funcao faz a conversao, e ela tem nome. Espalhar `float(x)` pelo
    codigo apaga a fronteira e e assim que um float acaba voltando para um
    calculo de dinheiro.
    """
    return np.array([float(v) for v in valores], dtype=np.float64)
