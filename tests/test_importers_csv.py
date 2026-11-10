from datetime import date

import pytest

from app.importers.base import LeitorErro, detectar_leitor
from app.importers.csv_ import ler_c6_cartao, ler_generico, ler_nubank_cartao, ler_nubank_conta
from app.models import PerfilCsv

CONTA = (
    "Data,Valor,Identificador,Descrição\n"
    "01/10/2026,-50.00,id-1,Transferência enviada - Fulano Ficticio\n"
    "02/10/2026,1234.56,id-2,Transferência recebida - Beltrano Ficticio\n"
    "03/10/2026,abc,id-3,Valor quebrado\n"
    "31/02/2026,-1.00,id-4,Data quebrada\n"
    ",,,\n"
    "04/10/2026,-10.00,id-5,Compra - Parcela 2/3\n"
)
CARTAO = (
    "date,title,amount\n"
    "2026-10-01,Loja Ficticia - Parcela 1/2,100.50\n"
    "2026-10-02,Estorno Loja,-20.00\n"
    "2026-10-03,Pagamento recebido,-500.00\n"
    "ontem,Data ruim,1.00\n"
)


def test_nubank_conta():
    conteudo = CONTA.encode("utf-8")
    assert detectar_leitor("NU_conta.csv", conteudo).nome == "csv_nubank_conta"
    r = ler_nubank_conta(conteudo)
    assert (r.origem, r.banco, r.tipo_conta) == ("csv", "nubank", "corrente")
    assert [(t.data, t.valor, t.id_externo) for t in r.transacoes] == [
        (date(2026, 10, 1), -5000, "id-1"),
        (date(2026, 10, 2), 123456, "id-2"),
        (date(2026, 10, 4), -1000, "id-5"),
    ]
    assert r.transacoes[2].descricao == "Compra" and r.transacoes[2].parcela_num == 2
    assert len(r.avisos) == 2 and "Linha 4" in r.avisos[0] and "Linha 5" in r.avisos[1]


def test_nubank_cartao_inverte_sinal_e_sugere_pagamento():
    conteudo = CARTAO.encode("utf-8")
    assert detectar_leitor("fatura.csv", conteudo).nome == "csv_nubank_cartao"
    r = ler_nubank_cartao(conteudo)
    assert r.tipo_conta == "cartao"
    assert [t.valor for t in r.transacoes] == [-10050, 2000, 50000]
    assert (r.transacoes[0].parcela_num, r.transacoes[0].parcela_total) == (1, 2)
    assert r.transacoes[2].tipo_sugerido == "pagamento_fatura"
    assert len(r.avisos) == 1


def test_generico_com_perfil_dict_e_modelo():
    csv_ = "Dia;Histórico;Quanto\n05/10/2026;Padaria;1.234,56\n06/10/2026;Estorno;-5,00\n07/10/2026;X;lixo\n"
    perfil = {"coluna_data": "Dia", "coluna_descricao": "Histórico", "coluna_valor": "Quanto",
              "formato_data": "%d/%m/%Y", "separador_decimal": ",", "inverter_sinal": True}
    r = ler_generico(csv_.encode("utf-8"), perfil)
    assert [t.valor for t in r.transacoes] == [-123456, 500]
    assert len(r.avisos) == 1
    r2 = ler_generico(csv_.encode("cp1252"), PerfilCsv(conta_id=1, mapeamento=perfil))
    assert [t.valor for t in r2.transacoes] == [-123456, 500]
    assert r2.transacoes[0].descricao == "Padaria"


def test_generico_ponto_decimal_e_delimitador_virgula():
    csv_ = "d,desc,v\n2026-10-05,A,12.30\n"
    r = ler_generico(csv_.encode(), {"coluna_data": "d", "coluna_descricao": "desc", "coluna_valor": "v",
                                     "separador_decimal": "."})
    assert r.transacoes[0].valor == 1230 and r.transacoes[0].data == date(2026, 10, 5)


def test_erros_claros():
    with pytest.raises(LeitorErro):
        ler_generico(b"a;b\n1;2\n", None)
    with pytest.raises(LeitorErro, match="Colunas não encontradas"):
        ler_generico(b"a;b\n1;2\n", {"coluna_data": "x", "coluna_descricao": "b", "coluna_valor": "a"})
    with pytest.raises(LeitorErro, match="falta coluna_valor"):
        ler_generico(b"a;b\n1;2\n", {"coluna_data": "a", "coluna_descricao": "b"})
    with pytest.raises(LeitorErro, match="vazio"):
        ler_nubank_conta(b"")


C6 = (
    "\ufeffData de Compra;Nome no Cartão;Final do Cartão;Categoria;Descrição;Parcela;"
    "Valor (em US$);Cotação (em R$);Valor (em R$)\r\n"
    "07/10/2026;FULANO FICTICIO;1234;Supermercados;MERCADO FICTICIO;Única;0;0;72.30\r\n"
    "08/10/2026;FULANO FICTICIO;1234;Departamento;LOJA FICTICIA;2/3;0;0;100.00\r\n"
    "09/10/2026;FULANO FICTICIO;1234;-;Inclusao de Pagamento;Única;0;0;-500.00\r\n"
    "10/10/2026;FULANO FICTICIO;1234;-;ESTORNO LOJA;Única;0;0;abc\r\n"
)


def test_c6_cartao():
    conteudo = C6.encode("utf-8")
    assert detectar_leitor("Fatura_2026-11-01.csv", conteudo).nome == "csv_c6_cartao"
    r = ler_c6_cartao(conteudo)
    assert (r.origem, r.banco, r.tipo_conta, r.identificador_conta) == ("csv", "c6", "cartao", "1234")
    assert [(t.data, t.valor, t.parcela_num, t.parcela_total, t.tipo_sugerido) for t in r.transacoes] == [
        (date(2026, 10, 7), -7230, None, None, None),
        (date(2026, 10, 8), -10000, 2, 3, None),
        (date(2026, 10, 9), 50000, None, None, "pagamento_fatura"),
    ]
    assert r.transacoes[0].dica_categoria == "Supermercados" and r.transacoes[0].portador == "1234"
    assert r.transacoes[1].descricao == "LOJA FICTICIA"
    assert len(r.avisos) == 1 and "Linha 5" in r.avisos[0]
