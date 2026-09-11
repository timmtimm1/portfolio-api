"""Excecoes de dominio.

A camada de servico levanta estas; a camada de rota traduz para HTTP. O servico
nao importa FastAPI de proposito -- assim a mesma funcao serve a um endpoint, a
um job de cron e a um comando de CLI sem arrastar o framework junto.
"""

from __future__ import annotations


class DomainError(Exception):
    """Raiz de todo erro de regra de negocio."""


class EmailJaCadastradoError(DomainError):
    pass


class CredenciaisInvalidasError(DomainError):
    """Usada tanto para email inexistente quanto para senha errada.

    Ser uma excecao unica nao e preguica: e o que garante que os dois casos
    produzam exatamente a mesma resposta, sem depender de o desenvolvedor da rota
    lembrar de nao diferenciar.
    """


class ContaInativaError(DomainError):
    pass


class EmailNaoConfirmadoError(DomainError):
    """Senha certa, conta ativa, e-mail ainda nao confirmado.

    So e levantada DEPOIS de conferir a senha, como a de conta inativa: quem nao
    sabe a senha recebe o 401 de sempre e nao descobre que a conta existe. Quem
    sabe a senha e o dono -- e precisa saber que falta abrir o e-mail.
    """
