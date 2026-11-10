"""Proposta de importação (deduplicação e conciliação) e aplicação/desfazer.

`propor_importacao` só lê o banco: não grava nada. `aplicar_importacao` grava tudo numa transação
(savepoint) e `desfazer_importacao` restaura o estado anterior a partir do snapshot.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dinheiro import formatar_reais
from app.core.periodo import competencia_de, somar_meses, somar_meses_data
from app.importers.base import ResultadoLeitura, TransacaoBruta
from app.importers.normalizacao import normalizar_descricao
from app.models import Conta, Fatura, Importacao, Lancamento, Recorrencia
from app.services.categorizacao import MODELO_PADRAO, sugerir_categorias
from app.services.faturas import (
    atualizar_status,
    competencia_na_data,
    datas_fatura,
    fatura_existente,
    obter_ou_criar_fatura,
    pagar_fatura,
    total_fatura,
)

DIAS_CONCILIAR = 5
DIAS_TRANSFERENCIA = 2
TOLERANCIA_FATURA = 100  # R$ 1,00 em centavos
PREFIXO_PARCELA = 6
TOLERANCIA_PARCELA = 5  # centavos: parcelas da mesma compra diferem pelo arredondamento

# Padrões de descrição do extrato -> palavras do nome do cartão (docs/design/amostras-importacao.md)
PADROES_PAGAMENTO: list[tuple[str, tuple[str, ...]]] = [
    ("ITAU VISA", ("ITAU", "UNICLASS")),
    ("UNICLASS", ("ITAU", "UNICLASS")),
    ("NU PAGAMENT", ("NUBANK", "NU")),
    ("MERCADO PAG", ("MERCADO",)),
]

ROTULOS_STATUS = {
    "nova": "Nova",
    "parcela": "Parcela",
    "duplicada": "Duplicada",
    "conciliar": "Conciliar",
    "transferencia": "Transferência",
    "pagamento_fatura": "Pagamento de fatura",
}


@dataclass
class LinhaProposta:
    indice: int
    transacao: TransacaoBruta
    descricao_norm: str
    hash_dedup: str
    tipo: str                              # tipo do lançamento a criar: receita | despesa
    status: str = "nova"                   # nova | parcela | duplicada | conciliar | transferencia | pagamento_fatura
    marcada: bool = True
    rotulo: str = ""
    categoria_id: int | None = None
    categoria_origem: str | None = None    # regra | historico | dica | ia
    conciliar_com_id: int | None = None    # lançamento existente (pendente, perna de pagamento...)
    transferencia_com_id: int | None = None
    fatura_id: int | None = None           # pagamento: fatura vinculada
    cartao_id: int | None = None           # pagamento: cartão (quando a fatura ainda não existe)
    fatura_competencia: str | None = None  # pagamento sem fatura existente / compra no cartão
    candidatos_fatura: list[tuple[int, str]] = field(default_factory=list)  # [(fatura_id, rótulo)]
    criar_fatura: list[tuple[int, str, str]] = field(default_factory=list)  # [(cartao_id, competência, rótulo)]
    parcelas_futuras: list[int] = field(default_factory=list)
    ignorar: bool = False                  # entra só para registro: fora de saldos, totais e fatura
    cancelar_ids: list[int] = field(default_factory=list)  # estorno: parcelas futuras pendentes a cancelar


@dataclass
class Proposta:
    resultado: ResultadoLeitura
    conta: Conta
    linhas: list[LinhaProposta]
    nome_arquivo: str = ""
    hash_arquivo: str | None = None
    avisos: list[str] = field(default_factory=list)
    arquivo_repetido_em: date | None = None
    total_importado: int = 0
    total_informado: int | None = None
    divergente: bool = False
    sugestao_fechamento: int | None = None
    aviso_ia: str | None = None


@dataclass
class Escolhas:
    """O que o usuário decidiu na tela de revisão. `None`/ausente = padrão da proposta."""
    marcadas: set[int] | None = None
    categorias: dict[int, int | None] = field(default_factory=dict)
    faturas: dict[int, int] = field(default_factory=dict)
    criar_faturas: dict[int, tuple[int, str]] = field(default_factory=dict)  # linha -> (cartão, competência)
    nao_e_pagamento: set[int] = field(default_factory=set)  # linhas que o usuário confirmou como despesa comum
    criar_parcelas_futuras: bool = True


class PagamentoSemFatura(ValueError):
    """Linha de pagamento de fatura marcada sem fatura escolhida nem confirmação de despesa."""

    def __init__(self, erros: dict[int, str]):
        self.erros = erros
        super().__init__(" ".join(erros.values()))


# ---------------------------------------------------------------- proposta

def calcular_hash(conta_id: int, data: date, valor: int, descricao_norm: str, id_externo: str | None,
                  ocorrencia: int) -> str:
    base = "|".join([str(conta_id), data.isoformat(), str(valor), descricao_norm, id_externo or "", str(ocorrencia)])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _prefixo_comum(a: str, b: str) -> int:
    return len(os.path.commonprefix([a, b]))


def _palavras(texto: str) -> set[str]:
    return set(normalizar_descricao(texto).split())


def _cartoes_do_padrao(cartoes: list[Conta], descricao_norm: str) -> list[Conta]:
    achados: dict[int, Conta] = {}
    for padrao, chaves in PADROES_PAGAMENTO:
        if padrao in descricao_norm:
            for c in cartoes:
                if _palavras(c.nome) & set(chaves):
                    achados[c.id] = c
    return list(achados.values())


def _rotulo_fatura(f: Fatura, cartoes: dict[int, Conta]) -> str:
    return f"{cartoes[f.cartao_id].nome} · vence {f.data_vencimento:%d/%m/%Y}"


def _duplicadas(s: Session, conta: Conta, linhas: list[LinhaProposta]) -> None:
    hashes = set(s.scalars(select(Lancamento.hash_dedup).where(
        Lancamento.conta_id == conta.id, Lancamento.hash_dedup.is_not(None))))
    por_ext: Counter = Counter()
    for ext, d, v in s.execute(select(Lancamento.id_externo, Lancamento.data, Lancamento.valor).where(
            Lancamento.conta_id == conta.id, Lancamento.id_externo.is_not(None),
            Lancamento.status != "cancelado")):
        por_ext[(ext, d, v)] += 1
    for ln in linhas:
        t = ln.transacao
        chave_ext = (t.id_externo, t.data, t.valor)
        if ln.hash_dedup in hashes:
            eh_dup = True
        elif t.id_externo and por_ext[chave_ext] > 0:
            por_ext[chave_ext] -= 1
            eh_dup = True
        else:
            eh_dup = False
        if eh_dup:
            ln.status, ln.marcada, ln.rotulo = "duplicada", False, "Duplicada (já importada)"


def _duplicadas_outra_fonte(s: Session, conta: Conta, linhas: list[LinhaProposta]) -> None:
    """No cartão, a mesma compra pode chegar por arquivos diferentes (o PDF do Itaú cola as palavras, a planilha
    do site não): mesma data e valor e a descrição sem espaços igual (ou com o mesmo começo) é a compra já
    importada. Cada lançamento existente casa uma vez; os lançados à mão (sem hash) seguem a conciliação."""
    if conta.tipo != "cartao":
        return
    existentes: dict[tuple, list[str]] = {}
    for d, v, desc in s.execute(select(Lancamento.data, Lancamento.valor, Lancamento.descricao).where(
            Lancamento.conta_id == conta.id, Lancamento.hash_dedup.is_not(None), Lancamento.status != "cancelado")):
        existentes.setdefault((d, v), []).append(normalizar_descricao(desc).replace(" ", ""))
    # o que já casou pelo hash gasta o seu lançamento antes: a segunda compra igual no mesmo dia continua nova
    for ln in linhas:
        candidatos = existentes.get((ln.transacao.data, ln.transacao.valor), [])
        compacta = ln.descricao_norm.replace(" ", "")
        if ln.status == "duplicada" and compacta in candidatos:
            candidatos.remove(compacta)
    for ln in linhas:
        if ln.status != "nova":
            continue
        candidatos = existentes.get((ln.transacao.data, ln.transacao.valor), [])
        compacta = ln.descricao_norm.replace(" ", "")
        for i, outra in enumerate(candidatos):
            if outra == compacta or _prefixo_comum(outra, compacta) >= PREFIXO_PARCELA:
                del candidatos[i]
                ln.status, ln.marcada, ln.rotulo = "duplicada", False, "Duplicada (já importada por outro arquivo)"
                break
    _iof_de_outra_fonte(s, conta, linhas)


def _iof_de_outra_fonte(s: Session, conta: Conta, linhas: list[LinhaProposta]) -> None:
    """O PDF do Itaú lança o IOF internacional de uma vez ("Repasse de IOF"); a planilha do site, um por compra.
    A soma de um lado igual ao valor do outro, em datas próximas, é o mesmo IOF já importado."""
    def eh_repasse(desc: str) -> bool:
        return normalizar_descricao(desc).replace(" ", "").startswith("REPASSEDEIOF")

    def eh_iof(desc: str) -> bool:
        return normalizar_descricao(desc).replace(" ", "").startswith("IOF")

    novas = [ln for ln in linhas if ln.status == "nova"]
    if not novas:
        return
    inicio = min(ln.transacao.data for ln in novas) - timedelta(days=45)
    fim = max(ln.transacao.data for ln in novas) + timedelta(days=45)
    existentes = list(s.execute(select(Lancamento.data, Lancamento.valor, Lancamento.descricao).where(
        Lancamento.conta_id == conta.id, Lancamento.hash_dedup.is_not(None), Lancamento.status != "cancelado",
        Lancamento.data.between(inicio, fim))))
    rotulo = "Duplicada (o IOF já foi importado por outro arquivo)"
    iof_por_compra = [ln for ln in novas if eh_iof(ln.transacao.descricao)]
    soma = sum(ln.transacao.valor for ln in iof_por_compra)
    if iof_por_compra and any(eh_repasse(d) and v == soma for _, v, d in existentes):
        for ln in iof_por_compra:
            ln.status, ln.marcada, ln.rotulo = "duplicada", False, rotulo
    ja_por_compra = sum(v for _, v, d in existentes if eh_iof(d))
    for ln in novas:
        if ln.status == "nova" and eh_repasse(ln.transacao.descricao) and ja_por_compra == ln.transacao.valor:
            ln.status, ln.marcada, ln.rotulo = "duplicada", False, rotulo


def _data_alterada(s: Session, conta: Conta, linhas: list[LinhaProposta]) -> None:
    """Mesma transação já importada com outra data (o banco muda a data entre um arquivo e outro, como o
    Nubank na fatura aberta): FITID longo (UUID) e valor iguais. Atualiza a data em vez de duplicar."""
    no_arquivo = {(ln.transacao.id_externo, ln.transacao.data) for ln in linhas}
    usados: set[int] = set()
    for ln in linhas:
        t = ln.transacao
        if ln.status != "nova" or not t.id_externo or len(t.id_externo) < 20:
            continue
        for antigo in s.scalars(select(Lancamento).where(
                Lancamento.conta_id == conta.id, Lancamento.id_externo == t.id_externo, Lancamento.valor == t.valor,
                Lancamento.data != t.data, Lancamento.status != "cancelado")):
            if antigo.id in usados or (antigo.id_externo, antigo.data) in no_arquivo:
                continue
            # o Nubank repete FITID e valor em todas as parcelas da compra: outra parcela não é a mesma transação
            if t.parcela_num and antigo.parcela_num and t.parcela_num != antigo.parcela_num:
                continue
            usados.add(antigo.id)
            if antigo.data > t.data:  # arquivo mais velho que o já importado: não volta a data
                ln.status, ln.marcada, ln.rotulo = "duplicada", False, "Duplicada (já importada com data mais nova)"
            else:
                ln.status, ln.conciliar_com_id = "conciliar", antigo.id
                ln.rotulo = f"Já importada; o banco mudou a data ({antigo.data:%d/%m} → {t.data:%d/%m})"
            break


def _estornos(s: Session, conta: Conta, linhas: list[LinhaProposta]) -> None:
    """Estorno de compra parcelada: a compra não terá as próximas parcelas. No mesmo arquivo, deixa de
    criá-las; em arquivo posterior, cancela as futuras pendentes que a importação anterior criou."""
    if conta.tipo != "cartao":
        return
    for est in linhas:
        t = est.transacao
        m = re.match(r'(?i)\s*estorno de\s+"([^"]+)"', t.descricao_original or t.descricao)
        if est.status != "nova" or t.valor <= 0 or not m:
            continue
        alvo = normalizar_descricao(m[1])[:PREFIXO_PARCELA]
        compra = next((ln for ln in linhas if ln.status == "parcela" and ln.transacao.valor == -t.valor
                       and ln.descricao_norm[:PREFIXO_PARCELA] == alvo and ln.parcelas_futuras), None)
        if compra is not None:
            compra.parcelas_futuras = []
            compra.rotulo += " · estornada: sem parcelas futuras"
            continue
        for anterior in s.scalars(select(Lancamento).where(
                Lancamento.conta_id == conta.id, Lancamento.valor == -t.valor, Lancamento.status == "efetivado",
                Lancamento.grupo_parcela_id.is_not(None), Lancamento.data <= t.data)
                .order_by(Lancamento.data.desc())):
            if normalizar_descricao(anterior.descricao)[:PREFIXO_PARCELA] != alvo:
                continue
            est.cancelar_ids = list(s.scalars(select(Lancamento.id).where(
                Lancamento.grupo_parcela_id == anterior.grupo_parcela_id, Lancamento.status == "pendente")))
            if est.cancelar_ids:
                est.rotulo = f"Estorno · {len(est.cancelar_ids)} parcelas futuras canceladas"
            break


def _pagamentos(s: Session, conta: Conta, linhas: list[LinhaProposta], competencia_arquivo: str | None) -> None:
    todas = list(s.scalars(select(Conta).where(Conta.ativa, Conta.tipo == "cartao")))
    cartoes_por_id = {c.id: c for c in todas}
    eh_cartao = conta.tipo == "cartao"
    faturas_cache: dict[int, list[tuple[Fatura, int, int | None]]] = {}

    def faturas_abertas(cartao: Conta):
        if cartao.id not in faturas_cache:
            fs = s.scalars(select(Fatura).where(Fatura.cartao_id == cartao.id, Fatura.status != "paga")
                           .order_by(Fatura.competencia.desc()))
            faturas_cache[cartao.id] = [(f, total_fatura(s, f), f.total_informado) for f in fs]
        return faturas_cache[cartao.id]

    pernas_usadas: set[int] = set()
    for ln in linhas:
        if ln.status == "duplicada":
            continue
        t = ln.transacao
        valor_abs = abs(t.valor)
        sugerido = t.tipo_sugerido == "pagamento_fatura"
        if eh_cartao:
            if not (sugerido and t.valor > 0):
                continue
            candidatos_cartao = [conta]
        else:
            if t.valor >= 0:
                continue
            candidatos_cartao = _cartoes_do_padrao(todas, ln.descricao_norm)
            if not candidatos_cartao:
                candidatos_cartao = [c for c in todas if c.conta_pagamento_id == conta.id]
            if not candidatos_cartao:
                continue
        # pernas de pagamento já existentes (pendente criada antes; no cartão, a perna criada pelo extrato)
        melhor = None
        for leg in s.scalars(select(Lancamento).where(
                Lancamento.conta_id == conta.id, Lancamento.tipo == "pagamento_fatura",
                Lancamento.status != "cancelado", Lancamento.fatura_id.is_not(None))):
            if leg.id in pernas_usadas or leg.hash_dedup is not None:
                continue
            if (leg.valor > 0) != (t.valor > 0) or abs(abs(leg.valor) - valor_abs) > TOLERANCIA_FATURA:
                continue
            dias = abs((leg.data - t.data).days)
            if dias <= DIAS_CONCILIAR and (melhor is None or dias < melhor[0]):
                melhor = (dias, leg)
        if melhor:
            leg = melhor[1]
            pernas_usadas.add(leg.id)
            f = s.get(Fatura, leg.fatura_id)
            ln.status, ln.conciliar_com_id, ln.fatura_id, ln.cartao_id = "pagamento_fatura", leg.id, f.id, f.cartao_id
            ln.rotulo = f"Pagamento de fatura · {_rotulo_fatura(f, cartoes_por_id)} (já registrado)"
            continue
        # senão, descobre a fatura
        abertas = [(f, tot, inf) for c in candidatos_cartao for (f, tot, inf) in faturas_abertas(c)]
        com_valor = [(f, tot, inf) for (f, tot, inf) in abertas
                     if abs(tot - valor_abs) <= TOLERANCIA_FATURA
                     or (inf is not None and abs(inf - valor_abs) <= TOLERANCIA_FATURA)]
        if not sugerido and not eh_cartao:
            # sem padrão nem tipo sugerido: só se valor ≈ fatura e data ≈ vencimento
            com_valor = [x for x in com_valor if abs((x[0].data_vencimento - t.data).days) <= DIAS_CONCILIAR]
            if not com_valor:
                continue
        if eh_cartao and competencia_arquivo and len(com_valor) != 1 and conta.conta_pagamento_id is not None:
            # fatura com competência conhecida: o pagamento que aparece nela quita a fatura anterior
            # (criada na aplicação se ainda não existir; antes do saldo inicial não mexe em saldos)
            comp = somar_meses(competencia_arquivo, -1)
            anterior = fatura_existente(s, conta, comp)
            ln.status, ln.cartao_id = "pagamento_fatura", conta.id
            if anterior is not None:
                ln.fatura_id = anterior.id
                ln.rotulo = f"Pagamento de fatura · {_rotulo_fatura(anterior, cartoes_por_id)}"
            else:
                ln.fatura_competencia = comp
                ln.rotulo = f"Pagamento de fatura · {conta.nome} · {comp}"
            continue
        ln.status = "pagamento_fatura"
        if len(com_valor) == 1:
            f = com_valor[0][0]
            ln.fatura_id, ln.cartao_id = f.id, f.cartao_id
            ln.rotulo = f"Pagamento de fatura · {_rotulo_fatura(f, cartoes_por_id)}"
        elif eh_cartao and not abertas:
            ln.cartao_id = conta.id
            if competencia_arquivo:
                ln.fatura_competencia = somar_meses(competencia_arquivo, -1)
            elif conta.dia_fechamento and conta.dia_vencimento:
                # arquivo sem competência (OFX do Nubank): a fatura paga é a de vencimento mais próximo
                ln.fatura_competencia = min(
                    (somar_meses(competencia_de(t.data), k) for k in (-2, -1, 0)),
                    key=lambda comp: abs((datas_fatura(conta, comp)[1] - t.data).days))
            else:
                ln.status = "nova"
                continue
            ln.rotulo = f"Pagamento de fatura · {conta.nome} · {ln.fatura_competencia}"
        else:
            base = com_valor or abertas
            base = sorted(base, key=lambda x: abs((x[0].data_vencimento - t.data).days))[:6]
            for c in candidatos_cartao:
                for k in (-1, 0):
                    comp = somar_meses(competencia_de(t.data), k)
                    if fatura_existente(s, c, comp) is None:
                        ln.criar_fatura.append((c.id, comp, f"Criar fatura do cartão {c.nome} em {comp}"))
            ln.candidatos_fatura = [(f.id, _rotulo_fatura(f, cartoes_por_id)) for f, _, _ in base]
            ln.marcada = False
            if len(ln.candidatos_fatura) == 1:
                ln.fatura_id = ln.candidatos_fatura[0][0]
                ln.cartao_id = base[0][0].cartao_id
                ln.rotulo = f"Pagamento de fatura · {ln.candidatos_fatura[0][1]} (confirme)"
            else:
                ln.rotulo = ("Pagamento de fatura · escolha a fatura" if ln.candidatos_fatura
                             else "Parece pagamento de fatura, mas nenhuma fatura foi encontrada")
        if eh_cartao:
            if conta.conta_pagamento_id is None:
                ln.status, ln.marcada, ln.rotulo = "nova", True, ""
                ln.fatura_id = ln.cartao_id = ln.fatura_competencia = None


def _conciliar(s: Session, conta: Conta, linhas: list[LinhaProposta]) -> None:
    pendentes = list(s.scalars(select(Lancamento).where(
        Lancamento.conta_id == conta.id, Lancamento.status == "pendente",
        Lancamento.tipo.in_(("receita", "despesa", "transferencia")))))
    if not pendentes:
        return
    tolerancia = {r.id: r.tolerancia_pct for r in s.scalars(select(Recorrencia))}
    pares: list[tuple[tuple, int, int]] = []
    for ln in linhas:
        if ln.status not in ("nova", "parcela"):
            continue
        t = ln.transacao
        for p in pendentes:
            if (p.valor > 0) != (t.valor > 0):
                continue
            dias = abs((p.data - t.data).days)
            dif = abs(abs(p.valor) - abs(t.valor))
            casa_parcela = bool(
                t.parcela_num and t.parcela_total and p.parcela_num == t.parcela_num
                and p.parcela_total == t.parcela_total
                and _prefixo_comum(normalizar_descricao(p.descricao), ln.descricao_norm) >= PREFIXO_PARCELA)
            if casa_parcela:
                pares.append(((0, dias, dif), ln.indice, p.id))
                continue
            pct = tolerancia.get(p.recorrencia_id, 0) if p.recorrencia_id else 0
            if dias <= DIAS_CONCILIAR and dif <= abs(p.valor) * pct // 100:
                pares.append(((1, dias, dif), ln.indice, p.id))
    pares.sort()
    linhas_usadas: set[int] = set()
    pends_usadas: set[int] = set()
    por_id = {p.id: p for p in pendentes}
    for _, idx, pid in pares:
        if idx in linhas_usadas or pid in pends_usadas:
            continue
        linhas_usadas.add(idx)
        pends_usadas.add(pid)
        ln, p = linhas[idx], por_id[pid]
        ln.status, ln.conciliar_com_id = "conciliar", p.id
        ln.rotulo = f"Conciliar com {p.descricao} · {p.data:%d/%m}"
        ln.categoria_id = p.categoria_id


def _transferencias(s: Session, conta: Conta, linhas: list[LinhaProposta]) -> None:
    if conta.tipo == "cartao":
        return
    outras = {c.id: c for c in s.scalars(select(Conta).where(Conta.ativa, Conta.tipo != "cartao", Conta.id != conta.id))}
    if not outras:
        return
    candidatas = list(s.scalars(select(Lancamento).where(
        Lancamento.conta_id.in_(outras), Lancamento.tipo.in_(("receita", "despesa")),
        Lancamento.status != "cancelado", Lancamento.transferencia_par_id.is_(None),
        Lancamento.recorrencia_id.is_(None), Lancamento.ignorar.is_(False))))
    pares = []
    for ln in linhas:
        if ln.status not in ("nova", "parcela") or ln.transacao.tipo_sugerido == "pagamento_fatura":
            continue
        for c in candidatas:
            dias = abs((c.data - ln.transacao.data).days)
            if c.valor == -ln.transacao.valor and dias <= DIAS_TRANSFERENCIA:
                pares.append((dias, ln.indice, c.id))
    pares.sort()
    usadas_l, usadas_c = set(), set()
    por_id = {c.id: c for c in candidatas}
    for _, idx, cid in pares:
        if idx in usadas_l or cid in usadas_c:
            continue
        usadas_l.add(idx)
        usadas_c.add(cid)
        ln, c = linhas[idx], por_id[cid]
        ln.status, ln.transferencia_com_id = "transferencia", c.id
        ln.rotulo = f"Transferência · {outras[c.conta_id].nome}"
        ln.categoria_id = None


def _parcelas_futuras(s: Session, conta: Conta, linhas: list[LinhaProposta]) -> None:
    # o começo da descrição sozinho confunde lojas parecidas ("Jim.Com* Sophistique" e "Jim.Com* B R de Olive"):
    # a parcela só já existe se o valor também bate (com folga para o centavo de arredondamento entre parcelas)
    existentes: dict[tuple, list[int]] = defaultdict(list)
    for d, total, num, valor in s.execute(
            select(Lancamento.descricao, Lancamento.parcela_total, Lancamento.parcela_num, Lancamento.valor)
            .where(Lancamento.conta_id == conta.id, Lancamento.parcela_num.is_not(None),
                   Lancamento.status != "cancelado")):
        existentes[(normalizar_descricao(d)[:PREFIXO_PARCELA], total, num)].append(valor)

    def ja_existe(chave: tuple, valor: int) -> bool:
        return any(abs(v - valor) <= max(TOLERANCIA_PARCELA, abs(valor) // 100) for v in existentes.get(chave, ()))
    for ln in linhas:
        t = ln.transacao
        if ln.status != "nova" or not (t.parcela_num and t.parcela_total):
            continue
        ln.status, ln.rotulo = "parcela", f"Parcela {t.parcela_num}/{t.parcela_total}"
        chave = ln.descricao_norm[:PREFIXO_PARCELA]
        ln.parcelas_futuras = [n for n in range(t.parcela_num + 1, t.parcela_total + 1)
                               if not ja_existe((chave, t.parcela_total, n), t.valor)]


def _avisos_total_parcial(s: Session, conta: Conta, linhas: list[LinhaProposta], hoje: date) -> list[str]:
    """Fatura já fechada que tinha total digitado à mão: compara com o que o arquivo traz (o a detalhar sai)."""
    no_arquivo: dict[str, int] = defaultdict(int)
    for ln in linhas:
        if ln.fatura_competencia and ln.transacao.tipo_sugerido not in ("pagamento_fatura", "saldo_anterior"):
            no_arquivo[ln.fatura_competencia] -= ln.transacao.valor
    avisos = []
    for comp, total in sorted(no_arquivo.items()):
        f = fatura_existente(s, conta, comp)
        if f is not None and f.total_parcial is not None and hoje > f.data_fechamento:
            avisos.append(f"Fatura que vence em {f.data_vencimento:%d/%m/%Y}: o total digitado era "
                          f"{formatar_reais(f.total_parcial)} e o arquivo traz {formatar_reais(total)}. "
                          "O valor a detalhar sai e ficam as compras do arquivo.")
    return avisos


def propor_importacao(s: Session, resultado: ResultadoLeitura, conta: Conta, hoje: date, *,
                      nome_arquivo: str = "", hash_arquivo: str | None = None,
                      cliente_ia=None, modelo_ia: str = MODELO_PADRAO) -> Proposta:
    """Monta a proposta de importação sem gravar nada."""
    if (resultado.tipo_conta == "cartao") != (conta.tipo == "cartao"):
        raise ValueError(
            f"O arquivo é de {'cartão' if resultado.tipo_conta == 'cartao' else 'conta corrente'}, "
            f"mas {conta.nome} é {'cartão' if conta.tipo == 'cartao' else 'conta'}.")
    eh_cartao = conta.tipo == "cartao"
    vistos: Counter = Counter()
    linhas: list[LinhaProposta] = []
    for i, t in enumerate(resultado.transacoes):
        norm = normalizar_descricao(t.descricao)
        chave = (t.data, t.valor, norm, t.id_externo or "")
        vistos[chave] += 1
        h = calcular_hash(conta.id, t.data, t.valor, norm, t.id_externo, vistos[chave])
        linhas.append(LinhaProposta(indice=i, transacao=t, descricao_norm=norm, hash_dedup=h,
                                    tipo="despesa" if (eh_cartao or t.valor < 0) else "receita",
                                    rotulo="Nova"))
    competencia = resultado.competencia_fatura
    _duplicadas(s, conta, linhas)
    _duplicadas_outra_fonte(s, conta, linhas)
    _data_alterada(s, conta, linhas)
    _pagamentos(s, conta, linhas, competencia)
    _conciliar(s, conta, linhas)
    _transferencias(s, conta, linhas)
    _parcelas_futuras(s, conta, linhas)
    _estornos(s, conta, linhas)
    if eh_cartao:
        for ln in linhas:
            if ln.status in ("nova", "parcela", "duplicada", "conciliar") and conta.dia_fechamento:
                ln.fatura_competencia = competencia or competencia_na_data(s, conta, ln.transacao.data)
            if ln.transacao.tipo_sugerido == "saldo_anterior" and ln.status == "nova":
                ln.ignorar = True
                ln.rotulo = "Saldo da fatura anterior · não é compra (entra ignorado)"

    proposta = Proposta(resultado=resultado, conta=conta, linhas=linhas, nome_arquivo=nome_arquivo,
                        hash_arquivo=hash_arquivo, avisos=list(resultado.avisos))
    if eh_cartao:
        proposta.avisos += _avisos_total_parcial(s, conta, linhas, hoje)
    # categorias (regra, histórico, dica do banco e, se pedido, IA)
    for tipo in ("despesa", "receita"):
        alvo = [ln for ln in linhas if ln.tipo == tipo and ln.status in ("nova", "parcela", "conciliar")
                and ln.categoria_id is None and not ln.ignorar]
        if not alvo:
            continue
        res = sugerir_categorias(s, [(ln.transacao.descricao, ln.transacao.dica_categoria) for ln in alvo],
                                 tipo=tipo, cliente=cliente_ia, modelo=modelo_ia)
        for ln, sg in zip(alvo, res.sugestoes):
            ln.categoria_id, ln.categoria_origem = sg.categoria_id, sg.origem
        proposta.aviso_ia = proposta.aviso_ia or res.aviso_ia
    if proposta.aviso_ia:
        proposta.avisos.append(proposta.aviso_ia)

    if hash_arquivo:
        anterior = s.scalar(select(Importacao).where(Importacao.hash_arquivo == hash_arquivo,
                                                     Importacao.desfeita.is_(False))
                            .order_by(Importacao.criada_em.desc()))
        if anterior:
            proposta.arquivo_repetido_em = anterior.criada_em.date()
            proposta.avisos.append(f"Este arquivo já foi importado em {anterior.criada_em:%d/%m/%Y}. "
                                   "As transações repetidas aparecem desmarcadas.")
    # validação da soma
    if eh_cartao:
        # compras e estornos do período; pagamento e saldo da fatura anterior se anulam no total do banco
        proposta.total_importado = -sum(ln.transacao.valor for ln in linhas
                                        if ln.transacao.tipo_sugerido not in ("pagamento_fatura", "saldo_anterior"))
    else:
        proposta.total_importado = sum(ln.transacao.valor for ln in linhas)
    proposta.total_informado = resultado.total_informado
    if eh_cartao and resultado.total_informado is not None:
        proposta.divergente = abs(proposta.total_importado - resultado.total_informado) > 1
        if proposta.divergente:
            proposta.avisos.append("A soma dos lançamentos não bate com o total informado na fatura.")
    if eh_cartao and resultado.fechamento and conta.dia_fechamento and resultado.fechamento.day != conta.dia_fechamento:
        proposta.sugestao_fechamento = resultado.fechamento.day
    return proposta


# ---------------------------------------------------------------- aplicar

_CAMPOS_SNAPSHOT = ("status", "data", "valor", "tipo", "categoria_id", "hash_dedup", "id_externo",
                    "importacao_id", "descricao_original", "origem", "transferencia_par_id", "fatura_id")


def _foto(lanc: Lancamento) -> dict:
    campos = {c: getattr(lanc, c) for c in _CAMPOS_SNAPSHOT}
    campos["data"] = lanc.data.isoformat()
    return {"id": lanc.id, "campos": campos}


def _origem(resultado: ResultadoLeitura) -> str:
    return "pdf" if resultado.origem == "pdf_ia" else resultado.origem


def _validar_pagamentos(proposta: Proposta, escolhas: Escolhas) -> None:
    """Pagamento de fatura marcado precisa de fatura (ou criar uma) ou da confirmação de que é despesa comum."""
    erros: dict[int, str] = {}
    for ln in proposta.linhas:
        marcada = ln.indice in escolhas.marcadas if escolhas.marcadas is not None else ln.marcada
        if not marcada or ln.status != "pagamento_fatura" or ln.conciliar_com_id is not None:
            continue
        if ln.indice in escolhas.nao_e_pagamento or ln.indice in escolhas.criar_faturas:
            continue
        if escolhas.faturas.get(ln.indice, ln.fatura_id) is not None or (ln.cartao_id and ln.fatura_competencia):
            continue
        erros[ln.indice] = (f"Escolha a fatura paga por «{ln.transacao.descricao}» ou indique que não é pagamento "
                            "de fatura (lançar como despesa).")
    if erros:
        raise PagamentoSemFatura(erros)


def aplicar_importacao(s: Session, proposta: Proposta, escolhas: Escolhas | None = None,
                       hoje: date | None = None) -> Importacao:
    """Grava a importação numa transação única (savepoint): lançamentos novos, conciliações e snapshot."""
    escolhas = escolhas or Escolhas()
    resultado, conta = proposta.resultado, proposta.conta
    origem = _origem(resultado)
    _validar_pagamentos(proposta, escolhas)
    criada_em = datetime.combine(hoje, datetime.now().time()) if hoje else datetime.now()
    with s.begin_nested():
        imp = Importacao(arquivo_nome=proposta.nome_arquivo or "arquivo", hash_arquivo=proposta.hash_arquivo or "",
                         tipo=resultado.origem, conta_id=conta.id,
                         criada_em=criada_em)
        s.add(imp)
        s.flush()
        fotos: dict[int, dict] = {}
        faturas_criadas: list[int] = []
        faturas_tocadas: set[int] = set()
        faturas_antes: dict[int, dict] = {}
        novas = conciliadas = duplicadas = 0

        def guardar(lanc: Lancamento) -> None:
            fotos.setdefault(lanc.id, _foto(lanc))

        def fatura_de(cartao: Conta, competencia: str) -> Fatura:
            f = fatura_existente(s, cartao, competencia)
            if f is None:
                f = obter_ou_criar_fatura(s, cartao, competencia)
                faturas_criadas.append(f.id)
            faturas_tocadas.add(f.id)
            return f

        def carimbar(lanc: Lancamento, ln: LinhaProposta, com_hash: bool = True) -> None:
            t = ln.transacao
            lanc.importacao_id = imp.id
            if com_hash:
                lanc.hash_dedup, lanc.id_externo = ln.hash_dedup, t.id_externo
            lanc.descricao_original = t.descricao_original[:300]
            lanc.origem = origem

        futuras: list[tuple[Lancamento, LinhaProposta]] = []
        for ln in proposta.linhas:
            marcada = ln.indice in escolhas.marcadas if escolhas.marcadas is not None else ln.marcada
            t = ln.transacao
            if ln.status == "duplicada" and not marcada:
                duplicadas += 1
                continue
            if not marcada:
                continue
            categoria = escolhas.categorias[ln.indice] if ln.indice in escolhas.categorias else ln.categoria_id
            status = ln.status
            fatura_id = escolhas.faturas.get(ln.indice, ln.fatura_id)
            criar = escolhas.criar_faturas.get(ln.indice)
            if status == "pagamento_fatura" and ln.conciliar_com_id is None and ln.indice in escolhas.nao_e_pagamento:
                status = "nova"  # confirmação explícita: entra como despesa comum
            if status == "pagamento_fatura":
                if ln.conciliar_com_id is not None:
                    perna = s.get(Lancamento, ln.conciliar_com_id)
                    par = s.get(Lancamento, perna.transferencia_par_id) if perna.transferencia_par_id else None
                    for item in (perna, par):
                        if item is not None:
                            guardar(item)
                            item.status = "efetivado"
                    perna.data, perna.valor = t.data, t.valor
                    if par is not None:
                        par.data, par.valor = t.data, -t.valor
                    carimbar(perna, ln)
                    faturas_tocadas.add(perna.fatura_id)
                    conciliadas += 1
                else:
                    cartao = s.get(Conta, ln.cartao_id) if ln.cartao_id else None
                    if criar is not None:
                        fatura = fatura_de(s.get(Conta, criar[0]), criar[1])
                    elif fatura_id is not None:
                        fatura = s.get(Fatura, fatura_id)
                        cartao = s.get(Conta, fatura.cartao_id)
                        faturas_tocadas.add(fatura.id)
                    else:
                        fatura = fatura_de(cartao, ln.fatura_competencia)
                    if conta.tipo == "cartao":
                        saida, entrada = pagar_fatura(s, fatura, abs(t.valor), t.data, efetivado=True)
                        da_conta, outra = entrada, saida
                    else:
                        saida, entrada = pagar_fatura(s, fatura, abs(t.valor), t.data, conta_origem=conta,
                                                      efetivado=True)
                        da_conta, outra = saida, entrada
                    carimbar(da_conta, ln)
                    carimbar(outra, ln, com_hash=False)
                    novas += 1
                continue
            if status == "conciliar":
                p = s.get(Lancamento, ln.conciliar_com_id)
                par = s.get(Lancamento, p.transferencia_par_id) if p.transferencia_par_id else None
                for item in (p, par):
                    if item is not None:
                        guardar(item)
                        item.status = "efetivado"
                p.data, p.valor = t.data, t.valor
                if par is not None:
                    par.data, par.valor = t.data, -t.valor
                if p.categoria_id is None and categoria is not None and p.tipo in ("receita", "despesa"):
                    p.categoria_id = categoria
                carimbar(p, ln)
                if p.fatura_id:
                    faturas_tocadas.add(p.fatura_id)
                if conta.tipo == "cartao" and p.fatura_id and ln.fatura_competencia:
                    # a data nova pode cair em outra fatura
                    p.fatura_id = fatura_de(conta, ln.fatura_competencia).id
                conciliadas += 1
                continue
            if status == "transferencia":
                outra = s.get(Lancamento, ln.transferencia_com_id)
                guardar(outra)
                lanc = Lancamento(conta_id=conta.id, data=t.data, descricao=t.descricao, valor=t.valor,
                                  tipo="transferencia", status="efetivado")
                carimbar(lanc, ln)
                s.add(lanc)
                s.flush()
                lanc.transferencia_par_id, outra.transferencia_par_id = outra.id, lanc.id
                outra.tipo, outra.categoria_id, outra.status = "transferencia", None, "efetivado"
                novas += 1
                continue
            # nova / parcela (e duplicada marcada de propósito)
            for fid in ln.cancelar_ids:  # estorno: parcelas futuras da compra estornada
                futura = s.get(Lancamento, fid)
                if futura is not None and futura.status == "pendente":
                    guardar(futura)
                    futura.status = "cancelado"
                    if futura.fatura_id:
                        faturas_tocadas.add(futura.fatura_id)
            lanc = Lancamento(conta_id=conta.id, data=t.data, descricao=t.descricao, valor=t.valor,
                              tipo=ln.tipo, status="efetivado", categoria_id=categoria,
                              parcela_num=t.parcela_num, parcela_total=t.parcela_total, ignorar=ln.ignorar)
            carimbar(lanc, ln)
            if conta.tipo == "cartao":
                lanc.tipo = "despesa"
                comp = ln.fatura_competencia or competencia_na_data(s, conta, t.data)
                lanc.fatura_id = fatura_de(conta, comp).id
            if t.parcela_num and t.parcela_total:
                lanc.grupo_parcela_id = uuid4().hex
            s.add(lanc)
            s.flush()
            if ln.parcelas_futuras and escolhas.criar_parcelas_futuras:
                futuras.append((lanc, ln))
            novas += 1

        for base, ln in futuras:
            for n in ln.parcelas_futuras:
                k = n - base.parcela_num
                futura = Lancamento(
                    conta_id=conta.id, data=somar_meses_data(base.data, k), descricao=base.descricao,
                    valor=base.valor, tipo=base.tipo, status="pendente", categoria_id=base.categoria_id,
                    grupo_parcela_id=base.grupo_parcela_id, parcela_num=n, parcela_total=base.parcela_total,
                    importacao_id=imp.id, origem=origem)
                if conta.tipo == "cartao":
                    futura.fatura_id = fatura_de(conta, somar_meses(ln.fatura_competencia, k)).id
                s.add(futura)
        s.flush()

        # fatura do PDF: total informado e datas próprias (o dia de fechamento do cartão não é alterado)
        if conta.tipo == "cartao" and resultado.competencia_fatura:
            fatura = fatura_de(conta, resultado.competencia_fatura)
            faturas_antes[fatura.id] = {"total_informado": fatura.total_informado,
                                        "data_fechamento": fatura.data_fechamento.isoformat(),
                                        "data_vencimento": fatura.data_vencimento.isoformat()}
            if resultado.total_informado is not None:
                fatura.total_informado = resultado.total_informado
            if resultado.fechamento:
                fatura.data_fechamento = resultado.fechamento
            if resultado.vencimento:
                fatura.data_vencimento = resultado.vencimento
        # fatura já fechada que tinha o total digitado à mão: as compras do arquivo tomam o lugar do a detalhar
        parciais = []
        for fid in faturas_tocadas:
            f = s.get(Fatura, fid) if fid is not None else None
            if f is not None and f.total_parcial is not None and (hoje or date.today()) > f.data_fechamento:
                parciais.append({"id": f.id, "total_parcial": f.total_parcial,
                                 "total_parcial_em": f.total_parcial_em.isoformat() if f.total_parcial_em else None})
                f.total_parcial = f.total_parcial_em = None
        for fid in faturas_tocadas:
            if fid is not None:
                atualizar_status(s, s.get(Fatura, fid))
        if resultado.identificador_conta and not conta.identificador_externo:
            conta.identificador_externo = resultado.identificador_conta
        imp.qtd_novas, imp.qtd_conciliadas, imp.qtd_duplicadas = novas, conciliadas, duplicadas
        imp.snapshot_conciliacoes = {
            "lancamentos": list(fotos.values()), "faturas_criadas": faturas_criadas, "faturas": [
                {"id": k, **v} for k, v in faturas_antes.items()],
            "faturas_tocadas": sorted(f for f in faturas_tocadas if f is not None),
            "parciais": parciais,
        }
        s.flush()
    return imp


# ---------------------------------------------------------------- desfazer

def desfazer_importacao(s: Session, imp: Importacao) -> None:
    """Apaga o que a importação criou e restaura os lançamentos conciliados a partir do snapshot."""
    if imp.desfeita:
        raise ValueError("Esta importação já foi desfeita.")
    snap = imp.snapshot_conciliacoes or {}
    with s.begin_nested():
        restaurados = {item["id"] for item in snap.get("lancamentos", [])}
        criados = [l for l in s.scalars(select(Lancamento).where(Lancamento.importacao_id == imp.id))
                   if l.id not in restaurados]
        ids_criados = {l.id for l in criados}
        for l in criados:
            l.transferencia_par_id = None
        if ids_criados:
            # pares feitos por outra importação (ex.: transferência entre extratos de bancos diferentes)
            for outro in s.scalars(select(Lancamento).where(Lancamento.transferencia_par_id.in_(ids_criados))):
                outro.transferencia_par_id = None
        for item in snap.get("lancamentos", []):
            lanc = s.get(Lancamento, item["id"])
            if lanc is not None:
                lanc.transferencia_par_id = None
        s.flush()
        for item in snap.get("lancamentos", []):
            lanc = s.get(Lancamento, item["id"])
            if lanc is None:
                continue
            campos = dict(item["campos"])
            campos["data"] = date.fromisoformat(campos["data"])
            par_id = campos.pop("transferencia_par_id")
            for k, v in campos.items():
                setattr(lanc, k, v)
            lanc.transferencia_par_id = None if par_id in ids_criados else par_id
        for l in criados:
            s.delete(l)
        s.flush()
        for f in snap.get("faturas", []):
            fatura = s.get(Fatura, f["id"])
            if fatura is not None:
                fatura.total_informado = f["total_informado"]
                fatura.data_fechamento = date.fromisoformat(f["data_fechamento"])
                fatura.data_vencimento = date.fromisoformat(f["data_vencimento"])
        for f in snap.get("parciais", []):
            fatura = s.get(Fatura, f["id"])
            if fatura is not None:
                fatura.total_parcial = f["total_parcial"]
                fatura.total_parcial_em = date.fromisoformat(f["total_parcial_em"]) if f["total_parcial_em"] else None
        s.flush()
        for fid in snap.get("faturas_criadas", []):
            fatura = s.get(Fatura, fid)
            if fatura is not None and s.scalar(select(Lancamento.id).where(Lancamento.fatura_id == fid).limit(1)) is None:
                s.delete(fatura)
        s.flush()
        for fid in snap.get("faturas_tocadas", []):
            fatura = s.get(Fatura, fid)
            if fatura is not None:
                atualizar_status(s, fatura)
        imp.desfeita = True
        s.flush()
