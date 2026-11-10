"""Aba "Por mês" de Cartões: a fatura que vence no mês escolhido, aberta ou fechada."""
from datetime import date

from app.services.faturas import fatura_do_mes
from app.services.lancamentos import criar_compra_parcelada, criar_lancamento
from tests.factories import nova_conta, novo_cartao

HOJE = date(2026, 10, 5)


def test_fatura_do_mes_pega_a_que_vence_no_mes(session):
    pagadora = nova_conta(session)
    # fecha 3, vence 10: a fatura de outubro fecha em 03/10
    mesmo_mes = novo_cartao(session, "Cartão A", pagadora=pagadora, fechamento=3, vencimento=10)
    # fecha 28, vence 5: a fatura de outubro fechou em 28/09
    mes_seguinte = novo_cartao(session, "Cartão B", pagadora=pagadora, fechamento=28, vencimento=5)
    criar_lancamento(session, conta=mesmo_mes, data=date(2026, 9, 20), descricao="Mercado",
                     valor_abs=12000, tipo="despesa", efetivado=True)
    criar_lancamento(session, conta=mes_seguinte, data=date(2026, 9, 20), descricao="Farmácia",
                     valor_abs=4500, tipo="despesa", efetivado=True)

    a = fatura_do_mes(session, mesmo_mes, "2026-10", HOJE)
    b = fatura_do_mes(session, mes_seguinte, "2026-10", HOJE)

    assert (a["competencia"], a["total"], a["vencimento"]) == ("2026-10", 12000, date(2026, 10, 10))
    assert a["status_chave"] == "fechada"
    assert (b["competencia"], b["total"], b["vencimento"]) == ("2026-09", 4500, date(2026, 10, 5))


def test_fatura_do_mes_sem_compras_e_aberta_no_futuro(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session))

    r = fatura_do_mes(session, cartao, "2026-12", HOJE)

    assert (r["total"], r["status_chave"], r["fechamento"]) == (0, "aberta", date(2026, 12, 3))


def test_tela_tem_aba_por_mes_com_seletor(client, db):
    cartao = novo_cartao(db, "Cartão Itaú", pagadora=nova_conta(db))
    criar_lancamento(db, conta=cartao, data=date(2026, 8, 1), descricao="Mercado",
                     valor_abs=9900, tipo="despesa", efetivado=True)
    db.commit()

    texto = client.get("/cartoes?mes=2026-08").text

    assert "Por mês" in texto and "aba: 'mes'" in texto
    assert "Agosto 2026" in texto
    assert "R$ 99,00" in texto
    assert f"/cartoes/{cartao.id}?fatura=2026-08" in texto
    assert "/cartoes?mes=2026-07" in texto and "/cartoes?mes=2026-09" in texto


def test_tela_tem_resumo_das_parcelas_com_link(client, db):
    cartao = novo_cartao(db, "Cartão Itaú", pagadora=nova_conta(db))
    criar_compra_parcelada(db, conta=cartao, data=date(2026, 10, 2), descricao="TV",
                           valor_total=30000, parcelas=3, efetivado_primeira=True)
    db.commit()

    texto = client.get("/cartoes").text

    assert 'href="/cartoes/parcelas"' in texto and "Falta pagar em parcelas" in texto
    assert "R$ 200,00" in texto  # as 2 parcelas pendentes de R$ 100,00
    assert "1 compra parcelada" in texto


def test_tela_sem_mes_abre_nas_faturas_abertas(client, db):
    novo_cartao(db, pagadora=nova_conta(db))
    db.commit()

    assert "aba: 'abertas'" in client.get("/cartoes").text
