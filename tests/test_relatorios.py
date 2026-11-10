from datetime import date

from app.services.relatorios import (
    despesas_hierarquia, fixos_variaveis, janela, receitas_despesas, saldo_liquido_mensal,
)
from tests.factories import nova_categoria, nova_conta, novo_lancamento


def test_janela_tem_12_meses_terminando_no_mes():
    j = janela("2026-10")
    assert (len(j), j[0], j[-1]) == (12, "2025-11", "2026-10")


def test_receitas_despesas_por_mes_com_zeros(db):
    conta = nova_conta(db, inicio=date(2026, 1, 1))
    novo_lancamento(db, conta, date(2026, 9, 5), 300000)
    novo_lancamento(db, conta, date(2026, 9, 6), -100000)
    novo_lancamento(db, conta, date(2026, 10, 6), -50000, status="pendente")
    novo_lancamento(db, conta, date(2026, 10, 7), -9999, ignorar=True)
    novo_lancamento(db, conta, date(2024, 1, 7), -9999)  # fora da janela
    pontos = {p.competencia: p for p in receitas_despesas(db, "2026-10")}
    assert (pontos["2026-09"].receitas, pontos["2026-09"].despesas) == (300000, 100000)
    assert (pontos["2026-10"].receitas, pontos["2026-10"].despesas) == (0, 50000)
    assert pontos["2026-01"].despesas == 0


def test_fixos_variaveis_ignora_divida_e_conta_sem_categoria_como_variavel(db):
    conta = nova_conta(db, inicio=date(2026, 1, 1))
    fixo = nova_categoria(db, "Aluguel", grupo="fixo")
    var = nova_categoria(db, "Mercado", grupo="variavel")
    div = nova_categoria(db, "Parcela", grupo="divida")
    novo_lancamento(db, conta, date(2026, 10, 2), -100000, categoria=fixo)
    novo_lancamento(db, conta, date(2026, 10, 3), -30000, categoria=var)
    novo_lancamento(db, conta, date(2026, 10, 4), -7000)
    novo_lancamento(db, conta, date(2026, 10, 5), -50000, categoria=div)
    ponto = fixos_variaveis(db, "2026-10")[-1]
    assert (ponto.fixos, ponto.variaveis) == (100000, 37000)


def test_saldo_liquido_mensal_acumula_e_respeita_data_inicial(db):
    a = nova_conta(db, "A", saldo=100000, inicio=date(2026, 1, 1))
    nova_conta(db, "B", saldo=50000, inicio=date(2026, 9, 1))
    nova_conta(db, "Fora", saldo=999999, sl=False, inicio=date(2026, 1, 1))
    novo_lancamento(db, a, date(2026, 8, 10), 20000)
    novo_lancamento(db, a, date(2026, 10, 2), -30000)
    novo_lancamento(db, a, date(2026, 10, 3), -1, status="pendente")
    saldos = {p.competencia: p.saldo for p in saldo_liquido_mensal(db, "2026-10")}
    assert saldos["2026-07"] == 100000
    assert saldos["2026-08"] == 120000
    assert saldos["2026-09"] == 170000       # B entra em setembro
    assert saldos["2026-10"] == 140000


def test_despesas_hierarquia_agrupa_subcategorias(db):
    conta = nova_conta(db, inicio=date(2026, 1, 1))
    pai = nova_categoria(db, "Mercado", cor="#111111")
    filha = nova_categoria(db, "Feira", pai=pai)
    outra = nova_categoria(db, "Lazer")
    novo_lancamento(db, conta, date(2026, 10, 2), -10000, categoria=pai)
    novo_lancamento(db, conta, date(2026, 10, 3), -5000, categoria=filha)
    novo_lancamento(db, conta, date(2026, 10, 4), -3000, categoria=outra)
    novo_lancamento(db, conta, date(2026, 10, 5), -1000)
    nos = despesas_hierarquia(db, "2026-10")
    assert [(n.nome, n.total) for n in nos] == [("Mercado", 15000), ("Lazer", 3000), ("Sem categoria", 1000)]
    assert nos[0].filhas == [("Feira", 5000)]
