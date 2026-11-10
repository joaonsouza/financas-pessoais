import json
import os
from datetime import date
from pathlib import Path

import pytest

from app.importers import pdf_itau_extrato, pdf_mercadopago
from app.importers.base import LeitorErro, detectar_leitor, detectar_pdf
from app.importers.documento import extrair, linhas_do_documento
from app.importers.pdf_itau_extrato import tipo_sugerido, validar_saldos

FIX = Path(__file__).parent / "fixtures" / "importacao"
AMOSTRAS = Path(os.environ.get("FINANCAS_AMOSTRAS", r"Z:\Meu Drive\Finanças\App\app-financas-amostras"))


def _doc(*paginas: list[str]) -> dict:
    return {"paginas": [
        {"numero": i, "largura": 595.0, "altura": 842.0, "palavras": [],
         "linhas": [{"texto": t, "x0": 10.0, "top": float(n * 12)} for n, t in enumerate(linhas)]}
        for i, linhas in enumerate(paginas, start=1)
    ]}


def _fixture(nome: str) -> dict:
    return json.loads((FIX / nome).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- documento / detecção

def test_documento_e_serializavel_e_pdf_invalido_da_erro():
    assert json.loads(json.dumps(_fixture("mp_fatura.json")))["paginas"]
    with pytest.raises(LeitorErro, match="Não foi possível ler o PDF"):
        extrair(b"%PDF-1.4 lixo")


def test_detecta_banco_pelo_texto_da_primeira_pagina():
    mp = linhas_do_documento({"paginas": _fixture("mp_fatura.json")["paginas"][:1]})
    assert detectar_pdf("\n".join(mp)).nome == "pdf_mercadopago"
    itau = linhas_do_documento({"paginas": _fixture("itau_extrato.json")["paginas"][:1]})
    assert detectar_pdf("\n".join(itau)).nome == "pdf_itau_extrato"
    assert detectar_leitor("x.pdf", b"%PDF-1.4 lixo") is None


# ---------------------------------------------------------------- Mercado Pago

def test_mp_fatura_anonimizada():
    r = pdf_mercadopago.interpretar(_fixture("mp_fatura.json"))
    assert (r.origem, r.banco, r.tipo_conta, r.identificador_conta) == ("pdf", "mercadopago", "cartao", "1234")
    assert (r.vencimento, r.fechamento, r.competencia_fatura) == (date(2026, 10, 5), date(2026, 9, 29), "2026-09")
    compras = [t for t in r.transacoes if t.tipo_sugerido != "pagamento_fatura"]
    assert len(compras) == 4
    assert -sum(t.valor for t in compras) == r.total_informado
    assert r.avisos == []
    assert all(t.valor < 0 and t.portador == "1234" for t in compras)
    assert [(t.parcela_num, t.parcela_total) for t in compras] == [(16, 21), (10, 12), (10, 14), (2, 3)]
    assert all("Parcela" not in t.descricao for t in compras)
    # datas só com DD/MM: a compra de dezembro é do ano anterior ao fechamento
    assert compras[1].data == date(2025, 12, 7) and compras[0].data == date(2026, 6, 25)
    (pag,) = [t for t in r.transacoes if t.tipo_sugerido == "pagamento_fatura"]
    assert pag.valor > 0 and pag.data == date(2026, 9, 2)


def test_mp_soma_divergente_gera_aviso_e_estorno_fica_positivo():
    doc = _fixture("mp_fatura.json")
    for p in doc["paginas"]:
        for linha in p["linhas"]:
            if linha["texto"].startswith("07/12 LOJA FICTICIA 02 Parcela 10 de 12"):
                linha["texto"] = "07/12 Estorno loja R$ -3,88"
    r = pdf_mercadopago.interpretar(doc)
    assert any("não bate" in a for a in r.avisos)
    estorno = next(t for t in r.transacoes if t.descricao == "Estorno loja")
    assert estorno.valor == 388


def test_mp_sem_resumo_levanta_erro():
    with pytest.raises(LeitorErro):
        pdf_mercadopago.interpretar(_doc(["Documento qualquer"]))


# ---------------------------------------------------------------- Itaú extrato

def test_itau_extrato_anonimizado():
    r = pdf_itau_extrato.interpretar(_fixture("itau_extrato.json"))
    assert (r.origem, r.banco, r.tipo_conta, r.identificador_conta) == ("pdf", "itau", "corrente", "1234/56789-0")
    assert len(r.transacoes) == 32
    assert r.avisos == []
    assert r.saldo_final_informado is not None
    datas = [t.data for t in r.transacoes]
    assert datas == sorted(datas)  # inverte a ordem decrescente do PDF
    assert not any("SALDO" in t.descricao_original for t in r.transacoes)
    glue = next(t for t in r.transacoes if t.descricao_original.startswith("PIX TRANSF PESSOA"))
    assert not glue.descricao[-5:].count("/")  # data grudada fora da descrição limpa
    tipos = {t.tipo_sugerido for t in r.transacoes}
    assert {"pagamento_fatura", "receita", "encargo", "transferencia", None} <= tipos


@pytest.mark.parametrize("desc,esperado", [
    ("ITAU VISA 1234-5678", "pagamento_fatura"),
    ("FATURA UNICLASS SIGNATURE", "pagamento_fatura"),
    ("PIX QRS NU PAGAMENT02/09", "pagamento_fatura"),
    ("PIX QRS MERCADO PAG02/09", "pagamento_fatura"),
    ("REMUNERACAO/SALARIO", "receita"),
    ("REND PAGO APLIC AUT MAIS", "receita"),
    ("JUROS LIMITE DA CONTA", "encargo"),
    ("IOF", "encargo"),
    ("PIX TRANSF FULANO02/09", "transferencia"),
    ("PAG BOLETO ALGUEM", None),
])
def test_itau_tipo_sugerido(desc, esperado):
    assert tipo_sugerido(desc) == esperado


def test_itau_saldo_divergente_gera_aviso():
    doc = _fixture("itau_extrato.json")
    for p in doc["paginas"]:
        for linha in p["linhas"]:
            if linha["texto"].startswith("30/09/2026 SALDO DO DIA"):
                linha["texto"] = "30/09/2026 SALDO DO DIA 999,99"
    r = pdf_itau_extrato.interpretar(doc)
    assert r.avisos and "30/09/2026" in r.avisos[0]


def test_validar_saldos_aceita_lancamentos_posteriores_no_ultimo_saldo():
    d1, d2, d3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
    saldos = {d1: 1000, d2: 1500}
    assert validar_saldos(saldos, [(d2, 500)]) == []
    assert validar_saldos(saldos, [(d3, 500)]) == []   # datado depois, mas contado no saldo do banco
    assert len(validar_saldos(saldos, [(d2, 400)])) == 1


def test_itau_sem_lancamentos_levanta_erro():
    with pytest.raises(LeitorErro):
        pdf_itau_extrato.interpretar(_doc(["extrato conta / lançamentos"]))


# ---------------------------------------------------------------- amostras reais (só validações)

def _amostra(nome: str) -> bytes:
    arq = AMOSTRAS / nome
    if not arq.exists():
        pytest.skip("amostra real ausente")
    return arq.read_bytes()


@pytest.mark.amostra_real
def test_amostra_real_mercado_pago():
    conteudo = _amostra("credit-card-mp-statement.pdf")
    assert detectar_leitor("fatura.pdf", conteudo).nome == "pdf_mercadopago"
    r = pdf_mercadopago.ler(conteudo)
    compras = [t for t in r.transacoes if t.tipo_sugerido != "pagamento_fatura"]
    assert -sum(t.valor for t in compras) == r.total_informado
    assert r.avisos == [] and any(t.tipo_sugerido == "pagamento_fatura" for t in r.transacoes)


@pytest.mark.amostra_real
def test_amostra_real_extrato_itau():
    conteudo = _amostra("itau_extrato_082026.pdf")
    assert detectar_leitor("extrato.pdf", conteudo).nome == "pdf_itau_extrato"
    r = pdf_itau_extrato.ler(conteudo)
    assert r.avisos == [] and r.saldo_final_informado is not None and len(r.transacoes) > 0
