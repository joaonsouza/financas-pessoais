"""Painel de assinaturas da fatura do cartão: cada assinatura ativa do cartão contra as cobranças reais daquela
fatura (confirmada / aguardando / não apareceu), mais as sugestões de inclusão do cartão."""
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Assinatura, Conta, Fatura
from app.services.assinaturas import cobrancas, sugestoes_de_inclusao
from app.services.detector_recorrencia import SugestaoRecorrencia

ESTADOS = ("nao_apareceu", "aguardando", "confirmada")


@dataclass
class ItemFatura:
    assinatura: Assinatura
    valor_esperado: int
    valor_real: int | None
    estado: str


@dataclass
class PainelAssinaturas:
    itens: list[ItemFatura] = field(default_factory=list)
    sugestoes: list[SugestaoRecorrencia] = field(default_factory=list)


def assinaturas_da_fatura(s: Session, cartao: Conta, fatura: Fatura, hoje: date) -> PainelAssinaturas:
    ativas = list(s.scalars(select(Assinatura).where(Assinatura.ativa, Assinatura.conta_id == cartao.id)))
    por_ass = cobrancas(s, ativas)
    itens: list[ItemFatura] = []
    for a in ativas:
        na_fatura = [l for l in por_ass[a.id] if l.fatura_id == fatura.id]
        if na_fatura:
            itens.append(ItemFatura(a, a.valor, -na_fatura[0].valor, "confirmada"))
            continue
        if a.intervalo_meses != 1 or (a.inicio and a.inicio > fatura.data_fechamento):
            continue  # só espera cobrança mensal, e só de quem já existia nesta fatura
        estado = "nao_apareceu" if fatura.data_fechamento < hoje else "aguardando"
        itens.append(ItemFatura(a, a.valor, None, estado))
    itens.sort(key=lambda i: (ESTADOS.index(i.estado), i.assinatura.nome.lower()))
    sugestoes = [x for x in sugestoes_de_inclusao(s, hoje) if x.conta_id == cartao.id]
    return PainelAssinaturas(itens, sugestoes)
