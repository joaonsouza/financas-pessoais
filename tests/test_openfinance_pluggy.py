import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.services.openfinance.pluggy import (
    ClientePluggy, OpenFinanceErro, criar_cliente, obter_chaves, remover_chaves, salvar_chaves, ultimos4_do_client_id,
)

FIX = Path(__file__).parent / "fixtures" / "pluggy"


def carregar(nome: str) -> dict:
    return json.loads((FIX / nome).read_text(encoding="utf-8"))


def cliente_com(handler, relogio=None):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    kw = {"relogio": relogio} if relogio else {}
    return ClientePluggy("id-ficticio", "segredo-ficticio", http, **kw)


def resposta(dados, status=200):
    return httpx.Response(status, json=dados)


def test_autentica_e_usa_cabecalho_x_api_key():
    vistos = []

    def handler(req):
        vistos.append(req)
        if req.url.path == "/auth":
            assert json.loads(req.content) == {"clientId": "id-ficticio", "clientSecret": "segredo-ficticio"}
            return resposta(carregar("auth.json"))
        assert req.headers["X-API-KEY"] == "chave-api-ficticia-001"
        return resposta(carregar("item.json"))

    c = cliente_com(handler)
    assert c.item("abc")["status"] == "UPDATED"
    assert c.item("abc")["status"] == "UPDATED"
    assert [r.url.path for r in vistos].count("/auth") == 1  # cache da apiKey


def test_renova_apikey_expirada():
    hora = [0.0]
    auths = []

    def handler(req):
        if req.url.path == "/auth":
            auths.append(1)
            return resposta(carregar("auth.json"))
        return resposta(carregar("item.json"))

    c = cliente_com(handler, relogio=lambda: hora[0])
    c.item("x")
    hora[0] = 3 * 3600
    c.item("x")
    assert len(auths) == 2


def test_401_renova_uma_vez_e_repete():
    estado = {"n": 0}

    def handler(req):
        if req.url.path == "/auth":
            return resposta(carregar("auth.json"))
        estado["n"] += 1
        return resposta({}, 401) if estado["n"] == 1 else resposta(carregar("item.json"))

    assert cliente_com(handler).item("x")["id"].startswith("1111")
    assert estado["n"] == 2


def test_401_persistente_vira_chaves_invalidas():
    def handler(req):
        return resposta(carregar("auth.json")) if req.url.path == "/auth" else resposta({}, 401)

    with pytest.raises(OpenFinanceErro, match="chaves"):
        cliente_com(handler).item("x")


@pytest.mark.parametrize("status", [401, 403])
def test_auth_recusada(status):
    with pytest.raises(OpenFinanceErro, match="chaves"):
        cliente_com(lambda req: resposta({}, status)).autenticar()


@pytest.mark.parametrize("status,trecho", [(404, "não encontrado"), (429, "Tente mais tarde"), (500, "erro 500")])
def test_erros_http_viram_mensagens(status, trecho):
    def handler(req):
        return resposta(carregar("auth.json")) if req.url.path == "/auth" else resposta({}, status)

    with pytest.raises(OpenFinanceErro, match=trecho):
        cliente_com(handler).item("x")


def test_rede_vira_sem_conexao():
    def handler(req):
        raise httpx.ConnectError("sem rede")

    with pytest.raises(OpenFinanceErro, match="Sem conexão"):
        cliente_com(handler).autenticar()


def test_contas_e_faturas():
    def handler(req):
        if req.url.path == "/auth":
            return resposta(carregar("auth.json"))
        if req.url.path == "/accounts":
            assert req.url.params["itemId"] == "it1"
            return resposta(carregar("contas.json"))
        assert req.url.path == "/bills" and req.url.params["accountId"] == "acc-cartao-0001"
        return resposta(carregar("faturas.json"))

    c = cliente_com(handler)
    assert [a["id"] for a in c.contas("it1")] == ["acc-corrente-0001", "acc-cartao-0001"]
    assert [b["id"] for b in c.faturas("acc-cartao-0001")] == ["bill-2026-09", "bill-2026-10"]


def test_transacoes_usam_v2_com_cursor_e_decimal():
    cursores = []

    def handler(req):
        if req.url.path == "/auth":
            return resposta(carregar("auth.json"))
        assert req.url.path == "/v2/transactions"
        p = req.url.params
        assert (p["accountId"], p["dateFrom"], p["dateTo"]) == ("acc-cartao-0001", "2026-09-01", "2026-10-05")
        assert "pageSize" not in p and "page" not in p
        cursores.append(p.get("after"))
        if p.get("after") is None:
            return resposta({"results": carregar("transacoes_cartao_p1.json")["results"], "next": "cur-2"})
        assert p["after"] == "cur-2"
        return resposta({"results": carregar("transacoes_cartao_p2.json")["results"], "next": None})

    itens = cliente_com(handler).transacoes("acc-cartao-0001", date(2026, 9, 1), date(2026, 10, 5))
    assert cursores == [None, "cur-2"]
    assert [t["id"] for t in itens] == ["tx-c-001", "tx-c-002", "tx-c-003", "tx-c-004"]
    assert itens[0]["amount"] == Decimal("120.5")


def test_chaves_no_keyring():
    assert obter_chaves() is None and criar_cliente() is None
    salvar_chaves(" 00000000-0000-4000-8000-000000001234 ", " segredo ")
    assert obter_chaves() == ("00000000-0000-4000-8000-000000001234", "segredo")
    assert ultimos4_do_client_id() == "1234"
    assert criar_cliente() is not None
    remover_chaves()
    assert obter_chaves() is None
    with pytest.raises(ValueError):
        salvar_chaves("", "x")


@pytest.mark.parametrize("client_id", ["abcd-1234", "minha-senha-openfinance", "123"])
def test_client_id_fora_do_formato_uuid_e_recusado(client_id):
    with pytest.raises(ValueError, match="formato"):
        salvar_chaves(client_id, "segredo")
    assert obter_chaves() is None
