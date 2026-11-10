import json
import re
from datetime import date

from app.models import Meta
from app.services.orcamento import definir_limite
from app.services.faturas import obter_ou_criar_fatura
from tests.factories import nova_conta, novo_cartao, novo_lancamento


def test_resumo_renderiza_cartao_e_botoes(client, db):
    conta = nova_conta(db, "Nubank", saldo=100_000)
    novo_lancamento(db, conta, date(2026, 10, 2), -20_000, descricao="Mercado")
    definir_limite(db, "2026-10", None, 100_000)
    db.commit()
    r = client.get("/resumo?mes=2026-10")
    assert r.status_code == 200
    texto = r.text
    assert 'id="boletim"' in texto and "Casa · Outubro 2026" in texto
    assert "Gastos vs. receita" in texto and "Copiar imagem" in texto and "Baixar imagem" in texto and "Copiar texto" in texto
    assert "/static/vendor/html-to-image.js" in texto
    embutido = json.loads(re.search(r"const TEXTO_WHATSAPP = (.*);\s*$", texto, re.M).group(1))
    assert "🏠 *Casa · Outubro 2026*" in embutido     # texto do WhatsApp embutido para o botão
    assert 'aria-label="Principal"' not in texto      # sem o menu lateral


def test_resumo_mostra_gastos_vs_receita_e_cartoes(client, db):
    conta = nova_conta(db, "Nubank", saldo=100_000)
    novo_lancamento(db, conta, date(2026, 10, 1), 500_000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 2), -20_000, descricao="Mercado")
    cartao = novo_cartao(db, "Itaú", pagadora=conta, fechamento=3, vencimento=10)
    f = obter_ou_criar_fatura(db, cartao, "2026-10")
    novo_lancamento(db, conta, date(2026, 10, 2), -30_000, fatura_id=f.id)
    db.commit()
    html = client.get("/resumo?mes=2026-10").text
    assert "Gastos vs. receita" in html and "Sobram" in html and "Cartões no mês" in html


def test_resumo_mes_invalido_cai_no_atual(client, db):
    nova_conta(db, "Nubank")
    db.commit()
    assert client.get("/resumo?mes=xx").status_code == 200


def test_dashboard_tem_atalho_para_o_resumo(client, db):
    nova_conta(db, "Nubank")
    db.commit()
    assert 'href="/resumo?mes=2026-10"' in client.get("/?mes=2026-10").text


def test_resumo_texto_embutido_sem_escape_html(client, db):
    conta = nova_conta(db, "Nubank", saldo=100_000)
    novo_cartao(db, "Água & Luz d'água", pagadora=conta)
    db.commit()
    html = client.get("/resumo?mes=2026-10").text
    m = re.search(r"const TEXTO_WHATSAPP = (.*);\s*$", html, re.M)
    texto = json.loads(m.group(1))                    # como o JS lê
    assert "Água & Luz d'água" in texto and "&amp;" not in texto and "&#39;" not in texto


def test_resumo_mes_passado_acima_do_teto(client, db):
    conta = nova_conta(db, "Nubank", saldo=100_000)
    novo_lancamento(db, conta, date(2026, 9, 5), -150_000, descricao="Obra")
    definir_limite(db, "2026-09", None, 100_000)
    db.commit()
    html = client.get("/resumo?mes=2026-09").text
    assert "Fechamos o mês" in html and "acima do limite" in html and "Passamos do limite" not in html


def test_resumo_falta_com_reserva_e_outras_caixinhas_no_cartao(client, db):
    conta = nova_conta(db, "Nubank", saldo=100_000)
    nova_conta(db, "Ações", tipo="investimento", saldo=200_000, sl=False)
    db.add(Meta(nome="Reserva", tipo="reserva_emergencia", valor_alvo=1_200_000, valor_manual=600_000))
    novo_lancamento(db, conta, date(2026, 10, 1), 100_000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 2), -160_000, descricao="Obra")
    db.commit()
    html = client.get("/resumo?mes=2026-10").text
    assert "Faltam" in html and "sai da reserva" in html and "(10% da reserva de R$ 6.000,00)" in html
    assert "Reserva de emergência" in html and "Investimentos e outras caixinhas" in html
    assert "Tudo em dia" not in html and "Sobram" not in html


def test_resumo_sem_falta_pagar_e_cartoes_numa_linha_so(client, db):
    conta = nova_conta(db, "Nubank", saldo=100_000)
    novo_lancamento(db, conta, date(2026, 10, 20), -12_000, status="pendente", descricao="Internet")
    cartao = novo_cartao(db, "Itaú", pagadora=conta, fechamento=3, vencimento=10)
    f = obter_ou_criar_fatura(db, cartao, "2026-10")
    novo_lancamento(db, conta, date(2026, 10, 2), -30_000, fatura_id=f.id)
    db.commit()
    html = client.get("/resumo?mes=2026-10").text
    assert "Falta pagar" not in html and "Cartões no mês" in html
    assert html.count("Cartões no mês") == 1


def test_resumo_cores_do_saldo_previsto_positivo_e_negativo(client, db):
    conta = nova_conta(db, "Nubank", saldo=100_000)
    db.commit()
    verde = client.get("/resumo?mes=2026-10").text
    assert re.search(r"tabular-nums text-income\">R\$ 1\.000,00", verde)
    novo_lancamento(db, conta, date(2026, 10, 20), -300_000, status="pendente", descricao="Obra")
    db.commit()
    vermelho = client.get("/resumo?mes=2026-10").text
    assert re.search(r"tabular-nums text-expense\">-?R\$ ?-?2\.000,00|text-expense\">[^<]*2\.000,00", vermelho)


def test_resumo_mostra_o_limite_em_linguagem_clara(client, db):
    conta = nova_conta(db, "Nubank", saldo=100_000)
    novo_lancamento(db, conta, date(2026, 10, 2), -50_000, descricao="Obra")
    definir_limite(db, "2026-10", None, 400_000)
    db.commit()
    html = client.get("/resumo?mes=2026-10").text
    for frase in ("Limite de gastos do mês", "Gastamos", "Só de parcelas", "Ainda dá para gastar"):
        assert frase in html, frase
    assert "Guardado" not in html and "Retirado das metas" not in html and "usado" not in html


def test_resumo_limite_estourado_e_automatico(client, db):
    conta = nova_conta(db, "Nubank", saldo=100_000)
    novo_lancamento(db, conta, date(2026, 10, 1), 100_000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 10, 2), -150_000, descricao="Obra")
    db.commit()
    html = client.get("/resumo?mes=2026-10").text
    assert "Passamos do limite em" in html and "calculado a partir da receita" in html
