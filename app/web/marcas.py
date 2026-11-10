"""Identidade visual de bancos e corretoras: sigla e cores do avatar redondo (sem logos de terceiros)."""
from app.importers.normalizacao import normalizar_descricao

# trecho do nome (normalizado) -> (sigla, fundo, texto). A ordem importa: o mais específico vem antes.
MARCAS: tuple[tuple[str, str, str, str], ...] = (
    ("NUINVEST", "nu", "#4A0F7C", "#FFFFFF"),
    ("NUCRIPTO", "nu", "#4A0F7C", "#FFFFFF"),
    ("NUBANK", "nu", "#820AD1", "#FFFFFF"),
    ("ITAU", "itaú", "#EC7000", "#FFFFFF"),
    ("C6", "C6", "#242424", "#FFFFFF"),
    ("MERCADO PAGO", "MP", "#00B1EA", "#FFFFFF"),
    ("MERCADOPAGO", "MP", "#00B1EA", "#FFFFFF"),
    ("MERCADO LIVRE", "ML", "#FFE600", "#2D3277"),
    ("SANTANDER", "S", "#EC0000", "#FFFFFF"),
    ("BRADESCO", "B", "#CC092F", "#FFFFFF"),
    ("BANCO DO BRASIL", "BB", "#FCFC30", "#465EFF"),
    ("CAIXA", "CX", "#005CA9", "#F39200"),
    ("INTER", "in", "#FF7A00", "#FFFFFF"),
    ("BRB", "BRB", "#0B3B8C", "#FFFFFF"),
    ("PICPAY", "PP", "#11C76F", "#FFFFFF"),
    ("NOMAD", "N", "#F9D649", "#1E1E1E"),
    ("AVENUE", "AV", "#0B1F3A", "#FFFFFF"),
    ("XP", "XP", "#000000", "#F6D300"),
    ("CLEAR", "CL", "#1A1A1A", "#41C3D3"),
    ("RICO", "R", "#FF5A00", "#FFFFFF"),
    ("BTG", "BTG", "#0A2240", "#FFFFFF"),
    ("WISE", "W", "#9FE870", "#163300"),
)


def marca(nome: str, cor: str | None = None) -> dict:
    """Sigla e cores para o avatar da conta/cartão; sem marca conhecida, a inicial na cor da conta."""
    texto = normalizar_descricao(nome).replace("CARTAO ", "")
    for trecho, sigla, fundo, frente in MARCAS:
        if trecho in texto:
            return {"sigla": sigla, "fundo": fundo, "frente": frente}
    return {"sigla": (texto[:1] or "?"), "fundo": cor or "#6366F1", "frente": "#FFFFFF"}
