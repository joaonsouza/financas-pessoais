from datetime import date

from sqlalchemy import func, select

from app.models import Assinatura, Lancamento, Recorrencia
from app.services.migrar_assinaturas import aplicar, planejar
from app.services.recorrencia import gerar_pendentes
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta, novo_cartao


def _cenario(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session))
    ass = Recorrencia(descricao="Spotify", tipo="despesa", conta_id=cartao.id, valor=4090, dia=15,
                      inicio="2026-10", eh_assinatura=True)
    comum = Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=cartao.id, valor=120000, dia=5,
                        inicio="2026-10")
    receita = Recorrencia(descricao="Salário", tipo="receita", conta_id=nova_conta(session, "Corrente").id,
                          valor=500000, dia=5, inicio="2026-10", eh_assinatura=True)
    session.add_all([ass, comum, receita])
    session.flush()
    gerar_pendentes(session, HOJE_TESTE)
    return cartao, ass, comum, receita


def _pendentes(session, rec):
    return session.scalar(select(func.count(Lancamento.id)).where(Lancamento.recorrencia_id == rec.id))


def test_planejar_so_le(session):
    _, ass, comum, receita = _cenario(session)
    antes = (_pendentes(session, ass), _pendentes(session, comum))
    [plano] = planejar(session, HOJE_TESTE)
    assert plano.recorrencia.id == ass.id and plano.qtd_pendentes == antes[0] > 0
    assert session.scalars(select(Assinatura)).all() == []
    assert (_pendentes(session, ass), _pendentes(session, comum)) == antes


def test_aplicar_cria_assinatura_encerra_e_apaga_so_os_pendentes_dela(session):
    cartao, ass, comum, receita = _cenario(session)
    pend_comum, pend_receita = _pendentes(session, comum), _pendentes(session, receita)
    aplicar(session, HOJE_TESTE)
    [a] = session.scalars(select(Assinatura)).all()
    assert (a.nome, a.valor, a.conta_id, a.dia, a.ativa, a.inicio) == ("Spotify", 4090, cartao.id, 15, True, date(2026, 10, 1))
    assert _pendentes(session, ass) == 0
    assert session.get(Recorrencia, ass.id).ativa is False
    assert _pendentes(session, comum) == pend_comum and session.get(Recorrencia, comum.id).ativa is True
    assert _pendentes(session, receita) == pend_receita  # receita não é assinatura de cartão


def test_aplicar_duas_vezes_nao_duplica(session):
    _cenario(session)
    aplicar(session, HOJE_TESTE)
    assert planejar(session, HOJE_TESTE) == []
    aplicar(session, HOJE_TESTE)
    assert len(session.scalars(select(Assinatura)).all()) == 1


def test_assinatura_ja_cadastrada_e_reaproveitada(session):
    from app.services.assinaturas import criar_assinatura
    cartao, ass, _, _ = _cenario(session)
    criar_assinatura(session, nome="Spotify", valor=4090, conta_id=cartao.id, hoje=HOJE_TESTE)
    [plano] = planejar(session, HOJE_TESTE)
    assert plano.assinatura_existente is not None
    aplicar(session, HOJE_TESTE)
    assert len(session.scalars(select(Assinatura)).all()) == 1 and _pendentes(session, ass) == 0
