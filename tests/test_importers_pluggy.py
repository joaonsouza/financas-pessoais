import json
from datetime import date
from pathlib import Path

from app.importers.pluggy_ import centavos, resultado_de_conta
from app.services.categorizacao import sugerir_categoria
from tests.factories import nova_categoria

FIX = Path(__file__).parent / "fixtures" / "pluggy"


def carregar(nome: str):
    return json.loads((FIX / nome).read_text(encoding="utf-8"), parse_float=__import__("decimal").Decimal)


def conta(tipo: str) -> dict:
    return next(c for c in carregar("contas.json")["results"] if c["type"] == tipo)


def transacoes_cartao() -> list[dict]:
    return carregar("transacoes_cartao_p1.json")["results"] + carregar("transacoes_cartao_p2.json")["results"]


def test_centavos_sem_float():
    assert centavos("120.5") == 12050 and centavos(0.1 + 0.2) == 30 and centavos(5) == 500


def test_cartao_sinal_pelo_tipo_e_ignora_pendente():
    r = resultado_de_conta(conta("CREDIT"), transacoes_cartao(), carregar("faturas.json")["results"])
    assert r.origem == "openfinance" and r.tipo_conta == "cartao"
    assert r.identificador_conta == "pluggy:acc-cartao-0001"
    assert [t.id_externo for t in r.transacoes] == ["tx-c-001", "tx-c-002", "tx-c-003"]  # PENDING fora
    assert [t.valor for t in r.transacoes] == [-12050, -9990, 3000]  # DEBIT negativo; CREDIT positivo mesmo com amount -30


def test_cartao_parcelas_dica_e_fatura():
    r = resultado_de_conta(conta("CREDIT"), transacoes_cartao(), carregar("faturas.json")["results"])
    t1, t2, _ = r.transacoes
    assert (t1.parcela_num, t1.parcela_total) == (None, None)  # 1 de 1 não é parcelado
    assert (t2.parcela_num, t2.parcela_total) == (3, 10)
    assert t2.descricao == "LOJA FICTICIA" and t2.descricao_original == "LOJA FICTICIA 03/10"
    assert t1.dica_categoria == "Groceries" and t1.data == date(2026, 9, 20)
    assert r.competencia_fatura == "2026-10" and r.total_informado == 34910
    assert r.vencimento == date(2026, 10, 15) and r.fechamento == date(2026, 10, 8)


def test_sem_total_quando_so_janela_ou_varias_faturas():
    faturas = carregar("faturas.json")["results"]
    r = resultado_de_conta(conta("CREDIT"), transacoes_cartao(), faturas, representa_fatura=False)
    assert r.total_informado is None and r.competencia_fatura == "2026-10"
    mistas = transacoes_cartao()
    mistas[0]["creditCardMetadata"]["billId"] = "bill-2026-09"
    r = resultado_de_conta(conta("CREDIT"), mistas, faturas)
    assert r.competencia_fatura is None and r.total_informado is None
    r = resultado_de_conta(conta("CREDIT"), transacoes_cartao(), [])
    assert r.competencia_fatura is None


def test_pagamento_recebido_no_cartao():
    tx = [{"id": "p1", "date": "2026-10-03T00:00:00.000Z", "description": "Pagamento recebido",
           "descriptionRaw": "PAGAMENTO RECEBIDO", "amount": 100, "type": "CREDIT", "status": "POSTED"}]
    assert resultado_de_conta(conta("CREDIT"), tx, []).transacoes[0].tipo_sugerido == "pagamento_fatura"


def test_conta_corrente_saldo_e_sinais():
    r = resultado_de_conta(conta("BANK"), carregar("transacoes_corrente.json")["results"], [])
    assert r.tipo_conta == "corrente" and r.identificador_conta == "pluggy:acc-corrente-0001"
    assert [t.valor for t in r.transacoes] == [-4500, 500000, -85090]
    assert r.saldo_final_informado == 15432055 and r.total_informado is None


def test_transacao_ilegivel_vira_aviso():
    r = resultado_de_conta(conta("BANK"), [{"id": "x", "date": None, "amount": 1, "type": "DEBIT", "status": "POSTED"}], [])
    assert r.transacoes == [] and "x" in r.avisos[0]


def test_dicas_de_categoria_da_pluggy(session):
    ids = {n: nova_categoria(session, n).id for n in ("Mercado", "Farmácia", "Combustível/Apps")}
    for dica, desc, esperado in [("Groceries", "SUPERMERCADO EXEMPLO", "Mercado"),
                                 ("Mantimentos", "ATACADAO FICTICIO", "Mercado"),
                                 ("Pharmacy", "DROGARIA X", "Farmácia"),
                                 ("Gas stations", "POSTO Y", "Combustível/Apps")]:
        sug = sugerir_categoria(session, desc, dica)
        assert (sug.origem, sug.categoria_id) == ("dica", ids[esperado]), dica
    assert sugerir_categoria(session, "COMPRA QUALQUER", "Shopping").categoria_id is None
