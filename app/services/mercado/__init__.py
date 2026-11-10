"""Provedores de dados de mercado (Yahoo, brapi, BCB). Todos aceitam um cliente HTTP injetável."""
import json
from decimal import Decimal

import httpx

TIMEOUT_S = 10.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


class ErroMercado(Exception):
    """Falha ao obter ou interpretar dados de mercado (rede, status HTTP ou formato inesperado)."""


def novo_cliente() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def obter_json(cliente: httpx.Client, url: str, params: dict | None = None):
    """GET + JSON, com números como Decimal (nunca float). Qualquer falha vira ErroMercado."""
    try:
        resp = cliente.get(url, params=params)
        resp.raise_for_status()
        return json.loads(resp.text, parse_float=Decimal)
    except (httpx.HTTPError, ValueError) as exc:
        raise ErroMercado(f"falha ao consultar {url}: {exc}") from exc
