"""Ajustes de saldo ficam fora da lista padrão; aparecem no tipo Ajuste ou com "Mostrar ajustes"."""
from datetime import date

from app.services.consulta_lancamentos import FiltrosLancamento, linhas_da_lista
from tests.factories import nova_conta, novo_lancamento


def _cenario(db):
    conta = nova_conta(db)
    novo_lancamento(db, conta, date(2026, 10, 2), -2000, descricao="Mercado")
    novo_lancamento(db, conta, date(2026, 10, 3), 1234, tipo="ajuste", descricao="Ajuste de saldo")
    db.commit()


def _descricoes(linhas):
    return {l.descricao for l in linhas}


def test_lista_padrao_esconde_ajustes(session):
    _cenario(session)

    assert _descricoes(linhas_da_lista(session, "2026-10")) == {"Mercado"}


def test_tipo_ajuste_mostra_so_ajustes(session):
    _cenario(session)

    assert _descricoes(linhas_da_lista(session, "2026-10", FiltrosLancamento(tipo="ajuste"))) == {"Ajuste de saldo"}


def test_mostrar_ajustes_inclui_na_lista_toda(session):
    _cenario(session)

    linhas = linhas_da_lista(session, "2026-10", FiltrosLancamento(ajustes=True))

    assert _descricoes(linhas) == {"Mercado", "Ajuste de saldo"}


def test_mostrar_ajustes_com_filtro_de_conta(session):
    _cenario(session)
    conta_id = linhas_da_lista(session, "2026-10")[0].conta_id

    assert _descricoes(linhas_da_lista(session, "2026-10", FiltrosLancamento(conta_id=conta_id))) == {"Mercado"}
    assert "Ajuste de saldo" in _descricoes(
        linhas_da_lista(session, "2026-10", FiltrosLancamento(conta_id=conta_id, ajustes=True)))


def test_tela_tem_opcao_e_link_mantem(client, db):
    _cenario(db)

    padrao = client.get("/lancamentos?mes=2026-10").text
    com_ajustes = client.get("/lancamentos?mes=2026-10&ajustes=1").text

    assert "Mostrar ajustes de saldo" in padrao
    assert "Ajuste de saldo" not in padrao.split('id="tabela-lancamentos"', 1)[1]
    assert "Ajuste de saldo" in com_ajustes.split('id="tabela-lancamentos"', 1)[1]
    # trocar de mês continua mostrando os ajustes
    assert "mes=2026-11&amp;ajustes=1" in com_ajustes
