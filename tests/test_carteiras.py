"""Carteiras com estratégia própria e a consolidada (metas derivadas, ponderadas pelo patrimônio)."""
from datetime import date
from decimal import Decimal as D

from sqlalchemy import func, select, text

from app.models import Ativo, AtivoDesejado, Carteira, CotacaoCache, MetaClasse, OperacaoInvest
from app.services.invest_carteira import (
    carteira_da_conta, carteiras_do_desejo, garantir_carteira_padrao, marcar_desejado, metas_da_carteira,
    migrar_ativos_desejados,
    resumo_carteira, salvar_metas_carteira,
)
from tests.conftest import HOJE_TESTE
from tests.factories import nova_conta
from tests.test_mercado_provedores import sem_rede_real  # noqa: F401
from tests.test_rotas_investimentos import AGORA, mercado, op_form  # noqa: F401

HOJE = date(2026, 10, 5)


def _cotacao(s, simbolo, preco):
    s.add(CotacaoCache(simbolo=simbolo, data=HOJE, fechamento=D(preco), moeda="BRL", fonte="yahoo",
                       atualizado_em=AGORA))


def cenario(s):
    """Minha carteira: 100 PETR4 (meta 100% de RV). Ana: 10 VALE3 (meta 50% RV / 50% RF, VALE3 100% de RV)."""
    padrao = garantir_carteira_padrao(s)
    ana = Carteira(nome="Carteira da Ana")
    s.add(ana)
    petr = Ativo(classe="acao", codigo="PETR4", simbolo_cotacao="PETR4.SA", alocacao="acoes", meta_pct=D(100))
    vale = Ativo(classe="acao", codigo="VALE3", simbolo_cotacao="VALE3.SA", alocacao="acoes")
    s.add_all([petr, vale])
    s.flush()
    s.add_all([MetaClasse(classe="acoes", meta_pct=D(100)),
               OperacaoInvest(ativo_id=petr.id, tipo="compra", data=date(2026, 8, 3), quantidade=D(100), preco_unitario=3000),
               OperacaoInvest(ativo_id=vale.id, tipo="compra", data=date(2026, 8, 3), quantidade=D(10), preco_unitario=6000,
                              carteira_id=ana.id)])
    s.flush()
    salvar_metas_carteira(s, ana, {"acoes": D(50), "renda_fixa": D(50), "cripto": D(0)}, {vale.id: D(100)})
    _cotacao(s, "PETR4.SA", "30")   # 3.000
    _cotacao(s, "VALE3.SA", "100")  # 1.000
    s.commit()
    return padrao, ana, petr, vale


def test_cada_carteira_so_ve_as_suas_operacoes_e_metas(session):
    padrao, ana, petr, vale = cenario(session)
    minha = resumo_carteira(session, HOJE, padrao)
    dela = resumo_carteira(session, HOJE, ana)
    assert [a.ativo.codigo for a in minha.ativos] == ["PETR4"] and minha.patrimonio == 300000
    assert [a.ativo.codigo for a in dela.ativos] == ["VALE3"] and dela.patrimonio == 100000
    assert {c.classe: c.meta_pct for c in dela.classes}["renda_fixa"] == 50
    assert dela.ativos[0].ideal_pct == 50


def test_consolidada_soma_tudo_com_as_metas_ponderadas_pelo_patrimonio(session):
    cenario(session)
    tudo = resumo_carteira(session, HOJE)
    assert tudo.patrimonio == 400000
    # minha: 3.000 × 100% RV; Ana: 1.000 × 50% RV / 50% RF  =>  RV 3.500 (87,5%), RF 500 (12,5%)
    metas = {c.classe: c.meta_pct for c in tudo.classes}
    assert metas["acoes"] == D("87.5") and metas["renda_fixa"] == D("12.5")
    ideal = {a.ativo.codigo: round(a.ideal_pct, 6) for a in tudo.ativos}
    assert ideal == {"PETR4": D(75), "VALE3": D("12.5")}  # cada carteira na meta dela: VALE3 só falta a RF


def test_consolidada_carteira_sem_metas_entra_com_a_alocacao_de_hoje(session):
    padrao, ana, petr, vale = cenario(session)
    salvar_metas_carteira(session, ana, {"acoes": D(0), "renda_fixa": D(0), "cripto": D(0)}, {vale.id: D(0)})
    session.commit()
    tudo = resumo_carteira(session, HOJE)
    assert {c.classe: c.meta_pct for c in tudo.classes} == {"acoes": D(100)}
    assert {a.ativo.codigo: round(a.ideal_pct, 6) for a in tudo.ativos} == {"PETR4": D(75), "VALE3": D(25)}


def test_carteira_sugerida_pela_conta(session):
    padrao = garantir_carteira_padrao(session)
    c6 = nova_conta(session, "C6 Invest", tipo="investimento", sl=False)
    ana = Carteira(nome="Ana", contas=str(c6.id))
    session.add(ana)
    session.flush()
    assert carteira_da_conta(session, c6.id) is ana and carteira_da_conta(session, 999) is padrao


def test_rotas_selecionam_carteira_e_salvam_metas_dela(client, db, mercado):  # noqa: F811
    nova_conta(db)
    padrao, ana, petr, vale = cenario(db)
    texto = client.get("/investimentos").text
    assert "Consolidada" in texto and "Carteira da Ana" in texto and "ponderadas pelo patrimônio" in texto
    assert f'name="ativo_{vale.id}"' not in texto  # consolidada: metas só para ler
    dela = client.get(f"/investimentos?carteira={ana.id}").text
    assert f'name="ativo_{vale.id}"' in dela and "PETR4" not in dela.split("Meus ativos")[1].split("Valor por local")[0]
    r = client.post("/investimentos/metas", data={"carteira_id": str(ana.id), "classe_acoes": "60",
                                                  "classe_renda_fixa": "40", "classe_cripto": "0",
                                                  f"ativo_{vale.id}": "100"}, headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(MetaClasse, "acoes").meta_pct == 100  # a padrão não mudou
    assert {c.classe: c.meta_pct for c in resumo_carteira(db, HOJE, ana).classes}["acoes"] == 60


def test_operacao_escolhe_carteira_e_lancamentos_filtram(client, db, mercado):  # noqa: F811
    nova_conta(db)
    padrao, ana, _, _ = cenario(db)
    r = client.post("/investimentos/operacoes", data=op_form(codigo="itsa4", carteira_id=str(ana.id)))
    assert r.status_code == 200, r.text
    nova = db.scalars(select(OperacaoInvest).order_by(OperacaoInvest.id.desc())).first()
    assert nova.carteira_id == ana.id
    so_dela = client.get(f"/investimentos/lancamentos?carteira={ana.id}").text.split("<tbody")[1]
    assert "ITSA4" in so_dela and "VALE3" in so_dela and "PETR4" not in so_dela
    minha = client.get(f"/investimentos/lancamentos?carteira={padrao.id}").text.split("<tbody")[1]
    assert "PETR4" in minha and "ITSA4" not in minha


def test_criar_e_editar_carteira(client, db):
    c6 = nova_conta(db, "C6 Invest", tipo="investimento", sl=False)
    db.commit()
    r = client.post("/investimentos/carteiras", data={"nome": "Carteira da Ana", "contas": [str(c6.id)]},
                    follow_redirects=False)
    assert r.status_code == 303
    c = db.scalar(select(Carteira).where(Carteira.nome == "Carteira da Ana"))
    assert c.contas == str(c6.id) and not c.padrao
    client.post("/investimentos/carteiras", data={"id": str(c.id), "nome": "Ana"})
    db.expire_all()
    assert db.get(Carteira, c.id).nome == "Ana" and db.get(Carteira, c.id).contas == ""
    assert db.scalar(select(Carteira).where(Carteira.padrao)) is not None


def test_sugerir_aporte_por_classe_manda_o_aporte_inteiro_para_ela(session):
    from app.services.invest_carteira import sugerir_aporte
    padrao = garantir_carteira_padrao(session)
    petr = Ativo(classe="acao", codigo="PETR4", simbolo_cotacao="PETR4.SA", alocacao="acoes", meta_pct=D(60))
    itsa = Ativo(classe="acao", codigo="ITSA4", simbolo_cotacao="ITSA4.SA", alocacao="acoes", meta_pct=D(40))
    mxrf = Ativo(classe="etf", codigo="IMAB11", simbolo_cotacao="IMAB11.SA", alocacao="renda_fixa", meta_pct=D(100))
    session.add_all([petr, itsa, mxrf, MetaClasse(classe="acoes", meta_pct=D(50)), MetaClasse(classe="renda_fixa", meta_pct=D(50))])
    session.flush()
    for a, q in ((petr, 50), (itsa, 50), (mxrf, 100)):
        session.add(OperacaoInvest(ativo_id=a.id, tipo="compra", data=date(2026, 8, 3), quantidade=D(q), preco_unitario=1000))
    for simbolo in ("PETR4.SA", "ITSA4.SA", "IMAB11.SA"):
        _cotacao(session, simbolo, "10")
    session.commit()
    carteira = resumo_carteira(session, HOJE, padrao)
    so_acoes = {s.ativo.codigo: s.valor for s in sugerir_aporte(carteira, 100_000, "acoes")}
    # ações valem 1.000, o aporte vai inteiro para elas: alvo 2.000; PETR4 60% = 1.200 (falta 700), ITSA4 40% = 800 (falta 300)
    assert so_acoes == {"PETR4": 70_000, "ITSA4": 30_000}
    so_fiis = {s.ativo.codigo: s.valor for s in sugerir_aporte(carteira, 100_000, "renda_fixa")}
    assert so_fiis == {"IMAB11": 100_000}
    assert sugerir_aporte(carteira, 100_000, "cripto") == []  # classe que a carteira não tem: nada
    global_ = {s.ativo.codigo: s.valor for s in sugerir_aporte(carteira, 100_000)}
    assert set(global_) <= {"PETR4", "ITSA4", "IMAB11"}  # sem classe, segue a regra global


def test_rota_rebalancear_filtra_por_classe(client, db, mercado):  # noqa: F811
    nova_conta(db)
    garantir_carteira_padrao(db)
    petr = Ativo(classe="acao", codigo="PETR4", simbolo_cotacao="PETR4.SA", alocacao="acoes", meta_pct=D(100))
    mxrf = Ativo(classe="etf", codigo="IMAB11", simbolo_cotacao="IMAB11.SA", alocacao="renda_fixa", meta_pct=D(100))
    db.add_all([petr, mxrf, MetaClasse(classe="acoes", meta_pct=D(50)), MetaClasse(classe="renda_fixa", meta_pct=D(50))])
    db.flush()
    for a in (petr, mxrf):
        db.add(OperacaoInvest(ativo_id=a.id, tipo="compra", data=date(2026, 8, 3), quantidade=D(50), preco_unitario=1000))
    _cotacao(db, "PETR4.SA", "10")
    _cotacao(db, "IMAB11.SA", "10")
    db.commit()
    pagina = client.get("/investimentos/rebalancear").text
    assert 'name="classe"' in pagina and "Todas (global)" in pagina
    assert '<option value="acoes">Renda variável</option>' in pagina and '<option value="renda_fixa">Renda fixa</option>' in pagina
    so_rf = client.get("/investimentos/rebalancear?aporte=1000&classe=renda_fixa").text
    assert "Aporte inteiro em <span" in so_rf and ">Renda fixa</span>, dividido" in so_rf
    sugestoes = so_rf.split("<tbody")[1].split("</tbody>")[0]   # a tabela de compras (a de antes/depois lista todos)
    assert "Aporte inteiro em" in so_rf and "IMAB11" in sugestoes and "PETR4" not in sugestoes
    invalida = client.get("/investimentos/rebalancear?aporte=1000&classe=nada")
    assert invalida.status_code == 200 and "Aporte inteiro em" not in invalida.text  # classe inválida: global


def test_ativo_ainda_nao_comprado_aparece_so_na_carteira_escolhida_e_na_consolidada(session):
    padrao = garantir_carteira_padrao(session)
    ana = Carteira(nome="Ana")
    session.add(ana)
    session.flush()
    mund, ivvb = Ativo(classe="etf", codigo="MUND11"), Ativo(classe="etf", codigo="IVVB11")
    session.add_all([mund, ivvb])
    session.flush()
    marcar_desejado(session, mund, {ana.id})
    marcar_desejado(session, ivvb, {padrao.id})
    session.commit()
    codigos = lambda c: [x.ativo.codigo for x in resumo_carteira(session, HOJE_TESTE, c).ativos]  # noqa: E731
    assert codigos(ana) == ["MUND11"]
    assert codigos(padrao) == ["IVVB11"]
    assert codigos(None) == ["IVVB11", "MUND11"]


def test_mesmo_ativo_desejado_em_varias_carteiras_e_comprado_numa_desejado_na_outra(session):
    padrao, ana, petr, vale = cenario(session)
    ivvb = Ativo(classe="etf", codigo="IVVB11")
    session.add(ivvb)
    session.flush()
    marcar_desejado(session, ivvb, {padrao.id, ana.id})
    marcar_desejado(session, petr, {ana.id})  # PETR4 já comprado na minha; na Ana ainda não
    session.commit()
    codigos = lambda c: [x.ativo.codigo for x in resumo_carteira(session, HOJE, c).ativos]  # noqa: E731
    assert codigos(padrao) == ["PETR4", "IVVB11"]           # a maior meta primeiro (PETR4 100)
    assert codigos(ana) == ["VALE3", "IVVB11", "PETR4"]   # VALE3 100; sem meta nem saldo, pelo código
    assert next(a for a in resumo_carteira(session, HOJE, ana).ativos if a.ativo.codigo == "PETR4").quantidade == 0
    marcar_desejado(session, ivvb, {ana.id})
    assert carteiras_do_desejo(session, ivvb) == {ana.id}


def test_migracao_dos_desejados_le_a_coluna_antiga_uma_vez(session):
    padrao = garantir_carteira_padrao(session)
    ana = Carteira(nome="Ana")
    session.add(ana)
    session.add_all([Ativo(classe="etf", codigo="MUND11"), Ativo(classe="etf", codigo="IVVB11")])
    session.flush()
    session.execute(text('ALTER TABLE ativo ADD COLUMN carteira_desejo_id INTEGER'))
    session.execute(text("UPDATE ativo SET carteira_desejo_id = :c WHERE codigo = 'MUND11'"), {"c": ana.id})
    migrar_ativos_desejados(session)
    por_codigo = {a.codigo: carteiras_do_desejo(session, a) for a in session.scalars(select(Ativo))}
    assert por_codigo == {"MUND11": {ana.id}, "IVVB11": {padrao.id}}
    marcar_desejado(session, session.scalar(select(Ativo).where(Ativo.codigo == "IVVB11")), set())
    migrar_ativos_desejados(session)  # já migrou: não volta a pôr o IVVB11 na padrão
    assert session.scalar(select(func.count()).select_from(AtivoDesejado)) == 1


def test_cadastrar_sem_compra_guarda_a_carteira_e_tela_do_ativo_troca(client, db, mercado):
    nova_conta(db)
    garantir_carteira_padrao(db)
    ana = Carteira(nome="Ana")
    db.add(ana)
    db.commit()
    r = client.post("/investimentos/operacoes", data=op_form(so_cadastrar="1", carteira_id=str(ana.id)))
    assert r.status_code == 200, r.text
    a = db.scalar(select(Ativo).where(Ativo.codigo == "PETR4"))
    assert carteiras_do_desejo(db, a) == {ana.id}
    # o mesmo ativo, já cadastrado, também na minha carteira: sem operação
    r = client.post("/investimentos/operacoes", data=op_form(ativo_id=str(a.id), so_cadastrar="1", carteira_id=""))
    assert r.status_code == 200, r.text
    assert carteiras_do_desejo(db, a) == {ana.id, garantir_carteira_padrao(db).id}
    assert db.scalars(select(OperacaoInvest)).all() == []
    assert 'action="/investimentos/ativos/%d/carteira"' % a.id in client.get(f"/investimentos/ativos/{a.id}").text
    r = client.post(f"/investimentos/ativos/{a.id}/carteira", data={"carteira_id": [str(ana.id), "999"]},
                    follow_redirects=False)
    assert r.status_code == 303
    assert carteiras_do_desejo(db, a) == {ana.id}


def test_desmarcar_carteira_tira_o_ativo_mesmo_com_meta_nela(client, db, mercado):  # noqa: F811
    """Ativo sem posição na carteira, mas com meta lá: a caixa vem marcada e desmarcar zera a meta (some da lista)."""
    nova_conta(db)
    padrao, ana, petr, vale = cenario(db)
    salvar_metas_carteira(db, ana, {"acoes": D(50), "renda_fixa": D(50), "cripto": D(0)}, {vale.id: D(50), petr.id: D(50)})
    db.commit()
    assert "PETR4" in [a.ativo.codigo for a in resumo_carteira(db, HOJE, ana).ativos]
    pagina = client.get(f"/investimentos/ativos/{petr.id}").text
    assert f'value="{ana.id}" checked' in pagina
    r = client.post(f"/investimentos/ativos/{petr.id}/carteira", data={"carteira_id": []}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert "PETR4" not in [a.ativo.codigo for a in resumo_carteira(db, HOJE, ana).ativos]
    assert metas_da_carteira(db, ana)[1][petr.id] == 0
    assert db.get(Ativo, petr.id).meta_pct == 100  # na minha carteira tem posição: a meta fica
