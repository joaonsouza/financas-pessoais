"""Card "Onde o dinheiro foi" do Planejamento: gasto por categoria contra o limite do orçamento."""
from datetime import date

from app.services.orcamento import definir_limite
from app.services.relatorios import NoCategoria
from app.web.routes.planejamento import MAX_CATEGORIAS, onde_foi
from app.services.consulta_lancamentos import FiltrosLancamento, linhas_da_lista, totais_da_lista
from app.services.faturas import obter_ou_criar_fatura
from app.services.relatorios import despesas_hierarquia
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento


def _fatias(*totais):
    return [NoCategoria(i + 1, f"Cat {i + 1}", "#123456", t, []) for i, t in enumerate(totais)]


def test_maiores_primeiro_e_o_resto_em_outras():
    itens = onde_foi(_fatias(*range(10_000, 0, -1_000)), [])
    assert len(itens) == MAX_CATEGORIAS + 1
    assert itens[-1]["nome"] == "Outras (3)" and itens[-1]["total"] == 3_000 + 2_000 + 1_000
    assert itens[0]["largura"] == 100 and itens[-1]["limite"] is None


def test_limite_entra_na_regua():
    class Linha:  # só o que onde_foi lê de LinhaOrcamento
        def __init__(self, categoria_id, limite):
            self.categoria = type("C", (), {"id": categoria_id})()
            self.limite = limite

    itens = onde_foi(_fatias(5_000, 2_000), [Linha(2, 10_000)])
    assert (itens[0]["largura"], itens[1]["largura"], itens[1]["marca"]) == (50, 20, 100)  # régua = maior limite
    assert itens[0]["marca"] is None


def test_subcategorias_na_mesma_regua_e_o_lancado_direto_na_raiz():
    raiz = NoCategoria(1, "Alimentação", "#123456", 10_000, [("Mercado", 6_000), ("Delivery", 3_000)])
    (item,) = onde_foi([raiz], [], {(1, "Mercado"): 11, (1, "Delivery"): 12})
    assert item["filhas"] == [("Mercado", 6_000, 60, 11, None, None), ("Delivery", 3_000, 30, 12, None, None),
                              ("Direto em Alimentação", 1_000, 10, 1, None, None)]  # o direto abre a lista da raiz
    assert onde_foi(_fatias(5_000), [])[0]["filhas"] == []  # sem subcategoria, nada para abrir
    outras = onde_foi(_fatias(*range(9_000, 0, -1_000)), [])[-1]
    assert [(f[0], f[3]) for f in outras["filhas"]] == [("Cat 8", 8), ("Cat 9", 9)]


def test_card_mostra_categorias_limite_e_atalho(client, db):
    conta = nova_conta(db, "Nubank", saldo=500_000)
    alimentacao, lazer = nova_categoria(db, "Alimentação"), nova_categoria(db, "Lazer")
    mercado = nova_categoria(db, "Mercado", pai=alimentacao)
    novo_lancamento(db, conta, date(2026, 10, 1), 1_000_000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 2), -60_000, categoria=mercado)
    novo_lancamento(db, conta, date(2026, 10, 3), -20_000, categoria=lazer)
    definir_limite(db, "2026-10", alimentacao.id, 50_000)
    db.commit()
    texto = client.get("/planejamento?mes=2026-10").text
    card = texto[texto.index("Onde o dinheiro foi"):texto.index("Ajuste do mês")]
    assert card.index("Alimentação") < card.index("Lazer")
    assert 'aria-label="Subcategorias de Alimentação"' in card and "Mercado" in card  # fechadas, abrem no clique
    assert f'href="/lancamentos?mes=2026-10&amp;tipo=despesa&amp;categoria={alimentacao.id}"' in card
    assert f'href="/lancamentos?mes=2026-10&tipo=despesa&categoria={mercado.id}"' in card
    assert "R$ 100,00 acima do limite de R$ 500,00" in card
    assert "definir limite" in card and "Sem meta de economia" in card


def test_total_bate_com_o_teto_e_creditos_aparecem_a_parte(client, db):
    conta = nova_conta(db, "Nubank", saldo=500_000)
    lazer, dividas = nova_categoria(db, "Lazer"), nova_categoria(db, "Dívidas")
    novo_lancamento(db, conta, date(2026, 10, 1), 1_000_000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 2), -50_000, categoria=lazer)
    novo_lancamento(db, conta, date(2026, 10, 3), 10_000, tipo="despesa", categoria=dividas,
                    descricao="Crédito de parcelamento de compra")  # estorno: despesa com valor positivo
    definir_limite(db, "2026-10", None, 100_000)  # teto
    db.commit()
    texto = client.get("/planejamento?mes=2026-10").text
    card = texto[texto.index("Onde o dinheiro foi"):texto.index("Ajuste do mês")]
    assert "R$ 400,00 · 4% da receita" in card                # 500 de lazer − 100 de crédito, como o teto
    assert "Créditos e estornos" in card and "-R$ 100,00" in card


def test_lista_de_lancamentos_filtra_sem_categoria(client, db):
    conta = nova_conta(db, "Nubank", saldo=500_000)
    lazer = nova_categoria(db, "Lazer")
    novo_lancamento(db, conta, date(2026, 10, 2), -5_000, categoria=lazer, descricao="Cinema")
    novo_lancamento(db, conta, date(2026, 10, 3), -7_000, descricao="Padaria sem categoria")
    db.commit()
    texto = client.get("/lancamentos?mes=2026-10&tipo=despesa&categoria=sem").text
    assert "Padaria sem categoria" in texto and "Cinema" not in texto
    assert 'name="categoria" value="sem"' in texto


def test_lista_da_categoria_soma_o_mesmo_que_o_card(db):
    """Clicar na categoria leva a uma lista com o mesmo total: a compra no cartão conta no mês da fatura."""
    conta = nova_conta(db, "Nubank", saldo=500_000)
    cartao = novo_cartao(db, "Cartão", pagadora=conta, fechamento=3, vencimento=10)
    lazer = nova_categoria(db, "Lazer")
    fatura = obter_ou_criar_fatura(db, cartao, "2026-10")  # vence 10/10
    novo_lancamento(db, cartao, date(2026, 9, 20), -8_000, categoria=lazer, descricao="Show", fatura_id=fatura.id)
    novo_lancamento(db, conta, date(2026, 10, 5), -2_000, categoria=lazer, descricao="Cinema")
    db.commit()
    (no,) = despesas_hierarquia(db, "2026-10")
    _, despesas = totais_da_lista(linhas_da_lista(db, "2026-10", FiltrosLancamento(categoria_id=lazer.id, tipo="despesa")))
    assert no.total == despesas == 10_000


def test_filha_com_limite_leva_marca_e_o_limite_entra_na_regua():
    class Linha:
        def __init__(self, categoria_id, limite):
            self.categoria = type("C", (), {"id": categoria_id})()
            self.limite = limite

    raiz = NoCategoria(1, "Alimentação", "#123456", 10_000, [("Mercado", 6_000), ("Delivery", 3_000)])
    (item,) = onde_foi([raiz], [Linha(11, 20_000)], {(1, "Mercado"): 11, (1, "Delivery"): 12})
    mercado, delivery, direto = item["filhas"]
    assert mercado == ("Mercado", 6_000, 30, 11, 100, 20_000)  # a régua passa a ser o limite da filha
    assert delivery[4:] == (None, None) and direto[4:] == (None, None)


def test_card_mostra_acima_do_limite_na_filha_que_estourou(client, db):
    conta = nova_conta(db, "Nubank", saldo=500_000)
    alimentacao = nova_categoria(db, "Alimentação")
    mercado = nova_categoria(db, "Mercado", pai=alimentacao)
    novo_lancamento(db, conta, date(2026, 10, 1), 1_000_000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 2), -60_000, categoria=mercado)
    definir_limite(db, "2026-10", mercado.id, 50_000)
    db.commit()
    texto = client.get("/planejamento?mes=2026-10").text
    card = texto[texto.index("Onde o dinheiro foi"):texto.index("Ajuste do mês")]
    sub = card[card.index('aria-label="Subcategorias de Alimentação"', card.index("<ul id=")):]
    assert "acima do limite" in sub and "bg-expense" in sub
