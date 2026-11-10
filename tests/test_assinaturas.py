from datetime import date

import pytest

from app.models import Assinatura
from app.services.assinaturas import (
    assinatura_da_sugestao, cancelar_assinatura, cobrancas, criar_assinatura, editar_assinatura,
    ignorar_sugestao, painel_assinaturas, reativar_assinatura, total_assinaturas,
)
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta, novo_cartao, novo_lancamento


@pytest.fixture
def cartao(session):
    return novo_cartao(session, pagadora=nova_conta(session))


def _ass(session, cartao, nome="Netflix", valor=4590, **kw):
    return criar_assinatura(session, nome=nome, valor=valor, conta_id=cartao.id, hoje=HOJE_TESTE, **kw)


def _cobranca(session, cartao, dia, valor=-4590, desc="Netflix", **kw):
    return novo_lancamento(session, cartao, dia, valor, descricao=desc, **kw)


# ---- comandos

def test_criar_normaliza_o_padrao_e_define_inicio(session, cartao):
    a = _ass(session, cartao, "Ifd*Ifood Club")
    assert a.padrao == "IFD*IFOOD CLUB" and a.ativa and a.inicio == HOJE_TESTE and a.intervalo_meses == 1


@pytest.mark.parametrize("campos, trecho", [
    ({"nome": " "}, "nome"),
    ({"valor": 0}, "valor"),
    ({"intervalo_meses": 13}, "periodicidade"),
    ({"dia": 32}, "dia"),
    ({"conta_id": 9999}, "cartão"),
])
def test_criar_valida(session, cartao, campos, trecho):
    base = {"nome": "Netflix", "valor": 4590, "conta_id": cartao.id}
    with pytest.raises(ValueError, match=trecho):
        criar_assinatura(session, hoje=HOJE_TESTE, **{**base, **campos})


def test_criar_rejeita_padrao_repetido_na_mesma_conta(session, cartao):
    _ass(session, cartao)
    with pytest.raises(ValueError, match="já existe"):
        _ass(session, cartao, "netflix")


def test_editar_cancelar_e_reativar(session, cartao):
    a = _ass(session, cartao)
    editar_assinatura(session, a, valor=5990, nome="Netflix Premium")
    assert (a.valor, a.nome) == (5990, "Netflix Premium")
    cancelar_assinatura(a, date(2026, 9, 1))
    assert (a.ativa, a.cancelada_em) == (False, date(2026, 9, 1))
    reativar_assinatura(a)
    assert (a.ativa, a.cancelada_em) == (True, None)
    with pytest.raises(ValueError, match="valor"):
        editar_assinatura(session, a, valor=-1)


# ---- casamento

def test_cobranca_casa_por_prefixo_na_conta_certa(session, cartao):
    a = _ass(session, cartao, "Spotify")
    ok = _cobranca(session, cartao, date(2026, 9, 10), desc="Spotify Brasil")
    _cobranca(session, cartao, date(2026, 9, 10), desc="Loja do Spotify")          # não começa igual
    _cobranca(session, cartao, date(2026, 9, 11), desc="Spotify", status="pendente")  # projeção
    _cobranca(session, cartao, date(2026, 9, 12), valor=4590, desc="Spotify")       # estorno
    outro = novo_cartao(session, "Outro", pagadora=nova_conta(session, "Pagadora 2"))
    _cobranca(session, outro, date(2026, 9, 13), desc="Spotify")                    # outra conta
    assert [l.id for l in cobrancas(session)[a.id]] == [ok.id]


def test_padrao_mais_longo_vence(session, cartao):
    google = _ass(session, cartao, "Google")
    one = _ass(session, cartao, "Google One")
    l = _cobranca(session, cartao, date(2026, 9, 7), desc="Google One")
    por = cobrancas(session)
    assert [x.id for x in por[one.id]] == [l.id] and por[google.id] == []


# ---- painel

def test_totais_e_gasto_12_meses(session, cartao):
    mensal = _ass(session, cartao, "Netflix", 4590)
    _ass(session, cartao, "iCloud Anual", 12000, intervalo_meses=12)
    for dia in (date(2026, 8, 10), date(2026, 9, 10), date(2026, 10, 2)):
        _cobranca(session, cartao, dia)
    _cobranca(session, cartao, date(2025, 9, 1), desc="Netflix")  # mais de 365 dias: fora do gasto 12m
    painel = painel_assinaturas(session, HOJE_TESTE)
    assert (painel.qtd_ativas, painel.qtd_canceladas) == (2, 0)
    assert painel.custo_anual == 4590 * 12 + 12000 and painel.custo_mensal == painel.custo_anual // 12
    item = next(i for i in painel.ativas if i.assinatura.id == mensal.id)
    assert (item.ultima_data, item.ultimo_valor) == (date(2026, 10, 2), 4590)
    assert (item.cobrado_12m, item.meses_cobrados) == (3 * 4590, 3)
    assert painel.gasto_12m == 3 * 4590
    assert total_assinaturas(session) == (painel.custo_mensal, painel.custo_anual)


def test_alerta_de_reajuste(session, cartao):
    a = _ass(session, cartao, valor=4590)
    _cobranca(session, cartao, date(2026, 10, 2), valor=-4990)
    assert painel_assinaturas(session, HOJE_TESTE).ativas[0].reajuste is True
    editar_assinatura(session, a, valor=4990)
    assert painel_assinaturas(session, HOJE_TESTE).ativas[0].reajuste is False


def test_alerta_sem_cobranca(session, cartao):
    velha = _ass(session, cartao, "Velha", inicio=date(2026, 1, 1))
    nova = _ass(session, cartao, "Nova")  # início = hoje: ainda não deu tempo de cobrar
    ativa = _ass(session, cartao, "Ativa", inicio=date(2026, 1, 1))
    _cobranca(session, cartao, date(2026, 9, 20), desc="Ativa")
    anual = _ass(session, cartao, "Anual", 12000, intervalo_meses=12, inicio=date(2025, 1, 1))
    _cobranca(session, cartao, date(2025, 11, 20), valor=-12000, desc="Anual")  # 11 meses atrás: dentro de 13
    por_nome = {i.assinatura.nome: i for i in painel_assinaturas(session, HOJE_TESTE).ativas}
    assert por_nome["Velha"].sem_cobranca is True
    assert por_nome["Nova"].sem_cobranca is False
    assert por_nome["Ativa"].sem_cobranca is False
    assert por_nome["Anual"].sem_cobranca is False


def test_cancelada_ainda_cobrando(session, cartao):
    a = _ass(session, cartao)
    cancelar_assinatura(a, date(2026, 9, 1))
    _cobranca(session, cartao, date(2026, 8, 10))
    assert painel_assinaturas(session, HOJE_TESTE).canceladas[0].ainda_cobrando is False
    _cobranca(session, cartao, date(2026, 10, 2))
    painel = painel_assinaturas(session, HOJE_TESTE)
    assert painel.canceladas[0].ainda_cobrando is True and (painel.qtd_ativas, painel.qtd_canceladas) == (0, 1)


# ---- sugestões

def _sequencia(session, conta, desc, valor=-2990):
    for dia in (date(2026, 8, 10), date(2026, 9, 10), date(2026, 10, 2)):
        novo_lancamento(session, conta, dia, valor, descricao=desc)


def test_sugestoes_filtradas(session, cartao):
    corrente = nova_conta(session, "Corrente X")
    _sequencia(session, cartao, "Disney Plus")
    _sequencia(session, cartao, "Hbo Max")
    _sequencia(session, cartao, "Netflix")
    _sequencia(session, cartao, "Pix no Crédito - Fulano")
    _sequencia(session, cartao, "Multa de atraso")
    _sequencia(session, corrente, "Academia")
    _ass(session, cartao, "Hbo Max")
    ignorar_sugestao(session, "Netflix", cartao.id)
    sugestoes = painel_assinaturas(session, HOJE_TESTE).sugestoes
    assert [s.descricao for s in sugestoes] == ["Disney Plus"]


def test_assinatura_da_sugestao(session, cartao):
    _sequencia(session, cartao, "Disney Plus")
    [sug] = painel_assinaturas(session, HOJE_TESTE).sugestoes
    a = assinatura_da_sugestao(session, sug, HOJE_TESTE)
    assert (a.nome, a.valor, a.conta_id, a.dia, a.inicio) == ("Disney Plus", 2990, cartao.id, 2, date(2026, 8, 1))
    assert painel_assinaturas(session, HOJE_TESTE).sugestoes == []


def test_editar_aceita_observacao_junto_com_outros_campos(session, cartao):
    a = _ass(session, cartao)
    editar_assinatura(session, a, valor=5000, observacao="plano família", padrao="Netflix Brasil")
    assert (a.valor, a.observacao, a.padrao) == (5000, "plano família", "NETFLIX BRASIL")
    editar_assinatura(session, a, observacao=None)
    assert a.observacao is None and a.valor == 5000
