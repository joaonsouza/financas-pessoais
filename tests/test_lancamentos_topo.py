from datetime import date

from app.services.faturas import obter_ou_criar_fatura
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento


def _base(db):
    conta = nova_conta(db, "Itaú")
    mercado = nova_categoria(db, "Mercado")
    novo_lancamento(db, conta, date(2026, 10, 2), -1250, categoria=mercado, descricao="Padaria")
    novo_lancamento(db, conta, date(2026, 10, 3), -30000, descricao="Conserto", status="pendente")
    novo_lancamento(db, conta, date(2026, 10, 5), 500000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 25), 20000, descricao="Freela", status="pendente")
    db.commit()
    return conta


def test_pilula_de_tipo_com_menu_que_mantem_filtros(client, db):
    conta = _base(db)
    texto = client.get(f"/lancamentos?mes=2026-10&conta={conta.id}").text
    assert 'aria-label="Tipo: Todas"' in texto
    for tipo in ("receita", "despesa", "transferencia", "pagamento_fatura", "ajuste"):
        assert f'href="?mes=2026-10&amp;conta={conta.id}&amp;tipo={tipo}"' in texto
    texto = client.get("/lancamentos?mes=2026-10&tipo=despesa").text
    assert 'aria-label="Tipo: Despesas"' in texto
    assert 'href="?mes=2026-10"' in texto  # volta para Todas


def test_botao_novo_conforme_o_tipo(client, db):
    _base(db)
    assert 'hx-get="/lancamentos/novo?tipo=receita"' in client.get("/lancamentos?mes=2026-10&tipo=receita").text
    assert ">Nova despesa<" in client.get("/lancamentos?mes=2026-10&tipo=despesa").text
    assert ">Nova transferência<" in client.get("/lancamentos?mes=2026-10&tipo=transferencia").text
    todas = client.get("/lancamentos?mes=2026-10").text
    assert ">Nova receita<" not in todas and ">Nova despesa<" not in todas


def test_cards_de_receitas(client, db):
    _base(db)
    texto = client.get("/lancamentos?mes=2026-10&tipo=receita").text
    assert "Receitas pendentes" in texto and "R$ 200,00" in texto
    assert "Receitas recebidas" in texto and "R$ 5.000,00" in texto
    assert "R$ 5.200,00" in texto
    assert 'href="?mes=2026-10&amp;tipo=receita&amp;status=pendente"' not in texto  # ordem dos parâmetros
    assert 'href="?mes=2026-10&amp;status=pendente&amp;tipo=receita"' in texto
    assert 'href="?mes=2026-10&amp;status=efetivado&amp;tipo=receita"' in texto


def test_card_de_situacao_ativo_desliga_e_cards_ignoram_o_filtro(client, db):
    _base(db)
    texto = client.get("/lancamentos?mes=2026-10&tipo=receita&status=pendente").text
    # lista filtrada, mas os cards continuam com o mês inteiro
    assert ">Freela<" in texto and ">Salário<" not in texto
    assert "R$ 5.000,00" in texto
    assert 'href="?mes=2026-10&amp;tipo=receita"' in texto


def test_cards_de_despesas_com_fatura(client, db):
    conta = _base(db)
    cartao = novo_cartao(db, "Cartão Nubank", pagadora=conta, fechamento=3, vencimento=10)
    fatura = obter_ou_criar_fatura(db, cartao, "2026-10")
    novo_lancamento(db, cartao, date(2026, 9, 20), -10000, descricao="Mercado A", fatura_id=fatura.id)
    db.commit()
    texto = client.get("/lancamentos?mes=2026-10&tipo=despesa").text
    # pendentes: conserto (300) + fatura aberta (100); pagas: padaria (12,50)
    assert "Despesas pendentes" in texto and "R$ 400,00" in texto
    assert "Despesas pagas" in texto and "R$ 12,50" in texto
    assert "R$ 412,50" in texto


def test_cards_de_todas(client, db):
    _base(db)
    texto = client.get("/lancamentos?mes=2026-10").text
    for rotulo in ("Saldo atual", "Receitas", "Despesas", "Balanço mensal"):
        assert f">{rotulo}<" in texto
    assert 'href="/contas?mes=2026-10"' in texto
    assert 'href="?mes=2026-10&amp;tipo=receita"' in texto
    assert 'href="?mes=2026-10&amp;tipo=despesa"' in texto


def test_navegacao_de_mes_na_lista_mantem_filtros(client, db):
    _base(db)
    texto = client.get("/lancamentos?mes=2026-10&tipo=receita&ordem=valor&dir=asc").text
    assert 'href="?mes=2026-09&amp;tipo=receita&amp;ordem=valor&amp;dir=asc"' in texto
    assert 'href="?mes=2026-11&amp;tipo=receita&amp;ordem=valor&amp;dir=asc"' in texto
    # o seletor de mês do cabeçalho (que perde os filtros) não aparece nesta tela
    assert 'aria-label="Selecionar mês"' not in texto
    assert 'aria-label="Mês da lista"' in texto


def test_busca_e_filtros_recolhidos_abrem_quando_ativos(client, db):
    conta = _base(db)
    texto = client.get("/lancamentos?mes=2026-10").text
    assert 'aria-label="Buscar"' in texto and 'aria-label="Filtros"' in texto
    assert "busca: false, filtros: false" in texto
    texto = client.get(f"/lancamentos?mes=2026-10&conta={conta.id}&q=pada").text
    assert "busca: true, filtros: true" in texto
    assert ">Padaria<" in texto


def test_outras_telas_mantem_o_seletor_de_mes(client, db):
    _base(db)
    assert 'aria-label="Selecionar mês"' in client.get("/contas?mes=2026-10").text
