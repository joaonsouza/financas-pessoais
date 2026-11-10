import pytest
from sqlalchemy import select

from app.importers.base import (
    LeitorErro, ResultadoLeitura, TransacaoBruta, detectar_leitor, detectar_pdf,
)
from app.importers.normalizacao import extrair_parcela, normalizar_descricao, valor_br
from app.models import Importacao, PerfilCsv, RegraCategoria
from tests.factories import nova_categoria, nova_conta


@pytest.mark.parametrize("texto,esperado", [
    ("COMPRA X - Parcela 1/2", ("COMPRA X", 1, 2)),
    ("COMPRA X - 1/4", ("COMPRA X", 1, 4)),
    ("Loja Y Parcela 16 de 21", ("Loja Y", 16, 21)),
    ("LOJAX03/04", ("LOJAX", 3, 4)),
    ("MERCADO 2", ("MERCADO 2", None, None)),
    ("PADARIA", ("PADARIA", None, None)),
    ("ITEM 5/2", ("ITEM 5/2", None, None)),  # num > total não é parcela
])
def test_extrair_parcela(texto, esperado):
    assert extrair_parcela(texto) == esperado


@pytest.mark.parametrize("texto,esperado", [
    ("  Padaria   São João ", "PADARIA SAO JOAO"),
    ("Compra Açaí - Parcela 1/2", "COMPRA ACAI"),
    ("LOJAX03/04", "LOJAX"),
    ("PIX TRANSF FULANO05/09", "PIX TRANSF FULANO"),
    ("Loja Y Parcela 16 de 21", "LOJA Y"),
])
def test_normalizar_descricao(texto, esperado):
    assert normalizar_descricao(texto) == esperado


def test_valor_br():
    assert valor_br("1.234,56") == 123456
    assert valor_br("-0,01") == -1
    assert valor_br("R$ 5,00") == 500


def test_detectar_ofx_por_conteudo():
    leitor = detectar_leitor("x.txt", b"OFXHEADER:100\nDATA:OFXSGML\n<OFX>")
    assert leitor and leitor.nome == "ofx"
    assert detectar_leitor("x.ofx", b"<OFX><SIGNONMSGSRSV1>").nome == "ofx"


def test_detectar_csv_pelo_cabecalho():
    assert detectar_leitor("a.csv", "Data,Valor,Identificador,Descrição\n".encode()).nome == "csv_nubank_conta"
    assert detectar_leitor("a.csv", b"date,title,amount\n").nome == "csv_nubank_cartao"
    assert detectar_leitor("a.csv", b"dia;historico;quanto\n").nome == "csv_generico"


def test_detectar_pdf_por_palavras_chave():
    assert detectar_pdf("Banco X Mercado Pago Essa é sua fatura").nome == "pdf_mercadopago"
    assert detectar_pdf("Itaú extrato conta corrente").nome == "pdf_itau_extrato"
    assert detectar_pdf("itau Resumo da fatura").nome == "pdf_itau_fatura"
    assert detectar_pdf("Totaldafatura Itaú Extratoconta") .nome == "pdf_itau_extrato"
    assert detectar_pdf("documento qualquer") is None


def test_desconhecido_devolve_none():
    assert detectar_leitor("a.bin", b"\x00\x01\x02") is None
    assert detectar_leitor("a.pdf", b"%PDF-1.4 corrompido") is None


def test_contrato_defaults():
    r = ResultadoLeitura("ofx", "nubank", "cartao", None, [])
    assert r.avisos == [] and r.total_informado is None
    assert TransacaoBruta.__dataclass_fields__["id_externo"].default is None
    assert issubclass(LeitorErro, ValueError)


def test_modelos_de_importacao(session):
    conta = nova_conta(session)
    cat = nova_categoria(session)
    session.add_all([
        Importacao(arquivo_nome="a.ofx", hash_arquivo="h", tipo="ofx", conta_id=conta.id,
                   snapshot_conciliacoes=[{"id": 1, "status": "pendente"}]),
        RegraCategoria(padrao="UBER", categoria_id=cat.id, prioridade=1, criada_de="Uber *Trip"),
        PerfilCsv(conta_id=conta.id, mapeamento={"data": "Data", "valor": "Valor"}),
    ])
    session.commit()
    imp = session.scalars(select(Importacao)).one()
    assert imp.desfeita is False and imp.qtd_novas == 0
    assert imp.snapshot_conciliacoes == [{"id": 1, "status": "pendente"}]
    assert session.scalars(select(PerfilCsv)).one().mapeamento["data"] == "Data"
