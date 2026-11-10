from datetime import date

from sqlalchemy import select

from app.models import Conta, Fatura, Lancamento
from app.services.lancamentos import criar_lancamento
from tests.factories import nova_conta, novo_cartao, novo_lancamento


def test_tela_contas_mostra_saldos(client, db):
    conta = nova_conta(db, "Nubank", saldo=100000)
    novo_lancamento(db, conta, date(2026, 10, 20), -25000, status="pendente")
    db.commit()
    texto = client.get("/contas").text
    assert "Nubank" in texto
    assert "R$ 1.000,00" in texto
    assert "R$ 750,00" in texto


def test_ajustar_saldo(client, db):
    conta = nova_conta(db, "NuInvest", tipo="investimento", saldo=100000)
    db.commit()
    resposta = client.post(f"/contas/{conta.id}/ajustar", data={"novo_saldo": "1.007,50", "data": "2026-10-05"},
                           follow_redirects=False)
    assert resposta.status_code == 303
    ajuste = db.scalars(select(Lancamento).where(Lancamento.tipo == "ajuste")).one()
    assert ajuste.valor == 750


def test_nova_conta_e_nome_duplicado(client, db):
    nova_conta(db, "Nubank")
    db.commit()
    dados = {"nome": "Itaú", "tipo": "corrente", "sl": "on", "saldo": "10,00", "data": "2026-10-05"}
    assert client.post("/contas", data=dados, follow_redirects=False).status_code == 303
    itau = db.scalars(select(Conta).where(Conta.nome == "Itaú")).one()
    assert (itau.saldo_inicial, itau.entra_saldo_liquido, itau.data_saldo_inicial) == (1000, True, date(2026, 10, 5))

    resposta = client.post("/contas", data={**dados, "nome": "Nubank"})
    assert resposta.status_code == 400
    assert "já existe" in resposta.text


def test_arquivar_conta(client, db):
    conta = nova_conta(db, "Antiga")
    nova_conta(db, "Nubank")
    db.commit()
    client.post(f"/contas/{conta.id}/arquivar")
    db.refresh(conta)
    assert conta.ativa is False


def _cartao_com_compra(db):
    nubank = nova_conta(db, "Nubank", saldo=500000)
    cartao = novo_cartao(db, "Cartão Itaú", pagadora=nubank, fechamento=3, vencimento=10, limite=1_000_000)
    criar_lancamento(db, conta=cartao, data=date(2026, 10, 2), descricao="Supermercado", valor_abs=30000,
                     tipo="despesa", efetivado=True)
    db.commit()
    return nubank, cartao


def test_lista_de_cartoes(client, db):
    _cartao_com_compra(db)
    texto = client.get("/cartoes").text
    assert "Cartão Itaú" in texto
    assert "R$ 9.700,00" in texto


def test_detalhe_da_fatura(client, db):
    _, cartao = _cartao_com_compra(db)
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10").text
    assert "Supermercado" in texto
    assert "R$ 300,00" in texto
    assert "10/10/2026" in texto
    assert "fatura=2026-09" in texto and "fatura=2026-11" in texto


def test_pagar_fatura(client, db):
    nubank, cartao = _cartao_com_compra(db)
    resposta = client.post(f"/cartoes/{cartao.id}/faturas/2026-10/pagar", data={
        "valor": "300,00", "data": "2026-10-10", "conta_origem_id": str(nubank.id), "efetivado": "on",
    }, follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == f"/cartoes/{cartao.id}?fatura=2026-10"
    fatura = db.scalars(select(Fatura)).one()
    db.refresh(fatura)
    assert fatura.status == "paga"


def test_novo_cartao(client, db):
    nubank = nova_conta(db, "Nubank")
    db.commit()
    resposta = client.post("/cartoes", data={
        "nome": "Cartão MP", "limite": "2.000,00", "fechamento": "28", "vencimento": "5",
        "pagadora_id": str(nubank.id),
    }, follow_redirects=False)
    assert resposta.status_code == 303
    cartao = db.scalars(select(Conta).where(Conta.nome == "Cartão MP")).one()
    assert (cartao.tipo, cartao.limite, cartao.dia_fechamento, cartao.dia_vencimento) == ("cartao", 200000, 28, 5)


def test_saldos_negativos_ficam_vermelhos_na_tela_de_contas(client, db):
    import re

    from tests.factories import nova_conta as _conta
    _conta(db, "Conta no vermelho", saldo=-500000)
    db.commit()
    html = client.get("/contas?mes=2026-10").text
    for rotulo in ("Saldo líquido atual", "Previsto hoje", "Previsto fim do mês"):
        bloco = re.search(re.escape(rotulo) + r'</div><div class="([^"]*)">', html)
        assert bloco and "text-expense" in bloco.group(1), rotulo
