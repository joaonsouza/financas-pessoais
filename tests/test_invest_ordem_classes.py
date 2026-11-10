"""Ordem dos blocos de alocação (maior gap primeiro — gap só para baixo, o que falta para a meta —, ou a escolha do
usuário) e a cor de cada um pelo risco."""
from datetime import date
from decimal import Decimal as D

from app.models import Ativo, CotacaoCache, MetaClasse, OperacaoInvest, set_config
from app.models.config import get_config
from app.services.invest_carteira import (
    CHAVE_ORDEM_CLASSES, classes_na_ordem, gap_alocacao, garantir_carteira_padrao, limpar_ordem_classes,
    mover_classe, ordem_manual, resumo_carteira,
)
from app.services.invest_resumo import CORES_CLASSE, RISCO_CLASSE
from tests.factories import nova_conta
from tests.test_rotas_investimentos import AGORA, mercado  # noqa: F401

HOJE = date(2026, 10, 5)
CLASSES = ["acoes", "renda_fixa", "cripto"]


def _cenario(s, com_cripto=True):
    """Cada classe vale um terço (33,3%); metas 60/30/10: faltam 26,7 em ações; cripto e renda fixa passam (gap 0)."""
    garantir_carteira_padrao(s)
    ativos = [Ativo(classe="acao", codigo="PETR4", simbolo_cotacao="PETR4.SA", alocacao="acoes", meta_pct=D(100)),
              Ativo(classe="etf", codigo="IMAB11", simbolo_cotacao="IMAB11.SA", alocacao="renda_fixa", meta_pct=D(100))]
    if com_cripto:
        ativos.append(Ativo(classe="cripto", codigo="BTC", simbolo_cotacao="BTC-USD", alocacao="cripto", meta_pct=D(100)))
    s.add_all(ativos + [MetaClasse(classe="acoes", meta_pct=D(60)), MetaClasse(classe="renda_fixa", meta_pct=D(30)),
                        MetaClasse(classe="cripto", meta_pct=D(10))])
    s.flush()
    for a in ativos:
        s.add(OperacaoInvest(ativo_id=a.id, tipo="compra", data=date(2026, 8, 3), quantidade=D(10), preco_unitario=1000))
        s.add(CotacaoCache(simbolo=a.simbolo_cotacao, data=HOJE, fechamento=D("10"), moeda="BRL", fonte="yahoo",
                           atualizado_em=AGORA))
    s.commit()


def _ordem(s):
    return [c.classe for c in classes_na_ordem(s, resumo_carteira(s, HOJE))]


def test_padrao_e_o_maior_gap_primeiro(session):
    _cenario(session)
    carteira = resumo_carteira(session, HOJE)
    gaps = {c.classe: round(gap_alocacao(c), 1) for c in carteira.classes}
    assert gaps == {"acoes": D("26.7"), "cripto": 0, "renda_fixa": 0}
    assert _ordem(session) == ["acoes", "renda_fixa", "cripto"]  # gap 0 empata: ordem padrão
    assert ordem_manual(session) == []


def test_abaixo_da_meta_vem_antes_mesmo_com_gap_menor(session):
    _cenario(session)
    session.get(MetaClasse, "acoes").meta_pct = D(5)       # ações com 33,3% e meta 5%: passa 28,3
    session.get(MetaClasse, "renda_fixa").meta_pct = D(60)  # renda fixa com 33,3% e meta 60%: faltam 26,7
    session.get(MetaClasse, "cripto").meta_pct = D(35)      # cripto com 33,3% e meta 35%: falta 1,7
    session.commit()
    assert _ordem(session) == ["renda_fixa", "cripto", "acoes"]


def test_empate_de_gap_segue_a_ordem_padrao(session):
    _cenario(session)
    for classe in CLASSES:
        session.get(MetaClasse, classe).meta_pct = D("33.33")
    session.commit()
    assert _ordem(session) == CLASSES


def test_mover_parte_da_ordem_exibida_e_guarda(session):
    _cenario(session)
    assert _ordem(session) == ["acoes", "renda_fixa", "cripto"]
    mover_classe(session, _ordem(session), "cripto", -1)   # sobe uma posição
    assert _ordem(session) == ["acoes", "cripto", "renda_fixa"]
    assert get_config(session, CHAVE_ORDEM_CLASSES)[:3] == ["acoes", "cripto", "renda_fixa"]
    mover_classe(session, _ordem(session), "cripto", -1)
    assert _ordem(session) == ["cripto", "acoes", "renda_fixa"]


def test_mover_nos_extremos_ou_com_dado_invalido_nao_faz_nada(session):
    _cenario(session)
    atual = _ordem(session)
    mover_classe(session, atual, atual[0], -1)
    mover_classe(session, atual, atual[-1], 1)
    mover_classe(session, atual, "cripto", 0)
    mover_classe(session, atual, "nada", -1)
    assert get_config(session, CHAVE_ORDEM_CLASSES) is None and _ordem(session) == atual


def test_ordem_escolhida_manda_e_o_que_ela_nao_cita_vai_pro_fim_por_gap(session):
    _cenario(session)
    set_config(session, CHAVE_ORDEM_CLASSES, ["renda_fixa", "inexistente", "renda_fixa"])
    assert ordem_manual(session) == ["renda_fixa"]
    assert _ordem(session) == ["renda_fixa", "acoes", "cripto"]       # as demais, por gap


def test_voltar_a_ordem_automatica(session):
    _cenario(session)
    mover_classe(session, _ordem(session), "renda_fixa", -1)
    assert ordem_manual(session)
    limpar_ordem_classes(session)
    assert ordem_manual(session) == [] and _ordem(session) == ["acoes", "renda_fixa", "cripto"]
    limpar_ordem_classes(session)                                    # de novo: sem erro


def test_cores_pelo_risco():
    assert [RISCO_CLASSE[c][0] for c in CLASSES] == ["Médio", "Baixo", "Alto"]
    assert CORES_CLASSE["renda_fixa"] == "#16A34A" and CORES_CLASSE["acoes"] == "#F59E0B" and CORES_CLASSE["cripto"] == "#DC2626"


def _posicoes(html):
    return {c: html.index(f'id="bloco-{c}"') for c in CLASSES}


def test_pagina_mostra_risco_gap_e_ordem_automatica(client, db, mercado):  # noqa: F811
    nova_conta(db)
    _cenario(db)
    html = client.get("/investimentos").text
    assert 'title="Risco baixo"' in html and 'title="Risco médio"' in html and 'title="Risco alto"' in html
    assert "background: #16A34A" in html and "background: #DC2626" in html     # a cor da bolinha segue o risco
    assert ">risco baixo<" not in html and ">risco alto<" not in html          # sem selo de texto ao lado do nome
    assert "ordem automática" in html and "Voltar à ordem automática" not in html
    assert "gap de 26,7%" in html and "Falta para a meta" in html and "acima da meta" in html
    p = _posicoes(html)
    assert p["acoes"] < p["renda_fixa"] < p["cripto"]                  # maior gap primeiro


def test_mover_pela_rota_e_voltar_ao_automatico(client, db, mercado):  # noqa: F811
    nova_conta(db)
    _cenario(db)
    r = client.post("/investimentos/classes/mover?classe=renda_fixa&passo=-1")
    assert r.status_code == 204 and r.headers["HX-Refresh"] == "true"
    html = client.get("/investimentos").text
    p = _posicoes(html)
    assert p["renda_fixa"] < p["acoes"] < p["cripto"]
    assert "Voltar à ordem automática" in html and "Blocos na ordem que você escolheu" in html
    r = client.post("/investimentos/classes/ordem-automatica")
    assert r.status_code == 204 and r.headers["HX-Refresh"] == "true"
    p = _posicoes(client.get("/investimentos").text)
    assert p["acoes"] < p["renda_fixa"] < p["cripto"]


def test_setas_do_primeiro_e_do_ultimo_ficam_desativadas(client, db, mercado):  # noqa: F811
    nova_conta(db)
    _cenario(db)
    html = client.get("/investimentos").text                           # ordem: ações, renda fixa, cripto
    assert "disabled" in html.split('aria-label="Subir Renda variável"')[0].rsplit("<button", 1)[1]
    assert "disabled" in html.split('aria-label="Descer Alternativo"')[0].rsplit("<button", 1)[1]
    assert "disabled" not in html.split('aria-label="Subir Renda fixa"')[0].rsplit("<button", 1)[1]


def test_mover_so_considera_os_blocos_visiveis(client, db, mercado):  # noqa: F811
    nova_conta(db)
    _cenario(db, com_cripto=False)  # ações e renda fixa 50% cada; cripto sem ativo, com meta 10: gaps 10, 0 e 10
    antes = _posicoes(client.get("/investimentos").text)
    assert antes["acoes"] < antes["cripto"] < antes["renda_fixa"]
    client.post("/investimentos/classes/mover?classe=renda_fixa&passo=-1")
    depois = _posicoes(client.get("/investimentos").text)
    assert depois["acoes"] < depois["renda_fixa"] < depois["cripto"]  # troca com o bloco visível de cima
