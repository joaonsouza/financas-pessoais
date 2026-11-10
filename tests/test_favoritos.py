import pytest
from sqlalchemy import func, select

from app.models import Favorito
from app.services.favoritos import alternar_favorito, eh_favorito, listar_favoritos
from tests.factories import nova_categoria, nova_conta


def test_alternar_cria_e_remove(session):
    conta = nova_conta(session)
    cat = nova_categoria(session)
    assert alternar_favorito(session, "despesa", " Padaria ", cat.id, conta.id) is True
    fav = session.scalars(select(Favorito)).one()
    assert (fav.tipo, fav.descricao, fav.categoria_id, fav.conta_id) == ("despesa", "Padaria", cat.id, conta.id)
    assert eh_favorito(session, "despesa", "Padaria")
    assert not eh_favorito(session, "receita", "Padaria")
    assert alternar_favorito(session, "despesa", "Padaria", None, None) is False
    assert session.scalar(select(func.count(Favorito.id))) == 0


def test_descricao_vazia_falha(session):
    with pytest.raises(ValueError):
        alternar_favorito(session, "despesa", "  ", None, None)


def test_listar_por_tipo_com_limite(session):
    for i in range(10):
        alternar_favorito(session, "despesa", f"D{i}", None, None)
    alternar_favorito(session, "receita", "Salário", None, None)
    assert len(listar_favoritos(session, "despesa")) == 8
    assert [f.descricao for f in listar_favoritos(session, "receita")] == ["Salário"]
