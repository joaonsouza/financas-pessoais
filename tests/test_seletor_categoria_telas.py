import json
from datetime import date

from tests.factories import nova_categoria, nova_conta, novo_lancamento


def _base(db):
    conta = nova_conta(db)
    alim = nova_categoria(db, "Alimentação")
    mercado = nova_categoria(db, "Mercado", pai=alim)
    salario = nova_categoria(db, "Salário", tipo="receita")
    return conta, alim, mercado, salario


def test_formulario_de_lancamento_usa_o_seletor(client, db):
    conta, alim, mercado, _ = _base(db)
    lanc = novo_lancamento(db, conta, date(2026, 10, 2), -5000, categoria=mercado, descricao="Pão")
    db.commit()
    texto = client.get("/lancamentos/novo?tipo=despesa").text
    assert 'data-seletor-categoria="categoria_id"' in texto and 'id="categorias-despesa"' in texto
    assert '<select name="categoria_id"' not in texto
    texto = client.get(f"/lancamentos/{lanc.id}/editar").text
    assert f'name="categoria_id" value="{mercado.id}"' in texto and "Alimentação › Mercado" in texto


def test_json_vai_uma_vez_por_tipo(client, db):
    _base(db)
    db.commit()
    texto = client.get("/lancamentos/novo?tipo=receita").text
    assert texto.count('id="categorias-receita"') == 1
    bruto = texto.split('id="categorias-receita">', 1)[1].split("</script>", 1)[0]
    assert [g["nome"] for g in json.loads(bruto)["grupos"]] == ["Salário"]


def test_lote_filtro_recorrencias_e_planejamento_usam_o_seletor(client, db):
    conta, alim, mercado, _ = _base(db)
    lanc = novo_lancamento(db, conta, date(2026, 10, 2), -5000, categoria=mercado, descricao="Pão")
    db.commit()
    texto = client.get(f"/lancamentos/editar-lote?ids={lanc.id}").text
    assert 'data-seletor-categoria="categoria_id"' in texto and "Não alterar" in texto
    texto = client.get("/lancamentos?mes=2026-10&categoria=sem").text
    assert 'name="categoria" value="sem"' in texto
    texto = client.get("/lancamentos/recorrencias").text
    assert 'data-seletor-categoria="categoria_id"' in texto and '<select name="categoria_id"' not in texto
    texto = client.get("/planejamento?mes=2026-10").text
    assert 'id="form-limite"' in texto and texto.count('data-seletor-categoria="categoria_id"') >= 2  # limite e meta


def test_criacao_rapida_ligada_so_onde_faz_sentido(client, db):
    _base(db)
    db.commit()
    assert '"criar": true' in client.get("/lancamentos/novo?tipo=despesa").text
    assert '"criar": true' not in client.get("/lancamentos?mes=2026-10").text   # filtro da lista
