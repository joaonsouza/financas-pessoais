"""Planilha da fatura do cartão Itaú (site do banco): fechada, atual ou próxima, todas no mesmo layout."""
import io
from datetime import date, datetime

import openpyxl
import pytest

from app.importers.base import LeitorErro, detectar_leitor
from app.importers.xlsx_itau_fatura import ler

LANCS = [
    (datetime(2026, 9, 10), "Pagamento Efetuado", None, -8931.27, None, "Titular", "Fulano", "Físico", "****7707"),
    (datetime(2026, 10, 2), "Burger King", None, 40.9, None, "Titular", "Fulano", "Wallet", "****1905"),
    (datetime(2026, 10, 2), "Loja Xyz     ", "Parcela 1 de 3", 63.6, None, "Titular", "Fulano", "Wallet", "****1905"),
    (datetime(2026, 9, 27), "Latam Air*abc", None, 100, "USD\xa018,32 \n(cotação R$\xa05,45)", "Adicional",
     "Ciclana", "Virtual recorrente", "****4070"),
    (datetime(2026, 9, 28), "Iof Compra Internaciona", None, 3.5, None, "Titular", "Fulano", "Físico", "****1905"),
    (datetime(2026, 9, 25), "Credito Airbnb * X", None, -21.78, None, "Titular", "Fulano", "Wallet", "****1905"),
    (datetime(2025, 11, 28), "Seguradora Auto ", "Parcela 10 de 10", 168.32, None, "Titular", "Fulano",
     "Virtual recorrente", "****4070"),
]


def _planilha(titulo="Fatura Fechada - Outubro/2026", rotulo_valor="Valor", total=354.54,
              vencimento=datetime(2026, 10, 13), lancs=LANCS) -> bytes:
    """O layout do site do Itaú: coluna A vazia, cabeçalho do cartão e a tabela de lançamentos."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Fatura 10-26"
    ws["B3"], ws["C3"] = "Nome", "Fulano de Tal"
    ws["B8"] = titulo
    ws["B9"], ws["G9"], ws["I9"] = "Cartão", rotulo_valor, "Vencimento"
    ws["B10"], ws["G10"], ws["I10"] = "Latam Pass Itau Infinite Visa - final 1905", total, vencimento
    ws["B13"] = "Lançamentos"
    for col, rotulo in zip("BCDEGHIJ", ["Data", "Lançamento", "Parcelamento", "Valor", "Titularidade", "Nome",
                                         "Tipo do cartão", "Número do cartão"]):
        ws[f"{col}14"] = rotulo
    linha = 15
    for d, desc, parc, valor, usd, tit, nome, tipo, num in lancs:
        for col, v in zip("BCDEFGHIJ", [d, desc, parc, valor, usd, tit, nome, tipo, num]):
            ws[f"{col}{linha}"] = v
        linha += 1
    ws[f"D{linha + 1}"] = "Subtotal  "
    ws[f"B{linha + 5}"] = "Importante saber"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_fatura_fechada():
    r = ler(_planilha())
    assert (r.origem, r.banco, r.tipo_conta, r.identificador_conta) == ("xlsx", "itau", "cartao", "1905")
    assert (r.vencimento, r.competencia_fatura, r.fechamento) == (date(2026, 10, 13), "2026-10", None)
    assert r.total_informado == 35454 and r.avisos == []
    por_desc = {t.descricao: t for t in r.transacoes}
    assert por_desc["Burger King"].valor == -4090                       # compra: saída no cartão
    assert por_desc["Pagamento Efetuado"].tipo_sugerido == "pagamento_fatura"
    assert por_desc["Pagamento Efetuado"].valor == 893127
    assert por_desc["Credito Airbnb * X"].valor == 2178                  # estorno: entrada
    assert por_desc["Iof Compra Internaciona"].tipo_sugerido == "encargo"
    loja = por_desc["Loja Xyz"]
    assert (loja.parcela_num, loja.parcela_total, loja.data, loja.portador) == (1, 3, date(2026, 10, 2), "1905")
    assert por_desc["Seguradora Auto"].parcela_num == 10
    assert por_desc["Latam Air*abc"].portador == "4070"
    assert "USD" in por_desc["Latam Air*abc"].descricao_original        # o valor em dólar fica no original
    assert len(r.transacoes) == len(LANCS)


def test_fatura_proxima_e_parcial_sem_total_e_competencia_pelo_vencimento():
    r = ler(_planilha(titulo="Fatura Próxima - Dezembro/2026", rotulo_valor="Valor (parcial)", total=231.92,
                      vencimento=datetime(2026, 12, 10), lancs=LANCS[1:3] + LANCS[6:]))
    assert (r.vencimento, r.competencia_fatura, r.total_informado) == (date(2026, 12, 10), "2026-12", None)
    assert r.avisos == []


def test_total_que_nao_bate_vira_aviso():
    r = ler(_planilha(total=999.99))
    assert r.total_informado == 99999 and any("não bate" in a for a in r.avisos)


def test_valores_em_texto_tambem_sao_lidos():
    lancs = [(d, desc, parc, f"{v:.2f}".replace(".", ","), usd, *resto) for d, desc, parc, v, usd, *resto in LANCS]
    assert sum(t.valor for t in ler(_planilha(lancs=lancs)).transacoes) == sum(t.valor for t in ler(_planilha()).transacoes)


def test_planilha_que_nao_e_fatura_do_itau():
    wb = openpyxl.Workbook()
    wb.active["A1"] = "outra coisa"
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(LeitorErro):
        ler(buf.getvalue())
    assert detectar_leitor("qualquer.xlsx", buf.getvalue()) is None


def test_detectar_leitor_reconhece_a_planilha():
    leitor = detectar_leitor("fatura-fechada-final 1905-outubro2026.xlsx", _planilha())
    assert leitor is not None and leitor.nome == "xlsx_itau_fatura"
    assert len(leitor.ler(_planilha()).transacoes) == len(LANCS)
