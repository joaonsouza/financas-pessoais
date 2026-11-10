from datetime import date

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Recorrencia(Base):
    __tablename__ = "recorrencia"

    id: Mapped[int] = mapped_column(primary_key=True)
    descricao: Mapped[str] = mapped_column(String(200))
    tipo: Mapped[str] = mapped_column(String(20))  # receita | despesa | transferencia
    conta_id: Mapped[int] = mapped_column(ForeignKey("conta.id"))
    conta_destino_id: Mapped[int | None] = mapped_column(ForeignKey("conta.id"))
    categoria_id: Mapped[int | None] = mapped_column(ForeignKey("categoria.id"))
    valor: Mapped[int]  # centavos, sempre positivo (o sinal vem do tipo)
    valor_variavel: Mapped[bool] = mapped_column(default=False)
    tolerancia_pct: Mapped[int] = mapped_column(default=0)
    dia: Mapped[int]
    inicio: Mapped[str] = mapped_column(String(7))  # competência AAAA-MM
    fim: Mapped[str | None] = mapped_column(String(7))
    intervalo_meses: Mapped[int] = mapped_column(default=1)  # a cada N meses (anual = 12, 24...)
    frequencia: Mapped[str] = mapped_column(String(8), default="mensal")  # mensal | semanal
    intervalo_semanas: Mapped[int] = mapped_column(default=1)  # frequência semanal: a cada N semanas
    dias_semana: Mapped[str | None] = mapped_column(String(13))  # "0,2,4" (segunda = 0); só semanal
    data_inicio: Mapped[date | None]  # primeira ocorrência exata (a semanal precisa do dia)
    fim_data: Mapped[date | None]  # nenhuma ocorrência depois desta data
    max_ocorrencias: Mapped[int | None]  # termina após N ocorrências, contadas desde a primeira
    eh_assinatura: Mapped[bool] = mapped_column(default=False)
    ativa: Mapped[bool] = mapped_column(default=True)
    origem_meta_id: Mapped[int | None]  # Plano 3
    origem_provisao_id: Mapped[int | None]  # legado: só a migração das provisões para metas lê
    meta_id: Mapped[int | None]  # objetivo escolhido no formulário: todas as ocorrências entram nele
    caixinha_id: Mapped[int | None]  # a perna que cai na conta desta caixinha entra nela

    @property
    def aporte_como_despesa(self) -> bool:
        """Aporte mensal de meta que, pendente, é despesa (conta no orçamento) e, ao efetivar, vira transferência."""
        return self.origem_meta_id is not None and self.tipo == "despesa" and self.conta_destino_id is not None
