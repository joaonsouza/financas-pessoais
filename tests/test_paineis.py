"""Ordem dos cards do painel lateral (Cartões e Contas) e das contas. Dados fictícios."""
import pytest

from app.services.paineis import PAINEIS, ordem_painel, salvar_ordem_painel
from app.services.saldos import saldos_contas
from tests.factories import nova_conta, novo_cartao


def test_padrao_e_a_ordem_de_sempre(db):
    assert ordem_painel(db, "cartoes") == list(PAINEIS["cartoes"])
    assert ordem_painel(db, "contas") == list(PAINEIS["contas"])


def test_salvar_ignora_desconhecidos_e_completa_o_resto(db):
    salvar_ordem_painel(db, "cartoes", ["vip", "xyz", "assinaturas", "vip"])
    assert ordem_painel(db, "cartoes") == ["vip", "assinaturas"] + [
        p for p in PAINEIS["cartoes"] if p not in ("vip", "assinaturas")]
    assert ordem_painel(db, "contas") == list(PAINEIS["contas"])  # cada página tem a sua


def test_pagina_desconhecida(db):
    with pytest.raises(KeyError):
        salvar_ordem_painel(db, "xyz", [])


def test_rota_de_cartoes_salva_e_a_tela_segue(client, db):
    c = novo_cartao(db, pagadora=nova_conta(db))
    c.vip_vouchers = 4
    db.commit()
    assert client.post("/cartoes/painel/ordem", data={"ids": ["assinaturas", "vip", "melhor"]}).status_code == 204
    texto = client.get("/cartoes").text
    pos = [texto.index(f'data-painel="{p}"') for p in ("assinaturas", "vip", "melhor", "limite")]
    assert pos == sorted(pos)


def test_rota_de_contas_salva_e_a_tela_segue(client, db):
    nova_conta(db)
    db.commit()
    assert client.post("/contas/painel/ordem", data={"ids": ["total", "mes"]}).status_code == 204
    texto = client.get("/contas").text
    pos = [texto.index(f'data-painel="{p}"') for p in ("total", "mes", "liquido", "dia")]
    assert pos == sorted(pos)


def test_ordem_das_contas_vale_nas_telas(client, db):
    a, b, c = nova_conta(db, "Conta A"), nova_conta(db, "Conta B"), nova_conta(db, "Conta C")
    db.commit()
    assert client.post("/contas/ordem", data={"ids": [str(c.id), str(a.id)]}).status_code == 204
    assert [x.nome for x in saldos_contas(db, a.data_saldo_inicial, "2026-10")] == ["Conta C", "Conta A", "Conta B"]
    texto = client.get("/contas").text
    assert texto.index("Conta C") < texto.index("Conta A") < texto.index("Conta B")
    assert 'data-conta="' in texto
    client.post("/contas", data={"nome": "Conta D", "tipo": "corrente"})
    assert [x.nome for x in saldos_contas(db, a.data_saldo_inicial, "2026-10")][-1] == "Conta D"
