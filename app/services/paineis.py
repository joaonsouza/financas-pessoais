"""Ordem dos cards do painel lateral de Cartões e Contas, escolhida arrastando (guardada em `config`)."""
from sqlalchemy.orm import Session

from app.models.config import get_config, set_config

# ordem padrão de cada página; ids usados em `data-painel` nos templates
PAINEIS = {
    "cartoes": ("melhor", "limite", "vip", "faturas", "parcelas", "assinaturas"),
    "contas": ("liquido", "dia", "mes", "total"),
}


def _chave(pagina: str) -> str:
    return f"ordem_painel_{pagina}"


def ordem_painel(s: Session, pagina: str) -> list[str]:
    """Ordem salva (só ids conhecidos, sem repetir), com os que faltam no fim, na ordem padrão."""
    padrao = PAINEIS[pagina]
    salvos = [p for p in dict.fromkeys(get_config(s, _chave(pagina), []) or []) if p in padrao]
    return salvos + [p for p in padrao if p not in salvos]


def salvar_ordem_painel(s: Session, pagina: str, ids: list[str]) -> None:
    padrao = PAINEIS[pagina]
    set_config(s, _chave(pagina), [p for p in dict.fromkeys(ids) if p in padrao])
