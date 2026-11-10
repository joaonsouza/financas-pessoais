from datetime import date

from app.models import set_config
from app.services.lancamentos import criar_lancamento
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento


def _cenario(db):
    nubank = nova_conta(db, "Nubank", saldo=100000)
    mercado = nova_categoria(db, "Mercado")
    novo_lancamento(db, nubank, date(2026, 10, 1), 500000, descricao="Salário")
    novo_lancamento(db, nubank, date(2026, 10, 2), -20000, categoria=mercado)
    novo_lancamento(db, nubank, date(2026, 10, 8), -5000, status="pendente", descricao="Conta de luz")
    cartao = novo_cartao(db, "Cartão Itaú", pagadora=nubank)
    criar_lancamento(db, conta=cartao, data=date(2026, 10, 2), descricao="Farmácia", valor_abs=3000,
                     tipo="despesa", efetivado=True)
    db.commit()


def test_dashboard_mostra_resumo(client, db):
    _cenario(db)
    texto = client.get("/").text
    assert "R$ 5.800,00" in texto        # saldo líquido atual: 1.000 + 5.000 - 200
    assert "R$ 5.720,00" in texto        # previsto mês: - 50 de luz pendente - 30 da fatura que vence em 10/10
    assert "R$ 5.000,00" in texto        # receitas
    assert "R$ 200,00" in texto          # despesas efetivadas: o cartão só conta quando a fatura for paga
    assert "R$ 280,00" in texto          # despesas previstas: 200 + 50 de luz + 30 da fatura que vence em 10/10
    assert "Conta de luz" in texto       # a pagar em 7 dias
    assert "Cartão Itaú" in texto
    assert '"Mercado"' in texto          # dados da rosca em JSON


def test_dashboard_aviso_de_backup(client, db):
    _cenario(db)
    set_config(db, "erro_backup", "05/10/2026 09:00 — pasta inacessível")
    db.commit()
    assert "pasta inacessível" in client.get("/").text


def test_dashboard_sem_lancamentos(client, db):
    nova_conta(db)
    db.commit()
    resposta = client.get("/?mes=2027-01")
    assert resposta.status_code == 200
    assert "Sem despesas neste mês" in resposta.text


def test_card_minhas_contas_com_saldo_atual_e_previsto(client, db):
    _cenario(db)
    texto = client.get("/").text
    inicio = texto.index("Minhas contas")
    card = texto[inicio:texto.index("Ver mais", inicio)]
    assert "Saldo atual" in card and "Saldo previsto" in card
    assert "R$ 5.800,00" in card and "R$ 5.720,00" in card
    assert "text-income" in card
    assert 'href="/contas?mes=' in texto[inicio:]


def test_card_cartoes_mostra_faturas_que_vencem_no_mes(client, db):
    _cenario(db)
    texto = client.get("/?mes=2026-10").text
    inicio = texto.index("Cartões de crédito")
    card = texto[inicio:]
    assert "Faturas que vencem em" in card
    assert "Pagar até sábado, 10 de outubro" in card and "em 5 dias" in card  # hoje 05/10, fatura dos R$ 30
    assert "R$ 30,00" in card
    assert ">Faturas abertas<" not in card
    assert 'href="/cartoes?mes=2026-10"' in card


def test_card_cartoes_segue_o_mes_escolhido(client, db):
    _cenario(db)
    card = client.get("/?mes=2026-11").text
    card = card[card.index("Cartões de crédito"):]
    assert "Pagar até terça, 10 de novembro" in card
    assert "Fecha em 3 de novembro" in card  # a de novembro ainda está aberta em 05/10


def test_resumo_cartao_soma_faturas_do_mes_escolhido(client, db):
    _cenario(db)
    outubro = client.get("/?mes=2026-10").text
    bloco = outubro[outubro.index('href="/cartoes?mes=2026-10" class='):]
    assert "R$ 30,00" in bloco[:bloco.index("</a>")] and "vencem em" in bloco  # fatura dos R$ 30 vence em 10/10
    novembro = client.get("/?mes=2026-11").text
    bloco = novembro[novembro.index('href="/cartoes?mes=2026-11" class='):]
    assert "R$ 0,00" in bloco[:bloco.index("</a>")]  # nada lançado na fatura que vence em 10/11


def test_novo_lancamento_com_conta_ja_escolhida(client, db):
    _cenario(db)
    from sqlalchemy import select

    from app.models import Conta
    cartao = db.scalars(select(Conta).where(Conta.tipo == "cartao")).one()
    texto = client.get(f"/lancamentos/novo?tipo=cartao&conta={cartao.id}").text
    assert f'<option value="{cartao.id}" selected>' in texto


def test_minhas_contas_separa_as_que_ficam_fora_do_saldo_liquido(client, db):
    nova_conta(db, "Nubank", saldo=100000)
    nova_conta(db, "Tesouro", tipo="investimento", saldo=250000, sl=False)
    nova_conta(db, "Reserva", saldo=30000, sl=False)
    db.commit()
    texto = client.get("/?mes=2026-10").text
    bloco = texto[texto.index("Minhas contas", texto.index('data-bloco="contas"')):]
    fora = bloco.index("Fora do saldo líquido")
    assert bloco.index("Nubank") < fora < bloco.index("Tesouro") and fora < bloco.index("Reserva")
    assert "R$ 2.800,00" in bloco[fora:]  # soma das que ficam de fora
