from datetime import date

import pytest

from app.models import Lancamento
from app.services.lancamentos import criar_transferencia, editar_lote, excluir_lote
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento

D = date(2026, 10, 5)


def test_muda_categoria_so_do_mesmo_tipo(session):
    conta = nova_conta(session)
    mercado = nova_categoria(session, "Mercado")
    desp = novo_lancamento(session, conta, D, -1000)
    rec = novo_lancamento(session, conta, D, 5000)
    r = editar_lote(session, [desp.id, rec.id], categoria_id=mercado.id)
    assert (desp.categoria_id, rec.categoria_id) == (mercado.id, None)
    assert (r.alterados, r.inalterados) == (1, 1)


def test_muda_conta_e_pula_cartao(session):
    a, b = nova_conta(session, "A"), nova_conta(session, "B")
    cartao = novo_cartao(session, pagadora=a)
    normal = novo_lancamento(session, a, D, -1000)
    no_cartao = novo_lancamento(session, cartao, D, -500)
    r = editar_lote(session, [normal.id, no_cartao.id], conta_id=b.id)
    assert (normal.conta_id, no_cartao.conta_id) == (b.id, cartao.id)
    assert r.alterados == 1


def test_conta_destino_cartao_e_invalida(session):
    a = nova_conta(session)
    cartao = novo_cartao(session, pagadora=a)
    lanc = novo_lancamento(session, a, D, -100)
    with pytest.raises(ValueError, match="conta"):
        editar_lote(session, [lanc.id], conta_id=cartao.id)


def test_situacao_e_explicita_e_transferencia_move_o_par(session):
    a, b = nova_conta(session, "A"), nova_conta(session, "B")
    saida, entrada = criar_transferencia(session, origem=a, destino=b, data=D, valor_abs=100)
    editar_lote(session, [saida.id, entrada.id], status="efetivado")
    assert (saida.status, entrada.status) == ("efetivado", "efetivado")
    editar_lote(session, [saida.id, entrada.id], status="pendente")
    assert (saida.status, entrada.status) == ("pendente", "pendente")


def test_tags_sao_acrescentadas_sem_apagar_as_existentes(session):
    conta = nova_conta(session)
    lanc = novo_lancamento(session, conta, D, -100)
    editar_lote(session, [lanc.id], tags=["casa"])
    editar_lote(session, [lanc.id], tags=["Casa", "viagem"])
    assert sorted(t.nome for t in lanc.tags) == ["casa", "viagem"]


def test_ignorar_e_ajuste_nao_mexe(session):
    conta = nova_conta(session)
    desp = novo_lancamento(session, conta, D, -100)
    ajuste = novo_lancamento(session, conta, D, 50, tipo="ajuste")
    editar_lote(session, [desp.id, ajuste.id], ignorar=True)
    assert (desp.ignorar, ajuste.ignorar) == (True, False)


def test_status_invalido(session):
    with pytest.raises(ValueError, match="situação"):
        editar_lote(session, [], status="xpto")


def test_excluir_lote(session):
    conta = nova_conta(session)
    ids = [novo_lancamento(session, conta, D, -100 * i).id for i in (1, 2)]
    assert excluir_lote(session, [*ids, 99999]) == 2
    assert session.get(Lancamento, ids[0]) is None


def test_rota_modal_editar_lote(client, db):
    conta = nova_conta(db)
    nova_categoria(db, "Mercado")
    ids = [novo_lancamento(db, conta, D, -100 * i).id for i in (1, 2)]
    db.commit()
    texto = client.get("/lancamentos/editar-lote", params={"ids": ids}).text
    assert "2 lançamentos" in texto and "Mercado" in texto and 'name="categoria_id"' in texto
    assert f'value="{ids[0]}"' in texto


def test_rota_editar_lote(client, db):
    conta = nova_conta(db)
    cat = nova_categoria(db, "Mercado")
    lanc = novo_lancamento(db, conta, D, -100, status="pendente")
    db.commit()
    r = client.post("/lancamentos/editar-lote", data={
        "ids": [str(lanc.id)], "categoria_id": str(cat.id), "status": "efetivado", "tags": "casa, viagem",
        "ignorar": "", "conta_id": "",
    })
    assert r.headers["HX-Refresh"] == "true"
    db.expire_all()
    l = db.get(Lancamento, lanc.id)
    assert (l.categoria_id, l.status) == (cat.id, "efetivado")
    assert sorted(t.nome for t in l.tags) == ["casa", "viagem"]


def test_rota_editar_lote_erro_volta_no_modal(client, db):
    conta = nova_conta(db)
    lanc = novo_lancamento(db, conta, D, -100)
    db.commit()
    r = client.post("/lancamentos/editar-lote", data={"ids": [str(lanc.id)], "conta_id": "99999"})
    assert r.status_code == 422 and "conta inválida" in r.text


def test_rota_excluir_lote(client, db):
    conta = nova_conta(db)
    ids = [novo_lancamento(db, conta, D, -100 * i).id for i in (1, 2)]
    db.commit()
    r = client.post("/lancamentos/excluir-lote", data={"ids": [str(i) for i in ids]})
    assert r.headers["HX-Refresh"] == "true"
    db.expire_all()
    assert db.get(Lancamento, ids[0]) is None


def test_checkbox_de_efetivada_habilitado_e_com_valor(client, db):
    conta = nova_conta(db)
    novo_lancamento(db, conta, D, -1250, descricao="Padaria")
    db.commit()
    texto = client.get("/lancamentos?mes=2026-10").text
    assert 'data-valor="-1250"' in texto
    trecho = texto.split('aria-label="Selecionar Padaria"')[0].rsplit("<input", 1)[1]
    assert " disabled" not in trecho


def test_barra_de_selecao_tem_total_editar_e_excluir(client, db):
    conta = nova_conta(db)
    novo_lancamento(db, conta, D, -100)
    db.commit()
    texto = client.get("/lancamentos?mes=2026-10").text
    assert "/lancamentos/editar-lote" in texto and "/lancamentos/excluir-lote" in texto
    assert "formatarTotal()" in texto
