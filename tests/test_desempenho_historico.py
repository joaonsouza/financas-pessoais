from datetime import date
from app.models import Categoria, Conta, Lancamento
from app.services.desempenho.historico import (
    categorias_desempenho_mes,
    serie_fixos_variaveis_dividas,
    serie_historica_categoria,
    serie_patrimonio,
    serie_receitas_despesas,
)
from tests.factories import nova_categoria, nova_conta, novo_lancamento

HOJE = date(2026, 10, 15)


def test_serie_receitas_despesas_efetivado_e_pendente(db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 9, 1))
    # Mês 09: receita efetivada 3000, despesa efetivada 1000
    novo_lancamento(db, conta, date(2026, 9, 5), 300000, tipo="receita", status="efetivado")
    novo_lancamento(db, conta, date(2026, 9, 6), -100000, tipo="despesa", status="efetivado")

    # Mês 10: receita pendente 5000, despesa pendente 2000
    novo_lancamento(db, conta, date(2026, 10, 20), 500000, tipo="receita", status="pendente")
    novo_lancamento(db, conta, date(2026, 10, 22), -200000, tipo="despesa", status="pendente")

    serie = serie_receitas_despesas(db, "2026-10", n=2)
    assert serie.competencias == ["2026-09", "2026-10"]
    # Mês 9: 3000 ef, 0 pend; 1000 ef, 0 pend
    assert serie.receitas_efetivado[0] == 3000.0
    assert serie.receitas_pendente[0] == 0.0
    assert serie.despesas_efetivado[0] == 1000.0
    # Mês 10: 0 ef, 5000 pend; 0 ef, 2000 pend
    assert serie.receitas_efetivado[1] == 0.0
    assert serie.receitas_pendente[1] == 5000.0
    assert serie.despesas_pendente[1] == 2000.0


def test_serie_patrimonio_inclui_investimentos(db):
    corrente = nova_conta(db, "Corrente", saldo=100000, sl=True, inicio=date(2026, 9, 1))
    invest = nova_conta(db, "Investimento", tipo="investimento", saldo=400000, sl=False, inicio=date(2026, 9, 1))

    novo_lancamento(db, corrente, date(2026, 9, 15), 10000)
    novo_lancamento(db, corrente, date(2026, 10, 5), 50000)
    novo_lancamento(db, invest, date(2026, 10, 6), 20000)

    pat = serie_patrimonio(db, "2026-10", n=2)
    assert pat.competencias == ["2026-09", "2026-10"]
    # Mês 09: saldo liq 1100, invest 4000 -> total 5100
    assert pat.saldo_liquido[0] == 1100.0
    assert pat.investimentos[0] == 4000.0
    assert pat.patrimonio_total[0] == 5100.0

    # Mês 10: saldo liq 1600, invest 4200 -> total 5800
    assert pat.saldo_liquido[1] == 1600.0
    assert pat.investimentos[1] == 4200.0
    assert pat.patrimonio_total[1] == 5800.0


def test_categorias_desempenho_mes_e_serie_categoria(db):
    conta = nova_conta(db, "Conta", inicio=date(2026, 8, 1))
    pai = nova_categoria(db, "Alimentação", cor="#22c55e")
    filha = nova_categoria(db, "Mercado", pai=pai)

    # Histórico de Alimentação nos meses 08 e 09
    novo_lancamento(db, conta, date(2026, 8, 10), -20000, categoria=filha)
    novo_lancamento(db, conta, date(2026, 9, 10), -40000, categoria=filha)

    # Mês 10: 10000 direto no pai, 30000 na filha = 40000
    novo_lancamento(db, conta, date(2026, 10, 2), -10000, categoria=pai)
    novo_lancamento(db, conta, date(2026, 10, 3), -30000, categoria=filha)

    painel = categorias_desempenho_mes(db, "2026-10")
    assert len(painel.itens) == 1
    item = painel.itens[0]
    assert item.nome == "Alimentação"
    assert item.total == 40000
    assert item.media_anterior == 30000

    # Subcategorias
    filhas_nomes = [f.nome for f in item.filhas]
    assert "Mercado" in filhas_nomes
    assert "(direto em Alimentação)" in filhas_nomes

    # Teste de série histórica da categoria
    serie_cat = serie_historica_categoria(db, pai.id, "2026-10", n=3)
    assert serie_cat["nome"] == "Alimentação"
    assert len(serie_cat["valores"]) == 3
    assert serie_cat["valores"][-1] == 400.0
