import sqlite3
from datetime import datetime, timedelta

from app.services.backup import fazer_backup, listar_backups, precisa_backup, restaurar_backup

T0 = datetime(2026, 10, 5, 9, 0, 0)


def _criar_db(caminho, valor):
    con = sqlite3.connect(caminho)
    con.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    con.execute("DELETE FROM t")
    con.execute("INSERT INTO t VALUES (?)", (valor,))
    con.commit()
    con.close()


def _ler(caminho):
    con = sqlite3.connect(caminho)
    valor = con.execute("SELECT v FROM t").fetchone()[0]
    con.close()
    return valor


def test_fazer_backup_copia_dados(tmp_path):
    db = tmp_path / "financas.db"
    _criar_db(db, "original")
    arquivo = fazer_backup(db, tmp_path / "bkp", T0)
    assert arquivo.name == "financas-20261005-090000.db"
    assert _ler(arquivo) == "original"


def test_retencao_mantem_os_mais_recentes(tmp_path):
    db = tmp_path / "financas.db"
    _criar_db(db, "x")
    destino = tmp_path / "bkp"
    for horas in range(3):
        fazer_backup(db, destino, T0 + timedelta(hours=horas), manter=2)
    assert [p.name for p in listar_backups(destino)] == [
        "financas-20261005-110000.db",
        "financas-20261005-100000.db",
    ]


def test_precisa_backup(tmp_path):
    db = tmp_path / "financas.db"
    _criar_db(db, "x")
    destino = tmp_path / "bkp"
    assert precisa_backup(destino, T0) is True
    fazer_backup(db, destino, T0)
    assert precisa_backup(destino, T0 + timedelta(hours=1)) is False
    assert precisa_backup(destino, T0 + timedelta(hours=24)) is True


def test_restaurar_backup(tmp_path):
    db = tmp_path / "financas.db"
    _criar_db(db, "original")
    arquivo = fazer_backup(db, tmp_path / "bkp", T0)
    _criar_db(db, "alterado")
    restaurar_backup(arquivo, db)
    assert _ler(db) == "original"
