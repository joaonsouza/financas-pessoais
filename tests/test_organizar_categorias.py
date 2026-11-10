from types import SimpleNamespace

import pytest

from app.importers.base import LeitorErro
from app.models import Categoria
from app.models import Categoria  # noqa: F811
from app.services.organizar_categorias import ItemProposta, aplicar_hierarquia, propor_hierarquia
from tests.factories import nova_categoria, nova_conta


class ClienteFalso:
    def __init__(self, resposta):
        self.resposta, self.chamadas = resposta, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.chamadas.append(kwargs)
        return self.resposta


def _ia(*itens, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, parsed_output=SimpleNamespace(itens=[
        SimpleNamespace(categoria_id=c, pai=p, grupo=g, natureza=n, motivo="porque") for c, p, g, n in itens]))


def test_propoe_a_arvore_e_ignora_ids_desconhecidos(session):
    energia = nova_categoria(session, "Energia")
    faxina = nova_categoria(session, "Faxina")
    cliente = ClienteFalso(_ia((energia.id, "Moradia", "fixo", "necessidade"),
                               (faxina.id, "Moradia", "fixo", "desejo"),
                               (999, "Lixo", "fixo", "desejo")))
    itens = propor_hierarquia(session, cliente)
    assert [(i.nome, i.pai, i.grupo, i.natureza) for i in itens] == [
        ("Energia", "Moradia", "fixo", "necessidade"), ("Faxina", "Moradia", "fixo", "desejo")]
    assert "Energia" in cliente.chamadas[0]["messages"][0]["content"][0]["text"]


def test_recusa_vira_erro_legivel(session):
    nova_categoria(session, "Energia")
    with pytest.raises(LeitorErro):
        propor_hierarquia(session, ClienteFalso(SimpleNamespace(stop_reason="refusal", parsed_output=None)))


def test_sem_candidatas_nao_chama_a_ia(session):
    cliente = ClienteFalso(None)
    assert propor_hierarquia(session, cliente) == [] and cliente.chamadas == []


def test_aplica_cria_pai_move_e_grava_atributos(session):
    energia = nova_categoria(session, "Energia", grupo="variavel", natureza="desejo")
    agua = nova_categoria(session, "Água")
    r = aplicar_hierarquia(session, [
        ItemProposta(energia.id, "Energia", "Moradia", "fixo", "necessidade"),
        ItemProposta(agua.id, "Água", "moradia", "fixo", "necessidade")])
    moradia = session.query(Categoria).filter_by(nome="Moradia").one()
    assert (energia.pai_id, agua.pai_id) == (moradia.id, moradia.id)
    assert (energia.grupo, energia.natureza) == ("fixo", "necessidade")
    assert (r.movidas, r.pais_criados) == (2, 1) and moradia.pai_id is None


def test_reaproveita_pai_existente_e_nao_cria_ciclo(session):
    moradia = nova_categoria(session, "Moradia")
    casa = nova_categoria(session, "Casa")
    aplicar_hierarquia(session, [
        ItemProposta(casa.id, "Casa", "Moradia", "variavel", "desejo"),
        ItemProposta(moradia.id, "Moradia", "Casa", "variavel", "desejo")])  # tentativa de ciclo
    assert casa.pai_id == moradia.id and moradia.pai_id is None


def test_sem_pai_so_atualiza_atributos(session):
    ass = nova_categoria(session, "Assinaturas", grupo="variavel", natureza="desejo")
    aplicar_hierarquia(session, [ItemProposta(ass.id, "Assinaturas", None, "fixo", "desejo")])
    assert (ass.pai_id, ass.grupo) == (None, "fixo")


def test_atributo_invalido(session):
    cat = nova_categoria(session, "X")
    with pytest.raises(ValueError):
        aplicar_hierarquia(session, [ItemProposta(cat.id, "X", None, "xpto", "desejo")])


def test_rota_organizar_mostra_proposta_e_aplicar_move(client, db, app_teste):
    nova_conta(db)
    energia, faxina = nova_categoria(db, "Energia"), nova_categoria(db, "Faxina")
    db.commit()
    app_teste.state.cliente_ia = ClienteFalso(_ia((energia.id, "Moradia", "fixo", "necessidade"),
                                                  (faxina.id, "Moradia", "fixo", "desejo")))
    r = client.post("/configuracoes/categorias/organizar")
    assert r.status_code == 200 and "Proposta da IA" in r.text and 'value="Moradia"' in r.text
    r = client.post("/configuracoes/categorias/organizar/aplicar", data={
        "aplicar": [str(energia.id), str(faxina.id)],
        f"pai_{energia.id}": "Moradia", f"grupo_{energia.id}": "fixo", f"natureza_{energia.id}": "necessidade",
        f"pai_{faxina.id}": "Moradia", f"grupo_{faxina.id}": "fixo", f"natureza_{faxina.id}": "desejo"})
    assert r.status_code == 200 and "2 categoria(s) movida(s)" in r.text
    db.expire_all()
    moradia = db.query(Categoria).filter_by(nome="Moradia").one()
    assert db.get(Categoria, energia.id).pai_id == moradia.id and db.get(Categoria, faxina.id).natureza == "desejo"


def test_rota_organizar_erro_da_ia_aparece(client, db, app_teste):
    nova_conta(db)
    nova_categoria(db, "Energia")
    db.commit()
    app_teste.state.cliente_ia = ClienteFalso(SimpleNamespace(stop_reason="refusal", parsed_output=None))
    r = client.post("/configuracoes/categorias/organizar")
    assert r.status_code == 400 and "não conseguiu organizar" in r.text


def test_mover_categoria_a_mao_valida(client, db):
    nova_conta(db)
    moradia, energia = nova_categoria(db, "Moradia"), nova_categoria(db, "Energia")
    db.commit()
    dados = {"nome": "Energia", "cor": "#111111", "grupo": "fixo", "natureza": "necessidade", "ativa": "on"}
    assert client.post(f"/configuracoes/categorias/{energia.id}", data={**dados, "pai_id": str(moradia.id)},
                       follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.get(Categoria, energia.id).pai_id == moradia.id
    outra = nova_categoria(db, "Outra")
    db.commit()
    r = client.post(f"/configuracoes/categorias/{moradia.id}", data={**dados, "nome": "Moradia", "pai_id": str(outra.id)})
    assert r.status_code == 400 and "já tem subcategorias" in r.text
