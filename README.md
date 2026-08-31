# Portfolio Tracker API

API REST para acompanhamento de carteira de investimentos da B3: o usuário registra
suas compras e vendas, e o sistema calcula posição, rentabilidade, risco e sugere
alocações usando a fronteira eficiente de Markowitz.

> **Em construção.** O projeto está sendo desenvolvido em etapas curtas e verificáveis;
> o quadro abaixo mostra onde ele está.

| # | Etapa | Estado |
|---|---|---|
| 0 | Fundação: FastAPI, Docker, configuração *fail-closed* | ✅ |
| 1 | Camada de dados async, Alembic, model `User` | ✅ |
| 2 | Registro e login (argon2id + JWT) | ✅ |
| 3 | Sessão: rotação de refresh token, detecção de reuso, rate limit | ✅ |
| 4 | Suíte de testes com Postgres efêmero + CI | ✅ |
| 5 | Catálogo de ativos da B3 e carga histórica | ✅ |
| 6 | Livro de transações e cálculo de posição | ✅ |
| 7 | Cotações (brapi/yfinance) com cache | ✅ |
| 8 | Métricas: retorno, volatilidade, correlação | ✅ |
| 9 | Otimização de Markowitz | ✅ |
| 10 | Snapshots diários da carteira | ✅ |
| 11 | Frontend com o gráfico da fronteira | ✅ |
| 12 | Deploy | ⬜ |

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2.0 (async, asyncpg) · Alembic · PostgreSQL 17 ·
pytest + testcontainers · uv · ruff · mypy strict

## Arquitetura

As dependências apontam sempre para baixo. A camada de serviço não importa FastAPI,
e o módulo de segurança não conhece banco nem models — é o que permite reusar as
regras num job de cron e testar a criptografia de forma exaustiva, sem Postgres.

```
routers/    HTTP: status code, cabeçalho, cookie
   ↓
schemas/    validação e normalização da entrada (≠ o que está no banco)
   ↓
services/   regras de negócio; levantam exceções de domínio, não HTTPException
   ↓
models/     tabelas
   ↓
core/       config, sessão de banco, segurança, dependências
```

## Regras de negócio

**Custo médio ponderado, a regra brasileira.** A venda **não altera o preço médio** —
reduz quantidade e custo proporcionalmente, deixando a divisão intacta. Quem implementa
FIFO por hábito de mercado estrangeiro produz preço médio errado e, com ele, imposto
errado. Taxas de compra entram no custo de aquisição; taxas de venda saem do resultado.

**A posição é derivada, nunca armazenada.** Quantidade e preço médio são reconstruídos
do livro a cada consulta. Uma coluna de saldo seria mais rápida e criaria duas fontes da
verdade — e quando elas divergissem, ninguém saberia qual está certa.

**Validação retroativa.** Lançar uma operação recalcula o livro inteiro daquele ativo em
ordem cronológica: uma venda com data antiga pode ser inválida mesmo com a posição de
hoje sendo positiva. Conferir só o saldo atual deixaria esse caso passar.

## Cotações

brapi.dev como fonte primária, Yahoo Finance completando as lacunas, e **cache em
tabela com TTL de 15 minutos**. Medido nesta máquina, com cotação real:

| | tempo |
|---|---|
| cache vazio (chama o fornecedor) | 3.740 ms |
| cache dentro do TTL | **8 ms** |

Chamar a API externa a cada request faria o endpoint depender da latência e da
disponibilidade de um terceiro — e a cota gratuita (15 mil chamadas/mês) evaporaria com
poucos usuários. O cache vive no banco, não em memória: com vários workers, um cache em
memória duplicaria as chamadas.

Quando **nenhum** fornecedor responde, a carteira ainda é devolvida — com o cache vencido
se houver, e os tickers afetados listados em `sem_cotacao`. Degradar é melhor que falhar.

## Métricas de risco

Retorno anualizado, volatilidade, Sharpe, maior queda e matriz de correlação, calculados
sobre o histórico no banco. Resultado com dados reais (249 pregões):

```
ativo     ret. ano   volat.   Sharpe   maior queda
VALE3       54.3%    25.6%     1.73        -20.2%
PETR4       49.3%    25.3%     1.55        -22.2%
ITUB4       17.2%    24.0%     0.30        -22.5%
BBAS3       -3.2%    28.2%    -0.47        -34.8%
```

Convenções, cada uma com um erro comum associado:

- **252 pregões por ano, não 365.** A B3 não negocia fim de semana nem feriado;
  anualizar com 365 infla a volatilidade em ~20%. E a anualização usa a **raiz** de 252
  — multiplicar por 252 infla o número em quase 16 vezes.
- **Retorno geométrico, não média aritmética.** Cai 50%, sobe 50%: a média aritmética
  diz 0%, o resultado real é −25%. A média mente sistematicamente para cima.
- **Taxa livre de risco ≠ zero.** No Brasil é o CDI/Selic. Com o CDI a 10%, o BBAS3
  acima tem Sharpe **negativo** — rendeu menos que o Tesouro Selic assumindo risco de
  renda variável. Usar `rf=0`, como em exemplos americanos, o tornaria positivo.
- **Correlação entre retornos, não entre preços.** Séries de preços de duas ações quase
  sempre correlacionam alto porque ambas sobem com o mercado — correlação espúria.
- **Séries alinhadas pela interseção das datas**, e o alinhamento é garantido pelo
  **tipo**, não por convenção. Correlacionar históricos de tamanhos ou períodos diferentes
  produz um número com a forma certa e o significado errado — e nada estoura. Por isso os
  cálculos recebem `SeriesAlinhadas`, cujas invariantes (um preço por data, datas em ordem
  estrita, sem repetição) são verificadas na construção: se o objeto existe, está alinhado,
  e nenhuma função adiante precisa reconferir.
- **Desvio-padrão amostral (`ddof=1`)**, que não subestima o risco.

`Decimal` para dinheiro, `float` para estatística — a fronteira é uma função com nome
(`para_float`), não `float(x)` espalhado pelo código.

## Comparação com CDI e Selic

O gráfico de evolução traz a curva do indexador ao lado da carteira, com a resposta que
todo investidor brasileiro quer: **bati o CDI?**

A curva **não** é a taxa acumulada pura. É *"se eu tivesse posto o mesmo dinheiro, nos
mesmos dias, no CDI, quanto teria hoje?"*:

```
equivalente[0] = custo[0]
equivalente[t] = equivalente[t-1] × (1 + taxa_do_dia) + aporte[t]
```

A diferença importa quando há aportes: aplicar a taxa só sobre o valor inicial subestima
o benchmark e faz a carteira parecer melhor do que foi. E o aporte entra **depois** de
render — dinheiro que chegou hoje não estava aplicado ontem.

### O gráfico é percentual, e usa TWR

Em reais, uma carteira que cresceu esmaga a escala e o CDI vira uma linha reta sem
informação. Em percentual, as duas curvas partem de 0% e a comparação fica legível.

Mas o percentual **não** é `valor_mercado / custo − 1`. Esse número despenca a cada
aporte, sem o mercado ter mexido:

```
dia 1: investe 1.000, vale 1.100          →  +10,0%
dia 2: aporta 1.000, mercado parado
       vale 2.100, custo 2.000            →   +5,0%   ← caiu pela metade!
```

Um gráfico assim mostraria quedas que nunca aconteceram, justamente nos dias em que a
pessoa investiu mais. Usamos **retorno ponderado pelo tempo (TWR)**, que isola o efeito do
mercado:

```
r[t] = (valor[t] − aporte[t]) / valor[t-1] − 1
acumulado[t] = acumulado[t-1] × (1 + r[t])
```

No exemplo: `(2.100 − 1.000) / 1.100 − 1 = 0%`, e o acumulado segue +10%. É a medida que
fundos reportam, e a única comparável com o CDI acumulado. O botão `R$` alterna para a
escala em reais quando o que interessa é o patrimônio, não o desempenho.

Fonte: [SGS do Banco Central](https://api.bcb.gov.br) (séries 12 e 11), oficial, pública e
sem token. Taxa passada não muda, então é gravada uma vez e nunca mais buscada — sem TTL,
só preenchimento de lacunas. Dia sem taxa publicada (fim de semana, feriado) não rende,
que é o comportamento real do CDI.

## Otimização de Markowitz

Implementada **na mão** com `scipy.optimize` (SLSQP), não com uma biblioteca de
otimização pronta. Resolve, para cada retorno-alvo:

```
minimizar    w' Σ w              (variância da carteira)
sujeito a    w' μ  = μ*          (atinge o retorno desejado)
             soma(w) = 1         (investe todo o capital)
             0 ≤ wᵢ ≤ limite     (sem venda a descoberto, sem concentrar)
```

Resultado com dados reais da B3 (249 pregões, teto de 35% por ativo):

```
              ativos individuais:  volatilidade 24% a 29%

MÍNIMA VARIÂNCIA   retorno 42.0%   volatilidade 14.1%   Sharpe 2.28
  PETR4 33.9%  ABEV3 20.0%  VALE3 19.8%  WEGE3 14.5%  ITUB4 11.8%

MÁXIMO SHARPE      retorno 47.3%   volatilidade 14.6%   Sharpe 2.55
  PETR4 35.0%  VALE3 35.0%  WEGE3 15.1%  ABEV3 14.9%
```

A volatilidade cai de ~25% (ativos isolados) para **14,1%** na carteira — esse é o
efeito que Markowitz formalizou: o risco de uma carteira não é a média dos riscos, e sim
função de como os ativos se movem juntos.

**Validação por três caminhos independentes** (`tests/test_optimizer.py`):

1. **Fórmula fechada.** A mínima variância sem restrições tem solução analítica exata
   (`w = Σ⁻¹1 / 1'Σ⁻¹1`). Diferença medida: **1.7e-15**.
2. **Propriedades.** Nenhuma de 500 carteiras aleatórias tem variância menor que a
   encontrada; nenhuma de 300 tem Sharpe maior.
3. **`skfolio`.** Implementação independente e madura, com as mesmas restrições: pesos
   diferem em <2e-3, Sharpe em **1.9e-9**.

**Limitações que o código não esconde.** O retorno esperado é estimado sobre o histórico,
e essa é a parte frágil do modelo — pequenas mudanças na janela produzem carteiras bem
diferentes. Por isso a carteira de **mínima variância é mais confiável** que a de máximo
Sharpe: ela não usa retorno esperado, só covariância, que é bem mais estável. E por isso
existe o limite por ativo: sem ele, o otimizador aloca quase tudo no papel que mais subiu
na amostra — ótimo para o passado, o oposto de diversificar. A resposta sempre inclui um
campo `aviso` com essa ressalva.

## Snapshots diários

Um job no GitHub Actions fotografa todas as carteiras às 18h15 (Brasília), depois do
fechamento da B3. É o **único dado do sistema que não é reconstruível**: a posição sai do
livro a qualquer momento, mas o valor de mercado de ontem dependia da cotação de ontem,
que já foi sobrescrita no cache.

- **Idempotência imposta pelo schema**: a chave primária `(user_id, date)` garante uma
  foto por dia. Rodar duas vezes atualiza a linha com a cotação mais recente — o cron
  pode ter nova tentativa sem risco de duplicar histórico.
- **Uma busca de cotação para todos os usuários.** Buscar por usuário seria N+1 contra a
  API externa: 100 usuários com PETR4 = 100 consultas do mesmo preço, e a cota gratuita
  de 15 mil chamadas/mês estoura em dias.
- **Autenticação de máquina, não de pessoa.** Chave de serviço de 384 bits em cabeçalho
  (nunca na URL — URLs vão para log de acesso e de proxy), comparada com
  `secrets.compare_digest`. Uma comparação normal para no primeiro byte diferente, e essa
  diferença de tempo é mensurável pela rede: o atacante descobre a chave um caractere por
  vez. Sem chave configurada, a rota devolve **404** — fail-closed.
- **Menor privilégio nos dois sentidos**: a chave de serviço não lê carteira de ninguém,
  e o token de usuário não dispara o job.

## Decisões de segurança

Cada uma está justificada na docstring do módulo correspondente.

- **A aplicação não sobe sem segredo.** `SECRET_KEY` e a senha do banco não têm valor
  padrão; uma chave com menos de 32 caracteres é rejeitada no boot. Variável de
  ambiente com typo vira erro de inicialização (`extra="forbid"`), não um segredo
  silenciosamente ignorado.
- **Segredos não vazam em log.** `SecretStr` mascara em `repr()`, e a URL do banco é
  um objeto `URL` do SQLAlchemy, que oculta a senha em traceback e em mensagem de
  erro de conexão.
- **Senha com argon2id** (`pwdlib`), com regravação automática do hash no login quando
  os parâmetros mudam. Limite de 128 caracteres na entrada: sem teto, uma senha de
  megabytes vira negação de serviço no hash.
- **Login em tempo constante.** Email inexistente paga o custo de um argon2 descartável.
  Sem isso, a resposta em ~2 ms denunciaria quais emails têm conta, mesmo com a
  mensagem de erro idêntica.
- **JWT com `algorithms` fixo** e claim `typ`: barra o ataque `alg=none` e a confusão
  entre token de acesso e de renovação.
- **Refresh token não é JWT.** É um valor opaco de 384 bits, guardado no banco apenas
  como SHA-256 — um dump vazado não contém sessão utilizável. Viaja em cookie
  `httpOnly` + `SameSite=Strict` com path restrito: XSS não lê, CSRF não envia.
- **Rotação com detecção de reuso.** Cada refresh token vale uma vez; reapresentar um
  já rotacionado derruba todas as sessões do usuário (RFC 9700).
- **Rate limit por IP** no login, cadastro e refresh — não bloqueio por conta, que
  seria negação de serviço contra o próprio usuário.
- **Chave primária em UUID**: não há id sequencial para enumerar.
- **CSP, HSTS, `X-Frame-Options`, `nosniff`, `Referrer-Policy`** em toda resposta.
- **Toda consulta filtra por `user_id`**, num único ponto centralizado — e recurso de
  outro usuário devolve **404, não 403** (403 confirmaria que aquele id existe).
- **Todo timeout configurado** nas chamadas externas (connect/read/write/pool). Sem
  teto, um fornecedor que aceita a conexão e nunca responde prende o worker para sempre.
- **Resposta externa lida defensivamente**: um campo que suma numa atualização do
  fornecedor é ignorado, não vira 500 para o usuário. Preço zero ou negativo é rejeitado
  como dado corrompido.
- **`pip-audit` no CI**, semanalmente — a maior parte das falhas de uma aplicação não
  está no código dela, está no que ela importa.

## Frontend

Painel escuro servido pela **própria API** em `/app` — sem build, sem deploy separado e,
o que mais importa, **sem CORS**: página e API compartilham a origem, então o cookie
httpOnly do refresh token viaja normalmente. Um frontend em outro domínio exigiria
`SameSite=None`, enfraquecendo justamente a proteção contra CSRF.

Quatro telas: visão geral (KPIs + evolução da carteira), posições com matriz de
correlação, fronteira eficiente interativa e livro de transações. HTML/CSS/JS puros,
Chart.js via CDN — nenhuma dependência de build.

Duas decisões de segurança governam o cliente, e há teste no CI para cada uma:

- **O access token vive numa variável, nunca em `localStorage`.** localStorage é legível
  por qualquer script da página. A sessão é retomada pelo cookie httpOnly, que o
  JavaScript não consegue ler nem vazar.
- **Nenhum `innerHTML` com dado da API.** Todo texto entra por `textContent`: um nome de
  ativo com `<img onerror=...>` vira texto, não script.

Os testes também garantem que o HTML não tem script inline nem `onclick=` — a CSP proíbe
os dois, e uma página que os use **carrega mas não funciona**, reclamando só no console.

## Rodando localmente

```bash
cp .env.example .env          # gere SECRET_KEY: python -c "import secrets; print(secrets.token_urlsafe(64))"
uv sync
make banco                    # Postgres via Docker
make migrar
make api                      # http://127.0.0.1:8000/docs
```

## Carga inicial de dados

O catálogo e o histórico vêm dos CSVs da pipeline
[`mercado_financeiro`](https://github.com/timmtimm1/mercado_financeiro) — 151 tickers
filtrados por liquidez e um ano de fechamentos reais:

```bash
uv run python -m scripts.seed_b3 --origem ~/Projects/mercado_financeiro
```

O script é idempotente (upsert por chave natural) e insere em lotes de 5.000 linhas:
37 mil `INSERT` individuais levariam minutos, o lote leva segundos.

Para reconstruir o histórico da carteira a partir desses fechamentos (em vez de esperar
meses para o gráfico ganhar forma):

```bash
uv run python -m scripts.backfill_snapshots --email voce@exemplo.com --desde 2026-01-01
```

## Testes

```bash
make testes       # suíte completa
make cobertura    # com relatório de cobertura
make verificar    # exatamente o que o CI roda: lint + tipagem + testes
```

A suíte sobe um **PostgreSQL real e descartável** via testcontainers e aplica as
migrations nele — testar em SQLite e rodar em Postgres seria testar outro banco.
Cada teste roda dentro de uma transação com rollback, então a ordem de execução não
importa.

## Licença

MIT
