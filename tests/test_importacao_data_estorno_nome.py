"""Compra que o banco muda de data, estorno de compra parcelada e nome da fatura pelo vencimento. Dados fictícios."""
from datetime import date

from sqlalchemy import select

from app.importers.base import ResultadoLeitura, TransacaoBruta
from app.models import Lancamento
from app.services.faturas import obter_ou_criar_fatura
from app.services.importacao import aplicar_importacao, propor_importacao
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta, novo_cartao, novo_lancamento

UUID = "6ac26648-c9e6-4b4f-b005-ecdc69ef7f84"


def _t(d: date, desc: str, valor: int, **kw) -> TransacaoBruta:
    return TransacaoBruta(data=d, descricao=desc, descricao_original=desc, valor=valor, **kw)


def _res(transacoes) -> ResultadoLeitura:
    return ResultadoLeitura(origem="ofx", banco="nubank", tipo_conta="cartao", identificador_conta="X",
                            transacoes=transacoes)


def _cartao(s):
    conta = nova_conta(s, "Nubank")
    cartao = novo_cartao(s, "Cartão Nubank", pagadora=conta, fechamento=23, vencimento=1)
    s.commit()
    return cartao


def _importar(s, cartao, transacoes, nome):
    p = propor_importacao(s, _res(transacoes), cartao, HOJE_TESTE, nome_arquivo=nome, hash_arquivo=nome)
    aplicar_importacao(s, p, None, HOJE_TESTE)
    return p


# ---------------------------------------------------------------- data alterada pelo banco


def test_mesma_transacao_com_data_nova_atualiza_em_vez_de_duplicar(session):
    cartao = _cartao(session)
    _importar(session, cartao, [_t(date(2026, 10, 4), "Google One", -3599, id_externo=UUID)], "a.ofx")
    p = propor_importacao(session, _res([_t(date(2026, 10, 7), "Google One", -3599, id_externo=UUID)]),
                          cartao, HOJE_TESTE, nome_arquivo="b.ofx", hash_arquivo="b.ofx")
    ln = p.linhas[0]
    assert (ln.status, ln.marcada) == ("conciliar", True)
    assert "04/10" in ln.rotulo and "07/10" in ln.rotulo
    aplicar_importacao(session, p, None, HOJE_TESTE)
    google = session.scalars(select(Lancamento).where(Lancamento.descricao == "Google One")).all()
    assert [(l.data, l.valor) for l in google] == [(date(2026, 10, 7), -3599)]


def test_arquivo_mais_velho_nao_volta_a_data(session):
    cartao = _cartao(session)
    _importar(session, cartao, [_t(date(2026, 10, 7), "Google One", -3599, id_externo=UUID)], "b.ofx")
    p = propor_importacao(session, _res([_t(date(2026, 10, 4), "Google One", -3599, id_externo=UUID)]),
                          cartao, HOJE_TESTE)
    assert (p.linhas[0].status, p.linhas[0].marcada) == ("duplicada", False)


def test_fitid_curto_nao_basta_para_dizer_que_e_a_mesma(session):
    cartao = _cartao(session)
    _importar(session, cartao, [_t(date(2026, 10, 4), "Uber", -1500, id_externo="1")], "a.ofx")
    p = propor_importacao(session, _res([_t(date(2026, 10, 7), "Uber", -1500, id_externo="1")]),
                          cartao, HOJE_TESTE)
    assert p.linhas[0].status == "nova"


def test_valor_diferente_nao_e_a_mesma(session):
    cartao = _cartao(session)
    _importar(session, cartao, [_t(date(2026, 10, 4), "Google One", -3599, id_externo=UUID)], "a.ofx")
    p = propor_importacao(session, _res([_t(date(2026, 10, 7), "Google One", -4599, id_externo=UUID)]),
                          cartao, HOJE_TESTE)
    assert p.linhas[0].status == "nova"


# ---------------------------------------------------------------- estorno de compra parcelada


def test_estorno_no_mesmo_arquivo_nao_cria_parcelas_futuras(session):
    cartao = _cartao(session)
    compra = _t(date(2026, 8, 30), "Tiktok Shop *Luizaenxo", -3729, parcela_num=1, parcela_total=2)
    estorno = _t(date(2026, 8, 30), 'Estorno de "Tiktok Shop *Luizaenxo" (Tiktok Shop *Luizaenxo)', 3729)
    p = propor_importacao(session, _res([compra, estorno]), cartao, HOJE_TESTE)
    assert p.linhas[0].parcelas_futuras == []
    assert "estornada" in p.linhas[0].rotulo
    aplicar_importacao(session, p, None, HOJE_TESTE)
    assert session.scalars(select(Lancamento).where(Lancamento.status == "pendente")).all() == []


def test_estorno_em_arquivo_posterior_cancela_parcelas_futuras(session):
    cartao = _cartao(session)
    compra = _t(date(2026, 8, 30), "Tiktok Shop *Luizaenxo", -3729, parcela_num=1, parcela_total=3)
    _importar(session, cartao, [compra], "a.ofx")
    futuras = session.scalars(select(Lancamento).where(Lancamento.status == "pendente")).all()
    assert len(futuras) == 2
    estorno = _t(date(2026, 9, 2), 'Estorno de "Tiktok Shop *Luizaenxo" (Tiktok Shop *Luizaenxo)', 3729)
    p = propor_importacao(session, _res([estorno]), cartao, HOJE_TESTE, nome_arquivo="b.ofx", hash_arquivo="b.ofx")
    assert "2 parcelas futuras canceladas" in p.linhas[0].rotulo
    aplicar_importacao(session, p, None, HOJE_TESTE)
    for l in futuras:
        session.refresh(l)
    assert {l.status for l in futuras} == {"cancelado"}


def test_estorno_de_outra_compra_nao_mexe_em_parcelas(session):
    cartao = _cartao(session)
    compra = _t(date(2026, 8, 30), "Loja A", -3729, parcela_num=1, parcela_total=2)
    estorno = _t(date(2026, 8, 30), 'Estorno de "Loja B" (Loja B)', 3729)
    p = propor_importacao(session, _res([compra, estorno]), cartao, HOJE_TESTE)
    assert p.linhas[0].parcelas_futuras == [2]


# ---------------------------------------------------------------- nome da fatura pelo vencimento


def test_fatura_tem_nome_do_mes_do_vencimento(client, db):
    cartao = _cartao(db)
    fatura = obter_ou_criar_fatura(db, cartao, "2026-10")  # fecha 23/10, vence 01/11
    novo_lancamento(db, cartao, date(2026, 10, 5), -1000, fatura_id=fatura.id)
    db.commit()
    texto = client.get(f"/cartoes/{cartao.id}?fatura=2026-10").text
    assert "Fatura Novembro 2026" in texto or "Fatura novembro" in texto.lower()
    assert "Fatura Outubro 2026" not in texto
