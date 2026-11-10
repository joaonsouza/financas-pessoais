from datetime import date

from app.services.consulta_lancamentos import FiltrosLancamento, linhas_da_lista, listar_lancamentos, totais_da_lista
from app.services.faturas import obter_ou_criar_fatura
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento


def _base(db):
    conta = nova_conta(db, "Nubank")
    cartao = novo_cartao(db, "Cartão Nubank", pagadora=conta, fechamento=3, vencimento=10)
    categoria = nova_categoria(db, "Mercado")
    # competência 2026-10: fecha 03/10, vence 10/10
    fatura = obter_ou_criar_fatura(db, cartao, "2026-10")
    db.commit()
    return conta, cartao, categoria, fatura


def _compras(db, cartao, fatura, categoria):
    # compras de setembro que caem na fatura que vence em outubro
    novo_lancamento(db, cartao, date(2026, 9, 20), -10000, categoria=categoria, descricao="Mercado A", fatura_id=fatura.id)
    novo_lancamento(db, cartao, date(2026, 9, 28), -2550, categoria=categoria, descricao="Uber B", fatura_id=fatura.id)
    novo_lancamento(db, cartao, date(2026, 9, 29), -999, descricao="Ignorada C", fatura_id=fatura.id, ignorar=True)
    db.commit()


def test_fatura_vira_uma_linha_no_mes_do_vencimento(db):
    conta, cartao, categoria, fatura = _base(db)
    _compras(db, cartao, fatura, categoria)
    novo_lancamento(db, conta, date(2026, 10, 5), 500000, descricao="Salário")
    db.commit()

    linhas = linhas_da_lista(db, "2026-10", FiltrosLancamento())

    faturas = [l for l in linhas if getattr(l, "eh_fatura", False)]
    assert len(faturas) == 1
    linha = faturas[0]
    assert (linha.data, linha.valor, linha.qtd, linha.cartao_id, linha.competencia) == (
        date(2026, 10, 10), -12550, 2, cartao.id, "2026-10")
    assert not any(getattr(l, "conta_id", None) == cartao.id for l in linhas)
    assert totais_da_lista(linhas) == (500000, 12550)


def test_compras_nao_aparecem_no_mes_da_compra(db):
    _, cartao, categoria, fatura = _base(db)
    _compras(db, cartao, fatura, categoria)
    assert linhas_da_lista(db, "2026-09", FiltrosLancamento()) == []


def test_filtro_por_cartao_mostra_compras_individuais(db):
    _, cartao, categoria, fatura = _base(db)
    _compras(db, cartao, fatura, categoria)
    # filtrada, a lista segue o mês da fatura (como os totais): as compras de setembro aparecem em outubro
    linhas = linhas_da_lista(db, "2026-10", FiltrosLancamento(conta_id=cartao.id))
    assert [l.descricao for l in linhas] == ["Ignorada C", "Uber B", "Mercado A"]
    assert linhas_da_lista(db, "2026-09", FiltrosLancamento(conta_id=cartao.id)) == []


def test_busca_mostra_compras_individuais(db):
    _, cartao, categoria, fatura = _base(db)
    _compras(db, cartao, fatura, categoria)
    linhas = linhas_da_lista(db, "2026-10", FiltrosLancamento(busca="uber"))
    assert [l.descricao for l in linhas] == ["Uber B"]


def test_filtro_receita_esconde_fatura(db):
    conta, cartao, categoria, fatura = _base(db)
    _compras(db, cartao, fatura, categoria)
    novo_lancamento(db, conta, date(2026, 10, 5), 500000, descricao="Salário")
    db.commit()
    linhas = linhas_da_lista(db, "2026-10", FiltrosLancamento(tipo="receita"))
    assert [l.descricao for l in linhas] == ["Salário"]
    linhas = linhas_da_lista(db, "2026-10", FiltrosLancamento(tipo="despesa"))
    assert [getattr(l, "eh_fatura", False) for l in linhas] == [True]


def test_fatura_sem_compras_nao_aparece(db):
    _base(db)
    assert linhas_da_lista(db, "2026-10", FiltrosLancamento()) == []


def test_pagamento_da_fatura_continua_na_conta(db):
    conta, cartao, categoria, fatura = _base(db)
    _compras(db, cartao, fatura, categoria)
    novo_lancamento(db, conta, date(2026, 10, 10), -12550, tipo="pagamento_fatura", descricao="Pagamento fatura", fatura_id=fatura.id)
    novo_lancamento(db, cartao, date(2026, 10, 10), 12550, tipo="pagamento_fatura", descricao="Pagamento recebido", fatura_id=fatura.id)
    db.commit()
    fatura.status = "paga"
    db.commit()
    linhas = linhas_da_lista(db, "2026-10", FiltrosLancamento())
    # em Todas a fatura é uma linha só, já marcada como paga; o pagamento não aparece de novo
    assert [l.status_fatura for l in linhas if getattr(l, "eh_fatura", False)] == ["paga"]
    assert [l for l in linhas if not getattr(l, "eh_fatura", False)] == []
    assert totais_da_lista(linhas) == (0, 12550)
    # pela conta ou pelo tipo, o pagamento aparece (explica o saldo da conta)
    assert [l.descricao for l in linhas_da_lista(db, "2026-10", FiltrosLancamento(conta_id=conta.id))] == [
        "Pagamento fatura"]
    assert [l.descricao for l in linhas_da_lista(db, "2026-10", FiltrosLancamento(tipo="pagamento_fatura"))] == [
        "Pagamento fatura"]


def test_listar_lancamentos_continua_individual(db):
    _, cartao, categoria, fatura = _base(db)
    _compras(db, cartao, fatura, categoria)
    assert len(listar_lancamentos(db, "2026-10")) == 3  # no mês da fatura


# ------------------------------------------------------------------ rota


def test_tela_mostra_linha_da_fatura_com_link(client, db):
    _, cartao, categoria, fatura = _base(db)
    _compras(db, cartao, fatura, categoria)
    texto = client.get("/lancamentos?mes=2026-10").text
    assert "Fatura Cartão Nubank" in texto
    assert f'href="/cartoes/{cartao.id}?fatura=2026-10"' in texto
    assert "2 compras" in texto
    assert "Mercado A" not in texto


def test_tela_filtrada_pelo_cartao_mostra_compras(client, db):
    _, cartao, categoria, fatura = _base(db)
    _compras(db, cartao, fatura, categoria)
    texto = client.get(f"/lancamentos?mes=2026-10&conta={cartao.id}").text
    assert "Mercado A" in texto
    assert "Fatura Cartão Nubank" not in texto


def test_cards_receita_e_despesa_filtram(client, db):
    conta, _, categoria, _ = _base(db)
    novo_lancamento(db, conta, date(2026, 10, 2), -1250, categoria=categoria, descricao="Padaria")
    novo_lancamento(db, conta, date(2026, 10, 5), 500000, descricao="Salário")
    db.commit()
    texto = client.get("/lancamentos?mes=2026-10").text
    assert 'href="?mes=2026-10&amp;tipo=receita"' in texto
    assert 'href="?mes=2026-10&amp;tipo=despesa"' in texto

    texto = client.get("/lancamentos?mes=2026-10&tipo=receita").text
    assert "Salário" in texto and "Padaria" not in texto
    # o seletor de tipo volta para Todas; em Receitas os cards são só de receitas
    assert 'href="?mes=2026-10"' in texto
    assert "-R$ 12,50" not in texto
