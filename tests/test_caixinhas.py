"""Caixinhas: a conta dividida por finalidade; o Livre fecha a conta com o saldo do banco."""
from datetime import date

import pytest
from sqlalchemy import select

from app.models import Caixinha, Lancamento, Meta, MovimentoCaixinha
from app.services.caixinhas import (
    contas_com_livre_negativo, criar_caixinha, divisao_conta, excluir_caixinha, guardar_na_caixinha,
    movimentos_caixinha, remanejar, resgatar_da_caixinha, saldo_caixinha,
)
from app.services.indicadores_curso import alertas
from app.services.lancamentos import ajustar_saldo, alternar_efetivado
from app.services.metas import (
    migrar_para_caixinhas, progresso_meta, sincronizar_caixinha_meta,
)
from tests.factories import nova_conta, novo_lancamento

HOJE = date(2026, 10, 5)


def test_divisao_livre_e_remanejar(db):
    conta = nova_conta(db, "Caixinha Nubank", saldo=200000)
    iptu = criar_caixinha(db, conta, "IPTU", HOJE, inicial=120000)                 # já estava no banco: soma
    seguro = criar_caixinha(db, conta, "Seguro", HOJE, inicial=80000, do_livre=True)  # já contado no Livre
    d = divisao_conta(db, conta)
    assert (d.saldo, d.em_caixinhas, d.livre) == (320000, 200000, 120000)

    remanejar(db, conta, de=None, para=seguro, valor=20000, data=HOJE)        # Livre → Seguro
    remanejar(db, conta, de=iptu, para=seguro, valor=10000, data=HOJE)        # IPTU → Seguro
    assert (saldo_caixinha(db, iptu), saldo_caixinha(db, seguro), divisao_conta(db, conta).livre) == (110000, 110000, 100000)
    assert divisao_conta(db, conta).saldo == 320000                            # o dinheiro da conta não mudou
    with pytest.raises(ValueError, match="IPTU tem só"):
        remanejar(db, conta, de=iptu, para=None, valor=999999, data=HOJE)
    with pytest.raises(ValueError, match="Livre"):
        criar_caixinha(db, conta, "livre", HOJE)
    with pytest.raises(ValueError, match="já existe"):
        criar_caixinha(db, conta, "iptu", HOJE)


def test_guardar_resgatar_gasto_e_rendimento(db):
    itau = nova_conta(db, "Itaú", saldo=500000)
    caixa = nova_conta(db, "Caixinha Nubank", saldo=0)
    iptu = criar_caixinha(db, caixa, "IPTU", HOJE)
    saida, entrada = guardar_na_caixinha(db, iptu, origem=itau, valor=30000, data=HOJE)
    assert (saida.caixinha_id, entrada.caixinha_id, entrada.conta_id) == (None, iptu.id, caixa.id)
    assert saldo_caixinha(db, iptu) == 30000 and divisao_conta(db, caixa).livre == 0
    # gasto pago direto da caixinha
    gasto = novo_lancamento(db, caixa, HOJE, -10000, descricao="IPTU parcela 1")
    gasto.caixinha_id = iptu.id
    assert saldo_caixinha(db, iptu) == 20000
    # rendimento: conferido com o banco, a diferença cai no Livre
    ajustar_saldo(db, caixa, 20150, HOJE)
    assert (saldo_caixinha(db, iptu), divisao_conta(db, caixa).livre) == (20000, 150)
    resgatar_da_caixinha(db, iptu, destino=itau, valor=5000, data=HOJE)
    assert saldo_caixinha(db, iptu) == 15000
    with pytest.raises(ValueError, match="tem só"):
        resgatar_da_caixinha(db, iptu, destino=itau, valor=99999, data=HOJE)
    assert [m["descricao"] for m in movimentos_caixinha(db, iptu)][:1]


def test_excluir_caixinha_devolve_ao_livre(db):
    conta = nova_conta(db, "Caixinha", saldo=100000)
    c = criar_caixinha(db, conta, "Viagem", HOJE, inicial=40000)
    assert divisao_conta(db, conta).saldo == 140000
    excluir_caixinha(db, c)
    assert divisao_conta(db, conta).livre == 140000 and db.get(Caixinha, c.id) is None


def test_meta_com_conta_guarda_em_caixinha_e_troca_de_conta(db):
    conta = nova_conta(db, "NuInvest", saldo=0)
    meta = Meta(nome="Reserva", tipo="reserva_emergencia", conta_id=conta.id, valor_manual=223225)
    db.add(meta)
    db.flush()
    caixinha = sincronizar_caixinha_meta(db, meta, HOJE)
    assert (caixinha.nome, saldo_caixinha(db, caixinha)) == ("Reserva", 223225)
    assert progresso_meta(db, meta, HOJE).atual == 223225
    d = divisao_conta(db, conta)
    assert (d.saldo, d.livre) == (223225, 0)                      # o "já tem guardado" soma na conta
    # caixinhas acima do saldo (ex.: distribuiu do Livre mais do que a conta tinha): avisa
    criar_caixinha(db, conta, "Viagem", HOJE, inicial=50000, do_livre=True)
    assert [d.conta.id for d in contas_com_livre_negativo(db)] == [conta.id]
    assert any(a.chave == f"livre_negativo:{conta.id}" for a in alertas(db, "2026-10", HOJE))
    ajustar_saldo(db, conta, 300000, HOJE)                         # conferido com o banco
    assert divisao_conta(db, conta).livre == 300000 - 223225 - 50000
    # passa a fora do app: a caixinha fica na conta, sem a meta
    meta.conta_id, meta.valor_manual = None, 1000
    sincronizar_caixinha_meta(db, meta, HOJE)
    assert db.get(Caixinha, caixinha.id).meta_id is None and progresso_meta(db, meta, HOJE).atual == 1000


def test_migracao_cria_caixinhas_de_metas(db):
    nuinvest = nova_conta(db, "NuInvest")
    db.add(Meta(nome="Reserva", tipo="reserva_emergencia", conta_id=nuinvest.id, valor_manual=300000))
    db.flush()
    assert migrar_para_caixinhas(db, HOJE) == 1
    assert migrar_para_caixinhas(db, HOJE) == 0   # idempotente
    reserva = db.scalar(select(Caixinha).where(Caixinha.nome == "Reserva"))
    assert saldo_caixinha(db, reserva) == 300000


def test_tela_da_conta_criar_remanejar_guardar(client, db):
    itau = nova_conta(db, "Itaú", saldo=500000)
    caixa = nova_conta(db, "Caixinha Nubank", saldo=100000)
    db.commit()
    r = client.post(f"/contas/{caixa.id}/caixinhas", data={"nome": "IPTU", "inicial": "300,00"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/contas/{caixa.id}"
    iptu = db.scalar(select(Caixinha).where(Caixinha.nome == "IPTU"))
    client.post(f"/contas/{caixa.id}/remanejar", data={"de": "livre", "para": str(iptu.id), "valor": "100,00"})
    client.post(f"/contas/{caixa.id}/caixinhas/{iptu.id}/guardar",
                data={"outra_id": str(itau.id), "valor": "50,00", "data": "2026-10-05"})
    client.post(f"/contas/{caixa.id}/caixinhas/{iptu.id}/guardar",
                data={"outra_id": "aporte", "valor": "10,00", "data": "2026-10-05"})
    db.expire_all()
    assert saldo_caixinha(db, iptu) == 46000
    d = divisao_conta(db, caixa)
    assert (d.saldo, d.livre) == (136000, 90000)   # 1.000 + 300 inicial + 50 transferido + 10 aporte
    texto = client.get(f"/contas/{caixa.id}").text
    assert "IPTU" in texto and "R$ 460,00" in texto and "Livre" in texto and "R$ 900,00" in texto
    assert "Livre R$ 900,00" in client.get("/contas").text
    r = client.post(f"/contas/{caixa.id}/remanejar", data={"de": str(iptu.id), "para": "livre", "valor": "9.999,00"})
    assert r.status_code == 422 and "IPTU tem só" in r.text
    # conferir com o banco pela tela da conta volta para ela
    r = client.post(f"/contas/{caixa.id}/ajustar", data={"novo_saldo": "1.060,00", "voltar": "conta"}, follow_redirects=False)
    assert r.headers["location"] == f"/contas/{caixa.id}"


def test_lancamento_marcado_na_caixinha_pelo_formulario(client, db):
    caixa = nova_conta(db, "Caixinha Nubank", saldo=100000)
    iptu = criar_caixinha(db, caixa, "IPTU", HOJE, inicial=50000)
    db.commit()
    assert "Caixinha" in client.get(f"/lancamentos/novo?tipo=despesa&conta={caixa.id}").text
    client.post("/lancamentos", data={"tipo": "despesa", "valor": "200,00", "descricao": "IPTU 1/10", "data": "2026-10-05",
                                      "conta_id": str(caixa.id), "efetivado": "on", "caixinha_id": str(iptu.id)})
    db.expire_all()
    lanc = db.scalar(select(Lancamento).where(Lancamento.descricao == "IPTU 1/10"))
    assert lanc.caixinha_id == iptu.id and saldo_caixinha(db, iptu) == 30000
    assert "IPTU" in client.get("/lancamentos?mes=2026-10").text
    client.post(f"/lancamentos/{lanc.id}", data={"valor": "200,00", "descricao": "IPTU 1/10", "data": "2026-10-05",
                                                 "efetivado": "on", "caixinha_id": ""})
    db.expire_all()
    assert db.get(Lancamento, lanc.id).caixinha_id is None and saldo_caixinha(db, iptu) == 50000


def test_objetivo_com_conta_pela_tela_cria_caixinha(client, db):
    itau = nova_conta(db, "Itaú", saldo=100000)
    nuinvest = nova_conta(db, "NuInvest", saldo=500000)
    db.commit()
    client.post("/planejamento/metas", data={"nome": "Viagem", "tipo": "objetivo", "valor_alvo": "5.000,00",
                                    "conta_id": str(nuinvest.id), "valor_manual": "1.000,00"})
    db.expire_all()
    meta = db.scalar(select(Meta))
    caixinha = db.scalar(select(Caixinha).where(Caixinha.meta_id == meta.id))
    assert caixinha.conta_id == nuinvest.id and saldo_caixinha(db, caixinha) == 100000
    assert divisao_conta(db, nuinvest).saldo == 600000
    client.post(f"/planejamento/metas/{meta.id}/guardar", data={"valor": "200,00", "conta_id": str(itau.id), "data": "2026-10-05"})
    db.expire_all()
    assert progresso_meta(db, meta, HOJE).atual == 120000
    detalhe = client.get(f"/planejamento/metas/{meta.id}").text
    assert "caixinha em" in detalhe and "Meta: Viagem" in detalhe and "Valor inicial: Viagem" in detalhe
    assert "Objetivo" in client.get(f"/contas/{nuinvest.id}").text


def test_apagar_do_extrato_da_caixinha(client, db):
    itau = nova_conta(db, "Itaú", saldo=100000)
    caixa = nova_conta(db, "Caixinha Nubank", saldo=0)
    iptu = criar_caixinha(db, caixa, "IPTU", HOJE, inicial=30000)
    seguro = criar_caixinha(db, caixa, "Seguro", HOJE)
    _, entrada = guardar_na_caixinha(db, iptu, origem=itau, valor=5000, data=HOJE)
    remanejar(db, caixa, de=iptu, para=seguro, valor=1000, data=HOJE)
    db.commit()
    entrada_id = entrada.id
    extrato = {m["descricao"]: m for m in movimentos_caixinha(db, iptu)}
    # transferência: as duas pernas saem (Itaú volta a ter o dinheiro)
    r = client.post(f"/contas/{caixa.id}/caixinhas/{iptu.id}/extrato/lancamento/{entrada_id}/excluir", follow_redirects=False)
    assert r.status_code == 303
    # remanejamento: desfaz as duas pontas
    rem = extrato["IPTU → Seguro"]
    client.post(f"/contas/{caixa.id}/caixinhas/{iptu.id}/extrato/divisao/{rem['id']}/excluir")
    db.expire_all()
    assert (saldo_caixinha(db, iptu), saldo_caixinha(db, seguro)) == (30000, 0)
    assert divisao_conta(db, itau).saldo == 100000
    # o valor inicial também se apaga, e sai do saldo da conta
    inicial = extrato["Valor inicial: IPTU"]
    client.post(f"/contas/{caixa.id}/caixinhas/{iptu.id}/extrato/lancamento/{inicial['id']}/excluir")
    db.expire_all()
    assert (saldo_caixinha(db, iptu), divisao_conta(db, caixa).saldo) == (0, 0)
    # item de outra caixinha não se apaga por esta
    r = client.post(f"/contas/{caixa.id}/caixinhas/{seguro.id}/extrato/lancamento/{inicial['id']}/excluir")
    assert r.status_code == 422


def test_objetivo_soma_todas_as_caixinhas_ligadas(client, db):
    conta = nova_conta(db, "Reserva de Emergência", saldo=0)
    meta = Meta(nome="Reserva", tipo="reserva_emergencia", conta_id=conta.id, valor_manual=200000)
    db.add(meta)
    db.flush()
    sincronizar_caixinha_meta(db, meta, HOJE)
    db.commit()
    # conta só da reserva: a caixinha nova já vem marcada como parte dela
    assert "Faz parte de" in client.get(f"/contas/{conta.id}").text
    client.post(f"/contas/{conta.id}/caixinhas", data={"nome": "100% CDI", "inicial": "250,00", "meta_id": str(meta.id)})
    db.expire_all()
    assert progresso_meta(db, meta, HOJE).atual == 225000
    cdi = db.scalar(select(Caixinha).where(Caixinha.nome == "100% CDI"))
    client.post(f"/contas/{conta.id}/caixinhas/{cdi.id}/objetivo", data={"meta_id": ""})   # tira do objetivo
    db.expire_all()
    assert progresso_meta(db, meta, HOJE).atual == 200000
    outra = nova_conta(db, "Outra")
    cx = criar_caixinha(db, outra, "X", HOJE)
    db.commit()
    r = client.post(f"/contas/{outra.id}/caixinhas/{cx.id}/objetivo", data={"meta_id": str(meta.id)})
    assert r.status_code == 422 and "outra conta" in r.text


def test_transferencia_comum_numa_linha_e_contas_fora_do_saldo_liquido(client, db):
    from app.services.consulta_lancamentos import linhas_da_lista
    from app.services.lancamentos import criar_transferencia
    itau = nova_conta(db, "Itaú", saldo=100000)
    nubank = nova_conta(db, "Nubank", saldo=0)
    invest = nova_conta(db, "NuInvest", saldo=500000, sl=False)
    criar_transferencia(db, origem=itau, destino=nubank, data=HOJE, valor_abs=20000, descricao="Pix", efetivado=True)
    db.commit()
    (linha,) = linhas_da_lista(db, "2026-10")
    assert (linha.descricao, linha.valor, linha.origem_nome, linha.destino_nome, linha.aporte_meta_id) == (
        "Pix", -20000, "Itaú", "Nubank", None)
    texto = client.get("/contas").text
    assert "Fora do saldo líquido" in texto and texto.index("Nubank") < texto.index("Fora do saldo líquido") < texto.index("NuInvest")
    assert "Total em todas as contas" in texto and "R$ 6.000,00" in texto
    total = texto[texto.index("Total em todas as contas"):]
    assert "Total atual" in total and "R$ 6.000,00" in total


def test_transferencia_com_entrada_pendente_mostra_as_duas_pernas(db):
    """Venda com liquidação em D+2: a saída da corretora já foi, a entrada no banco ainda está pendente."""
    from app.services.consulta_lancamentos import linhas_da_lista
    from app.services.lancamentos import criar_transferencia
    invest = nova_conta(db, "C6 Invest", saldo=0, sl=False)
    banco = nova_conta(db, "C6 Bank", saldo=0)
    saida, entrada = criar_transferencia(db, origem=invest, destino=banco, data=date(2026, 10, 26),
                                         valor_abs=16567, descricao="Venda HASH11", efetivado=True)
    entrada.status, entrada.data = "pendente", date(2026, 10, 28)
    db.commit()
    linhas = linhas_da_lista(db, "2026-10")
    assert sorted((l.data, l.status, l.valor) for l in linhas) == [
        (date(2026, 10, 26), "efetivado", -16567), (date(2026, 10, 28), "pendente", 16567)]


def test_ajustar_saldo_da_caixinha_pelo_banco(client, db):
    conta = nova_conta(db, "Reserva", saldo=0)
    turbo = criar_caixinha(db, conta, "Turbo", HOJE, inicial=200000)
    cdi = criar_caixinha(db, conta, "CDI", HOJE, inicial=20000)
    db.commit()
    r = client.post(f"/contas/{conta.id}/caixinhas/{turbo.id}/ajustar", data={"novo_saldo": "2.031,40", "data": "2026-10-05"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    d = divisao_conta(db, conta)
    assert (saldo_caixinha(db, turbo), saldo_caixinha(db, cdi), d.saldo, d.livre) == (203140, 20000, 223140, 0)
    assert "Ajuste de saldo: Turbo" in [m["descricao"] for m in movimentos_caixinha(db, turbo)]
    # para menos também (tarifa, resgate que o app não viu)
    client.post(f"/contas/{conta.id}/caixinhas/{cdi.id}/ajustar", data={"novo_saldo": "150,00"})
    db.expire_all()
    assert (saldo_caixinha(db, cdi), divisao_conta(db, conta).saldo) == (15000, 218140)
