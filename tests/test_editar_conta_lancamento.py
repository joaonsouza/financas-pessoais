"""Trocar a conta de um lançamento pela edição (receita/despesa de conta corrente, poupança etc.)."""
import re
from datetime import date

from sqlalchemy import select

from decimal import Decimal

from app.models import Ativo, Caixinha, Lancamento, OperacaoInvest, Recorrencia
from app.services.lancamentos import criar_lancamento, criar_transferencia
from app.services.recorrencia import gerar_pendentes
from tests.factories import nova_conta, novo_cartao


def _form(lanc, conta_id, **extra):
    inteiro, frac = divmod(abs(lanc.valor), 100)
    return {"valor": f"{inteiro},{frac:02d}", "descricao": lanc.descricao, "data": lanc.data.isoformat(),
            "conta_id": str(conta_id), **extra}


def test_form_de_edicao_libera_a_conta_de_despesa(client, db):
    itau = nova_conta(db, "Itaú")
    lanc = criar_lancamento(db, conta=itau, data=date(2026, 10, 3), descricao="Pão", valor_abs=500, tipo="despesa")
    db.commit()

    html = client.get(f"/lancamentos/{lanc.id}/editar").text

    select_conta = re.search(r'<select name="conta_id"[^>]*>', html).group(0)
    assert "disabled" not in select_conta
    assert '<input type="hidden" name="conta_id"' not in html


def test_trocar_conta_de_despesa(client, db):
    itau, nubank = nova_conta(db, "Itaú"), nova_conta(db, "Nubank")
    lanc = criar_lancamento(db, conta=itau, data=date(2026, 10, 3), descricao="Pão", valor_abs=500, tipo="despesa")
    db.commit()

    resposta = client.post(f"/lancamentos/{lanc.id}", data=_form(lanc, nubank.id))

    assert resposta.headers["HX-Refresh"] == "true"
    db.refresh(lanc)
    assert (lanc.conta_id, lanc.valor) == (nubank.id, -500)


def test_trocar_conta_tira_a_caixinha_da_conta_antiga(client, db):
    itau, nubank = nova_conta(db, "Itaú"), nova_conta(db, "Nubank")
    viagem = Caixinha(nome="Viagem", conta_id=itau.id)
    db.add(viagem)
    db.flush()
    lanc = criar_lancamento(db, conta=itau, data=date(2026, 10, 3), descricao="Hotel", valor_abs=500, tipo="despesa")
    lanc.caixinha_id = viagem.id
    db.commit()

    client.post(f"/lancamentos/{lanc.id}", data=_form(lanc, nubank.id))

    db.refresh(lanc)
    assert (lanc.conta_id, lanc.caixinha_id) == (nubank.id, None)


def test_transferencia_continua_com_a_conta_travada(client, db):
    itau, nubank = nova_conta(db, "Itaú"), nova_conta(db, "Nubank")
    saida, _ = criar_transferencia(db, origem=itau, destino=nubank, data=date(2026, 10, 3), descricao="Pix",
                                   valor_abs=500)
    db.commit()

    html = client.get(f"/lancamentos/{saida.id}/editar").text

    assert "disabled" in re.search(r'<select name="conta_id"[^>]*>', html).group(0)


def test_compra_no_cartao_nao_troca_de_conta(client, db):
    itau = nova_conta(db, "Itaú")
    cartao = novo_cartao(db, pagadora=itau)
    lanc = criar_lancamento(db, conta=cartao, data=date(2026, 10, 3), descricao="Livro", valor_abs=500,
                            tipo="despesa")
    db.commit()

    resposta = client.post(f"/lancamentos/{lanc.id}", data=_form(lanc, itau.id))

    assert resposta.status_code == 422
    db.refresh(lanc)
    assert lanc.conta_id == cartao.id


def test_conta_de_outra_moeda_recusa(client, db):
    itau = nova_conta(db, "Itaú")
    nomad = nova_conta(db, "Nomad")
    nomad.moeda = "USD"
    lanc = criar_lancamento(db, conta=itau, data=date(2026, 10, 3), descricao="Pão", valor_abs=500, tipo="despesa")
    db.commit()

    resposta = client.post(f"/lancamentos/{lanc.id}", data=_form(lanc, nomad.id))

    assert resposta.status_code == 422
    assert "moeda" in resposta.text
    db.refresh(lanc)
    assert lanc.conta_id == itau.id


def test_lancamento_de_investimento_nao_troca_de_conta(client, db):
    itau, nubank = nova_conta(db, "Itaú"), nova_conta(db, "Nubank")
    lanc = criar_lancamento(db, conta=itau, data=date(2026, 10, 3), descricao="Rendimento", valor_abs=500,
                            tipo="receita")
    db.flush()
    usdc = Ativo(classe="cripto", codigo="USDC", simbolo_cotacao="USDC-USD", alocacao="cripto")
    db.add(usdc)
    db.flush()
    db.add(OperacaoInvest(ativo_id=usdc.id, lancamento_id=lanc.id, data=lanc.data, tipo="rendimento",
                          quantidade=Decimal("5"), preco_unitario=100))
    db.commit()

    resposta = client.post(f"/lancamentos/{lanc.id}", data=_form(lanc, nubank.id))

    assert resposta.status_code == 422
    db.refresh(lanc)
    assert lanc.conta_id == itau.id


def _aluguel(db, conta):
    rec = Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=conta.id, valor=10000, dia=10, inicio="2026-10")
    db.add(rec)
    db.flush()
    gerar_pendentes(db, date(2026, 10, 5))
    db.commit()
    return rec, list(db.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id)
                                .order_by(Lancamento.competencia_recorrencia)))


def _contas(db, rec_id):
    db.expire_all()
    return [l.conta_id for l in db.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec_id)
                                           .order_by(Lancamento.competencia_recorrencia))]


def test_trocar_conta_so_deste_da_serie(client, db):
    itau, nubank = nova_conta(db, "Itaú"), nova_conta(db, "Nubank")
    rec, ocorrencias = _aluguel(db, itau)

    client.post(f"/lancamentos/{ocorrencias[1].id}", data=_form(ocorrencias[1], nubank.id, escopo="este"))

    assert _contas(db, rec.id) == [itau.id, nubank.id, itau.id]
    assert db.get(Recorrencia, rec.id).conta_id == itau.id


def test_trocar_conta_deste_e_dos_proximos(client, db):
    itau, nubank = nova_conta(db, "Itaú"), nova_conta(db, "Nubank")
    rec, ocorrencias = _aluguel(db, itau)

    client.post(f"/lancamentos/{ocorrencias[1].id}", data=_form(ocorrencias[1], nubank.id, escopo="proximos"))

    assert _contas(db, rec.id) == [itau.id, nubank.id, nubank.id]
    assert db.get(Recorrencia, rec.id).conta_id == nubank.id
