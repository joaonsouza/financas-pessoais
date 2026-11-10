from datetime import date

import pytest
from sqlalchemy import select

from app.models import Fatura, Lancamento
from app.services.lancamentos import atualizar_lancamento, criar_compra_parcelada, criar_lancamento
from tests.factories import nova_conta, novo_cartao


def _cartao(db):
    return novo_cartao(db, pagadora=nova_conta(db), fechamento=3)


def test_criar_lancamento_com_fatura_escolhida(session):
    cartao = _cartao(session)
    lanc = criar_lancamento(session, conta=cartao, data=date(2026, 10, 2), descricao="X", valor_abs=1000,
                            tipo="despesa", fatura_competencia="2026-12")
    assert session.get(Fatura, lanc.fatura_id).competencia == "2026-12"


def test_criar_lancamento_fatura_automatica_e_invalida(session):
    cartao = _cartao(session)
    lanc = criar_lancamento(session, conta=cartao, data=date(2026, 10, 4), descricao="X", valor_abs=1000,
                            tipo="despesa")
    assert session.get(Fatura, lanc.fatura_id).competencia == "2026-11"
    with pytest.raises(ValueError):
        criar_lancamento(session, conta=cartao, data=date(2026, 10, 4), descricao="X", valor_abs=1000,
                         tipo="despesa", fatura_competencia="lixo")


def test_parcelada_com_fatura_base(session):
    cartao = _cartao(session)
    parcelas = criar_compra_parcelada(session, conta=cartao, data=date(2026, 10, 2), descricao="TV",
                                      valor_total=3000, parcelas=3, fatura_competencia="2026-11")
    comps = [session.get(Fatura, p.fatura_id).competencia for p in parcelas]
    assert comps == ["2026-11", "2026-12", "2027-01"]


def test_atualizar_fatura_tem_precedencia_sobre_data(session):
    cartao = _cartao(session)
    lanc = criar_lancamento(session, conta=cartao, data=date(2026, 10, 2), descricao="X", valor_abs=1000,
                            tipo="despesa")
    kw = dict(data=date(2026, 10, 2), descricao="X", valor_abs=1000, categoria_id=None, tags=[])
    atualizar_lancamento(session, lanc, fatura_competencia="2026-12", **kw)
    assert session.get(Fatura, lanc.fatura_id).competencia == "2026-12"
    atualizar_lancamento(session, lanc, **kw)  # sem informar: recalcula pela data
    assert session.get(Fatura, lanc.fatura_id).competencia == "2026-10"


def test_rota_criar_com_fatura_e_opcoes_no_modal(client, db):
    cartao = _cartao(db)
    db.commit()
    modal = client.get("/lancamentos/novo?tipo=cartao").text
    assert 'name="fatura"' in modal and "Automática (pela data)" in modal
    for rotulo in ("Outubro 2026", "Novembro 2026", "Dezembro 2026", "Janeiro 2027"):
        assert rotulo in modal
    assert 'name="fatura"' not in client.get("/lancamentos/novo?tipo=despesa").text
    client.post("/lancamentos", data={
        "tipo": "cartao", "valor": "10,00", "descricao": "Cine", "data": "2026-10-02",
        "conta_id": str(cartao.id), "fatura": "2026-11", "repetir": "nao",
    })
    lanc = db.scalars(select(Lancamento)).one()
    assert db.get(Fatura, lanc.fatura_id).competencia == "2026-11"


def test_rota_edicao_traz_fatura_atual(client, db):
    cartao = _cartao(db)
    lanc = criar_lancamento(db, conta=cartao, data=date(2026, 10, 2), descricao="X", valor_abs=1000,
                            tipo="despesa", fatura_competencia="2026-12")
    db.commit()
    modal = client.get(f"/lancamentos/{lanc.id}/editar").text
    assert '<option value="2026-12" selected>' in modal
    client.post(f"/lancamentos/{lanc.id}", data={
        "valor": "10,00", "descricao": "X", "data": "2026-10-02", "conta_id": str(cartao.id), "fatura": "2026-11",
    })
    db.refresh(lanc)
    assert db.get(Fatura, lanc.fatura_id).competencia == "2026-11"
