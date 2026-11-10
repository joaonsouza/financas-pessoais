"""Migra as assinaturas que hoje são recorrências para o cadastro de controle de assinaturas.

Uso (na raiz do projeto):
    uv run python scripts/migrar_assinaturas.py            # só mostra o que seria feito
    uv run python scripts/migrar_assinaturas.py --aplicar  # copia o banco, migra e confirma

A tabela `assinatura` é criada se ainda não existir (o app faz o mesmo ao iniciar).
Com --aplicar: copia data/financas.db para data/financas.antes-assinaturas-AAAAMMDD.db, cria as assinaturas,
encerra as recorrências e apaga os lançamentos pendentes delas da competência atual em diante.
"""
from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from sqlalchemy.orm import Session  # noqa: E402

from app.db import Base, caminho_db_padrao, criar_engine  # noqa: E402
import app.models  # noqa: E402,F401  (registra as tabelas no metadata)
from app.services.migrar_assinaturas import aplicar, planejar  # noqa: E402


def main(argv: list[str]) -> int:
    hoje = date.today()
    engine = criar_engine()
    Base.metadata.create_all(engine)  # cria a tabela `assinatura` se ainda não existir (o app faz o mesmo ao iniciar)
    with Session(engine) as s:
        planos = planejar(s, hoje)
        if not planos:
            print("Nada a migrar: nenhuma recorrência ativa marcada como assinatura.")
            return 0
        for p in planos:
            existente = " (já existe uma assinatura igual: será reaproveitada)" if p.assinatura_existente else ""
            print(f"- {p.recorrencia.descricao}: {p.recorrencia.valor / 100:.2f} · {p.qtd_pendentes} pendente(s) a apagar{existente}")
        if "--aplicar" not in argv:
            print("\nSimulação: nada foi gravado. Rode com --aplicar para migrar.")
            return 0
        origem = caminho_db_padrao()
        copia = origem.with_name(f"financas.antes-assinaturas-{hoje:%Y%m%d}.db")
        shutil.copy2(origem, copia)
        print(f"\nCópia do banco: {copia}")
        aplicar(s, hoje)
        s.commit()
        print(f"Migradas {len(planos)} assinatura(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
