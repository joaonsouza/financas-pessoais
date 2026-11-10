import os
from datetime import date
from pathlib import Path

import pytest

from app.importers.base import LeitorErro, detectar_leitor
from app.importers.ofx import ler

FIX = Path(__file__).parent / "fixtures" / "importacao"
AMOSTRAS = Path(os.environ.get("FINANCAS_AMOSTRAS", r"Z:\Meu Drive\Finanças\App\app-financas-amostras"))


def test_fatura_nubank_anonimizada():
    conteudo = (FIX / "nubank_cartao.ofx").read_bytes()
    assert detectar_leitor("fatura.ofx", conteudo).nome == "ofx"
    r = ler(conteudo)
    assert (r.origem, r.banco, r.tipo_conta) == ("ofx", "nubank", "cartao")
    assert r.identificador_conta == "00000000-0000-4000-8000-000000000001"
    assert len(r.transacoes) == 82
    assert r.saldo_final_informado is None  # no cartão o BALAMT é ignorado
    assert r.avisos == []
    # FITID não é único: o leitor devolve todas as ocorrências
    ids = [t.id_externo for t in r.transacoes]
    assert len(set(ids)) == 64
    parceladas = [t for t in r.transacoes if t.parcela_num]
    assert parceladas and all(1 <= t.parcela_num <= t.parcela_total for t in parceladas)
    assert all(not t.descricao.rstrip().endswith(f"{t.parcela_num}/{t.parcela_total}") for t in parceladas)
    estornos = [t for t in r.transacoes if t.valor > 0]
    assert any(t.descricao_original.startswith("Crédito de") for t in estornos)
    assert all(t.valor < 0 for t in r.transacoes if t not in estornos)
    # positivos que não são estorno só podem ser pagamento da fatura anterior
    assert all(t.descricao_original.startswith("Crédito de") or t.tipo_sugerido == "pagamento_fatura"
               for t in estornos)


def test_conta_sintetica_cp1252_sem_tags_de_fechamento():
    conteudo = (FIX / "conta_sintetica.ofx").read_bytes()
    r = ler(conteudo)
    assert (r.tipo_conta, r.banco, r.identificador_conta) == ("corrente", None, "12345-6")
    assert r.saldo_final_informado == 329010
    a, b, c = r.transacoes
    assert (a.data, a.valor, a.descricao) == (date(2026, 10, 5), 350000, "Salário Ficticio")
    assert (b.data, b.valor, b.descricao) == (date(2026, 10, 7), -8990, "Padaria Pão Quente & Cia")
    assert (c.valor, c.parcela_num, c.parcela_total, c.descricao) == (-12000, 2, 3, "Loja Teste")
    assert c.descricao_original == "Loja Teste - Parcela 2/3"


def test_utf8_com_cabecalho_declarando_cp1252():
    ofx = ("OFXHEADER:100\nCHARSET:1252\n<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>"
           "<CCACCTFROM><ACCTID>X1</ACCTID></CCACCTFROM><BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT</TRNTYPE>"
           "<DTPOSTED>20261001000000[-3:BRT]</DTPOSTED><TRNAMT>-1.50</TRNAMT><FITID>1</FITID>"
           "<MEMO>Açaí</MEMO></STMTTRN></BANKTRANLIST></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>")
    r = ler(ofx.encode("utf-8"))
    assert r.transacoes[0].descricao == "Açaí" and r.transacoes[0].valor == -150


def test_transacao_invalida_vira_aviso():
    ofx = ("<OFX><BANKMSGSRSV1><STMTRS><BANKACCTFROM><ACCTID>1</ACCTID></BANKACCTFROM><BANKTRANLIST>"
           "<STMTTRN><DTPOSTED>lixo<TRNAMT>-1.00<FITID>1<MEMO>a</STMTTRN>"
           "<STMTTRN><DTPOSTED>20261001<TRNAMT>-2.00<FITID>2<MEMO>b</STMTTRN>"
           "</BANKTRANLIST></STMTRS></BANKMSGSRSV1></OFX>")
    r = ler(ofx.encode())
    assert len(r.transacoes) == 1 and r.transacoes[0].valor == -200
    assert len(r.avisos) == 1


def test_pagamento_recebido_no_cartao_sugere_pagamento_fatura():
    ofx = ("<OFX><CREDITCARDMSGSRSV1><CCSTMTRS><CCACCTFROM><ACCTID>1</ACCTID></CCACCTFROM><BANKTRANLIST>"
           "<STMTTRN><DTPOSTED>20261001<TRNAMT>500.00<FITID>1<MEMO>Pagamento recebido</STMTTRN>"
           "</BANKTRANLIST></CCSTMTRS></CREDITCARDMSGSRSV1></OFX>")
    assert ler(ofx.encode()).transacoes[0].tipo_sugerido == "pagamento_fatura"


def test_nao_ofx_levanta_erro():
    with pytest.raises(LeitorErro):
        ler(b"qualquer coisa")


@pytest.mark.amostra_real
def test_amostra_real_nubank():
    arq = AMOSTRAS / "Nubank_2026-11-01.ofx"
    if not arq.exists():
        pytest.skip("amostra real ausente")
    r = ler(arq.read_bytes())
    assert len(r.transacoes) == 82
    assert any(t.parcela_num for t in r.transacoes)
    assert r.avisos == []
