"""Migração única: recorrências marcadas como assinatura (despesa) viram `Assinatura` de controle.
A recorrência é encerrada e os pendentes dela, da competência atual em diante, são apagados."""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.periodo import competencia_de, primeiro_dia
from app.importers.normalizacao import normalizar_descricao
from app.models import Assinatura, Lancamento, Recorrencia
from app.services.assinaturas import criar_assinatura
from app.services.recorrencia import encerrar_recorrencia


@dataclass
class PlanoMigracao:
    recorrencia: Recorrencia
    qtd_pendentes: int
    assinatura_existente: Assinatura | None


def _alvos(s: Session) -> list[Recorrencia]:
    return list(s.scalars(select(Recorrencia).where(
        Recorrencia.eh_assinatura, Recorrencia.tipo == "despesa", Recorrencia.ativa).order_by(Recorrencia.id)))


def planejar(s: Session, hoje: date) -> list[PlanoMigracao]:
    """O que seria feito; não grava nada."""
    competencia = competencia_de(hoje)
    planos = []
    for rec in _alvos(s):
        pendentes = s.scalar(select(func.count(Lancamento.id)).where(
            Lancamento.recorrencia_id == rec.id, Lancamento.status == "pendente",
            Lancamento.competencia_recorrencia >= competencia))
        padrao = normalizar_descricao(rec.descricao).replace(" ", "")
        existente = next((a for a in s.scalars(select(Assinatura).where(Assinatura.conta_id == rec.conta_id))
                          if a.padrao.replace(" ", "") == padrao), None)
        planos.append(PlanoMigracao(rec, int(pendentes), existente))
    return planos


def aplicar(s: Session, hoje: date) -> list[PlanoMigracao]:
    """Cria as assinaturas, encerra as recorrências e apaga os pendentes. Não faz commit."""
    planos = planejar(s, hoje)
    for plano in planos:
        rec = plano.recorrencia
        if plano.assinatura_existente is None:
            criar_assinatura(s, nome=rec.descricao, valor=rec.valor, conta_id=rec.conta_id, hoje=hoje, dia=rec.dia,
                             intervalo_meses=rec.intervalo_meses if 1 <= rec.intervalo_meses <= 12 else 1,
                             inicio=primeiro_dia(rec.inicio))
        encerrar_recorrencia(s, rec, a_partir=competencia_de(hoje))
        rec.ativa = False
    s.flush()
    return planos
