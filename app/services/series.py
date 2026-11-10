"""Editar e excluir um lançamento de série (recorrente ou parcelado): só este, este e os próximos, ou todos."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.periodo import competencia_de, dia_no_mes, somar_meses
from app.models import Caixinha, Fatura, Lancamento, Recorrencia
from app.services.faturas import atualizar_status, em_fatura_paga
from app.services.lancamentos import atualizar_lancamento, excluir_lancamento
from app.services.recorrencia import encerrar_antes_de

ESCOPOS = ("este", "proximos", "todos")


def tipo_serie(s: Session, lanc: Lancamento) -> str | None:
    """"recorrencia", "parcela" ou None. Recorrência de meta fica de fora: a tela dela cuida."""
    if lanc.recorrencia_id is not None:
        rec = s.get(Recorrencia, lanc.recorrencia_id)
        if rec is None or rec.origem_meta_id is not None:
            return None
        return "recorrencia"
    if lanc.grupo_parcela_id is not None:
        return "parcela"
    return None


def retrato(lanc: Lancamento) -> dict:
    """Os campos que uma edição pode levar para o resto da série, para comparar antes e depois."""
    return {
        "descricao": lanc.descricao, "valor": abs(lanc.valor), "categoria_id": lanc.categoria_id,
        "tags": sorted(t.nome for t in lanc.tags), "observacao": lanc.observacao, "dia": lanc.data.day,
        "conta_id": lanc.conta_id,
    }


def _outros_da_serie(s: Session, lanc: Lancamento, escopo: str, conta_id: int) -> list[Lancamento]:
    """Os demais lançamentos da série na conta `conta_id` (a de antes da edição; a outra perna de
    transferência vem junto na edição)."""
    if lanc.recorrencia_id is not None:
        consulta = select(Lancamento).where(Lancamento.recorrencia_id == lanc.recorrencia_id)
        if escopo == "proximos":
            consulta = consulta.where(Lancamento.competencia_recorrencia > lanc.competencia_recorrencia)
    else:
        consulta = select(Lancamento).where(Lancamento.grupo_parcela_id == lanc.grupo_parcela_id)
        if escopo == "proximos":
            consulta = consulta.where(Lancamento.parcela_num > lanc.parcela_num)
    consulta = consulta.where(Lancamento.id != lanc.id, Lancamento.conta_id == conta_id,
                              Lancamento.status != "cancelado")
    return list(s.scalars(consulta))


def propagar_edicao(s: Session, lanc: Lancamento, antes: dict, escopo: str) -> int:
    """Leva para o resto da série só os campos que mudaram em `lanc` (já salvo). Situação, fatura e
    "ignorar" ficam de cada um. Na recorrência, o modelo também muda. Devolve quantos outros mudaram."""
    if escopo not in ("proximos", "todos"):
        return 0
    tipo = tipo_serie(s, lanc)
    if tipo is None:
        return 0
    depois = retrato(lanc)
    mudou = {campo for campo in antes if antes[campo] != depois[campo]}
    if tipo == "recorrencia" and s.get(Recorrencia, lanc.recorrencia_id).frequencia == "semanal":
        mudou.discard("dia")  # dia do mês não existe na regra semanal
    if not mudou:
        return 0
    outros = _outros_da_serie(s, lanc, escopo, antes["conta_id"])
    for outro in outros:
        fatura = s.get(Fatura, outro.fatura_id) if outro.fatura_id else None
        atualizar_lancamento(
            s, outro,
            data=dia_no_mes(competencia_de(outro.data), depois["dia"]) if "dia" in mudou else outro.data,
            descricao=depois["descricao"] if "descricao" in mudou else outro.descricao,
            valor_abs=depois["valor"] if "valor" in mudou else abs(outro.valor),
            categoria_id=depois["categoria_id"] if "categoria_id" in mudou else outro.categoria_id,
            tags=depois["tags"] if "tags" in mudou else [t.nome for t in outro.tags],
            observacao=depois["observacao"] if "observacao" in mudou else outro.observacao,
            # a compra continua na fatura dela (mudar o dia de uma parcela não troca a fatura)
            fatura_competencia=fatura.competencia if fatura else None,
            conta_id=depois["conta_id"] if "conta_id" in mudou else None,
        )
    if tipo == "recorrencia":
        rec = s.get(Recorrencia, lanc.recorrencia_id)
        for campo, atributo in (("descricao", "descricao"), ("valor", "valor"), ("categoria_id", "categoria_id"),
                                ("dia", "dia"), ("conta_id", "conta_id")):
            if campo in mudou:
                setattr(rec, atributo, depois[campo])
        caixinha = s.get(Caixinha, rec.caixinha_id) if rec.caixinha_id else None
        if caixinha is not None and caixinha.conta_id != rec.conta_id:
            rec.caixinha_id = None  # a caixinha era da conta antiga
    s.flush()
    return len(outros)


def _travar_pagas(s: Session, lancamentos: list[Lancamento]) -> None:
    if any(em_fatura_paga(s, l) for l in lancamentos):
        raise ValueError("a fatura já foi paga: compra de fatura paga não pode ser excluída")


def _apagar(s: Session, lancamentos: list[Lancamento]) -> None:
    """Apaga os lançamentos e a outra perna das transferências; refaz a situação das faturas mexidas."""
    _travar_pagas(s, lancamentos)
    por_id = {l.id: l for l in lancamentos}
    for l in lancamentos:
        if l.transferencia_par_id and l.transferencia_par_id not in por_id:
            par = s.get(Lancamento, l.transferencia_par_id)
            if par is not None:
                por_id[par.id] = par
    faturas = {l.fatura_id for l in por_id.values() if l.fatura_id}
    for l in por_id.values():
        l.transferencia_par_id = None
    s.flush()
    for l in por_id.values():
        s.delete(l)
    s.flush()
    for fatura_id in faturas:
        if (fatura := s.get(Fatura, fatura_id)) is not None:
            atualizar_status(s, fatura)


def excluir_serie(s: Session, lanc: Lancamento, escopo: str) -> None:
    """"este" é a exclusão de sempre. "proximos" apaga este e os seguintes (na recorrência, ela termina no
    mês anterior); "todos" apaga a série inteira, inclusive os já pagos, e a própria recorrência."""
    tipo = tipo_serie(s, lanc)
    if escopo not in ("proximos", "todos") or tipo is None:
        _travar_pagas(s, [lanc])
        excluir_lancamento(s, lanc)
        return
    if tipo == "recorrencia":
        rec = s.get(Recorrencia, lanc.recorrencia_id)
        consulta = select(Lancamento).where(Lancamento.recorrencia_id == rec.id)  # inclusive os cancelados
        if escopo == "proximos":
            consulta = consulta.where(Lancamento.competencia_recorrencia >= lanc.competencia_recorrencia)
            encerrar_antes_de(rec, lanc.competencia_recorrencia)
        _apagar(s, list(s.scalars(consulta)))
        if escopo == "todos":
            s.delete(rec)
            s.flush()
        return
    grupo, numero = lanc.grupo_parcela_id, lanc.parcela_num
    consulta = select(Lancamento).where(Lancamento.grupo_parcela_id == grupo)
    if escopo == "proximos":
        consulta = consulta.where(Lancamento.parcela_num >= numero)
    _apagar(s, list(s.scalars(consulta)))
    if escopo == "proximos":
        for restante in s.scalars(select(Lancamento).where(Lancamento.grupo_parcela_id == grupo)):
            restante.parcela_total = numero - 1
        s.flush()
