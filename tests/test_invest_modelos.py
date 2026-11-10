from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.quantidade import fmt_qtd, q
from app.models import Ativo, CotacaoCache, MetaClasse, OperacaoInvest, SerieIndicador


def test_q_aceita_ponto_e_virgula_e_rejeita_float():
    assert q("1.5") == Decimal("1.5")
    assert q("1.234,56") == Decimal("1234.56")
    with pytest.raises(TypeError):
        q(1.5)
    with pytest.raises(ValueError):
        q("abc")


@pytest.mark.parametrize("valor,esperado", [
    ("0", "0"), ("10", "10"), ("1234.5", "1.234,5"), ("0.12345678", "0,12345678"),
    ("0.123456789", "0,12345679"), ("100.00", "100"), ("1000000", "1.000.000"), ("-2.50", "-2,5"),
])
def test_fmt_qtd(valor, esperado):
    assert fmt_qtd(Decimal(valor)) == esperado


def test_decimal_texto_ida_e_volta_e_texto_canonico(session):
    a = Ativo(classe="cripto", codigo="BTC", meta_pct=Decimal("12.50"))
    session.add(a)
    session.flush()
    session.add(OperacaoInvest(ativo_id=a.id, tipo="compra", data=date(2026, 1, 2),
                               quantidade=Decimal("0.00012345"), preco_unitario=100, cambio=Decimal("5.4321")))
    session.commit()
    session.expire_all()
    op = session.query(OperacaoInvest).one()
    assert op.quantidade == Decimal("0.00012345") and isinstance(op.quantidade, Decimal)
    assert op.cambio == Decimal("5.4321")
    assert session.get(Ativo, a.id).meta_pct == Decimal("12.5")
    bruto = session.execute(text("select quantidade, cambio from operacao_invest")).one()
    assert tuple(bruto) == ("0.00012345", "5.4321")
    assert session.execute(text("select meta_pct from ativo")).scalar() == "12.5"


def test_decimal_texto_rejeita_float(session):
    session.add(Ativo(classe="acao", codigo="X", meta_pct=1.5))
    with pytest.raises(Exception):
        session.flush()


def test_decimal_texto_opcional_none(session):
    a = Ativo(classe="renda_fixa", codigo="CDB")
    session.add(a)
    session.commit()
    assert session.get(Ativo, a.id).taxa is None


def test_ativo_unico_por_classe_e_codigo(session):
    session.add(Ativo(classe="acao", codigo="PETR4"))
    session.add(Ativo(classe="fii", codigo="PETR4"))  # classe diferente: ok
    session.commit()
    session.add(Ativo(classe="acao", codigo="PETR4"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_cotacao_cache_unica_por_simbolo_e_data(session):
    agora = datetime(2026, 10, 5, 10, 0)
    session.add(CotacaoCache(simbolo="PETR4.SA", data=date(2026, 10, 5), fechamento=Decimal("38.12"), atualizado_em=agora))
    session.commit()
    session.add(CotacaoCache(simbolo="PETR4.SA", data=date(2026, 10, 6), fechamento=Decimal("38.5"), atualizado_em=agora))
    session.commit()
    session.add(CotacaoCache(simbolo="PETR4.SA", data=date(2026, 10, 5), fechamento=Decimal("1"), atualizado_em=agora))
    with pytest.raises(IntegrityError):
        session.commit()


def test_serie_indicador_unica_por_serie_e_data(session):
    session.add(SerieIndicador(serie="cdi", data=date(2026, 10, 5), valor_pct=Decimal("0.040168")))
    session.add(SerieIndicador(serie="ipca", data=date(2026, 10, 5), valor_pct=Decimal("0.44")))
    session.commit()
    session.add(SerieIndicador(serie="cdi", data=date(2026, 10, 5), valor_pct=Decimal("0.04")))
    with pytest.raises(IntegrityError):
        session.commit()


def test_meta_classe_pk(session):
    session.add(MetaClasse(classe="acao", meta_pct=Decimal("40")))
    session.commit()
    assert session.get(MetaClasse, "acao").meta_pct == Decimal("40")


def test_migrar_cria_tabelas_novas_em_banco_existente():
    from app.db import Base, criar_engine, migrar
    eng = criar_engine("sqlite://")
    Base.metadata.create_all(eng)
    migrar(eng)  # idempotente com as tabelas novas
    with eng.connect() as con:
        assert con.execute(text("select count(*) from ativo")).scalar() == 0
