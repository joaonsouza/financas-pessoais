from decimal import Decimal

import httpx
import keyring
from keyring.errors import PasswordDeleteError

from app.services.mercado import ErroMercado, novo_cliente, obter_json

URL = "https://brapi.dev/api/quote/{ticker}"
KEYRING_SERVICO = "app-financas"
KEYRING_USUARIO = "brapi"


def token_brapi() -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICO, KEYRING_USUARIO) or None
    except Exception:  # sem backend de keyring
        return None


def salvar_token(token: str) -> None:
    token = token.strip()
    if not token:
        raise ValueError("informe o token da brapi")
    try:
        keyring.set_password(KEYRING_SERVICO, KEYRING_USUARIO, token)
    except Exception as exc:
        raise ValueError(f"não foi possível guardar o token no cofre do sistema: {exc}") from exc


def remover_token() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICO, KEYRING_USUARIO)
    except PasswordDeleteError:
        pass
    except Exception as exc:
        raise ValueError(f"não foi possível remover o token do cofre do sistema: {exc}") from exc


def ultimos4_do_token() -> str | None:
    token = token_brapi()
    return token[-4:] if token else None


class Brapi:
    def __init__(self, token: str, cliente: httpx.Client | None = None):
        self.token = token
        self.cliente = cliente or novo_cliente()

    def cotacao_atual(self, ticker: str) -> tuple[Decimal, str, Decimal | None]:
        """(preço, moeda, variação do dia em %). Aceita `PETR4` ou `PETR4.SA`; um ticker por requisição."""
        ticker = ticker.removesuffix(".SA")
        dados = obter_json(self.cliente, URL.format(ticker=ticker), {"token": self.token})
        try:
            r = dados["results"][0]
            preco = Decimal(r["regularMarketPrice"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ErroMercado(f"resposta da brapi sem preço para {ticker}") from exc
        variacao = r.get("regularMarketChangePercent")
        return preco, r.get("currency") or "BRL", None if variacao is None else Decimal(variacao)
