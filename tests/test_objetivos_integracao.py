"""Objetivos conversam com o resto do app: guardar/retirar vira lançamento, o progresso vem deles."""
import re
from datetime import date

import pytest
from sqlalchemy import select

from app.models import Caixinha, Categoria, Lancamento, Meta, Recorrencia
from app.services.caixinhas import agendado_caixinha, saldo_caixinha
from app.services.indicadores_curso import painel_503020
from app.services.metas import (
    agendado_da_meta, aplicar_pague_primeiro, caixinhas_da_meta, economia_do_mes, excluir_meta, guardar,
    historico_meta, progresso_meta, reserva, retirar, vincular_aportes_recorrentes,
)
from app.services.saldos import saldo_atual
from tests.factories import nova_categoria, nova_conta, novo_lancamento

HOJE = date(2026, 10, 5)


def _meta(db, **campos) -> Meta:
    meta = Meta(**({"nome": "Viagem", "tipo": "objetivo", "valor_alvo": 500000} | campos))
    db.add(meta)
    db.flush()
    return meta


def test_guardar_sem_conta_vira_despesa_e_conta_no_progresso(db):
    corrente = nova_conta(db, "Corrente", saldo=300000)
    meta = _meta(db, valor_manual=10000)
    (lanc,) = guardar(db, meta, valor=50000, data=HOJE, conta=corrente)
    cat = db.get(Categoria, lanc.categoria_id)
    assert (lanc.tipo, lanc.valor, lanc.status, lanc.meta_id, cat.nome) == ("despesa", -50000, "efetivado", meta.id, "Objetivos")
    assert lanc.descricao == "Meta: Viagem"
    assert saldo_atual(db, corrente) == 250000
    assert progresso_meta(db, meta, HOJE).atual == 60000      # 100 já guardados + 500

    (resgate,) = retirar(db, meta, valor=20000, data=HOJE, conta=corrente, hoje=HOJE)
    assert (resgate.tipo, resgate.valor, db.get(Categoria, resgate.categoria_id).tipo) == ("receita", 20000, "receita")
    assert progresso_meta(db, meta, HOJE).atual == 40000
    assert saldo_atual(db, corrente) == 270000
    assert sorted((h["descricao"], h["valor"]) for h in historico_meta(db, meta)) == [
        ("Meta: Viagem", 50000), ("Resgate: Viagem", -20000)]
    with pytest.raises(ValueError, match="maior do que o guardado"):
        retirar(db, meta, valor=50000, data=HOJE, conta=corrente, hoje=HOJE)


def test_guardar_com_conta_vira_transferencia_para_ela(db):
    corrente = nova_conta(db, "Corrente", saldo=300000)
    cofrinho = nova_conta(db, "Cofrinho", saldo=0)
    meta = _meta(db, conta_id=cofrinho.id)
    saida, entrada = guardar(db, meta, valor=80000, data=HOJE, conta=corrente)
    assert (saida.tipo, saida.conta_id, entrada.conta_id) == ("transferencia", corrente.id, cofrinho.id)
    assert saida.meta_id == entrada.meta_id == meta.id
    assert progresso_meta(db, meta, HOJE).atual == 80000
    with pytest.raises(ValueError, match="já está nesta conta"):
        guardar(db, meta, valor=100, data=HOJE, conta=cofrinho)
    # data futura: agendado, ainda não conta como guardado
    guardar(db, meta, valor=1000, data=date(2026, 11, 5), conta=corrente, efetivado=False)
    assert progresso_meta(db, meta, HOJE).atual == 80000 and agendado_da_meta(db, meta) == 1000


def test_pague_primeiro_sem_conta_gera_despesas_ligadas_a_meta(db):
    corrente = nova_conta(db, "Corrente")
    meta = _meta(db, pagar_primeiro=True, conta_origem_id=corrente.id, dia_pagar=6, valor_mensal_pagar=30000)
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    assert (rec.tipo, rec.conta_destino_id, db.get(Categoria, rec.categoria_id).nome) == ("despesa", None, "Objetivos")
    pend = list(db.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id)))
    assert len(pend) == 3 and all(l.meta_id == meta.id and l.status == "pendente" for l in pend)
    assert agendado_da_meta(db, meta) == 90000

    # passou a ter conta: a despesa mensal vira transferência para ela
    cofrinho = nova_conta(db, "Cofrinho")
    meta.conta_id = cofrinho.id
    nova = aplicar_pague_primeiro(db, meta, HOJE)
    assert nova.id != rec.id and nova.tipo == "transferencia" and nova.conta_destino_id == cofrinho.id
    assert db.get(Recorrencia, rec.id).ativa is False
    assert not db.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id)).all()


def test_reserva_alvo_sugerido_ou_definido_pela_pessoa(db):
    aluguel = nova_categoria(db, "Aluguel", grupo="fixo", essencial=True)
    corrente = nova_conta(db, "Corrente")
    for mes in (7, 8, 9):
        novo_lancamento(db, corrente, date(2026, mes, 10), -100000, categoria=aluguel)
    meta = _meta(db, nome="Reserva", tipo="reserva_emergencia", valor_alvo=0, multiplicador_reserva=6)
    r = reserva(db, meta, HOJE)
    assert (r.alvo, r.sugerido, r.personalizado) == (600000, 600000, False)
    meta.valor_alvo = 1000000
    r = reserva(db, meta, HOJE)
    assert (r.alvo, r.sugerido, r.personalizado) == (1000000, 600000, True)
    assert progresso_meta(db, meta, HOJE).alvo == 1000000


def test_retirar_da_reserva_cria_repor_reserva(db):
    corrente = nova_conta(db, "Corrente")
    meta = _meta(db, nome="Reserva", tipo="reserva_emergencia", valor_alvo=100000, valor_manual=50000)
    (lanc,) = retirar(db, meta, valor=30000, data=HOJE, conta=corrente, hoje=HOJE, checklist={"urgente"})
    repor = db.scalar(select(Meta).where(Meta.nome == "Repor reserva"))
    assert repor.valor_alvo == 30000 and "Urgente: sim" in lanc.observacao


def test_aportes_contam_como_economia_e_sobra_no_503020(db):
    corrente = nova_conta(db, "Corrente")
    novo_lancamento(db, corrente, date(2026, 10, 1), 500000, descricao="Salário")
    novo_lancamento(db, corrente, date(2026, 10, 2), -100000, categoria=nova_categoria(db, "Mercado"))
    db.add(Meta(nome="Economia", tipo="economia_mensal", valor_alvo=100000))
    meta = _meta(db)
    guardar(db, meta, valor=150000, data=HOJE, conta=corrente)
    e = economia_do_mes(db, "2026-10")
    assert e.realizado == 400000            # 5.000 − 1.000 de mercado; os 1.500 guardados são economia
    fatias = {f.rotulo: f.real for f in painel_503020(db, "2026-10").fatias}
    assert fatias["Desejos"] == 0 and fatias["Dívidas e sobra"] == 400000


def test_excluir_meta_mantem_lancamentos(db):
    corrente = nova_conta(db, "Corrente", saldo=100000)
    meta = _meta(db)
    (lanc,) = guardar(db, meta, valor=10000, data=HOJE, conta=corrente)
    excluir_meta(db, meta, HOJE)
    db.refresh(lanc)
    assert lanc.meta_id is None and db.get(Meta, meta.id) is None


def test_excluir_meta_deixa_na_conta_a_caixinha_com_saldo(db):
    corrente, cofrinho = nova_conta(db, "Corrente", saldo=100000), nova_conta(db, "Cofrinho")
    meta = _meta(db, conta_id=cofrinho.id)
    guardar(db, meta, valor=10000, data=HOJE, conta=corrente)
    (caixinha,) = caixinhas_da_meta(db, meta)
    excluir_meta(db, meta, HOJE)
    caixinha = db.get(Caixinha, caixinha.id)
    assert caixinha is not None and caixinha.meta_id is None and caixinha.conta_id == cofrinho.id
    assert saldo_caixinha(db, caixinha) == 10000


def test_excluir_meta_deixa_a_caixinha_com_guardar_agendado(db):
    corrente, cofrinho = nova_conta(db, "Corrente", saldo=100000), nova_conta(db, "Cofrinho")
    meta = _meta(db, conta_id=cofrinho.id)
    guardar(db, meta, valor=10000, data=date(2026, 11, 5), conta=corrente, hoje=HOJE, efetivado=False)
    (caixinha,) = caixinhas_da_meta(db, meta)
    excluir_meta(db, meta, HOJE)
    caixinha = db.get(Caixinha, caixinha.id)
    assert caixinha is not None and caixinha.meta_id is None
    assert (saldo_caixinha(db, caixinha), agendado_caixinha(db, caixinha)) == (0, 10000)


def test_excluir_meta_solta_as_recorrencias_dela(db):
    corrente, cofrinho = nova_conta(db, "Corrente"), nova_conta(db, "Cofrinho")
    meta = _meta(db, conta_id=cofrinho.id, pagar_primeiro=True, conta_origem_id=corrente.id, dia_pagar=6,
                 valor_mensal_pagar=1000)
    aporte = aplicar_pague_primeiro(db, meta, HOJE)
    escolhida = Recorrencia(descricao="Para a viagem", tipo="transferencia", conta_id=corrente.id,
                            conta_destino_id=cofrinho.id, valor=500, dia=1, inicio="2026-10", meta_id=meta.id)
    db.add(escolhida)
    db.flush()
    excluir_meta(db, meta, HOJE)
    # o id da meta pode voltar numa meta nova: nada pode continuar apontando para ele
    assert (aporte.origem_meta_id, escolhida.meta_id) == (None, None)


def test_vincular_aportes_de_recorrencias_antigas(db):
    corrente, cofrinho = nova_conta(db, "Corrente"), nova_conta(db, "Cofrinho")
    meta = _meta(db, conta_id=cofrinho.id, pagar_primeiro=True, conta_origem_id=corrente.id, dia_pagar=6,
                 valor_mensal_pagar=1000)
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    for lanc in db.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id)):
        lanc.meta_id = None  # como nos bancos de antes do vínculo
    assert vincular_aportes_recorrentes(db) == 6
    assert vincular_aportes_recorrentes(db) == 0


# --- telas ---

def test_tela_abas_ordem_e_expira_em_breve(client, db):
    nova_conta(db, "Corrente")
    db.add_all([
        Meta(nome="Carro", tipo="objetivo", horizonte="sonho", valor_alvo=100000, valor_manual=0, prazo=date(2026, 12, 1)),
        Meta(nome="Celular", tipo="objetivo", horizonte="mes", valor_alvo=1000, valor_manual=1000),
        Meta(nome="Casa", tipo="objetivo", horizonte="sonho", valor_alvo=900000, valor_manual=0, prazo=date(2030, 1, 1)),
    ])
    db.commit()
    texto = client.get("/objetivos").text
    assert "Expira em breve" in texto and "01/12/2026" in texto
    assert "Carro" in texto and "Casa" in texto and "Celular" not in texto  # concluído vai para a outra aba
    assert texto.index("Carro") < texto.index("Casa")                       # prazo mais próximo primeiro
    assert "Celular" in client.get("/objetivos?aba=concluidos").text
    por_nome = client.get("/objetivos?ordem=alvo").text.split("Ordenar por")[1]  # a grade, sem o aviso do topo
    assert por_nome.index("/planejamento/metas/3\"") < por_nome.index("/planejamento/metas/1\"")  # maior alvo primeiro: Casa antes de Carro
    assert "Carro" not in client.get("/objetivos?horizonte=mes").text.split("Ordenar por")[1]


def test_guardar_pela_tela_e_aparecer_nos_lancamentos(client, db):
    corrente = nova_conta(db, "Corrente", saldo=100000)
    db.add(Meta(nome="Viagem", tipo="objetivo", valor_alvo=500000, valor_manual=0, icone="plane", cor="#3b82f6"))
    db.commit()
    meta = db.scalar(select(Meta))
    r = client.post(f"/planejamento/metas/{meta.id}/guardar", data={"valor": "250,00", "conta_id": str(corrente.id),
                                                           "data": "2026-10-05"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/planejamento/metas/{meta.id}"
    detalhe = client.get(f"/planejamento/metas/{meta.id}").text
    assert "R$ 250,00" in detalhe and "Meta: Viagem" in detalhe and "Histórico" in detalhe
    lancs = client.get("/lancamentos?mes=2026-10").text
    assert "Meta: Viagem" in lancs and f'href="/planejamento/metas/{meta.id}"' in lancs
    r = client.post(f"/planejamento/metas/{meta.id}/retirar", data={"valor": "999,00", "conta_id": str(corrente.id),
                                                           "data": "2026-10-05"})
    assert r.status_code == 422 and "maior do que o guardado" in r.text


def test_ligar_lancamento_a_objetivo_pelo_formulario(client, db):
    corrente = nova_conta(db, "Corrente", saldo=100000)
    db.add(Meta(nome="Viagem", tipo="objetivo", valor_alvo=500000, valor_manual=0))
    db.commit()
    meta = db.scalar(select(Meta))
    assert "Objetivo" in client.get("/lancamentos/novo?tipo=despesa").text
    client.post("/lancamentos", data={"tipo": "despesa", "valor": "40,00", "descricao": "Aplicação", "data": "2026-10-05",
                                      "conta_id": str(corrente.id), "efetivado": "on", "meta_id": str(meta.id)})
    db.expire_all()
    lanc = db.scalar(select(Lancamento).where(Lancamento.descricao == "Aplicação"))
    assert lanc.meta_id == meta.id
    assert progresso_meta(db, meta, HOJE).atual == 4000
    form = {"valor": "40,00", "descricao": "Aplicação", "data": "2026-10-05", "efetivado": "on", "meta_id": ""}
    client.post(f"/lancamentos/{lanc.id}", data=form)
    db.expire_all()
    assert db.get(Lancamento, lanc.id).meta_id is None


def test_reserva_com_teto_pela_tela_e_card_no_dashboard(client, db):
    nova_conta(db, "Corrente")
    db.commit()
    client.post("/planejamento/metas", data={"nome": "Reserva", "tipo": "reserva_emergencia", "valor_alvo": "20.000,00",
                                    "multiplicador_reserva": "3"})
    r = client.post("/planejamento/metas", data={"nome": "Viagem", "tipo": "objetivo", "valor_alvo": "1.000,00", "valor_manual": "100,00",
                                    "icone": "plane", "cor": "#3b82f6"}, follow_redirects=False)
    assert r.status_code == 303, r.text[r.text.find('role="alert"'):][:300]
    db.expire_all()
    viagem = db.scalar(select(Meta).where(Meta.nome == "Viagem"))
    assert (viagem.icone, viagem.cor) == ("plane", "#3b82f6")
    assert "Alvo definido por você" in client.get("/planejamento").text
    painel = client.get("/").text
    assert "Objetivos" in painel and f'href="/planejamento/metas/{viagem.id}"' in painel and "R$ 100,00 de R$ 1.000,00" in painel


def test_reserva_com_alvo_definido_ajusta_degraus_e_meses(db):
    aluguel = nova_categoria(db, "Aluguel", grupo="fixo", essencial=True)
    corrente = nova_conta(db, "Corrente")
    for mes in (7, 8, 9):
        novo_lancamento(db, corrente, date(2026, mes, 10), -10000, categoria=aluguel)   # essencial calculado: R$ 100
    meta = _meta(db, nome="Reserva", tipo="reserva_emergencia", valor_alvo=3600000, multiplicador_reserva=6,
                 valor_manual=300000)
    r = reserva(db, meta, HOJE)
    assert (r.alvo, r.gasto_essencial, r.base, r.sugerido) == (3600000, 600000, "alvo", 60000)
    assert [d.valor for d in r.degraus] == [100000, 600000, 1800000, 3600000]
    assert r.meses_que_aguento == 0.5 and r.proximo_degrau.rotulo == "1 mês"


def _reserva_no_vermelho(db):
    conta = nova_conta(db, "Corrente")
    novo_lancamento(db, conta, date(2026, 9, 10), 200000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 9, 12), -450000, descricao="Contas")      # déficit de R$ 2.500
    lazer = nova_categoria(db, "Lazer", natureza="desejo")
    novo_lancamento(db, conta, date(2026, 10, 3), -30000, categoria=lazer, status="pendente")
    meta = _meta(db, nome="Reserva", tipo="reserva_emergencia", valor_alvo=1800000, valor_manual=1000000)
    db.commit()
    return meta, lazer


def test_card_da_reserva_mostra_projecao_e_botao_programar(client, db):
    meta, lazer = _reserva_no_vermelho(db)
    html = client.get("/planejamento").text
    assert "a reserva acaba em ~4 meses (fev/2027)" in html and "Para parar de consumir, corte R$ 2.500,00/mês" in html
    assert f"/lancamentos?mes=2026-10&amp;tipo=despesa&amp;categoria={lazer.id}" in html or \
        f"/lancamentos?mes=2026-10&tipo=despesa&categoria={lazer.id}" in html
    assert "Primeiro pare de consumir a reserva" in html
    assert "Programar" not in html                      # no vermelho e sem aporte: só o aviso


def test_card_da_reserva_no_vermelho_com_aporte_mostra_acaba_em_e_programar(client, db):
    meta, _ = _reserva_no_vermelho(db)
    meta.pagar_primeiro, meta.valor_mensal_pagar = True, 50000
    db.commit()
    html = client.get("/planejamento").text
    assert "a reserva acaba em ~4 meses" in html and "Programar" in html and "Primeiro pare" not in html


def test_card_da_reserva_no_azul_mostra_plano_com_programar(client, db):
    conta = nova_conta(db, "Corrente")
    novo_lancamento(db, conta, date(2026, 9, 10), 500000, descricao="Salário")
    novo_lancamento(db, conta, date(2026, 9, 12), -400000, descricao="Contas")
    meta = _meta(db, nome="Reserva", tipo="reserva_emergencia", valor_alvo=1800000, valor_manual=150000)
    db.commit()
    html = client.get("/planejamento").text
    assert "a reserva acaba em" not in html and "~mar/2027</span>" in html
    assert f"/planejamento/metas/{meta.id}?editar=1&amp;aporte=137500" in html and "Programar" in html


def test_detalhe_com_editar_e_aporte_vem_com_o_formulario_preenchido(client, db):
    nova_conta(db, "Corrente")
    meta = _meta(db, nome="Reserva", tipo="reserva_emergencia", valor_alvo=1800000, valor_manual=150000)
    db.commit()
    html = client.get(f"/planejamento/metas/{meta.id}?editar=1&aporte=50000").text
    assert "editando: true" in html and 'name="valor_mensal_pagar" value="500,00"' in html
    assert re.search(r'name="pagar_primeiro"[^>]*checked', html) and "pagar: true" in html and "aporte: 'fixo'" in html
    vazio = client.get(f"/planejamento/metas/{meta.id}").text
    assert "editando: false" in vazio and 'name="valor_mensal_pagar" value=""' in vazio
    assert not re.search(r'name="pagar_primeiro"[^>]*checked', vazio)
    db.expire_all()
    assert db.get(Meta, meta.id).pagar_primeiro is False   # nada é gravado até salvar
