"""A reserva de emergência na carteira consolidada: aparece na lista, mas fora da alocação e do rebalanceamento."""
from datetime import date
from decimal import Decimal as D

from app.models import Ativo, CotacaoCache, Meta, MetaClasse, OperacaoInvest
from app.services.caixinhas import criar_caixinha
from app.services.invest_carteira import (
    garantir_carteira_padrao, locais_da_reserva, operacoes_por_ativo, resumo_carteira, sugerir_aporte,
)
from app.services.invest_conciliacao import conciliacao
from tests.factories import nova_conta
from tests.test_rotas_investimentos import AGORA, mercado  # noqa: F401

HOJE = date(2026, 10, 5)


def _cotacao(s, simbolo, preco):
    s.add(CotacaoCache(simbolo=simbolo, data=HOJE, fechamento=D(preco), moeda="BRL", fonte="yahoo", atualizado_em=AGORA))


def _op(s, ativo, qtd, preco, conta, caixinha=None):
    s.add(OperacaoInvest(ativo_id=ativo.id, tipo="compra", data=date(2026, 8, 3), quantidade=D(qtd),
                         preco_unitario=preco, custodia_conta_id=conta.id,
                         custodia_caixinha_id=caixinha.id if caixinha else None))


def cenario(s):
    """Bolsa: 100 PETR4 a R$ 30 (R$ 3.000) numa conta; reserva: 50 RESV3 a R$ 20 (R$ 1.000) na caixinha da meta."""
    garantir_carteira_padrao(s)
    bolsa = nova_conta(s, "NuInvest", tipo="investimento", sl=False)
    reserva_conta = nova_conta(s, "Reserva de Emergência", tipo="investimento", sl=False)
    meta = Meta(nome="Reserva de Emergência", tipo="reserva_emergencia", valor_alvo=3_600_000, conta_id=reserva_conta.id)
    s.add(meta)
    s.flush()
    turbo = criar_caixinha(s, reserva_conta, "Caixinha Turbo", HOJE, meta_id=meta.id)
    outra = criar_caixinha(s, reserva_conta, "Outra", HOJE)  # caixinha da mesma conta, mas de outra meta
    petr = Ativo(classe="acao", codigo="PETR4", simbolo_cotacao="PETR4.SA", alocacao="acoes", meta_pct=D(100))
    resv = Ativo(classe="acao", codigo="RESV3", simbolo_cotacao="RESV3.SA", alocacao="acoes")
    s.add_all([petr, resv, MetaClasse(classe="acoes", meta_pct=D(100))])
    s.flush()
    _op(s, petr, 100, 3000, bolsa)
    _op(s, resv, 50, 2000, reserva_conta, turbo)
    _cotacao(s, "PETR4.SA", "30")
    _cotacao(s, "RESV3.SA", "20")
    s.commit()
    return dict(bolsa=bolsa, reserva_conta=reserva_conta, meta=meta, turbo=turbo, outra=outra, petr=petr, resv=resv)


def test_locais_da_reserva(session):
    c = cenario(session)
    assert locais_da_reserva(session) == ({c["turbo"].id}, {c["reserva_conta"].id})


def test_consolidada_lista_a_reserva_fora_da_alocacao(session):
    cenario(session)
    tudo = resumo_carteira(session, HOJE)
    assert [a.ativo.codigo for a in tudo.ativos] == ["PETR4"]            # as classes só têm o investido
    assert tudo.patrimonio == 300_000                                    # percentuais e metas seguem só o investido
    assert [a.ativo.codigo for a in tudo.reserva] == ["RESV3"]
    assert tudo.reserva_saldo == 100_000 and tudo.patrimonio_com_reserva == 400_000
    assert tudo.reserva_alvo == 3_600_000 and round(tudo.reserva_pct, 1) == D("2.8")
    assert tudo.reserva[0].ideal_pct == 0 and tudo.reserva[0].comprar is False
    assert tudo.ativos[0].pct_carteira == 100                            # PETR4 é 100% do investido, sem diluir


def test_reserva_nao_entra_no_rebalanceamento(session):
    cenario(session)
    tudo = resumo_carteira(session, HOJE)
    assert [s.ativo.codigo for s in sugerir_aporte(tudo, 50_000)] == ["PETR4"]


def test_carteira_especifica_nao_mostra_a_reserva(session):
    c = cenario(session)
    padrao = garantir_carteira_padrao(session)
    minha = resumo_carteira(session, HOJE, padrao)
    assert minha.reserva == [] and minha.patrimonio == 300_000 and [a.ativo.codigo for a in minha.ativos] == ["PETR4"]


def test_reserva_entra_nos_locais_para_a_conciliacao_das_caixinhas(session):
    c = cenario(session)
    tudo = resumo_carteira(session, HOJE)
    assert tudo.por_local[(c["reserva_conta"].id, c["turbo"].id)] == 100_000
    assert tudo.por_local[(c["bolsa"].id, None)] == 300_000
    assert tudo.por_corretora["Reserva de Emergência · Caixinha Turbo"] == 100_000
    linha = next(l for l in conciliacao(session, HOJE) if l.caixinha and l.caixinha.id == c["turbo"].id)
    assert linha.posicoes == 100_000


def test_lote_sem_caixinha_na_conta_da_reserva_e_reserva_mas_outra_caixinha_nao(session):
    c = cenario(session)
    _op(session, c["resv"], 10, 2000, c["reserva_conta"])                  # sem caixinha: conta da reserva
    _op(session, c["petr"], 10, 3000, c["reserva_conta"], c["outra"])      # caixinha que não é da reserva
    session.commit()
    tudo = resumo_carteira(session, HOJE)
    assert tudo.reserva_saldo == 120_000                                   # 50 + 10 RESV3 a R$ 20
    assert tudo.patrimonio == 330_000                                      # 100 + 10 PETR4 a R$ 30


def test_mesmo_ativo_com_lotes_na_reserva_e_fora_se_separa(session):
    c = cenario(session)
    _op(session, c["resv"], 20, 2000, c["bolsa"])
    session.commit()
    tudo = resumo_carteira(session, HOJE)
    assert sorted(a.ativo.codigo for a in tudo.ativos) == ["PETR4", "RESV3"]
    assert next(a for a in tudo.ativos if a.ativo.codigo == "RESV3").saldo == 40_000   # só os 20 fora da reserva
    assert tudo.reserva_saldo == 100_000 and tudo.patrimonio == 340_000


def test_sem_meta_de_reserva_nada_muda(session):
    c = cenario(session)
    c["meta"].ativa = False
    session.commit()
    tudo = resumo_carteira(session, HOJE)
    assert tudo.reserva == [] and tudo.patrimonio == 400_000
    assert sorted(a.ativo.codigo for a in tudo.ativos) == ["PETR4", "RESV3"]


def test_operacoes_por_ativo_modos(session):
    cenario(session)
    assert sorted(operacoes_por_ativo(session)) and len(operacoes_por_ativo(session)) == 1
    assert len(operacoes_por_ativo(session, reserva="so")) == 1
    assert len(operacoes_por_ativo(session, reserva="todas")) == 2


def test_pagina_consolidada_lista_a_reserva_e_a_carteira_especifica_nao(client, db, mercado):  # noqa: F811
    nova_conta(db)
    c = cenario(db)
    padrao = garantir_carteira_padrao(db)
    texto = client.get("/investimentos").text
    assert 'id="titulo-reserva-carteira"' in texto and "não entra na alocação por classe nem no rebalanceamento" in texto
    assert f'/investimentos/ativos/{c["resv"].id}' in texto and "Patrimônio com a reserva" in texto
    assert "R$ 4.000,00" in texto and "R$ 1.000,00" in texto                 # com a reserva / a reserva
    assert 'aria-controls="bloco-reserva"' in texto and 'id="bloco-reserva" x-show="aberto"' in texto   # recolhível
    reserva = texto.split('id="bloco-reserva"')[1].split("</section>")[0]
    assert f'hx-get="/investimentos/operacoes/nova?ativo_id={c["resv"].id}"' in reserva   # botão de aporte na linha
    especifica = client.get(f"/investimentos?carteira={padrao.id}").text
    assert 'id="titulo-reserva-carteira"' not in especifica and "Patrimônio com a reserva" not in especifica


def test_reserva_tem_destaque_de_fora_da_meta(client, db, mercado):  # noqa: F811
    nova_conta(db)
    cenario(db)
    texto = client.get("/investimentos").text
    assert "Fora da meta · meta da reserva R$ 36.000,00" in texto               # a tag traz o valor da meta
    assert "bg-expense text-white" in texto and "border-expense/60" in texto    # cor de alerta
    assert "R$ 1.000,00 de R$ 36.000,00 (2,8%)" in texto                        # saldo da reserva contra a meta


def test_reserva_abaixo_da_meta_vem_antes_de_todas_as_classes(client, db, mercado):  # noqa: F811
    nova_conta(db)
    cenario(db)                                            # reserva R$ 1.000 de R$ 36.000
    html = client.get("/investimentos").text
    assert resumo_carteira(db, HOJE).reserva_abaixo_da_meta is True
    assert html.index('id="titulo-reserva-carteira"') < html.index('id="bloco-acoes"')
    assert "no topo enquanto estiver abaixo da meta" in html


def test_reserva_que_bateu_a_meta_vai_para_depois_das_classes(client, db, mercado):  # noqa: F811
    nova_conta(db)
    c = cenario(db)
    c["meta"].valor_alvo = 50_000                          # a reserva (R$ 1.000) passa da meta de R$ 500
    db.commit()
    assert resumo_carteira(db, HOJE).reserva_abaixo_da_meta is False
    html = client.get("/investimentos").text
    assert html.index('id="bloco-acoes"') < html.index('id="titulo-reserva-carteira"')
    assert "no topo enquanto estiver abaixo da meta" not in html
