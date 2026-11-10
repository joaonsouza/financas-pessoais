from datetime import date

import pytest

from app.services.assinaturas import cancelar_assinatura, criar_assinatura, ignorar_sugestao
from app.services.assinaturas_fatura import assinaturas_da_fatura
from app.services.faturas import obter_ou_criar_fatura
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta, novo_cartao, novo_lancamento


@pytest.fixture
def cartao(session):
    return novo_cartao(session, pagadora=nova_conta(session))


def _ass(session, cartao, nome="Netflix", valor=4590, **kw):
    return criar_assinatura(session, nome=nome, valor=valor, conta_id=cartao.id, hoje=date(2026, 1, 1), **kw)


def _cobranca(session, cartao, fatura, valor=-4590, desc="Netflix"):
    return novo_lancamento(session, cartao, fatura.data_vencimento, valor, descricao=desc, fatura_id=fatura.id)


def test_confirmada_quando_ha_cobranca_na_fatura(session, cartao):
    fatura = obter_ou_criar_fatura(session, cartao, "2026-10")
    _ass(session, cartao)
    _cobranca(session, cartao, fatura, -4990)
    [item] = assinaturas_da_fatura(session, cartao, fatura, HOJE_TESTE).itens
    assert (item.estado, item.valor_esperado, item.valor_real) == ("confirmada", 4590, 4990)


def test_aguardando_com_fatura_aberta(session, cartao):
    fatura = obter_ou_criar_fatura(session, cartao, "2026-11")  # fecha 03/11, depois de HOJE_TESTE
    _ass(session, cartao)
    [item] = assinaturas_da_fatura(session, cartao, fatura, HOJE_TESTE).itens
    assert (item.estado, item.valor_real) == ("aguardando", None)


def test_nao_apareceu_com_fatura_fechada(session, cartao):
    fatura = obter_ou_criar_fatura(session, cartao, "2026-10")  # fechou em 03/10
    _ass(session, cartao)
    [item] = assinaturas_da_fatura(session, cartao, fatura, HOJE_TESTE).itens
    assert item.estado == "nao_apareceu"


def test_ordem_por_estado_e_nome(session, cartao):
    fatura = obter_ou_criar_fatura(session, cartao, "2026-10")
    for nome, cobrada in (("Zeta", True), ("Alfa", True), ("Beta", False)):
        _ass(session, cartao, nome)
        if cobrada:
            _cobranca(session, cartao, fatura, desc=nome)
    itens = assinaturas_da_fatura(session, cartao, fatura, HOJE_TESTE).itens
    assert [(i.assinatura.nome, i.estado) for i in itens] == [
        ("Beta", "nao_apareceu"), ("Alfa", "confirmada"), ("Zeta", "confirmada")]


def test_fora_do_painel(session, cartao):
    fatura = obter_ou_criar_fatura(session, cartao, "2026-10")
    outro = novo_cartao(session, "Outro", pagadora=nova_conta(session, "Pagadora 2"))
    criar_assinatura(session, nome="De outro cartão", valor=1000, conta_id=outro.id, hoje=date(2026, 1, 1))
    cancelada = _ass(session, cartao, "Cancelada")
    cancelar_assinatura(cancelada, date(2026, 9, 1))
    _ass(session, cartao, "Anual", 12000, intervalo_meses=12)             # anual sem cobrança nesta fatura
    _ass(session, cartao, "Futura", inicio=date(2026, 12, 1))              # ainda não existia
    assert assinaturas_da_fatura(session, cartao, fatura, HOJE_TESTE).itens == []


def test_anual_com_cobranca_na_fatura_aparece(session, cartao):
    fatura = obter_ou_criar_fatura(session, cartao, "2026-10")
    _ass(session, cartao, "iCloud Anual", 12000, intervalo_meses=12)
    _cobranca(session, cartao, fatura, -12000, "iCloud Anual")
    [item] = assinaturas_da_fatura(session, cartao, fatura, HOJE_TESTE).itens
    assert item.estado == "confirmada"


def _sequencia(session, conta, desc):
    for dia in (date(2026, 8, 10), date(2026, 9, 10), date(2026, 10, 2)):
        novo_lancamento(session, conta, dia, -2990, descricao=desc)


def test_sugestoes_so_do_cartao_e_sem_ignoradas(session, cartao):
    fatura = obter_ou_criar_fatura(session, cartao, "2026-10")
    outro = novo_cartao(session, "Outro", pagadora=nova_conta(session, "Pagadora 2"))
    _sequencia(session, cartao, "Disney Plus")
    _sequencia(session, cartao, "Netflix Extra")
    _sequencia(session, outro, "Hbo Max")
    ignorar_sugestao(session, "Netflix Extra", cartao.id)
    sugestoes = assinaturas_da_fatura(session, cartao, fatura, HOJE_TESTE).sugestoes
    assert [(s.descricao, s.conta_id) for s in sugestoes] == [("Disney Plus", cartao.id)]
