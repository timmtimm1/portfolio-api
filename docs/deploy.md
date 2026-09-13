# Deploy: Neon + Render

Guia para tirar a API do "roda só na máquina que subiu o Docker" e colocar num
endereço único, acessível de qualquer PC (ou celular). Depois disso os dois
notebooks — e qualquer outro dispositivo — abrem a mesma URL e veem os mesmos
dados, porque passam a falar com o mesmo banco.

**Sem instalação nova nos PCs.** O painel é servido pela própria API
(`app/main.py`, seção "Frontend servido pela PRÓPRIA API"), então acessar o
deploy é abrir a URL no navegador. O ambiente local (`make banco` / `make api`)
continua existindo — é o que você usa para *desenvolver*, testado antes de subir.

---

## 1. Banco: um projeto novo no Neon

Você já tem conta no Neon (usada no `mercado_financeiro`/`volume-scanner-b3`).
Este projeto ganha um banco **separado** — não faz sentido dividir schema com
outro produto.

1. Entre em [neon.tech](https://neon.tech) com a conta que já usa.
2. **New Project** → nome `portfolio-api` → região mais próxima de você
   (`sa-east-1`/São Paulo, se disponível; senão `us-east-1`).
3. Neon cria o banco `neondb` e te dá uma *connection string* assim:
   ```
   postgresql://usuario:senha@ep-xxxx.sa-east-1.aws.neon.tech/neondb?sslmode=require
   ```
4. Quebre essa URL nas variáveis que o projeto usa (ver `.env.example`):

   | Variável | Vem de |
   |---|---|
   | `POSTGRES_HOST` | `ep-xxxx.sa-east-1.aws.neon.tech` |
   | `POSTGRES_PORT` | `5432` |
   | `POSTGRES_DB` | `neondb` |
   | `POSTGRES_USER` | `usuario` |
   | `POSTGRES_PASSWORD` | `senha` |

   Guarde esses cinco valores — entram no Render no passo 3.

   *(O app monta a URL de conexão sozinho a partir dessas variáveis — ver
   `database_url` em `app/core/config.py`.)*

   O Neon **recusa conexão sem TLS**, então o serviço no Render também precisa
   de `POSTGRES_SSL=true` (está na tabela do passo 3). Localmente ela fica
   `false`: o Postgres do compose não tem TLS nenhum.

---

## 2. E-mail: não há

O projeto não envia e-mail, e isso é consequência direta deste deploy: **o plano
Free do Render bloqueia as portas SMTP 25, 465 e 587** desde 26/09/2025. A
confirmação de cadastro por link existia, dependia de SMTP, e foi removida
quando ficou claro que nenhum e-mail sairia dali — uma conta que só entra depois
de um link que nunca chega é uma conta trancada.

Nada a configurar aqui. A conta criada no painel já entra na mesma ação.

**Se um dia quiser e-mail de volta**, são dois caminhos: uma instância paga do
Render (libera 587/465), ou um provedor transacional na **porta 2525**, que o
Render não bloqueia — Brevo, SendGrid e Mailgun oferecem. O código do envio foi
removido; o histórico está na migration `0fe51817224f`.

---

## 3. API: novo Web Service no Render

1. Entre em [render.com](https://render.com) e crie conta (GitHub login é o
   mais rápido — ele já lista os repositórios).
2. **New** → **Web Service** → conecte o repositório `timmtimm1/portfolio-api`.
3. Configuração do serviço:
   - **Language/Runtime**: `Docker` (o `Dockerfile` já está no repo)
   - **Branch**: `main`
   - **Instance Type**: `Free`

   Não existe passo de "Pre-Deploy Command" aqui: **o plano Free do Render não
   suporta esse recurso** (é pago; um blueprint que o usa é rejeitado). A
   migration entra de outro jeito — já está dentro do próprio `CMD` do
   `Dockerfile` (`alembic upgrade head && exec uvicorn ...`), rodando a cada
   boot do container, antes da API aceitar requisição. É seguro porque o Free
   tier mantém uma instância só por vez, então não há duas rodando a mesma
   migration ao mesmo tempo. Nada a configurar aqui — já vem pronto.
4. **Environment** → adicione as variáveis abaixo. Os "❓" pedem um valor
   gerado ou escolhido por você:

   | Variável | Valor |
   |---|---|
   | `ENVIRONMENT` | `production` |
   | `POSTGRES_HOST` | do passo 1 |
   | `POSTGRES_PORT` | `5432` |
   | `POSTGRES_DB` | do passo 1 |
   | `POSTGRES_USER` | do passo 1 |
   | `POSTGRES_PASSWORD` | do passo 1 |
   | `POSTGRES_SSL` | `true` — o Neon recusa conexão sem TLS |
   | `SECRET_KEY` | ❓ gere com `python3 -c "import secrets; print(secrets.token_urlsafe(64))"` |
   | `SERVICE_API_KEY` | ❓ gere com `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` — chave do cron de snapshots |
   | `LOG_JSON` | `true` |
   | `RISK_FREE_RATE` | `0.10` (ou a Selic/CDI atual) |
   | `BRAPI_TOKEN` | opcional — deixe em branco ou pegue o seu em brapi.dev |

   `CORS_ORIGINS` fica **de fora** de propósito: o painel é servido pela
   própria API (mesma origem), então CORS não entra em jogo — adicionar a
   variável sem necessidade só abriria uma superfície que não existe hoje.

5. **Create Web Service**. O primeiro build demora (baixa a imagem base,
   instala as dependências); os próximos reaproveitam cache.
6. Quando o deploy terminar, copie a URL pública — ela entra nos secrets do
   GitHub, no passo 4. Não há variável de ambiente que precise dela.

> **Atualizando um serviço que já existe:** a configuração rejeita variável
> desconhecida (`extra="forbid"`, para um typo virar erro de boot em vez de
> segredo ignorado). Se o serviço foi criado quando o e-mail existia, **apague**
> `APP_URL` e todas as `SMTP_*` antes do próximo deploy — senão a aplicação não
> sobe.

**Free tier hiberna após 15 min sem tráfego.** A primeira requisição depois de
um tempo parado demora ~1 min para acordar — normal, não é erro.

---

## 4. Secrets do GitHub Actions

Dois workflows dependem de secrets. Enquanto faltarem, ambos rodam no horário,
detectam a ausência e **pulam com um aviso** — não quebram nada, só não fazem
nada.

No GitHub: **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**.

**Para `dados.yml`** (carrega catálogo e histórico no banco — sem isto a busca
de ativos não devolve nada, porque o catálogo mora na tabela `assets`):

| Secret | Valor |
|---|---|
| `POSTGRES_HOST` | do passo 1 |
| `POSTGRES_DB` | do passo 1 |
| `POSTGRES_USER` | do passo 1 |
| `POSTGRES_PASSWORD` | do passo 1 |

`POSTGRES_PORT` e `POSTGRES_SSL` estão fixos no workflow — não são segredo.

**Para `snapshots.yml`** (a foto diária da carteira):

| Secret | Valor |
|---|---|
| `API_URL` | a URL pública do Render (sem `/` no final) |
| `SERVICE_API_KEY` | o mesmo valor gerado no passo 3 |

---

## 5. Primeira carga de dados

Um banco novo começa vazio. Dispare a carga **uma vez, à mão**:

**Actions** → **"Carga do catalogo e do historico no banco"** → **Run workflow**.

Leva menos de um minuto. A saída mostra o que entrou:

```
[seed] 151 ativos no catalogo
[seed] 37408 cotacoes inseridas, 0 ignoradas
```

Daí em diante ele roda sozinho às 19h10 (BRT) nos dias úteis, pouco depois do
pipeline do `mercado_financeiro` publicar os CSVs do dia. O seed é idempotente:
rodar de novo não duplica nada.

> O `scripts/atualizar_historico.sh` continua no repositório, mas carrega o
> banco **local**, para desenvolvimento. Ele não toca em produção.

---

## 6. Testar

```bash
curl https://SUA-URL.onrender.com/api/v1/health
# {"status":"ok","environment":"production","version":"0.1.0"}
```

Abra `https://SUA-URL.onrender.com/painel/` e cadastre-se: você entra na mesma
ação, sem confirmar nada. A senha é pedida duas vezes de propósito — não existe
recuperar senha, e um erro de digitação criaria uma conta que ninguém abre.

A partir daqui, os dois PCs (e o celular) usam essa mesma URL. O
`docker compose` local continua de pé para quando você for mexer no código —
sem afetar o que já está no ar.

---

## O que ainda fica de fora

- **Domínio próprio**: o Render já dá HTTPS na URL `.onrender.com`; um domínio
  customizado é configuração extra do Render, não deste guia.
- **Domínio próprio** já está acima; fora isso, nada. A carga de dados de
  mercado deixou de ser pendência: virou o workflow do passo 5.
