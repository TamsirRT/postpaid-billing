// Parent page: keep each child's Pay button showing the amount that's selected.
document.addEventListener("change", function (e) {
  var input = e.target;
  if (!input.matches || !input.matches('.pay-form input[name="lunches"]')) return;
  var button = input.form && input.form.querySelector("[data-pay-button]");
  if (button && input.dataset.amount) button.textContent = "Pay " + input.dataset.amount;
});
