from datetime import date

import pytest
from sqlalchemy import select

from app.models import Lancamento, Meta, Recorrencia
from app.services.metas import (
    aplicar_pague_primeiro, economia_do_mes, gasto_essencial, progresso_meta,
    registrar_saque_reserva, reserva, sincronizar_caixinha_meta,
)
from app.services.orcamento import definir_limite
from tests.factories import nova_categoria, nova_conta, novo_lancamento

HOJE = date(2026, 10, 5)


def _essencial(db):
    return nova_categoria(db, "Aluguel", grupo="fixo", essencial=True)


def test_progresso_com_saldo_da_conta(db):
    conta = nova_conta(db, "Viagem", saldo=200000)
    meta = Meta(nome="Viagem", tipo="objetivo", valor_alvo=1000000, prazo=date(2027, 1, 5), conta_id=conta.id)
    db.add(meta)
    db.flush()
    sincronizar_caixinha_meta(db, meta, HOJE)  # caixinha nova leva todo o Livre da conta
    p = progresso_meta(db, meta, HOJE)
    assert (p.atual, p.meses_restantes, p.necessario_mes, p.pct) == (200000, 3, 266667, 20)


def test_progresso_valor_manual_prazo_vencido_e_sem_prazo(db):
    meta = Meta(nome="Carro", tipo="objetivo", valor_alvo=500000, prazo=date(2026, 9, 1), valor_manual=125000)
    p = progresso_meta(db, meta, HOJE)
    assert (p.atual, p.meses_restantes, p.necessario_mes, p.pct) == (125000, 1, 375000, 25)
    sem_prazo = Meta(nome="Sonho", tipo="objetivo", valor_alvo=500000)
    assert progresso_meta(db, sem_prazo, HOJE).necessario_mes == 500000
    concluida = Meta(nome="Ok", tipo="objetivo", valor_alvo=1000, valor_manual=5000, prazo=date(2027, 1, 1))
    p = progresso_meta(db, concluida, HOJE)
    assert (p.necessario_mes, p.pct) == (0, 500)


def test_economia_do_mes_contra_a_meta(db):
    conta = nova_conta(db)
    db.add(Meta(nome="Economizar", tipo="economia_mensal", valor_alvo=50000))
    novo_lancamento(db, conta, date(2026, 10, 1), 500000)
    novo_lancamento(db, conta, date(2026, 10, 2), -400000)
    novo_lancamento(db, conta, date(2026, 10, 20), -30000, status="pendente")
    e = economia_do_mes(db, "2026-10")
    assert (e.meta, e.realizado, e.projetado) == (50000, 100000, 70000)
    assert e.atingida_realizado and e.atingida_projetado
    assert economia_do_mes(db, "2026-11").realizado == 0
    db.query(Meta).delete()
    assert economia_do_mes(db, "2026-10") is None


def _gastos_essenciais(db, conta, cat, por_mes):
    for mes, valor in por_mes.items():
        novo_lancamento(db, conta, date(2026, mes, 10), -valor, categoria=cat)


def test_gasto_essencial_base_3_meses(db):
    conta = nova_conta(db)
    cat = _essencial(db)
    lazer = nova_categoria(db, "Lazer", natureza="desejo")
    _gastos_essenciais(db, conta, cat, {6: 999999, 7: 30000, 8: 60000, 9: 90000, 10: 777777})
    novo_lancamento(db, conta, date(2026, 9, 11), -50000, categoria=lazer)
    novo_lancamento(db, conta, date(2026, 9, 12), -50000, categoria=cat, ignorar=True)
    assert gasto_essencial(db, HOJE) == (60000, "3_meses")


def test_gasto_essencial_conta_subcategoria_de_essencial(db):
    conta = nova_conta(db)
    pai = _essencial(db)
    filha = nova_categoria(db, "Condomínio", pai=pai, grupo="fixo", essencial=False)
    novo_lancamento(db, conta, date(2026, 7, 10), -10000, categoria=pai)
    novo_lancamento(db, conta, date(2026, 9, 10), -20000, categoria=filha)
    assert gasto_essencial(db, HOJE) == (10000, "3_meses")


def test_gasto_essencial_base_n_meses(db):
    conta = nova_conta(db)
    cat = _essencial(db)
    _gastos_essenciais(db, conta, cat, {8: 40000, 9: 80000, 10: 5000})
    assert gasto_essencial(db, HOJE) == (60000, "n_meses")


def test_gasto_essencial_base_previsto(db):
    conta = nova_conta(db)
    cat = _essencial(db)
    lazer = nova_categoria(db, "Lazer", natureza="desejo")
    novo_lancamento(db, conta, date(2026, 10, 2), -1000, categoria=cat)  # só o mês corrente: sem mês completo
    db.add_all([
        Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=conta.id, categoria_id=cat.id, valor=100000,
                    dia=5, inicio="2026-10"),
        Recorrencia(descricao="Netflix", tipo="despesa", conta_id=conta.id, categoria_id=lazer.id, valor=4000,
                    dia=5, inicio="2026-10"),
        Recorrencia(descricao="Antigo", tipo="despesa", conta_id=conta.id, categoria_id=cat.id, valor=9000,
                    dia=5, inicio="2026-01", ativa=False),
    ])
    definir_limite(db, "2026-10", cat.id, 20000)
    definir_limite(db, "2026-10", lazer.id, 99999)
    # mesma categoria com recorrência (100.000) e orçamento (20.000): vale o maior, sem somar
    assert gasto_essencial(db, HOJE) == (100000, "previsto")


def test_gasto_essencial_previsto_soma_categorias_e_usa_o_maior_de_cada(db):
    conta = nova_conta(db)
    aluguel = _essencial(db)
    mercado = nova_categoria(db, "Mercado", essencial=True)
    luz = nova_categoria(db, "Energia", grupo="fixo", essencial=True)
    db.add(Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=conta.id, categoria_id=aluguel.id,
                       valor=100000, dia=5, inicio="2026-10"))
    definir_limite(db, "2026-10", aluguel.id, 90000)   # recorrência maior: 100.000
    definir_limite(db, "2026-10", mercado.id, 80000)   # só orçamento: 80.000
    db.add(Recorrencia(descricao="Luz", tipo="despesa", conta_id=conta.id, categoria_id=luz.id,
                       valor=15000, dia=12, inicio="2026-10"))
    definir_limite(db, "2026-10", luz.id, 25000)       # orçamento maior: 25.000
    assert gasto_essencial(db, HOJE) == (205000, "previsto")


def _reserva(db, saldo):
    cat = _essencial(db)
    corrente = nova_conta(db, "Corrente")
    _gastos_essenciais(db, corrente, cat, {7: 100000, 8: 100000, 9: 100000})
    conta = nova_conta(db, "Reserva", saldo=saldo)
    meta = Meta(nome="Reserva", tipo="reserva_emergencia", conta_id=conta.id, multiplicador_reserva=3)
    db.add(meta)
    db.flush()
    sincronizar_caixinha_meta(db, meta, HOJE)
    return meta


def test_reserva_degraus_e_meses_que_aguento(db):
    meta = _reserva(db, 250000)
    r = reserva(db, meta, HOJE)
    assert (r.gasto_essencial, r.base, r.alvo, r.atual) == (100000, "3_meses", 300000, 250000)
    assert r.meses_que_aguento == 2.5
    assert [(d.rotulo, d.valor, d.atingido) for d in r.degraus] == [
        ("R$ 1.000", 100000, True), ("1 mês", 100000, True), ("3 meses", 300000, False), ("6 meses", 600000, False),
    ]
    assert r.proximo_degrau.rotulo == "3 meses"


def test_reserva_multiplicador_6_e_primeiro_degrau(db):
    meta = _reserva(db, 50000)
    meta.multiplicador_reserva = 6
    r = reserva(db, meta, HOJE)
    assert (r.alvo, r.meses_que_aguento, r.proximo_degrau.rotulo) == (600000, 0.5, "R$ 1.000")
    meta.valor_manual, meta.conta_id = 700000, None
    sincronizar_caixinha_meta(db, meta, HOJE)  # fora do app: a caixinha antiga fica na conta, sem a meta
    r = reserva(db, meta, HOJE)
    assert r.proximo_degrau is None and r.meses_que_aguento == 7.0


def test_reserva_sem_gasto_essencial_nao_divide_por_zero(db):
    conta = nova_conta(db, "Reserva", saldo=1000)
    meta = Meta(nome="Reserva", tipo="reserva_emergencia", conta_id=conta.id)
    db.add(meta)
    db.flush()
    r = reserva(db, meta, HOJE)
    assert (r.alvo, r.meses_que_aguento) == (0, None)


def _pendentes(db, rec):
    return list(db.scalars(select(Lancamento).where(
        Lancamento.recorrencia_id == rec.id, Lancamento.valor < 0).order_by(Lancamento.data)))


def test_pague_a_meta_primeiro_cria_e_atualiza_recorrencia(db):
    origem = nova_conta(db, "Corrente")
    destino = nova_conta(db, "Viagem")
    meta = Meta(nome="Viagem", tipo="objetivo", valor_alvo=1000000, conta_id=destino.id,
                pagar_primeiro=True, conta_origem_id=origem.id, dia_pagar=6, valor_mensal_pagar=50000)
    db.add(meta)
    db.flush()
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    assert (rec.tipo, rec.conta_id, rec.conta_destino_id, rec.valor, rec.dia, rec.origem_meta_id) == (
        "transferencia", origem.id, destino.id, 50000, 6, meta.id)
    pend = _pendentes(db, rec)
    assert [l.data for l in pend] == [date(2026, 10, 6), date(2026, 11, 6), date(2026, 12, 6)]
    assert all(l.status == "pendente" and l.valor == -50000 for l in pend)

    meta.valor_mensal_pagar, meta.dia_pagar = 70000, 8
    assert aplicar_pague_primeiro(db, meta, HOJE).id == rec.id
    pend = _pendentes(db, rec)
    assert [(l.data, l.valor) for l in pend] == [(date(2026, m, 8), -70000) for m in (10, 11, 12)]

    meta.pagar_primeiro = False
    assert aplicar_pague_primeiro(db, meta, HOJE) is None
    assert _pendentes(db, rec) == []
    assert db.scalars(select(Recorrencia).where(Recorrencia.origem_meta_id == meta.id)).one().ativa is False


def test_pague_a_meta_primeiro_exige_dados(db):
    meta = Meta(nome="X", tipo="objetivo", pagar_primeiro=True)
    db.add(meta)
    db.flush()
    with pytest.raises(ValueError):
        aplicar_pague_primeiro(db, meta, HOJE)


def test_saque_da_reserva_registra_checklist_e_cria_meta_repor(db):
    reserva_conta = nova_conta(db, "Reserva", saldo=500000)
    db.add(Meta(nome="Reserva", tipo="reserva_emergencia", conta_id=reserva_conta.id))
    lanc = novo_lancamento(db, reserva_conta, HOJE, -80000, descricao="Conserto do carro")
    nova = registrar_saque_reserva(db, lanc, {"inesperado", "necessario"}, HOJE)
    assert (nova.nome, nova.tipo, nova.valor_alvo, nova.prazo) == ("Repor reserva", "objetivo", 80000, date(2027, 4, 5))
    assert "Inesperado: sim" in lanc.observacao and "Necessário: sim" in lanc.observacao
    assert "Urgente: não" in lanc.observacao


def test_saque_so_vale_para_saida_da_conta_da_reserva(db):
    reserva_conta = nova_conta(db, "Reserva", saldo=500000)
    outra = nova_conta(db, "Outra")
    db.add(Meta(nome="Reserva", tipo="reserva_emergencia", conta_id=reserva_conta.id))
    entrada = novo_lancamento(db, reserva_conta, HOJE, 1000)
    de_outra = novo_lancamento(db, outra, HOJE, -1000)
    assert registrar_saque_reserva(db, entrada, set(), HOJE) is None
    assert registrar_saque_reserva(db, de_outra, set(), HOJE) is None
    assert db.scalars(select(Meta).where(Meta.nome == "Repor reserva")).first() is None
