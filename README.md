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
| 9 | Otimização de Markowitz | ⬜ |
| 10 | Snapshots diários da carteira | ⬜ |
| 11 | Frontend com o gráfico da fronteira | ⬜ |
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
- **Séries alinhadas pela interseção das datas** antes de qualquer cálculo. Correlacionar
  históricos de tamanhos diferentes produz um número com a forma certa e o significado
  errado: nada estoura, nada avisa.
- **Desvio-padrão amostral (`ddof=1`)**, que não subestima o risco.

`Decimal` para dinheiro, `float` para estatística — a fronteira é uma função com nome
(`para_float`), não `float(x)` espalhado pelo código.

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
