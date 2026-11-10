"""Banco de demonstração: uma pessoa fictícia com ~6 meses de vida financeira, novo a cada execução.

    uv run python -m scripts.gerar_demo [caminho] [--semente N]

Padrão: data/demo.db. Apaga o arquivo e recria tudo pelos serviços do app (faturas, parcelas, caixinhas e saldos
batem). Datas relativas a hoje. Investimentos usam o preço histórico do Yahoo; sem rede, a carteira fica só com o CDB.
Nada aqui lê o banco real.
"""
import argparse
import random
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  (registra as tabelas)
from app.core import relogio
from app.core.periodo import competencia_de, primeiro_dia, somar_meses
from app.db import Base, criar_engine, migrar
from app.models import Ativo, Categoria, Conta, Fatura, Meta, OperacaoInvest, Recorrencia, set_config
from app.services.categorias_padrao import semear_categorias
from app.services.faturas import pagar_fatura, total_fatura
from app.services.lancamentos import criar_compra_parcelada, criar_lancamento, criar_transferencia
from app.services.metas import guardar
from app.services.recorrencia import gerar_pendentes

PADRAO = Path(__file__).resolve().parents[1] / "data" / "demo.db"
MESES_DE_HISTORIA = 6

# (categoria, descrições, faixa em reais, compras por semana)
GASTOS_DO_DIA = [
    ("Mercado", ["Supermercado Bom Preço", "Mercado da Esquina", "Atacadão Central"], (60, 420), 1.5),
    ("Feira/Açougue/Padaria", ["Padaria Pão Quente", "Feira de domingo", "Açougue Boi Bom"], (12, 90), 2.0),
    ("Comer fora/Delivery", ["iFood", "Restaurante Sabor Caseiro", "Pizzaria Napoli", "Hamburgueria 99"], (35, 140), 1.5),
    ("Combustível/Apps", ["Uber", "99", "Posto Shell"], (15, 180), 2.0),
    ("Farmácia", ["Drogaria São Paulo", "Farmácia Popular"], (20, 140), 0.5),
    ("Lazer", ["Cinema", "Ingresso show", "Bar do Zé", "Parque"], (40, 260), 0.7),
    ("Roupas", ["Renner", "C&A", "Loja de tênis"], (80, 350), 0.25),
    ("Cuidados pessoais", ["Barbearia", "Salão", "Perfumaria"], (40, 180), 0.4),
    ("Casa/Manutenção", ["Leroy Merlin", "Loja de utilidades"], (30, 300), 0.3),
]
PARCELADAS = [
    ("Notebook", (3500, 6500), 10, "Outros variáveis"),
    ("Geladeira", (2800, 4800), 12, "Casa/Manutenção"),
    ("Passagem aérea", (1200, 3200), 6, "Lazer"),
    ("Celular", (2500, 5500), 10, "Outros variáveis"),
    ("Sofá", (1800, 3500), 8, "Casa/Manutenção"),
]
ASSINATURAS = [("Netflix", 5590), ("Spotify", 2190), ("iCloud", 1490), ("Amazon Prime", 1990), ("YouTube Premium", 2690)]
# (classe, código, símbolo do Yahoo, compra mensal aproximada em reais)
ACOES = [("etf", "BOVA11", "BOVA11.SA", 600), ("etf", "IVVB11", "IVVB11.SA", 500), ("etf", "HASH11", "HASH11.SA", 200),
         ("acao", "ITUB4", "ITUB4.SA", 300), ("fii", "MXRF11", "MXRF11.SA", 250)]


def _reais(rng: random.Random, faixa: tuple[int, int]) -> int:
    """Centavos sorteados na faixa (em reais), com centavos quebrados."""
    return rng.randint(faixa[0] * 100, faixa[1] * 100)


def _categoria(s: Session, nome: str) -> int:
    return s.scalar(select(Categoria.id).where(Categoria.nome == nome))


def _conta(s: Session, nome: str, tipo: str = "corrente", *, saldo: int, inicio: date, sl: bool = True,
           moeda: str = "BRL", cor: str = "#6366f1") -> Conta:
    conta = Conta(nome=nome, tipo=tipo, saldo_inicial=saldo, data_saldo_inicial=inicio, entra_saldo_liquido=sl,
                  moeda=moeda, cor=cor)
    s.add(conta)
    s.flush()
    return conta


def _cartao(s: Session, nome: str, *, pagadora: Conta, limite: int, fecha: int, vence: int, inicio: date,
            cor: str) -> Conta:
    cartao = Conta(nome=nome, tipo="cartao", saldo_inicial=0, data_saldo_inicial=inicio, entra_saldo_liquido=False,
                   limite=limite, dia_fechamento=fecha, dia_vencimento=vence, conta_pagamento_id=pagadora.id, cor=cor)
    s.add(cartao)
    s.flush()
    return cartao


def _dias(inicio: date, ate: date):
    dia = inicio
    while dia <= ate:
        yield dia
        dia += timedelta(days=1)


def _recorrencia(s: Session, *, descricao: str, tipo: str, conta: Conta, valor: int, dia: int, categoria: str | None,
                 competencia: str, variavel: bool = False, assinatura: bool = False) -> Recorrencia:
    rec = Recorrencia(descricao=descricao, tipo=tipo, conta_id=conta.id,
                      categoria_id=_categoria(s, categoria) if categoria else None,
                      valor=valor, valor_variavel=variavel, dia=dia, inicio=competencia, eh_assinatura=assinatura)
    s.add(rec)
    s.flush()
    return rec


def _mensais(s: Session, rng: random.Random, *, inicio: date, hoje: date, contas: dict, cartoes: dict) -> None:
    """Contas do mês: lançamentos avulsos nos meses passados e recorrências do mês corrente em diante (o app
    materializa as pendentes a partir do mês de hoje)."""
    mes_atual = competencia_de(hoje)
    salario = rng.randrange(1_300_000, 2_000_000, 5_000)
    fixas = [  # (descrição, tipo, conta, valor, dia, categoria, variável)
        ("Salário", "receita", contas["Itaú"], salario, 5, "Salário", False),
        ("Aluguel", "despesa", contas["Itaú"], rng.randrange(180_000, 320_000, 5_000), 10, "Aluguel", False),
        ("Condomínio", "despesa", contas["Itaú"], rng.randrange(45_000, 90_000, 1_000), 10, "Condomínio/IPTU", False),
        ("Energia elétrica", "despesa", contas["Nubank"], rng.randrange(14_000, 28_000), 15, "Energia", True),
        ("Internet fibra", "despesa", contas["Nubank"], 11_990, 20, "Internet/Celular", False),
        ("Plano de saúde", "despesa", contas["Itaú"], rng.randrange(40_000, 85_000, 100), 8, "Plano de saúde", False),
        ("Transferência para o Nubank", "transferencia", contas["Itaú"], 150_000, 6, None, False),
    ]
    competencia = competencia_de(inicio)
    while competencia < mes_atual:
        for descricao, tipo, conta, valor, dia, categoria, variavel in fixas:
            data = primeiro_dia(competencia).replace(day=dia)
            if data < inicio:
                continue
            if variavel:
                valor = int(valor * rng.uniform(0.8, 1.25))
            if tipo == "transferencia":
                criar_transferencia(s, origem=conta, destino=contas["Nubank"], data=data, valor_abs=valor,
                                    descricao=descricao, efetivado=True)
                continue
            criar_lancamento(s, conta=conta, data=data, descricao=descricao, valor_abs=valor, tipo=tipo,
                             categoria_id=_categoria(s, categoria), efetivado=True)
        competencia = somar_meses(competencia, 1)
    for descricao, tipo, conta, valor, dia, categoria, variavel in fixas:
        rec = _recorrencia(s, descricao=descricao, tipo=tipo, conta=conta, valor=valor, dia=dia, categoria=categoria,
                           competencia=mes_atual, variavel=variavel)
        if tipo == "transferencia":
            rec.conta_destino_id = contas["Nubank"].id
    # assinaturas nos cartões: só as recorrências (a compra cai na fatura de cada mês)
    nomes_cartoes = list(cartoes)
    for nome, valor in rng.sample(ASSINATURAS, 3):
        cartao = cartoes[rng.choice(nomes_cartoes)]
        dia = rng.randint(3, 25)
        competencia = competencia_de(inicio)
        while competencia < mes_atual:
            criar_lancamento(s, conta=cartao, data=primeiro_dia(competencia).replace(day=dia), descricao=nome,
                             valor_abs=valor, tipo="despesa", categoria_id=_categoria(s, "Assinaturas"),
                             efetivado=True)
            competencia = somar_meses(competencia, 1)
        _recorrencia(s, descricao=nome, tipo="despesa", conta=cartao, valor=valor, dia=dia, categoria="Assinaturas",
                     competencia=mes_atual, assinatura=True)
    gerar_pendentes(s, hoje)
    # as do mês que já passaram do dia ficam como pagas
    for rec in s.scalars(select(Recorrencia)):
        for lanc in s.scalars(select(app.models.Lancamento).where(app.models.Lancamento.recorrencia_id == rec.id,
                                                                  app.models.Lancamento.data <= hoje)):
            lanc.status = "efetivado"
    # renda extra de vez em quando
    for _ in range(rng.randint(1, 3)):
        dia = inicio + timedelta(days=rng.randint(0, (hoje - inicio).days))
        criar_lancamento(s, conta=contas["Nubank"], data=dia, descricao=rng.choice(["Freela", "Venda usado", "Reembolso"]),
                         valor_abs=_reais(rng, (300, 2500)), tipo="receita", categoria_id=_categoria(s, "Renda extra"),
                         efetivado=True)


def _dia_a_dia(s: Session, rng: random.Random, *, inicio: date, hoje: date, contas: dict, cartoes: dict) -> None:
    pagamentos = [*cartoes.values(), *cartoes.values(), contas["Nubank"]]  # mais cartão que Pix
    for dia in _dias(inicio, hoje):
        for categoria, descricoes, faixa, por_semana in GASTOS_DO_DIA:
            if rng.random() < por_semana / 7:
                criar_lancamento(s, conta=rng.choice(pagamentos), data=dia, descricao=rng.choice(descricoes),
                                 valor_abs=_reais(rng, faixa), tipo="despesa",
                                 categoria_id=_categoria(s, categoria), efetivado=True)
    for nome, faixa, vezes, categoria in rng.sample(PARCELADAS, rng.randint(3, 4)):
        dia = inicio + timedelta(days=rng.randint(10, max(11, (hoje - inicio).days - 5)))
        criar_compra_parcelada(s, conta=rng.choice(list(cartoes.values())), data=dia, descricao=nome,
                               valor_total=_reais(rng, faixa), parcelas=vezes, categoria_id=_categoria(s, categoria),
                               efetivado_primeira=True)


def _pagar_faturas(s: Session, hoje: date) -> None:
    """Faturas vencidas: pagas no vencimento pela conta pagadora. As outras ficam em aberto."""
    for fatura in s.scalars(select(Fatura).where(Fatura.data_vencimento < hoje).order_by(Fatura.data_vencimento)):
        total = total_fatura(s, fatura)
        if total > 0:
            pagar_fatura(s, fatura, total, fatura.data_vencimento)


def _metas(s: Session, rng: random.Random, *, inicio: date, hoje: date, contas: dict) -> None:
    reserva = Meta(nome="Reserva de emergência", tipo="reserva_emergencia", conta_id=contas["Reserva"].id,
                   multiplicador_reserva=6, icone="shield", cor="#0ea5e9")
    viagem = Meta(nome=rng.choice(["Viagem para o Japão", "Mochilão na Europa", "Viagem para o Chile"]),
                  tipo="objetivo", valor_alvo=rng.randrange(1_800_000, 3_500_000, 50_000),
                  prazo=somar_meses_data(hoje, rng.randint(8, 14)), conta_id=contas["Inter"].id,
                  icone="plane", cor="#f59e0b")
    s.add_all([reserva, viagem])
    s.flush()
    competencia = competencia_de(inicio)
    while competencia <= competencia_de(hoje):
        data = primeiro_dia(competencia).replace(day=6)
        if inicio <= data <= hoje:
            guardar(s, reserva, valor=rng.randrange(80_000, 150_000, 5_000), data=data, conta=contas["Itaú"], hoje=hoje)
            guardar(s, viagem, valor=rng.randrange(40_000, 120_000, 5_000), data=data, conta=contas["Itaú"], hoje=hoje)
        competencia = somar_meses(competencia, 1)


def somar_meses_data(d: date, meses: int) -> date:
    return primeiro_dia(somar_meses(competencia_de(d), meses)).replace(day=min(d.day, 28))


def _investimentos(s: Session, rng: random.Random, *, inicio: date, hoje: date, corretora: Conta, yahoo) -> int:
    """CDB que já estava na corretora (vira o saldo inicial dela) e compras mensais ao fechamento histórico de cada
    mês; devolve o total comprado em renda variável (centavos), que sai do Itaú em aportes mensais."""
    aplicado = 0
    cdb = Ativo(classe="renda_fixa", codigo="CDB Banco Fictício 110% CDI", nome="CDB 110% do CDI",
                indexador="cdi", taxa=Decimal("110"), vencimento=somar_meses_data(hoje, 36),
                corretora_conta_id=corretora.id)
    s.add(cdb)
    s.flush()
    valor = rng.randrange(1_000_000, 3_000_000, 50_000)
    s.add(OperacaoInvest(ativo_id=cdb.id, tipo="aplicacao", data=inicio, quantidade=Decimal(1), preco_unitario=valor,
                         custodia_conta_id=corretora.id))
    corretora.saldo_inicial = valor
    if yahoo is None:
        return aplicado
    for classe, codigo, simbolo, mensal in ACOES:
        try:
            historico = yahoo.historico_mensal(simbolo, inicio, hoje)
        except Exception as exc:  # sem rede ou símbolo fora do ar: segue sem ele
            print(f"  sem histórico de {codigo}: {exc}", file=sys.stderr)
            continue
        if not historico:
            continue
        ativo = Ativo(classe=classe, codigo=codigo, simbolo_cotacao=simbolo, corretora_conta_id=corretora.id)
        s.add(ativo)
        s.flush()
        for dia, fechamento in historico:
            preco = int((fechamento * 100).quantize(Decimal(1)))
            if preco <= 0:
                continue
            quantidade = max(1, round(mensal * rng.uniform(0.7, 1.3) * 100 / preco))
            s.add(OperacaoInvest(ativo_id=ativo.id, tipo="compra", data=min(dia, hoje), quantidade=Decimal(quantidade),
                                 preco_unitario=preco, custodia_conta_id=corretora.id))
            aplicado += quantidade * preco
    return aplicado


def gerar(caminho: Path = PADRAO, *, semente: int | None = None, hoje: date | None = None, yahoo="rede") -> None:
    """Recria `caminho` do zero. `yahoo`: provedor de histórico ("rede" = Yahoo de verdade; None = sem rede)."""
    hoje = hoje or relogio.hoje()
    rng = random.Random(semente)
    if yahoo == "rede":
        from app.services.mercado.yahoo import Yahoo
        yahoo = Yahoo()
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.unlink(missing_ok=True)
    engine = criar_engine(f"sqlite:///{caminho}")
    Base.metadata.create_all(engine)
    migrar(engine)
    inicio = primeiro_dia(somar_meses(competencia_de(hoje), -MESES_DE_HISTORIA))
    with sessionmaker(engine, expire_on_commit=False)() as s:
        semear_categorias(s)
        set_config(s, "data_inicio", inicio.isoformat())
        contas = {
            "Itaú": _conta(s, "Itaú", saldo=rng.randrange(300_000, 900_000, 100), inicio=inicio, cor="#f97316"),
            "Nubank": _conta(s, "Nubank", saldo=rng.randrange(100_000, 400_000, 100), inicio=inicio, cor="#8b5cf6"),
            "Inter": _conta(s, "Inter", saldo=rng.randrange(50_000, 200_000, 100), inicio=inicio, cor="#f59e0b"),
            "Reserva": _conta(s, "Reserva", saldo=rng.randrange(1_000_000, 2_500_000, 1_000), inicio=inicio,
                              sl=False, cor="#0ea5e9"),
        }
        corretora = _conta(s, "XP Investimentos", "investimento", saldo=0, inicio=inicio, sl=False, cor="#111827")
        _conta(s, "Nomad", saldo=rng.randrange(5_000, 120_000), inicio=hoje, sl=False, moeda="USD", cor="#eab308")
        cartoes = {
            "Nubank": _cartao(s, "Cartão Nubank", pagadora=contas["Itaú"], limite=rng.randrange(800_000, 1_500_000, 50_000),
                              fecha=3, vence=10, inicio=inicio, cor="#8b5cf6"),
            "Itaú": _cartao(s, "Cartão Itaú", pagadora=contas["Itaú"], limite=rng.randrange(1_500_000, 3_000_000, 50_000),
                            fecha=25, vence=2, inicio=inicio, cor="#f97316"),
            "Inter": _cartao(s, "Cartão Inter", pagadora=contas["Itaú"], limite=rng.randrange(400_000, 900_000, 50_000),
                             fecha=10, vence=17, inicio=inicio, cor="#f59e0b"),
        }
        cartoes["Itaú"].vip_vouchers, cartoes["Itaú"].vip_usados = 4, rng.randint(0, 3)
        cartoes["Itaú"].vip_renova_em = somar_meses_data(hoje, rng.randint(2, 10))
        _mensais(s, rng, inicio=inicio, hoje=hoje, contas=contas, cartoes=cartoes)
        _dia_a_dia(s, rng, inicio=inicio, hoje=hoje, contas=contas, cartoes=cartoes)
        _pagar_faturas(s, hoje)
        _metas(s, rng, inicio=inicio, hoje=hoje, contas=contas)
        aplicado = _investimentos(s, rng, inicio=inicio, hoje=hoje, corretora=corretora, yahoo=yahoo)
        # o dinheiro que foi para a corretora saiu do Itaú, em aportes mensais
        meses = MESES_DE_HISTORIA + 1
        for i in range(meses):
            data = primeiro_dia(somar_meses(competencia_de(inicio), i)).replace(day=7)
            if data <= hoje and aplicado // meses > 0:  # sem rede não há renda variável
                criar_transferencia(s, origem=contas["Itaú"], destino=corretora, data=data,
                                    valor_abs=aplicado // meses, descricao="Aporte investimentos", efetivado=True)
        s.commit()
    engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("caminho", nargs="?", default=str(PADRAO))
    parser.add_argument("--semente", type=int, default=None)
    args = parser.parse_args()
    gerar(Path(args.caminho), semente=args.semente)
    print(f"Banco de demonstração criado em {args.caminho}")


if __name__ == "__main__":
    main()
