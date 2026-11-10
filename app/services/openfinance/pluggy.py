"""Cliente da API da Pluggy (Open Finance). Só leitura; aceita um cliente HTTP injetável (testes usam transporte falso)."""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import httpx
import keyring
from keyring.errors import PasswordDeleteError

BASE_URL = "https://api.pluggy.ai"
TIMEOUT_S = 20.0
VALIDADE_API_KEY_S = 110 * 60  # a chave vale 2 h; renova um pouco antes
MAX_PAGINAS_CURSOR = 200  # trava de segurança contra cursor em loop
KEYRING_SERVICO = "app-financas"
KEYRING_ID = "pluggy_client_id"
KEYRING_SEGREDO = "pluggy_client_secret"


class OpenFinanceErro(Exception):
    """Falha ao falar com a Pluggy; a mensagem já vem em português para a tela."""


# ---------- chaves (Windows Credential Manager via keyring) ----------

def _ler(usuario: str) -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICO, usuario) or None
    except Exception:  # sem backend de keyring
        return None


def obter_chaves() -> tuple[str, str] | None:
    cid, segredo = _ler(KEYRING_ID), _ler(KEYRING_SEGREDO)
    return (cid, segredo) if cid and segredo else None


def salvar_chaves(client_id: str, client_secret: str) -> None:
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id or not client_secret:
        raise ValueError("informe o Client ID e o Client Secret")
    try:
        uuid.UUID(client_id)
    except ValueError as exc:
        raise ValueError(
            "o Client ID deve ter o formato xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (copie do painel da Pluggy)"
        ) from exc
    try:
        keyring.set_password(KEYRING_SERVICO, KEYRING_ID, client_id)
        keyring.set_password(KEYRING_SERVICO, KEYRING_SEGREDO, client_secret)
    except Exception as exc:
        raise ValueError(f"não foi possível guardar as chaves no cofre do sistema: {exc}") from exc


def remover_chaves() -> None:
    for usuario in (KEYRING_ID, KEYRING_SEGREDO):
        try:
            keyring.delete_password(KEYRING_SERVICO, usuario)
        except PasswordDeleteError:
            pass
        except Exception as exc:
            raise ValueError(f"não foi possível remover as chaves do cofre do sistema: {exc}") from exc


def ultimos4_do_client_id() -> str | None:
    cid = _ler(KEYRING_ID)
    return cid[-4:] if cid else None


def criar_cliente() -> "ClientePluggy | None":
    """Cliente com as chaves do keyring; `None` quando ainda não foram configuradas."""
    chaves = obter_chaves()
    return ClientePluggy(*chaves) if chaves else None


# ---------- cliente ----------

class ClientePluggy:
    def __init__(self, client_id: str, client_secret: str, http: httpx.Client | None = None,
                 relogio: Callable[[], float] = time.monotonic):
        self.client_id, self.client_secret = client_id, client_secret
        self.http = http or httpx.Client(timeout=TIMEOUT_S)
        self._relogio = relogio
        self._api_key: str | None = None
        self._expira_em = 0.0

    # -- transporte --
    def _enviar(self, metodo: str, caminho: str, **kw) -> httpx.Response:
        try:
            return self.http.request(metodo, BASE_URL + caminho, timeout=TIMEOUT_S, **kw)
        except httpx.HTTPError as exc:
            raise OpenFinanceErro("Sem conexão com a Pluggy. Verifique a internet e tente de novo.") from exc

    @staticmethod
    def _checar(resp: httpx.Response) -> httpx.Response:
        c = resp.status_code
        if c in (401, 403):
            raise OpenFinanceErro("A Pluggy recusou as chaves (Client ID ou Client Secret inválidos).")
        if c == 404:
            raise OpenFinanceErro("Item não encontrado na Pluggy. Confira o ID da conexão.")
        if c == 429:
            raise OpenFinanceErro("Limite de consultas da Pluggy atingido. Tente mais tarde.")
        if c >= 400:
            raise OpenFinanceErro(f"A Pluggy respondeu com erro {c}.")
        return resp

    @staticmethod
    def _json(resp: httpx.Response):
        try:
            return json.loads(resp.text, parse_float=Decimal)  # valores em reais nunca passam por float
        except ValueError as exc:
            raise OpenFinanceErro("Resposta inesperada da Pluggy.") from exc

    def autenticar(self) -> str:
        """apiKey em cache até expirar (2 h)."""
        if self._api_key and self._relogio() < self._expira_em:
            return self._api_key
        resp = self._checar(self._enviar("POST", "/auth", json={
            "clientId": self.client_id, "clientSecret": self.client_secret}))
        chave = self._json(resp).get("apiKey")
        if not chave:
            raise OpenFinanceErro("Resposta inesperada da Pluggy ao autenticar.")
        self._api_key, self._expira_em = chave, self._relogio() + VALIDADE_API_KEY_S
        return chave

    def _get(self, caminho: str, params: dict | None = None):
        resp = self._enviar("GET", caminho, params=params, headers={"X-API-KEY": self.autenticar()})
        if resp.status_code == 401:  # expirou antes do previsto: renova uma vez
            self._api_key = None
            resp = self._enviar("GET", caminho, params=params, headers={"X-API-KEY": self.autenticar()})
        return self._json(self._checar(resp))

    def _paginado(self, caminho: str, params: dict) -> list[dict]:
        itens: list[dict] = []
        pagina = 1
        while True:
            dados = self._get(caminho, {**params, "pageSize": 500, "page": pagina})
            itens.extend(dados.get("results") or [])
            if pagina >= int(dados.get("totalPages") or 1):
                return itens
            pagina += 1

    # -- recursos --
    def item(self, item_id: str) -> dict:
        return self._get(f"/items/{item_id}")

    def contas(self, item_id: str) -> list[dict]:
        return self._paginado("/accounts", {"itemId": item_id})

    def transacoes(self, account_id: str, desde: date, ate: date) -> list[dict]:
        """`/v2/transactions` (a `/transactions` antiga responde 410): paginação por cursor,
        `next` na resposta e `after` na requisição; não aceita `pageSize`."""
        params = {"accountId": account_id, "dateFrom": desde.isoformat(), "dateTo": ate.isoformat()}
        itens: list[dict] = []
        for _ in range(MAX_PAGINAS_CURSOR):
            dados = self._get("/v2/transactions", params)
            itens.extend(dados.get("results") or [])
            cursor = dados.get("next")
            if not cursor:
                return itens
            params = {**params, "after": cursor}
        raise OpenFinanceErro("A Pluggy devolveu páginas demais; tente sincronizar um período menor.")

    def faturas(self, account_id: str) -> list[dict]:
        return self._paginado("/bills", {"accountId": account_id})
