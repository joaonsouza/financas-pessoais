// Seletor de categoria (macro _seletor_categoria.html): busca sem acento, grupos por mãe, mais usadas e teclado.
// O valor fica num <input type="hidden">; quem preenche por fora dispara "definir" nele (definirCategoria).
(function () {
  var cache = {};

  function dados(tipo) {
    if (!cache[tipo]) {
      var el = document.getElementById("categorias-" + tipo);
      if (!el) return { grupos: [], mais_usadas: [], por_id: {} };
      cache[tipo] = JSON.parse(el.textContent);
    }
    return cache[tipo];
  }

  function semAcento(texto) {
    return String(texto).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  }

  window.definirCategoria = function (raiz, nome, valor) {
    var input = raiz && raiz.querySelector('input[type=hidden][name="' + nome + '"]');
    if (input) input.dispatchEvent(new CustomEvent("definir", { detail: valor == null ? "" : String(valor) }));
  };

  document.addEventListener("alpine:init", function () {
    Alpine.data("seletorCategoria", function (cfg) {
      return {
        aberto: false,
        busca: "",
        valor: String(cfg.valor == null ? "" : cfg.valor),
        ativo: 0,
        estilo: "",
        criando: false,   // criação rápida: escolhendo a mãe da categoria nova
        salvando: false,
        erro: "",
        busca_el: null,   // elementos do painel, que vai para o <body> (x-teleport) e sai do alcance de $refs
        lista_el: null,
        _fechar_ao_rolar: null,

        init: function () {
          var self = this;
          this.$refs.input.addEventListener("definir", function (e) { self.definir(e.detail); });
          this.$watch("busca", function () { self.ativo = 0; self.criando = false; self.erro = ""; });
        },

        get d() { return dados(cfg.tipo); },

        permitido: function (id) { return !cfg.somente || cfg.somente.indexOf(Number(id)) >= 0; },

        extra: function (v) {
          var todos = cfg.topo.concat(cfg.fim);
          for (var i = 0; i < todos.length; i++) if (String(todos[i][0]) === v) return todos[i][1];
          return null;
        },

        get rotuloAtual() {
          var extra = this.extra(this.valor);
          if (extra !== null) return extra;
          var c = this.d.por_id[this.valor];
          if (c) return c.rotulo + (c.inativa ? " (inativa)" : "");
          return this.valor === "" && cfg.vazio ? cfg.vazio : cfg.placeholder;
        },

        get corAtual() {
          var c = this.d.por_id[this.valor];
          return c ? c.cor : null;
        },

        get linhas() {
          var q = semAcento(this.busca.trim()), out = [], n = 0, self = this, receitas = false;
          function casa(t) { return !q || semAcento(t).indexOf(q) >= 0; }
          function opcao(valor, rotulo, cor, nivel) {
            out.push({ valor: String(valor), rotulo: rotulo, cor: cor, nivel: nivel || 0, i: n++ });
          }
          cfg.topo.forEach(function (e) { if (casa(e[1])) opcao(e[0], e[1], null); });
          if (!q) {
            var mais = this.d.mais_usadas.filter(function (id) { return self.permitido(id) && self.d.por_id[id]; });
            if (mais.length) {
              out.push({ titulo: "Mais usadas" });
              mais.forEach(function (id) { var c = self.d.por_id[id]; opcao(id, c.rotulo, c.cor); });
            }
          }
          this.d.grupos.forEach(function (g) {
            var casaMae = q && casa(g.nome);
            var filhas = g.filhas.filter(function (f) { return self.permitido(f.id) && (casaMae || casa(f.nome)); });
            var mae = self.permitido(g.id) && (!q || casaMae);
            if (!mae && !filhas.length) return;
            if (cfg.tipo === "todas" && g.tipo === "receita" && !receitas) {
              receitas = true;
              out.push({ titulo: "Receitas" });
            }
            // sem filhas que possam ser escolhidas (ou nenhuma): a mãe é uma linha só, sem título nem "(geral)"
            if (!g.filhas.some(function (f) { return self.permitido(f.id); })) { opcao(g.id, g.nome, g.cor); return; }
            out.push({ titulo: g.nome });
            if (mae) opcao(g.id, g.nome + " (geral)", g.cor);
            filhas.forEach(function (f) { opcao(f.id, f.nome, f.cor, 1); });
          });
          cfg.fim.forEach(function (e) { if (casa(e[1])) opcao(e[0], e[1], null); });
          if (cfg.vazio && casa(cfg.vazio)) opcao("", cfg.vazio, null);
          return out;
        },

        get opcoes() { return this.linhas.filter(function (l) { return !l.titulo; }); },

        // busca sem nenhuma categoria encontrada: oferece criar (só onde a macro pediu `criar`)
        get semResultado() {
          var self = this;
          return !!cfg.criar && !!this.busca.trim() && !this.opcoes.some(function (o) { return self.d.por_id[o.valor]; });
        },

        get maes() { return this.d.grupos.filter(function (g) { return g.tipo === cfg.tipo; }); },

        iniciarCriacao: function () {
          this.erro = "";
          this.criando = true;
        },

        criar: function (paiId) {
          var self = this;
          if (this.salvando) return;
          this.salvando = true;
          this.erro = "";
          fetch("/categorias/rapida", {
            method: "POST",
            body: new URLSearchParams({ nome: this.busca.trim(), tipo: cfg.tipo, pai_id: paiId == null ? "" : paiId }),
          })
            .then(function (r) { return r.json().then(function (corpo) { return { ok: r.ok, corpo: corpo }; }); })
            .then(function (res) {
              if (!res.ok) { self.erro = res.corpo.erro || "Não deu para criar a categoria."; return; }
              self.inserir(res.corpo);
              self.criando = false;
              self.aplicar(String(res.corpo.id));
              self.fechar();
            })
            .catch(function () { self.erro = "Não deu para criar a categoria."; })
            .finally(function () { self.salvando = false; });
        },

        // a categoria nova entra nos dados da página, valendo para os outros seletores do mesmo tipo
        inserir: function (nova) {
          var d = this.d;
          var porNome = function (a, b) { return semAcento(a.nome).localeCompare(semAcento(b.nome)); };
          d.por_id[nova.id] = { nome: nova.nome, rotulo: nova.rotulo, cor: nova.cor, inativa: false };
          if (nova.pai_id) {
            var mae = d.grupos.find(function (g) { return g.id === nova.pai_id; });
            if (mae) { mae.filhas.push({ id: nova.id, nome: nova.nome, cor: nova.cor }); mae.filhas.sort(porNome); }
          } else {
            d.grupos.push({ id: nova.id, nome: nova.nome, cor: nova.cor, tipo: nova.tipo, filhas: [] });
            d.grupos.sort(function (a, b) { return (a.tipo === "receita") - (b.tipo === "receita") || porNome(a, b); });
          }
        },

        abrir: function () {
          var self = this;
          this.busca = "";
          this.criando = false;
          this.posicionar();
          this.aberto = true;
          this._fechar_ao_rolar = function (e) {
            if (!self.lista_el || !self.lista_el.contains(e.target)) self.fechar(false);
          };
          window.addEventListener("scroll", this._fechar_ao_rolar, true);
          window.addEventListener("resize", this._fechar_ao_rolar);
          var i = this.valor === "" ? -1 : this.opcoes.findIndex(function (o) { return o.valor === self.valor; });
          this.ativo = i < 0 ? 0 : i;  // vazio começa na primeira opção, não em "Sem categoria" lá no fim
          this.$nextTick(function () {
            if (self.busca_el) self.busca_el.focus({ preventScroll: true });
            self.rolar();
          });
        },

        // no PC o painel é fixo logo abaixo (ou acima) do botão; no celular é uma folha no pé da tela (só CSS)
        posicionar: function () {
          if (!window.matchMedia("(min-width: 640px)").matches) { this.estilo = ""; return; }
          var r = this.$refs.botao.getBoundingClientRect();
          var largura = Math.max(r.width, 320);
          var esquerda = Math.max(8, Math.min(r.left, window.innerWidth - largura - 8));
          var abaixo = window.innerHeight - r.bottom - 12, acima = r.top - 12;
          var embaixo = abaixo >= 240 || abaixo >= acima;
          var altura = Math.min(384, embaixo ? abaixo : acima);
          var topo = embaixo ? r.bottom + 4 : r.top - 4 - altura;
          this.estilo = "top:" + topo + "px;left:" + esquerda + "px;width:" + largura + "px;max-height:" + altura + "px";
        },

        fechar: function (focar) {
          this.aberto = false;
          window.removeEventListener("scroll", this._fechar_ao_rolar, true);
          window.removeEventListener("resize", this._fechar_ao_rolar);
          if (focar !== false) this.$refs.botao.focus();
        },

        aplicar: function (v) {
          this.valor = v;
          this.$refs.input.value = v;
          this.$refs.input.dispatchEvent(new Event("change", { bubbles: true }));
        },

        escolher: function (o) {
          this.aplicar(o.valor);
          this.fechar();
        },

        definir: function (v) {
          v = String(v == null ? "" : v);
          if (this.extra(v) !== null || (v === "" && cfg.vazio) || (this.d.por_id[v] && this.permitido(v))) {
            this.aplicar(v);
          }
        },

        mover: function (passo) {
          var total = this.opcoes.length;
          if (!total) return;
          this.ativo = (this.ativo + passo + total) % total;
          this.rolar();
        },

        confirmar: function () {
          var o = this.opcoes[this.ativo];
          if (o) this.escolher(o);
          else if (this.semResultado) this.iniciarCriacao();
        },

        rolar: function () {
          var lista = this.lista_el;
          this.$nextTick(function () {  // à mão: scrollIntoView rolaria a página e fecharia o painel
            var el = lista && lista.querySelector('[data-ativo="true"]');
            if (!el) return;
            var topo = el.offsetTop, base = topo + el.offsetHeight;
            if (topo < lista.scrollTop) lista.scrollTop = topo;
            else if (base > lista.scrollTop + lista.clientHeight) lista.scrollTop = base - lista.clientHeight;
          });
        },
      };
    });
  });
})();
