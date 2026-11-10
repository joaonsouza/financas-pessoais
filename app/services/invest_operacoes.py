"""Cadastro de ativos e operações de investimento, com validação da posição resultante."""
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Ativo, OperacaoInvest
from app.models.investimento import (
    ALOCACOES, CLASSES_INVEST, INDEXADORES, TIPOS_OPERACAO, AtivoDesejado, MetaAtivoCarteira, alocacao_padrao,
)
from app.services.invest_carteira import posicao_do_ativo, series_indicadores
from app.services.invest_conta import desvincular

ROTULOS_CLASSE = {
    "acao": "Ações", "fii": "FIIs", "etf": "ETFs", "exterior": "Exterior", "cripto": "Cripto",
    "renda_fixa": "Renda fixa",
}
ETIQUETAS_CLASSE = {  # tipo do papel, em etiqueta curta ao lado do código
    "acao": "Ação", "fii": "FII", "etf": "ETF", "exterior": "Exterior", "cripto": "Cripto", "renda_fixa": "Título",
}
ROTULOS_TIPO = {
    "compra": "Compra", "venda": "Venda", "aplicacao": "Aplicação", "resgate": "Resgate",
    "ajuste_posicao": "Ajuste de posição", "rendimento": "Rendimento",
}
TIPOS_POR_CLASSE = {  # rendimento: creditado em quantidade pela corretora (renda fixa já rende pela taxa)
    c: ("aplicacao", "resgate", "ajuste_posicao") if c == "renda_fixa"
    else ("compra", "venda", "ajuste_posicao", "rendimento")
    for c in CLASSES_INVEST
}


def sugerir_simbolo(classe: str, codigo: str) -> str:
    """Símbolo de cotação sugerido: B3 -> `.SA`, cripto -> `-USD` (o Yahoo não cota mais em reais); exterior usa o próprio código."""
    codigo = codigo.strip().upper()
    if not codigo or classe == "renda_fixa":
        return ""
    if classe in ("acao", "fii", "etf"):
        return codigo if codigo.endswith(".SA") else f"{codigo}.SA"
    if classe == "cripto":
        return codigo if "-" in codigo else f"{codigo}-USD"
    return codigo


@dataclass(frozen=True)
class DadosAtivo:
    classe: str
    codigo: str
    nome: str = ""
    simbolo_cotacao: str = ""
    corretora_conta_id: int | None = None
    indexador: str | None = None
    taxa: Decimal | None = None
    vencimento: date | None = None
    por_titulos: bool = False  # Tesouro Direto: quantidade de títulos e preço de mercado
    alocacao: str = ""  # chave de ALOCACOES; vazia (ou inválida): `alocacao_padrao`


def criar_ativo(d: DadosAtivo) -> Ativo:
    if d.classe not in CLASSES_INVEST:
        raise ValueError("escolha a classe do ativo")
    codigo = d.codigo.strip()
    if not codigo:
        raise ValueError("informe o código do ativo")
    if d.classe != "renda_fixa":
        codigo = codigo.upper()
    if d.classe == "renda_fixa":
        if d.indexador not in INDEXADORES:
            raise ValueError("informe o indexador do título (pré, % do CDI ou IPCA+)")
        if d.taxa is None or d.taxa <= 0:
            raise ValueError("informe a taxa do título")
    ativo = Ativo(
        classe=d.classe, alocacao=d.alocacao if d.alocacao in ALOCACOES else alocacao_padrao(d.classe, codigo),
        codigo=codigo, nome=d.nome.strip(),
        simbolo_cotacao="" if d.classe == "renda_fixa" else (d.simbolo_cotacao.strip() or sugerir_simbolo(d.classe, codigo)),
        moeda="USD" if d.classe == "exterior" else "BRL", corretora_conta_id=d.corretora_conta_id,
        indexador=d.indexador if d.classe == "renda_fixa" else None,
        taxa=d.taxa if d.classe == "renda_fixa" else None,
        vencimento=d.vencimento if d.classe == "renda_fixa" else None,
        por_titulos=d.por_titulos and d.classe == "renda_fixa",
    )
    return ativo


def adicionar_ativo(s: Session, d: DadosAtivo) -> Ativo:
    """Cadastra o ativo. Se o código já existe encerrado (fora da lista do formulário), reativa o mesmo, com o
    histórico, em vez de recusar."""
    ativo = criar_ativo(d)
    existente = s.scalar(select(Ativo).where(Ativo.classe == ativo.classe, Ativo.codigo == ativo.codigo))
    if existente is not None:
        if existente.ativo:
            raise ValueError(f"o ativo {ativo.codigo} já está cadastrado: escolha-o na lista de ativos")
        existente.ativo = True
        if d.alocacao in ALOCACOES:
            existente.alocacao = d.alocacao
        s.flush()
        return existente
    s.add(ativo)
    try:
        s.flush()
    except IntegrityError as exc:
        s.rollback()
        raise ValueError(f"o ativo {ativo.codigo} já existe nessa classe") from exc
    return ativo


@dataclass(frozen=True)
class DadosOperacao:
    tipo: str
    data: date
    quantidade: Decimal
    preco_unitario: int  # centavos na moeda do ativo (renda fixa: valor total)
    cambio: Decimal | None = None
    taxas: int = 0
    observacao: str = ""
    taxa: Decimal | None = None  # renda fixa: taxa desta aplicação (vazia = a do ativo)
    valor_liquido: int | None = None  # venda/resgate com imposto retido: o que caiu na conta


def _validar(ativo: Ativo, d: DadosOperacao) -> None:
    if d.tipo not in TIPOS_OPERACAO or d.tipo not in TIPOS_POR_CLASSE[ativo.classe]:
        raise ValueError(f"tipo de operação inválido para {ROTULOS_CLASSE[ativo.classe].lower()}")
    if d.tipo == "ajuste_posicao":
        if d.quantidade < 0:
            raise ValueError("a quantidade não pode ser negativa")
    elif d.quantidade <= 0:
        raise ValueError("a quantidade deve ser maior que zero")
    if d.preco_unitario <= 0 and d.tipo not in ("ajuste_posicao", "rendimento"):
        raise ValueError("informe o preço (ou o valor, na renda fixa)")
    if d.preco_unitario < 0 or d.taxas < 0:
        raise ValueError("valores não podem ser negativos")
    if ativo.classe == "exterior" and d.tipo != "rendimento" and (d.cambio is None or d.cambio <= 0):
        raise ValueError("informe o câmbio (R$ por US$) da operação")
    if d.valor_liquido is not None and d.tipo in ("venda", "resgate"):
        bruto = Decimal(d.quantidade) * d.preco_unitario * (d.cambio or 1)
        if d.valor_liquido <= 0 or d.valor_liquido > bruto - d.taxas:
            raise ValueError("o valor líquido deve ser maior que zero e no máximo o bruto menos as taxas")


def salvar_operacao(s: Session, ativo: Ativo, d: DadosOperacao, op: OperacaoInvest | None = None) -> OperacaoInvest:
    """Cria (ou edita, se `op`) uma operação; recusa se a posição resultante ficar inconsistente.

    No rendimento, `d.quantidade` é o saldo total na corretora depois do crédito (o que se sabe); guarda-se a
    diferença para a posição de antes.
    """
    existentes = [o for o in s.scalars(select(OperacaoInvest).where(OperacaoInvest.ativo_id == ativo.id))
                  if op is None or o.id != op.id]
    chave_nova = (d.data, op.id if op else float("inf"))
    cdi, ipca = series_indicadores(s)
    if d.tipo == "rendimento":  # só quantidade: sem preço, taxas nem câmbio (não tem custo)
        d = replace(d, preco_unitario=0, taxas=0, cambio=None)
        _validar(ativo, d)
        anteriores = sorted((o for o in existentes if (o.data, o.id) < chave_nova), key=lambda o: (o.data, o.id))
        antes = posicao_do_ativo(ativo, anteriores, cdi, ipca, d.data).quantidade
        if d.quantidade <= antes:
            raise ValueError(f"o saldo informado ({_qtd(d.quantidade)}) precisa ser maior que a posição de "
                             f"{_qtd(antes)} em {d.data:%d/%m/%Y}; para diminuir, use venda ou ajuste de posição")
        d = replace(d, quantidade=d.quantidade - antes)
    _validar(ativo, d)
    quantidade = Decimal(1) if ativo.classe == "renda_fixa" and not ativo.por_titulos else d.quantidade
    campos = dict(
        ativo_id=ativo.id, tipo=d.tipo, data=d.data, quantidade=quantidade, preco_unitario=d.preco_unitario,
        cambio=d.cambio if ativo.classe == "exterior" else None, taxas=d.taxas, observacao=d.observacao.strip(),
        taxa=d.taxa if ativo.classe == "renda_fixa" and d.tipo == "aplicacao" else None,
        valor_liquido=d.valor_liquido if d.tipo in ("venda", "resgate") else None,
    )
    candidata = OperacaoInvest(**campos)
    todas = sorted([*existentes, candidata], key=lambda o: (o.data, o.id or 0) if o is not candidata else chave_nova)
    posicao_do_ativo(ativo, todas, cdi, ipca)  # ValueError (pt-BR) se a venda/resgate passar da posição
    if op is None:
        s.add(candidata)
        s.flush()
        return candidata
    for chave, valor in campos.items():
        setattr(op, chave, valor)
    s.flush()
    return op


def _qtd(q: Decimal) -> str:
    return f"{q.normalize():f}".replace(".", ",")


def saldo_depois(s: Session, ativo: Ativo, op: OperacaoInvest) -> Decimal:
    """Posição logo depois da operação (o que o formulário do rendimento mostra: o saldo total)."""
    ops = [o for o in s.scalars(select(OperacaoInvest).where(OperacaoInvest.ativo_id == ativo.id)
                                .order_by(OperacaoInvest.data, OperacaoInvest.id)) if (o.data, o.id) <= (op.data, op.id)]
    cdi, ipca = series_indicadores(s)
    return posicao_do_ativo(ativo, ops, cdi, ipca, op.data).quantidade


def excluir_ativo(s: Session, ativo: Ativo) -> None:
    """Apaga o ativo com todas as operações (e as transferências ligadas) e as metas dele nas carteiras."""
    for op in s.scalars(select(OperacaoInvest).where(OperacaoInvest.ativo_id == ativo.id)):
        desvincular(s, op)
        s.delete(op)
    _apagar_ativo(s, ativo)


def _apagar_ativo(s: Session, ativo: Ativo) -> None:
    s.execute(delete(MetaAtivoCarteira).where(MetaAtivoCarteira.ativo_id == ativo.id))
    s.execute(delete(AtivoDesejado).where(AtivoDesejado.ativo_id == ativo.id))
    s.flush()
    s.delete(ativo)
    s.flush()


def excluir_operacao(s: Session, op: OperacaoInvest) -> bool:
    """Exclui a operação (e a transferência ligada). Sem outras operações, o ativo sai junto: devolve True."""
    ativo = s.get(Ativo, op.ativo_id)
    restantes = [o for o in s.scalars(select(OperacaoInvest).where(OperacaoInvest.ativo_id == op.ativo_id)
                                      .order_by(OperacaoInvest.data, OperacaoInvest.id)) if o.id != op.id]
    cdi, ipca = series_indicadores(s)
    posicao_do_ativo(ativo, restantes, cdi, ipca)  # não deixa a exclusão criar uma venda sem posição
    desvincular(s, op)
    s.delete(op)
    s.flush()
    if not restantes:
        _apagar_ativo(s, ativo)
    return not restantes
