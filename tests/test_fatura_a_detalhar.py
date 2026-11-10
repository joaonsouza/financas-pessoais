"""Total parcial da fatura aberta: o total que o app do banco mostra, antes de haver arquivo (o C6 só exporta a
fatura fechada). O que falta lançar entra como "a detalhar" até a importação trazer as compras. Dados fictícios."""
from datetime import date

from app.importers.base import ResultadoLeitura, TransacaoBruta
from app.services.faturas import (
    a_detalhar, definir_total_parcial, limite_disponivel, obter_ou_criar_fatura, total_fatura,
)
from app.services.importacao import aplicar_importacao, desfazer_importacao, propor_importacao
from tests.factories import nova_conta, novo_cartao, novo_lancamento

HOJE = date(2026, 11, 4)


def _cartao(s, limite=500_000):
    return novo_cartao(s, "Cartão C6", pagadora=nova_conta(s), fechamento=25, vencimento=1, limite=limite)


def _compra(s, cartao, fatura, dia, valor, mes=11):
    l = novo_lancamento(s, cartao, date(2026, mes, dia), valor, tipo="despesa", status="pendente")
    l.fatura_id = fatura.id
    s.flush()
    return l


def test_a_detalhar_e_o_total_do_banco_menos_o_que_ja_foi_lancado(session):
    cartao = _cartao(session)
    fatura = obter_ou_criar_fatura(session, cartao, "2026-11")
    _compra(session, cartao, fatura, 2, -25_083)
    definir_total_parcial(session, fatura, 100_743, HOJE)
    assert (a_detalhar(session, fatura), total_fatura(session, fatura)) == (75_660, 100_743)
    # compra lançada depois diminui o que falta detalhar, sem contar duas vezes
    _compra(session, cartao, fatura, 3, -10_000)
    assert (a_detalhar(session, fatura), total_fatura(session, fatura)) == (65_660, 100_743)


def test_lancado_acima_do_total_do_banco_nao_deixa_a_detalhar_negativo(session):
    cartao = _cartao(session)
    fatura = obter_ou_criar_fatura(session, cartao, "2026-11")
    _compra(session, cartao, fatura, 2, -50_000)
    definir_total_parcial(session, fatura, 40_000, HOJE)
    assert (a_detalhar(session, fatura), total_fatura(session, fatura)) == (0, 50_000)


def test_sem_total_do_banco_nada_muda_e_apagar_o_valor_volta_ao_normal(session):
    cartao = _cartao(session)
    fatura = obter_ou_criar_fatura(session, cartao, "2026-11")
    _compra(session, cartao, fatura, 2, -25_083)
    assert (a_detalhar(session, fatura), total_fatura(session, fatura)) == (0, 25_083)
    definir_total_parcial(session, fatura, 100_743, HOJE)
    definir_total_parcial(session, fatura, None, HOJE)
    assert (fatura.total_parcial, fatura.total_parcial_em, total_fatura(session, fatura)) == (None, None, 25_083)


def test_a_detalhar_ocupa_o_limite_do_cartao(session):
    cartao = _cartao(session, limite=500_000)
    fatura = obter_ou_criar_fatura(session, cartao, "2026-11")
    _compra(session, cartao, fatura, 2, -25_083)
    definir_total_parcial(session, fatura, 100_743, HOJE)
    assert limite_disponivel(session, cartao) == 500_000 - 100_743


def _res(transacoes, **kw):
    return ResultadoLeitura(origem="csv", banco="c6", tipo_conta="cartao", identificador_conta=None,
                            transacoes=transacoes, **kw)


def _t(dia, desc, valor, mes=11):
    return TransacaoBruta(data=date(2026, mes, dia), descricao=desc, descricao_original=desc, valor=valor)


def test_importar_a_fatura_fechada_troca_o_a_detalhar_pelas_compras_e_desfazer_devolve(session):
    cartao = _cartao(session)
    fatura = obter_ou_criar_fatura(session, cartao, "2026-11")
    definir_total_parcial(session, fatura, 100_743, HOJE)
    depois = date(2026, 11, 27)   # a fatura fechou em 25/11
    res = _res([_t(5, "Mercado Ficticio", -60_000), _t(20, "Posto Ficticio", -45_000)], competencia_fatura="2026-11")
    p = propor_importacao(session, res, cartao, depois, nome_arquivo="fatura.csv", hash_arquivo="h1")
    assert any("R$ 1.007,43" in a and "R$ 1.050,00" in a for a in p.avisos)
    imp = aplicar_importacao(session, p, None, depois)
    assert (fatura.total_parcial, a_detalhar(session, fatura), total_fatura(session, fatura)) == (None, 0, 105_000)
    desfazer_importacao(session, imp)
    assert (fatura.total_parcial, fatura.total_parcial_em, total_fatura(session, fatura)) == (100_743, HOJE, 100_743)


def test_importacao_com_a_fatura_ainda_aberta_mantem_o_total_do_banco(session):
    """OFX parcial (Nubank) antes do fechamento: o a detalhar só encolhe com as compras que chegaram."""
    cartao = _cartao(session)
    fatura = obter_ou_criar_fatura(session, cartao, "2026-11")
    definir_total_parcial(session, fatura, 100_743, HOJE)
    res = _res([_t(2, "Mercado Ficticio", -60_000)], competencia_fatura="2026-11")
    aplicar_importacao(session, propor_importacao(session, res, cartao, HOJE, hash_arquivo="h2"), None, HOJE)
    assert (fatura.total_parcial, a_detalhar(session, fatura), total_fatura(session, fatura)) == (100_743, 40_743, 100_743)


def test_tela_da_fatura_salva_o_total_do_banco(client, db):
    cartao = _cartao(db)
    db.commit()
    r = client.post(f"/cartoes/{cartao.id}/faturas/2026-11/parcial", data={"valor": "1.007,43"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    fatura = obter_ou_criar_fatura(db, cartao, "2026-11")
    assert fatura.total_parcial == 100_743
    pagina = client.get(f"/cartoes/{cartao.id}?fatura=2026-11").text
    assert "A detalhar" in pagina and "1.007,43" in pagina
    client.post(f"/cartoes/{cartao.id}/faturas/2026-11/parcial", data={"valor": ""})
    db.expire_all()
    assert obter_ou_criar_fatura(db, cartao, "2026-11").total_parcial is None


def test_lista_de_lancamentos_mostra_a_fatura_com_o_a_detalhar(session):
    from app.services.consulta_lancamentos import linhas_da_lista
    cartao = _cartao(session)
    fatura = obter_ou_criar_fatura(session, cartao, "2026-11")   # vence em 01/12
    _compra(session, cartao, fatura, 2, -25_083)
    definir_total_parcial(session, fatura, 100_743, HOJE)
    (linha,) = [l for l in linhas_da_lista(session, "2026-12") if getattr(l, "eh_fatura", False)]
    assert linha.valor == -100_743
    # só com o total digitado, sem nenhuma compra lançada, a fatura também aparece
    vazia = obter_ou_criar_fatura(session, cartao, "2026-12")
    definir_total_parcial(session, vazia, 30_000, HOJE)
    assert [l.valor for l in linhas_da_lista(session, "2027-01") if getattr(l, "eh_fatura", False)] == [-30_000]
