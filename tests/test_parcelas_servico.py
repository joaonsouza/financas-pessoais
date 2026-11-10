from datetime import date

import pytest

from app.models.config import get_config
from app.services.faturas import obter_ou_criar_fatura
from app.services.parcelas import (
    TETO_PADRAO, definir_teto_parcelas, parcelas_do_mes, teto_parcelas, visao_parcelas,
)
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta, novo_cartao, novo_lancamento


def _parcela(s, cartao, comp, valor, *, grupo, num, total, desc="Loja", status="pendente",
             data=None, ignorar=False):
    fatura = obter_ou_criar_fatura(s, cartao, comp)
    lanc = novo_lancamento(s, cartao, data or fatura.data_vencimento, valor, status=status, descricao=desc,
                           fatura_id=fatura.id, ignorar=ignorar)
    lanc.grupo_parcela_id, lanc.parcela_num, lanc.parcela_total = grupo, num, total
    s.flush()
    return lanc


def cartao_pagadora(session):
    return nova_conta(session, "Pagadora 2")


@pytest.fixture
def cartao(session):
    return novo_cartao(session, pagadora=nova_conta(session))


def _cenario(session, cartao):
    for num, comp in ((2, "2026-10"), (3, "2026-11"), (4, "2026-12")):
        _parcela(session, cartao, comp, -10000, grupo="g1", num=num, total=4, desc="Tv")
    for num, comp in ((1, "2026-11"), (2, "2026-12")):
        _parcela(session, cartao, comp, -5000, grupo="g2", num=num, total=2, desc="Sofá")


def test_teto_padrao_e_gravacao(session):
    assert teto_parcelas(session) == TETO_PADRAO == 200000
    definir_teto_parcelas(session, 350000)
    assert teto_parcelas(session) == 350000
    assert get_config(session, "teto_parcelas") == 350000


def test_teto_negativo_e_rejeitado(session):
    with pytest.raises(ValueError):
        definir_teto_parcelas(session, -1)
    assert teto_parcelas(session) == TETO_PADRAO


def test_totais_e_meses(session, cartao):
    _cenario(session, cartao)
    v = visao_parcelas(session, HOJE_TESTE)
    assert v.total_a_pagar == 40000 and v.qtd_compras == 2
    # janela começa 2 meses antes do mês atual: ago e set entram zerados
    assert [(m.competencia, m.total, m.qtd) for m in v.meses] == [
        ("2026-08", 0, 0), ("2026-09", 0, 0), ("2026-10", 10000, 1), ("2026-11", 15000, 2), ("2026-12", 15000, 2)]
    assert v.meses_acima == 0 and v.teto == 200000


def test_meses_acima_do_teto_e_pico(session, cartao):
    _cenario(session, cartao)
    definir_teto_parcelas(session, 12000)
    v = visao_parcelas(session, HOJE_TESTE)
    assert [m.acima for m in v.meses] == [False, False, False, True, True]
    assert v.meses_acima == 2 and v.pico.competencia == "2026-11"


def test_teto_zero_nao_marca_nada(session, cartao):
    _cenario(session, cartao)
    definir_teto_parcelas(session, 0)
    v = visao_parcelas(session, HOJE_TESTE)
    assert v.meses_acima == 0 and not any(m.acima for m in v.meses)


def test_mes_sem_parcela_entra_com_zero(session, cartao):
    _parcela(session, cartao, "2026-10", -1000, grupo="g", num=1, total=2)
    _parcela(session, cartao, "2026-12", -1000, grupo="g", num=2, total=2)
    v = visao_parcelas(session, HOJE_TESTE)
    assert [(m.competencia, m.total) for m in v.meses][2:] == [("2026-10", 1000), ("2026-11", 0), ("2026-12", 1000)]


def test_compra_agrupada(session, cartao):
    _cenario(session, cartao)
    tv = next(c for c in visao_parcelas(session, HOJE_TESTE).compras if c.descricao == "Tv")
    assert (tv.proxima_num, tv.total_parcelas, tv.valor_parcela) == (2, 4, 10000)
    assert (tv.restantes, tv.falta, tv.ultimo_mes) == (3, 30000, "2026-12")
    assert tv.cartao == cartao.nome and tv.cartao_id == cartao.id


def test_ignora_o_que_nao_e_parcela_futura_de_cartao_ativo(session, cartao):
    _cenario(session, cartao)
    _parcela(session, cartao, "2026-11", -999, grupo="x", num=2, total=3, status="efetivado")
    _parcela(session, cartao, "2026-11", -999, grupo="x", num=3, total=3, ignorar=True)
    _parcela(session, cartao, "2026-11", 999, grupo="y", num=1, total=2)  # crédito
    outro = novo_cartao(session, "Cartão Velho", pagadora=cartao_pagadora(session))
    _parcela(session, outro, "2026-11", -999, grupo="z", num=1, total=2)
    outro.ativa = False
    session.flush()
    conta = nova_conta(session, "Conta X")
    solto = novo_lancamento(session, conta, date(2026, 11, 5), -999, status="pendente")
    solto.grupo_parcela_id, solto.parcela_num, solto.parcela_total = "w", 1, 2
    session.flush()
    assert visao_parcelas(session, HOJE_TESTE).total_a_pagar == 40000


def test_sobras_ficam_fora_dos_totais(session, cartao):
    _cenario(session, cartao)
    velha = _parcela(session, cartao, "2026-08", -7000, grupo="v", num=2, total=3, desc="Velha",
                     data=date(2026, 8, 10))
    v = visao_parcelas(session, HOJE_TESTE)
    assert v.total_a_pagar == 40000
    assert [(x.id, x.descricao, x.valor) for x in v.sobras] == [(velha.id, "Velha", -7000)]


def test_parcela_com_data_passada_em_fatura_aberta_ainda_e_divida_futura(session, cartao):
    # data da compra em outubro (antes de hoje), fatura de novembro: o vencimento decide "futura", não é sobra
    _parcela(session, cartao, "2026-11", -4000, grupo="a", num=2, total=3, desc="Aberta",
             data=date(2026, 10, 2))
    v = visao_parcelas(session, HOJE_TESTE)
    assert v.sobras == []
    assert v.total_a_pagar == 4000 and v.qtd_compras == 1
    assert [(m.competencia, m.total) for m in v.meses][2:] == [("2026-10", 0), ("2026-11", 4000)]


def test_filtro_por_cartao(session, cartao):
    _cenario(session, cartao)
    outro = novo_cartao(session, "Outro", pagadora=cartao_pagadora(session))
    _parcela(session, outro, "2026-11", -2000, grupo="o", num=1, total=2, desc="Notebook")
    assert visao_parcelas(session, HOJE_TESTE).total_a_pagar == 42000
    so = visao_parcelas(session, HOJE_TESTE, cartao_id=outro.id)
    assert so.total_a_pagar == 2000 and [c.descricao for c in so.compras] == ["Notebook"]


def test_parcela_sem_fatura_usa_o_mes_da_data(session, cartao):
    lanc = novo_lancamento(session, cartao, date(2026, 11, 20), -3000, status="pendente")
    lanc.grupo_parcela_id, lanc.parcela_num, lanc.parcela_total = "s", 1, 2
    session.flush()
    v = visao_parcelas(session, HOJE_TESTE)
    assert [(m.competencia, m.total) for m in v.meses][2:] == [("2026-10", 0), ("2026-11", 3000)]


def test_sem_parcelas(session, cartao):
    v = visao_parcelas(session, HOJE_TESTE)
    assert v.total_a_pagar == 0 and v.qtd_compras == 0 and v.meses == [] and v.pico is None


def test_parcelas_do_mes(session, cartao):
    _cenario(session, cartao)
    assert parcelas_do_mes(session, HOJE_TESTE, "2026-11") == (15000, 200000)
    assert parcelas_do_mes(session, HOJE_TESTE, "2027-03") == (0, 200000)


def test_mes_pela_competencia_da_fatura_nao_pelo_vencimento(session):
    # fechamento 25, vencimento 5: a fatura de 2026-10 fecha em 25/10 e vence em 05/11
    c = novo_cartao(session, "Fecha 25", pagadora=nova_conta(session), fechamento=25, vencimento=5)
    l = _parcela(session, c, "2026-10", -8000, grupo="c", num=1, total=2)
    assert obter_ou_criar_fatura(session, c, "2026-10").data_vencimento == date(2026, 11, 5)
    v = visao_parcelas(session, HOJE_TESTE)
    assert {m.competencia: m.total for m in v.meses}["2026-10"] == 8000
    assert "2026-11" not in {m.competencia for m in v.meses}  # nada pendente depois: a janela acaba em outubro
    assert v.total_a_pagar == 8000 and v.sobras == []
    assert parcelas_do_mes(session, HOJE_TESTE, "2026-10")[0] == 8000
    assert l.status == "pendente"


def test_efetivada_entra_na_curva_mas_nao_no_que_falta_pagar(session, cartao):
    _cenario(session, cartao)
    _parcela(session, cartao, "2026-10", -15000, grupo="p", num=1, total=3, desc="Paga", status="efetivado")
    v = visao_parcelas(session, HOJE_TESTE)
    out = next(m for m in v.meses if m.competencia == "2026-10")
    assert (out.total, out.qtd) == (25000, 2)
    assert v.total_a_pagar == 40000 and v.qtd_compras == 2 and v.sobras == []
    assert "Paga" not in [c.descricao for c in v.compras]


def test_cancelada_fica_fora_da_curva(session, cartao):
    _parcela(session, cartao, "2026-10", -1000, grupo="k", num=1, total=2, status="cancelado")
    assert visao_parcelas(session, HOJE_TESTE).meses == []


def test_so_efetivadas_ainda_tem_curva_sem_pendencia(session, cartao):
    _parcela(session, cartao, "2026-10", -15000, grupo="p", num=1, total=1, status="efetivado")
    v = visao_parcelas(session, HOJE_TESTE)
    assert v.total_a_pagar == 0 and v.compras == []
    assert [(m.competencia, m.total) for m in v.meses] == [("2026-08", 0), ("2026-09", 0), ("2026-10", 15000)]


def test_flag_passado_e_mes_atual_nao_e_passado(session, cartao):
    _parcela(session, cartao, "2026-09", -2000, grupo="a", num=1, total=3, status="efetivado")
    _parcela(session, cartao, "2026-11", -2000, grupo="a", num=2, total=3)
    v = visao_parcelas(session, HOJE_TESTE)
    assert [(m.competencia, m.passado) for m in v.meses] == [
        ("2026-08", True), ("2026-09", True), ("2026-10", False), ("2026-11", False)]


def test_janela_com_mes_ref_anterior_e_posterior(session, cartao):
    _parcela(session, cartao, "2026-11", -2000, grupo="a", num=1, total=1)
    antes = visao_parcelas(session, HOJE_TESTE, mes_ref="2026-06")
    assert antes.meses[0].competencia == "2026-04" and antes.meses[-1].competencia == "2026-11"
    depois = visao_parcelas(session, HOJE_TESTE, mes_ref="2026-12")
    assert depois.meses[0].competencia == "2026-08" and depois.meses[-1].competencia == "2026-12"


def test_pico_e_meses_acima_ignoram_meses_passados(session, cartao):
    _parcela(session, cartao, "2026-09", -90000, grupo="a", num=1, total=2, status="efetivado")
    _parcela(session, cartao, "2026-11", -5000, grupo="a", num=2, total=2)
    definir_teto_parcelas(session, 10000)
    v = visao_parcelas(session, HOJE_TESTE)
    assert next(m for m in v.meses if m.competencia == "2026-09").acima is True  # flag fica no mês passado
    assert v.meses_acima == 0
    assert v.pico.competencia == "2026-11" and v.pico.total == 5000


def test_parcelas_do_mes_soma_lancadas_e_pendentes_fora_da_janela(session, cartao):
    _parcela(session, cartao, "2026-10", -10000, grupo="a", num=1, total=2, status="efetivado")
    _parcela(session, cartao, "2026-10", -5000, grupo="b", num=1, total=2)
    _parcela(session, cartao, "2026-02", -3000, grupo="c", num=1, total=1, status="efetivado")
    assert parcelas_do_mes(session, HOJE_TESTE, "2026-10") == (15000, 200000)
    assert parcelas_do_mes(session, HOJE_TESTE, "2026-02") == (3000, 200000)
