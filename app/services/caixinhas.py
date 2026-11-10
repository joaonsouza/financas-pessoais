"""Caixinhas: a divisão do dinheiro de cada conta por finalidade.

Saldo da caixinha = movimentos de divisão (remanejamentos) + lançamentos marcados com ela.
Livre da conta = saldo da conta − soma das caixinhas. Rendimento e ajuste de saldo sem caixinha caem no Livre.
Pôr valor numa caixinha sem vir de outra conta (aporte) soma no saldo da conta; distribuir do Livre só divide.
"""
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Caixinha, Conta, Lancamento, MovimentoCaixinha
from app.services.lancamentos import criar_transferencia, excluir_lancamento
from app.services.saldos import saldo_atual


def _do_lancamento(s: Session, caixinha: Caixinha, status: tuple[str, ...]) -> int:
    conta = s.get(Conta, caixinha.conta_id)
    return int(s.scalar(select(func.coalesce(func.sum(Lancamento.valor), 0)).where(
        Lancamento.caixinha_id == caixinha.id, Lancamento.conta_id == caixinha.conta_id,
        Lancamento.status.in_(status), Lancamento.ignorar.is_(False),
        Lancamento.data >= conta.data_saldo_inicial)))


def _da_divisao(s: Session, caixinha: Caixinha) -> int:
    return int(s.scalar(select(func.coalesce(func.sum(MovimentoCaixinha.valor), 0)).where(
        MovimentoCaixinha.caixinha_id == caixinha.id)))


def saldo_caixinha(s: Session, caixinha: Caixinha) -> int:
    return _da_divisao(s, caixinha) + _do_lancamento(s, caixinha, ("efetivado",))


def agendado_caixinha(s: Session, caixinha: Caixinha) -> int:
    """Entradas e saídas ainda pendentes (aporte mensal agendado, por exemplo). O aporte de meta "como despesa"
    pendente é uma despesa na conta de origem, sem perna na caixinha: entra pela recorrência que o gera."""
    from app.models import Recorrencia
    a_receber = -int(s.scalar(select(func.coalesce(func.sum(Lancamento.valor), 0))
                              .join(Recorrencia, Lancamento.recorrencia_id == Recorrencia.id)
                              .where(Recorrencia.caixinha_id == caixinha.id, Recorrencia.origem_meta_id.is_not(None),
                                     Recorrencia.tipo == "despesa", Recorrencia.conta_destino_id.is_not(None),
                                     Lancamento.tipo == "despesa", Lancamento.status == "pendente",
                                     Lancamento.ignorar.is_(False))))
    return _do_lancamento(s, caixinha, ("pendente",)) + a_receber


def caixinhas_da_conta(s: Session, conta_id: int) -> list[Caixinha]:
    return list(s.scalars(select(Caixinha).where(Caixinha.conta_id == conta_id, Caixinha.ativa)
                          .order_by(Caixinha.nome)))


@dataclass(frozen=True)
class LinhaCaixinha:
    caixinha: Caixinha
    saldo: int
    agendado: int


@dataclass(frozen=True)
class DivisaoConta:
    conta: Conta
    saldo: int
    caixinhas: list[LinhaCaixinha]
    livre: int

    @property
    def em_caixinhas(self) -> int:
        return sum(c.saldo for c in self.caixinhas)


def divisao_conta(s: Session, conta: Conta) -> DivisaoConta:
    saldo = saldo_atual(s, conta)
    linhas = [LinhaCaixinha(c, saldo_caixinha(s, c), agendado_caixinha(s, c)) for c in caixinhas_da_conta(s, conta.id)]
    return DivisaoConta(conta, saldo, linhas, saldo - sum(l.saldo for l in linhas))


def _validar_nome(s: Session, conta: Conta, nome: str, ignorar_id: int | None = None) -> str:
    nome = nome.strip()
    if not nome:
        raise ValueError("informe o nome da caixinha")
    if nome.casefold() == "livre":
        raise ValueError('"Livre" é o nome do dinheiro sem caixinha; escolha outro')
    if any(c.nome.casefold() == nome.casefold() and c.id != ignorar_id for c in caixinhas_da_conta(s, conta.id)):
        raise ValueError(f"já existe a caixinha {nome} em {conta.nome}")
    return nome


def criar_caixinha(s: Session, conta: Conta, nome: str, hoje: date, *, inicial: int = 0, do_livre: bool = False,
                   meta_id: int | None = None, icone: str | None = None, cor: str | None = None) -> Caixinha:
    """Nova caixinha. O valor inicial é o que já está nela no banco e soma no saldo da conta;
    com `do_livre`, sai do Livre (dinheiro que a conta já contava)."""
    if conta.tipo == "cartao":
        raise ValueError("cartão de crédito não tem caixinhas")
    if inicial < 0:
        raise ValueError("o valor inicial não pode ser negativo")
    caixinha = Caixinha(conta_id=conta.id, nome=_validar_nome(s, conta, nome), meta_id=meta_id)
    if icone:
        caixinha.icone = icone
    if cor:
        caixinha.cor = cor
    s.add(caixinha)
    s.flush()
    if inicial and do_livre:
        s.add(MovimentoCaixinha(caixinha_id=caixinha.id, data=hoje, valor=inicial, descricao="Valor inicial (do Livre)"))
        s.flush()
    elif inicial:
        aportar(s, caixinha, valor=inicial, data=hoje, descricao=f"Valor inicial: {caixinha.nome}")
    return caixinha


def aportar(s: Session, caixinha: Caixinha, *, valor: int, data: date, descricao: str | None = None) -> Lancamento:
    """Registra dinheiro que está na caixinha no banco e o app não contava: soma no saldo da conta.
    É um ajuste de saldo marcado com a caixinha (não é receita nem despesa do mês)."""
    if valor <= 0:
        raise ValueError("o valor deve ser maior que zero")
    lanc = Lancamento(conta_id=caixinha.conta_id, data=data, descricao=descricao or f"Aporte: {caixinha.nome}",
                      valor=valor, tipo="ajuste", status="efetivado", caixinha_id=caixinha.id)
    s.add(lanc)
    s.flush()
    return lanc


def objetivo_da_conta(s: Session, conta_id: int) -> int | None:
    """Objetivo dono da conta: todas as caixinhas dela são do mesmo objetivo (ex.: conta só da reserva).
    Caixinha nova nessa conta já nasce dele."""
    donos = {c.meta_id for c in caixinhas_da_conta(s, conta_id)}
    return donos.pop() if len(donos) == 1 and None not in donos else None


def vincular_caixinha(s: Session, caixinha: Caixinha, meta_id: int | None) -> None:
    """Faz a caixinha compor um objetivo (o guardado dele soma as caixinhas ligadas). None desliga."""
    from app.models import Meta
    if meta_id is not None:
        meta = s.get(Meta, meta_id)
        if meta is None or meta.tipo == "economia_mensal":
            raise ValueError("objetivo inválido")
        if meta.conta_id != caixinha.conta_id:
            raise ValueError(f"o objetivo {meta.nome} guarda em outra conta")
    caixinha.meta_id = meta_id
    s.flush()


def ajustar_caixinha(s: Session, caixinha: Caixinha, novo_saldo: int, data: date) -> Lancamento | None:
    """Acerta a caixinha com o valor que o banco mostra para ela (rendimento, tarifa...). A diferença vira
    um ajuste de saldo marcado com a caixinha, então a conta muda junto; o Livre não mexe."""
    diferenca = novo_saldo - saldo_caixinha(s, caixinha)
    if diferenca == 0:
        return None
    lanc = Lancamento(conta_id=caixinha.conta_id, data=data, descricao=f"Ajuste de saldo: {caixinha.nome}",
                      valor=diferenca, tipo="ajuste", status="efetivado", caixinha_id=caixinha.id)
    s.add(lanc)
    s.flush()
    return lanc


def editar_caixinha(s: Session, caixinha: Caixinha, nome: str, *, icone: str | None = None,
                    cor: str | None = None) -> None:
    """Nome, ícone e cor; ícone ou cor vazios mantêm o que já tem."""
    caixinha.nome = _validar_nome(s, s.get(Conta, caixinha.conta_id), nome, ignorar_id=caixinha.id)
    if icone:
        caixinha.icone = icone
    if cor:
        caixinha.cor = cor
    s.flush()


def remanejar(s: Session, conta: Conta, *, de: Caixinha | None, para: Caixinha | None, valor: int, data: date) -> None:
    """Passa dinheiro entre caixinhas da mesma conta (None = Livre). No banco nada muda."""
    if valor <= 0:
        raise ValueError("o valor deve ser maior que zero")
    if de is None and para is None or (de is not None and para is not None and de.id == para.id):
        raise ValueError("escolha origem e destino diferentes")
    for c in (de, para):
        if c is not None and c.conta_id != conta.id:
            raise ValueError("as caixinhas devem ser da mesma conta")
    disponivel = saldo_caixinha(s, de) if de is not None else divisao_conta(s, conta).livre
    if valor > disponivel:
        nome = de.nome if de is not None else "Livre"
        raise ValueError(f"{nome} tem só {_reais(max(disponivel, 0))}")
    descricao = f"{de.nome if de else 'Livre'} → {para.nome if para else 'Livre'}"
    grupo = uuid4().hex
    if de is not None:
        s.add(MovimentoCaixinha(caixinha_id=de.id, data=data, valor=-valor, descricao=descricao, grupo=grupo))
    if para is not None:
        s.add(MovimentoCaixinha(caixinha_id=para.id, data=data, valor=valor, descricao=descricao, grupo=grupo))
    s.flush()


def _reais(centavos: int) -> str:
    from app.core.dinheiro import formatar_reais
    return formatar_reais(centavos)


def guardar_na_caixinha(s: Session, caixinha: Caixinha, *, origem: Conta, valor: int, data: date,
                        efetivado: bool = True, descricao: str | None = None) -> tuple[Lancamento, Lancamento]:
    """Dinheiro de outra conta entra na caixinha: transferência para a conta dela, com a entrada marcada."""
    if origem.id == caixinha.conta_id:
        raise ValueError("para mudar dentro da mesma conta, use Remanejar")
    saida, entrada = criar_transferencia(s, origem=origem, destino=s.get(Conta, caixinha.conta_id), data=data,
                                         valor_abs=valor, descricao=descricao or f"Caixinha: {caixinha.nome}",
                                         efetivado=efetivado)
    entrada.caixinha_id = caixinha.id
    s.flush()
    return saida, entrada


def resgatar_da_caixinha(s: Session, caixinha: Caixinha, *, destino: Conta, valor: int, data: date,
                         efetivado: bool = True, descricao: str | None = None) -> tuple[Lancamento, Lancamento]:
    """Dinheiro sai da caixinha para outra conta: transferência com a saída marcada."""
    if destino.id == caixinha.conta_id:
        raise ValueError("para mudar dentro da mesma conta, use Remanejar")
    if valor > saldo_caixinha(s, caixinha):
        raise ValueError(f"a caixinha {caixinha.nome} tem só {_reais(max(saldo_caixinha(s, caixinha), 0))}")
    saida, entrada = criar_transferencia(s, origem=s.get(Conta, caixinha.conta_id), destino=destino, data=data,
                                         valor_abs=valor, descricao=descricao or f"Resgate: {caixinha.nome}",
                                         efetivado=efetivado)
    saida.caixinha_id = caixinha.id
    s.flush()
    return saida, entrada


def marcar_caixinha(s: Session, pernas: list[Lancamento], caixinha_id: int | None) -> None:
    """Marca o lançamento (ou a perna da transferência na conta da caixinha). None desmarca."""
    if caixinha_id is None:
        for lanc in pernas:
            lanc.caixinha_id = None
        s.flush()
        return
    caixinha = s.get(Caixinha, caixinha_id)
    if caixinha is None:
        raise ValueError("caixinha inválida")
    alvo = [l for l in pernas if l.conta_id == caixinha.conta_id]
    if not alvo:
        raise ValueError(f"a caixinha {caixinha.nome} é de outra conta")
    for lanc in pernas:
        lanc.caixinha_id = caixinha.id if lanc in alvo else None
    s.flush()


def excluir_caixinha(s: Session, caixinha: Caixinha) -> None:
    """O saldo dela volta para o Livre; os lançamentos continuam, só sem a marca."""
    from app.models import Meta, Recorrencia
    meta = s.get(Meta, caixinha.meta_id) if caixinha.meta_id else None
    if meta is not None and meta.aporte_como_despesa:
        raise ValueError(f"esta caixinha é da meta {meta.nome}; exclua a meta em Planejamento")
    for lanc in s.scalars(select(Lancamento).where(Lancamento.caixinha_id == caixinha.id)):
        lanc.caixinha_id = None
    for rec in s.scalars(select(Recorrencia).where(Recorrencia.caixinha_id == caixinha.id)):
        rec.caixinha_id = None
    for mov in s.scalars(select(MovimentoCaixinha).where(MovimentoCaixinha.caixinha_id == caixinha.id)):
        s.delete(mov)
    s.flush()
    s.delete(caixinha)
    s.flush()


def contas_com_livre_negativo(s: Session) -> list[DivisaoConta]:
    """Contas cujas caixinhas somam mais que o saldo: o saldo da conta está desatualizado ou faltou marcar uma saída."""
    ids = set(s.scalars(select(Caixinha.conta_id).where(Caixinha.ativa)))
    return [d for d in (divisao_conta(s, s.get(Conta, i)) for i in sorted(ids)) if d.livre < 0]


def movimentos_caixinha(s: Session, caixinha: Caixinha, limite: int = 100) -> list[dict]:
    """Extrato da caixinha (lançamentos marcados e mudanças de divisão), do mais novo ao mais antigo."""
    linhas = [{"id": l.id, "data": l.data, "descricao": l.descricao, "valor": l.valor,
               "pendente": l.status == "pendente", "divisao": False}
              for l in s.scalars(select(Lancamento).where(
                  Lancamento.caixinha_id == caixinha.id, Lancamento.status != "cancelado"))]
    linhas += [{"id": m.id, "data": m.data, "descricao": m.descricao, "valor": m.valor, "pendente": False,
                "divisao": True}
               for m in s.scalars(select(MovimentoCaixinha).where(MovimentoCaixinha.caixinha_id == caixinha.id))]
    return sorted(linhas, key=lambda x: (x["data"], x["id"]), reverse=True)[:limite]


def excluir_movimento(s: Session, caixinha: Caixinha, *, divisao: bool, movimento_id: int) -> None:
    """Apaga um item do extrato da caixinha. Lançamento: some dos lançamentos e do saldo da conta
    (transferência: as duas pernas). Remanejamento: desfaz as duas pontas."""
    if not divisao:
        lanc = s.get(Lancamento, movimento_id)
        if lanc is None or lanc.caixinha_id != caixinha.id:
            raise ValueError("lançamento não encontrado nesta caixinha")
        excluir_lancamento(s, lanc)
        return
    mov = s.get(MovimentoCaixinha, movimento_id)
    if mov is None or mov.caixinha_id != caixinha.id:
        raise ValueError("movimento não encontrado nesta caixinha")
    if mov.grupo:
        pontas = list(s.scalars(select(MovimentoCaixinha).where(MovimentoCaixinha.grupo == mov.grupo)))
    else:
        # remanejamentos de antes do vínculo: a outra ponta tem mesma data, descrição e valor oposto
        ids_conta = select(Caixinha.id).where(Caixinha.conta_id == caixinha.conta_id)
        outra = s.scalar(select(MovimentoCaixinha).where(
            MovimentoCaixinha.grupo.is_(None), MovimentoCaixinha.id != mov.id, MovimentoCaixinha.data == mov.data,
            MovimentoCaixinha.descricao == mov.descricao, MovimentoCaixinha.valor == -mov.valor,
            MovimentoCaixinha.caixinha_id.in_(ids_conta)).limit(1)) if "→" in mov.descricao else None
        pontas = [mov] + ([outra] if outra else [])
    for m in pontas:
        s.delete(m)
    s.flush()
