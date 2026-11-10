from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import Lancamento, Recorrencia
from app.services.consulta_lancamentos import (
    FiltrosLancamento,
    agrupar_por_dia,
    listar_lancamentos,
    totais_da_lista,
)
from app.services.entrada_rapida import NovaEntrada, registrar_entrada, sugestao_por_descricao
from app.services.lancamentos import criar_lancamento, obter_tags
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento

HOJE = date(2026, 10, 5)


def _entrada(conta, **kw):
    dados = dict(tipo="despesa", valor=1250, descricao="Padaria", data=date(2026, 10, 2), conta_id=conta.id)
    dados.update(kw)
    return NovaEntrada(**dados)


def test_despesa_simples(session):
    conta = nova_conta(session)
    [lanc] = registrar_entrada(session, _entrada(conta), HOJE)
    assert (lanc.valor, lanc.status) == (-1250, "efetivado")


def test_cartao_exige_cartao_e_vice_versa(session):
    conta = nova_conta(session)
    cartao = novo_cartao(session, pagadora=conta)
    with pytest.raises(ValueError, match="cartão"):
        registrar_entrada(session, _entrada(conta, tipo="cartao"), HOJE)
    with pytest.raises(ValueError):
        registrar_entrada(session, _entrada(cartao, tipo="despesa"), HOJE)


def test_parcelada(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session))
    lancs = registrar_entrada(session, _entrada(cartao, tipo="cartao", valor=30000, repetir="parcelada", parcelas=3), HOJE)
    assert [l.valor for l in lancs] == [-10000, -10000, -10000]


def test_observacao_fica_em_todas_as_parcelas_e_nas_duas_pernas(session):
    conta = nova_conta(session)
    cartao = novo_cartao(session, pagadora=conta)
    lancs = registrar_entrada(session, _entrada(cartao, tipo="cartao", valor=30000, repetir="parcelada", parcelas=3,
                                                observacao="presente fictício"), HOJE)
    assert [l.observacao for l in lancs] == ["presente fictício"] * 3
    destino = nova_conta(session, "B")
    pernas = registrar_entrada(session, _entrada(conta, tipo="transferencia", conta_destino_id=destino.id,
                                                 observacao="acerto fictício"), HOJE)
    assert [l.observacao for l in pernas] == ["acerto fictício"] * 2


def test_repetida_lanca_o_mesmo_valor_todo_mes(session):
    conta = nova_conta(session)
    lancs = registrar_entrada(session, _entrada(conta, tipo="receita", valor=50000, descricao="Aula fictícia",
                                                repetir="repetida", parcelas=3, observacao="obs"), HOJE)
    assert [(l.valor, l.data, l.tipo, l.parcela_num, l.parcela_total, l.observacao) for l in lancs] == [
        (50000, date(2026, 10, 2), "receita", 1, 3, "obs"),
        (50000, date(2026, 11, 2), "receita", 2, 3, "obs"),
        (50000, date(2026, 12, 2), "receita", 3, 3, "obs")]
    assert [l.status for l in lancs] == ["efetivado", "pendente", "pendente"]
    assert len({l.grupo_parcela_id for l in lancs}) == 1


def test_repetida_no_cartao_cai_em_faturas_seguidas(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session), fechamento=8, vencimento=15)
    lancs = registrar_entrada(session, _entrada(cartao, tipo="cartao", valor=10000, repetir="repetida", parcelas=2), HOJE)
    assert [l.valor for l in lancs] == [-10000, -10000]
    assert lancs[0].fatura_id != lancs[1].fatura_id


def test_repetida_exige_duas_vezes_e_nao_vale_para_transferencia(session):
    a, b = nova_conta(session, "A"), nova_conta(session, "B")
    with pytest.raises(ValueError, match="2 vezes"):
        registrar_entrada(session, _entrada(a, repetir="repetida", parcelas=1), HOJE)
    with pytest.raises(ValueError, match="transferência"):
        registrar_entrada(session, _entrada(a, tipo="transferencia", conta_destino_id=b.id, repetir="repetida",
                                            parcelas=2), HOJE)


def test_receita_parcelada_falha(session):
    conta = nova_conta(session)
    with pytest.raises(ValueError, match="parcelad"):
        registrar_entrada(session, _entrada(conta, tipo="receita", repetir="parcelada", parcelas=2), HOJE)


def test_fixa_cria_recorrencia_sem_duplicar_mes_atual(session):
    conta = nova_conta(session)
    registrar_entrada(session, _entrada(conta, descricao="Aluguel", valor=120000, data=date(2026, 10, 10),
                                        repetir="fixa", efetivado=False), HOJE)
    rec = session.scalars(select(Recorrencia)).one()
    assert (rec.dia, rec.inicio, rec.valor, rec.tipo) == (10, "2026-10", 120000, "despesa")
    comps = list(session.scalars(select(Lancamento.competencia_recorrencia).where(Lancamento.recorrencia_id == rec.id)
                                 .order_by(Lancamento.data)))
    assert comps == ["2026-10", "2026-11", "2026-12"]


def test_transferencia_fixa(session):
    a, b = nova_conta(session, "A"), nova_conta(session, "B", tipo="investimento")
    registrar_entrada(session, _entrada(a, tipo="transferencia", descricao="Reserva", conta_destino_id=b.id,
                                        repetir="fixa"), HOJE)
    rec = session.scalars(select(Recorrencia)).one()
    assert rec.conta_destino_id == b.id
    assert session.scalar(select(func.count(Lancamento.id)).where(Lancamento.recorrencia_id == rec.id)) == 6


def test_transferencia_sem_destino(session):
    conta = nova_conta(session)
    with pytest.raises(ValueError, match="destino"):
        registrar_entrada(session, _entrada(conta, tipo="transferencia"), HOJE)


def test_descricao_obrigatoria(session):
    conta = nova_conta(session)
    with pytest.raises(ValueError, match="descrição"):
        registrar_entrada(session, _entrada(conta, descricao="  "), HOJE)


def test_sugestao_por_descricao(session):
    conta = nova_conta(session)
    mercado = nova_categoria(session)
    criar_lancamento(session, conta=conta, data=date(2026, 10, 1), descricao="Padaria", valor_abs=1,
                     tipo="despesa", categoria_id=mercado.id)
    assert sugestao_por_descricao(session, "padaria") == {"categoria_id": mercado.id, "conta_id": conta.id}
    assert sugestao_por_descricao(session, "nada") == {}


def test_listar_com_filtros_e_agrupar(session):
    conta = nova_conta(session)
    outra = nova_conta(session, "Itaú")
    alimentacao = nova_categoria(session, "Alimentação")
    mercado = nova_categoria(session, "Mercado", pai=alimentacao)
    l1 = novo_lancamento(session, conta, date(2026, 10, 2), -1000, categoria=mercado, descricao="Mercado Extra")
    l1.tags = obter_tags(session, ["casa"])
    novo_lancamento(session, conta, date(2026, 10, 2), 5000, descricao="Pix recebido")
    novo_lancamento(session, outra, date(2026, 10, 3), -300, status="pendente", descricao="Uber")
    novo_lancamento(session, conta, date(2026, 10, 4), -999, status="cancelado")
    novo_lancamento(session, conta, date(2026, 11, 1), -1)
    session.flush()

    todos = listar_lancamentos(session, "2026-10")
    assert [l.descricao for l in todos] == ["Uber", "Pix recebido", "Mercado Extra"]
    assert [d for d, _ in agrupar_por_dia(todos)] == [date(2026, 10, 3), date(2026, 10, 2)]
    assert totais_da_lista(todos) == (5000, 1300)

    def descricoes(**f):
        return [l.descricao for l in listar_lancamentos(session, "2026-10", FiltrosLancamento(**f))]

    assert descricoes(categoria_id=alimentacao.id) == ["Mercado Extra"]
    assert descricoes(conta_id=outra.id) == ["Uber"]
    assert descricoes(tipo="receita") == ["Pix recebido"]
    assert descricoes(status="pendente") == ["Uber"]
    assert descricoes(tag="Casa") == ["Mercado Extra"]
    assert descricoes(busca="extra") == ["Mercado Extra"]


def test_checklist_da_reserva_cria_meta_de_reposicao(db):
    from app.models import Meta
    from app.services.entrada_rapida import NovaEntrada, registrar_entrada

    reserva_conta = nova_conta(db, "Reserva", saldo=500000)
    db.add(Meta(nome="Reserva", tipo="reserva_emergencia", conta_id=reserva_conta.id))
    (lanc,) = registrar_entrada(db, NovaEntrada(
        tipo="despesa", valor=30000, descricao="Dentista", data=date(2026, 10, 5), conta_id=reserva_conta.id,
        checklist_reserva=["enviado", "urgente"]), date(2026, 10, 5))
    assert "Urgente: sim" in lanc.observacao and "Inesperado: não" in lanc.observacao
    repor = db.query(Meta).filter(Meta.nome == "Repor reserva").one()
    assert repor.valor_alvo == 30000


def test_fixa_anual_com_fim(session):
    conta = nova_conta(session)
    registrar_entrada(session, _entrada(conta, tipo="receita", repetir="fixa", intervalo=12,
                                        fim="2028-12", efetivado=False), HOJE)
    rec = session.scalar(select(Recorrencia))
    assert (rec.intervalo_meses, rec.fim) == (12, "2028-12")


def test_fim_antes_do_inicio_e_invalido(session):
    conta = nova_conta(session)
    with pytest.raises(ValueError, match="fim"):
        registrar_entrada(session, _entrada(conta, repetir="fixa", fim="2020-01"), HOJE)


def test_intervalo_invalido(session):
    conta = nova_conta(session)
    with pytest.raises(ValueError, match="intervalo"):
        registrar_entrada(session, _entrada(conta, repetir="fixa", intervalo=0), HOJE)


def test_repetida_a_cada_3_meses(session):
    conta = nova_conta(session)
    lancs = registrar_entrada(session, _entrada(conta, repetir="repetida", parcelas=3, intervalo=3), HOJE)
    assert [l.data for l in lancs] == [date(2026, 10, 2), date(2027, 1, 2), date(2027, 4, 2)]


def test_fixa_semanal_toda_quinta_nao_duplica_a_primeira(session):
    conta = nova_conta(session)
    registrar_entrada(session, _entrada(conta, repetir="fixa", unidade="semana", data=date(2026, 10, 8),
                                        efetivado=False), HOJE)
    rec = session.scalar(select(Recorrencia))
    assert (rec.frequencia, rec.dias_semana, rec.intervalo_semanas) == ("semanal", "3", 1)
    datas = [l.data for l in session.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id)
                                             .order_by(Lancamento.data))]
    assert datas[:3] == [date(2026, 10, 8), date(2026, 10, 15), date(2026, 10, 22)]
    assert len(datas) == len(set(datas))


def test_fixa_semanal_inclui_o_dia_da_data_escolhida(session):
    conta = nova_conta(session)
    registrar_entrada(session, _entrada(conta, repetir="fixa", unidade="semana", dias_semana=[0, 4],
                                        data=date(2026, 10, 8)), HOJE)  # quinta
    assert session.scalar(select(Recorrencia)).dias_semana == "0,3,4"


def test_fixa_anual_a_cada_2_anos_ate_data_e_n_vezes(session):
    conta = nova_conta(session)
    registrar_entrada(session, _entrada(conta, repetir="fixa", unidade="ano", intervalo=2,
                                        fim_data=date(2040, 1, 1), fim_vezes=3), HOJE)
    rec = session.scalar(select(Recorrencia))
    assert (rec.intervalo_meses, rec.fim_data, rec.max_ocorrencias) == (24, date(2040, 1, 1), 3)


def test_fim_data_antes_do_inicio_e_unidade_invalida(session):
    conta = nova_conta(session)
    with pytest.raises(ValueError, match="fim"):
        registrar_entrada(session, _entrada(conta, repetir="fixa", fim_data=date(2020, 1, 1)), HOJE)
    with pytest.raises(ValueError, match="unidade"):
        registrar_entrada(session, _entrada(conta, repetir="fixa", unidade="dia"), HOJE)
