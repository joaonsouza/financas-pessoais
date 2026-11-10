# Amostras reais de importação — achados (sem dados pessoais)

As amostras originais ficam em `Z:\Meu Drive\Finanças\App\app-financas-amostras\` (fora do git). As fixtures de teste do Plano 2 serão versões **anonimizadas**, com o mesmo layout e nomes, documentos e valores trocados.

## Itaú — fatura do cartão em PDF (6 páginas)
- **Texto:** extraível com `pdfplumber`, sem senha e sem OCR.
- **Palavras grudadas** no `extract_text()` padrão ("Totaldafaturaanterior"). Extrair por palavras com posição (`extract_words`) ou com `layout=True`.
- **Duas colunas** de lançamentos lado a lado na mesma página. É preciso recortar cada página em metade esquerda e direita (`page.crop`) e ler cada coluna separadamente, antes de mandar o texto pra IA.
- **Cada lançamento ocupa 2 linhas:**
  - linha 1: `DD/MM ESTABELECIMENTO [NN/NN] VALOR`. A parcela às vezes vem grudada no nome ("LOJAX03/04"). O valor é BR ("1.234,56") e pode ser negativo (estorno, "-0,01").
  - linha 2: `CATEGORIA_ITAU.CIDADE` (ALIMENTAÇÃO, SAÚDE, VEÍCULOS, VESTUÁRIO, DIVERSOS, HOBBY, TURISMO E ENTRETENIM.). **Usar como pista de categoria.**
- **Blocos por portador:** titular e adicionais, cada um com "(final NNNN)" e subtotal "Lançamentos no cartão (final NNNN) X". Guardar o final como tag opcional (ex.: `#final-1234`).
- **Parcelas antigas** aparecem com a data da compra original (meses atrás). A fatura de destino é a do PDF, nunca a calculada pela data.
- **Página 1 (resumo):** total da fatura anterior, pagamento efetuado, saldo financiado, lançamentos atuais, **total desta fatura**, **vencimento**, **limite total**, pagamento mínimo, opções de parcelamento, emissão, **previsão do próximo fechamento**.
- **Validação:** a soma dos lançamentos da fatura deve bater com "Lançamentos atuais" (ou com o total desta fatura menos o saldo financiado).
- **Páginas finais:** juros e encargos, avisos e "produtos e serviços". Ignorar.

## Nubank — fatura do cartão em OFX
- OFX 1.02 SGML, com tags de fechamento. **O cabeçalho mente:** declara `ENCODING:USASCII` / `CHARSET:1252`, mas o arquivo é **UTF-8** (tem acentos). Decodificar como UTF-8 primeiro e cair pra cp1252 se falhar, antes de passar pro `ofxparse` (ou fazer um parser próprio).
- Bloco `CREDITCARDMSGSRSV1/CCSTMTRS`. `CCACCTFROM/ACCTID` é um **UUID do cartão** e vira o `identificador_externo` do cartão. `ORG`: "NU PAGAMENTOS S.A.", `FID` 260.
- **`FITID` NÃO é único:** em 82 transações há 64 FITIDs distintos, com grupos de 3 e 4 repetidos (provavelmente parcelas da mesma compra). A deduplicação **não pode** usar só o FITID: a chave é `FITID + data + valor + memo + nº da ocorrência`.
- `TRNTYPE`: DEBIT para compras (`TRNAMT` negativo) e CREDIT para estornos (positivo; memo "Crédito de \"…\""). No app, o estorno é `despesa` com valor positivo na mesma fatura.
- **Parcelas no MEMO:** "… - Parcela 1/2" ou "… - 1/4". Regex: `(?:Parcela\s*)?(\d{1,2})/(\d{1,2})\s*$`.
- `DTSTART`–`DTEND` é o período da fatura (24/09 a 25/10 na amostra, **fatura aberta**). `DTPOSTED` é a data da compra.
- `LEDGERBAL/BALAMT` ≠ soma das transações (na amostra, o BALAMT era cerca de 35% maior): inclui parcelas futuras ou saldo. **Não usar o BALAMT** pra validar a soma no OFX; mostrar só como informação.

## Mercado Pago — fatura do cartão em PDF (5 páginas)
- **Texto limpo:** uma coluna, com espaços entre as palavras. Dá pra ler com regex local, sem IA (a IA fica como reserva).
- **Página 1 (resumo):** "Essa é sua fatura de <mês>", **Total a pagar**, **Vence em**, **Limite total**, "Consumos de DD/MM a DD/MM", "Pagamentos e créditos devolvidos", "Total da fatura de <mês anterior>", "Total".
- **Página 2 ("Detalhes de consumo"):**
  - Bloco "Movimentações na fatura", com o **pagamento da fatura anterior** ("Pagamento da fatura de setembro/AAAA R$ X"). **Não é compra:** é conciliar com o pagamento ou ignorar.
  - Bloco por cartão, "Cartão Visa [************NNNN]", com linhas `DD/MM DESCRICAO [Parcela X de Y] R$ VALOR` e o "Total R$ X" no fim.
  - Regex da parcela: `Parcela (\d+) de (\d+)`. As datas são da compra original (meses atrás).
- **Página 4 (datas):** **Melhor dia de compra**, **Fechamento da fatura**, **Próximo fechamento**, limite utilizado e disponível. Serve pra preencher ou validar o dia de fechamento do cartão.
- **Páginas 3 e 5:** ofertas de parcelamento e textos legais. Ignorar.
- **Validação:** a soma das linhas do bloco do cartão = "Total" do bloco = "Total a pagar".

## Itaú — extrato da conta corrente em PDF (2 páginas)
- **Texto limpo:** uma coluna, e o `pdfplumber` detecta tabelas. Dá pra ler com regex ou tabela, sem IA.
- **Cabeçalho:** titular, **CPF**, agência e conta (mascarar tudo se for pra IA), **saldo em conta**, limite da conta (cheque especial) utilizado, disponível e total, **período de visualização** e emissão.
- **Linhas** `DD/MM/AAAA DESCRIÇÃO VALOR`, em **ordem decrescente** de data. Valor BR com sinal (`-500,00`).
- **Linhas "SALDO DO DIA X":** não são lançamentos, mas trazem o saldo de fechamento de cada dia. **Usar pra validar a extração** (saldo anterior + lançamentos do dia = saldo do dia) e pra conferir o saldo da conta no app.
- **Descrições com padrões úteis pra conciliação:**
  - `ITAU VISA NNNN-NNNN`, `FATURA UNICLASS SIGNATURE` → **pagamento de fatura** de cartão Itaú.
  - `PIX QRS NU PAGAMENT…` → pagamento da fatura **Nubank**. `PIX QRS MERCADO PAG…` → fatura **Mercado Pago** (bate com "Pagamento da fatura" no PDF do MP).
  - `PIX TRANSF <NOME>DD/MM` (data grudada no fim) → transferência. Quando o nome é do próprio titular, é **candidata a transferência entre contas** do usuário.
  - `REMUNERACAO/SALARIO` → receita Salário. `REND PAGO APLIC AUT…` → Rendimentos.
  - `JUROS LIMITE DA CONTA`, `IOF` → encargos de **cheque especial** (alerta do curso no Plano 3).
  - `PAG BOLETO …`, `PAGAMENTOCONTA <EMPRESA>`, `INT /<EMPRESA>` → contas de consumo (luz, gás, saúde).
- **Ignorar:** bloco "Aviso!" e textos legais no fim.

## Ainda faltam
- Nubank conta (OFX ou CSV)
- C6: fatura do cartão (PDF/CSV) e extrato da conta
