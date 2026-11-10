from datetime import date

from sqlalchemy.orm import Session

from app.models import Categoria, Conta, Lancamento

INICIO = date(2026, 10, 1)


def nova_conta(s: Session, nome: str = "Nubank", *, tipo: str = "corrente", saldo: int = 0,
               sl: bool = True, inicio: date = INICIO) -> Conta:
    conta = Conta(nome=nome, tipo=tipo, saldo_inicial=saldo, entra_saldo_liquido=sl, data_saldo_inicial=inicio)
    s.add(conta)
    s.flush()
    return conta


def novo_cartao(s: Session, nome: str = "Cartão Nubank", *, pagadora: Conta, fechamento: int = 3,
                vencimento: int = 10, limite: int = 1_000_000, inicio: date = INICIO) -> Conta:
    cartao = Conta(
        nome=nome, tipo="cartao", saldo_inicial=0, entra_saldo_liquido=False, data_saldo_inicial=inicio,
        limite=limite, dia_fechamento=fechamento, dia_vencimento=vencimento, conta_pagamento_id=pagadora.id,
    )
    s.add(cartao)
    s.flush()
    return cartao


def nova_categoria(s: Session, nome: str = "Mercado", *, tipo: str = "despesa", pai: Categoria | None = None,
                   grupo: str | None = "variavel", natureza: str | None = "necessidade",
                   essencial: bool = False, cor: str = "#22c55e", inflacao: str | None = None) -> Categoria:
    cat = Categoria(nome=nome, tipo=tipo, pai_id=pai.id if pai else None, grupo=grupo,
                    natureza=natureza, essencial=essencial, cor=cor, inflacao=inflacao)
    s.add(cat)
    s.flush()
    return cat


def novo_lancamento(s: Session, conta: Conta, data: date, valor: int, *, tipo: str | None = None,
                    status: str = "efetivado", categoria: Categoria | None = None,
                    descricao: str = "Teste", fatura_id: int | None = None,
                    ignorar: bool = False) -> Lancamento:
    lanc = Lancamento(
        conta_id=conta.id, data=data, valor=valor, descricao=descricao, status=status,
        tipo=tipo or ("receita" if valor > 0 else "despesa"),
        categoria_id=categoria.id if categoria else None, fatura_id=fatura_id,
        ignorar=ignorar,
    )
    s.add(lanc)
    s.flush()
    return lanc
