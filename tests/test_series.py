"""Editar e excluir lançamento recorrente ou parcelado: só este, este e os próximos, ou todos."""
from datetime import date

from sqlalchemy import select

from app.models import Lancamento, Meta, Recorrencia
from app.services.lancamentos import atualizar_lancamento, criar_compra_parcelada, criar_lancamento_repetido
from app.services.recorrencia import gerar_pendentes
from app.services.series import excluir_serie, propagar_edicao, retrato, tipo_serie
from tests.factories import nova_categoria, nova_conta, novo_cartao


def _recorrente(s, *, valor=10000, origem_meta_id=None, conta="Nubank"):
    """Aluguel de outubro a dezembro (hoje 05/10, horizonte 2): outubro efetivado, nov e dez pendentes."""
    conta = nova_conta(s, conta)
    if origem_meta_id is not None:  # a chave estrangeira exige a meta
        s.add(Meta(id=origem_meta_id, nome="Meta", tipo="objetivo"))
        s.flush()
    rec = Recorrencia(descricao="Aluguel", tipo="despesa", conta_id=conta.id, valor=valor, dia=10,
                      inicio="2026-10", origem_meta_id=origem_meta_id)
    s.add(rec)
    s.flush()
    gerar_pendentes(s, date(2026, 10, 5))
    ocorrencias = _ocorrencias(s, rec)
    ocorrencias[0].status = "efetivado"
    s.flush()
    return conta, rec, ocorrencias


def _ocorrencias(s, rec):
    return list(s.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id)
                          .order_by(Lancamento.competencia_recorrencia)))


def _editar(s, lanc, escopo, **mudar):
    antes = retrato(lanc)
    campos = {"data": lanc.data, "descricao": lanc.descricao, "valor_abs": abs(lanc.valor),
              "categoria_id": lanc.categoria_id, "tags": [t.nome for t in lanc.tags], "observacao": lanc.observacao}
    campos |= mudar
    atualizar_lancamento(s, lanc, **campos)
    return propagar_edicao(s, lanc, antes, escopo)


def test_tipo_serie(session):
    _, _, ocorrencias = _recorrente(session)
    conta = nova_conta(session, "Outra")
    parcelas = criar_lancamento_repetido(session, conta=conta, data=date(2026, 10, 1), descricao="Curso",
                                         valor_abs=5000, vezes=3, tipo="despesa")
    _, _, gerenciadas = _recorrente(session, origem_meta_id=99, conta="Provisões")

    assert tipo_serie(session, ocorrencias[0]) == "recorrencia"
    assert tipo_serie(session, parcelas[0]) == "parcela"
    # recorrência de meta é cuidada pela tela dela
    assert tipo_serie(session, gerenciadas[0]) is None


def test_editar_so_este(session):
    _, rec, ocorrencias = _recorrente(session)

    _editar(session, ocorrencias[1], "este", valor_abs=12000)

    assert [abs(l.valor) for l in _ocorrencias(session, rec)] == [10000, 12000, 10000]
    assert rec.valor == 10000


def test_editar_este_e_os_proximos_muda_os_seguintes_e_o_modelo(session):
    _, rec, ocorrencias = _recorrente(session)
    moradia = nova_categoria(session, "Moradia")

    alterados = _editar(session, ocorrencias[1], "proximos", valor_abs=12000, descricao="Aluguel novo",
                        categoria_id=moradia.id, data=date(2026, 11, 15))

    lista = _ocorrencias(session, rec)
    assert alterados == 1
    assert [abs(l.valor) for l in lista] == [10000, 12000, 12000]
    assert [l.descricao for l in lista] == ["Aluguel", "Aluguel novo", "Aluguel novo"]
    assert [l.data for l in lista] == [date(2026, 10, 10), date(2026, 11, 15), date(2026, 12, 15)]
    assert lista[2].categoria_id == moradia.id
    assert (rec.valor, rec.descricao, rec.dia, rec.categoria_id) == (12000, "Aluguel novo", 15, moradia.id)
    # status continua de cada um
    assert [l.status for l in lista] == ["efetivado", "pendente", "pendente"]


def test_editar_todos_inclui_os_anteriores(session):
    _, rec, ocorrencias = _recorrente(session)

    _editar(session, ocorrencias[2], "todos", descricao="Aluguel apto")

    assert {l.descricao for l in _ocorrencias(session, rec)} == {"Aluguel apto"}
    assert rec.descricao == "Aluguel apto"


def test_editar_propaga_so_o_que_mudou(session):
    """Mudar a descrição em Todos não iguala valores diferentes nos outros meses."""
    _, rec, ocorrencias = _recorrente(session)
    ocorrencias[0].valor = -9500
    session.flush()

    _editar(session, ocorrencias[1], "todos", descricao="Aluguel apto")

    assert [abs(l.valor) for l in _ocorrencias(session, rec)] == [9500, 10000, 10000]


def test_editar_parcelas_proximas(session):
    conta = nova_conta(session)
    parcelas = criar_lancamento_repetido(session, conta=conta, data=date(2026, 10, 1), descricao="Curso",
                                         valor_abs=5000, vezes=4, tipo="despesa")

    _editar(session, parcelas[1], "proximos", descricao="Curso de inglês", tags=["educacao"])

    assert [l.descricao for l in parcelas] == ["Curso", "Curso de inglês", "Curso de inglês", "Curso de inglês"]
    assert [t.nome for t in parcelas[3].tags] == ["educacao"]
    assert parcelas[0].tags == []


def test_editar_parcelas_no_cartao_mantem_a_fatura_de_cada_uma(session):
    cartao = novo_cartao(session, pagadora=nova_conta(session))
    parcelas = criar_compra_parcelada(session, conta=cartao, data=date(2026, 10, 15), descricao="TV",
                                      valor_total=30000, parcelas=3)
    faturas = [l.fatura_id for l in parcelas]

    _editar(session, parcelas[0], "todos", descricao="Televisão")

    assert [l.descricao for l in parcelas] == ["Televisão"] * 3
    assert [l.fatura_id for l in parcelas] == faturas


def test_excluir_so_este_cancela_a_recorrente_pendente(session):
    _, rec, ocorrencias = _recorrente(session)

    excluir_serie(session, ocorrencias[1], "este")

    assert [l.status for l in _ocorrencias(session, rec)] == ["efetivado", "cancelado", "pendente"]
    assert rec.ativa


def test_excluir_este_e_os_proximos_encerra_a_recorrencia(session):
    _, rec, ocorrencias = _recorrente(session)

    excluir_serie(session, ocorrencias[1], "proximos")
    gerar_pendentes(session, date(2026, 12, 5))

    assert [l.competencia_recorrencia for l in _ocorrencias(session, rec)] == ["2026-10"]
    assert rec.fim == "2026-10"


def test_excluir_todos_apaga_a_serie_e_a_recorrencia(session):
    _, rec, ocorrencias = _recorrente(session)
    rec_id = rec.id

    excluir_serie(session, ocorrencias[1], "todos")

    assert session.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec_id)).all() == []
    assert session.get(Recorrencia, rec_id) is None


def test_excluir_transferencia_recorrente_apaga_as_duas_pernas(session):
    origem, destino = nova_conta(session, "A"), nova_conta(session, "B")
    rec = Recorrencia(descricao="Poupança", tipo="transferencia", conta_id=origem.id, conta_destino_id=destino.id,
                      valor=5000, dia=10, inicio="2026-10")
    session.add(rec)
    session.flush()
    gerar_pendentes(session, date(2026, 10, 5))
    saida = session.scalar(select(Lancamento).where(Lancamento.recorrencia_id == rec.id,
                                                    Lancamento.conta_id == origem.id,
                                                    Lancamento.competencia_recorrencia == "2026-11"))

    excluir_serie(session, saida, "proximos")

    assert {l.competencia_recorrencia for l in _ocorrencias(session, rec)} == {"2026-10"}
    assert len(_ocorrencias(session, rec)) == 2


def test_excluir_parcelas_proximas_renumera_as_que_ficam(session):
    conta = nova_conta(session)
    parcelas = criar_lancamento_repetido(session, conta=conta, data=date(2026, 10, 1), descricao="Curso",
                                         valor_abs=5000, vezes=4, tipo="despesa")
    grupo = parcelas[0].grupo_parcela_id

    excluir_serie(session, parcelas[2], "proximos")

    restantes = session.scalars(select(Lancamento).where(Lancamento.grupo_parcela_id == grupo)
                                .order_by(Lancamento.parcela_num)).all()
    assert [(l.parcela_num, l.parcela_total) for l in restantes] == [(1, 2), (2, 2)]


def test_excluir_todas_as_parcelas(session):
    conta = nova_conta(session)
    parcelas = criar_lancamento_repetido(session, conta=conta, data=date(2026, 10, 1), descricao="Curso",
                                         valor_abs=5000, vezes=3, tipo="despesa")
    grupo = parcelas[0].grupo_parcela_id

    excluir_serie(session, parcelas[1], "todos")

    assert session.scalars(select(Lancamento).where(Lancamento.grupo_parcela_id == grupo)).all() == []


def test_escopo_invalido_ou_serie_gerenciada_vale_so_este(session):
    _, rec, ocorrencias = _recorrente(session, origem_meta_id=7)

    assert _editar(session, ocorrencias[1], "todos", descricao="Outro") == 0
    assert [l.descricao for l in _ocorrencias(session, rec)] == ["Aluguel", "Outro", "Aluguel"]
