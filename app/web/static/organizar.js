/* Organizar arrastando (PC) ou pelas setas (celular/teclado): reordena o DOM e salva na hora.
   Uso: x-data="organizarLista('conta', '/contas/ordem')" @organizar.window="alternar($event)" na lista; os itens são os
   filhos diretos com data-conta="<id>" e chamam iniciar/sobre/soltar; as setas chamam mover($el, -1 | 1).
   O botão do topo faz $dispatch('organizar'); "Concluir" faz $dispatch('organizar', 'sair').
   Salvar manda os ids de todos os [data-<nome>] da página: contas em mais de uma lista (dentro e fora do saldo
   líquido) vão juntas. */
function organizarLista(nome, url) {
  const atributo = "data-" + nome;
  return {
    organizando: false, arrastando: null, lista: null,
    // a lista guardada no init: chamado de dentro de um item com x-data próprio, $root seria o item
    init() { this.lista = this.$el; },
    itens() { return [...this.lista.querySelectorAll(`:scope > [${atributo}]`)]; },
    alternar(e) { this.organizando = e.detail === "sair" ? false : !this.organizando; },
    iniciar(e) {
      if (!this.organizando) return;
      this.arrastando = e.currentTarget;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", this.arrastando.getAttribute(atributo));
      this.arrastando.classList.add("opacity-50");
    },
    sobre(e) {
      const alvo = e.currentTarget;
      if (!this.arrastando || alvo === this.arrastando || !this.itens().includes(alvo)) return;
      e.preventDefault();
      // vindo de antes, entra depois do alvo; vindo de depois, entra antes: funciona em grade e em coluna
      const lista = this.itens();
      const depois = lista.indexOf(this.arrastando) < lista.indexOf(alvo);
      alvo.parentNode.insertBefore(this.arrastando, depois ? alvo.nextSibling : alvo);
    },
    soltar() {
      if (!this.arrastando) return;
      this.arrastando.classList.remove("opacity-50");
      this.arrastando = null;
      this.salvar();
    },
    mover(botao, passo) {
      const item = botao.closest(`[${atributo}]`);
      const lista = this.itens();
      const vizinho = lista[lista.indexOf(item) + passo];
      if (!vizinho) return;
      item.parentNode.insertBefore(item, passo < 0 ? vizinho : vizinho.nextSibling);
      botao.focus();
      this.salvar();
    },
    async salvar() {
      const dados = new URLSearchParams();
      for (const el of document.querySelectorAll(`[${atributo}]`)) dados.append("ids", el.getAttribute(atributo));
      try { await fetch(url, { method: "POST", body: dados }); } catch (e) { /* tenta de novo no próximo movimento */ }
    },
  };
}
