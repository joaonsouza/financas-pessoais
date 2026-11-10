from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.periodo import competencia_de, dia_no_mes, somar_meses
from app.models import Conta, Fatura, Lancamento
from app.services.ordem import criterio_ordem, reordenar


def competencia_da_compra(d: date, dia_fechamento: int) -> str:
    comp = competencia_de(d)
    if d <= dia_no_mes(comp, dia_fechamento):
        return comp
    return somar_meses(comp, 1)


def competencia_na_data(s: Session, cartao: Conta, d: date) -> str:
    """A fatura de uma compra pelo dia de fechamento cadastrado, corrigida pelo fechamento real das faturas já
    gravadas: o banco fecha um dia antes ou depois (fim de semana, feriado), e a fatura que veio do banco sabe."""
    comp = competencia_da_compra(d, cartao.dia_fechamento)
    atual = fatura_existente(s, cartao, comp)
    if atual is not None and d > atual.data_fechamento:
        return somar_meses(comp, 1)
    anterior = fatura_existente(s, cartao, somar_meses(comp, -1))
    if anterior is not None and d <= anterior.data_fechamento:
        return anterior.competencia
    return comp


def datas_fatura(cartao: Conta, competencia: str) -> tuple[date, date]:
    fechamento = dia_no_mes(competencia, cartao.dia_fechamento)
    if cartao.dia_vencimento > cartao.dia_fechamento:
        comp_vencimento = competencia
    else:
        comp_vencimento = somar_meses(competencia, 1)
    return fechamento, dia_no_mes(comp_vencimento, cartao.dia_vencimento)


def obter_ou_criar_fatura(s: Session, cartao: Conta, competencia: str) -> Fatura:
    if cartao.tipo != "cartao":
        raise ValueError(f"{cartao.nome} não é cartão de crédito")
    fatura = s.scalar(
        select(Fatura).where(Fatura.cartao_id == cartao.id, Fatura.competencia == competencia)
    )
    if fatura is None:
        fechamento, vencimento = datas_fatura(cartao, competencia)
        fatura = Fatura(
            cartao_id=cartao.id, competencia=competencia,
            data_fechamento=fechamento, data_vencimento=vencimento,
        )
        s.add(fatura)
        s.flush()
    return fatura


def fatura_da_compra(s: Session, cartao: Conta, d: date) -> Fatura:
    return obter_ou_criar_fatura(s, cartao, competencia_na_data(s, cartao, d))


def compras_lancadas(s: Session, fatura: Fatura) -> int:
    """Soma das compras (menos estornos) lançadas na fatura, em centavos positivos."""
    soma = s.scalar(
        select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
            Lancamento.fatura_id == fatura.id,
            Lancamento.tipo == "despesa",
            Lancamento.status != "cancelado",
            Lancamento.ignorar.is_(False),
        )
    )
    return -int(soma)


def a_detalhar(s: Session, fatura: Fatura) -> int:
    """Parte do total informado pelo banco que ainda não virou lançamento (nunca negativa)."""
    if fatura.total_parcial is None:
        return 0
    return max(0, fatura.total_parcial - compras_lancadas(s, fatura))


def total_fatura(s: Session, fatura: Fatura) -> int:
    """Compras lançadas mais o que falta detalhar do total que o banco mostra."""
    compras = compras_lancadas(s, fatura)
    if fatura.total_parcial is None:
        return compras
    return max(compras, fatura.total_parcial)


def definir_total_parcial(s: Session, fatura: Fatura, valor: int | None, hoje: date) -> None:
    """Guarda (ou apaga, com None) o total da fatura aberta visto no app do banco."""
    if valor is not None and valor < 0:
        raise ValueError("o total da fatura não pode ser negativo")
    fatura.total_parcial = valor
    fatura.total_parcial_em = None if valor is None else hoje
    s.flush()
    atualizar_status(s, fatura)


def total_pago(s: Session, fatura: Fatura) -> int:
    soma = s.scalar(
        select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
            Lancamento.fatura_id == fatura.id,
            Lancamento.tipo == "pagamento_fatura",
            Lancamento.conta_id == fatura.cartao_id,
            Lancamento.status == "efetivado",
            Lancamento.ignorar.is_(False),
        )
    )
    return int(soma)


def atualizar_status(s: Session, fatura: Fatura) -> str:
    pago = total_pago(s, fatura)
    if pago <= 0:
        fatura.status = "aberta"
    elif pago >= total_fatura(s, fatura):
        fatura.status = "paga"
    else:
        fatura.status = "paga_parcial"
    s.flush()
    return fatura.status


def em_fatura_paga(s: Session, lanc: Lancamento) -> bool:
    """Compra de fatura já paga: valor, data, fatura e exclusão ficam travados, senão o total pago muda por baixo."""
    if lanc.tipo != "despesa" or lanc.fatura_id is None:
        return False
    fatura = s.get(Fatura, lanc.fatura_id)
    return fatura is not None and fatura.status == "paga"


def status_exibicao(fatura: Fatura, hoje: date, total: int | None = None) -> str:
    """aberta → fechada (passou o fechamento) → vencida (passou o vencimento sem pagar tudo) | paga."""
    if fatura.status != "paga" and hoje > fatura.data_vencimento and total != 0:
        return "vencida"
    if fatura.status == "aberta" and hoje > fatura.data_fechamento:
        return "fechada"
    return fatura.status


def pagar_fatura(s: Session, fatura: Fatura, valor: int, data: date, *,
                 conta_origem: Conta | None = None, efetivado: bool = True) -> tuple[Lancamento, Lancamento]:
    cartao = s.get(Conta, fatura.cartao_id)
    origem = conta_origem or (s.get(Conta, cartao.conta_pagamento_id) if cartao.conta_pagamento_id else None)
    if origem is None:
        raise ValueError(f"{cartao.nome} não tem conta pagadora")
    status = "efetivado" if efetivado else "pendente"
    descricao = f"Pagamento fatura {cartao.nome} {fatura.competencia}"
    saida = Lancamento(conta_id=origem.id, data=data, descricao=descricao, valor=-valor,
                       tipo="pagamento_fatura", status=status, fatura_id=fatura.id)
    entrada = Lancamento(conta_id=cartao.id, data=data, descricao=descricao, valor=valor,
                         tipo="pagamento_fatura", status=status, fatura_id=fatura.id)
    s.add_all([saida, entrada])
    s.flush()
    saida.transferencia_par_id = entrada.id
    entrada.transferencia_par_id = saida.id
    atualizar_status(s, fatura)
    return saida, entrada


def faturas_do_cartao(s: Session, cartao: Conta) -> list[Fatura]:
    consulta = select(Fatura).where(Fatura.cartao_id == cartao.id).order_by(Fatura.competencia.desc())
    return list(s.scalars(consulta))


def fatura_existente(s: Session, cartao: Conta, competencia: str) -> Fatura | None:
    return s.scalar(select(Fatura).where(Fatura.cartao_id == cartao.id, Fatura.competencia == competencia))


def compras_da_fatura(s: Session, fatura: Fatura) -> list[Lancamento]:
    consulta = select(Lancamento).where(
        Lancamento.fatura_id == fatura.id,
        Lancamento.tipo == "despesa",
        Lancamento.status != "cancelado",
    ).order_by(Lancamento.data, Lancamento.id)
    return list(s.scalars(consulta))


def pagamentos_da_fatura(s: Session, fatura: Fatura) -> list[Lancamento]:
    consulta = select(Lancamento).where(
        Lancamento.fatura_id == fatura.id,
        Lancamento.tipo == "pagamento_fatura",
        Lancamento.conta_id == fatura.cartao_id,
        Lancamento.status != "cancelado",
    ).order_by(Lancamento.data, Lancamento.id)
    return list(s.scalars(consulta))


def eh_pagamento_manual(lanc: Lancamento) -> bool:
    """Lançado à mão (não veio nem foi conciliado por importação)."""
    return lanc.origem == "manual" and lanc.hash_dedup is None and lanc.importacao_id is None


def desfazer_pagamento(s: Session, entrada: Lancamento) -> Fatura:
    """Apaga as duas pernas de um pagamento manual de fatura e recalcula a situação da fatura."""
    if entrada.tipo != "pagamento_fatura" or not eh_pagamento_manual(entrada):
        raise ValueError("só dá para desfazer pagamento lançado à mão; o importado se desfaz pela importação")
    fatura = s.get(Fatura, entrada.fatura_id)
    par = s.get(Lancamento, entrada.transferencia_par_id) if entrada.transferencia_par_id else None
    entrada.transferencia_par_id = None
    if par is not None:
        par.transferencia_par_id = None
        s.flush()
        s.delete(par)
    s.delete(entrada)
    s.flush()
    atualizar_status(s, fatura)
    return fatura


def limite_disponivel(s: Session, cartao: Conta) -> int:
    soma = s.scalar(
        select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
            Lancamento.conta_id == cartao.id,
            Lancamento.data >= cartao.data_saldo_inicial,
            Lancamento.ignorar.is_(False),
            or_(
                and_(Lancamento.tipo == "despesa", Lancamento.status != "cancelado"),
                and_(Lancamento.tipo == "pagamento_fatura", Lancamento.status == "efetivado"),
            ),
        )
    )
    detalhar = sum(a_detalhar(s, f) for f in s.scalars(
        select(Fatura).where(Fatura.cartao_id == cartao.id, Fatura.total_parcial.is_not(None))))
    return (cartao.limite or 0) + cartao.saldo_inicial + int(soma) - detalhar


def cartoes_ativos(s: Session) -> list[Conta]:
    """Cartões ativos na ordem escolhida pelo João (os sem posição no fim, por nome): a mesma em todas as telas."""
    return list(s.scalars(select(Conta).where(Conta.tipo == "cartao", Conta.ativa)
                          .order_by(*criterio_ordem(Conta.nome))))


def reordenar_cartoes(s: Session, ids: list[int]) -> None:
    reordenar(cartoes_ativos(s), ids)


ROTULOS_STATUS = {"aberta": "Aberta", "fechada": "Fechada", "vencida": "Vencida", "paga": "Paga",
                  "paga_parcial": "Paga parcialmente"}


def resumo_cartao(s: Session, cartao: Conta, hoje: date) -> dict:
    competencia = competencia_da_compra(hoje, cartao.dia_fechamento)
    fatura = fatura_existente(s, cartao, competencia)
    fechamento, vencimento = datas_fatura(cartao, competencia)
    total = total_fatura(s, fatura) if fatura else 0
    chave = status_exibicao(fatura, hoje, total) if fatura else "aberta"
    disponivel = limite_disponivel(s, cartao)
    limite = cartao.limite or 0
    # última fatura fechada (a anterior à aberta)
    comp_fechada = somar_meses(competencia, -1)
    f_fechada = fatura_existente(s, cartao, comp_fechada)
    _, venc_fechada = datas_fatura(cartao, comp_fechada)
    total_fechada = total_fatura(s, f_fechada) if f_fechada else 0
    chave_fechada = status_exibicao(f_fechada, hoje, total_fechada) if f_fechada else "fechada"
    return {
        "cartao": cartao,
        "competencia": competencia,
        "total": total,
        "fechamento": fechamento,
        "vencimento": vencimento,
        "status": ROTULOS_STATUS[chave],
        "status_chave": chave,
        "disponivel": disponivel,
        "limite": limite,
        # % do limite em uso; negativo quando há crédito sobrando no cartão
        "usado_pct": (limite - disponivel) / limite * 100 if limite else 0.0,
        "fechada": {
            "competencia": comp_fechada, "total": total_fechada, "vencimento": venc_fechada,
            "status": ROTULOS_STATUS[chave_fechada], "status_chave": chave_fechada,
        },
    }


def fatura_do_mes(s: Session, cartao: Conta, mes: str, hoje: date) -> dict:
    """Fatura que vence em `mes` (é o mês que a tela mostra como "Fatura <mês>"), aberta ou fechada."""
    competencia = mes if cartao.dia_vencimento > cartao.dia_fechamento else somar_meses(mes, -1)
    fatura = fatura_existente(s, cartao, competencia)
    fechamento, vencimento = datas_fatura(cartao, competencia)
    total = total_fatura(s, fatura) if fatura else 0
    if fatura:
        chave = status_exibicao(fatura, hoje, total)
    else:
        chave = "fechada" if hoje > fechamento else "aberta"
    return {
        "cartao": cartao, "competencia": competencia, "total": total,
        "fechamento": fechamento, "vencimento": vencimento,
        "status": ROTULOS_STATUS[chave], "status_chave": chave,
    }


def melhor_cartao_hoje(s: Session, hoje: date) -> dict | None:
    """O cartão em que a compra de hoje demora mais para ser paga: a que cai na fatura de vencimento mais
    distante (o fechamento passou há pouco). Empate: o de maior limite disponível. Só cartões com limite livre."""
    melhor = None
    for cartao in s.scalars(select(Conta).where(Conta.tipo == "cartao", Conta.ativa)):
        if not (cartao.dia_fechamento and cartao.dia_vencimento) or not cartao.sugerir_compra:
            continue
        disponivel = limite_disponivel(s, cartao)
        if cartao.limite and disponivel <= 0:
            continue
        competencia = competencia_da_compra(hoje, cartao.dia_fechamento)
        _, vencimento = datas_fatura(cartao, competencia)
        chave = ((vencimento - hoje).days, disponivel)
        if melhor is None or chave > melhor["chave"]:
            melhor = {"chave": chave, "cartao": cartao, "vencimento": vencimento, "dias": chave[0],
                      "disponivel": disponivel}
    return melhor
