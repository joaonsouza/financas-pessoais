from datetime import date

from app.models import RegraCategoria, get_metas_503020, set_config
from app.services.parcelas import teto_parcelas
from app.services.visao_configuracoes import visao_geral
from tests.factories import nova_categoria, nova_conta, novo_lancamento


def _por_chave(cartoes):
    return {c.aba: c for c in cartoes}


def test_backup_com_copias_e_com_erro(db, tmp_path):
    for nome in ("financas-20261027-090000.db", "financas-20261028-093820.db"):
        (tmp_path / nome).write_bytes(b"x")
    backup = _por_chave(visao_geral(db, tmp_path, True))["backup"]
    assert backup.estado == "ok" and "28/10/2026 09:38" in backup.linhas[0] and "2 cópias" in backup.linhas[0]
    set_config(db, "erro_backup", "28/10/2026 10:00 — disco cheio")
    backup = _por_chave(visao_geral(db, tmp_path, True))["backup"]
    assert backup.estado == "aviso" and "disco cheio" in " ".join(backup.linhas)


def test_backup_sem_copias_e_banco_em_memoria(db, tmp_path):
    assert _por_chave(visao_geral(db, tmp_path / "nada", True))["backup"].estado == "vazio"
    assert "memória" in _por_chave(visao_geral(db, tmp_path, False))["backup"].linhas[0]


def test_ia_sem_chave_e_categorias_com_pendencia(db, tmp_path):
    conta = nova_conta(db)
    cat = nova_categoria(db, "Mercado")
    db.add(RegraCategoria(padrao="ATACADAO", categoria_id=cat.id))
    novo_lancamento(db, conta, date(2026, 10, 1), -100, descricao="Sem nada")
    db.flush()
    cartoes = _por_chave(visao_geral(db, tmp_path, True))
    assert cartoes["ia"].estado == "vazio" and "sem chave" in cartoes["ia"].linhas[0]
    assert cartoes["categorias"].estado == "aviso"
    assert "1 categoria · 1 regra automática" in cartoes["categorias"].linhas[0]
    assert "1 lançamento sem categoria" in cartoes["categorias"].linhas[1]


def test_preferencias_resumidas(db, tmp_path):
    prefs = _por_chave(visao_geral(db, tmp_path, True))["preferencias"]
    a, b, c = get_metas_503020(db)
    assert prefs.estado == "ok" and f"{a} · {b} · {c}" in prefs.linhas[0]
    assert "Teto de parcelas" in prefs.linhas[0] and str(teto_parcelas(db) // 100) in prefs.linhas[0].replace(".", "")


def test_rota_abre_na_visao_geral_e_abas_antigas_continuam(client, db):
    nova_conta(db)
    db.commit()
    texto = client.get("/configuracoes").text
    assert 'data-secao="geral"' in texto and "Visão geral" in texto and 'href="?aba=preferencias"' in texto
    assert 'data-secao="categorias"' in client.get("/configuracoes?aba=categorias").text
    assert 'data-secao="backup"' in client.get("/configuracoes?aba=backup").text
    assert 'data-secao="geral"' in client.get("/configuracoes?aba=xpto").text


def test_salvar_preferencias(client, db):
    nova_conta(db)
    db.commit()
    r = client.post("/configuracoes/preferencias", data={"teto": "1.500,00", "necessidades": "60", "desejos": "20",
                                                          "terceira": "20"})
    assert r.status_code == 200 and "aba=preferencias" in str(r.url)
    db.expire_all()
    assert teto_parcelas(db) == 150000 and get_metas_503020(db) == [60, 20, 20]
    r = client.post("/configuracoes/preferencias", data={"teto": "900,00", "necessidades": "60", "desejos": "30",
                                                          "terceira": "20"})
    assert r.status_code == 422 and "somar 100%" in r.text
    db.expire_all()
    assert teto_parcelas(db) == 150000                       # nada gravado quando dá erro
