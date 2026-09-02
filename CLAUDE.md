# Portfolio Tracker — instruções do projeto

Rastreador de carteira da B3 com análise de risco. Vitrine técnica (LinkedIn /
recrutadores), não um produto comercial. **Não é corretora: não envia ordem,
não vende nada.** Todo "alvo" é lembrete visual.

Python 3.12 · FastAPI · SQLAlchemy 2.0 async (asyncpg) · Alembic · Postgres 17 ·
uv · ruff · mypy strict · pytest + testcontainers · frontend em JS puro
(`app/static/`, sem framework, sem build).

## Comandos

```bash
make verificar   # lint + tipos + testes -- rode ANTES de commitar
make migration m="descrição"   # gera migration E formata (nunca use o alembic cru)
make banco       # sobe o Postgres
make api         # uvicorn --reload
```

O CI roda `ruff check .` no repositório **inteiro**. Conferir só `app tests`
deixa `migrations/` de fora e o CI reprova — já aconteceu duas vezes.

## Invariantes que não se quebram

**Ledger-as-truth.** Não existe coluna de saldo. Posição, preço médio e
resultado são recalculados das transações a cada consulta. A exceção
deliberada é `portfolio_snapshots`: o valor de mercado de ontem não é
reconstruível porque a cotação é sobrescrita.

**Um único portão de autorização.** `get_current_user` e `get_carteira`
(`app/core/deps.py`). Nenhuma rota aceita `user_id`/`portfolio_id` do corpo.
Recurso de outro usuário responde **404, nunca 403** (403 confirma existência).

**Módulos puros.** `optimizer`, `position`, `rebalance`, `simulation`, `split`,
`dividend`, `target`, `trade` não tocam banco, ORM nem HTTP. Entram dados, saem
dados — testáveis com calculadora. Serviços `*_service.py` são a camada que
persiste.

**Decimal para dinheiro, float para estatística.** A conversão acontece numa
fronteira explícita, nunca no meio da conta.

**`lazy="raise"`** nos relationships: N+1 falha alto, não em silêncio.

## Frontend (`app/static/`)

- **Nunca `innerHTML`** com dado da API — monte nós de DOM. Há teste que barra.
- **Campo `type="number"` recebe `paraCampoNumerico()`, nunca `num()`.**
  `num(2500)` devolve "2.500" (pt-BR) e o campo lê como 2,5 — o estrago só
  aparece ao salvar. Há teste que barra.
- `[hidden] { display: none !important }` no CSS é obrigatório: sem ele,
  qualquer `display` do autor anula o atributo `hidden`.

## Migrations

- `make migration` (o autogenerate cru emite linhas de 140 chars e o lint reprova).
- **CHECK constraints não são detectados** pelo autogenerate — o `include_object`
  em `migrations/env.py` os exclui de propósito (senão ele apaga os que nascem
  dos enums). CHECK novo se escreve à mão na migration.
- Coluna `NOT NULL` em tabela com dados precisa de `server_default`.
- Enum reusado em duas colunas da mesma tabela precisa de `name=` distinto em
  `coluna_enum` — senão os dois CHECK colidem no mesmo nome.
- Sempre teste o round-trip: `upgrade` → `downgrade -1` → `upgrade`.

## Testes

657 testes. A disciplina é **teste de mutação**: depois de escrever um teste,
quebre o código de propósito e confirme que ele falha. Teste que não sabe
falhar não prova nada — já houve quatro testes vacuosos pegos assim.

Para desfazer uma mutação, use **cópia de backup**, nunca `git checkout` num
arquivo com trabalho não commitado (isso já apagou uma feature inteira aqui).

## Ao trabalhar com o app rodando

**Nunca escreva dados de teste na conta real** (`bernardo@exemplo.com`). Use a
conta de demonstração e **confirme com `/auth/me` que `is_demo` é true antes de
escrever** — o app restaura a sessão real sozinha pelo cookie, e uma checagem
de "o app está visível?" não basta. Já aconteceu.

Não digite senhas em formulários; peça para o Bernardo fazer esse passo.

## Estado atual

Feito: auth (JWT + refresh com detecção de reuso), carteiras real/simuladas,
transações, cotações com cache, proventos, desdobramentos, snapshots,
fronteira eficiente (Markowitz), rebalanceamento, Monte Carlo, observabilidade
(JSON logs + Prometheus), conta demo de 2h, alvos (stop gain/loss + meta de
acumulação), área de trade (trade ótimo).

Pendente: **deploy** (adiado de propósito), editar transação (não existe
PUT/PATCH), recuperar senha, PWA para celular. Imposto de renda foi excluído
deliberadamente — modelar IR exigiria somar vendas do mês, prejuízo acumulado
e tipo de operação; número fiscal quase certo é pior que nenhum.

Dados de mercado vêm do repo irmão `~/Projects/mercado_financeiro` via CSV,
carregados por cron (ver `scripts/atualizar_historico.sh`).
