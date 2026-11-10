"""Modal de pagamento da fatura e conciliação de pagamento manual com importação posterior. Dados fictícios."""
from datetime import date

from sqlalchemy import select

from app.importers.base import ResultadoLeitura, TransacaoBruta
from app.models import Lancamento
from app.services.faturas import obter_ou_criar_fatura, pagar_fatura
from app.services.importacao import aplicar_importacao, propor_importacao
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta, novo_cartao, novo_lancamento


def _fatura(db, saldo_desde=date(2026, 1, 1)):
    conta = nova_conta(db, "Mercado Livre", inicio=saldo_desde)
    outra = nova_conta(db, "Itaú")
    cartao = novo_cartao(db, "Cartão Mercado Pago", pagadora=conta, fechamento=28, vencimento=5)
    fatura = obter_ou_criar_fatura(db, cartao, "2026-09")  # vence 05/10
    novo_lancamento(db, cartao, date(2026, 9, 20), -43796, descricao="Compra", fatura_id=fatura.id)
    db.commit()
    return conta, outra, cartao, fatura


def test_modal_com_hoje_ontem_outros_e_conta_pagadora(client, db):
    conta, outra, cartao, _ = _fatura(db)
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-09").text
    assert ">Pagar fatura<" in texto
    for chip in (">Hoje<", ">Ontem<", ">Outros…<"):
        assert chip in texto
    # data padrão = hoje (não o vencimento); valor = restante; conta = pagadora do cartão
    assert f"data: '{HOJE_TESTE.isoformat()}'" in texto
    assert 'value="437,96"' in texto
    assert f'<option value="{conta.id}" selected>' in texto
    assert ">Pagar total<" in texto


def test_modal_avisa_data_anterior_ao_saldo_inicial(client, db):
    conta, _, cartao, _ = _fatura(db, saldo_desde=date(2026, 10, 6))
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-09").text
    assert f'"{conta.id}": "2026-10-06"' in texto
    assert "anterior ao saldo inicial" in texto


def test_pagar_com_data_futura_fica_pendente_e_passada_efetivado(client, db):
    conta, _, cartao, fatura = _fatura(db)
    client.post(f"/cartoes/{cartao.id}/faturas/2026-09/pagar", data={
        "valor": "100,00", "data": "2026-10-05", "conta_origem_id": str(conta.id), "efetivado": "on"})
    client.post(f"/cartoes/{cartao.id}/faturas/2026-09/pagar", data={
        "valor": "100,00", "data": "2026-10-20", "conta_origem_id": str(conta.id), "efetivado": ""})
    saidas = db.scalars(select(Lancamento).where(Lancamento.conta_id == conta.id).order_by(Lancamento.data)).all()
    assert [(l.data, l.status) for l in saidas] == [(date(2026, 10, 5), "efetivado"), (date(2026, 10, 20), "pendente")]


def test_fatura_ja_paga_nao_oferece_pagamento_manual(client, db):
    conta, _, cartao, fatura = _fatura(db)
    pagar_fatura(db, fatura, 43796, date(2026, 10, 5), conta_origem=conta, efetivado=True)
    db.commit()
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-09").text
    assert ">Pagar fatura<" not in texto
    assert "Fatura paga" in texto


# ---------------------------------------------------------------- desfazer


def test_desfazer_pagamento_manual(client, db):
    conta, _, cartao, fatura = _fatura(db)
    _, entrada = pagar_fatura(db, fatura, 43796, date(2026, 10, 5), conta_origem=conta, efetivado=True)
    db.commit()
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-09").text
    assert f'action="/cartoes/{cartao.id}/pagamentos/{entrada.id}/desfazer"' in texto
    resposta = client.post(f"/cartoes/{cartao.id}/pagamentos/{entrada.id}/desfazer", follow_redirects=False)
    assert resposta.status_code == 303
    assert db.scalars(select(Lancamento).where(Lancamento.tipo == "pagamento_fatura")).all() == []
    db.refresh(fatura)
    assert fatura.status == "aberta"
    assert ">Pagar fatura<" in client.get(f"/cartoes/{cartao.id}?fatura=2026-09").text


def test_pagamento_importado_nao_tem_desfazer(client, db):
    conta, _, cartao, fatura = _fatura(db)
    saida, entrada = pagar_fatura(db, fatura, 43796, date(2026, 10, 5), conta_origem=conta, efetivado=True)
    entrada.origem, entrada.hash_dedup = "ofx", "h1"
    db.commit()
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-09").text
    assert "/desfazer" not in texto and "importado" in texto
    assert client.post(f"/cartoes/{cartao.id}/pagamentos/{entrada.id}/desfazer").status_code == 400


# ---------------------------------------------------------------- manual primeiro, importação depois


def _res(tipo_conta, transacoes):
    return ResultadoLeitura(origem="ofx", banco="banco", tipo_conta=tipo_conta, identificador_conta="X",
                            transacoes=transacoes)


def test_pagamento_manual_concilia_com_extrato_da_conta_importado_depois(session):
    conta, _, cartao, fatura = _fatura(session)
    pagar_fatura(session, fatura, 43796, date(2026, 10, 7), conta_origem=conta, efetivado=True)
    session.commit()
    extrato = [TransacaoBruta(data=date(2026, 10, 7), descricao="Pagamento de fatura Mercado Pago",
                              descricao_original="Pagamento de fatura Mercado Pago", valor=-43796)]
    p = propor_importacao(session, _res("corrente", extrato), conta, HOJE_TESTE)
    assert (p.linhas[0].status, p.linhas[0].conciliar_com_id is not None) == ("pagamento_fatura", True)
    aplicar_importacao(session, p, None, HOJE_TESTE)
    pagamentos = session.scalars(select(Lancamento).where(Lancamento.tipo == "pagamento_fatura")).all()
    assert len(pagamentos) == 2  # as duas pernas do manual, sem duplicar


def test_pagamento_manual_concilia_com_fatura_do_cartao_importada_depois(session):
    conta, _, cartao, fatura = _fatura(session)
    pagar_fatura(session, fatura, 43796, date(2026, 10, 7), conta_origem=conta, efetivado=True)
    session.commit()
    arquivo = [TransacaoBruta(data=date(2026, 10, 8), descricao="Pagamento recebido",
                              descricao_original="Pagamento recebido", valor=43796, tipo_sugerido="pagamento_fatura")]
    p = propor_importacao(session, _res("cartao", arquivo), cartao, HOJE_TESTE)
    assert p.linhas[0].conciliar_com_id is not None
    aplicar_importacao(session, p, None, HOJE_TESTE)
    pagamentos = session.scalars(select(Lancamento).where(Lancamento.tipo == "pagamento_fatura")).all()
    assert len(pagamentos) == 2


# ---------------------------------------------------------------- situação vencida


def test_fatura_nao_paga_depois_do_vencimento_aparece_vencida(client, db):
    from app.services.faturas import status_exibicao
    _, _, cartao, fatura = _fatura(db)  # vence 05/10
    assert status_exibicao(fatura, date(2026, 10, 5), 43796) == "fechada"
    assert status_exibicao(fatura, date(2026, 10, 6), 43796) == "vencida"
    assert status_exibicao(fatura, date(2026, 10, 6), 0) == "fechada"  # sem valor não vence
    fatura.status = "paga_parcial"
    assert status_exibicao(fatura, date(2026, 10, 6), 43796) == "vencida"
    fatura.status = "paga"
    assert status_exibicao(fatura, date(2026, 10, 6), 43796) == "paga"


def test_tela_mostra_vencida_com_destaque(client, db, app_teste):
    from app.web.deps import get_hoje
    _, _, cartao, _ = _fatura(db)
    app_teste.dependency_overrides[get_hoje] = lambda: date(2026, 10, 7)
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-09").text
    assert ">Vencida<" in texto and "venceu em 05/10/2026" in texto and "alert-triangle" in texto
