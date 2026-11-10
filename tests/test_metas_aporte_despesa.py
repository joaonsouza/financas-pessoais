"""Aporte de meta "como despesa": pendente é despesa da origem; efetivado vira transferência para a caixinha."""
import re
from datetime import date

import pytest
from sqlalchemy import select

from app.models import Caixinha, Lancamento, Meta
from app.services.caixinhas import excluir_caixinha
from app.services.consulta_lancamentos import FiltrosLancamento, linhas_da_lista, totais_da_lista
from app.services.lancamentos import alternar_efetivado
from app.services.metas import aplicar_pague_primeiro, caixinhas_da_meta, excluir_meta
from app.services.saldos import saldos_contas
from tests.factories import nova_categoria, nova_conta

HOJE = date(2026, 10, 5)


def _meta(db, nome="IPTU", origem=None, destino=None, **campos):
    origem = origem or nova_conta(db, "Corrente", saldo=100000)
    destino = destino or nova_conta(db, "Caixinha Provisões")
    meta = Meta(**({"nome": nome, "tipo": "objetivo", "valor_alvo": 120000, "prazo": date(2027, 9, 30),
                    "conta_id": destino.id, "conta_origem_id": origem.id, "dia_pagar": 10, "pagar_primeiro": True,
                    "aporte_automatico": True, "aporte_como_despesa": True, "repete_anual": True} | campos))
    db.add(meta)
    db.flush()
    rec = aplicar_pague_primeiro(db, meta, HOJE)
    return meta, rec, origem, destino


def _lancs(db, rec):
    return list(db.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec.id)
                           .order_by(Lancamento.data, Lancamento.id)))


def test_pendente_entra_no_total_de_despesas_e_a_entrada_no_previsto_da_conta_da_meta(db):
    meta, rec, origem, destino = _meta(db)
    db.commit()
    receitas, despesas = totais_da_lista(linhas_da_lista(db, "2026-10"))
    assert despesas == rec.valor
    saldos = {s.conta_id: s for s in saldos_contas(db, HOJE, "2026-10")}
    assert saldos[origem.id].previsto_mes == 100000 - rec.valor
    # a entrada ainda pendente vai para o previsto da conta da meta (o dinheiro sai de um lado e entra no outro)
    assert (saldos[destino.id].atual, saldos[destino.id].previsto_mes) == (0, rec.valor)
    assert saldos[destino.id].previsto_dia == 0  # o aporte de outubro é no dia 10, depois de hoje (05/10)


def test_efetivar_vira_transferencia_e_desfazer_volta_a_despesa(db):
    cat = nova_categoria(db, "Impostos")
    meta, rec, origem, destino = _meta(db, categoria_id=cat.id)
    saida = _lancs(db, rec)[0]
    alternar_efetivado(db, saida)
    par = db.get(Lancamento, saida.transferencia_par_id)
    assert (saida.tipo, saida.status, saida.categoria_id) == ("transferencia", "efetivado", None)
    assert (par.conta_id, par.valor, par.status, par.caixinha_id) == (destino.id, -saida.valor, "efetivado", rec.caixinha_id)
    assert totais_da_lista(linhas_da_lista(db, "2026-10"))[1] == 0  # efetivada deixa de ser despesa

    alternar_efetivado(db, par)  # desfazer pela perna de entrada
    assert (saida.tipo, saida.status, saida.categoria_id, saida.transferencia_par_id) == (
        "despesa", "pendente", cat.id, None)
    assert db.get(Lancamento, par.id) is None


def test_caixinha_da_meta_nao_se_exclui_e_sai_com_a_meta_se_vazia(db):
    meta, rec, origem, destino = _meta(db)
    caixinha = caixinhas_da_meta(db, meta)[0]
    assert caixinha.nome == "IPTU" and caixinha.conta_id == destino.id and rec.caixinha_id == caixinha.id
    pendente = _lancs(db, rec)[0]
    alternar_efetivado(db, pendente)  # só ao efetivar a entrada nasce, na caixinha
    entradas = [l for l in _lancs(db, rec) if l.conta_id == destino.id]
    assert entradas and all(l.caixinha_id == caixinha.id for l in entradas)
    with pytest.raises(ValueError, match="esta caixinha é da meta IPTU; exclua a meta em Planejamento"):
        excluir_caixinha(db, caixinha)
    alternar_efetivado(db, entradas[0])  # desfaz: a caixinha volta a ficar sem dinheiro
    excluir_meta(db, meta, HOJE)
    assert db.get(Caixinha, caixinha.id) is None   # sem dinheiro guardado, a caixinha sai junto


def test_cada_aporte_aparece_numa_linha_so(client, db):
    itau, caixa = nova_conta(db, "Itaú"), nova_conta(db, "Caixinha Provisões")
    metas = []
    for nome in ("IPTU", "IPVA"):
        meta, rec, _, _ = _meta(db, nome, origem=itau, destino=caixa)
        metas.append(meta)
        for l in _lancs(db, rec):
            if l.competencia_recorrencia == "2026-11":
                alternar_efetivado(db, l)  # efetivada é transferência (pendente é despesa)
    db.commit()
    linhas = linhas_da_lista(db, "2026-11")
    assert sorted(l.aporte_meta_id for l in linhas) == sorted(m.id for m in metas)
    assert sorted((l.descricao, l.valor, l.origem_nome, l.destino_nome) for l in linhas) == [
        ("Meta: IPTU", -10000, "Itaú", "Caixinha Provisões"), ("Meta: IPVA", -10000, "Itaú", "Caixinha Provisões")]
    assert [l.valor for l in linhas_da_lista(db, "2026-11", FiltrosLancamento(conta_id=caixa.id))] == [10000, 10000]
    texto = client.get("/lancamentos?mes=2026-11").text
    assert "Ver meta" in texto
    assert f'href="/planejamento/metas/{metas[0].id}"' in texto
    # a coluna da categoria diz de qual meta é o aporte
    assert re.search(r"</span>\s*Meta: IPTU\s*</div>", texto) and re.search(r"</span>\s*Meta: IPVA\s*</div>", texto)


def test_alternar_pela_rota_recarrega(client, db):
    meta, rec, origem, destino = _meta(db, categoria_id=nova_categoria(db, "Impostos").id)
    db.commit()
    lanc = _lancs(db, rec)[0]
    r = client.post(f"/lancamentos/{lanc.id}/alternar?formato=tabela")
    assert r.headers["HX-Refresh"] == "true"
    db.expire_all()
    assert db.get(Lancamento, lanc.id).tipo == "transferencia"


def test_aporte_entre_contas_do_saldo_liquido_nao_muda_o_liquido(db):
    from app.services.saldos import saldo_liquido, saldo_liquido_em
    origem, destino = nova_conta(db, "Corrente", saldo=100000), nova_conta(db, "Poupança")  # as duas no líquido
    meta, rec, _, _ = _meta(db, origem=origem, destino=destino)
    db.commit()
    saldos = saldos_contas(db, HOJE, "2026-10")
    assert saldo_liquido(saldos, "previsto_mes") == 100000  # sai de uma e entra na outra
    assert saldo_liquido_em(db, date(2026, 10, 31)) == 100000


def test_caixinha_da_meta_mostra_o_aporte_agendado(db):
    from app.services.caixinhas import agendado_caixinha
    meta, rec, origem, destino = _meta(db)
    caixinha = caixinhas_da_meta(db, meta)[0]
    pendentes = [l for l in _lancs(db, rec) if l.status == "pendente"]
    assert agendado_caixinha(db, caixinha) == -sum(l.valor for l in pendentes) > 0
    alternar_efetivado(db, pendentes[0])  # pago: vira entrada de verdade na caixinha, sai do agendado
    assert agendado_caixinha(db, caixinha) == -sum(l.valor for l in pendentes[1:])
