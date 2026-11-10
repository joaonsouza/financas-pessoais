/* Máscara de reais nos campos com data-mascara: os dígitos entram pela direita ("1" -> "0,01", "1234" -> "12,34",
   "123456" -> "1.234,56"). data-mascara="conta" (valor do lançamento, que aceita contas como 12,50+8) formata cada
   número da conta. O formato bate com parse_reais/avaliar_valor no servidor. */
(function () {
  function milhar(inteiro) {
    return inteiro.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  }

  // `inicial`: valor que veio do servidor; sem vírgula ("100") é número inteiro de reais, não centavos
  function reais(texto, inicial) {
    let d = texto.replace(/\D/g, "");
    if (!d) return "";
    if (inicial && texto.indexOf(",") < 0) d += "00";
    d = d.replace(/^0+/, "").padStart(3, "0");
    return milhar(d.slice(0, -2)) + "," + d.slice(-2);
  }

  function aplicar(valor, modo, inicial) {
    if (modo === "conta") {
      return valor.split(/([+\-*/x×÷()])/).map((p, i) => (i % 2 ? p : reais(p, inicial))).join("");
    }
    return (valor.trim().startsWith("-") ? "-" : "") + reais(valor, inicial);
  }

  function formatar(el, inicial) {
    if (!el.value) return;
    const novo = aplicar(el.value, el.dataset.mascara, inicial);
    if (novo !== el.value) el.value = novo;
  }

  // captura: formata antes do x-model do Alpine ler o valor
  document.addEventListener("input", (e) => {
    if (e.target.matches && e.target.matches("input[data-mascara]")) formatar(e.target, false);
  }, true);

  function iniciar(raiz) {
    raiz.querySelectorAll("input[data-mascara]").forEach((el) => formatar(el, true));
  }
  document.addEventListener("DOMContentLoaded", () => iniciar(document));
  document.addEventListener("htmx:afterSwap", (e) => iniciar(e.target));

  window.mascaraReais = aplicar; // para testes no console
})();
