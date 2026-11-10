"""Proposta de importação: deduplicação, conciliação, pagamento de fatura, parcelas. Dados fictícios."""
from datetime import date

import pytest
from sqlalchemy import func, select

from app.importers.base import ResultadoLeitura, TransacaoBruta
from app.importers.normalizacao import normalizar_descricao
from app.models import Fatura, Importacao, Lancamento, Recorrencia, RegraCategoria
from app.services.faturas import obter_ou_criar_fatura, pagar_fatura
from app.services.importacao import calcular_hash, propor_importacao
from tests.conftest import HOJE_TESTE
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento


def _t(dia: int, desc: str, valor: int, mes: int = 10, **kw) -> TransacaoBruta:
    return TransacaoBruta(data=date(2026, mes, dia), descricao=desc, descricao_original=desc, valor=valor, **kw)


def _res(transacoes, tipo_conta="corrente", **kw) -> ResultadoLeitura:
    return ResultadoLeitura(origem="ofx", banco="banco", tipo_conta=tipo_conta, identificador_conta="ID-1",
                            transacoes=transacoes, **kw)


def _stamp(s, conta, t: TransacaoBruta, ocorrencia: int = 1, **kw) -> Lancamento:
    """Lançamento já importado antes (com hash), como o aplicar deixa."""
    h = calcular_hash(conta.id, t.data, t.valor, normalizar_descricao(t.descricao), t.id_externo, ocorrencia)
    lanc = novo_lancamento(s, conta, t.data, t.valor, descricao=t.descricao, **kw)
    lanc.hash_dedup, lanc.id_externo = h, t.id_externo
    s.flush()
    return lanc


def test_linha_nova_com_categoria_da_regra(session):
    conta = nova_conta(session)
    mercado = nova_categoria(session, "Mercado")
    session.add(RegraCategoria(padrao="SUPERLOJA", categoria_id=mercado.id, prioridade=0, criada_de="x"))
    session.flush()
    p = propor_importacao(session, _res([_t(3, "SUPERLOJA CENTRO", -4500)]), conta, HOJE_TESTE)
    assert [ln.status for ln in p.linhas] == ["nova"]
    assert p.linhas[0].categoria_id == mercado.id and p.linhas[0].marcada
    assert p.linhas[0].tipo == "despesa"


def test_propor_nao_grava_nada(session):
    conta = nova_conta(session)
    cartao = novo_cartao(session, "Cartão Teste", pagadora=conta)
    propor_importacao(session, _res([_t(3, "LOJA UM", -1000)], "cartao", competencia_fatura="2026-10"),
                      cartao, HOJE_TESTE)
    assert session.scalar(select(func.count(Lancamento.id))) == 0
    assert session.scalar(select(func.count(Fatura.id))) == 0
    assert session.scalar(select(func.count(Importacao.id))) == 0


def test_tipo_do_arquivo_diferente_do_da_conta_e_erro(session):
    conta = nova_conta(session)
    with pytest.raises(ValueError, match="cartão"):
        propor_importacao(session, _res([_t(3, "LOJA", -100)], "cartao"), conta, HOJE_TESTE)


# ---------------------------------------------------------------- duplicadas

def test_duplicada_por_hash_fica_desmarcada(session):
    conta = nova_conta(session)
    t = _t(3, "PADARIA TESTE", -1500)
    _stamp(session, conta, t)
    p = propor_importacao(session, _res([t]), conta, HOJE_TESTE)
    assert p.linhas[0].status == "duplicada" and not p.linhas[0].marcada


def test_duas_compras_iguais_no_mesmo_dia_sao_distintas(session):
    conta = nova_conta(session)
    a, b = _t(3, "CAFE TESTE", -800), _t(3, "CAFE TESTE", -800)
    p = propor_importacao(session, _res([a, b]), conta, HOJE_TESTE)
    assert [ln.status for ln in p.linhas] == ["nova", "nova"]
    assert p.linhas[0].hash_dedup != p.linhas[1].hash_dedup


def test_reimportacao_sobreposta_so_marca_a_ocorrencia_ja_existente(session):
    conta = nova_conta(session)
    a = _t(3, "CAFE TESTE", -800)
    _stamp(session, conta, a, ocorrencia=1)  # só a 1ª compra foi importada antes
    p = propor_importacao(session, _res([a, _t(3, "CAFE TESTE", -800)]), conta, HOJE_TESTE)
    assert [ln.status for ln in p.linhas] == ["duplicada", "nova"]


def test_duplicada_por_id_externo_data_e_valor(session):
    conta = nova_conta(session)
    antigo = novo_lancamento(session, conta, date(2026, 10, 3), -2000, descricao="Descrição diferente")
    antigo.id_externo = "FIT-1"
    session.flush()
    t = _t(3, "OUTRA DESCRICAO", -2000, id_externo="FIT-1")
    outra = _t(3, "OUTRA DESCRICAO", -2000, id_externo="FIT-1")  # FITID repetido no arquivo: só 1 é duplicada
    p = propor_importacao(session, _res([t, outra]), conta, HOJE_TESTE)
    assert [ln.status for ln in p.linhas] == ["duplicada", "nova"]


def test_arquivo_ja_importado_gera_aviso(session):
    conta = nova_conta(session)
    session.add(Importacao(arquivo_nome="a.ofx", hash_arquivo="abc", tipo="ofx", conta_id=conta.id,
                           criada_em=__import__("datetime").datetime(2026, 9, 20, 10, 0)))
    session.flush()
    p = propor_importacao(session, _res([_t(3, "LOJA", -100)]), conta, HOJE_TESTE, hash_arquivo="abc")
    assert p.arquivo_repetido_em == date(2026, 9, 20)
    assert any("20/09/2026" in a for a in p.avisos)


# ---------------------------------------------------------------- conciliar pendente

def test_pendente_casa_com_valor_igual_e_data_proxima_e_so_uma_vez(session):
    conta = nova_conta(session)
    pend = novo_lancamento(session, conta, date(2026, 10, 10), -5000, status="pendente", descricao="Internet")
    p = propor_importacao(session, _res([_t(12, "PAG INTERNET", -5000), _t(13, "PAG INTERNET", -5000)]),
                          conta, HOJE_TESTE)
    assert p.linhas[0].status == "conciliar" and p.linhas[0].conciliar_com_id == pend.id
    assert p.linhas[1].status == "nova"  # o pendente só pode ser usado uma vez


def test_pendente_mais_proximo_vence(session):
    conta = nova_conta(session)
    longe = novo_lancamento(session, conta, date(2026, 10, 5), -5000, status="pendente", descricao="A")
    perto = novo_lancamento(session, conta, date(2026, 10, 11), -5000, status="pendente", descricao="B")
    p = propor_importacao(session, _res([_t(10, "X", -5000)]), conta, HOJE_TESTE)
    assert p.linhas[0].conciliar_com_id == perto.id and longe.id != perto.id


def test_pendente_fora_de_5_dias_ou_de_outra_conta_nao_casa(session):
    conta, outra = nova_conta(session), nova_conta(session, "Outra")
    novo_lancamento(session, conta, date(2026, 10, 20), -5000, status="pendente")
    novo_lancamento(session, outra, date(2026, 10, 10), -5000, status="pendente")
    p = propor_importacao(session, _res([_t(10, "X", -5000)]), conta, HOJE_TESTE)
    assert p.linhas[0].status == "nova"


def test_pendente_de_recorrencia_aceita_tolerancia_percentual(session):
    conta = nova_conta(session)
    rec = Recorrencia(descricao="Luz", tipo="despesa", conta_id=conta.id, valor=10000, valor_variavel=True,
                      tolerancia_pct=5, dia=10, inicio="2026-10")
    session.add(rec)
    session.flush()
    pend = novo_lancamento(session, conta, date(2026, 10, 10), -10000, status="pendente", descricao="Luz")
    pend.recorrencia_id = rec.id
    session.flush()
    p = propor_importacao(session, _res([_t(10, "CONTA DE LUZ", -10400)]), conta, HOJE_TESTE)
    assert p.linhas[0].status == "conciliar"
    p = propor_importacao(session, _res([_t(10, "CONTA DE LUZ", -11000)]), conta, HOJE_TESTE)
    assert p.linhas[0].status == "nova"


# ---------------------------------------------------------------- transferência

def test_transferencia_entre_contas_do_usuario(session):
    a, b = nova_conta(session, "Conta A"), nova_conta(session, "Conta B")
    entrada = novo_lancamento(session, b, date(2026, 10, 4), 30000, descricao="Recebido")
    p = propor_importacao(session, _res([_t(3, "ENVIO", -30000)]), a, HOJE_TESTE)
    ln = p.linhas[0]
    assert ln.status == "transferencia" and ln.transferencia_com_id == entrada.id
    assert "Conta B" in ln.rotulo
    # fora de ±2 dias não casa
    p = propor_importacao(session, _res([_t(1, "ENVIO", -30000)]), a, HOJE_TESTE)
    assert p.linhas[0].status == "nova"


# ---------------------------------------------------------------- pagamento de fatura

def _cenario_fatura(session, valor: int = 25000):
    corrente = nova_conta(session, "Banco Um")
    cartao = novo_cartao(session, "Mercado Pago Teste", pagadora=corrente)
    fat = obter_ou_criar_fatura(session, cartao, "2026-09")
    novo_lancamento(session, cartao, date(2026, 8, 20), -valor, descricao="Compra", fatura_id=fat.id)
    return corrente, cartao, fat


def test_pagamento_no_extrato_vira_pagamento_da_fatura_do_cartao_pelo_padrao(session):
    corrente, cartao, fat = _cenario_fatura(session)
    t = _t(10, "PIX QRS MERCADO PAG", -25000, mes=9, tipo_sugerido="pagamento_fatura")
    p = propor_importacao(session, _res([t]), corrente, HOJE_TESTE)
    ln = p.linhas[0]
    assert ln.status == "pagamento_fatura" and ln.fatura_id == fat.id and ln.marcada
    assert "Mercado Pago Teste" in ln.rotulo


def test_pagamento_ambiguo_deixa_o_usuario_escolher(session):
    corrente = nova_conta(session, "Banco Um")
    for nome in ("Cartão Alfa", "Cartão Beta"):
        c = novo_cartao(session, nome, pagadora=corrente)
        f = obter_ou_criar_fatura(session, c, "2026-09")
        novo_lancamento(session, c, date(2026, 8, 20), -25000, fatura_id=f.id)
    t = _t(10, "PAGAMENTO DE FATURA", -25000, mes=9, tipo_sugerido="pagamento_fatura")
    p = propor_importacao(session, _res([t]), corrente, HOJE_TESTE)
    ln = p.linhas[0]
    assert ln.status == "pagamento_fatura" and ln.fatura_id is None and not ln.marcada
    assert len(ln.candidatos_fatura) == 2


def test_valor_igual_a_fatura_na_data_do_vencimento_vira_pagamento_sem_padrao(session):
    corrente, cartao, fat = _cenario_fatura(session)
    t = _t(fat.data_vencimento.day, "DEBITO AUTOMATICO", -25000, mes=fat.data_vencimento.month)
    p = propor_importacao(session, _res([t]), corrente, HOJE_TESTE)
    assert p.linhas[0].status == "pagamento_fatura" and p.linhas[0].fatura_id == fat.id
    # mesma quantia bem longe do vencimento: despesa comum
    p = propor_importacao(session, _res([_t(28, "OUTRA COISA", -25000, mes=12)]), corrente, HOJE_TESTE)
    assert p.linhas[0].status == "nova"


def test_pagamento_ja_pendente_no_extrato_e_conciliado(session):
    corrente, cartao, fat = _cenario_fatura(session)
    saida, _ = pagar_fatura(session, fat, 25000, date(2026, 9, 10), efetivado=False)
    t = _t(11, "PIX QRS MERCADO PAG", -25000, mes=9, tipo_sugerido="pagamento_fatura")
    p = propor_importacao(session, _res([t]), corrente, HOJE_TESTE)
    assert p.linhas[0].status == "pagamento_fatura" and p.linhas[0].conciliar_com_id == saida.id


def test_pagamento_no_arquivo_do_cartao_casa_com_a_perna_existente_ou_propoe_o_par(session):
    corrente, cartao, fat = _cenario_fatura(session)
    pg = _t(10, "Pagamento da fatura de setembro", 25000, mes=9, tipo_sugerido="pagamento_fatura")
    res = _res([pg, _t(3, "LOJA UM", -1000)], "cartao", competencia_fatura="2026-10")
    # sem perna: propõe criar o par, contra a fatura aberta de mesmo valor
    p = propor_importacao(session, res, cartao, HOJE_TESTE)
    assert p.linhas[0].status == "pagamento_fatura" and p.linhas[0].fatura_id == fat.id
    assert p.linhas[0].conciliar_com_id is None
    # com a perna já criada pelo extrato (sem hash): casa com ela
    _, entrada = pagar_fatura(session, fat, 25000, date(2026, 9, 10), efetivado=True)
    p = propor_importacao(session, res, cartao, HOJE_TESTE)
    assert p.linhas[0].status == "pagamento_fatura" and p.linhas[0].conciliar_com_id == entrada.id
    # a compra é compra
    assert p.linhas[1].status == "nova"


def test_pagamento_no_cartao_sem_fatura_anterior_usa_a_competencia_anterior(session):
    corrente = nova_conta(session)
    cartao = novo_cartao(session, "Cartão Novo", pagadora=corrente)
    pg = _t(10, "Pagamento da fatura de setembro", 9000, mes=9, tipo_sugerido="pagamento_fatura")
    p = propor_importacao(session, _res([pg], "cartao", competencia_fatura="2026-10"), cartao, HOJE_TESTE)
    ln = p.linhas[0]
    assert ln.status == "pagamento_fatura" and ln.cartao_id == cartao.id and ln.fatura_competencia == "2026-09"


def test_pagamento_no_cartao_sem_competencia_no_arquivo_usa_o_vencimento_mais_proximo(session):
    """OFX do Nubank não informa a competência: o pagamento de 02/10 quita a fatura que venceu em 01/10."""
    corrente = nova_conta(session)
    cartao = novo_cartao(session, "Cartão Novo", pagadora=corrente, fechamento=25, vencimento=1)
    pg = _t(2, "Pagamento recebido", 627566, tipo_sugerido="pagamento_fatura")
    p = propor_importacao(session, _res([pg, _t(3, "LOJA UM", -1000)], "cartao"), cartao, HOJE_TESTE)
    ln = p.linhas[0]
    assert ln.status == "pagamento_fatura" and ln.cartao_id == cartao.id and ln.fatura_competencia == "2026-09"
    assert p.linhas[1].status == "nova"


# ---------------------------------------------------------------- parcelas e fatura de destino

def test_parcela_pendente_casa_e_parcela_nova_propoe_as_futuras(session):
    conta = nova_conta(session)
    pend = novo_lancamento(session, conta, date(2026, 10, 25), -3000, status="pendente",
                           descricao="Loja Fantasia Sapatos")
    pend.parcela_num, pend.parcela_total = 2, 4
    session.flush()
    t_casa = _t(3, "LOJA FANTASIA SAPATOS", -3000, parcela_num=2, parcela_total=4)
    t_nova = _t(4, "OUTRA LOJA CALCADOS", -1000, parcela_num=1, parcela_total=3)
    p = propor_importacao(session, _res([t_casa, t_nova]), conta, HOJE_TESTE)
    assert p.linhas[0].status == "conciliar" and p.linhas[0].conciliar_com_id == pend.id
    assert p.linhas[1].status == "parcela" and p.linhas[1].parcelas_futuras == [2, 3]


def test_parcelas_futuras_ja_existentes_nao_sao_propostas_de_novo(session):
    conta = nova_conta(session)
    for n in (2, 3):
        futura = novo_lancamento(session, conta, date(2026, 10 + n - 1, 4), -1000, status="pendente",
                                 descricao="Outra Loja Calcados")
        futura.parcela_num, futura.parcela_total = n, 3
    session.flush()
    t = _t(4, "OUTRA LOJA CALCADOS", -1000, parcela_num=1, parcela_total=3)
    p = propor_importacao(session, _res([t]), conta, HOJE_TESTE)
    assert p.linhas[0].parcelas_futuras == []


def test_fatura_de_destino_vem_do_pdf_ou_da_data(session):
    corrente = nova_conta(session)
    cartao = novo_cartao(session, "Cartão Teste", pagadora=corrente, fechamento=3)
    t = [_t(5, "LOJA A", -1000, mes=9), _t(20, "LOJA B", -1000, mes=9, parcela_num=7, parcela_total=10)]
    com_pdf = propor_importacao(session, _res(t, "cartao", competencia_fatura="2026-10"), cartao, HOJE_TESTE)
    assert [ln.fatura_competencia for ln in com_pdf.linhas] == ["2026-10", "2026-10"]
    sem_pdf = propor_importacao(session, _res(t, "cartao"), cartao, HOJE_TESTE)
    assert [ln.fatura_competencia for ln in sem_pdf.linhas] == ["2026-10", "2026-10"]
    antigo = propor_importacao(session, _res([_t(2, "LOJA C", -1000, mes=9)], "cartao"), cartao, HOJE_TESTE)
    assert antigo.linhas[0].fatura_competencia == "2026-09"


# ---------------------------------------------------------------- validação e fechamento

def test_total_importado_x_informado_e_sugestao_de_fechamento(session):
    corrente = nova_conta(session)
    cartao = novo_cartao(session, "Cartão Teste", pagadora=corrente, fechamento=3)
    t = [_t(5, "LOJA A", -6000), _t(6, "LOJA B", -4000), _t(7, "ESTORNO", 500),
         _t(2, "Pagamento recebido", 9000, tipo_sugerido="pagamento_fatura")]
    ok = propor_importacao(session, _res(t, "cartao", total_informado=9500, fechamento=date(2026, 10, 7)),
                           cartao, HOJE_TESTE)
    assert ok.total_importado == 9500 and not ok.divergente
    assert ok.sugestao_fechamento == 7 and cartao.dia_fechamento == 3  # sugere, não altera
    ruim = propor_importacao(session, _res(t, "cartao", total_informado=9000), cartao, HOJE_TESTE)
    assert ruim.divergente and any("não bate" in a for a in ruim.avisos)


def test_pagamento_ja_registrado_pelo_arquivo_do_cartao_casa_com_o_extrato(session):
    """A perna na conta corrente criada ao importar a fatura (efetivada, sem hash) não vira despesa nova."""
    corrente, cartao, fat = _cenario_fatura(session)
    saida, _ = pagar_fatura(session, fat, 25000, date(2026, 9, 10), efetivado=True)
    t = _t(10, "PIX QRS MERCADO PAG", -25000, mes=9, tipo_sugerido="pagamento_fatura")
    p = propor_importacao(session, _res([t]), corrente, HOJE_TESTE)
    assert p.linhas[0].status == "pagamento_fatura" and p.linhas[0].conciliar_com_id == saida.id


def test_candidatos_de_fatura_ordenados_pela_proximidade_do_vencimento(session):
    corrente = nova_conta(session, "Banco Um")
    cartao = novo_cartao(session, "Cartão Alfa", pagadora=corrente)
    for comp in ("2026-07", "2026-09", "2027-03"):
        f = obter_ou_criar_fatura(session, cartao, comp)
        novo_lancamento(session, cartao, date(2026, 8, 20), -1000, fatura_id=f.id)
    t = _t(10, "PAGAMENTO", -777, mes=9, tipo_sugerido="pagamento_fatura")
    p = propor_importacao(session, _res([t]), corrente, HOJE_TESTE)
    rotulos = [r for _, r in p.linhas[0].candidatos_fatura]
    assert rotulos[0].endswith("vence 10/09/2026") and rotulos[-1].endswith("/03/2027")

def test_mesma_compra_importada_de_outro_arquivo_e_duplicada(session):
    """A fatura do Itaú em PDF cola as palavras ("BURGERKING"); a planilha do site traz "Burger King".
    Mesma data e valor e a mesma descrição sem espaços: é a compra já importada, não uma nova."""
    conta = nova_conta(session)
    cartao = novo_cartao(session, "Cartão Itaú", pagadora=conta)
    _stamp(session, cartao, _t(2, "BURGERKING", -4090))
    _stamp(session, cartao, _t(20, "PANIFICADORASAYONARA", -1550, mes=9))
    _stamp(session, cartao, _t(3, "MERCADO CENTRAL", -2000))          # parecido, mas de outro valor: nova
    novo_lancamento(session, cartao, date(2026, 10, 4), -999, descricao="Padaria Manual")  # manual: não conta
    p = propor_importacao(session, _res([
        _t(2, "Burger King", -4090), _t(20, "Panificadora Sayonara Ltda", -1550, mes=9),
        _t(3, "Mercado Central", -2500), _t(4, "Padaria Manual", -999), _t(2, "Burger King", -4090),
    ], tipo_conta="cartao", competencia_fatura="2026-10"), cartao, HOJE_TESTE)
    assert [ln.status for ln in p.linhas][:4] == ["duplicada", "duplicada", "nova", "nova"]
    assert p.linhas[4].status == "nova"   # segunda compra igual no mesmo dia: só uma já existia


def test_segunda_compra_igual_no_dia_continua_nova_quando_so_uma_existia(session):
    conta = nova_conta(session)
    cartao = novo_cartao(session, "Cartão Itaú", pagadora=conta)
    _stamp(session, cartao, _t(5, "Cafe", -500))   # importada antes pelo mesmo tipo de arquivo (casa pelo hash)
    p = propor_importacao(session, _res([_t(5, "Cafe", -500), _t(5, "Cafe", -500)], tipo_conta="cartao",
                                        competencia_fatura="2026-10"), cartao, HOJE_TESTE)
    assert [ln.status for ln in p.linhas] == ["duplicada", "nova"]


def test_iof_por_compra_e_repasse_de_iof_sao_o_mesmo(session):
    conta = nova_conta(session)
    cartao = novo_cartao(session, "Cartão Itaú", pagadora=conta)
    _stamp(session, cartao, _t(3, "Repasse de IOF", -5830))            # do PDF: o IOF do mês de uma vez
    iofs = [_t(16, "Iof Compra Internaciona", -5635, mes=9), _t(15, "Iof Compra Internaciona", -22, mes=9),
            _t(13, "Iof Compra Internaciona", -173, mes=9)]
    p = propor_importacao(session, _res(iofs, tipo_conta="cartao", competencia_fatura="2026-10"), cartao, HOJE_TESTE)
    assert {ln.status for ln in p.linhas} == {"duplicada"}

    outro = novo_cartao(session, "Cartão Itaú 2", pagadora=conta)          # ao contrário: planilha antes, PDF depois
    for i, t in enumerate(iofs):
        _stamp(session, outro, t)
    p = propor_importacao(session, _res([_t(3, "Repasse de IOF", -5830), _t(3, "Repasse de IOF", -1)],
                                        tipo_conta="cartao", competencia_fatura="2026-10"), outro, HOJE_TESTE)
    assert [ln.status for ln in p.linhas] == ["duplicada", "nova"]


def test_outra_parcela_da_mesma_compra_nao_e_mudanca_de_data(session):
    """O Nubank repete o FITID e o valor em todas as parcelas da compra: a parcela 5/5 (outra fatura, outra
    data) não pode ser tomada como a 4/5 com a data trocada."""
    conta = nova_conta(session)
    fitid = "6a401552-d121-4685-bb30-2d5e8bae04c6"
    antiga = _stamp(session, conta, _t(24, "Havan Brasilia - Parcela 4/5", -6559, mes=9, id_externo=fitid))
    antiga.parcela_num, antiga.parcela_total = 4, 5
    session.flush()
    t = _t(27, "Havan Brasilia - Parcela 5/5", -6559, id_externo=fitid, parcela_num=5, parcela_total=5)
    p = propor_importacao(session, _res([t]), conta, HOJE_TESTE)
    assert p.linhas[0].status != "conciliar" and p.linhas[0].conciliar_com_id is None


def test_mesma_parcela_com_data_trocada_pelo_banco_continua_conciliando(session):
    conta = nova_conta(session)
    fitid = "6a401552-d121-4685-bb30-2d5e8bae04c6"
    antiga = _stamp(session, conta, _t(24, "Havan Brasilia - Parcela 4/5", -6559, mes=9, id_externo=fitid))
    antiga.parcela_num, antiga.parcela_total = 4, 5
    session.flush()
    t = _t(26, "Havan Brasilia - Parcela 4/5", -6559, mes=9, id_externo=fitid, parcela_num=4, parcela_total=5)
    p = propor_importacao(session, _res([t]), conta, HOJE_TESTE)
    assert p.linhas[0].status == "conciliar" and p.linhas[0].conciliar_com_id == antiga.id


def test_parcela_futura_de_outra_compra_com_o_mesmo_comeco_nao_impede_criar_a_desta(session):
    """Caso real: "Jim.Com* Sophistique 2/2" já existia; "Jim.Com* B R de Olive 1/2" (outro valor) ficou sem a 2/2."""
    conta = nova_conta(session)
    outra = novo_lancamento(session, conta, date(2026, 9, 24), -16912, descricao="Jim.Com* Sophistique")
    outra.parcela_num, outra.parcela_total = 2, 2
    session.flush()
    t = _t(30, "Jim.Com* B R de Olive", -13213, parcela_num=1, parcela_total=2)
    p = propor_importacao(session, _res([t]), conta, HOJE_TESTE)
    assert p.linhas[0].parcelas_futuras == [2]


def test_parcela_futura_com_centavo_de_arredondamento_conta_como_ja_existente(session):
    conta = nova_conta(session)
    futura = novo_lancamento(session, conta, date(2026, 11, 4), -4222, status="pendente", descricao="Shein Ficticia")
    futura.parcela_num, futura.parcela_total = 2, 2
    session.flush()
    t = _t(4, "SHEIN FICTICIA", -4223, parcela_num=1, parcela_total=2)
    assert propor_importacao(session, _res([t]), conta, HOJE_TESTE).linhas[0].parcelas_futuras == []
