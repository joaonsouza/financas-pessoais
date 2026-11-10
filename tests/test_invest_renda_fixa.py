from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal as D

import pytest

from app.models import Ativo, OperacaoInvest
from app.services.invest_posicao import posicao
from app.services.invest_renda_fixa import (
    valor_atualizado, valor_atualizado_detalhe, valor_na_data_para_posicao,
)


def dias_uteis_sinteticos(inicio=date(2026, 1, 2), fim=date(2026, 6, 30), cdi="0.0400"):
    """Série CDI sintética: seg-sex, valor diário constante (feriados não existem aqui)."""
    serie, d = {}, inicio
    while d <= fim:
        if d.weekday() < 5:
            serie[d] = D(cdi)
        d += timedelta(days=1)
    return serie


def contar_du(serie, ini_excl, fim_incl):
    return sum(1 for d in serie if ini_excl < d <= fim_incl)


def centavo(x: D) -> int:
    return int(x.quantize(D(1), rounding=ROUND_HALF_UP))


def aplicacao(dia, valor):
    return OperacaoInvest(tipo="aplicacao", data=dia, quantidade=D(1), preco_unitario=valor, taxas=0)


def resgate(dia, valor):
    return OperacaoInvest(tipo="resgate", data=dia, quantidade=D(1), preco_unitario=valor, taxas=0)


def titulo(indexador, taxa, vencimento=None):
    return Ativo(classe="renda_fixa", codigo="T", indexador=indexador, taxa=D(taxa), vencimento=vencimento)


CDI = dias_uteis_sinteticos()


def test_pre_fixado():
    ap, ate = date(2026, 1, 5), date(2026, 2, 27)
    du = contar_du(CDI, ap, ate)
    esperado = centavo(D(1_000_000) * D("1.12") ** (D(du) / D(252)))
    assert valor_atualizado(titulo("pre", "12"), [aplicacao(ap, 1_000_000)], CDI, {}, ate) == esperado
    assert du == 39 and esperado > 1_000_000


def test_percentual_do_cdi():
    ap, ate = date(2026, 1, 5), date(2026, 3, 13)
    esperado = D(2_000_000)
    for d in sorted(CDI):
        if ap < d <= ate:
            esperado *= 1 + CDI[d] / 100 * D("1.10")
    assert valor_atualizado(titulo("cdi", "110"), [aplicacao(ap, 2_000_000)], CDI, {}, ate) == centavo(esperado)


def test_cdi_com_taxa_diaria_variavel_usa_cada_dia():
    serie = dias_uteis_sinteticos(date(2026, 1, 2), date(2026, 1, 16))
    serie[date(2026, 1, 9)] = D("0.0500")
    ap, ate = date(2026, 1, 2), date(2026, 1, 16)  # 2/1 exclusivo: 5/1..16/1 = 10 dias úteis
    esperado = D(1_000_000)
    for d in sorted(serie):
        if ap < d <= ate:
            esperado *= 1 + serie[d] / 100
    assert valor_atualizado(titulo("cdi", "100"), [aplicacao(ap, 1_000_000)], serie, {}, ate) == centavo(esperado)


IPCA = {date(2026, 1, 1): D("0.50"), date(2026, 2, 1): D("0.40"), date(2026, 3, 1): D("0.30")}


def test_ipca_mais_taxa_com_ipca_divulgado():
    ap, ate = date(2026, 1, 15), date(2026, 4, 10)
    du = contar_du(CDI, ap, ate)
    esperado = D(1_000_000) * D("1.005") * D("1.004") * D("1.003") * D("1.06") ** (D(du) / D(252))
    det = valor_atualizado_detalhe(titulo("ipca", "6"), [aplicacao(ap, 1_000_000)], CDI, IPCA, ate)
    assert det.valor == centavo(esperado) and det.estimado is False


def test_ipca_mes_nao_divulgado_usa_ultimo_e_avisa_estimado():
    ap, ate = date(2026, 1, 15), date(2026, 4, 10)
    ipca = {k: v for k, v in IPCA.items() if k != date(2026, 3, 1)}  # março ainda não saiu
    du = contar_du(CDI, ap, ate)
    esperado = D(1_000_000) * D("1.005") * D("1.004") * D("1.004") * D("1.06") ** (D(du) / D(252))
    det = valor_atualizado_detalhe(titulo("ipca", "6"), [aplicacao(ap, 1_000_000)], CDI, ipca, ate)
    assert det.valor == centavo(esperado) and det.estimado is True


def test_aceita_series_como_lista_de_pares():
    ap, ate = date(2026, 1, 5), date(2026, 2, 27)
    a = valor_atualizado(titulo("pre", "12"), [aplicacao(ap, 1_000_000)], list(CDI.items()), [], ate)
    assert a == valor_atualizado(titulo("pre", "12"), [aplicacao(ap, 1_000_000)], CDI, {}, ate)


def test_varios_aportes_somam_por_aplicacao():
    a1, a2, ate = date(2026, 1, 5), date(2026, 2, 2), date(2026, 3, 13)
    esperado = D(0)
    for ini, valor in ((a1, 1_000_000), (a2, 500_000)):
        f = D(1)
        for d in sorted(CDI):
            if ini < d <= ate:
                f *= 1 + CDI[d] / 100 * D("1.10")
        esperado += valor * f
    ops = [aplicacao(a1, 1_000_000), aplicacao(a2, 500_000)]
    assert valor_atualizado(titulo("cdi", "110"), ops, CDI, {}, ate) == centavo(esperado)


def test_aplicacao_no_proprio_dia_ou_futura():
    t = titulo("cdi", "100")
    assert valor_atualizado(t, [aplicacao(date(2026, 3, 2), 1000)], CDI, {}, date(2026, 3, 2)) == 1000
    assert valor_atualizado(t, [aplicacao(date(2026, 3, 9), 1000)], CDI, {}, date(2026, 3, 2)) == 0
    assert valor_atualizado(t, [], CDI, {}, date(2026, 3, 2)) == 0


def test_resgate_baixa_proporcionalmente_todas_as_aplicacoes():
    t = titulo("pre", "12")
    ap1, ap2, rg, ate = date(2026, 1, 5), date(2026, 1, 12), date(2026, 2, 13), date(2026, 3, 13)
    f = lambda ini, fim: D("1.12") ** (D(contar_du(CDI, ini, fim)) / D(252))  # noqa: E731
    v1, v2 = D(1_000_000) * f(ap1, rg), D(500_000) * f(ap2, rg)
    resgatado = 700_000
    restante = 1 - D(resgatado) / (v1 + v2)
    esperado = restante * (D(1_000_000) * f(ap1, ate) + D(500_000) * f(ap2, ate))
    ops = [aplicacao(ap1, 1_000_000), aplicacao(ap2, 500_000), resgate(rg, resgatado)]
    assert valor_atualizado(t, ops, CDI, {}, ate) == centavo(esperado)
    # resgate posterior à data de cálculo é ignorado
    antes = date(2026, 2, 6)
    sem_resgate = D(1_000_000) * f(ap1, antes) + D(500_000) * f(ap2, antes)
    assert valor_atualizado(t, ops, CDI, {}, antes) == centavo(sem_resgate)


def test_resgate_total_zera():
    t = titulo("cdi", "100")
    ap, rg = date(2026, 1, 5), date(2026, 2, 13)
    bruto = valor_atualizado(t, [aplicacao(ap, 1_000_000)], CDI, {}, rg)
    assert valor_atualizado(t, [aplicacao(ap, 1_000_000), resgate(rg, bruto)], CDI, {}, date(2026, 3, 13)) == 0


def test_vencimento_limita_o_rendimento():
    ap, venc = date(2026, 1, 5), date(2026, 2, 6)
    t = titulo("pre", "12", vencimento=venc)
    assert valor_atualizado(t, [aplicacao(ap, 1_000_000)], CDI, {}, date(2026, 5, 29)) == \
        valor_atualizado(t, [aplicacao(ap, 1_000_000)], CDI, {}, venc)


def test_indexador_desconhecido():
    with pytest.raises(ValueError):
        valor_atualizado(titulo("selic", "1"), [aplicacao(date(2026, 1, 5), 1000)], CDI, {}, date(2026, 2, 5))


def test_integra_com_posicao_no_resgate():
    t = titulo("cdi", "100")
    ap, rg = date(2026, 1, 5), date(2026, 2, 13)
    bruto = valor_atualizado(t, [aplicacao(ap, 1_000_000)], CDI, {}, rg)
    ops = [aplicacao(ap, 1_000_000), resgate(rg, bruto // 2)]
    p = posicao(ops, valor_na_data=valor_na_data_para_posicao(t, CDI, {}))
    assert p.custo_brl == 500_000 and p.lucro_realizado_brl == bruto // 2 - 500_000
