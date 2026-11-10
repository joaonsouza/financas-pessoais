"""OFX de fatura fechada do Nubank: total oficial (LEDGERBAL), saldo da fatura anterior e pagamento. Dados fictícios."""
from datetime import date

from sqlalchemy import select

from app.importers.ofx import ler
from app.models import Fatura, Lancamento
from app.services.faturas import obter_ou_criar_fatura, total_fatura
from app.services.importacao import aplicar_importacao, propor_importacao
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta, novo_cartao, novo_lancamento


def _trn(data: str, valor: str, memo: str, fitid: str) -> str:
    return (f"<STMTTRN>\n<TRNTYPE>{'CREDIT' if not valor.startswith('-') else 'DEBIT'}\n"
            f"<DTPOSTED>{data}000000[-3:BRT]\n<TRNAMT>{valor}\n<FITID>{fitid}\n<MEMO>{memo}\n</STMTTRN>\n")


def _ofx(transacoes: list[str], *, dtend="20260924", dtserver="20261007", saldo="-150.00") -> bytes:
    return (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nCHARSET:1252\n\n<OFX>\n<SIGNONMSGSRSV1><SONRS>\n"
        f"<DTSERVER>{dtserver}191057[0:GMT]\n<LANGUAGE>POR\n<FI><ORG>NU PAGAMENTOS S.A.<FID>260</FI>\n"
        "</SONRS></SIGNONMSGSRSV1>\n<CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CURDEF>BRL\n"
        "<CCACCTFROM><ACCTID>conta-ficticia</CCACCTFROM>\n"
        f"<BANKTRANLIST>\n<DTSTART>20260825000000[-3:BRT]\n<DTEND>{dtend}000000[-3:BRT]\n"
        + "".join(transacoes)
        + f"</BANKTRANLIST>\n<LEDGERBAL><BALAMT>{saldo}\n<DTASOF>{dtend}000000[-3:BRT]\n</LEDGERBAL>\n"
        "</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>\n</OFX>\n"
    ).encode("cp1252")


TRANSACOES = [
    _trn("20260901", "-400.00", "Valor pendente do mês anterior", "f1"),
    _trn("20260902", "500.00", "Pagamento recebido", "f2"),
    _trn("20260905", "-100.00", "Mercado Fictício", "f3"),
    _trn("20260910", "-60.00", "Posto Fictício", "f4"),
    _trn("20260912", "10.00", "Estorno de \"Assinatura\"", "f5"),
]


# ---------------------------------------------------------------- leitor


def test_le_total_e_fechamento_da_fatura_fechada():
    r = ler(_ofx(TRANSACOES))
    assert (r.total_informado, r.fechamento, r.competencia_fatura) == (15000, date(2026, 9, 23), "2026-09")
    tipos = {t.descricao_original: t.tipo_sugerido for t in r.transacoes}
    assert tipos["Valor pendente do mês anterior"] == "saldo_anterior"
    assert tipos["Pagamento recebido"] == "pagamento_fatura"
    assert tipos["Mercado Fictício"] is None


def test_fatura_ainda_aberta_nao_informa_total_mas_diz_a_competencia():
    """Aberta, o saldo é parcial; o período do arquivo (DTEND menos um dia) ainda diz de que fatura são as linhas."""
    r = ler(_ofx(TRANSACOES, dtend="20261025", dtserver="20261007"))
    assert (r.total_informado, r.fechamento, r.competencia_fatura) == (None, None, "2026-10")


def test_fatura_aberta_poe_a_compra_do_primeiro_dia_do_periodo_na_fatura_do_arquivo(session):
    """Caso real: período 25/10 a 24/11, cartão cadastrado com fechamento dia 25. A compra de 25/10 é da
    fatura de novembro (a de outubro fechou em 24/10), não da de outubro pelo dia cadastrado."""
    conta = nova_conta(session, "Nubank")
    cartao = novo_cartao(session, "Cartão Nubank", pagadora=conta, fechamento=25, vencimento=1)
    session.commit()
    ofx = _ofx([_trn("20261025", "-76.94", "iFood Fictício", "g1"), _trn("20261104", "-35.99", "Google Fictício", "g2")],
               dtend="20261124", dtserver="20261104")
    p = propor_importacao(session, ler(ofx), cartao, HOJE_TESTE)
    assert {ln.transacao.descricao_original: ln.fatura_competencia for ln in p.linhas} == {
        "iFood Fictício": "2026-11", "Google Fictício": "2026-11"}


# ---------------------------------------------------------------- proposta e aplicação


def _cartao(s):
    conta = nova_conta(s, "Nubank")
    cartao = novo_cartao(s, "Cartão Nubank", pagadora=conta, fechamento=23, vencimento=1)
    s.commit()
    return conta, cartao


def _por_desc(proposta):
    return {ln.transacao.descricao_original: ln for ln in proposta.linhas}


def test_saldo_anterior_entra_ignorado_e_pagamento_quita_a_fatura_anterior(session):
    _, cartao = _cartao(session)
    p = propor_importacao(session, ler(_ofx(TRANSACOES)), cartao, HOJE_TESTE)
    linhas = _por_desc(p)
    saldo, pagamento = linhas["Valor pendente do mês anterior"], linhas["Pagamento recebido"]
    assert (saldo.status, saldo.marcada, saldo.ignorar) == ("nova", True, True)
    assert "não é compra" in saldo.rotulo
    # a fatura paga (2026-08) ainda não existe: é criada na aplicação
    assert (pagamento.status, pagamento.marcada, pagamento.fatura_competencia) == ("pagamento_fatura", True, "2026-08")
    # soma das compras confere com o total do banco: sem aviso de divergência
    assert (p.total_importado, p.total_informado, p.divergente) == (15000, 15000, False)

    aplicar_importacao(session, p, None, HOJE_TESTE)
    fatura = session.scalars(select(Fatura).where(Fatura.competencia == "2026-09")).one()
    assert total_fatura(session, fatura) == 15000
    assert fatura.total_informado == 15000
    ignorados = session.scalars(select(Lancamento.descricao).where(Lancamento.ignorar.is_(True))).all()
    assert ignorados == ["Valor pendente do mês anterior"]
    agosto = session.scalars(select(Fatura).where(Fatura.competencia == "2026-08")).one()
    assert agosto.status == "paga"


def test_pagamento_vai_para_a_fatura_anterior_quando_ela_existe(session):
    _, cartao = _cartao(session)
    agosto = obter_ou_criar_fatura(session, cartao, "2026-08")
    # total de agosto (400) diferente do pagamento (500): a fatura sai da competência do arquivo, não do valor
    novo_lancamento(session, cartao, date(2026, 8, 10), -40000, descricao="Compra de agosto", fatura_id=agosto.id)
    # fatura de setembro também aberta: antes o pagamento ficava desmarcado, pedindo escolha
    setembro = obter_ou_criar_fatura(session, cartao, "2026-09")
    novo_lancamento(session, cartao, date(2026, 9, 5), -100, descricao="Outra", fatura_id=setembro.id)
    session.commit()
    p = propor_importacao(session, ler(_ofx(TRANSACOES)), cartao, HOJE_TESTE)
    pagamento = _por_desc(p)["Pagamento recebido"]
    assert (pagamento.status, pagamento.marcada, pagamento.fatura_id) == ("pagamento_fatura", True, agosto.id)

    aplicar_importacao(session, p, None, HOJE_TESTE)
    session.refresh(agosto)
    assert agosto.status == "paga"


def test_divergencia_com_o_total_do_banco_e_avisada(session):
    _, cartao = _cartao(session)
    p = propor_importacao(session, ler(_ofx(TRANSACOES, saldo="-161.65")), cartao, HOJE_TESTE)
    assert p.divergente


# ---------------------------------------------------------------- tela da fatura


def test_tela_da_fatura_avisa_quando_total_do_banco_nao_bate(client, db):
    _, cartao = _cartao(db)
    fatura = obter_ou_criar_fatura(db, cartao, "2026-09")
    novo_lancamento(db, cartao, date(2026, 9, 5), -15000, descricao="Mercado", fatura_id=fatura.id)
    fatura.total_informado = 16165
    db.commit()
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-09").text
    assert "O banco informa R$ 161,65" in texto
    assert "R$ 11,65" in texto


def test_tela_da_fatura_sem_aviso_quando_bate(client, db):
    _, cartao = _cartao(db)
    fatura = obter_ou_criar_fatura(db, cartao, "2026-09")
    novo_lancamento(db, cartao, date(2026, 9, 5), -15000, descricao="Mercado", fatura_id=fatura.id)
    fatura.total_informado = 15000
    db.commit()
    assert "O banco informa" not in client.get(f"/cartoes/{cartao.id}?fatura=2026-09").text
