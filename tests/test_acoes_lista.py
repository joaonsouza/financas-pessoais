from datetime import date

import pytest
from sqlalchemy import select

from app.models import Lancamento, Recorrencia
from app.services.lancamentos import converter_em_transferencia, efetivar_lote
from app.services.resumo_mes import totais_mes
from app.services.saldos import saldo_atual
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento

D = date(2026, 10, 2)


def test_efetivar_lote_so_pendentes(session):
    conta = nova_conta(session)
    p1 = novo_lancamento(session, conta, D, -1000, status="pendente")
    p2 = novo_lancamento(session, conta, D, -2000, status="pendente")
    ok = novo_lancamento(session, conta, D, -500)
    canc = novo_lancamento(session, conta, D, -700, status="cancelado")
    assert efetivar_lote(session, [p1.id, p2.id, ok.id, canc.id, 99999]) == 2
    assert (p1.status, p2.status, ok.status, canc.status) == ("efetivado", "efetivado", "efetivado", "cancelado")
    assert saldo_atual(session, conta) == -3500


def test_efetivar_lote_transferencia_nao_alterna_duas_vezes(session):
    from app.services.lancamentos import criar_transferencia
    a, b = nova_conta(session, "A"), nova_conta(session, "B")
    saida, entrada = criar_transferencia(session, origem=a, destino=b, data=D, valor_abs=1000)
    assert efetivar_lote(session, [saida.id, entrada.id]) == 1
    assert saida.status == entrada.status == "efetivado"


def test_converter_despesa_em_transferencia(session):
    a, reserva = nova_conta(session, "Nubank", saldo=100000), nova_conta(session, "Reserva")
    cat = nova_categoria(session)
    lanc = novo_lancamento(session, a, D, -30000, categoria=cat, descricao="Pix pra Reserva")
    saida, entrada = converter_em_transferencia(session, lanc, reserva)
    assert saida is lanc
    assert (lanc.tipo, lanc.categoria_id, lanc.valor, lanc.conta_id) == ("transferencia", None, -30000, a.id)
    assert (entrada.tipo, entrada.valor, entrada.conta_id) == ("transferencia", 30000, reserva.id)
    assert (entrada.data, entrada.status, entrada.descricao) == (D, "efetivado", "Pix pra Reserva")
    assert lanc.transferencia_par_id == entrada.id and entrada.transferencia_par_id == lanc.id
    assert saldo_atual(session, a) == 70000 and saldo_atual(session, reserva) == 30000
    assert totais_mes(session, "2026-10").despesas_efetivado == 0


def test_converter_receita_em_transferencia_vira_outra_para_conta(session):
    a, outra = nova_conta(session, "A"), nova_conta(session, "Outra", saldo=5000)
    lanc = novo_lancamento(session, a, D, 8000, status="pendente")
    lanc_a, par = converter_em_transferencia(session, lanc, outra)
    assert (par.conta_id, par.valor, par.status) == (outra.id, -8000, "pendente")
    assert (lanc_a.conta_id, lanc_a.valor) == (a.id, 8000)


def test_converter_invalidos(session):
    a, b = nova_conta(session, "A"), nova_conta(session, "B")
    cartao = novo_cartao(session, pagadora=a)
    with pytest.raises(ValueError):
        converter_em_transferencia(session, novo_lancamento(session, a, D, -100), a)  # mesma conta
    with pytest.raises(ValueError):
        converter_em_transferencia(session, novo_lancamento(session, a, D, -100), cartao)  # destino cartão
    with pytest.raises(ValueError):
        converter_em_transferencia(session, novo_lancamento(session, cartao, D, -100), b)  # origem cartão
    parcela = novo_lancamento(session, a, D, -100)
    parcela.grupo_parcela_id, parcela.parcela_num, parcela.parcela_total = "g", 1, 2
    with pytest.raises(ValueError):
        converter_em_transferencia(session, parcela, b)
    rec = novo_lancamento(session, a, D, -100)
    r = Recorrencia(descricao="R", tipo="despesa", conta_id=a.id, valor=100, dia=2, inicio="2026-10")
    session.add(r)
    session.flush()
    rec.recorrencia_id = r.id
    with pytest.raises(ValueError):
        converter_em_transferencia(session, rec, b)
    with pytest.raises(ValueError):
        converter_em_transferencia(session, novo_lancamento(session, a, D, -100, tipo="ajuste"), b)


def test_rota_efetivar_lote(client, db):
    conta = nova_conta(db)
    ids = [novo_lancamento(db, conta, D, -100 * i, status="pendente").id for i in (1, 2, 3)]
    feito = novo_lancamento(db, conta, D, -50)
    db.commit()
    r = client.post("/lancamentos/efetivar-lote", data={"ids": [*map(str, ids), str(feito.id)]})
    assert r.status_code == 200 and r.headers["HX-Refresh"] == "true"
    db.expire_all()
    assert all(db.get(Lancamento, i).status == "efetivado" for i in ids)


def test_rota_efetivar_lote_sem_ids(client, db):
    nova_conta(db)
    db.commit()
    assert client.post("/lancamentos/efetivar-lote", data={}).headers["HX-Refresh"] == "true"


def test_lista_tem_checkbox_so_em_pendentes_e_barra(client, db):
    conta = nova_conta(db)
    pend = novo_lancamento(db, conta, D, -100, status="pendente", descricao="Pendente X")
    novo_lancamento(db, conta, D, -200, descricao="Feito Y")
    db.commit()
    texto = client.get("/lancamentos").text
    assert f'value="{pend.id}"' in texto and 'name="ids"' in texto
    assert texto.count('name="ids"') == 4  # um habilitado e um desabilitado, na lista (celular) e na tabela (PC)
    assert "disabled" in texto and "Efetivar" in texto and "selecionados" in texto


def test_menu_converter_so_para_elegiveis(client, db):
    a, _ = nova_conta(db, "A"), nova_conta(db, "B")
    cartao = novo_cartao(db, pagadora=a)
    ok = novo_lancamento(db, a, D, -100)
    nao = novo_lancamento(db, cartao, D, -100)
    db.commit()
    texto = client.get("/lancamentos").text
    assert f"/lancamentos/{ok.id}/converter" in texto
    assert f"/lancamentos/{nao.id}/converter" not in texto


def test_rota_converter(client, db):
    a, reserva = nova_conta(db, "Nubank"), nova_conta(db, "Reserva")
    lanc = novo_lancamento(db, a, D, -3000, descricao="Pix pra Reserva")
    db.commit()
    modal = client.get(f"/lancamentos/{lanc.id}/converter").text
    assert "Reserva" in modal and 'name="conta_destino_id"' in modal and ">Nubank<" not in modal
    r = client.post(f"/lancamentos/{lanc.id}/converter", data={"conta_destino_id": str(reserva.id)})
    assert r.status_code == 200 and r.headers["HX-Refresh"] == "true"
    db.expire_all()
    assert db.get(Lancamento, lanc.id).tipo == "transferencia"
    assert len(db.scalars(select(Lancamento)).all()) == 2


def test_rota_converter_invalido_422(client, db):
    a = nova_conta(db, "Nubank")
    lanc = novo_lancamento(db, a, D, -3000)
    db.commit()
    r = client.post(f"/lancamentos/{lanc.id}/converter", data={"conta_destino_id": str(a.id)})
    assert r.status_code == 422 and "role" in r.text
