from datetime import date

from app.models import get_config
from app.services.tela_inicial import BLOCOS, blocos_tela_inicial, salvar_tela_inicial
from tests.factories import nova_conta, novo_cartao, novo_lancamento


def test_padrao_mostra_todos_na_ordem_padrao(session):
    blocos = blocos_tela_inicial(session)

    assert [b.id for b in blocos] == list(BLOCOS)
    assert all(b.visivel for b in blocos)


def test_salvar_guarda_ordem_e_visibilidade(session):
    salvar_tela_inicial(session, ["objetivos", "cartoes", "contas", "resumo"], {"cartoes", "resumo"})

    blocos = blocos_tela_inicial(session)
    ids = [b.id for b in blocos]

    assert ids[:4] == ["objetivos", "cartoes", "contas", "resumo"]
    # os que não vieram no formulário vão para o fim, na ordem padrão, escondidos
    assert set(ids) == set(BLOCOS)
    assert {b.id for b in blocos if b.visivel} == {"cartoes", "resumo"}


def test_ignora_bloco_desconhecido_e_acrescenta_bloco_novo(session):
    from app.models import set_config
    set_config(session, "tela_inicial", [{"id": "sumiu", "visivel": True}, {"id": "contas", "visivel": False}])

    blocos = blocos_tela_inicial(session)

    assert [b.id for b in blocos] == list(BLOCOS)  # os não salvos se encaixam pela ordem padrão
    assert "sumiu" not in [b.id for b in blocos]
    # bloco que não estava salvo (novo numa versão do app) aparece visível; o salvo mantém a escolha
    assert {b.id for b in blocos if not b.visivel} == {"contas"}


def test_restaurar_padrao(session):
    salvar_tela_inicial(session, ["contas"], set())
    salvar_tela_inicial(session, None, None)

    assert get_config(session, "tela_inicial") is None
    assert all(b.visivel for b in blocos_tela_inicial(session))


def _dados(db):
    nubank = nova_conta(db, "Nubank", saldo=100000)
    novo_lancamento(db, nubank, date(2026, 10, 1), 500000, descricao="Salário")
    novo_lancamento(db, nubank, date(2026, 10, 2), -20000)
    novo_cartao(db, "Cartão Itaú", pagadora=nubank)
    db.commit()


def test_dashboard_tem_faixa_de_resumo_e_blocos(client, db):
    _dados(db)
    texto = client.get("/?mes=2026-10").text

    assert "Cartão de crédito" in texto
    assert "Meu desempenho" in texto
    assert 'id="titulo-balanco"' in texto
    assert 'id="titulo-pendencias"' in texto
    assert "Gerenciar tela inicial" in texto


def test_dashboard_esconde_bloco_desligado(client, db):
    _dados(db)
    resposta = client.post("/tela-inicial", data={"ordem": ["resumo", "contas"], "visivel": ["resumo"]},
                           follow_redirects=False)
    assert resposta.status_code == 303

    texto = client.get("/?mes=2026-10").text

    assert "Meu desempenho" in texto
    # os títulos continuam no modal de gerenciar; o bloco em si some
    assert 'aria-label="Saldos das contas"' not in texto
    assert 'id="titulo-balanco"' not in texto


def test_dashboard_restaurar_padrao(client, db):
    _dados(db)
    client.post("/tela-inicial", data={"ordem": ["resumo"], "visivel": []})
    client.post("/tela-inicial", data={"restaurar": "1"})

    assert 'id="titulo-balanco"' in client.get("/?mes=2026-10").text


def test_bloco_novo_entra_antes_do_seguinte_na_ordem_padrao(session):
    from app.models import set_config
    # configuração salva antes de existir o bloco de investimentos
    set_config(session, "tela_inicial", [{"id": i, "visivel": True} for i in ("resumo", "cartoes", "objetivos", "contas")])

    ids = [b.id for b in blocos_tela_inicial(session)]

    assert ids.index("investimentos") == ids.index("objetivos") - 1  # os objetivos descem, investimentos entram


def test_salvar_guarda_a_coluna_de_cada_bloco(session):
    salvar_tela_inicial(session, ["investimentos", "resumo"], {"investimentos", "resumo"},
                        {"investimentos": "topo", "resumo": "lixo"})

    colunas = {b.id: b.coluna for b in blocos_tela_inicial(session)}

    assert colunas["investimentos"] == "topo" and colunas["resumo"] == BLOCOS["resumo"][1]


def test_organizar_arrastando_salva_sem_recarregar(client, db):
    _dados(db)
    r = client.post("/tela-inicial", data={"ordem": ["contas", "resumo"], "visivel": ["contas", "resumo"],
                                          "coluna_contas": "direita"}, headers={"X-Organizar": "1"})
    assert r.status_code == 204
    texto = client.get("/?mes=2026-10").text
    assert "Organizar cards" in texto and 'data-zona="direita"' in texto
    direita = texto.split('data-zona="direita"')[1]
    assert 'data-bloco="contas"' in direita


def test_card_de_investimentos_na_tela_inicial(client, db):
    from tests.test_rotas_investimentos import op_form
    _dados(db)
    r = client.post("/investimentos/operacoes", data=op_form())
    assert r.status_code == 200
    texto = client.get("/?mes=2026-10").text
    assert 'id="titulo-investimentos"' in texto and "Ver carteira" in texto and "Renda variável" in texto


def test_balanco_mostra_o_previsto_e_contas_fora_ficam_recolhidas(client, db):
    _dados(db)
    nova_conta(db, "Tesouro", tipo="investimento", saldo=250000, sl=False)
    db.commit()
    texto = client.get("/?mes=2026-10").text
    assert "Balanço previsto" in texto and "Realizado até agora" in texto
    assert 'aria-controls="lista-contas-fora"' in texto and 'x-show="aberto"' in texto


def test_gerenciar_tem_seletor_de_coluna_por_bloco(client, db):
    _dados(db)
    texto = client.get("/?mes=2026-10").text
    assert 'x-model="b.coluna"' in texto
    for valor in ("topo", "esquerda", "direita"):
        assert f'<option value="{valor}">' in texto


def test_gerenciar_grava_a_coluna_escolhida(client, db):
    from app.services.tela_inicial import blocos_tela_inicial
    _dados(db)
    r = client.post("/tela-inicial", data={"ordem": ["contas", "resumo"], "visivel": ["contas", "resumo"],
                                           "coluna_contas": "direita", "coluna_resumo": "topo"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    por_id = {b.id: b for b in blocos_tela_inicial(db)}
    assert por_id["contas"].coluna == "direita" and por_id["resumo"].coluna == "topo"


def test_card_do_saldo_inteiro_leva_para_as_contas(client, db):
    _dados(db)
    texto = client.get("/?mes=2026-10").text
    assert 'href="/contas?mes=2026-10" aria-label="Ver contas" class="after:absolute after:inset-0' in texto
