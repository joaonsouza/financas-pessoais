"""Reserva de oportunidade: caixinha de investimento cujo saldo fica fora da alocação e entra no rebalanceamento."""
from datetime import date
from decimal import Decimal as D

from app.models import Ativo, Carteira, Caixinha, CotacaoCache, Meta, MetaClasse, OperacaoInvest
from app.services.caixinhas import criar_caixinha
from app.services.invest_carteira import (
    Sugestao, garantir_carteira_padrao, operacoes_por_ativo, plano_oportunidade, resumo_carteira,
    salvar_metas_carteira,
)
from app.services.invest_irpf import relatorio_bens_e_direitos
from tests.factories import nova_conta
from tests.test_rotas_investimentos import AGORA, mercado  # noqa: F401

HOJE = date(2026, 10, 5)


def _cotacao(s, simbolo, preco):
    s.add(CotacaoCache(simbolo=simbolo, data=HOJE, fechamento=D(preco), moeda="BRL", fonte="yahoo", atualizado_em=AGORA))


def cenario(s):
    """Minha: 100 PETR4 (R$ 3.000). Ana: 1 VALE3 (R$ 60) e 37 CDBX11 a R$ 1 (R$ 37) na caixinha de oportunidade."""
    padrao = garantir_carteira_padrao(s)
    ana = Carteira(nome="Carteira da Ana")
    bolsa = nova_conta(s, "NuInvest", tipo="investimento", sl=False)
    c6 = nova_conta(s, "C6 Invest", tipo="investimento", sl=False)
    s.add(ana)
    s.flush()
    oport = criar_caixinha(s, c6, "Reserva de oportunidade", HOJE)
    oport.oportunidade = True
    petr = Ativo(classe="acao", codigo="PETR4", simbolo_cotacao="PETR4.SA", alocacao="acoes", meta_pct=D(100))
    vale = Ativo(classe="acao", codigo="VALE3", simbolo_cotacao="VALE3.SA", alocacao="acoes")
    cdb = Ativo(classe="etf", codigo="CDBX11", simbolo_cotacao="CDBX11.SA", alocacao="renda_fixa")
    s.add_all([petr, vale, cdb, MetaClasse(classe="acoes", meta_pct=D(100))])
    s.flush()
    salvar_metas_carteira(s, ana, {"acoes": D(100), "renda_fixa": D(0), "cripto": D(0)}, {vale.id: D(100)})
    for ativo, qtd, preco, conta, cx, cart in ((petr, 100, 3000, bolsa, None, None), (vale, 1, 6000, c6, None, ana),
                                               (cdb, 37, 100, c6, oport, ana)):
        s.add(OperacaoInvest(ativo_id=ativo.id, tipo="compra", data=date(2026, 8, 3), quantidade=D(qtd),
                             preco_unitario=preco, custodia_conta_id=conta.id,
                             custodia_caixinha_id=cx.id if cx else None, carteira_id=cart.id if cart else None))
    _cotacao(s, "PETR4.SA", "30")
    _cotacao(s, "VALE3.SA", "60")
    _cotacao(s, "CDBX11.SA", "1")
    s.commit()
    return dict(padrao=padrao, ana=ana, c6=c6, oport=oport, petr=petr, vale=vale, cdb=cdb)


def test_oportunidade_fica_fora_da_alocacao_e_na_carteira_da_operacao(session):
    c = cenario(session)
    dela = resumo_carteira(session, HOJE, c["ana"])
    assert [a.ativo.codigo for a in dela.ativos] == ["VALE3"] and dela.patrimonio == 6000
    assert [a.ativo.codigo for a in dela.oportunidade] == ["CDBX11"] and dela.oportunidade_saldo == 3700
    assert dela.patrimonio_com_reserva == 9700
    assert resumo_carteira(session, HOJE, c["padrao"]).oportunidade == []
    tudo = resumo_carteira(session, HOJE)
    assert "CDBX11" not in [a.ativo.codigo for a in tudo.ativos] and tudo.oportunidade_saldo == 3700


def test_reserva_de_emergencia_vence_a_oportunidade(session):
    c = cenario(session)
    meta = Meta(nome="Reserva de Emergência", tipo="reserva_emergencia", valor_alvo=100_000)
    session.add(meta)
    session.flush()
    c["oport"].meta_id = meta.id
    session.commit()
    tudo = resumo_carteira(session, HOJE)
    assert [a.ativo.codigo for a in tudo.reserva] == ["CDBX11"] and tudo.oportunidade == []


def test_irpf_continua_vendo_a_oportunidade(session):
    c = cenario(session)
    assert c["cdb"].id in operacoes_por_ativo(session, carteira=c["ana"], oportunidade="todas")
    rel = relatorio_bens_e_direitos(session, 2026, c["ana"])
    assert "CDBX11" in [i.ativo.codigo for g in rel.grupos for i in g.itens]


def _sg(valor):
    return Sugestao(ativo=Ativo(codigo="X"), valor=valor, quantidade=None)


def test_plano_oportunidade_resgata_ou_aplica_a_diferenca():
    usando = plano_oportunidade(saldo=3700, aporte=11000, usar=True, sugestoes=[_sg(12000)])
    assert (usando.disponivel, usando.comprado, usando.fica, usando.resgatar, usando.aplicar) == (14700, 12000, 2700, 1000, 0)
    sem_usar = plano_oportunidade(saldo=3700, aporte=11000, usar=False, sugestoes=[_sg(10000)])
    assert (sem_usar.disponivel, sem_usar.fica, sem_usar.resgatar, sem_usar.aplicar) == (11000, 4700, 0, 1000)


def test_rebalancear_soma_o_saldo_da_oportunidade(client, db, mercado):  # noqa: F811
    c = cenario(db)
    primeira = client.get(f"/investimentos/rebalancear?carteira={c['ana'].id}").text
    assert 'name="usar_oportunidade" value="1" checked' in primeira and "R$ 37,00" in primeira
    r = client.get(f"/investimentos/rebalancear?carteira={c['ana'].id}&aporte=110&usar_oportunidade=1").text
    # disponível 147: compra 2 VALE3 (120) e ficam 27 na reserva, que tinha 37: resgate 10
    assert "R$ 147,00" in r and "Resgate" in r and "R$ 10,00" in r
    sem = client.get(f"/investimentos/rebalancear?carteira={c['ana'].id}&aporte=110").text
    # sem usar a reserva: compra 1 VALE3 (60), sobram 50 para aplicar
    assert 'name="usar_oportunidade" value="1" checked' not in sem and "Aplique" in sem and "R$ 50,00" in sem


def test_caixinha_marcada_como_oportunidade_so_em_conta_de_investimento(client, db):
    c6 = nova_conta(db, "C6 Invest", tipo="investimento", sl=False)
    corrente = nova_conta(db, "C6 Bank")
    db.commit()
    client.post(f"/contas/{c6.id}/caixinhas", data={"nome": "Reserva de oportunidade", "oportunidade": "1"})
    client.post(f"/contas/{corrente.id}/caixinhas", data={"nome": "Outra", "oportunidade": "1"})
    db.expire_all()
    cx = db.query(Caixinha).filter_by(nome="Reserva de oportunidade").one()
    assert cx.oportunidade and not db.query(Caixinha).filter_by(nome="Outra").one().oportunidade
    assert 'name="oportunidade"' in client.get(f"/contas/{c6.id}").text
    assert 'name="oportunidade"' not in client.get(f"/contas/{corrente.id}").text
    client.post(f"/contas/{c6.id}/caixinhas/{cx.id}/editar", data={"nome": "Reserva de oportunidade"})
    db.expire_all()
    assert not db.get(Caixinha, cx.id).oportunidade


def test_pagina_da_carteira_mostra_o_bloco_da_oportunidade(client, db, mercado):  # noqa: F811
    c = cenario(db)
    dela = client.get(f"/investimentos?carteira={c['ana'].id}").text
    assert "Reserva de oportunidade" in dela and 'id="bloco-oportunidade"' in dela and "R$ 37,00" in dela
    assert f'hx-get="/investimentos/operacoes/nova?ativo_id={c["cdb"].id}"' in dela   # botão de aporte na linha
    padrao = client.get(f"/investimentos?carteira={c['padrao'].id}").text
    assert 'id="bloco-oportunidade"' not in padrao and "Reserva de oportunidade <span class=\"font-medium text-white\">R$ 0,00" in padrao


def test_rebalancear_mostra_a_carteira_antes_e_depois(client, db, mercado):  # noqa: F811
    c = cenario(db)
    r = client.get(f"/investimentos/rebalancear?carteira={c['ana'].id}&aporte=110&usar_oportunidade=1").text
    assert "Carteira antes → depois das compras" in r and "Falta antes → depois" in r
    assert "recebeu R$ 120,00" in r                           # 2 VALE3 na renda variável
