# Referência: Mobills Web (observado em 2026-10-05)

Navegação só de leitura na conta do usuário (nada salvo). Foco em entrada de receitas e despesas.

## Botão "+ Novo"
Menu com 4 ações: **Despesa**, **Receita**, **Despesa cartão**, **Transferência**. Cada uma abre um modal com cor própria: despesa vermelho, receita verde, cartão verde-azulado, transferência azul.

## Modal de Despesa / Receita
Campos visíveis, em ordem:
1. **Valor** grande, com ícone de **calculadora** à esquerda e moeda (BRL) à direita. Valida "deve ter um valor diferente de 0".
2. **Foi paga** / **Foi recebida**: toggle, **ligado por padrão**.
3. **Data**: chips **Hoje** (padrão), **Ontem**, **Outros…** (abre o calendário).
4. **Descrição** com ícone de **coração**, que favorita a descrição como modelo rápido.
5. **Categoria**: chip colorido com ícone. Vem pré-selecionada (despesa "Alimentação", receita "Bonificação"). A lista mostra categoria e subcategoria juntas.
6. **Conta**: chip com o logo do banco.
7. **Anexar arquivo** (comprovante).
8. **Ignorar transação**: toggle; tira o lançamento de saldos e relatórios.

**Mais detalhes** (abre uma segunda coluna no modal): **Tags**, **Observação**, **Despesa fixa / Receita fixa** (toggle), **Repetir N vezes** com periodicidade (Meses…).

Botões: **Salvar e criar nova** e **Salvar**.

## Modal de Despesa no cartão
Valor, data, descrição, categoria, **cartão** (chip com bandeira e final), **fatura de destino** (seletor editável, ex.: "4 de nov de 2026") e Ignorar transação. **Não tem** "Foi paga". Mais detalhes: Tags, Observação, **Despesa fixa** e **Parcelado** (N vezes).

## Modal de Transferência
Valor, data, **Conta de origem**, **Conta de destino** e Mais detalhes.

## Lista de transações
- Cards no topo: Saldo atual, Receitas, Despesas, Balanço mensal.
- Filtro de tipo ("Todas"), busca, filtros avançados e menu ⋮.
- **Tabela** (não agrupa por dia): checkbox de seleção em massa, **Situação** (ícone vermelho "!" = pendente, verde ✓ = paga), Data, Descrição (com "(10/12)" nas parcelas), Categoria (círculo com ícone), Conta, Valor colorido e Ações.
- Menu de ações da linha: **Efetivar**, **Editar**, **Anexar arquivo**, **Deletar**, **Converter em transferência**.

## Dashboard
- Cards: Saldo atual, Receitas, Despesas, Cartão de crédito.
- Roscas: despesas por categoria e receitas por categoria, com o total no centro.
- Balanço mensal.
- **Pendências e alertas**: total de despesas pendentes e de receitas pendentes.
- **Minhas contas**: saldo atual e **saldo previsto** por conta.
- **Cartões**: abas faturas abertas/fechadas, status (vencida/paga), data do pagamento, % de variação e limite disponível.
- **Objetivos**: valor atual / meta (ex.: fundo de emergência).
- **Gerenciar tela inicial**: o usuário escolhe os blocos do dashboard.

## Categorias do usuário no Mobills
Hierarquia **inferida** pela ordem da lista (pai seguido das filhas):
- **Despesa:**
  - Alimentação (Lanches, Mercado, Padaria, Restaurante)
  - Assinatura
  - Carro (Combustível, Estacionamento, Impostos, Manutenção, Multas)
  - Cartão Nubank
  - Casa (Aluguel, Condomínio, Energia Elétrica, Faxina, Gás Encanado, Impostos, Internet, Manutenção)
  - Compras, Educação, Eletrônicos, Investimentos
  - Lazer (Streaming)
  - Operação bancária, Outros, Pix, Poupança, Presente
  - Saúde (Academia, Plano de Saúde, Remédios, Suplementos)
  - Serviços, Supermercado, Vestuário
  - Viagem (Milhas)
- **Receita:** Bonificação, Empréstimo, Investimento, Outros, Pix, Presente, Renda extra, Salário, Transferência bancária.
