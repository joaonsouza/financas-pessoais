from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import criar_app
from app.models import Categoria, Lancamento, Recorrencia
from app.web.deps import get_hoje
from tests.conftest import HOJE_TESTE
from tests.factories import nova_categoria, nova_conta


def test_aba_categorias_e_edicao(client, db):
    nova_conta(db)
    mercado = nova_categoria(db, "Mercado", essencial=False, natureza="desejo")
    db.commit()
    assert "Mercado" in client.get("/configuracoes?aba=categorias").text

    resposta = client.post(f"/configuracoes/categorias/{mercado.id}", data={
        "nome": "Mercado", "cor": "#22c55e", "grupo": "variavel", "natureza": "necessidade",
        "essencial": "on", "ativa": "on",
    }, follow_redirects=False)
    assert resposta.status_code == 303
    db.refresh(mercado)
    assert (mercado.natureza, mercado.essencial) == ("necessidade", True)


def test_nova_subcategoria_e_duplicada(client, db):
    nova_conta(db)
    alimentacao = nova_categoria(db, "Alimentação")
    db.commit()
    dados = {"nome": "Padaria", "tipo": "despesa", "pai_id": str(alimentacao.id), "grupo": "variavel",
             "natureza": "necessidade", "cor": "#f97316"}
    assert client.post("/configuracoes/categorias", data=dados, follow_redirects=False).status_code == 303
    padaria = db.scalars(select(Categoria).where(Categoria.nome == "Padaria")).one()
    assert padaria.pai_id == alimentacao.id

    resposta = client.post("/configuracoes/categorias", data=dados)
    assert resposta.status_code == 400
    assert "já existe" in resposta.text


def test_nova_recorrencia_gera_pendentes(client, db):
    conta = nova_conta(db)
    db.commit()
    resposta = client.post("/lancamentos/recorrencias", data={
        "descricao": "Aluguel", "tipo": "despesa", "conta_id": str(conta.id), "valor": "1.200,00",
        "dia": "10", "inicio": "2026-10",
    }, follow_redirects=False)
    assert resposta.status_code == 303
    rec = db.scalars(select(Recorrencia)).one()
    assert db.scalar(select(func.count(Lancamento.id)).where(Lancamento.recorrencia_id == rec.id)) == 3


def test_editar_e_encerrar_recorrencia(client, db):
    conta = nova_conta(db)
    db.commit()
    client.post("/lancamentos/recorrencias", data={
        "descricao": "Aluguel", "tipo": "despesa", "conta_id": str(conta.id), "valor": "1.200,00",
        "dia": "10", "inicio": "2026-10",
    })
    rec = db.scalars(select(Recorrencia)).one()

    client.post(f"/lancamentos/recorrencias/{rec.id}", data={
        "descricao": "Aluguel", "conta_id": str(conta.id), "valor": "1.500,00", "dia": "10", "a_partir": "2026-11",
    })
    db.expire_all()
    valores = list(db.scalars(select(Lancamento.valor).where(Lancamento.recorrencia_id == rec.id)
                              .order_by(Lancamento.data)))
    assert valores == [-120000, -150000, -150000]

    client.post(f"/lancamentos/recorrencias/{rec.id}/encerrar", data={"a_partir": "2026-12"})
    db.expire_all()
    assert db.get(Recorrencia, rec.id).fim == "2026-11"


@pytest.fixture
def client_arquivo(tmp_path):
    aplicacao = criar_app(f"sqlite:///{tmp_path / 'financas.db'}", rotinas=False)
    aplicacao.dependency_overrides[get_hoje] = lambda: HOJE_TESTE
    with TestClient(aplicacao, base_url="http://127.0.0.1") as c:
        with aplicacao.state.fabrica_sessao() as s:
            nova_conta(s)
            s.commit()
        yield c
    aplicacao.state.engine.dispose()


def test_backup_e_restauracao(client_arquivo, tmp_path):
    pasta = tmp_path / "bkp"
    client_arquivo.post("/configuracoes/backup/pasta", data={"pasta": str(pasta)})
    resposta = client_arquivo.post("/configuracoes/backup", follow_redirects=False)
    assert resposta.status_code == 303
    arquivos = list(pasta.glob("financas-*.db"))
    assert len(arquivos) == 1
    assert arquivos[0].name in client_arquivo.get("/configuracoes?aba=backup").text

    resposta = client_arquivo.post("/configuracoes/backup/restaurar", data={"arquivo": arquivos[0].name},
                                   follow_redirects=False)
    assert resposta.status_code == 303


def test_restaurar_rejeita_arquivo_fora_da_pasta(client_arquivo, tmp_path):
    client_arquivo.post("/configuracoes/backup/pasta", data={"pasta": str(tmp_path / "bkp")})
    resposta = client_arquivo.post("/configuracoes/backup/restaurar", data={"arquivo": "..\\financas.db"})
    assert resposta.status_code == 400


def test_backup_indisponivel_em_memoria(client, db):
    nova_conta(db)
    db.commit()
    resposta = client.post("/configuracoes/backup")
    assert resposta.status_code == 400


def test_nova_recorrencia_anual(client, db, monkeypatch):
    monkeypatch.setattr("app.services.recorrencia.HORIZONTE_PADRAO", 36)
    conta = nova_conta(db)
    db.commit()
    client.post("/lancamentos/recorrencias", data={
        "descricao": "IPVA", "tipo": "despesa", "conta_id": str(conta.id), "valor": "900,00",
        "dia": "10", "inicio": "2026-12", "intervalo_meses": "12", "fim": "2030-12",
    }, follow_redirects=False)
    rec = db.scalars(select(Recorrencia)).one()
    assert (rec.intervalo_meses, rec.fim) == (12, "2030-12")
    assert db.scalar(select(func.count(Lancamento.id)).where(Lancamento.recorrencia_id == rec.id)) == 3


def test_lista_mostra_regra_e_alterar_semanal_nao_exige_dia(client, db):
    from datetime import date
    conta = nova_conta(db)
    rec = Recorrencia(descricao="Diarista", tipo="despesa", conta_id=conta.id, valor=5000, dia=8, inicio="2026-10",
                      frequencia="semanal", dias_semana="0,3", data_inicio=date(2026, 10, 8))
    db.add(rec)
    db.commit()
    texto = client.get("/lancamentos/recorrencias").text
    assert "toda semana (segunda, quinta)" in texto
    r = client.post(f"/lancamentos/recorrencias/{rec.id}", data={
        "descricao": "Diarista", "conta_id": str(conta.id), "valor": "60,00", "a_partir": "2026-10"},
        follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Recorrencia, rec.id).valor == 6000


def test_categoria_salva_e_limpa_inflacao_pessoal(client, db):
    nova_conta(db)
    cat = nova_categoria(db, "Aluguel", grupo="fixo")
    db.commit()
    dados = {"nome": "Aluguel", "grupo": "fixo", "natureza": "necessidade", "ativa": "on"}
    assert client.post(f"/configuracoes/categorias/{cat.id}", data={**dados, "inflacao": "fixo"}).status_code == 200
    db.refresh(cat)
    assert cat.inflacao == "fixo"
    client.post(f"/configuracoes/categorias/{cat.id}", data=dados)       # formulário sem o campo não mexe
    db.refresh(cat)
    assert cat.inflacao == "fixo"
    client.post(f"/configuracoes/categorias/{cat.id}", data={**dados, "inflacao": ""})
    db.refresh(cat)
    assert cat.inflacao is None
    assert 'name="inflacao"' in client.get(f"/configuracoes?aba=categorias&cat={cat.id}").text


def test_categoria_recusa_inflacao_invalida(client, db):
    nova_conta(db)
    cat = nova_categoria(db, "Aluguel")
    db.commit()
    r = client.post(f"/configuracoes/categorias/{cat.id}",
                    data={"nome": "Aluguel", "grupo": "fixo", "natureza": "necessidade", "inflacao": "x"})
    assert r.status_code == 400
