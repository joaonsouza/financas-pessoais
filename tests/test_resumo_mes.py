from datetime import date

from app.services.faturas import fatura_da_compra, pagar_fatura
from app.services.resumo_mes import a_pagar, despesas_por_categoria, totais_mes
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento


def test_totais_mes(session):
    conta = nova_conta(session)
    novo_lancamento(session, conta, date(2026, 10, 30), 500000)
    novo_lancamento(session, conta, date(2026, 10, 15), 100000, status="pendente")
    novo_lancamento(session, conta, date(2026, 10, 2), -20000)
    novo_lancamento(session, conta, date(2026, 10, 20), -5000, status="pendente")
    novo_lancamento(session, conta, date(2026, 10, 21), -7000, status="cancelado")
    novo_lancamento(session, conta, date(2026, 10, 3), -9000, tipo="transferencia")
    novo_lancamento(session, conta, date(2026, 10, 3), -9000, tipo="pagamento_fatura")
    novo_lancamento(session, conta, date(2026, 10, 3), 300, tipo="ajuste")
    novo_lancamento(session, conta, date(2026, 11, 1), -1000)

    t = totais_mes(session, "2026-10")

    assert (t.receitas_efetivado, t.receitas_projetado) == (500000, 600000)
    assert (t.despesas_efetivado, t.despesas_projetado) == (20000, 25000)
    assert (t.balanco_efetivado, t.balanco_projetado) == (480000, 575000)


def test_despesas_por_categoria_agrupa_no_pai(session):
    conta = nova_conta(session)
    alimentacao = nova_categoria(session, "Alimentação", cor="#f97316")
    mercado = nova_categoria(session, "Mercado", pai=alimentacao)
    lazer = nova_categoria(session, "Lazer", cor="#a855f7")
    novo_lancamento(session, conta, date(2026, 10, 2), -3000, categoria=mercado)
    novo_lancamento(session, conta, date(2026, 10, 3), -1000, categoria=alimentacao)
    novo_lancamento(session, conta, date(2026, 10, 4), -2000, categoria=lazer, status="pendente")
    novo_lancamento(session, conta, date(2026, 10, 5), -500)

    projetado = despesas_por_categoria(session, "2026-10")
    efetivado = despesas_por_categoria(session, "2026-10", projetado=False)

    assert [(f.nome, f.total) for f in projetado] == [("Alimentação", 4000), ("Lazer", 2000), ("Sem categoria", 500)]
    assert projetado[0].cor == "#f97316"
    assert [(f.nome, f.total) for f in efetivado] == [("Alimentação", 4000), ("Sem categoria", 500)]


def test_a_pagar(session):
    hoje = date(2026, 10, 5)
    nubank = nova_conta(session, "Nubank", saldo=100000)
    novo_lancamento(session, nubank, date(2026, 10, 1), -1000, status="pendente", descricao="Luz")
    novo_lancamento(session, nubank, date(2026, 10, 8), -2000, status="pendente", descricao="Água")
    novo_lancamento(session, nubank, date(2026, 10, 20), -3000, status="pendente", descricao="Fora da janela")
    novo_lancamento(session, nubank, date(2026, 10, 9), 5000, status="pendente", descricao="Receita")

    cartao = novo_cartao(session, "Cartão Itaú", pagadora=nubank, fechamento=3, vencimento=10)
    fatura = fatura_da_compra(session, cartao, date(2026, 10, 2))
    novo_lancamento(session, cartao, date(2026, 10, 2), -30000, fatura_id=fatura.id)

    outro = novo_cartao(session, "Cartão MP", pagadora=nubank, fechamento=3, vencimento=9)
    fatura_mp = fatura_da_compra(session, outro, date(2026, 10, 2))
    novo_lancamento(session, outro, date(2026, 10, 2), -4000, fatura_id=fatura_mp.id)
    pagar_fatura(session, fatura_mp, 4000, date(2026, 10, 9), efetivado=False)

    itens = a_pagar(session, hoje)

    assert [(i.descricao, i.valor, i.atrasado) for i in itens] == [
        ("Luz", 1000, True),
        ("Água", 2000, False),
        ("Pagamento fatura Cartão MP 2026-10", 4000, False),
        ("Fatura Cartão Itaú", 30000, False),
    ]
    assert itens[-1].fatura_id == fatura.id
