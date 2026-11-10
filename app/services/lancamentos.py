from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.periodo import somar_meses, somar_meses_data, validar_competencia, validar_intervalo
from app.models import Caixinha, Categoria, Conta, Fatura, Lancamento, OperacaoInvest, Recorrencia, Tag
from app.services.faturas import (
    atualizar_status,
    competencia_na_data,
    fatura_da_compra,
    obter_ou_criar_fatura,
)
from app.services.saldos import saldo_atual

SINAL = {"receita": 1, "despesa": -1}


def obter_tags(s: Session, nomes: list[str]) -> list[Tag]:
    tags = []
    for nome in dict.fromkeys(n.strip().lower() for n in nomes if n.strip()):
        tag = s.scalar(select(Tag).where(Tag.nome == nome))
        if tag is None:
            tag = Tag(nome=nome)
            s.add(tag)
        tags.append(tag)
    return tags


def _status(efetivado: bool) -> str:
    return "efetivado" if efetivado else "pendente"


def criar_lancamento(s: Session, *, conta: Conta, data: date, descricao: str, valor_abs: int, tipo: str,
                     categoria_id: int | None = None, efetivado: bool = False,
                     tags: list[str] | None = None, observacao: str | None = None,
                     ignorar: bool = False, fatura_competencia: str | None = None) -> Lancamento:
    if tipo not in SINAL:
        raise ValueError(f"tipo inválido para lançamento simples: {tipo}")
    if valor_abs <= 0:
        raise ValueError("o valor deve ser maior que zero")
    if conta.tipo == "cartao" and tipo != "despesa":
        raise ValueError("no cartão de crédito só é possível lançar despesa")
    lanc = Lancamento(
        conta_id=conta.id, data=data, descricao=descricao.strip(), valor=SINAL[tipo] * valor_abs,
        tipo=tipo, status=_status(efetivado), categoria_id=categoria_id, observacao=observacao,
        ignorar=ignorar,
    )
    if conta.tipo == "cartao":
        if fatura_competencia:
            lanc.fatura_id = obter_ou_criar_fatura(s, conta, validar_competencia(fatura_competencia)).id
        else:
            lanc.fatura_id = fatura_da_compra(s, conta, data).id
    lanc.tags = obter_tags(s, tags or [])
    s.add(lanc)
    s.flush()
    return lanc


def criar_transferencia(s: Session, *, origem: Conta, destino: Conta, data: date, valor_abs: int,
                        descricao: str = "Transferência",
                        efetivado: bool = False, ignorar: bool = False) -> tuple[Lancamento, Lancamento]:
    if origem.id == destino.id:
        raise ValueError("origem e destino devem ser contas diferentes")
    if destino.tipo == "cartao" or origem.tipo == "cartao":
        raise ValueError("para cartão use o pagamento de fatura")
    if origem.moeda != destino.moeda:
        raise ValueError("transferência entre contas de moedas diferentes ainda não é suportada")
    if valor_abs <= 0:
        raise ValueError("o valor deve ser maior que zero")
    status = _status(efetivado)
    saida = Lancamento(conta_id=origem.id, data=data, descricao=descricao, valor=-valor_abs,
                       tipo="transferencia", status=status, ignorar=ignorar)
    entrada = Lancamento(conta_id=destino.id, data=data, descricao=descricao, valor=valor_abs,
                         tipo="transferencia", status=status, ignorar=ignorar)
    s.add_all([saida, entrada])
    s.flush()
    saida.transferencia_par_id = entrada.id
    entrada.transferencia_par_id = saida.id
    s.flush()
    return saida, entrada


def criar_compra_parcelada(s: Session, *, conta: Conta, data: date, descricao: str, valor_total: int,
                           parcelas: int, categoria_id: int | None = None, efetivado_primeira: bool = False,
                           tags: list[str] | None = None, ignorar: bool = False,
                           fatura_competencia: str | None = None, observacao: str | None = None) -> list[Lancamento]:
    if parcelas < 2:
        raise ValueError("compra parcelada precisa de pelo menos 2 parcelas")
    if valor_total <= 0:
        raise ValueError("o valor deve ser maior que zero")
    base, resto = divmod(valor_total, parcelas)
    return _criar_serie(s, conta=conta, data=data, descricao=descricao, tipo="despesa",
                        valores=[-(base + (resto if i == 0 else 0)) for i in range(parcelas)],
                        categoria_id=categoria_id, efetivado_primeira=efetivado_primeira, tags=tags,
                        ignorar=ignorar, fatura_competencia=fatura_competencia, observacao=observacao)


def criar_lancamento_repetido(s: Session, *, conta: Conta, data: date, descricao: str, valor_abs: int, vezes: int,
                              tipo: str, categoria_id: int | None = None, efetivado_primeira: bool = False,
                              tags: list[str] | None = None, ignorar: bool = False,
                              fatura_competencia: str | None = None,
                              observacao: str | None = None, intervalo: int = 1) -> list[Lancamento]:
    """O mesmo valor lançado `vezes` vezes, uma a cada `intervalo` meses (o "Repetir" do Mobills), agrupado como parcelas 1/N."""
    if vezes < 2:
        raise ValueError("para repetir, informe pelo menos 2 vezes")
    validar_intervalo(intervalo)
    if tipo not in ("receita", "despesa"):
        raise ValueError("só receitas e despesas podem ser repetidas")
    if valor_abs <= 0:
        raise ValueError("o valor deve ser maior que zero")
    valor = valor_abs if tipo == "receita" else -valor_abs
    return _criar_serie(s, conta=conta, data=data, descricao=descricao, tipo=tipo, valores=[valor] * vezes,
                        categoria_id=categoria_id, efetivado_primeira=efetivado_primeira, tags=tags,
                        ignorar=ignorar, fatura_competencia=fatura_competencia, observacao=observacao,
                        passo=intervalo)


def _criar_serie(s: Session, *, conta: Conta, data: date, descricao: str, tipo: str, valores: list[int],
                 categoria_id: int | None, efetivado_primeira: bool, tags: list[str] | None, ignorar: bool,
                 fatura_competencia: str | None, observacao: str | None, passo: int = 1) -> list[Lancamento]:
    """Um lançamento a cada `passo` meses, um para cada valor, ligados pelo mesmo grupo de parcelas."""
    parcelas = len(valores)
    grupo = uuid4().hex
    tags_obj = obter_tags(s, tags or [])
    comp_base = None
    if conta.tipo == "cartao":
        comp_base = (validar_competencia(fatura_competencia) if fatura_competencia
                     else competencia_na_data(s, conta, data))
    criadas = []
    for i, valor in enumerate(valores):
        lanc = Lancamento(
            conta_id=conta.id, data=somar_meses_data(data, i * passo), descricao=descricao.strip(),
            valor=valor, tipo=tipo,
            status=_status(efetivado_primeira and i == 0), categoria_id=categoria_id,
            grupo_parcela_id=grupo, parcela_num=i + 1, parcela_total=parcelas, ignorar=ignorar,
            observacao=observacao,
        )
        if comp_base:
            lanc.fatura_id = obter_ou_criar_fatura(s, conta, somar_meses(comp_base, i * passo)).id
        lanc.tags = list(tags_obj)
        s.add(lanc)
        criadas.append(lanc)
    s.flush()
    return criadas


def ajustar_saldo(s: Session, conta: Conta, novo_saldo: int, data: date, *,
                  categoria_id: int | None = None) -> Lancamento | None:
    diferenca = novo_saldo - saldo_atual(s, conta)
    if diferenca == 0:
        return None
    lanc = Lancamento(conta_id=conta.id, data=data, descricao="Ajuste de saldo", valor=diferenca,
                      tipo="ajuste", status="efetivado", categoria_id=categoria_id)
    s.add(lanc)
    s.flush()
    return lanc


def ajustar_saldo_inicial(s: Session, conta: Conta, novo_saldo: int) -> int:
    """Corrige o saldo inicial para o saldo atual bater com `novo_saldo`, sem criar lançamento.
    Devolve a diferença aplicada."""
    diferenca = novo_saldo - saldo_atual(s, conta)
    if diferenca:
        conta.saldo_inicial += diferenca
        s.flush()
    return diferenca


def _par(s: Session, lanc: Lancamento) -> Lancamento | None:
    return s.get(Lancamento, lanc.transferencia_par_id) if lanc.transferencia_par_id else None


def _recalcular_fatura_de_pagamento(s: Session, lanc: Lancamento) -> None:
    if lanc.tipo == "pagamento_fatura" and lanc.fatura_id:
        atualizar_status(s, s.get(Fatura, lanc.fatura_id))


def _recorrencia_de_aporte(s: Session, lanc: Lancamento) -> Recorrencia | None:
    if lanc.recorrencia_id is None:
        return None
    rec = s.get(Recorrencia, lanc.recorrencia_id)
    return rec if rec is not None and rec.aporte_como_despesa else None


def _efetivar_aporte(s: Session, lanc: Lancamento, rec: Recorrencia) -> Lancamento:
    """O aporte da meta era uma despesa pendente: ao efetivar vira a transferência de verdade
    (a saída continua, ganha a entrada na conta da meta e perde a categoria)."""
    entrada = Lancamento(
        conta_id=rec.conta_destino_id, data=lanc.data, descricao=lanc.descricao, valor=-lanc.valor,
        tipo="transferencia", status="efetivado", recorrencia_id=rec.id,
        competencia_recorrencia=lanc.competencia_recorrencia, origem="recorrencia", caixinha_id=rec.caixinha_id,
        ignorar=lanc.ignorar, transferencia_par_id=lanc.id,
    )
    s.add(entrada)
    s.flush()
    lanc.tipo, lanc.categoria_id, lanc.status, lanc.transferencia_par_id = "transferencia", None, "efetivado", entrada.id
    s.flush()
    return lanc


def _desfazer_aporte(s: Session, lanc: Lancamento, rec: Recorrencia) -> Lancamento:
    """Desfaz a efetivação: some a entrada e a saída volta a ser a despesa pendente do aporte."""
    saida = lanc if lanc.valor < 0 else _par(s, lanc)
    entrada = _par(s, saida) if saida is not None else None
    if saida is None or entrada is None:
        raise ValueError("transferência do aporte incompleta")
    saida.transferencia_par_id = entrada.transferencia_par_id = None
    s.flush()
    s.delete(entrada)
    saida.tipo, saida.categoria_id, saida.status = "despesa", rec.categoria_id, "pendente"
    s.flush()
    return saida


def alternar_efetivado(s: Session, lanc: Lancamento) -> Lancamento:
    if lanc.status == "cancelado":
        raise ValueError("lançamento cancelado não pode ser efetivado")
    rec = _recorrencia_de_aporte(s, lanc)
    if rec is not None:
        if lanc.tipo == "despesa" and lanc.status == "pendente":
            return _efetivar_aporte(s, lanc, rec)
        if lanc.tipo == "transferencia" and lanc.status == "efetivado" and lanc.transferencia_par_id:
            return _desfazer_aporte(s, lanc, rec)
    novo = "pendente" if lanc.status == "efetivado" else "efetivado"
    lanc.status = novo
    par = _par(s, lanc)
    if par:
        par.status = novo
    s.flush()
    _recalcular_fatura_de_pagamento(s, lanc)
    return lanc


def pode_trocar_conta(s: Session, lanc: Lancamento) -> bool:
    """Receita ou despesa de conta ativa fora do cartão, sem operação de investimento ligada (a posição manda)."""
    conta = s.get(Conta, lanc.conta_id)
    return (lanc.tipo in SINAL and conta.tipo != "cartao" and conta.ativa
            and s.scalar(select(OperacaoInvest.id).where(OperacaoInvest.lancamento_id == lanc.id)) is None)


def _trocar_conta(s: Session, lanc: Lancamento, conta_id: int) -> None:
    if not pode_trocar_conta(s, lanc):
        raise ValueError("a conta deste lançamento não pode ser trocada")
    atual, destino = s.get(Conta, lanc.conta_id), s.get(Conta, conta_id)
    if destino is None or destino.tipo == "cartao" or not destino.ativa:
        raise ValueError("conta inválida")
    if destino.moeda != atual.moeda:
        raise ValueError("a conta escolhida é de outra moeda")
    lanc.conta_id = destino.id
    caixinha = s.get(Caixinha, lanc.caixinha_id) if lanc.caixinha_id else None
    if caixinha is not None and caixinha.conta_id != destino.id:
        lanc.caixinha_id = None


def atualizar_lancamento(s: Session, lanc: Lancamento, *, data: date, descricao: str, valor_abs: int,
                         categoria_id: int | None, tags: list[str],
                         observacao: str | None = None, ignorar: bool | None = None,
                         fatura_competencia: str | None = None, conta_id: int | None = None) -> Lancamento:
    if valor_abs <= 0:
        raise ValueError("o valor deve ser maior que zero")
    if conta_id is not None and conta_id != lanc.conta_id:
        _trocar_conta(s, lanc, conta_id)  # None = fica na conta de sempre
    sinal = -1 if lanc.valor < 0 else 1
    lanc.data = data
    lanc.descricao = descricao.strip()
    lanc.valor = sinal * valor_abs
    lanc.categoria_id = categoria_id
    lanc.observacao = observacao
    if ignorar is not None:
        lanc.ignorar = ignorar
    lanc.tags = obter_tags(s, tags)
    conta = s.get(Conta, lanc.conta_id)
    if conta.tipo == "cartao" and lanc.tipo == "despesa" and lanc.grupo_parcela_id is None:
        if fatura_competencia:
            lanc.fatura_id = obter_ou_criar_fatura(s, conta, validar_competencia(fatura_competencia)).id
        else:
            lanc.fatura_id = fatura_da_compra(s, conta, data).id
    par = _par(s, lanc)
    if par:
        par.data = data
        par.descricao = lanc.descricao
        par.valor = -lanc.valor
        par.ignorar = lanc.ignorar
    s.flush()
    _recalcular_fatura_de_pagamento(s, lanc)
    return lanc


def excluir_lancamento(s: Session, lanc: Lancamento) -> None:
    par = _par(s, lanc)
    afetados = [lanc] + ([par] if par else [])
    fatura_pagamento = lanc.fatura_id if lanc.tipo == "pagamento_fatura" else None
    if lanc.recorrencia_id is not None and lanc.status == "pendente":
        for item in afetados:
            item.status = "cancelado"
    else:
        for item in afetados:
            item.transferencia_par_id = None
        # a operação de investimento continua, só sem a conta vinculada
        for op in s.scalars(select(OperacaoInvest).where(OperacaoInvest.lancamento_id.in_([i.id for i in afetados]))):
            op.lancamento_id = None
        s.flush()
        for item in afetados:
            s.delete(item)
    s.flush()
    if fatura_pagamento:
        atualizar_status(s, s.get(Fatura, fatura_pagamento))


def efetivar_lote(s: Session, ids: list[int]) -> int:
    """Efetiva os lançamentos pendentes informados (demais são ignorados). Devolve quantos foram efetivados."""
    efetivados = 0
    for lanc_id in dict.fromkeys(ids):
        lanc = s.get(Lancamento, lanc_id)
        if lanc is not None and lanc.status == "pendente":
            alternar_efetivado(s, lanc)
            efetivados += 1
    return efetivados


@dataclass
class ResultadoLote:
    alterados: int
    inalterados: int


def editar_lote(s: Session, ids: list[int], *, categoria_id: int | None = None, conta_id: int | None = None,
                status: str | None = None, tags: list[str] | None = None,
                ignorar: bool | None = None) -> ResultadoLote:
    """Aplica só os campos informados (None = não alterar) a cada lançamento. O que não se aplica a um
    lançamento (categoria de outro tipo, conta de cartão, ajuste...) fica como estava e conta como inalterado."""
    if status not in (None, "efetivado", "pendente"):
        raise ValueError("situação inválida")
    destino = None
    if conta_id is not None:
        destino = s.get(Conta, conta_id)
        if destino is None or destino.tipo == "cartao" or not destino.ativa:
            raise ValueError("conta inválida")
    categoria = None
    if categoria_id is not None:
        categoria = s.get(Categoria, categoria_id)
        if categoria is None:
            raise ValueError("categoria inválida")
    novas_tags = obter_tags(s, tags) if tags else []
    alterados = inalterados = 0
    for lanc_id in dict.fromkeys(ids):
        lanc = s.get(Lancamento, lanc_id)
        if lanc is None or lanc.status == "cancelado" or lanc.tipo == "ajuste":
            inalterados += 1
            continue
        mudou = False
        if status and lanc.status != status:
            alternar_efetivado(s, lanc)
            mudou = True
        if lanc.tipo in SINAL:
            if categoria and categoria.tipo == lanc.tipo and lanc.categoria_id != categoria.id:
                lanc.categoria_id = categoria.id
                mudou = True
            if destino and lanc.conta_id != destino.id and s.get(Conta, lanc.conta_id).tipo != "cartao":
                lanc.conta_id = destino.id
                mudou = True
            acrescentar = [t for t in novas_tags if t not in lanc.tags]
            if acrescentar:
                lanc.tags = [*lanc.tags, *acrescentar]
                mudou = True
            if ignorar is not None and lanc.ignorar != ignorar:
                lanc.ignorar = ignorar
                mudou = True
        alterados += mudou
        inalterados += not mudou
    s.flush()
    return ResultadoLote(alterados, inalterados)


def excluir_lote(s: Session, ids: list[int]) -> int:
    """Exclui cada lançamento informado só ele mesmo (sem propagar a séries). Devolve quantos foram excluídos."""
    excluidos = 0
    for lanc_id in dict.fromkeys(ids):
        lanc = s.get(Lancamento, lanc_id)
        if lanc is not None:
            excluir_lancamento(s, lanc)
            excluidos += 1
    return excluidos


def converter_em_transferencia(s: Session, lanc: Lancamento,
                               outra_conta: Conta) -> tuple[Lancamento, Lancamento]:
    """Transforma receita/despesa em transferência, criando a perna oposta em outra_conta.

    Despesa que saía da conta A vira A -> outra; receita que entrava em A vira outra -> A.
    Devolve (lançamento original, perna criada).
    """
    conta = s.get(Conta, lanc.conta_id)
    if lanc.tipo not in SINAL:
        raise ValueError("só receita ou despesa pode virar transferência")
    if conta.tipo == "cartao" or lanc.fatura_id is not None:
        raise ValueError("lançamento de cartão não pode virar transferência")
    if lanc.grupo_parcela_id is not None:
        raise ValueError("lançamento parcelado não pode virar transferência")
    if lanc.recorrencia_id is not None:
        raise ValueError("lançamento recorrente não pode virar transferência")
    if outra_conta.id == lanc.conta_id:
        raise ValueError("escolha uma conta diferente da do lançamento")
    if outra_conta.tipo == "cartao":
        raise ValueError("para cartão use o pagamento de fatura")
    lanc.tipo = "transferencia"
    lanc.categoria_id = None
    par = Lancamento(
        conta_id=outra_conta.id, data=lanc.data, descricao=lanc.descricao, valor=-lanc.valor,
        tipo="transferencia", status=lanc.status, ignorar=lanc.ignorar,
    )
    s.add(par)
    s.flush()
    lanc.transferencia_par_id = par.id
    par.transferencia_par_id = lanc.id
    s.flush()
    return lanc, par
