"""Cada provisão antiga vira uma meta anual que guarda automático e conta como despesa, sem mexer nos lançamentos."""
import sqlite3
from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import sessionmaker

import app.main as main
import app.services.backup as backup
from app.core import relogio
from app.db import Base, criar_engine
from app.main import _rotina_inicial, criar_app
from app.models import Caixinha, Conta, Lancamento, Meta, MovimentoCaixinha, Provisao, Recorrencia, get_config, set_config
from app.services.caixinhas import criar_caixinha, saldo_caixinha
from app.services.metas import (
    aplicar_pague_primeiro, caixinhas_da_meta, migrar_para_caixinhas, migrar_provisoes_para_metas, plano_aporte,
    recalcular_metas_automaticas, vincular_aportes_recorrentes,
)
from app.services.recorrencia import gerar_pendentes
from tests.factories import nova_categoria, nova_conta

HOJE = date(2026, 10, 5)


def _provisao_antiga(db, nome="IPTU", *, itau=None, caixa=None, caixinha_em="destino", saldo_destino=0,
                     rec_campos=None, **campos):
    """Como o app gravava: provisão + caixinha + recorrência de despesa com origem_provisao_id."""
    itau = itau or nova_conta(db, "Itaú", saldo=500000)
    caixa = caixa or nova_conta(db, "Caixinha Provisões", saldo=saldo_destino)
    cat = nova_categoria(db, f"Impostos {nome}")
    caixinha = None
    if caixinha_em:
        caixinha = criar_caixinha(db, caixa if caixinha_em == "destino" else itau, nome, HOJE, icone="calendar-clock")
        db.add(MovimentoCaixinha(caixinha_id=caixinha.id, data=HOJE, valor=30000, descricao="já tinha"))
    prov = Provisao(**({"nome": nome, "valor_anual": 120000, "conta_origem_id": itau.id,
                        "conta_destino_id": caixa.id, "dia": 10, "data_alvo": date(2027, 2, 15),
                        "repete_anual": True, "categoria_id": cat.id,
                        "caixinha_id": caixinha.id if caixinha else None} | campos))
    db.add(prov)
    db.flush()
    rec = Recorrencia(**({"descricao": f"Provisão: {nome}", "tipo": "despesa", "conta_id": itau.id,
                          "conta_destino_id": caixa.id, "categoria_id": cat.id, "valor": 18000, "dia": 10,
                          "inicio": "2026-10", "fim": "2027-02", "caixinha_id": caixinha.id if caixinha else None,
                          "origem_provisao_id": prov.id} | (rec_campos or {})))
    db.add(rec)
    db.flush()
    gerar_pendentes(db, HOJE)
    return prov, rec, caixinha, cat


def _n_lancamentos(db) -> int:
    return db.scalar(select(func.count(Lancamento.id)))


def test_provisao_vira_meta_com_os_mesmos_valores(db):
    prov, rec, caixinha, cat = _provisao_antiga(db)
    origem, destino, prov_id = prov.conta_origem_id, prov.conta_destino_id, prov.id
    antes = _n_lancamentos(db)

    assert migrar_provisoes_para_metas(db, HOJE) == 1

    meta = db.scalar(select(Meta).where(Meta.nome == "IPTU"))
    assert (meta.tipo, meta.horizonte, meta.valor_alvo, meta.prazo, meta.repete_anual) == (
        "objetivo", "ano", 120000, date(2027, 2, 15), True)
    assert (meta.conta_id, meta.conta_origem_id, meta.dia_pagar, meta.categoria_id) == (destino, origem, 10, cat.id)
    assert (meta.pagar_primeiro, meta.aporte_automatico, meta.aporte_como_despesa, meta.icone) == (
        True, True, True, "calendar-clock")
    assert (rec.origem_meta_id, rec.origem_provisao_id) == (meta.id, None) and rec.aporte_como_despesa
    assert db.get(Caixinha, caixinha.id).meta_id == meta.id
    assert db.get(Provisao, prov_id) is None
    assert _n_lancamentos(db) == antes   # nenhum lançamento criado ou apagado
    assert plano_aporte(db, meta, HOJE).guardado == 30000           # o guardado vem da caixinha


def test_provisao_sem_vencimento_vira_aporte_fixo_de_um_doze_avos(db):
    _provisao_antiga(db, data_alvo=None, valor_anual=100001)
    migrar_provisoes_para_metas(db, HOJE)
    meta = db.scalar(select(Meta).where(Meta.nome == "IPTU"))
    assert (meta.aporte_automatico, meta.valor_mensal_pagar, meta.prazo) == (False, 8334, None)


def test_migracao_e_idempotente(db):
    _provisao_antiga(db)
    assert migrar_provisoes_para_metas(db, HOJE) == 1
    assert migrar_provisoes_para_metas(db, HOJE) == 0
    assert db.scalar(select(func.count(Meta.id))) == 1


def test_recorrencia_legada_de_transferencia_vira_despesa_da_categoria(db):
    _, rec, _, cat = _provisao_antiga(db, rec_campos={"tipo": "transferencia", "categoria_id": None})
    antes = _n_lancamentos(db)
    migrar_provisoes_para_metas(db, HOJE)
    assert (rec.tipo, rec.categoria_id) == ("despesa", cat.id) and rec.aporte_como_despesa
    assert _n_lancamentos(db) == antes


def test_provisao_sem_caixinha_ganha_caixinha_vazia_na_conta_de_destino(db):
    _, rec, _, _ = _provisao_antiga(db, caixinha_em=None, saldo_destino=200000)
    migrar_provisoes_para_metas(db, HOJE)
    migrar_para_caixinhas(db, HOJE)
    meta = db.scalar(select(Meta).where(Meta.nome == "IPTU"))
    (caixinha,) = caixinhas_da_meta(db, meta)
    assert (caixinha.conta_id, caixinha.icone, saldo_caixinha(db, caixinha)) == (meta.conta_id, "calendar-clock", 0)
    assert rec.caixinha_id == caixinha.id
    assert plano_aporte(db, meta, HOJE).guardado == 0   # o saldo da conta não vira guardado da meta


def test_caixinha_da_provisao_em_outra_conta_fica_e_a_meta_ganha_uma_vazia(db):
    _, _, antiga, _ = _provisao_antiga(db, caixinha_em="origem", saldo_destino=200000)
    migrar_provisoes_para_metas(db, HOJE)
    migrar_para_caixinhas(db, HOJE)
    meta = db.scalar(select(Meta).where(Meta.nome == "IPTU"))
    assert db.get(Caixinha, antiga.id).meta_id is None
    (caixinha,) = caixinhas_da_meta(db, meta)
    assert caixinha.conta_id == meta.conta_id and saldo_caixinha(db, caixinha) == 0


def test_recorrencia_de_provisao_apagada_perde_a_referencia(db):
    itau, caixa = nova_conta(db, "Itaú"), nova_conta(db, "Caixinha Provisões")
    rec = Recorrencia(descricao="Provisão: Seguro", tipo="transferencia", conta_id=itau.id, conta_destino_id=caixa.id,
                      valor=10000, dia=5, inicio="2026-01", fim="2026-03", ativa=False, origem_provisao_id=3)
    db.add(rec)
    db.flush()
    migrar_provisoes_para_metas(db, HOJE)
    assert rec.origem_provisao_id is None


def test_id_de_meta_apagada_nao_herda_recorrencia_nem_lancamentos(db):
    prov, rec_prov, _, _ = _provisao_antiga(db)
    viagem = Meta(nome="Viagem", tipo="objetivo", valor_alvo=500000, pagar_primeiro=True,
                  conta_origem_id=prov.conta_origem_id, dia_pagar=6, valor_mensal_pagar=5000)
    db.add(viagem)
    db.flush()
    rec_morta = aplicar_pague_primeiro(db, viagem, HOJE)          # despesa sem conta, ligada à meta
    pago = db.scalars(select(Lancamento).where(Lancamento.recorrencia_id == rec_morta.id)
                      .order_by(Lancamento.data)).first()
    pago.status = "efetivado"                                     # o aporte de outubro caiu
    viagem.pagar_primeiro = False
    aplicar_pague_primeiro(db, viagem, HOJE)                      # encerrada, como fazia o excluir antigo
    id_antigo = viagem.id
    db.commit()
    # o excluir antigo não limpava as referências: a meta some e o id dela fica nas recorrências e lançamentos
    db.execute(text("PRAGMA foreign_keys=OFF"))
    db.execute(delete(Meta).where(Meta.id == id_antigo))
    db.execute(text("UPDATE recorrencia SET meta_id = :m WHERE id = :r"), {"m": id_antigo, "r": rec_morta.id})
    db.commit()
    db.execute(text("PRAGMA foreign_keys=ON"))
    db.expire_all()

    migrar_provisoes_para_metas(db, HOJE)
    meta = db.scalar(select(Meta).where(Meta.nome == "IPTU"))
    assert meta.id == id_antigo                                      # o SQLite reaproveitou o id
    vincular_aportes_recorrentes(db)
    recalcular_metas_automaticas(db, HOJE)

    ativas = list(db.scalars(select(Recorrencia).where(Recorrencia.origem_meta_id == meta.id, Recorrencia.ativa)))
    assert [r.id for r in ativas] == [rec_prov.id]                   # um aporte só, o da provisão
    morta = db.get(Recorrencia, rec_morta.id)
    assert (morta.origem_meta_id, morta.meta_id) == (None, None)
    assert db.get(Lancamento, pago.id).meta_id is None               # o aporte da meta apagada não vai para a nova


def test_varias_provisoes_numa_execucao(db):
    itau, caixa = nova_conta(db, "Itaú", saldo=500000), nova_conta(db, "Caixinha Provisões")
    criadas = [_provisao_antiga(db, nome, itau=itau, caixa=caixa, valor_anual=valor)
               for nome, valor in (("IPTU", 120000), ("IPVA", 240000), ("Seguro", 60000))]
    antes = _n_lancamentos(db)

    assert migrar_provisoes_para_metas(db, HOJE) == 3

    metas = {m.nome: m for m in db.scalars(select(Meta))}
    assert {n: m.valor_alvo for n, m in metas.items()} == {"IPTU": 120000, "IPVA": 240000, "Seguro": 60000}
    for prov_nome, (_, rec, caixinha, _) in zip(("IPTU", "IPVA", "Seguro"), criadas):
        assert rec.origem_meta_id == metas[prov_nome].id and caixinha.meta_id == metas[prov_nome].id
    assert db.scalar(select(func.count(Provisao.id))) == 0
    assert _n_lancamentos(db) == antes
    assert migrar_provisoes_para_metas(db, HOJE) == 0


def test_varias_recorrencias_da_mesma_provisao(db):
    itau, caixa = nova_conta(db, "Itaú", saldo=500000), nova_conta(db, "Caixinha Provisões")
    # a transferência antiga, encerrada, e a despesa que a substituiu
    antiga = Recorrencia(descricao="Provisão: IPTU", tipo="transferencia", conta_id=itau.id, conta_destino_id=caixa.id,
                         valor=10000, dia=10, inicio="2026-01", fim="2026-09", ativa=True)
    db.add(antiga)
    db.flush()
    prov, atual, _, cat = _provisao_antiga(db, itau=itau, caixa=caixa)
    antiga.origem_provisao_id = prov.id
    db.flush()
    antes = _n_lancamentos(db)

    migrar_provisoes_para_metas(db, HOJE)
    meta = db.scalar(select(Meta))
    for rec in (antiga, atual):
        assert (rec.origem_meta_id, rec.origem_provisao_id, rec.tipo, rec.categoria_id) == (
            meta.id, None, "despesa", cat.id)
    assert _n_lancamentos(db) == antes
    recalcular_metas_automaticas(db, HOJE)
    assert recalcular_metas_automaticas(db, HOJE) == 0
    pendentes = db.scalars(select(Lancamento.competencia_recorrencia).where(
        Lancamento.recorrencia_id.in_((antiga.id, atual.id)), Lancamento.status == "pendente",
        Lancamento.conta_id == itau.id)).all()
    assert len(pendentes) == len(set(pendentes))                     # um aporte por mês


# --- abertura do app ---


def _app_com_provisao(url):
    aplicacao = criar_app(url, rotinas=True)
    with aplicacao.state.fabrica_sessao() as s:
        _provisao_antiga(s)
        s.commit()
    return aplicacao


def test_falha_da_migracao_desfaz_grava_o_erro_e_o_app_sobe(monkeypatch):
    def quebrar(s, hoje):
        s.add(Meta(nome="pela metade", tipo="objetivo"))
        s.flush()
        raise RuntimeError("deu ruim")

    monkeypatch.setattr(main, "migrar_provisoes_para_metas", quebrar)
    aplicacao = _app_com_provisao("sqlite://")
    with TestClient(aplicacao, base_url="http://127.0.0.1") as client:
        texto = client.get("/").text
    with aplicacao.state.fabrica_sessao() as s:
        assert s.scalar(select(func.count(Meta.id))) == 0              # desfeito
        assert s.scalar(select(func.count(Provisao.id))) == 1          # as provisões ficam como estavam
        erro = get_config(s, "erro_migracao_metas")
    aplicacao.state.engine.dispose()
    assert erro and "deu ruim" in erro
    assert "A conversão das provisões em metas falhou" in texto and "deu ruim" in texto


def test_migracao_que_da_certo_limpa_o_erro_anterior():
    aplicacao = _app_com_provisao("sqlite://")
    with aplicacao.state.fabrica_sessao() as s:
        set_config(s, "erro_migracao_metas", "01/10/2026 10:00 — antigo")
        s.commit()
    with TestClient(aplicacao, base_url="http://127.0.0.1") as client:
        texto = client.get("/").text
    with aplicacao.state.fabrica_sessao() as s:
        assert get_config(s, "erro_migracao_metas") is None
        assert s.scalar(select(func.count(Meta.id))) == 1
    aplicacao.state.engine.dispose()
    assert "A conversão das provisões em metas falhou" not in texto


AGORA = datetime(2026, 10, 5, 8, 30, 15, tzinfo=relogio.FUSO)
COPIA = "financas.antes-metas-unificadas-20261005-083015.db"


def _banco_em_arquivo(tmp_path, com_provisao=True):
    arquivo = tmp_path / "financas.db"
    engine = criar_engine(f"sqlite:///{arquivo}")
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(engine, expire_on_commit=False)
    with fabrica() as s:
        if com_provisao:
            _provisao_antiga(s)
        set_config(s, "pasta_backup", str(tmp_path / "backups"))
        s.commit()
    return engine, fabrica, arquivo


def _copias(tmp_path):
    return sorted(p.name for p in tmp_path.glob("financas.antes-metas-unificadas*"))


def _provisoes_da_copia(copia):
    con = sqlite3.connect(copia)
    try:
        return [n for (n,) in con.execute("SELECT nome FROM provisao")]
    finally:
        con.close()


def test_copia_do_banco_antes_de_migrar_leva_data_e_hora(tmp_path, monkeypatch):
    monkeypatch.setattr(relogio, "agora", lambda: AGORA)
    engine, fabrica, arquivo = _banco_em_arquivo(tmp_path)

    _rotina_inicial(fabrica, arquivo)

    assert _copias(tmp_path) == [COPIA]
    assert _provisoes_da_copia(tmp_path / COPIA) == ["IPTU"]
    with fabrica() as s:
        assert s.scalar(select(func.count(Provisao.id))) == 0
        meta = s.scalar(select(Meta))
        _provisao_antiga(s, "IPVA", itau=s.get(Conta, meta.conta_origem_id), caixa=s.get(Conta, meta.conta_id))
        s.commit()
    _rotina_inicial(fabrica, arquivo)                                  # mesmo segundo: a que já existe não é sobrescrita
    assert _provisoes_da_copia(tmp_path / COPIA) == ["IPTU"]
    engine.dispose()


def test_copia_manual_com_o_nome_antigo_fica_intacta(tmp_path, monkeypatch):
    monkeypatch.setattr(relogio, "agora", lambda: AGORA)
    engine, fabrica, arquivo = _banco_em_arquivo(tmp_path)
    manual = tmp_path / "financas.antes-metas-unificadas.db"
    manual.write_bytes(b"minha copia")
    _rotina_inicial(fabrica, arquivo)
    assert manual.read_bytes() == b"minha copia" and (tmp_path / COPIA).exists()
    engine.dispose()


def test_copia_que_falha_nao_deixa_arquivo_e_nao_migra(tmp_path, monkeypatch):
    monkeypatch.setattr(relogio, "agora", lambda: AGORA)
    engine, fabrica, arquivo = _banco_em_arquivo(tmp_path)

    def copiar_pela_metade(origem, alvo):
        Path(alvo).write_bytes(b"pela metade")
        raise OSError("disco cheio")

    monkeypatch.setattr(backup, "_copiar", copiar_pela_metade)
    _rotina_inicial(fabrica, arquivo)

    assert _copias(tmp_path) == []
    with fabrica() as s:
        assert s.scalar(select(func.count(Provisao.id))) == 1          # sem cópia, não migra
        assert s.scalar(select(func.count(Meta.id))) == 0
        assert "disco cheio" in get_config(s, "erro_migracao_metas")
    engine.dispose()


def test_sem_provisoes_nao_copia_o_banco(tmp_path):
    engine, fabrica, arquivo = _banco_em_arquivo(tmp_path, com_provisao=False)
    _rotina_inicial(fabrica, arquivo)
    assert _copias(tmp_path) == []
    engine.dispose()


def test_erro_ao_gravar_o_aviso_nao_impede_o_app_de_subir(monkeypatch, caplog):
    def quebrar(s, hoje):
        raise RuntimeError("deu ruim")

    def config_travada(s, chave, valor):
        raise RuntimeError("banco travado")

    monkeypatch.setattr(main, "migrar_provisoes_para_metas", quebrar)
    monkeypatch.setattr(main, "set_config", config_travada)
    aplicacao = _app_com_provisao("sqlite://")
    with TestClient(aplicacao, base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 200
    aplicacao.state.engine.dispose()
    assert "banco travado" in caplog.text
