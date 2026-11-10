from datetime import date

from sqlalchemy import select

from app.models import Fatura, Lancamento, Recorrencia
from app.services.lancamentos import excluir_lancamento
from app.services.recorrencia import (
    editar_recorrencia,
    encerrar_recorrencia,
    gerar_pendentes,
)
from tests.factories import nova_conta, novo_cartao

HOJE = date(2026, 10, 5)


def _rec(session, conta, **kw):
    dados = dict(descricao="Aluguel", tipo="despesa", conta_id=conta.id, valor=120000, dia=10, inicio="2026-10")
    dados.update(kw)
    rec = Recorrencia(**dados)
    session.add(rec)
    session.flush()
    return rec


def _gerados(session, rec):
    return list(session.scalars(
        select(Lancamento).where(Lancamento.recorrencia_id == rec.id).order_by(Lancamento.data, Lancamento.id)
    ))


def test_gera_mes_atual_mais_dois_e_e_idempotente(session):
    rec = _rec(session, nova_conta(session), dia=31)
    assert gerar_pendentes(session, HOJE) == 3
    assert gerar_pendentes(session, HOJE) == 0
    gerados = _gerados(session, rec)
    assert [l.data for l in gerados] == [date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31)]
    assert {(l.status, l.valor, l.origem) for l in gerados} == {("pendente", -120000, "recorrencia")}
    assert [l.competencia_recorrencia for l in gerados] == ["2026-10", "2026-11", "2026-12"]


def test_respeita_inicio_fim_e_ativa(session):
    conta = nova_conta(session)
    so_novembro = _rec(session, conta, inicio="2026-11", fim="2026-11")
    inativa = _rec(session, conta, descricao="Velha", ativa=False)
    gerar_pendentes(session, HOJE)
    assert [l.competencia_recorrencia for l in _gerados(session, so_novembro)] == ["2026-11"]
    assert _gerados(session, inativa) == []


def test_transferencia_recorrente_gera_duas_pernas(session):
    nubank = nova_conta(session, "Nubank")
    nuinvest = nova_conta(session, "NuInvest", tipo="investimento")
    rec = _rec(session, nubank, descricao="Pague a meta primeiro", tipo="transferencia",
               conta_destino_id=nuinvest.id, valor=50000, dia=30)
    gerar_pendentes(session, HOJE)
    gerados = _gerados(session, rec)
    assert len(gerados) == 6
    assert sorted(l.valor for l in gerados if l.competencia_recorrencia == "2026-10") == [-50000, 50000]


def test_assinatura_no_cartao_recebe_fatura(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session), fechamento=3)
    rec = _rec(session, cartao, descricao="Spotify", valor=4090, dia=15, eh_assinatura=True)
    gerar_pendentes(session, HOJE)
    primeiro = _gerados(session, rec)[0]
    assert session.get(Fatura, primeiro.fatura_id).competencia == "2026-11"


def test_valor_variavel_usa_ultimo_efetivado(session):
    conta = nova_conta(session, inicio=date(2026, 9, 1))
    rec = _rec(session, conta, descricao="Luz", valor=10000, valor_variavel=True, inicio="2026-09")
    session.add(Lancamento(conta_id=conta.id, data=date(2026, 9, 10), descricao="Luz", valor=-12345,
                           tipo="despesa", status="efetivado", recorrencia_id=rec.id,
                           competencia_recorrencia="2026-09"))
    session.flush()
    gerar_pendentes(session, HOJE)
    outubro = [l for l in _gerados(session, rec) if l.competencia_recorrencia == "2026-10"][0]
    assert outubro.valor == -12345


def test_editar_este_e_proximos(session):
    rec = _rec(session, nova_conta(session))
    gerar_pendentes(session, HOJE)
    editar_recorrencia(session, rec, a_partir="2026-11", hoje=HOJE, valor=150000)
    assert [l.valor for l in _gerados(session, rec)] == [-120000, -150000, -150000]


def test_encerrar_recorrencia(session):
    rec = _rec(session, nova_conta(session))
    gerar_pendentes(session, HOJE)
    encerrar_recorrencia(session, rec, a_partir="2026-11")
    assert rec.fim == "2026-10"
    assert [l.competencia_recorrencia for l in _gerados(session, rec)] == ["2026-10"]
    assert gerar_pendentes(session, HOJE) == 0


def test_cancelado_nao_e_regerado(session):
    rec = _rec(session, nova_conta(session))
    gerar_pendentes(session, HOJE)
    excluir_lancamento(session, _gerados(session, rec)[0])
    assert gerar_pendentes(session, HOJE) == 0


def test_intervalo_anual_gera_tres_anos(session):
    rec = _rec(session, nova_conta(session), descricao="13º", tipo="receita", inicio="2026-12",
               intervalo_meses=12, dia=20)
    assert gerar_pendentes(session, HOJE, 36) == 3
    assert [l.competencia_recorrencia for l in _gerados(session, rec)] == ["2026-12", "2027-12", "2028-12"]
    assert gerar_pendentes(session, HOJE, 36) == 0


def test_intervalo_anual_gera_a_do_ano_seguinte_quando_o_mes_passa(session):
    rec = _rec(session, nova_conta(session), descricao="13º", tipo="receita", inicio="2026-12",
               intervalo_meses=12, dia=20)
    gerar_pendentes(session, HOJE, 36)
    gerar_pendentes(session, date(2027, 1, 5), 36)
    assert [l.competencia_recorrencia for l in _gerados(session, rec)] == [
        "2026-12", "2027-12", "2028-12", "2029-12"]


def test_intervalo_trimestral_alinha_ao_inicio(session):
    rec = _rec(session, nova_conta(session), inicio="2026-07", intervalo_meses=3)
    gerar_pendentes(session, HOJE, 36)
    comps = [l.competencia_recorrencia for l in _gerados(session, rec)]
    assert comps[:3] == ["2026-10", "2027-01", "2027-04"] and comps[-1] == "2029-10"


def test_intervalo_respeita_fim(session):
    rec = _rec(session, nova_conta(session), inicio="2026-12", intervalo_meses=12, fim="2027-06")
    gerar_pendentes(session, date(2027, 1, 5), 36)
    assert _gerados(session, rec) == []


def test_editar_recorrencia_aceita_intervalo(session):
    rec = _rec(session, nova_conta(session))
    gerar_pendentes(session, HOJE, 36)
    editar_recorrencia(session, rec, a_partir="2026-10", hoje=HOJE, horizonte=36, intervalo_meses=6)
    comps = [l.competencia_recorrencia for l in _gerados(session, rec)]
    assert comps[:3] == ["2026-10", "2027-04", "2027-10"]


def test_padrao_real_gera_36_meses_a_frente(session, monkeypatch):
    from app.services import recorrencia
    monkeypatch.undo()  # volta ao HORIZONTE_PADRAO de produção
    assert recorrencia.HORIZONTE_PADRAO == 36
    rec = _rec(session, nova_conta(session))
    assert gerar_pendentes(session, HOJE) == 37
    assert _gerados(session, rec)[-1].competencia_recorrencia == "2029-10"


def test_fixa_do_formulario_leva_objetivo_para_todas_as_ocorrencias(session):
    from app.models import Meta
    from app.services.entrada_rapida import NovaEntrada, registrar_entrada
    conta = nova_conta(session)
    meta = Meta(nome="Viagem", tipo="objetivo", valor_alvo=100000)
    session.add(meta)
    session.flush()
    registrar_entrada(session, NovaEntrada(tipo="despesa", valor=5000, descricao="Aporte", data=date(2026, 10, 8),
                                           conta_id=conta.id, repetir="fixa", meta_id=meta.id), HOJE)
    rec = session.scalar(select(Recorrencia))
    assert rec.meta_id == meta.id
    assert {l.meta_id for l in _gerados(session, rec)} == {meta.id}
    assert len(_gerados(session, rec)) > 1


def test_reparar_vinculos_de_recorrencia_antiga(session):
    from app.models import Meta
    from app.services.recorrencia import reparar_vinculos
    conta = nova_conta(session)
    meta = Meta(nome="Viagem", tipo="objetivo", valor_alvo=100000)
    session.add(meta)
    session.flush()
    rec = _rec(session, conta)
    gerar_pendentes(session, HOJE)
    primeiro = _gerados(session, rec)[0]
    primeiro.meta_id = meta.id  # como o formulário antigo deixava: só a primeira
    reparar_vinculos(session)
    assert rec.meta_id == meta.id
    assert {l.meta_id for l in _gerados(session, rec)} == {meta.id}


def test_semanal_toda_quinta_gera_ate_o_horizonte(session):
    from app.services.recorrencia import ocorrencias  # noqa: F401
    rec = _rec(session, nova_conta(session), frequencia="semanal", data_inicio=date(2026, 10, 8),
               dias_semana="3", inicio="2026-10", dia=8)
    gerar_pendentes(session, HOJE, 1)  # até o fim de novembro
    datas = [l.data for l in _gerados(session, rec)]
    assert datas[0] == date(2026, 10, 8) and datas[-1] == date(2026, 11, 26)
    assert all(d.weekday() == 3 for d in datas) and len(datas) == 8
    assert gerar_pendentes(session, HOJE, 1) == 0


def test_semanal_a_cada_2_semanas_em_dois_dias(session):
    rec = _rec(session, nova_conta(session), frequencia="semanal", data_inicio=date(2026, 10, 5),
               dias_semana="0,2", intervalo_semanas=2, inicio="2026-10", dia=5)
    gerar_pendentes(session, HOJE, 0)
    assert [l.data for l in _gerados(session, rec)] == [
        date(2026, 10, 5), date(2026, 10, 7), date(2026, 10, 19), date(2026, 10, 21)]


def test_semanal_nao_gera_antes_do_inicio_no_mesmo_dia_da_semana(session):
    rec = _rec(session, nova_conta(session), frequencia="semanal", data_inicio=date(2026, 10, 7),
               dias_semana="0,2", inicio="2026-10", dia=7)
    gerar_pendentes(session, HOJE, 0)
    assert _gerados(session, rec)[0].data == date(2026, 10, 7)  # a segunda 5/10 é anterior ao início


def test_termina_em_data(session):
    rec = _rec(session, nova_conta(session), fim_data=date(2026, 11, 15))
    gerar_pendentes(session, HOJE, 36)
    assert [l.competencia_recorrencia for l in _gerados(session, rec)] == ["2026-10", "2026-11"]


def test_termina_apos_n_ocorrencias(session):
    rec = _rec(session, nova_conta(session), max_ocorrencias=4)
    gerar_pendentes(session, HOJE, 36)
    assert len(_gerados(session, rec)) == 4


def test_n_ocorrencias_conta_desde_o_inicio_mesmo_as_passadas(session):
    rec = _rec(session, nova_conta(session), inicio="2026-08", max_ocorrencias=4)  # ago, set, out, nov
    gerar_pendentes(session, HOJE, 36)
    assert [l.competencia_recorrencia for l in _gerados(session, rec)] == ["2026-10", "2026-11"]


def test_encerrar_antes_de_semanal(session):
    from app.services.recorrencia import encerrar_antes_de
    rec = _rec(session, nova_conta(session), frequencia="semanal", data_inicio=date(2026, 10, 8),
               dias_semana="3", inicio="2026-10", dia=8)
    encerrar_antes_de(rec, "2026-10-22")
    assert rec.fim_data == date(2026, 10, 21) and rec.ativa
    gerar_pendentes(session, HOJE, 2)
    assert [l.data for l in _gerados(session, rec)] == [date(2026, 10, 8), date(2026, 10, 15)]
    encerrar_antes_de(rec, "2026-10-08")
    assert not rec.ativa
