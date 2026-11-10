from app.services.faturas import obter_ou_criar_fatura
from app.services.parcelas import definir_teto_parcelas
from tests.factories import nova_conta, novo_cartao, novo_lancamento


def _parcelas_em_novembro(db, valor=-15000):
    cartao = novo_cartao(db, "Cartão Teste", pagadora=nova_conta(db))
    f = obter_ou_criar_fatura(db, cartao, "2026-11")
    l = novo_lancamento(db, cartao, f.data_vencimento, valor, status="pendente", descricao="Tv", fatura_id=f.id)
    l.grupo_parcela_id, l.parcela_num, l.parcela_total = "g1", 1, 3
    db.commit()


def test_resumo_de_parcelas_no_planejamento(client, db):
    _parcelas_em_novembro(db)
    r = client.get("/planejamento?mes=2026-11")
    assert r.status_code == 200
    assert "Parcelas do mês" in r.text and "150,00" in r.text and "2.000,00" in r.text
    assert 'href="/cartoes/parcelas"' in r.text and "acima do teto" not in r.text


def test_resumo_marca_mes_acima_do_teto(client, db):
    _parcelas_em_novembro(db, valor=-250000)
    r = client.get("/planejamento?mes=2026-11")
    assert "acima do teto" in r.text


def test_resumo_com_teto_zero_nao_alerta(client, db):
    _parcelas_em_novembro(db, valor=-250000)
    definir_teto_parcelas(db, 0)
    db.commit()
    r = client.get("/planejamento?mes=2026-11")
    assert "Parcelas do mês" in r.text and "acima do teto" not in r.text


def test_card_soma_parcela_ja_lancada_e_mostra_grafico(client, db):
    cartao = novo_cartao(db, "Cartão Teste", pagadora=nova_conta(db))
    f = obter_ou_criar_fatura(db, cartao, "2026-10")
    l = novo_lancamento(db, cartao, f.data_vencimento, -15000, status="efetivado", descricao="Tv", fatura_id=f.id)
    l.grupo_parcela_id, l.parcela_num, l.parcela_total = "g1", 1, 3
    db.commit()
    r = client.get("/planejamento?mes=2026-10")
    assert r.status_code == 200
    assert "150,00" in r.text and 'id="grafico-parcelas-mes"' in r.text
    assert "&#34;rotulos&#34;" not in r.text and '"rotulos"' in r.text and '"selecionado"' in r.text
    assert 'aria-label="Parcelas do mês contra o teto"' in r.text


def test_card_sem_parcelas_mostra_zero_sem_grafico(client, db):
    nova_conta(db)
    db.commit()
    r = client.get("/planejamento?mes=2026-10")
    assert r.status_code == 200 and "Parcelas do mês" in r.text and 'id="grafico-parcelas-mes"' not in r.text


def _teto_e_parcela(db, parcela=-300000, teto=700000, status="pendente"):
    from app.services.orcamento import definir_limite
    cartao = novo_cartao(db, "Cartão Teste", pagadora=nova_conta(db))
    f = obter_ou_criar_fatura(db, cartao, "2026-11")  # vence 10/11: conta no mês de novembro
    l = novo_lancamento(db, cartao, f.data_vencimento, parcela, status=status, descricao="Tv", fatura_id=f.id)
    l.grupo_parcela_id, l.parcela_num, l.parcela_total = "g1", 1, 3
    definir_limite(db, "2026-11", None, teto)
    db.commit()


def test_barra_do_teto_mostra_o_pedaco_das_parcelas(client, db):
    _teto_e_parcela(db)
    html = client.get("/planejamento?mes=2026-11").text
    assert 'style="width: 42%" title="Parcelas: R$ 3.000,00"' in html  # 3.000 de 7.000 = 42%
    assert "Parcelas R$ 3.000,00" in html and "Demais gastos R$ 0,00" in html


def test_barra_do_teto_sem_parcelas_continua_simples(client, db):
    from app.services.orcamento import definir_limite
    nova_conta(db)
    definir_limite(db, "2026-11", None, 700000)
    db.commit()
    html = client.get("/planejamento?mes=2026-11").text
    assert 'aria-label="Teto total"' in html and "Demais gastos" not in html


def test_parcelas_no_gasto_do_mes_segue_o_mes_do_vencimento(session):
    from app.services.parcelas import parcelas_no_gasto_do_mes
    cartao = novo_cartao(session, pagadora=nova_conta(session))
    for comp, status in (("2026-10", "efetivado"), ("2026-11", "pendente"), ("2026-12", "cancelado")):
        f = obter_ou_criar_fatura(session, cartao, comp)
        l = novo_lancamento(session, cartao, f.data_vencimento, -10000, status=status, fatura_id=f.id)
        l.grupo_parcela_id, l.parcela_num, l.parcela_total = "g", 1, 3
    session.flush()
    assert parcelas_no_gasto_do_mes(session, "2026-10") == 10000
    assert parcelas_no_gasto_do_mes(session, "2026-11") == 10000
    assert parcelas_no_gasto_do_mes(session, "2026-12") == 0  # cancelada não conta
    assert parcelas_no_gasto_do_mes(session, "2027-03") == 0


def test_pedaco_das_parcelas_na_barra_e_amarelo(client, db):
    _teto_e_parcela(db)
    html = client.get("/planejamento?mes=2026-11").text
    assert 'class="h-full bg-warning" style="width: 42%" title="Parcelas' in html
