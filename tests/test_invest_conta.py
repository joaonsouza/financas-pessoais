"""Operação de investimento ligada a uma conta: transferência conta ↔ conta de investimento, com caixinhas."""
import re

import pytest

from datetime import date
from decimal import Decimal as D

from sqlalchemy import select

from app.models import Ativo, Lancamento, OperacaoInvest
from app.services.caixinhas import criar_caixinha, saldo_caixinha
from app.services.lancamentos import excluir_lancamento
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta
from tests.test_rotas_investimentos import mercado, op_form  # noqa: F401
from tests.test_mercado_provedores import sem_rede_real  # noqa: F401


def contas(db):
    corrente = nova_conta(db, "Itaú", saldo=1_000_000, inicio=date(2026, 1, 1))
    corretora = nova_conta(db, "NuInvest", tipo="investimento", sl=False, inicio=date(2026, 1, 1))
    db.flush()
    bolsa = criar_caixinha(db, corretora, "Bolsa de Valores", HOJE_TESTE)
    db.commit()
    return corrente, corretora, bolsa


def postar(client, **kw):
    r = client.post("/investimentos/operacoes", data=op_form(**kw))
    assert r.status_code == 200, r.text
    return r


def transferencias(db):
    return {l.conta_id: l for l in db.scalars(select(Lancamento).where(Lancamento.tipo == "transferencia"))}


def test_compra_debita_a_conta_e_entra_na_caixinha_da_corretora(client, db, mercado):  # noqa: F811
    corrente, corretora, bolsa = contas(db)
    postar(client, taxas="5,00", conta_id=str(corrente.id), custodia_conta_id=str(corretora.id),
           custodia_caixinha_id=str(bolsa.id))                                   # 100 x 30 + 5
    pernas = transferencias(db)
    assert pernas[corrente.id].valor == -300500 and pernas[corretora.id].valor == 300500
    assert pernas[corretora.id].caixinha_id == bolsa.id and pernas[corrente.id].caixinha_id is None
    assert pernas[corrente.id].descricao == "Compra PETR4" and pernas[corrente.id].status == "efetivado"
    op = db.scalars(select(OperacaoInvest)).one()
    assert op.lancamento_id == pernas[corrente.id].id
    assert saldo_caixinha(db, bolsa) == 300500
    assert db.get(Ativo, op.ativo_id).corretora_conta_id == corretora.id  # vira a corretora do ativo


def test_venda_credita_a_conta_saindo_da_caixinha(client, db, mercado):  # noqa: F811
    corrente, corretora, bolsa = contas(db)
    postar(client)
    postar(client, ativo_id="1", tipo="venda", quantidade="10", preco="40,00", taxas="2,00",
           conta_id=str(corrente.id), custodia_conta_id=str(corretora.id), custodia_caixinha_id=str(bolsa.id))
    pernas = transferencias(db)
    assert pernas[corrente.id].valor == 39800 and pernas[corretora.id].valor == -39800
    assert pernas[corretora.id].caixinha_id == bolsa.id and pernas[corrente.id].descricao == "Venda PETR4"


def test_sem_conta_nao_movimenta_e_conta_sem_destino_da_erro(client, db, mercado):  # noqa: F811
    corrente, _, _ = contas(db)
    postar(client)
    assert transferencias(db) == {}
    r = client.post("/investimentos/operacoes", data=op_form(conta_id=str(corrente.id)))
    assert r.status_code == 422, r.text
    assert "onde o investimento fica" in r.text, re.findall(r'role="alert".{0,300}', r.text, re.S)
    assert db.scalars(select(OperacaoInvest)).all().__len__() == 1


def test_caixinha_de_outra_conta_e_recusada(client, db, mercado):  # noqa: F811
    corrente, corretora, bolsa = contas(db)
    r = client.post("/investimentos/operacoes", data=op_form(conta_id=str(corrente.id), custodia_conta_id=str(corretora.id),
                                                             caixinha_conta_id=str(bolsa.id)))
    assert r.status_code == 422 and "Bolsa de Valores é de outra conta" in r.text


def test_editar_refaz_e_excluir_remove_a_transferencia(client, db, mercado):  # noqa: F811
    corrente, corretora, bolsa = contas(db)
    vinc = dict(conta_id=str(corrente.id), custodia_conta_id=str(corretora.id), custodia_caixinha_id=str(bolsa.id))
    postar(client, **vinc)
    form = client.get("/investimentos/operacoes/1/editar").text
    assert f'conta: "{corrente.id}"' in form.replace("&#34;", '"') and f'cxCustodia: "{bolsa.id}"' in form.replace("&#34;", '"')
    r = client.post("/investimentos/operacoes/1", data=op_form(ativo_id="1", preco="31,00", **vinc))
    assert r.status_code == 200
    db.expire_all()
    assert len(transferencias(db)) == 2 and transferencias(db)[corrente.id].valor == -310000
    client.post("/investimentos/operacoes/1", data=op_form(ativo_id="1", preco="31,00"))  # tira a conta
    db.expire_all()
    assert transferencias(db) == {}
    client.post("/investimentos/operacoes/1", data=op_form(ativo_id="1", preco="31,00", **vinc))
    client.post("/investimentos/operacoes/1/excluir")
    db.expire_all()
    assert transferencias(db) == {} and db.scalars(select(OperacaoInvest)).all() == []


def test_excluir_a_transferencia_mantem_a_operacao_sem_vinculo(client, db, mercado):  # noqa: F811
    corrente, corretora, _ = contas(db)
    postar(client, conta_id=str(corrente.id), custodia_conta_id=str(corretora.id))
    excluir_lancamento(db, transferencias(db)[corretora.id])
    db.commit()
    assert db.scalars(select(OperacaoInvest)).one().lancamento_id is None


def test_nova_operacao_do_ativo_sugere_a_ultima_conta(client, db, mercado):  # noqa: F811
    corrente, corretora, bolsa = contas(db)
    postar(client, conta_id=str(corrente.id), custodia_conta_id=str(corretora.id), custodia_caixinha_id=str(bolsa.id))
    texto = client.get("/investimentos/operacoes/nova?ativo_id=1").text.replace("&#34;", '"')
    assert f'conta: "{corrente.id}"' in texto and f'custodia: "{corretora.id}"' in texto
    assert "Bolsa de Valores" in texto


def test_operacao_futura_fica_pendente(client, db, mercado):  # noqa: F811
    corrente, corretora, _ = contas(db)
    futuro = date(HOJE_TESTE.year + 1, 1, 5).isoformat()
    postar(client, data=futuro, conta_id=str(corrente.id), custodia_conta_id=str(corretora.id))
    assert transferencias(db)[corrente.id].status == "pendente"


def test_custodia_sem_pagamento_e_trocada_pela_lista(client, db, mercado):  # noqa: F811
    corrente, corretora, bolsa = contas(db)
    postar(client, custodia_conta_id=str(corretora.id), custodia_caixinha_id=str(bolsa.id))
    op = db.scalars(select(OperacaoInvest)).one()
    assert (op.custodia_conta_id, op.custodia_caixinha_id, op.lancamento_id) == (corretora.id, bolsa.id, None)
    assert transferencias(db) == {}
    texto = client.get("/investimentos/lancamentos?local=sem").text
    assert "Nenhum lançamento com esses filtros" in texto
    r = client.post(f"/investimentos/operacoes/{op.id}/custodia", data={"custodia": f"{corretora.id}:"})
    assert r.status_code == 204
    db.expire_all()
    assert (op.custodia_conta_id, op.custodia_caixinha_id) == (corretora.id, None)
    client.post(f"/investimentos/operacoes/{op.id}/custodia", data={"custodia": ""})
    db.expire_all()
    assert op.custodia_conta_id is None
    assert "PETR4" in client.get("/investimentos/lancamentos?local=sem").text


def test_trocar_custodia_move_a_perna_da_transferencia(client, db, mercado):  # noqa: F811
    corrente, corretora, bolsa = contas(db)
    outra = nova_conta(db, "C6 Invest", tipo="investimento", sl=False, inicio=date(2026, 1, 1))
    db.commit()
    postar(client, conta_id=str(corrente.id), custodia_conta_id=str(corretora.id), custodia_caixinha_id=str(bolsa.id))
    op = db.scalars(select(OperacaoInvest)).one()
    assert client.post(f"/investimentos/operacoes/{op.id}/custodia", data={"custodia": ""}).status_code == 422
    client.post(f"/investimentos/operacoes/{op.id}/custodia", data={"custodia": f"{outra.id}:"})
    db.expire_all()
    pernas = transferencias(db)
    assert set(pernas) == {corrente.id, outra.id} and pernas[outra.id].caixinha_id is None


def test_custodia_precisa_ser_conta_de_investimento(client, db, mercado):  # noqa: F811
    corrente, _, _ = contas(db)
    r = client.post("/investimentos/operacoes", data=op_form(custodia_conta_id=str(corrente.id)))
    assert r.status_code == 422 and "conta de investimento" in r.text


def test_quantidade_por_local():
    from types import SimpleNamespace as O
    from app.services.invest_conta import quantidade_por_local
    ops = [O(tipo="compra", quantidade=D(10), preco_unitario=100, custodia_conta_id=1, custodia_caixinha_id=7),
           O(tipo="compra", quantidade=D(5), preco_unitario=100, custodia_conta_id=2, custodia_caixinha_id=None),
           O(tipo="venda", quantidade=D(4), preco_unitario=120, custodia_conta_id=1, custodia_caixinha_id=7),
           O(tipo="compra", quantidade=D(1), preco_unitario=100, custodia_conta_id=None, custodia_caixinha_id=None)]
    assert quantidade_por_local(ops) == {(1, 7): D(6), (2, None): D(5), None: D(1)}


# ---------- conciliação na página de contas ----------

def test_conciliar_iguala_a_caixinha_ao_valor_das_posicoes(client, db, mercado):  # noqa: F811
    from app.models import CotacaoCache
    from datetime import datetime
    corrente, corretora, bolsa = contas(db)
    postar(client, conta_id=str(corrente.id), custodia_conta_id=str(corretora.id),
           custodia_caixinha_id=str(bolsa.id))                                   # 100 x 30: caixinha com 3.000
    db.add(CotacaoCache(simbolo="PETR4.SA", data=HOJE_TESTE, fechamento=D("35"), moeda="BRL", fonte="yahoo",
                        atualizado_em=datetime(2026, 10, 5, 10)))
    db.commit()
    texto = client.get("/contas").text
    assert "Conciliar investimentos" in texto and "R$ 3.500,00" in texto and "+R$ 500,00" in texto
    r = client.post("/contas/conciliar-investimentos", data={"caixinha_id": str(bolsa.id)}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert saldo_caixinha(db, bolsa) == 350000
    ajuste = db.scalars(select(Lancamento).where(Lancamento.tipo == "ajuste")).one()
    assert ajuste.descricao == "Rendimento investimentos: Bolsa de Valores" and ajuste.valor == 50000
    assert "Em dia" in client.get("/contas").text
    client.post("/contas/conciliar-investimentos")  # nada a fazer: não cria outro ajuste
    db.expire_all()
    assert len(db.scalars(select(Lancamento).where(Lancamento.tipo == "ajuste")).all()) == 1


def test_posicao_fora_de_caixinha_aparece_sem_conciliar(client, db, mercado):  # noqa: F811
    _, corretora, _ = contas(db)
    postar(client, custodia_conta_id=str(corretora.id))
    texto = client.get("/contas").text
    assert "Conciliar investimentos" in texto and "sem caixinha" in texto and "Conciliar todas" not in texto


def test_excluir_ativo_apaga_operacoes_transferencias_e_metas(client, db, mercado):
    from app.models.investimento import MetaAtivoCarteira
    from app.services.invest_carteira import garantir_carteira_padrao
    corrente, corretora, _ = contas(db)
    postar(client, conta_id=str(corrente.id), custodia_conta_id=str(corretora.id))
    postar(client, ativo_id="1", tipo="venda", quantidade="40", preco="35,00", conta_id=str(corrente.id),
           custodia_conta_id=str(corretora.id))
    db.add(MetaAtivoCarteira(carteira_id=garantir_carteira_padrao(db).id, ativo_id=1, meta_pct=D("100")))
    db.commit()
    assert db.scalars(select(Lancamento).where(Lancamento.tipo == "transferencia")).all()
    r = client.post("/investimentos/ativos/1/excluir", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/investimentos"
    db.expire_all()
    assert db.get(Ativo, 1) is None
    assert db.scalars(select(OperacaoInvest)).all() == []
    assert db.scalars(select(MetaAtivoCarteira)).all() == []
    assert db.scalars(select(Lancamento).where(Lancamento.tipo == "transferencia")).all() == []


def test_tela_do_ativo_oferece_encerrar_com_a_posicao_inteira(client, db, mercado):
    corrente, corretora, _ = contas(db)
    postar(client, quantidade="100", custodia_conta_id=str(corretora.id))
    texto = client.get("/investimentos/ativos/1").text
    assert "/investimentos/operacoes/nova?" in texto and "tipo=venda" in texto and "quantidade=100" in texto
    assert 'action="/investimentos/ativos/1/excluir"' in texto


@pytest.mark.parametrize("extra", [
    {},  # ação: venda da quantidade inteira
    {"classe": "renda_fixa", "codigo": "CDB X", "tipo": "aplicacao", "preco": "1.000,00", "indexador": "cdi",
     "taxa": "100"},  # CDB: resgate do valor atualizado
])
def test_encerrar_pelo_link_zera_a_posicao_e_tira_da_lista(client, db, mercado, extra):
    from html import unescape
    from urllib.parse import parse_qsl, urlsplit
    from app.services.invest_carteira import resumo_carteira
    contas(db)
    postar(client, **extra)
    client.post("/investimentos/atualizar")  # cotação e CDI do dia
    texto = client.get("/investimentos/ativos/1").text
    link = unescape(re.search(r'hx-get="(/investimentos/operacoes/nova\?[^"]*tipo=[^"]+)"', texto)[1])
    params = dict(parse_qsl(urlsplit(link).query, keep_blank_values=True))
    assert params["preco"]
    postar(client, ativo_id="1", tipo=params["tipo"], quantidade=params["quantidade"], preco=params["preco"],
           data=HOJE_TESTE.isoformat())
    db.expire_all()
    assert [a.ativo.codigo for a in resumo_carteira(db, HOJE_TESTE).ativos] == []


def test_venda_na_bolsa_cai_na_conta_em_d_mais_2_uteis_pendente(client, db, mercado):  # noqa: F811
    """Ação/FII/ETF: a liquidação é D+2 úteis. Venda na sexta 02/10 (hoje é segunda 05/10) cai na terça 06/10,
    pendente; venda na terça 29/09 já liquidou na quinta 01/10."""
    corrente, corretora, bolsa = contas(db)
    vinculo = dict(conta_id=str(corrente.id), custodia_conta_id=str(corretora.id), custodia_caixinha_id=str(bolsa.id))
    postar(client, classe="etf", codigo="HASH11", simbolo="HASH11.SA", **vinculo)  # 100 x 30 na caixinha
    postar(client, ativo_id="1", tipo="venda", data="2026-10-02", quantidade="10", preco="40,00", **vinculo)
    venda_na_conta = lambda: next(l for l in db.scalars(select(Lancamento).where(  # noqa: E731
        Lancamento.conta_id == corrente.id, Lancamento.descricao == "Venda HASH11")))
    perna = venda_na_conta()
    assert perna.data == date(2026, 10, 6) and perna.status == "pendente"
    # na corretora o dinheiro sai no dia da venda: a caixinha bate com as posições (conciliação) desde já
    custodia = next(l for l in db.scalars(select(Lancamento).where(
        Lancamento.conta_id == corretora.id, Lancamento.descricao == "Venda HASH11")))
    assert custodia.data == date(2026, 10, 2) and custodia.status == "efetivado" and custodia.valor == -40000
    assert saldo_caixinha(db, bolsa) == 300000 - 40000
    assert db.scalars(select(OperacaoInvest).where(OperacaoInvest.tipo == "venda")).one().data == date(2026, 10, 2)
    excluir_lancamento(db, perna)
    db.commit()
    postar(client, ativo_id="1", tipo="venda", data="2026-09-29", quantidade="10", preco="40,00", **vinculo)
    perna = venda_na_conta()
    assert perna.data == date(2026, 10, 1) and perna.status == "efetivado"


def test_venda_de_cripto_cai_no_mesmo_dia(client, db, mercado):  # noqa: F811
    corrente, corretora, _ = contas(db)
    postar(client, classe="cripto", codigo="BTC", simbolo="BTC-USD", quantidade="1", preco="100,00")
    postar(client, ativo_id="1", tipo="venda", data="2026-10-02", quantidade="1", preco="100,00",
           conta_id=str(corrente.id), custodia_conta_id=str(corretora.id))
    perna = transferencias(db)[corrente.id]
    assert perna.data == date(2026, 10, 2) and perna.status == "efetivado"


def test_data_que_cai_na_conta_escolhida_no_formulario_vale_e_fica_na_edicao(client, db, mercado):  # noqa: F811
    """Feriado: a data sugerida (D+2) é trocada à mão; editar a operação depois mantém a escolhida."""
    corrente, corretora, bolsa = contas(db)
    vinculo = dict(conta_id=str(corrente.id), custodia_conta_id=str(corretora.id), custodia_caixinha_id=str(bolsa.id))
    postar(client, classe="etf", codigo="LFTB11", simbolo="LFTB11.SA")
    postar(client, ativo_id="1", tipo="venda", data="2026-10-02", data_liquidacao="2026-10-07", quantidade="10",
           preco="40,00", **vinculo)
    assert transferencias(db)[corrente.id].data == date(2026, 10, 7)
    venda = db.scalars(select(OperacaoInvest).where(OperacaoInvest.tipo == "venda")).one()
    assert 'value="2026-10-07"' in client.get(f"/investimentos/operacoes/{venda.id}/editar").text
    r = client.post(f"/investimentos/operacoes/{venda.id}", data=op_form(
        ativo_id="1", tipo="venda", data="2026-10-02", data_liquidacao="2026-10-07", quantidade="5", preco="40,00",
        **vinculo))
    assert r.status_code == 200, r.text
    db.expire_all()
    assert transferencias(db)[corrente.id].data == date(2026, 10, 7)
    r = client.post("/investimentos/operacoes", data=op_form(ativo_id="1", tipo="venda", data="2026-10-02",
                                                             data_liquidacao="2026-10-01", quantidade="1",
                                                             preco="40,00", **vinculo))
    assert r.status_code == 422 and "antes da venda" in r.text
