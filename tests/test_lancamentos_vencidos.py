from datetime import date

from app.services.consulta_lancamentos import resumo_vencidos
from app.services.faturas import obter_ou_criar_fatura
from app.services.lancamentos import criar_transferencia
from tests.factories import nova_conta, novo_cartao, novo_lancamento

HOJE = date(2026, 10, 5)  # a data fixa dos testes de rota


def test_resumo_conta_so_pendente_ja_vencido_em_qualquer_mes(db):
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 8, 20), -30000, status="pendente", descricao="Antigo")
    novo_lancamento(db, conta, date(2026, 10, 4), 50000, status="pendente", descricao="Receber")
    novo_lancamento(db, conta, date(2026, 10, 5), -1000, status="pendente", descricao="Hoje nao vence")
    novo_lancamento(db, conta, date(2026, 10, 1), -2000, status="efetivado", descricao="Pago")
    novo_lancamento(db, conta, date(2026, 9, 1), -4000, status="pendente", descricao="Ignorado", ignorar=True)
    r = resumo_vencidos(db, HOJE)
    assert (r.qtd, r.despesas, r.receitas, r.mais_antigo) == (2, 30000, 50000, date(2026, 8, 20))
    assert r.competencia_mais_antiga == "2026-08" and r.tem


def test_transferencia_vencida_conta_uma_vez_e_nao_vira_despesa(db):
    a, b = nova_conta(db, "A"), nova_conta(db, "B")
    criar_transferencia(db, origem=a, destino=b, data=date(2026, 9, 10), valor_abs=5000)
    r = resumo_vencidos(db, HOJE)
    assert (r.qtd, r.despesas, r.receitas) == (1, 0, 0)


def test_fatura_vencida_nao_paga_entra_no_resumo(db):
    conta = nova_conta(db, "Nubank")
    cartao = novo_cartao(db, "Cartão", pagadora=conta, fechamento=3, vencimento=10)
    fatura = obter_ou_criar_fatura(db, cartao, "2026-09")
    novo_lancamento(db, cartao, date(2026, 8, 20), -10000, descricao="Compra", fatura_id=fatura.id)
    db.commit()
    r = resumo_vencidos(db, date(2026, 10, 20))
    assert (r.faturas, r.valor_faturas) == (1, 10000) and r.tem and r.qtd == 0


def test_sem_vencidos_nao_mostra_alerta(client, db):
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 10, 20), -1000, status="pendente")
    db.commit()
    assert 'role="alert"' not in client.get("/lancamentos?mes=2026-10").text


def test_alerta_e_destaque_na_linha_vencida(client, db):
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 9, 28), -30000, status="pendente", descricao="Conta atrasada")
    novo_lancamento(db, conta, date(2026, 10, 20), -1000, status="pendente", descricao="Conta futura")
    db.commit()
    texto = client.get("/lancamentos?mes=2026-10").text
    assert "1 lançamento vencido" in texto and "o mais antigo é de 28/09/2026" in texto
    assert "mes=2026-09&status=pendente" in texto  # o link leva ao mês do mais antigo
    # em outubro só a futura aparece, sem a marca de vencida
    assert ">vencido<" not in texto
    setembro = client.get("/lancamentos?mes=2026-09").text
    assert "Conta atrasada" in setembro and ">vencido<" in setembro
    assert "text-expense font-medium" in setembro


def test_pagina_mostra_pendentes_antes_dos_efetivados_em_data_crescente(client, db):
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 10, 9), -1000, status="pendente", descricao="Pend B")
    novo_lancamento(db, conta, date(2026, 10, 7), -1000, status="pendente", descricao="Pend A")
    novo_lancamento(db, conta, date(2026, 10, 3), -1000, descricao="Pago B")
    novo_lancamento(db, conta, date(2026, 10, 2), -1000, descricao="Pago A")
    db.commit()
    texto = client.get("/lancamentos?mes=2026-10").text
    ordem = [texto.index(f">{n}<") for n in ("Pend A", "Pend B", "Pago A", "Pago B")]
    assert ordem == sorted(ordem)
