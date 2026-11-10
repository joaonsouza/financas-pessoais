from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Lancamento, Recorrencia, Tag, get_config, set_config
from tests.factories import nova_conta


def test_cria_conta_e_lancamento_com_tag(session):
    conta = nova_conta(session)
    lanc = Lancamento(
        conta_id=conta.id, data=date(2026, 10, 2), descricao="Padaria",
        valor=-1250, tipo="despesa", status="efetivado",
    )
    lanc.tags.append(Tag(nome="viagem"))
    session.add(lanc)
    session.commit()

    salvo = session.scalars(select(Lancamento)).one()
    assert salvo.valor == -1250
    assert [t.nome for t in salvo.tags] == ["viagem"]
    assert salvo.origem == "manual"


def test_status_padrao_e_pendente(session):
    conta = nova_conta(session)
    lanc = Lancamento(conta_id=conta.id, data=date(2026, 10, 2), descricao="X", valor=-1, tipo="despesa")
    session.add(lanc)
    session.flush()
    assert lanc.status == "pendente"


def test_recorrencia_unica_por_mes_e_conta(session):
    conta = nova_conta(session)
    rec = Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=conta.id, valor=120000, dia=10, inicio="2026-10")
    session.add(rec)
    session.flush()
    for _ in range(2):
        session.add(Lancamento(
            conta_id=conta.id, data=date(2026, 10, 10), descricao="Aluguel", valor=-120000,
            tipo="despesa", recorrencia_id=rec.id, competencia_recorrencia="2026-10",
        ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_config_guarda_json(session):
    set_config(session, "metas_503020", [50, 30, 20])
    assert get_config(session, "metas_503020") == [50, 30, 20]
    set_config(session, "metas_503020", [60, 20, 20])
    assert get_config(session, "metas_503020") == [60, 20, 20]
    assert get_config(session, "inexistente", 7) == 7


def test_categoria_raiz_nao_permite_duplicata(session):
    """Duas categorias raiz (pai_id IS NULL) com mesmo nome e tipo devem falhar."""
    from app.models import Categoria

    cat1 = Categoria(nome="Mercado", tipo="despesa")
    session.add(cat1)
    session.flush()

    cat2 = Categoria(nome="Mercado", tipo="despesa")
    session.add(cat2)
    with pytest.raises(IntegrityError):
        session.flush()


def test_categoria_raiz_e_filha_mesmo_nome_permitido(session):
    """Uma categoria raiz e uma filha com mesmo nome devem ser permitidas."""
    from app.models import Categoria

    cat_raiz = Categoria(nome="Mercado", tipo="despesa")
    session.add(cat_raiz)
    session.flush()

    cat_filha = Categoria(nome="Mercado", tipo="despesa", pai_id=cat_raiz.id)
    session.add(cat_filha)
    session.flush()

    # Não deve gerar erro


def test_recorrencia_mesmo_mes_contas_diferentes(session):
    """Mesma (recorrencia_id, competencia_recorrencia) em diferentes conta_ids deve ser permitido."""
    from app.models import Recorrencia

    conta_a = nova_conta(session, "ContaA")
    conta_b = nova_conta(session, "ContaB")

    rec = Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=conta_a.id, valor=120000, dia=10, inicio="2026-10")
    session.add(rec)
    session.flush()

    # Mesmo recorrencia_id e competencia_recorrencia, mas contas diferentes
    lanc_a = Lancamento(
        conta_id=conta_a.id, data=date(2026, 10, 10), descricao="Aluguel", valor=-120000,
        tipo="despesa", recorrencia_id=rec.id, competencia_recorrencia="2026-10",
    )
    session.add(lanc_a)
    session.flush()

    lanc_b = Lancamento(
        conta_id=conta_b.id, data=date(2026, 10, 10), descricao="Aluguel", valor=-120000,
        tipo="despesa", recorrencia_id=rec.id, competencia_recorrencia="2026-10",
    )
    session.add(lanc_b)
    session.flush()  # Não deve gerar erro


def test_lancamento_fk_conta_enforcement(session):
    """FK enforcement: Lancamento com conta_id inválido deve falhar."""
    lanc = Lancamento(
        conta_id=9999, data=date(2026, 10, 2), descricao="Teste", valor=-1250,
        tipo="despesa",
    )
    session.add(lanc)
    with pytest.raises(IntegrityError):
        session.flush()
