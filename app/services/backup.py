import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

PASTA_BACKUP_PADRAO = Path(r"Z:\Meu Drive\Finanças\App\app-financas-backups")
PREFIXO = "financas-"
FORMATO = "%Y%m%d-%H%M%S"


def listar_backups(destino: Path) -> list[Path]:
    if not destino.exists():
        return []
    return sorted(destino.glob(f"{PREFIXO}*.db"), reverse=True)


def _copiar(origem: Path, alvo: Path) -> None:
    con_origem = sqlite3.connect(origem)
    con_alvo = sqlite3.connect(alvo)
    try:
        con_origem.backup(con_alvo)
    finally:
        con_alvo.close()
        con_origem.close()


def copia_ao_lado(db_path: Path, nome: str) -> Path | None:
    """Cópia do banco na pasta dele (antes de uma migração de dados); a que já existe não é sobrescrita.
    Vai primeiro para um .tmp e só ganha o nome final inteira; falhando, o .tmp é apagado e o erro sobe."""
    alvo = db_path.with_name(nome)
    if alvo.exists():
        return None
    temporario = alvo.with_name(alvo.name + ".tmp")
    try:
        _copiar(db_path, temporario)
        temporario.rename(alvo)
    except BaseException:
        temporario.unlink(missing_ok=True)
        raise
    return alvo


def fazer_backup(db_path: Path, destino: Path, agora: datetime, manter: int = 30) -> Path:
    destino.mkdir(parents=True, exist_ok=True)
    arquivo = destino / f"{PREFIXO}{agora.strftime(FORMATO)}.db"
    _copiar(db_path, arquivo)
    for antigo in listar_backups(destino)[manter:]:
        antigo.unlink()
    return arquivo


def _data_do_backup(arquivo: Path) -> datetime:
    return datetime.strptime(arquivo.stem.removeprefix(PREFIXO), FORMATO)


def precisa_backup(destino: Path, agora: datetime, intervalo: timedelta = timedelta(hours=24)) -> bool:
    backups = listar_backups(destino)
    return not backups or agora - _data_do_backup(backups[0]) >= intervalo


def restaurar_backup(arquivo: Path, db_path: Path) -> None:
    _copiar(arquivo, db_path)
