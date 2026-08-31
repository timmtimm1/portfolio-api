"""Configuracao da aplicacao, carregada de variaveis de ambiente.

Principio de seguranca central deste modulo: **nenhum segredo tem valor padrao**.
Se SECRET_KEY ou a senha do banco nao estiverem no ambiente, a aplicacao se recusa
a subir. E o oposto do padrao comum de `SECRET_KEY = os.getenv("SECRET_KEY", "dev")`,
que funciona em producao com a chave "dev" e ninguem percebe ate vazar.

Segredos usam `SecretStr`: o valor nao aparece em `repr()`, em log de excecao nem
no traceback. So sai com `.get_secret_value()`, chamada explicita e rastreavel.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Rejeita variaveis desconhecidas: um typo em PORTFOLIO_SECRET_KEY vira erro
        # de boot, nao um segredo silenciosamente ignorado.
        extra="forbid",
        case_sensitive=False,
    )

    ENVIRONMENT: Environment = "local"
    # Loga todo o SQL executado. Util para caçar N+1 no desenvolvimento;
    # ignorado em producao mesmo se ligado por engano (ver app/core/db.py).
    SQL_ECHO: bool = False
    PROJECT_NAME: str = "Portfolio Tracker API"
    API_V1_PREFIX: str = "/api/v1"

    # --- Banco de dados -------------------------------------------------------
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "portfolio"
    POSTGRES_USER: str = "portfolio"
    POSTGRES_PASSWORD: SecretStr  # sem default: obrigatorio

    # --- Seguranca ------------------------------------------------------------
    # Sem default. Gere com: python -c "import secrets; print(secrets.token_urlsafe(64))"
    SECRET_KEY: SecretStr
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Onde o contador de rate limit e guardado. "memory://" e por processo;
    # em producao com mais de um worker, use "redis://host:6379".
    RATE_LIMIT_STORAGE: str = "memory://"
    RATE_LIMIT_LOGIN: str = "5/minute"
    RATE_LIMIT_REGISTER: str = "3/minute"

    # --- Cotacoes -------------------------------------------------------------
    # Token da brapi.dev. Opcional: sem ele o plano gratuito ainda responde para
    # alguns tickers, com limite bem menor.
    BRAPI_TOKEN: SecretStr | None = None

    # Idade maxima de uma cotacao em cache, em segundos. 15 minutos e coerente
    # com o atraso do proprio plano gratuito -- buscar mais rapido que isso
    # gastaria cota para receber o mesmo numero.
    QUOTE_TTL_SECONDS: int = 900

    # Timeout de chamada externa. Sem teto, um fornecedor lento prende o worker
    # indefinidamente e a aplicacao inteira para -- e a forma mais comum de uma
    # API cair por causa de um terceiro.
    HTTP_TIMEOUT_SECONDS: float = 5.0

    # Origens permitidas para CORS. Lista explicita, nunca "*" junto com credenciais.
    #
    # `NoDecode` desliga o parse automatico de JSON que o pydantic-settings faz em
    # campos de tipo complexo. Sem ele, CORS_ORIGINS=http://a,http://b (o formato
    # natural de uma variavel de ambiente) estoura antes do validador abaixo rodar.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("SECRET_KEY")
    @classmethod
    def _secret_key_forte(cls, v: SecretStr) -> SecretStr:
        """Uma chave curta transforma o JWT em algo forcavel por brute force offline.
        32 bytes e o minimo razoavel para HMAC-SHA256."""
        if len(v.get_secret_value()) < 32:
            raise ValueError("SECRET_KEY precisa de pelo menos 32 caracteres")
        return v

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _parse_cors(cls, v: object) -> object:
        """Aceita "http://a,http://b" (formato natural de variavel de ambiente)
        alem de lista JSON."""
        if isinstance(v, str) and not v.startswith("["):
            return [origem.strip() for origem in v.split(",") if origem.strip()]
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> URL:
        """URL async (asyncpg) usada pela aplicacao em runtime.

        Devolve o objeto `URL` do SQLAlchemy, nao uma string: o `repr()` dele
        mascara a senha (`***`), entao a credencial nao vaza num log de erro de
        conexao, num traceback do Sentry nem no `echo=True` do engine. Uma string
        crua vazaria em todos esses lugares.
        """
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD.get_secret_value(),
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            database=self.POSTGRES_DB,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_sync(self) -> URL:
        """Mesma URL, driver sincrono (psycopg). O Alembic roda migrations fora do
        event loop, entao ele usa esta -- nao a async."""
        return self.database_url.set(drivername="postgresql+psycopg")


@lru_cache
def get_settings() -> Settings:
    """Instancia unica, cacheada.

    E uma funcao (nao uma variavel de modulo) de proposito: assim os testes podem
    sobrescrever via `app.dependency_overrides[get_settings]` sem precisar mexer em
    variavel de ambiente global nem reimportar modulo.
    """
    return Settings()  # type: ignore[call-arg]  # os campos vem do ambiente
