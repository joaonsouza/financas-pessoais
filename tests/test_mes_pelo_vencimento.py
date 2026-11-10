"""Compra no cartão conta no mês em que a fatura vence (dinheiro que sai), igual à tela de Lançamentos.
Fica pendente até a fatura ser paga."""
from datetime import date

from app.services.faturas import fatura_da_compra, pagar_fatura
from app.services.indicadores_curso import _carregar_mes, _grupo, _soma_por
from app.services.orcamento import gasto_categoria
from app.services.relatorios import despesas_hierarquia, fixos_variaveis, receitas_despesas
from app.services.resumo_mes import despesas_por_categoria, totais_mes
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento


def _cenario(session):
    """Conta com despesa de R$ 10 em outubro; cartão (fecha 3, vence 10) com compra de R$ 50 em 15/10,
    que cai na fatura que vence em 10/11."""
    conta = nova_conta(session)
    cartao = novo_cartao(session, pagadora=conta)
    mercado = nova_categoria(session, "Mercado", grupo="variavel")
    novo_lancamento(session, conta, date(2026, 10, 2), -1000, categoria=mercado)
    fatura = fatura_da_compra(session, cartao, date(2026, 10, 15))
    novo_lancamento(session, cartao, date(2026, 10, 15), -5000, categoria=mercado, fatura_id=fatura.id)
    return conta, cartao, mercado, fatura


def test_totais_mes_compra_no_cartao_conta_no_vencimento(session):
    _cenario(session)

    outubro = totais_mes(session, "2026-10")
    novembro = totais_mes(session, "2026-11")

    assert (outubro.despesas_efetivado, outubro.despesas_projetado) == (1000, 1000)
    # fatura aberta: a compra está a pagar
    assert (novembro.despesas_efetivado, novembro.despesas_projetado) == (0, 5000)


def test_compra_fica_efetivada_quando_a_fatura_e_paga(session):
    _, _, _, fatura = _cenario(session)
    pagar_fatura(session, fatura, 5000, date(2026, 11, 10))

    novembro = totais_mes(session, "2026-11")

    assert (novembro.despesas_efetivado, novembro.despesas_projetado) == (5000, 5000)


def test_compra_cancelada_nao_conta(session):
    _, cartao, _, fatura = _cenario(session)
    novo_lancamento(session, cartao, date(2026, 10, 16), -700, fatura_id=fatura.id, status="cancelado")

    assert totais_mes(session, "2026-11").despesas_projetado == 5000


def test_despesas_por_categoria_pelo_vencimento(session):
    _cenario(session)

    assert [(f.nome, f.total) for f in despesas_por_categoria(session, "2026-10")] == [("Mercado", 1000)]
    assert [(f.nome, f.total) for f in despesas_por_categoria(session, "2026-11")] == [("Mercado", 5000)]
    assert despesas_por_categoria(session, "2026-11", projetado=False) == []


def test_orcamento_pelo_vencimento(session):
    _, _, mercado, _ = _cenario(session)

    assert gasto_categoria(session, "2026-10", mercado.id) == (1000, 1000)
    assert gasto_categoria(session, "2026-11", mercado.id) == (0, 5000)


def test_relatorios_pelo_vencimento(session):
    _, _, mercado, _ = _cenario(session)

    serie = {p.competencia: p.despesas for p in receitas_despesas(session, "2026-11", n=2)}
    fixos = {p.competencia: p.variaveis for p in fixos_variaveis(session, "2026-11", n=2)}

    assert serie == {"2026-10": 1000, "2026-11": 5000}
    assert fixos == {"2026-10": 1000, "2026-11": 5000}
    assert [(n.nome, n.total) for n in despesas_hierarquia(session, "2026-11")] == [("Mercado", 5000)]


def test_indicadores_pelo_vencimento(session):
    conta, _, _, _ = _cenario(session)
    novo_lancamento(session, conta, date(2026, 11, 5), 100000)

    assert _soma_por(_carregar_mes(session, "2026-11"), _grupo)["variavel"] == 5000
