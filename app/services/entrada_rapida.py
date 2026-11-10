from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.core.periodo import competencia_de, validar_competencia, validar_intervalo
from app.models import Conta, Lancamento, Recorrencia
from app.services.lancamentos import (
    criar_compra_parcelada, criar_lancamento, criar_lancamento_repetido, criar_transferencia,
)
from app.services.metas import registrar_saque_reserva
from app.services.recorrencia import gerar_pendentes

TIPOS_ENTRADA = ("receita", "despesa", "transferencia", "cartao")
REPETICOES = ("nao", "fixa", "parcelada", "repetida")


@dataclass
class NovaEntrada:
    tipo: str
    valor: int
    descricao: str
    data: date
    conta_id: int | None
    conta_destino_id: int | None = None
    categoria_id: int | None = None
    tags: list[str] = field(default_factory=list)
    efetivado: bool = True
    repetir: str = "nao"
    parcelas: int = 1
    observacao: str | None = None
    ignorar: bool = False
    fatura_competencia: str | None = None
    meta_id: int | None = None  # objetivo: a fixa leva o vínculo para todas as ocorrências
    caixinha_id: int | None = None
    intervalo: int = 1  # a cada N (meses na repetida; na fixa, da unidade escolhida)
    unidade: str = "mes"  # fixa: mes | semana | ano
    dias_semana: list[int] = field(default_factory=list)  # fixa semanal (segunda = 0); o dia da data sempre entra
    fim: str | None = None  # competência AAAA-MM em que a fixa termina
    fim_data: date | None = None  # a fixa não passa desta data
    fim_vezes: int | None = None  # a fixa termina após N ocorrências
    checklist_reserva: list[str] | None = None  # respostas "sim" do checklist de saque da reserva


def registrar_entrada(s: Session, e: NovaEntrada, hoje: date) -> list[Lancamento]:
    if e.tipo not in TIPOS_ENTRADA:
        raise ValueError("tipo de lançamento inválido")
    if e.repetir not in REPETICOES:
        raise ValueError("opção de repetição inválida")
    validar_intervalo(e.intervalo)
    if e.unidade not in ("mes", "semana", "ano"):
        raise ValueError("unidade de repetição inválida")
    if any(d not in range(7) for d in e.dias_semana):
        raise ValueError("dia da semana inválido")
    if e.fim:
        validar_competencia(e.fim)
        if e.fim < competencia_de(e.data):
            raise ValueError("o fim não pode ser antes do início")
    if e.fim_data and e.fim_data < e.data:
        raise ValueError("o fim não pode ser antes do início")
    if e.fim_vezes is not None and e.fim_vezes < 1:
        raise ValueError("o número de vezes deve ser pelo menos 1")
    conta = s.get(Conta, e.conta_id) if e.conta_id else None
    if conta is None:
        raise ValueError("escolha a conta")
    if e.tipo == "cartao" and conta.tipo != "cartao":
        raise ValueError("escolha um cartão de crédito")
    if e.tipo != "cartao" and conta.tipo == "cartao":
        raise ValueError("para cartão use 'Despesa no cartão'")
    tipo_base = "despesa" if e.tipo == "cartao" else e.tipo
    descricao = e.descricao.strip()
    if tipo_base != "transferencia" and not descricao:
        raise ValueError("informe a descrição")

    if e.repetir == "parcelada":
        if tipo_base != "despesa":
            raise ValueError("só despesas podem ser parceladas")
        return criar_compra_parcelada(
            s, conta=conta, data=e.data, descricao=descricao, valor_total=e.valor, parcelas=e.parcelas,
            categoria_id=e.categoria_id, efetivado_primeira=e.efetivado, tags=e.tags, ignorar=e.ignorar,
            fatura_competencia=e.fatura_competencia, observacao=e.observacao,
        )

    if e.repetir == "repetida":
        if tipo_base == "transferencia":
            raise ValueError("transferência não pode ser repetida; use transferência fixa")
        return criar_lancamento_repetido(
            s, conta=conta, data=e.data, descricao=descricao, valor_abs=e.valor, vezes=e.parcelas, tipo=tipo_base,
            categoria_id=e.categoria_id, efetivado_primeira=e.efetivado, tags=e.tags, ignorar=e.ignorar,
            fatura_competencia=e.fatura_competencia, observacao=e.observacao, intervalo=e.intervalo,
        )

    if tipo_base == "transferencia":
        destino = s.get(Conta, e.conta_destino_id) if e.conta_destino_id else None
        if destino is None:
            raise ValueError("escolha a conta de destino")
        lancamentos = list(criar_transferencia(
            s, origem=conta, destino=destino, data=e.data, valor_abs=e.valor,
            descricao=descricao or "Transferência", efetivado=e.efetivado, ignorar=e.ignorar,
        ))
        for lanc in lancamentos:
            lanc.observacao = e.observacao
    else:
        lancamentos = [criar_lancamento(
            s, conta=conta, data=e.data, descricao=descricao, valor_abs=e.valor, tipo=tipo_base,
            categoria_id=e.categoria_id, efetivado=e.efetivado, tags=e.tags, observacao=e.observacao,
            ignorar=e.ignorar, fatura_competencia=e.fatura_competencia,
        )]

    if e.meta_id:
        for lanc in lancamentos:
            lanc.meta_id = e.meta_id
    if e.repetir == "fixa":
        rec = Recorrencia(
            descricao=lancamentos[0].descricao, tipo=tipo_base, conta_id=conta.id,
            conta_destino_id=e.conta_destino_id if tipo_base == "transferencia" else None,
            categoria_id=e.categoria_id, valor=e.valor, dia=e.data.day, inicio=competencia_de(e.data),
            intervalo_meses=e.intervalo * (12 if e.unidade == "ano" else 1), fim=e.fim or None,
            meta_id=e.meta_id, caixinha_id=e.caixinha_id, data_inicio=e.data, fim_data=e.fim_data,
            max_ocorrencias=e.fim_vezes,
        )
        chave = rec.inicio
        if e.unidade == "semana":
            rec.frequencia, rec.intervalo_meses, rec.intervalo_semanas = "semanal", 1, e.intervalo
            rec.dias_semana = ",".join(str(d) for d in sorted({*e.dias_semana, e.data.weekday()}))
            chave = e.data.isoformat()
        s.add(rec)
        s.flush()
        for lanc in lancamentos:
            lanc.recorrencia_id = rec.id
            lanc.competencia_recorrencia = chave
            lanc.origem = "recorrencia"
        s.flush()
        gerar_pendentes(s, hoje)
    if e.checklist_reserva:
        registrar_saque_reserva(s, lancamentos[0], set(e.checklist_reserva), hoje)
    return lancamentos


def sugestao_por_descricao(s: Session, descricao: str) -> dict:
    lanc = s.scalar(
        select(Lancamento)
        .where(
            func.lower(Lancamento.descricao) == descricao.strip().lower(),
            Lancamento.tipo.in_(("receita", "despesa")),
        )
        .order_by(Lancamento.data.desc(), Lancamento.id.desc())
        .limit(1)
    )
    return {"categoria_id": lanc.categoria_id, "conta_id": lanc.conta_id} if lanc else {}


def descricoes_recentes(s: Session, limite: int = 200) -> list[str]:
    linhas = s.execute(
        select(Lancamento.descricao, func.max(Lancamento.data).label("ultima"))
        .where(Lancamento.tipo.in_(("receita", "despesa")))
        .group_by(Lancamento.descricao)
        .order_by(desc("ultima"))
        .limit(limite)
    ).all()
    return [linha[0] for linha in linhas]
