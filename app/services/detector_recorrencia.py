"""Detector de cobrança recorrente (spec §8.6): sugere virar assinatura/recorrência."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.periodo import competencia_de, somar_meses
from app.importers.normalizacao import normalizar_descricao
from app.models import Lancamento, Recorrencia

TOLERANCIA = 0.05
MESES_RECENTES = 2  # a sequência precisa terminar no mês atual ou nos 2 anteriores


@dataclass
class SugestaoRecorrencia:
    descricao: str
    conta_id: int
    categoria_id: int | None
    valor: int           # centavos, positivo (última cobrança)
    dia: int
    meses: list[str]     # competências consecutivas encontradas
    inicio: str          # competência a partir da qual a recorrência valeria (mês seguinte ao último)

    @property
    def custo_anual(self) -> int:
        return self.valor * 12


def _sequencia_final(por_mes: dict[str, int]) -> list[str]:
    """Maior sequência de meses consecutivos, terminando no último mês, com valor ±5% entre vizinhos."""
    meses = sorted(por_mes)
    seq = [meses[-1]]
    for mes in reversed(meses[:-1]):
        if mes != somar_meses(seq[0], -1):
            break
        if abs(por_mes[mes] - por_mes[seq[0]]) > TOLERANCIA * por_mes[seq[0]]:
            break
        seq.insert(0, mes)
    return seq


def detectar_recorrencias(s: Session, hoje: date) -> list[SugestaoRecorrencia]:
    ativas = {
        (r.conta_id, normalizar_descricao(r.descricao)) for r in s.scalars(select(Recorrencia).where(Recorrencia.ativa))
    }
    grupos: dict[tuple[int, str], list[Lancamento]] = defaultdict(list)
    for l in s.scalars(select(Lancamento).where(
            Lancamento.tipo == "despesa", Lancamento.status != "cancelado", Lancamento.ignorar.is_(False),
            Lancamento.recorrencia_id.is_(None), Lancamento.parcela_num.is_(None))):
        norm = normalizar_descricao(l.descricao)
        if norm and (l.conta_id, norm) not in ativas:
            grupos[(l.conta_id, norm)].append(l)
    limite = somar_meses(competencia_de(hoje), -MESES_RECENTES)
    sugestoes = []
    for (conta_id, _), itens in grupos.items():
        por_mes: dict[str, int] = {}
        ultimo_do_mes: dict[str, Lancamento] = {}
        for l in sorted(itens, key=lambda x: (x.data, x.id)):
            mes = competencia_de(l.data)
            por_mes[mes] = por_mes.get(mes, 0) + abs(l.valor)
            ultimo_do_mes[mes] = l
        seq = _sequencia_final(por_mes)
        if len(seq) < 2 or seq[-1] < limite:
            continue
        ult = ultimo_do_mes[seq[-1]]
        sugestoes.append(SugestaoRecorrencia(
            descricao=ult.descricao, conta_id=conta_id, categoria_id=ult.categoria_id,
            valor=por_mes[seq[-1]], dia=ult.data.day, meses=seq, inicio=somar_meses(seq[-1], 1)))
    return sorted(sugestoes, key=lambda x: -x.valor)


def criar_recorrencia_da_sugestao(s: Session, sug: SugestaoRecorrencia, hoje: date) -> Recorrencia:
    """Cria a recorrência (assinatura) sugerida e gera os pendentes dos próximos meses."""
    from app.services.recorrencia import gerar_pendentes

    rec = Recorrencia(
        descricao=sug.descricao, tipo="despesa", conta_id=sug.conta_id, categoria_id=sug.categoria_id,
        valor=sug.valor, tolerancia_pct=int(TOLERANCIA * 100), dia=min(sug.dia, 28), inicio=sug.inicio,
        eh_assinatura=True,
    )
    s.add(rec)
    s.flush()
    gerar_pendentes(s, hoje)
    return rec
