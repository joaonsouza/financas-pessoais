from datetime import date

import pytest

from app.services.categorias_seletor import criar_categoria_rapida, opcoes_seletor
from tests.factories import nova_categoria, nova_conta, novo_lancamento

HOJE = date(2026, 10, 28)


def _arvore(db):
    alim = nova_categoria(db, "Alimentação")
    mercado = nova_categoria(db, "Mercado", pai=alim)
    acougue = nova_categoria(db, "Feira/Açougue/Padaria", pai=alim)
    escola = nova_categoria(db, "Educação")
    salario = nova_categoria(db, "Salário", tipo="receita")
    velha = nova_categoria(db, "Antiga", pai=alim)
    velha.ativa = False
    db.flush()
    return alim, mercado, acougue, escola, salario, velha


def test_grupos_por_mae_com_filhas_por_nome_sem_inativas(db):
    alim, mercado, acougue, escola, salario, velha = _arvore(db)
    d = opcoes_seletor(db, "despesa", HOJE)
    assert [g["nome"] for g in d["grupos"]] == ["Alimentação", "Educação"]
    assert [f["nome"] for f in d["grupos"][0]["filhas"]] == ["Feira/Açougue/Padaria", "Mercado"]
    assert d["grupos"][1]["filhas"] == []
    assert d["por_id"][str(mercado.id)]["rotulo"] == "Alimentação › Mercado"
    assert d["por_id"][str(alim.id)]["rotulo"] == "Alimentação"
    assert d["por_id"][str(velha.id)]["inativa"] is True      # fica no por_id para o rótulo
    assert str(salario.id) not in d["por_id"]


def test_todas_traz_despesa_antes_de_receita(db):
    _arvore(db)
    grupos = [(g["tipo"], g["nome"]) for g in opcoes_seletor(db, "todas", HOJE)["grupos"]]
    assert grupos == [("despesa", "Alimentação"), ("despesa", "Educação"), ("receita", "Salário")]


def test_mais_usadas_dos_ultimos_90_dias_so_efetivados(db):
    alim, mercado, acougue, escola, *_ = _arvore(db)
    conta = nova_conta(db)
    for _ in range(3):
        novo_lancamento(db, conta, date(2026, 10, 1), -100, categoria=mercado)
    for _ in range(2):
        novo_lancamento(db, conta, date(2026, 9, 1), -100, categoria=escola)
    novo_lancamento(db, conta, date(2026, 10, 2), -100, categoria=acougue, status="pendente")
    for _ in range(5):
        novo_lancamento(db, conta, date(2026, 6, 1), -100, categoria=alim)     # mais de 90 dias atrás
    assert opcoes_seletor(db, "despesa", HOJE)["mais_usadas"] == [mercado.id, escola.id]


def test_tipo_invalido(db):
    with pytest.raises(ValueError):
        opcoes_seletor(db, "x", HOJE)


# ---------- criação rápida ----------

def test_filha_criada_herda_da_mae(db):
    mae = nova_categoria(db, "Moradia", grupo="fixo", natureza="necessidade", essencial=True, cor="#123456",
                         inflacao="fixo")
    nova = criar_categoria_rapida(db, "  Pets ", "despesa", mae.id)
    assert (nova.nome, nova.pai_id, nova.tipo) == ("Pets", mae.id, "despesa")
    assert (nova.grupo, nova.natureza, nova.essencial, nova.cor, nova.inflacao) == ("fixo", "necessidade", True,
                                                                                      "#123456", "fixo")


def test_principal_de_despesa_e_de_receita(db):
    despesa = criar_categoria_rapida(db, "Pets", "despesa", None)
    receita = criar_categoria_rapida(db, "Aluguel recebido", "receita", None)
    assert (despesa.pai_id, despesa.grupo, despesa.natureza) == (None, "variavel", "desejo")
    assert (receita.grupo, receita.natureza) == (None, None) and receita.cor.startswith("#")


def test_criacao_rapida_recusa_invalidos(db):
    mae = nova_categoria(db, "Moradia")
    filha = nova_categoria(db, "Aluguel", pai=mae)
    salario = nova_categoria(db, "Salário", tipo="receita")
    for nome, tipo, pai in (("", "despesa", None), ("Aluguel", "despesa", mae.id), ("Moradia", "despesa", None),
                            ("X", "despesa", salario.id), ("X", "despesa", filha.id), ("X", "outro", None),
                            ("X", "despesa", 9999)):
        with pytest.raises(ValueError):
            criar_categoria_rapida(db, nome, tipo, pai)


def test_rota_de_criacao_rapida(client, db):
    nova_conta(db)
    mae = nova_categoria(db, "Moradia", cor="#123456")
    db.commit()
    r = client.post("/categorias/rapida", data={"nome": "Pets", "tipo": "despesa", "pai_id": str(mae.id)})
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["nome"] == "Pets" and corpo["rotulo"] == "Moradia › Pets" and corpo["pai_id"] == mae.id
    assert corpo["cor"] == "#123456" and isinstance(corpo["id"], int)
    r = client.post("/categorias/rapida", data={"nome": "Pets", "tipo": "despesa", "pai_id": str(mae.id)})
    assert r.status_code == 400 and "erro" in r.json()
