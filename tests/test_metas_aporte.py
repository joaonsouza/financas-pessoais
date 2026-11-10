"""Aporte mensal das metas: fixo ou automático (o que falta ÷ meses até o prazo), anual e "como despesa"."""
from datetime import date

import pytest
from sqlalchemy import select

from app.models import Lancamento, Meta, MovimentoCaixinha, Recorrencia
from app.services.metas import (
    aplicar_pague_primeiro, categoria_objetivos, aporte_mensal, caixinhas_da_meta, plano_aporte, recalcular_metas_automaticas,
    vincular_aportes_recorrentes,
)
from app.services.lancamentos import alternar_efetivado
from tests.factories import nova_categoria, nova_conta

HOJE = date(2026, 10, 5)


def _meta(db, **campos) -> Meta:
    itau, caixa = nova_conta(db, "Itaú", saldo=1_000_000), nova_conta(db, "Caixinha Provisões")
    meta = Meta(**({"nome": "IPTU", "tipo": "objetivo", "valor_alvo": 120000, "conta_id": caixa.id,
                    "conta_origem_id": itau.id, "dia_pagar": 10, "pagar_primeiro": True,
                    "aporte_automatico": True, "aporte_como_despesa": True, "prazo": date(2027, 2, 15)} | campos))
    db.add(meta)
    db.flush()
    return meta


def _lancs(db, rec, **filtro):
    consulta = select(Lancamento).where(Lancamento.recorrencia_id == rec.id)
    for campo, valor in filtro.items():
        consulta = consulta.where(getattr(Lancamento, campo) == valor)
    return list(db.scalars(consulta.order_by(Lancamento.data, Lancamento.id)))


def test_automatico_pelo_que_falta_ate_o_prazo(db):
    meta = _meta(db)                                            # out, nov, dez, jan, fev (dia 10 ≤ 15)
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    plano = plano_aporte(db, meta, HOJE)
    assert (plano.meses, plano.mensal, plano.ultima, rec.valor, rec.fim) == (5, 24000, "2027-02", 24000, "2027-02")
    caixinha = caixinhas_da_meta(db, meta)[0]
    db.add(MovimentoCaixinha(caixinha_id=caixinha.id, data=HOJE, valor=60000, descricao="já tinha"))
    db.flush()
    assert recalcular_metas_automaticas(db, HOJE) == 1
    assert db.get(Recorrencia, rec.id).valor == 12000
    assert recalcular_metas_automaticas(db, HOJE) == 0         # nada mudou: não mexe
    db.add(MovimentoCaixinha(caixinha_id=caixinha.id, data=HOJE, valor=60000, descricao="resto"))
    db.flush()
    recalcular_metas_automaticas(db, HOJE)
    assert plano_aporte(db, meta, HOJE).completo and not _lancs(db, rec, status="pendente")
    assert recalcular_metas_automaticas(db, HOJE) == 0         # meta completa fica parada


def test_prazo_que_passou_vai_para_o_ano_seguinte(db):
    meta = _meta(db, prazo=date(2026, 3, 10), repete_anual=True)
    aplicar_pague_primeiro(db, meta, HOJE)
    assert meta.prazo == date(2027, 3, 10) and plano_aporte(db, meta, HOJE).meses == 6


def test_aporte_do_mes_cancelado_divide_pelos_meses_restantes(db):
    meta = _meta(db)                                            # out a fev = 5 aportes
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    for lanc in _lancs(db, rec, competencia_recorrencia="2026-10"):
        lanc.status = "cancelado"
    db.flush()
    assert plano_aporte(db, meta, HOJE).meses == 4             # outubro não cai mais
    recalcular_metas_automaticas(db, HOJE)
    assert db.get(Recorrencia, rec.id).valor == 30000


def test_fixo_usa_o_valor_mensal(db):
    meta = _meta(db, aporte_automatico=False, prazo=None, valor_mensal_pagar=8334)
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    assert (rec.valor, rec.dia, rec.origem_meta_id, rec.tipo, rec.fim) == (8334, 10, meta.id, "despesa", None)
    assert len(_lancs(db, rec, status="pendente")) == 3


def test_como_despesa_gera_despesa_pendente_da_categoria_sem_meta_id(db):
    cat = nova_categoria(db, "Impostos")
    meta = _meta(db, categoria_id=cat.id)
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    caixinha = caixinhas_da_meta(db, meta)[0]
    assert rec.aporte_como_despesa and rec.caixinha_id == caixinha.id and rec.conta_destino_id == meta.conta_id
    lancs = _lancs(db, rec)
    assert lancs and {(l.tipo, l.conta_id, l.categoria_id, l.status, l.meta_id) for l in lancs} == {
        ("despesa", meta.conta_origem_id, cat.id, "pendente", None)}


def test_sem_como_despesa_e_transferencia_ligada_a_meta(db):
    meta = _meta(db, aporte_como_despesa=False)
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    assert rec.tipo == "transferencia" and not rec.aporte_como_despesa
    assert {l.meta_id for l in _lancs(db, rec)} == {meta.id}


def test_validacoes(db):
    meta = _meta(db, prazo=None)
    with pytest.raises(ValueError, match="informe o valor e o prazo"):
        aplicar_pague_primeiro(db, meta, HOJE)
    meta.prazo, meta.conta_id = date(2027, 2, 15), None
    with pytest.raises(ValueError, match="escolha a conta onde o dinheiro fica"):
        aplicar_pague_primeiro(db, meta, HOJE)
    meta.conta_id = meta.conta_origem_id
    with pytest.raises(ValueError, match="diferente"):
        aplicar_pague_primeiro(db, meta, HOJE)


def test_aporte_mensal(db):
    meta = _meta(db)
    assert aporte_mensal(db, meta, HOJE) == 24000
    meta.aporte_automatico, meta.valor_mensal_pagar = False, 5000
    assert aporte_mensal(db, meta, HOJE) == 5000
    meta.pagar_primeiro = False
    assert aporte_mensal(db, meta, HOJE) is None


def test_vincular_nao_liga_aporte_como_despesa(db):
    meta = _meta(db)
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    assert vincular_aportes_recorrentes(db) == 0
    assert {l.meta_id for l in _lancs(db, rec)} == {None}


def test_prazo_vencido_sem_repetir_nao_reaplica_a_cada_abertura(db):
    meta = _meta(db, prazo=date(2026, 3, 10))
    assert aplicar_pague_primeiro(db, meta, HOJE) is None
    recalcular_metas_automaticas(db, HOJE)
    assert recalcular_metas_automaticas(db, HOJE) == 0


def test_sem_conta_e_despesa_em_objetivos_ligada_a_meta(db):
    meta = _meta(db, conta_id=None, aporte_como_despesa=False, aporte_automatico=False, prazo=None,
                 valor_mensal_pagar=5000)
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    assert rec.tipo == "despesa" and rec.categoria_id == categoria_objetivos(db).id
    assert {l.meta_id for l in _lancs(db, rec)} == {meta.id}


def test_prazo_antes_do_dia_do_aporte_termina_no_mes_anterior(db):
    meta = _meta(db, prazo=date(2027, 2, 5))                    # dia 10 > 5: fevereiro não cai
    assert plano_aporte(db, meta, HOJE).ultima == "2027-01"


def _aportes_do_mes(db, meta, competencia):
    """Saídas da conta de origem, de qualquer recorrência da meta, no mês."""
    return list(db.scalars(select(Lancamento).where(
        Lancamento.recorrencia_id.in_(select(Recorrencia.id).where(Recorrencia.origem_meta_id == meta.id)),
        Lancamento.competencia_recorrencia == competencia, Lancamento.conta_id == meta.conta_origem_id,
        Lancamento.status != "cancelado")))


def test_trocar_como_despesa_depois_do_aporte_do_mes_nao_repete_o_mes(db):
    meta = _meta(db, aporte_como_despesa=False)
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    (outubro,) = _aportes_do_mes(db, meta, "2026-10")
    alternar_efetivado(db, outubro)                             # o aporte de outubro caiu
    for como_despesa in (True, False):                          # liga e desliga "conta como despesa"
        meta.aporte_como_despesa = como_despesa
        nova = aplicar_pague_primeiro(db, meta, HOJE)
        assert nova.id != rec.id and nova.inicio == "2026-11"
        assert len(_aportes_do_mes(db, meta, "2026-10")) == 1
        assert nova.valor == 24000                              # 96.000 que faltam ÷ nov a fev
        assert recalcular_metas_automaticas(db, HOJE) == 0


def test_trocar_como_despesa_antes_do_aporte_do_mes_comeca_neste_mes(db):
    meta = _meta(db, aporte_como_despesa=False)
    aplicar_pague_primeiro(db, meta, HOJE)
    meta.aporte_como_despesa = True
    nova = aplicar_pague_primeiro(db, meta, HOJE)
    assert nova.inicio == "2026-10" and len(_aportes_do_mes(db, meta, "2026-10")) == 1


def test_meta_completa_com_aporte_desde_mes_anterior_nao_reencerra_a_cada_abertura(db):
    meta = _meta(db, conta_id=None, aporte_como_despesa=False)  # fora do app: o já guardado conta
    rec = aplicar_pague_primeiro(db, meta, date(2026, 9, 5))    # começou em setembro
    meta.valor_manual = 120000                                  # já guardou tudo
    assert recalcular_metas_automaticas(db, HOJE) == 1          # encerra a partir de outubro
    assert rec.fim == "2026-09"
    assert recalcular_metas_automaticas(db, HOJE) == 0
    assert rec.fim == "2026-09"
