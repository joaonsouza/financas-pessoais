# Guia de UI — App de Finanças

Gerado com a skill ui-ux-pro-max (design system "Personal Finance Tracker" + estilo Bento Grid), adaptado ao estilo Mobills. Fonte única de verdade visual: toda tela nova segue este guia.

## Direção
- Estilo **Bento / cartões modulares**: fundo neutro, cartões brancos com cantos 20px, sombra suave, hierarquia clara, muito espaço negativo. Nada de gradientes roxo/rosa genéricos, nada "brincalhão".
- Tom: confiável, limpo, financeiro. Referência de UX: Mobills (ícones de categoria em círculos coloridos, lista por dia, botão "+" flutuante).
- Modo claro padrão + **modo escuro automático** (`prefers-color-scheme`).

## Tokens (definir em `app/web/static/input.css` com `@theme` do Tailwind v4 e variáveis CSS)
| Token | Claro | Escuro |
|---|---|---|
| `--color-page` | `#F5F5F7` | `#0B1120` |
| `--color-card` | `#FFFFFF` | `#151E32` |
| `--color-ink` (texto) | `#0F172A` | `#F1F5F9` |
| `--color-ink-muted` | `#64748B` | `#94A3B8` |
| `--color-line` (borda) | `#E2E8F0` | `rgba(255,255,255,0.08)` |
| `--color-primary` | `#1E40AF` | `#3B82F6` |
| `--color-primary-strong` (hover) | `#1E3A8A` | `#2563EB` |
| `--color-primary-soft` (fundos de destaque) | `#DBEAFE` | `rgba(59,130,246,0.15)` |
| `--color-income` (receita/positivo) | `#059669` | `#34D399` |
| `--color-expense` (despesa/negativo) | `#DC2626` | `#F87171` |
| `--color-warning` | `#B45309` | `#FBBF24` |

Expor como utilitários Tailwind (`bg-page`, `bg-card`, `text-ink`, `text-ink-muted`, `border-line`, `bg-primary`, `text-income`, `text-expense`...). Nos templates usar os tokens, não cores cruas (exceto a cor dinâmica de cada categoria/conta, que vem do banco via `style`).

## Tipografia
- **IBM Plex Sans** (Google Fonts, pesos 400/500/600/700) para tudo; fallback `ui-sans-serif, system-ui`.
- Base 16px, line-height 1.5. Títulos de página 24px/600. Valores grandes (saldo) 30–36px/600.
- **Todo valor monetário** com `tabular-nums`.

## Ícones
- **Lucide** (SVG). Vendorizar `app/web/static/vendor/lucide.min.js` (UMD de `https://unpkg.com/lucide@latest/dist/umd/lucide.min.js`) e usar `<i data-lucide="nome" class="size-5"></i>` + `lucide.createIcons()` no load e após cada swap do HTMX (`htmx:afterSwap`).
- **Proibido emoji/caractere como ícone** (✓ ✕ ↻ ‹ › +). Substituir: `check`, `x`, `repeat`, `chevron-left`, `chevron-right`, `plus`.
- Menu: Dashboard `layout-dashboard`, Lançamentos `arrow-left-right`, Contas `landmark`, Cartões `credit-card`, Configurações `settings`.
- Categoria: círculo 36–40px com fundo = cor da categoria a ~15% (ex.: `style="background: {{cor}}26; color: {{cor}}"`) e ícone `tag` (ou o `icone` da categoria quando houver). Receita: `arrow-down-left`; despesa sem categoria: `circle-dashed`; transferência: `arrow-left-right`; pagamento de fatura: `credit-card`; ajuste: `sliders-horizontal`.

## Componentes
- **Cartão**: `bg-card rounded-[20px] border border-line shadow-[0_4px_6px_rgba(0,0,0,0.04)] p-5`.
- **Botão primário**: `bg-primary text-white rounded-xl px-4 h-11 font-medium hover:bg-primary-strong transition-colors duration-150 cursor-pointer focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-primary/30`. Secundário: fundo `primary-soft`, texto `primary`. Fantasma: texto `ink-muted`, hover fundo `page`.
- **Inputs**: label visível acima, `h-11 rounded-xl border border-line bg-card px-3 focus:ring-4 focus:ring-primary/20 focus:border-primary`. Erro: mensagem perto do campo/topo do form com ícone `alert-circle`, cor expense.
- **Alvos de toque/clique ≥ 44×44px** (check de efetivado, botões de ícone).
- Todo elemento clicável: `cursor-pointer` + estado hover com transição 150–200ms. Foco visível em tudo (ring 3–4px). Respeitar `prefers-reduced-motion` (desligar transições).
- Botões só-ícone sempre com `aria-label`.

## Layout
- **Sidebar** fixa 240px em telas ≥ 1024px, fundo `card`, borda direita `line`; item ativo com fundo `primary-soft` + texto `primary` + ícone. < 1024px: topo com botão de menu (`menu`) que abre a sidebar como gaveta (Alpine), sem scroll horizontal.
- **Cabeçalho da página**: título à esquerda; seletor de mês à direita em "pílula" (`chevron-left` · "Outubro 2026" · `chevron-right`).
- **FAB "+"**: 56px, `bg-primary`, sombra maior, canto inferior direito; menu de ações em cartões com ícone colorido (Despesa expense, Receita income, Transferência primary, Despesa no cartão roxo `#7C3AED`).
- **Modal**: overlay `bg-slate-900/50` com blur leve, painel `rounded-3xl` max 512px, foco no campo valor, fechar com Esc/botão `x`. Campo valor grande (36px) com prefixo "R$". Chips "Hoje"/"Ontem".
- Grade do dashboard (bento): linha 1 — cartão herói de saldo (span 2, fundo `primary`, texto branco, alternância Atual/Previsto) + Receitas + Despesas; linha 2 — Despesas por categoria (span 2, rosca + legenda) + A pagar; linha 3 — Contas + Cartões. Responsivo 4→2→1 colunas.
- Lista de lançamentos: grupos por dia com cabeçalho "Seg, 05 out" e total do dia; cada linha com círculo da categoria, descrição + subtítulo (categoria · conta · tags), valor colorido à direita, check de efetivado (círculo vazio = pendente; verde com `check` = efetivado); ações Editar/Excluir como botões de ícone (`pencil`, `trash-2`) que aparecem no hover e no foco.
- Cartões de crédito: visual de cartão físico (gradiente `#1E3A8A → #1E40AF`, cantos 20px) com nome, fatura atual, vencimento, barra de limite usado.

## Gráficos
- Rosca com `cutout 70%`, valor total no centro, legenda em lista ao lado (cor + nome + valor + %), tooltip em BRL. Não depender só de cor: legenda sempre com texto.

## Checklist antes de entregar uma tela
- [ ] Sem emoji/caractere como ícone; Lucide em tudo
- [ ] `cursor-pointer` e hover 150–200ms em todo clicável
- [ ] Contraste ≥ 4.5:1 (claro e escuro)
- [ ] Foco visível; `aria-label` em botões de ícone
- [ ] `prefers-reduced-motion` respeitado
- [ ] Sem scroll horizontal em 375 / 768 / 1024 / 1440px
- [ ] Valores com `tabular-nums`; receitas `text-income`, despesas `text-expense`
