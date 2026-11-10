from datetime import date

from app.services.consulta_lancamentos import listar_lancamentos, totais_da_lista
from app.services.entrada_rapida import NovaEntrada, registrar_entrada
from app.services.faturas import fatura_da_compra, limite_disponivel, pagar_fatura, total_fatura, total_pago
from app.services.lancamentos import atualizar_lancamento, criar_transferencia
from app.services.resumo_mes import a_pagar, despesas_por_categoria, totais_mes
from app.services.saldos import saldo_atual, saldo_previsto, saldos_contas
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento

HOJE = date(2026, 10, 5)


def test_saldos_ignoram_lancamento_ignorado(session):
    conta = nova_conta(session, saldo=10000)
    novo_lancamento(session, conta, date(2026, 10, 2), -3000)
    novo_lancamento(session, conta, date(2026, 10, 2), -9999, ignorar=True)
    novo_lancamento(session, conta, date(2026, 10, 8), -500, status="pendente")
    novo_lancamento(session, conta, date(2026, 10, 8), -7777, status="pendente", ignorar=True)
    assert saldo_atual(session, conta) == 7000
    assert saldo_previsto(session, conta, date(2026, 10, 31)) == 6500
    [saldo] = saldos_contas(session, HOJE, "2026-10")
    assert (saldo.atual, saldo.previsto_dia, saldo.previsto_mes) == (7000, 7000, 6500)


def test_resumo_mes_ignora_lancamento_ignorado(session):
    conta = nova_conta(session)
    cat = nova_categoria(session, "Lazer")
    novo_lancamento(session, conta, date(2026, 10, 2), 100000)
    novo_lancamento(session, conta, date(2026, 10, 2), 888800, ignorar=True)
    novo_lancamento(session, conta, date(2026, 10, 3), -2000, categoria=cat)
    novo_lancamento(session, conta, date(2026, 10, 3), -5000, categoria=cat, ignorar=True)
    novo_lancamento(session, conta, date(2026, 10, 6), -4000, status="pendente", ignorar=True)
    t = totais_mes(session, "2026-10")
    assert (t.receitas_efetivado, t.receitas_projetado) == (100000, 100000)
    assert (t.despesas_efetivado, t.despesas_projetado) == (2000, 2000)
    assert [(f.nome, f.total) for f in despesas_por_categoria(session, "2026-10")] == [("Lazer", 2000)]
    assert a_pagar(session, HOJE) == []


def test_faturas_ignoram_lancamento_ignorado(session):
    conta = nova_conta(session)
    cartao = novo_cartao(session, pagadora=conta, limite=100000)
    fatura = fatura_da_compra(session, cartao, date(2026, 10, 2))
    novo_lancamento(session, cartao, date(2026, 10, 2), -3000, fatura_id=fatura.id, status="pendente")
    novo_lancamento(session, cartao, date(2026, 10, 2), -9000, fatura_id=fatura.id, status="pendente", ignorar=True)
    assert total_fatura(session, fatura) == 3000
    assert limite_disponivel(session, cartao) == 97000
    _, entrada = pagar_fatura(session, fatura, 3000, HOJE)
    entrada.ignorar = True
    assert total_pago(session, fatura) == 0


def test_totais_da_lista_ignoram_ignorado_mas_lista_mostra(session):
    conta = nova_conta(session)
    novo_lancamento(session, conta, date(2026, 10, 2), -2000)
    novo_lancamento(session, conta, date(2026, 10, 2), -5000, ignorar=True)
    novo_lancamento(session, conta, date(2026, 10, 2), 9000, ignorar=True)
    itens = listar_lancamentos(session, "2026-10")
    assert len(itens) == 3
    assert totais_da_lista(itens) == (0, 2000)


def test_nova_entrada_ignorar_e_transferencia_acompanha_par(session):
    a, b = nova_conta(session, "A"), nova_conta(session, "B")
    [lanc] = registrar_entrada(session, NovaEntrada(
        tipo="despesa", valor=1000, descricao="X", data=HOJE, conta_id=a.id, ignorar=True), HOJE)
    assert lanc.ignorar is True
    saida, entrada = registrar_entrada(session, NovaEntrada(
        tipo="transferencia", valor=1000, descricao="T", data=HOJE, conta_id=a.id, conta_destino_id=b.id,
        ignorar=True), HOJE)
    assert saida.ignorar and entrada.ignorar
    assert saldo_atual(session, b) == 0


def test_atualizar_lancamento_ignorar_propaga_ao_par(session):
    a, b = nova_conta(session, "A"), nova_conta(session, "B")
    saida, entrada = criar_transferencia(session, origem=a, destino=b, data=HOJE, valor_abs=1000, efetivado=True)
    kw = dict(data=HOJE, descricao="T", valor_abs=1000, categoria_id=None, tags=[])
    atualizar_lancamento(session, saida, ignorar=True, **kw)
    assert saida.ignorar and entrada.ignorar
    assert saldo_atual(session, b) == 0
    atualizar_lancamento(session, saida, **kw)  # sem informar: mantém
    assert entrada.ignorar
    atualizar_lancamento(session, saida, ignorar=False, **kw)
    assert not entrada.ignorar and saldo_atual(session, b) == 1000
