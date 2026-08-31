"""Testes do modulo de criptografia.

Sao testes unitarios puros -- sem banco, sem HTTP. E o que permite cobrir cada
caminho de forma exaustiva: e justamente o codigo de seguranca que merece isso.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import jwt
import pytest

from app.core.security import (
    create_token,
    decode_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)

UM_MINUTO = timedelta(minutes=1)


class TestHashDeSenha:
    def test_hash_nao_contem_a_senha(self) -> None:
        senha = "carteira-b3-2026-forte"
        assert senha not in hash_password(senha)

    def test_senhas_iguais_geram_hashes_diferentes(self) -> None:
        """Prova que ha salt por senha. Sem salt, duas contas com a mesma senha
        teriam o mesmo hash -- e uma rainbow table quebraria as duas de uma vez."""
        assert hash_password("mesma-senha-123456") != hash_password("mesma-senha-123456")

    def test_verifica_senha_correta(self) -> None:
        senha = "carteira-b3-2026-forte"
        valida, _ = verify_password(senha, hash_password(senha))
        assert valida

    def test_rejeita_senha_errada(self) -> None:
        valida, _ = verify_password("senha-errada-1234", hash_password("carteira-b3-2026-forte"))
        assert not valida

    def test_usa_argon2id(self) -> None:
        """Guarda contra troca acidental de algoritmo numa atualizacao de lib."""
        assert hash_password("carteira-b3-2026-forte").startswith("$argon2id$")


class TestRefreshToken:
    def test_token_e_unico_a_cada_chamada(self) -> None:
        assert generate_refresh_token()[0] != generate_refresh_token()[0]

    def test_entropia_suficiente(self) -> None:
        """48 bytes em base64 url-safe dao 64 caracteres. Menos que isso indicaria
        que alguem reduziu REFRESH_TOKEN_BYTES sem perceber a consequencia."""
        assert len(generate_refresh_token()[0]) >= 64

    def test_hash_e_deterministico(self) -> None:
        """Precisa ser: e por ele que o token e localizado no banco."""
        token, digest = generate_refresh_token()
        assert hash_refresh_token(token) == digest

    def test_hash_nao_contem_o_token(self) -> None:
        token, digest = generate_refresh_token()
        assert token not in digest


class TestJWT:
    def test_token_valido_e_aceito(self) -> None:
        user_id = uuid.uuid4()
        token, jti = create_token(user_id, "access", UM_MINUTO)
        payload = decode_token(token, "access")
        assert payload["sub"] == str(user_id)
        assert payload["jti"] == jti

    def test_rejeita_assinatura_de_outro_segredo(self) -> None:
        forjado = jwt.encode(
            {"sub": str(uuid.uuid4()), "typ": "access", "exp": 9999999999, "iat": 1, "jti": "x"},
            "chave-do-atacante-com-tamanho-suficiente-aqui",
            algorithm="HS256",
        )
        with pytest.raises(jwt.InvalidTokenError):
            decode_token(forjado, "access")

    def test_rejeita_ataque_alg_none(self) -> None:
        """A vulnerabilidade classica de JWT: o atacante remove a assinatura e
        declara `alg: none`. So nao funciona porque `algorithms` e lista fechada."""
        forjado = jwt.encode(
            {"sub": str(uuid.uuid4()), "typ": "access", "exp": 9999999999, "iat": 1, "jti": "x"},
            None,
            algorithm="none",
        )
        with pytest.raises(jwt.InvalidTokenError):
            decode_token(forjado, "access")

    def test_rejeita_token_expirado(self) -> None:
        token, _ = create_token(uuid.uuid4(), "access", timedelta(seconds=-1))
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_token(token, "access")

    def test_rejeita_confusao_de_tipo(self) -> None:
        """Sem a claim `typ`, um refresh token de 30 dias passaria como token de
        acesso e anularia a expiracao curta de 15 minutos."""
        token, _ = create_token(uuid.uuid4(), "refresh", UM_MINUTO)
        with pytest.raises(jwt.InvalidTokenError):
            decode_token(token, "access")

    def test_rejeita_token_sem_claims_obrigatorias(self) -> None:
        incompleto = jwt.encode(
            {"sub": "x"},
            "chave-de-teste-deterministica-com-mais-de-32-caracteres",
            algorithm="HS256",
        )
        with pytest.raises(jwt.MissingRequiredClaimError):
            decode_token(incompleto, "access")
