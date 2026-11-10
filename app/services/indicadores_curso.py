"""Indicadores e alertas dos métodos do curso (spec §9.2, §9.3, §9.6).

Base dos percentuais: valores projetados do mês (efetivado + pendente), sem lançamentos `ignorar`.
Despesas sem categoria entram em "variáveis" nos alertas; no 50/30/20 ficam à parte ("sem categoria"),
sem inflar os desejos.
"""
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.periodo import primeiro_dia, ultimo_dia
from app.models import (
    Categoria, Conta, Fatura, Lancamento, alertas_dispensados, get_config, get_metas_503020,
)
from app.services.assinaturas import sugestoes_de_inclusao
from app.services.mes_caixa import com_status, no_mes
from app.services.orcamento import painel_orcamento, teto_mes

PROJETADO = ("efetivado", "pendente")
MAPA_INDICADORES_PADRAO = {
    "moradia": ["Moradia", "Aluguel", "Condomínio/IPTU"],  # o pai soma todas as subcategorias
    "alimentacao": ["Alimentação", "Mercado", "Feira/Açougue/Padaria", "Comer fora/Delivery"],
}
_RE_ENCARGO_LIMITE = re.compile(r"JUROS\s+LIMITE|\bIOF\b")
MIN_CATEGORIAS_TODAS_NECESSIDADE = 2


def get_mapa_indicadores(s: Session) -> dict[str, list[str]]:
    mapa = {k: list(v) for k, v in MAPA_INDICADORES_PADRAO.items()}
    for chave, nomes in (get_config(s, "mapa_indicadores", {}) or {}).items():
        if chave in mapa and isinstance(nomes, list):
            mapa[chave] = [str(n) for n in nomes]
    return mapa


def _pct(valor: int, base: int) -> float:
    return round(valor * 100 / base, 1) if base > 0 else 0.0


@dataclass(frozen=True)
class _Mes:
    receitas: int
    despesas: int
    por_categoria: dict[int | None, int]  # centavos positivos
    categorias: dict[int, Categoria]

    @property
    def sobra(self) -> int:
        return self.receitas - self.despesas

    def categoria(self, cid: int | None) -> Categoria | None:
        return self.categorias.get(cid) if cid is not None else None

    def raiz(self, cid: int | None) -> Categoria | None:
        cat = self.categoria(cid)
        if cat is not None and cat.pai_id is not None:
            return self.categorias.get(cat.pai_id, cat)
        return cat

    def total_nomes(self, nomes: list[str]) -> int:
        alvo = set(nomes)
        total = 0
        for cid, valor in self.por_categoria.items():
            cat = self.categoria(cid)
            if cat is None:
                continue
            pai = self.categorias.get(cat.pai_id) if cat.pai_id else None
            if cat.nome in alvo or (pai is not None and pai.nome in alvo):
                total += valor
        return total


def _carregar_mes(s: Session, competencia: str) -> _Mes:
    # dinheiro guardado em objetivos (despesa) ou resgatado (receita) não é gasto nem renda: fica na sobra
    base = (no_mes(competencia), com_status(PROJETADO), Lancamento.ignorar.is_(False), Lancamento.meta_id.is_(None))
    receitas = int(s.scalar(select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
        Lancamento.tipo == "receita", *base)))
    por_categoria = {
        cid: -int(soma) for cid, soma in s.execute(
            select(Lancamento.categoria_id, func.sum(Lancamento.valor))
            .where(Lancamento.tipo == "despesa", *base).group_by(Lancamento.categoria_id))
    }
    return _Mes(receitas, sum(por_categoria.values()), por_categoria,
                {c.id: c for c in s.scalars(select(Categoria))})


def _soma_por(mes: _Mes, chave) -> dict[str, int]:
    resultado: dict[str, int] = {}
    for cid, valor in mes.por_categoria.items():
        rotulo = chave(mes.categoria(cid))
        resultado[rotulo] = resultado.get(rotulo, 0) + valor
    return resultado


def _grupo(cat: Categoria | None) -> str:
    return cat.grupo if cat is not None and cat.grupo in ("fixo", "divida") else "variavel"


@dataclass(frozen=True)
class Fatia503020:
    rotulo: str
    real: int
    pct_real: float
    pct_meta: int
    diferenca: int  # real − meta em centavos (positivo = acima da meta)


@dataclass(frozen=True)
class Painel503020:
    receitas: int
    fatias: list[Fatia503020]
    sem_categoria: int = 0  # despesas sem categoria: ficam fora das fatias
    pct_sem_categoria: float = 0.0  # % das despesas do mês


def _natureza(cat: Categoria | None) -> str:
    if cat is None or cat.grupo == "divida":
        return "divida" if cat is not None else "sem_categoria"
    return "necessidade" if cat.natureza == "necessidade" else "desejo"


def painel_503020(s: Session, competencia: str) -> Painel503020:
    mes = _carregar_mes(s, competencia)
    naturezas = _soma_por(mes, _natureza)
    terceira = naturezas.get("divida", 0) + max(mes.sobra, 0)
    metas = get_metas_503020(s)
    valores = [("Necessidades", naturezas.get("necessidade", 0)), ("Desejos", naturezas.get("desejo", 0)),
               ("Dívidas e sobra", terceira)]
    sem_categoria = naturezas.get("sem_categoria", 0)
    return Painel503020(mes.receitas, [
        Fatia503020(rotulo, valor, _pct(valor, mes.receitas), meta, valor - mes.receitas * meta // 100)
        for (rotulo, valor), meta in zip(valores, metas)
    ], sem_categoria, _pct(sem_categoria, mes.despesas))


@dataclass(frozen=True)
class Alerta:
    chave: str
    nivel: str  # perigo | atencao | info
    titulo: str
    texto: str
    acao_url: str


def _reais(centavos: int) -> str:
    from app.core.dinheiro import formatar_reais
    return formatar_reais(centavos)


def _alertas_de_gastos(s: Session, competencia: str, mes: _Mes) -> list[Alerta]:
    alertas: list[Alerta] = []
    plano = f"/planejamento?mes={competencia}"
    grupos = _soma_por(mes, _grupo)
    fixos, variaveis = grupos.get("fixo", 0), grupos.get("variavel", 0)
    if variaveis > fixos:
        alertas.append(Alerta("variaveis_maior_fixos", "atencao", "Variáveis maiores que os fixos",
                              f"Gastos variáveis ({_reais(variaveis)}) superam os fixos ({_reais(fixos)}). "
                              "É onde dá para ajustar primeiro.", plano))
    if mes.receitas > 0:
        saldo_real = _saldo_efetivado(s, competencia)
        if saldo_real < 0:
            alertas.append(Alerta("sobra_negativa", "perigo", "Mês pago com dívida",
                                  f"O balanço realizado está negativo em {_reais(-saldo_real)}.", plano))
        if mes.despesas > mes.receitas:
            alertas.append(Alerta("saida_maior_receita", "perigo", "Saída acima de 100% da receita",
                                  f"As despesas ({_reais(mes.despesas)}) passam das receitas ({_reais(mes.receitas)}).",
                                  plano))
    nomes_moradia = get_mapa_indicadores(s)["moradia"]
    moradia = mes.total_nomes(nomes_moradia)
    outras: dict[int | None, int] = {}
    for cid, valor in mes.por_categoria.items():
        raiz = mes.raiz(cid)
        if raiz is None or raiz.nome not in nomes_moradia:
            chave = raiz.id if raiz is not None else None
            outras[chave] = outras.get(chave, 0) + valor
    if moradia > 0 and outras and moradia > max(outras.values()):
        alertas.append(Alerta("moradia_maior_fatia", "info", "Moradia é a maior fatia",
                              f"Moradia soma {_reais(moradia)}, mais que qualquer outra categoria.", plano))
    com_gasto = [mes.categoria(cid) for cid in mes.por_categoria if mes.categoria(cid) is not None]
    if len(com_gasto) >= MIN_CATEGORIAS_TODAS_NECESSIDADE and all(c.natureza == "necessidade" for c in com_gasto):
        alertas.append(Alerta("tudo_necessidade", "info", "Tudo marcado como necessidade",
                              "Todas as categorias com gasto estão como necessidade. Vale revisar o que é desejo.",
                              "/configuracoes?aba=categorias"))
    return alertas


def _saldo_efetivado(s: Session, competencia: str) -> int:
    from app.services.resumo_mes import totais_mes
    return totais_mes(s, competencia).balanco_efetivado


def _alertas_de_cartao_e_encargos(s: Session, competencia: str) -> list[Alerta]:
    alertas: list[Alerta] = []
    faturas = s.execute(
        select(Fatura, Conta).join(Conta, Fatura.cartao_id == Conta.id)
        .where(Fatura.status == "paga_parcial", Fatura.competencia <= competencia).order_by(Fatura.competencia)
    ).all()
    for fatura, cartao in faturas:
        alertas.append(Alerta(f"fatura_parcial:{fatura.id}", "perigo", f"Fatura parcial: {cartao.nome}",
                              "Pagamento parcial gera juros do rotativo. Quite o restante o quanto antes.",
                              f"/cartoes/{cartao.id}"))
    encargos = [
        l for l in s.scalars(
            select(Lancamento).join(Conta, Lancamento.conta_id == Conta.id).where(
                Conta.tipo != "cartao", Lancamento.valor < 0, Lancamento.status != "cancelado",
                Lancamento.ignorar.is_(False),
                Lancamento.data.between(primeiro_dia(competencia), ultimo_dia(competencia))))
        if _RE_ENCARGO_LIMITE.search((l.descricao or "").upper())
    ]
    if encargos:
        total = -sum(l.valor for l in encargos)
        alertas.append(Alerta("juros_cheque_especial", "perigo", "Juros ou IOF de cheque especial",
                              f"{len(encargos)} lançamento(s) de juros/IOF somam {_reais(total)} no mês.",
                              f"/lancamentos?mes={competencia}&q=juros"))
    return alertas


def _alertas_de_orcamento(s: Session, competencia: str) -> list[Alerta]:
    alertas: list[Alerta] = []
    plano = f"/planejamento?mes={competencia}"
    for linha in painel_orcamento(s, competencia):
        if linha.faixa == "estourado":
            alertas.append(Alerta(f"orcamento:{linha.categoria.id}", "atencao", f"Orçamento estourado: {linha.categoria.nome}",
                                  f"{_reais(linha.projetado)} de {_reais(linha.limite)} ({linha.pct}%).", plano))
    teto = teto_mes(s, competencia)
    if teto is not None and teto.faixa == "estourado":
        alertas.append(Alerta("teto_estourado", "perigo", "Teto de gastos atingido",
                              f"{_reais(teto.projetado)} de {_reais(teto.limite)} ({teto.pct}%).", plano))
    return alertas


def _alertas_de_reserva(s: Session, hoje: date) -> list[Alerta]:
    """Reserva sendo consumida: perigo se acaba em até 2 meses (ou não cobre o déficit de um), atenção até 6."""
    from app.models import Meta
    from app.services.reserva_plano import projecao_reserva
    meta = s.scalar(select(Meta).where(Meta.tipo == "reserva_emergencia", Meta.ativa).order_by(Meta.id).limit(1))
    proj = projecao_reserva(s, meta, hoje) if meta is not None else None
    if proj is None or (proj.acaba_em is not None and proj.meses_ate_acabar > 6):
        return []
    perigo = proj.acaba_em is None or proj.meses_ate_acabar <= 2
    meses = f"{proj.meses_ate_acabar:.1f}".replace(".", ",").removesuffix(",0")
    return [Alerta("reserva_acaba", "perigo" if perigo else "atencao",
                   f"Reserva acaba em ~{meses} meses" if proj.acaba_em else "Reserva não cobre o déficit do mês",
                   f"No ritmo atual ({proj.base}) o mês fecha {_reais(proj.deficit)} no vermelho; "
                   f"corte {_reais(proj.cortar)}/mês para parar de consumir.", "/planejamento#reserva")]


def alertas(s: Session, competencia: str, hoje: date) -> list[Alerta]:
    mes = _carregar_mes(s, competencia)
    todos = [
        *_alertas_de_gastos(s, competencia, mes),
        *_alertas_de_cartao_e_encargos(s, competencia),
        *_alertas_de_orcamento(s, competencia),
        *_alertas_de_reserva(s, hoje),
    ]
    for sug in sugestoes_de_inclusao(s, hoje):
        todos.append(Alerta(f"assinatura:{sug.conta_id}:{sug.descricao.strip().lower()}", "info",
                            f"Assinatura sugerida: {sug.descricao}",
                            f"Cobrança de {_reais(sug.valor)} repete há {len(sug.meses)} meses "
                            f"({_reais(sug.custo_anual)} por ano). Quer cadastrar?",
                            "/lancamentos/assinaturas"))
    from app.services.caixinhas import contas_com_livre_negativo
    for d in contas_com_livre_negativo(s):
        todos.append(Alerta(f"livre_negativo:{d.conta.id}", "atencao", f"Caixinhas de {d.conta.nome} acima do saldo",
                            f"As caixinhas somam {_reais(-d.livre)} a mais que o saldo da conta. Confira o saldo com "
                            "o banco ou marque de qual caixinha saiu algum gasto.", f"/contas/{d.conta.id}"))
    dispensados = set(alertas_dispensados(s, competencia))
    ordem = {"perigo": 0, "atencao": 1, "info": 2}
    return sorted((a for a in todos if a.chave not in dispensados), key=lambda a: ordem[a.nivel])
