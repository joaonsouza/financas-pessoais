import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Conta, Lancamento, get_config
from app.services.importacao import aplicar_importacao
from app.services.openfinance.pluggy import ClientePluggy, OpenFinanceErro
from app.services.openfinance.sincronizar import (
    adicionar_item, consultar_itens, itens_configurados, propor_para_conta, propor_sincronizacao, registrar_sincronizacao,
    remover_item, status_dos_itens, ultima_sincronizacao,
)
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta, novo_cartao

FIX = Path(__file__).parent / "fixtures" / "pluggy"


def carregar(nome):
    return json.loads((FIX / nome).read_text(encoding="utf-8"))


def cliente_fake(item=None, chamadas=None, falha_transacoes=None):
    def handler(req):
        p = req.url.path
        if chamadas is not None:
            chamadas.append((p, dict(req.url.params)))
        if p == "/auth":
            return httpx.Response(200, json=carregar("auth.json"))
        if p.startswith("/items/"):
            if p.endswith("inexistente"):
                return httpx.Response(404, json={})
            return httpx.Response(200, json=item or carregar("item.json"))
        if p == "/accounts":
            return httpx.Response(200, json=carregar("contas.json"))
        if p == "/bills":
            return httpx.Response(200, json=carregar("faturas.json"))
        if p == "/v2/transactions":
            if falha_transacoes:
                return httpx.Response(falha_transacoes, json={})
            if req.url.params["accountId"] == "acc-corrente-0001":
                return httpx.Response(200, json={"results": carregar("transacoes_corrente.json")["results"], "next": None})
            if req.url.params.get("after") is None:
                return httpx.Response(200, json={"results": carregar("transacoes_cartao_p1.json")["results"], "next": "p2"})
            return httpx.Response(200, json={"results": carregar("transacoes_cartao_p2.json")["results"], "next": None})
        return httpx.Response(404)

    return ClientePluggy("i", "s", httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture
def contas(session):
    corrente = nova_conta(session, "Corrente")
    cartao = novo_cartao(session, "Cartão", pagadora=corrente, fechamento=8, vencimento=15)
    return corrente, cartao


def test_sem_vinculo_pede_escolha_e_janela_de_90_dias(session, contas):
    chamadas = []
    props = propor_sincronizacao(session, cliente_fake(chamadas=chamadas), ["it1"], HOJE_TESTE)
    assert [(p.account_id, p.tipo) for p in props] == [("acc-corrente-0001", "corrente"), ("acc-cartao-0001", "cartao")]
    assert all(p.conta is None and p.proposta is None and p.resultado is not None for p in props)
    assert [c.nome for c in props[0].candidatas] == ["Corrente"] and [c.nome for c in props[1].candidatas] == ["Cartão"]
    assert props[0].desde == date(2026, 7, 7)  # hoje - 90 dias
    tx = [c for c in chamadas if c[0] == "/v2/transactions"][0][1]
    assert tx["dateFrom"] == "2026-07-07" and tx["dateTo"] == "2026-10-05"


def test_vinculado_gera_proposta_e_janela_desde_saldo_inicial(session, contas):
    corrente, cartao = contas
    corrente.identificador_externo = "pluggy:acc-corrente-0001"
    session.flush()
    props = propor_sincronizacao(session, cliente_fake(), ["it1"], HOJE_TESTE)
    assert props[0].conta is corrente and props[0].desde == corrente.data_saldo_inicial
    assert [ln.status for ln in props[0].proposta.linhas].count("nova") >= 2
    assert props[1].conta is None


def test_janela_incremental_com_sobreposicao_de_7_dias(session, contas):
    registrar_sincronizacao(session, "acc-cartao-0001", date(2026, 10, 1))
    props = propor_sincronizacao(session, cliente_fake(), ["it1"], HOJE_TESTE)
    assert props[1].desde == date(2026, 9, 24)
    assert props[0].desde == date(2026, 7, 7)  # outra conta: ainda sem sync
    assert ultima_sincronizacao(session) == date(2026, 10, 1)
    props = propor_sincronizacao(session, cliente_fake(), ["it1"], HOJE_TESTE, desde=date(2026, 9, 1))
    assert {p.desde for p in props} == {date(2026, 9, 1)}


def test_cartao_aplicar_e_sincronizar_de_novo_nao_duplica(session, contas):
    _, cartao = contas
    props = propor_sincronizacao(session, cliente_fake(), ["it1"], HOJE_TESTE)
    pc = propor_para_conta(session, props[1], cartao, HOJE_TESTE)
    aplicar_importacao(session, pc.proposta, hoje=HOJE_TESTE)
    registrar_sincronizacao(session, pc.account_id, HOJE_TESTE)
    session.commit()
    assert cartao.identificador_externo == "pluggy:acc-cartao-0001"  # aprendido
    lancs = list(session.scalars(select(Lancamento).where(Lancamento.conta_id == cartao.id)
                                 .order_by(Lancamento.data, Lancamento.id)))
    efetivados = [ln for ln in lancs if ln.status == "efetivado"]
    assert [(ln.valor, ln.origem) for ln in efetivados] == [(-12050, "openfinance"), (-9990, "openfinance"),
                                                             (3000, "openfinance")]
    assert {ln.parcela_num for ln in lancs if ln.valor == -9990} >= {3}
    assert all(ln.fatura_id for ln in efetivados)
    antes = session.scalar(select(func.count(Lancamento.id)))

    props2 = propor_sincronizacao(session, cliente_fake(), ["it1"], HOJE_TESTE, desde=date(2026, 9, 1))
    assert props2[1].conta.id == cartao.id
    assert {ln.status for ln in props2[1].proposta.linhas} == {"duplicada"}
    aplicar_importacao(session, props2[1].proposta, hoje=HOJE_TESTE)
    assert session.scalar(select(func.count(Lancamento.id))) == antes


def test_status_dos_itens_e_alertas(session):
    item = carregar("item.json") | {"status": "LOGIN_ERROR", "lastUpdatedAt": "2026-09-20T00:00:00.000Z",
                                    "consentExpiresAt": "2026-10-20T00:00:00.000Z"}
    st = consultar_itens(session, cliente_fake(item=item), ["ok", "it-inexistente"], HOJE_TESTE)
    assert st[0].nome == "Banco Ficticio" and not st[0].ok
    assert any("login" in a for a in st[0].alertas) and any("há 15 dias" in a for a in st[0].alertas)
    assert any("expira em 20/10/2026" in a for a in st[0].alertas)
    assert st[1].erro and "não encontrado" in st[1].erro
    guardado = status_dos_itens(session)
    assert guardado["ok"].alertas == st[0].alertas and guardado["it-inexistente"].erro == st[1].erro


def test_item_saudavel_sem_alertas(session):
    assert consultar_itens(session, cliente_fake(), ["it1"], date(2026, 10, 5))[0].ok


def test_erro_de_transacoes_fica_na_conta(session, contas):
    props = propor_sincronizacao(session, cliente_fake(falha_transacoes=429), ["it1"], HOJE_TESTE)
    assert all(p.erro and "Tente mais tarde" in p.erro and p.proposta is None for p in props)


def test_chaves_invalidas_propagam(session):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(OpenFinanceErro, match="chaves"):
        propor_sincronizacao(session, ClientePluggy("i", "s", http), ["it1"], HOJE_TESTE)


def test_itens_configurados(session):
    adicionar_item(session, " abc ")
    adicionar_item(session, "abc")
    adicionar_item(session, "def")
    assert itens_configurados(session) == ["abc", "def"]
    assert get_config(session, "pluggy_itens") == ["abc", "def"]
    remover_item(session, "abc")
    assert itens_configurados(session) == ["def"]
    with pytest.raises(ValueError):
        adicionar_item(session, " ")
