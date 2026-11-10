"""Formulário de despesa no cartão: parcelar o total e faturas com o nome do vencimento. Dados fictícios."""
from datetime import date

from sqlalchemy import select

from app.models import Fatura, Lancamento
from tests.factories import nova_conta, novo_cartao, novo_lancamento


def _cartao(db):
    conta = nova_conta(db, "Nubank")
    cartao = novo_cartao(db, "Cartão Nubank", pagadora=conta, fechamento=23, vencimento=1)
    db.commit()
    return cartao


def test_form_tem_parcelar_compra_e_faturas_pelo_vencimento(client, db):
    cartao = _cartao(db)
    texto = client.get(f"/lancamentos/novo?tipo=cartao&conta={cartao.id}").text
    assert "Parcelar compra" in texto and "repetir === 'parcelada'" in texto
    assert "Vence 01/11/2026" in texto  # fatura que fecha em 23/10


def test_parcelar_divide_o_total_em_faturas_seguidas(client, db):
    cartao = _cartao(db)
    resposta = client.post("/lancamentos", data={
        "tipo": "cartao", "valor": "600,00", "descricao": "TV", "data": "2026-10-05", "conta_id": str(cartao.id),
        "efetivado": "on", "repetir": "parcelada", "parcelas": "3"})
    assert resposta.status_code == 200
    parcelas = db.scalars(select(Lancamento).where(Lancamento.descricao.like("TV%")).order_by(Lancamento.data)).all()
    assert [(l.valor, l.parcela_num, l.parcela_total) for l in parcelas] == [(-20000, 1, 3), (-20000, 2, 3), (-20000, 3, 3)]
    comps = [db.get(Fatura, l.fatura_id).competencia for l in parcelas]
    assert comps == ["2026-10", "2026-11", "2026-12"]


def test_form_de_cartao_nao_tem_paga_efetivada(client, db):
    cartao = _cartao(db)
    assert "Paga / efetivada" not in client.get(f"/lancamentos/novo?tipo=cartao&conta={cartao.id}").text
    assert "Paga / efetivada" in client.get("/lancamentos/novo?tipo=despesa").text


def test_compra_lancada_a_mao_fica_pendente_ate_a_fatura_importada(client, db):
    cartao = _cartao(db)
    client.post("/lancamentos", data={
        "tipo": "cartao", "valor": "50,00", "descricao": "Livro", "data": "2026-10-05", "conta_id": str(cartao.id)})
    assert db.scalar(select(Lancamento).where(Lancamento.descricao == "Livro")).status == "pendente"


def test_editar_compra_no_cartao_mantem_o_status(client, db):
    cartao = _cartao(db)
    importada = novo_lancamento(db, cartao, date(2026, 10, 5), -5000, descricao="Livro")
    db.commit()
    resposta = client.post(f"/lancamentos/{importada.id}", data={
        "tipo": "cartao", "valor": "50,00", "descricao": "Livro usado", "data": "2026-10-05",
        "conta_id": str(cartao.id)})
    assert resposta.status_code == 200
    db.refresh(importada)
    assert (importada.descricao, importada.status) == ("Livro usado", "efetivado")
