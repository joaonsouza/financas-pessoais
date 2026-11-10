import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    AjusteMes, Meta, Orcamento, Provisao, alertas_dispensados, dispensar_alerta, get_metas_503020,
    set_metas_503020,
)
from tests.factories import nova_categoria, nova_conta


def test_orcamento_unico_por_mes_e_categoria(db):
    cat = nova_categoria(db, "Mercado")
    db.add(Orcamento(competencia="2026-10", categoria_id=cat.id, limite=100000))
    db.flush()
    db.add(Orcamento(competencia="2026-10", categoria_id=cat.id, limite=5))
    with pytest.raises(IntegrityError):
        db.flush()


def test_teto_unico_por_mes(db):
    db.add(Orcamento(competencia="2026-10", categoria_id=None, limite=300000))
    db.flush()
    db.add(Orcamento(competencia="2026-10", categoria_id=None, limite=1))
    with pytest.raises(IntegrityError):
        db.flush()


def test_ajuste_mes_unico_por_competencia(db):
    db.add(AjusteMes(competencia="2026-10", descricao="Cortar delivery", valor_liberado=20000, destino="Reserva"))
    db.flush()
    db.add(AjusteMes(competencia="2026-10", descricao="Outro"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_meta_e_provisao_persistem_com_padroes(db):
    conta = nova_conta(db, "Reserva")
    origem = nova_conta(db, "Corrente")
    meta = Meta(nome="Reserva de emergência", tipo="reserva_emergencia", conta_id=conta.id)
    prov = Provisao(nome="IPTU", valor_anual=120000, conta_destino_id=conta.id, conta_origem_id=origem.id)
    db.add_all([meta, prov])
    db.flush()
    assert (meta.horizonte, meta.multiplicador_reserva, meta.pagar_primeiro, meta.ativa) == ("ano", 3, False, True)
    assert prov.dia == 5


def test_config_metas_503020_padrao_e_validacao(db):
    assert get_metas_503020(db) == [50, 30, 20]
    set_metas_503020(db, [60, 20, 20])
    assert get_metas_503020(db) == [60, 20, 20]
    with pytest.raises(ValueError):
        set_metas_503020(db, [50, 30, 30])


def test_alertas_dispensados_por_competencia(db):
    assert alertas_dispensados(db, "2026-10") == []
    dispensar_alerta(db, "2026-10", "sobra_negativa")
    dispensar_alerta(db, "2026-10", "sobra_negativa")
    assert alertas_dispensados(db, "2026-10") == ["sobra_negativa"]
    assert alertas_dispensados(db, "2026-11") == []


def test_meta_tem_campos_de_aporte_com_padroes(db):
    meta = Meta(nome="IPTU", tipo="objetivo")
    db.add(meta)
    db.flush()
    assert (meta.repete_anual, meta.aporte_automatico, meta.aporte_como_despesa, meta.categoria_id) == (
        False, False, False, None)


def test_recorrencia_aporte_como_despesa():
    from app.models import Recorrencia
    base = dict(descricao="x", conta_id=1, valor=100, dia=5, inicio="2026-10")
    assert Recorrencia(**base, tipo="despesa", origem_meta_id=1, conta_destino_id=2).aporte_como_despesa
    assert not Recorrencia(**base, tipo="despesa", origem_meta_id=1).aporte_como_despesa          # meta fora do app
    assert not Recorrencia(**base, tipo="transferencia", origem_meta_id=1, conta_destino_id=2).aporte_como_despesa
    assert not Recorrencia(**base, tipo="despesa", conta_destino_id=2).aporte_como_despesa        # não é de meta
