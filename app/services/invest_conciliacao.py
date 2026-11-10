"""Conciliação das caixinhas de investimento com o valor de mercado das posições guardadas nelas.

As compras e vendas movem dinheiro pelo custo; a variação de mercado só aparece aqui, como um ajuste
"Rendimento investimentos" na caixinha (tipo ajuste: fica fora de receitas, despesas e teto).
"""
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.models import Conta
from app.models.caixinha import Caixinha
from app.services.caixinhas import ajustar_caixinha, saldo_caixinha
from app.services.invest_carteira import resumo_carteira


@dataclass(frozen=True)
class LinhaConciliacao:
    conta: Conta
    caixinha: Caixinha | None  # None: posições fora de caixinha, que não dá para separar do dinheiro livre
    posicoes: int
    saldo: int | None

    @property
    def diferenca(self) -> int | None:
        return None if self.saldo is None else self.posicoes - self.saldo


def conciliacao(s: Session, hoje: date) -> list[LinhaConciliacao]:
    """Um item por local com posição hoje (conta de investimento + caixinha), na ordem conta, caixinha."""
    linhas = []
    for chave, valor in resumo_carteira(s, hoje).por_local.items():
        if chave is None or valor <= 0:
            continue
        conta_id, caixinha_id = chave
        caixinha = s.get(Caixinha, caixinha_id) if caixinha_id else None
        linhas.append(LinhaConciliacao(s.get(Conta, conta_id), caixinha, valor,
                                       saldo_caixinha(s, caixinha) if caixinha else None))
    return sorted(linhas, key=lambda l: (l.conta.nome, l.caixinha.nome if l.caixinha else ""))


def conciliar(s: Session, hoje: date, caixinha_id: int | None = None) -> int:
    """Iguala a caixinha (ou todas, sem `caixinha_id`) ao valor das posições; devolve quantas mudaram."""
    feitos = 0
    for linha in conciliacao(s, hoje):
        if linha.caixinha is None or not linha.diferenca or (caixinha_id and linha.caixinha.id != caixinha_id):
            continue
        lanc = ajustar_caixinha(s, linha.caixinha, linha.posicoes, hoje)
        lanc.descricao = f"Rendimento investimentos: {linha.caixinha.nome}"
        feitos += 1
    s.flush()
    return feitos
