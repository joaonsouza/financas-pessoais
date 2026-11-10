from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

from app.models import Categoria, Lancamento, RegraCategoria
from app.services.categorizar_ia import NovaSugestao, aplicar_categorizacao, propor_categorizacao
from tests.factories import nova_categoria, nova_conta, novo_lancamento

D = date(2026, 10, 5)


class ClienteFalso:
    def __init__(self, *respostas):
        self.respostas, self.chamadas = list(respostas), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.chamadas.append(kwargs)
        return self.respostas.pop(0)


def _ia(*pares):
    return SimpleNamespace(stop_reason="end_turn", parsed_output=SimpleNamespace(
        itens=[SimpleNamespace(descricao=d, categoria_id=c) for d, c in pares]))


def _cenario(s):
    conta = nova_conta(s)
    moradia = nova_categoria(s, "Moradia")
    energia = nova_categoria(s, "Energia", pai=moradia, grupo="fixo", natureza="necessidade")
    lazer = nova_categoria(s, "Lazer")
    return conta, moradia, energia, lazer


def test_agrupa_por_descricao_e_ordena_pelo_valor(session):
    conta, *_ = _cenario(session)
    novo_lancamento(session, conta, D, -1000, descricao="Padaria Central")
    novo_lancamento(session, conta, D, -2000, descricao="padaria central")
    novo_lancamento(session, conta, D, -90000, descricao="Xablau Qwerty")
    proposta = propor_categorizacao(session, None)
    assert [(g.descricao, g.qtd, g.total) for g in proposta.linhas] == [
        ("Xablau Qwerty", 1, 90000), ("Padaria Central", 2, 3000)]


def test_metodos_locais_primeiro_e_ia_so_no_que_sobra(session):
    conta, moradia, energia, lazer = _cenario(session)
    ja = novo_lancamento(session, conta, D, -5000, descricao="Conta de luz", categoria=energia)
    novo_lancamento(session, conta, D, -6000, descricao="Conta de luz")  # o histórico resolve
    novo_lancamento(session, conta, D, -9000, descricao="Xablau Qwerty")
    cliente = ClienteFalso(_ia(("Xablau Qwerty", lazer.id)))
    proposta = propor_categorizacao(session, cliente)
    por_desc = {g.descricao: (g.categoria_id, g.origem) for g in proposta.linhas}
    assert por_desc["Conta de luz"] == (energia.id, "historico")
    assert por_desc["Xablau Qwerty"] == (lazer.id, "ia")
    texto = cliente.chamadas[0]["messages"][0]["content"][0]["text"]
    assert "Xablau Qwerty" in texto and "Conta de luz" not in texto
    assert "Moradia › Energia (fixo, necessidade)" in texto  # a IA vê o pai e os atributos
    assert f"{moradia.id}:" not in texto  # o pai só agrupa: não é opção


def test_ia_com_id_invalido_deixa_sem_sugestao(session):
    conta, *_ = _cenario(session)
    novo_lancamento(session, conta, D, -9000, descricao="Coisa estranha")
    proposta = propor_categorizacao(session, ClienteFalso(_ia(("Coisa estranha", 9999))))
    assert proposta.linhas[0].categoria_id is None


def test_falha_da_ia_vira_aviso_e_mantem_o_resto(session):
    from app.importers.base import LeitorErro
    conta, moradia, energia, lazer = _cenario(session)
    novo_lancamento(session, conta, D, -5000, descricao="Conta de luz", categoria=energia)
    novo_lancamento(session, conta, D, -6000, descricao="Conta de luz")
    novo_lancamento(session, conta, D, -9000, descricao="Xablau Qwerty")

    class Recusa(ClienteFalso):
        def _parse(self, **kwargs):
            return SimpleNamespace(stop_reason="refusal", parsed_output=None)

    proposta = propor_categorizacao(session, Recusa())
    assert proposta.aviso and any(g.origem == "historico" for g in proposta.linhas)
    assert isinstance(LeitorErro("x"), Exception)


def test_aplicar_categoriza_todos_da_mesma_descricao_e_cria_regra(session):
    conta, moradia, energia, lazer = _cenario(session)
    a = novo_lancamento(session, conta, D, -9000, descricao="Xablau Qwerty")
    b = novo_lancamento(session, conta, D, -8000, descricao="XABLAU QWERTY")
    canc = novo_lancamento(session, conta, D, -7000, descricao="Xablau Qwerty", status="cancelado")
    lancs, regras, novas = aplicar_categorizacao(session, [("despesa", "XABLAU QWERTY", lazer.id)])
    assert (lancs, regras, novas) == (2, 1, 0)
    assert (a.categoria_id, b.categoria_id, canc.categoria_id) == (lazer.id, lazer.id, None)
    assert session.scalar(select(RegraCategoria).where(RegraCategoria.categoria_id == lazer.id)) is not None


def test_aplicar_recusa_categoria_de_outro_tipo(session):
    import pytest
    conta, moradia, energia, lazer = _cenario(session)
    salario = nova_categoria(session, "Salário", tipo="receita", grupo=None, natureza=None)
    novo_lancamento(session, conta, D, -9000, descricao="Xablau Qwerty")
    with pytest.raises(ValueError):
        aplicar_categorizacao(session, [("despesa", "XABLAU QWERTY", salario.id)])


def test_rotas_revisao_e_aplicar(client, db, app_teste):
    conta, moradia, energia, lazer = _cenario(db)
    novo_lancamento(db, conta, D, -9000, descricao="Xablau Qwerty")
    db.commit()
    app_teste.state.cliente_ia = ClienteFalso(_ia(("Xablau Qwerty", lazer.id)))
    r = client.post("/configuracoes/categorizar/ia")
    assert r.status_code == 200 and "Categorização sugerida" in r.text and "Xablau Qwerty" in r.text
    assert f'name="cat_0" value="{lazer.id}"' in r.text
    r = client.post("/configuracoes/categorizar/ia/aplicar", data={
        "ok": ["0"], "tipo_0": "despesa", "chave_0": "XABLAU QWERTY", "cat_0": str(lazer.id)})
    assert r.status_code == 200 and "1 lançamento(s) categorizado(s)" in r.text
    db.expire_all()
    assert db.scalar(select(Lancamento).where(Lancamento.descricao == "Xablau Qwerty")).categoria_id == lazer.id


def test_rota_aplicar_sem_escolha_da_erro(client, db):
    _cenario(db)
    db.commit()
    r = client.post("/configuracoes/categorizar/ia/aplicar", data={"ok": ["0"], "cat_0": ""})
    assert r.status_code == 400 and "marque ao menos uma" in r.text


def test_sem_chave_da_api_mostra_aviso_em_vez_de_erro(client, db, monkeypatch):
    conta, *_ = _cenario(db)
    novo_lancamento(db, conta, D, -9000, descricao="Xablau Qwerty")
    db.commit()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for rota in ("/configuracoes/categorizar/ia", "/configuracoes/categorias/organizar"):
        r = client.post(rota)
        assert r.status_code == 400 and "Falta a chave da API" in r.text and "Configurações &gt; IA" in r.text


# ---------------------------------------------------------------- categorias novas sugeridas

def _ia_nova(*itens):
    """itens = (descricao, categoria_id, nova|None) com nova = (nome, pai, grupo, natureza)."""
    return SimpleNamespace(stop_reason="end_turn", parsed_output=SimpleNamespace(itens=[
        SimpleNamespace(descricao=d, categoria_id=c, nova=None if n is None else SimpleNamespace(
            nome=n[0], pai=n[1], grupo=n[2], natureza=n[3])) for d, c, n in itens]))


def test_ia_propoe_categoria_nova_quando_nada_serve(session):
    conta, *_ = _cenario(session)
    novo_lancamento(session, conta, D, -9000, descricao="Petz Banho e Tosa")
    novo_lancamento(session, conta, D, -4000, descricao="Cobasi Racao")
    cliente = ClienteFalso(_ia_nova(("Petz Banho e Tosa", None, ("Pets", "Casa", "variavel", "desejo")),
                                    ("Cobasi Racao", None, ("Pets", "Casa", "variavel", "desejo"))))
    proposta = propor_categorizacao(session, cliente)
    assert all(g.categoria_id is None and g.nova.rotulo == "Casa › Pets (variavel, desejo)" for g in proposta.linhas)
    assert "Prefira SEMPRE uma categoria existente" in cliente.chamadas[0]["messages"][0]["content"][0]["text"]
    assert session.scalar(select(Categoria).where(Categoria.nome == "Pets")) is None  # nada criado ainda


def test_aplicar_cria_categoria_nova_sob_pai_novo_e_reaproveita_nas_linhas_parecidas(session):
    conta, *_ = _cenario(session)
    a = novo_lancamento(session, conta, D, -9000, descricao="Petz Banho e Tosa")
    b = novo_lancamento(session, conta, D, -4000, descricao="Cobasi Racao")
    nova = NovaSugestao("Pets", "Casa", "variavel", "desejo")
    lancs, regras, novas = aplicar_categorizacao(session, [("despesa", "PETZ BANHO E TOSA", nova),
                                                           ("despesa", "COBASI RACAO", nova)])
    pets = session.scalar(select(Categoria).where(Categoria.nome == "Pets"))
    casa = session.scalar(select(Categoria).where(Categoria.nome == "Casa"))
    assert pets.pai_id == casa.id and casa.pai_id is None
    assert (pets.tipo, pets.grupo, pets.natureza) == ("despesa", "variavel", "desejo")
    assert (a.categoria_id, b.categoria_id) == (pets.id, pets.id)
    assert (lancs, regras, novas) == (2, 2, 2)  # Casa e Pets


def test_nova_reaproveita_pai_e_categoria_que_ja_existem(session):
    conta, moradia, energia, lazer = _cenario(session)
    novo_lancamento(session, conta, D, -9000, descricao="Conta nova de luz")
    aplicar_categorizacao(session, [("despesa", "CONTA NOVA DE LUZ", NovaSugestao("Energia", "Moradia", "fixo", "necessidade"))])
    assert session.query(Categoria).filter_by(nome="Energia").count() == 1
    assert session.query(Categoria).filter_by(nome="Moradia").count() == 1


def test_nova_invalida_ou_receita_e_recusada(session):
    import pytest
    conta, *_ = _cenario(session)
    novo_lancamento(session, conta, D, -9000, descricao="Coisa estranha")
    with pytest.raises(ValueError):
        aplicar_categorizacao(session, [("despesa", "COISA ESTRANHA", NovaSugestao("X", None, "xpto", "desejo"))])
    with pytest.raises(ValueError):
        aplicar_categorizacao(session, [("receita", "COISA ESTRANHA", NovaSugestao("X", None, "fixo", "desejo"))])


def test_rota_mostra_criar_e_aplica_categoria_nova(client, db, app_teste):
    conta, *_ = _cenario(db)
    novo_lancamento(db, conta, D, -9000, descricao="Petz Banho e Tosa")
    db.commit()
    app_teste.state.cliente_ia = ClienteFalso(_ia_nova(("Petz Banho e Tosa", None, ("Pets", "Casa", "variavel", "desejo"))))
    r = client.post("/configuracoes/categorizar/ia")
    assert "＋ Criar: Casa › Pets (variavel, desejo)" in r.text and 'name="cat_0" value="nova"' in r.text
    r = client.post("/configuracoes/categorizar/ia/aplicar", data={
        "ok": ["0"], "tipo_0": "despesa", "chave_0": "PETZ BANHO E TOSA", "cat_0": "nova", "nova_nome_0": "Pets",
        "nova_pai_0": "Casa", "nova_grupo_0": "variavel", "nova_natureza_0": "desejo"})
    assert r.status_code == 200 and "1 categoria(s) nova(s)" not in r.text and "2 categoria(s) nova(s) criada(s)" in r.text
    db.expire_all()
    pets = db.scalar(select(Categoria).where(Categoria.nome == "Pets"))
    assert db.scalar(select(Lancamento).where(Lancamento.descricao == "Petz Banho e Tosa")).categoria_id == pets.id
