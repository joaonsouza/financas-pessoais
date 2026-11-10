"""Aplicar e desfazer importação; detector de cobrança recorrente. Dados fictícios."""
from datetime import date

import pytest
from sqlalchemy import func, select

from app.importers.base import ResultadoLeitura, TransacaoBruta
from app.models import Fatura, Importacao, Lancamento, Recorrencia
from app.services.detector_recorrencia import criar_recorrencia_da_sugestao, detectar_recorrencias
from app.services.faturas import obter_ou_criar_fatura, pagar_fatura, total_fatura
from app.services.importacao import Escolhas, aplicar_importacao, desfazer_importacao, propor_importacao
from tests.conftest import HOJE_TESTE
from tests.factories import nova_categoria, nova_conta, novo_cartao, novo_lancamento


def _t(dia: int, desc: str, valor: int, mes: int = 10, **kw) -> TransacaoBruta:
    return TransacaoBruta(data=date(2026, mes, dia), descricao=desc, descricao_original=desc + " (orig)",
                          valor=valor, **kw)


def _res(transacoes, tipo_conta="corrente", **kw) -> ResultadoLeitura:
    return ResultadoLeitura(origem="ofx", banco="banco", tipo_conta=tipo_conta, identificador_conta="ID-FICT-1",
                            transacoes=transacoes, **kw)


def _importar(s, res, conta, escolhas=None, nome="arquivo.ofx"):
    p = propor_importacao(s, res, conta, HOJE_TESTE, nome_arquivo=nome, hash_arquivo="h-" + nome)
    return p, aplicar_importacao(s, p, escolhas, HOJE_TESTE)


def _estado(s) -> list:
    return sorted(
        (l.id, l.conta_id, l.data, l.valor, l.tipo, l.status, l.categoria_id, l.hash_dedup, l.id_externo,
         l.importacao_id, l.transferencia_par_id, l.fatura_id, l.parcela_num)
        for l in s.scalars(select(Lancamento)))


def test_aplica_novas_com_campos_de_importacao_e_aprende_identificador(session):
    conta = nova_conta(session)
    mercado = nova_categoria(session, "Mercado")
    res = _res([_t(3, "SUPERLOJA", -4500, id_externo="F1"), _t(4, "SALARIO FICTICIO", 100000)])
    p = propor_importacao(session, res, conta, HOJE_TESTE, nome_arquivo="a.ofx", hash_arquivo="h1")
    imp = aplicar_importacao(session, p, Escolhas(categorias={0: mercado.id}), HOJE_TESTE)
    assert (imp.qtd_novas, imp.qtd_conciliadas, imp.qtd_duplicadas) == (2, 0, 0)
    assert imp.arquivo_nome == "a.ofx" and imp.hash_arquivo == "h1" and imp.tipo == "ofx"
    compra, salario = session.scalars(select(Lancamento).order_by(Lancamento.id)).all()
    assert compra.tipo == "despesa" and compra.valor == -4500 and compra.status == "efetivado"
    assert compra.categoria_id == mercado.id and compra.id_externo == "F1" and compra.origem == "ofx"
    assert compra.importacao_id == imp.id and compra.hash_dedup and compra.descricao_original == "SUPERLOJA (orig)"
    assert salario.tipo == "receita"
    assert conta.identificador_externo == "ID-FICT-1"


def test_reimportar_o_mesmo_arquivo_nao_cria_duplicados(session):
    conta = nova_conta(session)
    res = _res([_t(3, "CAFE", -800), _t(3, "CAFE", -800), _t(5, "LOJA", -2000, id_externo="X")])
    _importar(session, res, conta)
    p2 = propor_importacao(session, res, conta, HOJE_TESTE, hash_arquivo="h-arquivo.ofx")
    assert [ln.status for ln in p2.linhas] == ["duplicada"] * 3
    imp2 = aplicar_importacao(session, p2, None, HOJE_TESTE)
    assert (imp2.qtd_novas, imp2.qtd_duplicadas) == (0, 3)
    assert session.scalar(select(func.count(Lancamento.id))) == 3


def test_desmarcadas_nao_entram(session):
    conta = nova_conta(session)
    p = propor_importacao(session, _res([_t(3, "A", -100), _t(4, "B", -200)]), conta, HOJE_TESTE)
    imp = aplicar_importacao(session, p, Escolhas(marcadas={1}), HOJE_TESTE)
    assert imp.qtd_novas == 1
    assert [l.valor for l in session.scalars(select(Lancamento))] == [-200]


def test_conciliacao_efetiva_o_pendente_e_desfazer_restaura(session):
    conta = nova_conta(session)
    cat = nova_categoria(session, "Internet")
    pend = novo_lancamento(session, conta, date(2026, 10, 10), -5000, status="pendente",
                           descricao="Internet", categoria=cat)
    antes = _estado(session)
    p, imp = _importar(session, _res([_t(12, "PAG INTERNET", -5000, id_externo="Z"), _t(13, "NOVA", -100)]), conta)
    assert imp.qtd_conciliadas == 1 and imp.qtd_novas == 1
    session.refresh(pend)
    assert pend.status == "efetivado" and pend.data == date(2026, 10, 12) and pend.categoria_id == cat.id
    assert pend.importacao_id == imp.id and pend.id_externo == "Z" and pend.hash_dedup
    desfazer_importacao(session, imp)
    session.refresh(pend)
    assert imp.desfeita
    assert _estado(session) == antes
    assert pend.status == "pendente" and pend.data == date(2026, 10, 10)


def test_desfazer_duas_vezes_e_erro(session):
    conta = nova_conta(session)
    _, imp = _importar(session, _res([_t(3, "A", -100)]), conta)
    desfazer_importacao(session, imp)
    with pytest.raises(ValueError, match="já foi desfeita"):
        desfazer_importacao(session, imp)
    assert session.scalar(select(func.count(Lancamento.id))) == 0


def test_transferencia_cria_o_par_e_desfazer_volta_a_receita(session):
    a, b = nova_conta(session, "Conta A"), nova_conta(session, "Conta B")
    entrada = novo_lancamento(session, b, date(2026, 10, 4), 30000, descricao="Recebido")
    antes = _estado(session)
    _, imp = _importar(session, _res([_t(3, "ENVIO", -30000)]), a)
    session.refresh(entrada)
    saida = session.scalar(select(Lancamento).where(Lancamento.conta_id == a.id))
    assert saida.tipo == entrada.tipo == "transferencia"
    assert saida.transferencia_par_id == entrada.id and entrada.transferencia_par_id == saida.id
    desfazer_importacao(session, imp)
    session.refresh(entrada)
    assert _estado(session) == antes and entrada.tipo == "receita"


def test_pagamento_de_fatura_no_extrato_cria_o_par_quita_e_desfazer_reabre(session):
    corrente = nova_conta(session, "Banco Um")
    cartao = novo_cartao(session, "Mercado Pago Teste", pagadora=corrente)
    fat = obter_ou_criar_fatura(session, cartao, "2026-09")
    novo_lancamento(session, cartao, date(2026, 8, 20), -25000, descricao="Compra", fatura_id=fat.id)
    antes = _estado(session)
    t = _t(10, "PIX QRS MERCADO PAG", -25000, mes=9, tipo_sugerido="pagamento_fatura")
    _, imp = _importar(session, _res([t]), corrente)
    pagamentos = session.scalars(select(Lancamento).where(Lancamento.tipo == "pagamento_fatura")).all()
    assert {(l.conta_id, l.valor) for l in pagamentos} == {(corrente.id, -25000), (cartao.id, 25000)}
    assert all(l.status == "efetivado" and l.fatura_id == fat.id for l in pagamentos)
    session.refresh(fat)
    assert fat.status == "paga"
    # não virou despesa
    assert session.scalar(select(func.count(Lancamento.id)).where(
        Lancamento.conta_id == corrente.id, Lancamento.tipo == "despesa")) == 0
    desfazer_importacao(session, imp)
    session.refresh(fat)
    assert fat.status == "aberta" and _estado(session) == antes


def test_pagamento_ambiguo_com_fatura_escolhida_pelo_usuario(session):
    corrente = nova_conta(session, "Banco Um")
    for nome in ("Cartão Alfa", "Cartão Beta"):
        c = novo_cartao(session, nome, pagadora=corrente)
        f = obter_ou_criar_fatura(session, c, "2026-09")
        novo_lancamento(session, c, date(2026, 8, 20), -25000, fatura_id=f.id)
    t = _t(10, "PAGAMENTO DE FATURA", -25000, mes=9, tipo_sugerido="pagamento_fatura")
    p = propor_importacao(session, _res([t]), corrente, HOJE_TESTE)
    # desmarcada por padrão: sem escolha, nada entra
    assert aplicar_importacao(session, p, None, HOJE_TESTE).qtd_novas == 0
    escolhida = p.linhas[0].candidatos_fatura[1][0]
    imp = aplicar_importacao(session, p, Escolhas(marcadas={0}, faturas={0: escolhida}), HOJE_TESTE)
    assert imp.qtd_novas == 1
    assert session.get(Fatura, escolhida).status == "paga"
    outra = [f for f in session.scalars(select(Fatura)) if f.id != escolhida][0]
    assert outra.status == "aberta"


def _ambiguo(session):
    corrente = nova_conta(session, "Banco Um")
    for nome in ("Cartão Alfa", "Cartão Beta"):
        c = novo_cartao(session, nome, pagadora=corrente)
        f = obter_ou_criar_fatura(session, c, "2026-09")
        novo_lancamento(session, c, date(2026, 8, 20), -25000, fatura_id=f.id)
    t = _t(10, "PAGAMENTO DE FATURA", -25000, mes=9, tipo_sugerido="pagamento_fatura")
    return corrente, propor_importacao(session, _res([t]), corrente, HOJE_TESTE)


def test_pagamento_marcado_sem_fatura_nem_confirmacao_e_recusado_sem_gravar_nada(session):
    corrente, p = _ambiguo(session)
    antes = _estado(session)
    with pytest.raises(ValueError, match="Escolha a fatura paga") as exc:
        aplicar_importacao(session, p, Escolhas(marcadas={0}), HOJE_TESTE)
    assert exc.value.erros.keys() == {0}
    assert _estado(session) == antes
    assert session.scalar(select(func.count(Importacao.id))) == 0
    assert all(f.status == "aberta" for f in session.scalars(select(Fatura)))


def test_pagamento_confirmado_como_nao_e_pagamento_entra_como_despesa(session):
    corrente, p = _ambiguo(session)
    aplicar_importacao(session, p, Escolhas(marcadas={0}, nao_e_pagamento={0}), HOJE_TESTE)
    lanc = session.scalar(select(Lancamento).where(Lancamento.conta_id == corrente.id))
    assert lanc.tipo == "despesa" and lanc.fatura_id is None
    assert all(f.status == "aberta" for f in session.scalars(select(Fatura)))


def test_pagamento_sem_nenhuma_fatura_oferece_criar_e_cria_a_fatura_com_as_duas_pernas(session):
    corrente = nova_conta(session, "Banco Um")
    cartao = novo_cartao(session, "Cartão Alfa", pagadora=corrente)
    t = _t(10, "PAGAMENTO DE FATURA ALFA", -25000, mes=9, tipo_sugerido="pagamento_fatura")
    p = propor_importacao(session, _res([t]), corrente, HOJE_TESTE)
    ln = p.linhas[0]
    assert ln.status == "pagamento_fatura" and not ln.marcada and ln.candidatos_fatura == []
    opcoes = {(cid, comp): rot for cid, comp, rot in ln.criar_fatura}
    assert opcoes[(cartao.id, "2026-09")] == "Criar fatura do cartão Cartão Alfa em 2026-09"
    with pytest.raises(ValueError):
        aplicar_importacao(session, p, Escolhas(marcadas={0}), HOJE_TESTE)
    imp = aplicar_importacao(session, p, Escolhas(marcadas={0}, criar_faturas={0: (cartao.id, "2026-09")}), HOJE_TESTE)
    fat = session.scalar(select(Fatura).where(Fatura.cartao_id == cartao.id, Fatura.competencia == "2026-09"))
    pernas = session.scalars(select(Lancamento).where(Lancamento.tipo == "pagamento_fatura")).all()
    assert {(l.conta_id, l.valor) for l in pernas} == {(corrente.id, -25000), (cartao.id, 25000)}
    assert all(l.fatura_id == fat.id and l.status == "efetivado" for l in pernas)
    assert pernas[0].transferencia_par_id == pernas[1].id and pernas[1].transferencia_par_id == pernas[0].id
    desfazer_importacao(session, imp)
    assert session.scalar(select(Fatura).where(Fatura.id == fat.id)) is None


def test_pagamento_no_arquivo_do_cartao_reaproveita_a_perna_e_nao_duplica(session):
    corrente = nova_conta(session, "Banco Um")
    cartao = novo_cartao(session, "Cartão Teste", pagadora=corrente, fechamento=3, vencimento=10)
    fat = obter_ou_criar_fatura(session, cartao, "2026-09")
    novo_lancamento(session, cartao, date(2026, 8, 20), -25000, descricao="Compra", fatura_id=fat.id)
    saida, entrada = pagar_fatura(session, fat, 25000, date(2026, 9, 10), efetivado=True)
    pg = _t(10, "Pagamento da fatura", 25000, mes=9, tipo_sugerido="pagamento_fatura")
    res = _res([pg, _t(5, "LOJA UM", -3000)], "cartao", competencia_fatura="2026-10",
               total_informado=3000, vencimento=date(2026, 11, 10), fechamento=date(2026, 10, 7))
    p, imp = _importar(session, res, cartao)
    assert (imp.qtd_novas, imp.qtd_conciliadas) == (1, 1)
    assert session.scalar(select(func.count(Lancamento.id)).where(Lancamento.tipo == "pagamento_fatura")) == 2
    session.refresh(entrada)
    assert entrada.hash_dedup and entrada.importacao_id == imp.id
    nova = session.scalar(select(Fatura).where(Fatura.cartao_id == cartao.id, Fatura.competencia == "2026-10"))
    assert nova.total_informado == 3000 and nova.data_vencimento == date(2026, 11, 10)
    assert total_fatura(session, nova) == 3000
    assert cartao.dia_fechamento == 3  # o dia de fechamento do cartão não é sobrescrito
    p2 = propor_importacao(session, res, cartao, HOJE_TESTE, hash_arquivo="h-arquivo.ofx")
    assert [ln.status for ln in p2.linhas] == ["duplicada", "duplicada"]
    desfazer_importacao(session, imp)
    assert session.scalar(select(Fatura).where(Fatura.cartao_id == cartao.id, Fatura.competencia == "2026-10")) is None
    session.refresh(entrada)
    assert entrada.hash_dedup is None


def test_compra_parcelada_cria_as_parcelas_futuras_pendentes_e_desfazer_apaga(session):
    corrente = nova_conta(session)
    cartao = novo_cartao(session, "Cartão Teste", pagadora=corrente, fechamento=3)
    t = _t(20, "LOJA FICTICIA CALCADOS", -1000, mes=9, parcela_num=1, parcela_total=3)
    p, imp = _importar(session, _res([t], "cartao", competencia_fatura="2026-10"), cartao)
    lancs = session.scalars(select(Lancamento).order_by(Lancamento.parcela_num)).all()
    assert [(l.parcela_num, l.status, l.data) for l in lancs] == [
        (1, "efetivado", date(2026, 9, 20)), (2, "pendente", date(2026, 10, 20)), (3, "pendente", date(2026, 11, 20))]
    assert len({l.grupo_parcela_id for l in lancs}) == 1
    assert {session.get(Fatura, l.fatura_id).competencia for l in lancs} == {"2026-10", "2026-11", "2026-12"}
    desfazer_importacao(session, imp)
    assert session.scalar(select(func.count(Lancamento.id))) == 0
    assert session.scalar(select(func.count(Fatura.id))) == 0


def test_sem_parcelas_futuras_quando_o_usuario_desliga(session):
    conta = nova_conta(session)
    t = _t(4, "LOJA FICTICIA CALCADOS", -1000, parcela_num=1, parcela_total=3)
    p = propor_importacao(session, _res([t]), conta, HOJE_TESTE)
    aplicar_importacao(session, p, Escolhas(criar_parcelas_futuras=False), HOJE_TESTE)
    assert session.scalar(select(func.count(Lancamento.id))) == 1


def test_falha_no_meio_nao_grava_nada(session):
    conta = nova_conta(session)
    p = propor_importacao(session, _res([_t(3, "A", -100), _t(4, "B", -200)]), conta, HOJE_TESTE)
    p.linhas[1].status = "conciliar"  # pendente inexistente: estoura no meio
    p.linhas[1].conciliar_com_id = 99999
    with pytest.raises(Exception):
        aplicar_importacao(session, p, None, HOJE_TESTE)
    assert session.scalar(select(func.count(Lancamento.id))) == 0
    assert session.scalar(select(func.count(Importacao.id))) == 0


# ---------------------------------------------------------------- detector

def _mensal(s, conta, desc, valores, mes_inicial=7):
    for i, v in enumerate(valores):
        novo_lancamento(s, conta, date(2026, mes_inicial + i, 9), -v, descricao=desc)


def test_detector_sugere_valor_estavel_em_meses_seguidos_com_custo_anual(session):
    conta = nova_conta(session)
    _mensal(session, conta, "STREAMING FICTICIO", [3990, 3990, 4100])
    _mensal(session, conta, "VARIA MUITO", [1000, 5000, 1000])
    sug = detectar_recorrencias(session, HOJE_TESTE)
    assert [x.descricao for x in sug] == ["STREAMING FICTICIO"]
    x = sug[0]
    assert x.meses == ["2026-07", "2026-08", "2026-09"] and x.valor == 4100 and x.custo_anual == 49200
    assert x.inicio == "2026-10"


def test_detector_ignora_um_mes_so_meses_nao_seguidos_parcelas_e_ja_recorrentes(session):
    conta = nova_conta(session)
    novo_lancamento(session, conta, date(2026, 9, 9), -3000, descricao="UM MES SO")
    novo_lancamento(session, conta, date(2026, 7, 9), -3000, descricao="PULA MES")
    novo_lancamento(session, conta, date(2026, 9, 9), -3000, descricao="PULA MES")
    for i in range(3):
        l = novo_lancamento(session, conta, date(2026, 7 + i, 9), -1000, descricao="LOJA PARCELADA")
        l.parcela_num, l.parcela_total = i + 1, 10
    _mensal(session, conta, "JA E RECORRENTE", [2000, 2000])
    session.add(Recorrencia(descricao="Ja e recorrente", tipo="despesa", conta_id=conta.id, valor=2000,
                            dia=9, inicio="2026-07"))
    session.flush()
    assert detectar_recorrencias(session, HOJE_TESTE) == []


def test_detector_nao_sugere_sequencia_antiga(session):
    conta = nova_conta(session)
    _mensal(session, conta, "ANTIGA", [3000, 3000], mes_inicial=1)
    assert detectar_recorrencias(session, HOJE_TESTE) == []


def test_virar_assinatura_cria_recorrencia_com_inicio_no_mes_seguinte(session):
    conta = nova_conta(session)
    _mensal(session, conta, "STREAMING FICTICIO", [3990, 3990])
    sug = detectar_recorrencias(session, HOJE_TESTE)[0]
    rec = criar_recorrencia_da_sugestao(session, sug, HOJE_TESTE)
    assert rec.eh_assinatura and rec.valor == 3990 and rec.inicio == "2026-09"
    assert session.scalar(select(func.count(Lancamento.id)).where(Lancamento.recorrencia_id == rec.id)) >= 1


def test_desfazer_importacao_com_par_criado_por_outra_importacao(session):
    """Transferência entre extratos de dois bancos: a 2ª importação pareia com um lançamento da 1ª.
    Desfazer a 1ª não pode quebrar por chave estrangeira; o lançamento da 2ª fica sem par."""
    a = nova_conta(session, "Banco A")
    b = nova_conta(session, "Banco B")
    _, imp_a = _importar(session, _res([_t(3, "TRANSF PARA B", -50000)]), a, nome="a.ofx")
    _, imp_b = _importar(session, _res([_t(3, "TRANSF DE A", 50000)]), b, nome="b.ofx")
    saida = session.scalars(select(Lancamento).where(Lancamento.importacao_id == imp_a.id)).one()
    entrada = session.scalars(select(Lancamento).where(Lancamento.importacao_id == imp_b.id)).one()
    entrada.transferencia_par_id, saida.transferencia_par_id = saida.id, entrada.id
    session.flush()

    desfazer_importacao(session, imp_a)

    assert session.get(Lancamento, saida.id) is None
    session.refresh(entrada)
    assert entrada.transferencia_par_id is None and imp_a.desfeita
