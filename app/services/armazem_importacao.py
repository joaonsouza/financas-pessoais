"""Guarda, só em memória, o arquivo enviado e a proposta entre o upload e a confirmação (1 h de validade).

O arquivo nunca vai para o disco.
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

TTL_PADRAO = 3600


@dataclass
class Entrada:
    nome: str
    conteudo: bytes
    hash_arquivo: str
    resultado: object | None = None   # ResultadoLeitura já lido (evita reler, e evita nova chamada à IA)
    proposta: object | None = None
    expira_em: float = 0.0
    account_id: str | None = None     # Open Finance: conta da Pluggy cuja sincronização esta entrada representa


class Armazem:
    def __init__(self, ttl: int = TTL_PADRAO, relogio: Callable[[], float] = time.time):
        self.ttl, self._relogio = ttl, relogio
        self._itens: dict[str, Entrada] = {}

    def _limpar(self) -> None:
        agora = self._relogio()
        for token in [t for t, e in self._itens.items() if e.expira_em <= agora]:
            del self._itens[token]

    def guardar(self, entrada: Entrada) -> str:
        self._limpar()
        token = secrets.token_urlsafe(16)
        entrada.expira_em = self._relogio() + self.ttl
        self._itens[token] = entrada
        return token

    def obter(self, token: str) -> Entrada | None:
        self._limpar()
        return self._itens.get(token)

    def remover(self, token: str) -> None:
        self._itens.pop(token, None)
