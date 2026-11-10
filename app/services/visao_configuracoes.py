"""Visão geral de Configurações: um cartão por área, com o estado e o que precisa de atenção."""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.dinheiro import formatar_reais
from app.models import Categoria, Lancamento, RegraCategoria, get_config, get_metas_503020
from app.services.backup import PREFIXO, listar_backups
from app.services.gemini import ultimos4_gemini
from app.services.ia import obter_provedor, ultimos4_da_chave
from app.services.mercado.brapi import ultimos4_do_token
from app.services.mercado.cotacoes import ids_desatualizados
from app.services.parcelas import teto_parcelas

OK, AVISO, VAZIO = "ok", "aviso", "vazio"
NOMES_PROVEDOR = {"gemini": "Gemini", "anthropic": "Anthropic"}


@dataclass
class Cartao:
    aba: str
    titulo: str
    icone: str
    estado: str  # ok | aviso | vazio
    linhas: list[str] = field(default_factory=list)


def _n(qtd: int, singular: str, plural: str) -> str:
    return f"{qtd} {singular if qtd == 1 else plural}"


def _quando_backup(nome: str) -> str:
    try:
        return datetime.strptime(nome.removeprefix(PREFIXO).removesuffix(".db"), "%Y%m%d-%H%M%S").strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return nome


def _backup(s: Session, pasta: Path, disponivel: bool) -> Cartao:
    if not disponivel:
        return Cartao("backup", "Backup", "database-backup", VAZIO, ["Banco em memória: não há o que copiar."])
    copias = listar_backups(pasta)
    erro = get_config(s, "erro_backup", None)
    if not copias:
        linhas = ["Nenhuma cópia ainda."]
    else:
        linhas = [f"Última: {_quando_backup(copias[0].name)} · {_n(len(copias), 'cópia', 'cópias')}"]
    if erro:
        return Cartao("backup", "Backup", "database-backup", AVISO, linhas + [f"O último backup falhou: {erro}"])
    return Cartao("backup", "Backup", "database-backup", OK if copias else VAZIO, linhas)


def _ia(s: Session) -> Cartao:
    provedor = obter_provedor(s)
    chave = ultimos4_gemini() if provedor == "gemini" else ultimos4_da_chave()
    nome = NOMES_PROVEDOR.get(provedor, provedor)
    if chave:
        return Cartao("ia", "IA", "sparkles", OK, [f"{nome} · chave …{chave}"])
    return Cartao("ia", "IA", "sparkles", VAZIO,
                  [f"{nome} · sem chave", "Categorizar e organizar com IA ficam indisponíveis."])


def _categorias(s: Session) -> Cartao:
    categorias = s.scalar(select(func.count(Categoria.id)))
    regras = s.scalar(select(func.count(RegraCategoria.id)))
    sem = s.scalar(select(func.count(Lancamento.id)).where(
        Lancamento.categoria_id.is_(None), Lancamento.tipo.in_(("despesa", "receita")), Lancamento.status != "cancelado"))
    linhas = [f"{_n(categorias, 'categoria', 'categorias')} · {_n(regras, 'regra automática', 'regras automáticas')}"]
    if sem:
        return Cartao("categorias", "Categorias", "tags", AVISO,
                      linhas + [_n(sem, "lançamento sem categoria", "lançamentos sem categoria")])
    return Cartao("categorias", "Categorias", "tags", OK, linhas)


def _investimentos(s: Session) -> Cartao:
    token = ultimos4_do_token()
    linhas = [f"Token da brapi …{token}" if token else "Sem token da brapi (opcional)"]
    desatualizados = len(ids_desatualizados(s))
    if desatualizados:
        return Cartao("investimentos", "Investimentos", "trending-up", AVISO,
                      linhas + [_n(desatualizados, "ativo com cotação desatualizada", "ativos com cotação desatualizada")])
    return Cartao("investimentos", "Investimentos", "trending-up", OK if token else VAZIO, linhas)


def _preferencias(s: Session) -> Cartao:
    a, b, c = get_metas_503020(s)
    return Cartao("preferencias", "Preferências", "sliders-horizontal", OK,
                  [f"Teto de parcelas: {formatar_reais(teto_parcelas(s))}/mês · 50/30/20: {a} · {b} · {c}"])


def visao_geral(s: Session, pasta_backup: Path, backup_disponivel: bool) -> list[Cartao]:
    return [_backup(s, pasta_backup, backup_disponivel), _ia(s), _categorias(s), _investimentos(s), _preferencias(s)]
