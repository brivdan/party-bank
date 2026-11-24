// Party Bank: small progressive enhancements. Every page works without JS.
(function () {
  "use strict";

  // Ask before destructive form submits: <form data-confirm="...">
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // Payment forms: disable the button after the first click. The server's
  // idempotency key is the real guard against double payments.
  document.querySelectorAll("form.once").forEach(function (form) {
    form.addEventListener("submit", function () {
      var btn = form.querySelector("button[type=submit]");
      if (btn) { btn.disabled = true; btn.textContent = "Processing…"; }
    });
  });

  // Balance auto-refresh: poll a tiny JSON endpoint and reload the page when
  // a new ledger entry appears (e.g. incoming money), unless the user is typing.
  var balanceCard = document.querySelector("[data-balance-url]");
  if (balanceCard && window.fetch) {
    var lastEntry = balanceCard.dataset.lastEntry;
    setInterval(function () {
      if (document.hidden) return;
      fetch(balanceCard.dataset.balanceUrl, { credentials: "same-origin", cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data || String(data.last_entry) === String(lastEntry)) return;
          var active = document.activeElement;
          if (active && (active.tagName === "INPUT" || active.tagName === "SELECT")) return;
          window.location.reload();
        })
        .catch(function () { /* offline for a moment; try again next tick */ });
    }, 8000);
  }

  // Payment terminal: +/- buttons and a running total (display only; the
  // server recalculates the real total from database prices).
  var terminal = document.getElementById("terminal");
  if (terminal) {
    var currency = terminal.dataset.currency || "";
    var totalEl = document.getElementById("order-total");
    var fmt = function (cents) {
      return currency + (cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };
    var recalc = function () {
      var total = 0;
      terminal.querySelectorAll(".pos-item").forEach(function (item) {
        var qty = parseInt(item.querySelector(".qty").value, 10) || 0;
        item.classList.toggle("selected", qty > 0);
        total += qty * parseInt(item.dataset.price, 10);
      });
      var custom = parseFloat((terminal.querySelector(".custom-amount").value || "0").replace(",", "."));
      if (custom > 0) total += Math.round(custom * 100);
      totalEl.textContent = fmt(total);
    };
    terminal.addEventListener("click", function (e) {
      var btn = e.target.closest(".qty-btn");
      if (!btn) return;
      var input = btn.parentNode.querySelector(".qty");
      var next = (parseInt(input.value, 10) || 0) + parseInt(btn.dataset.step, 10);
      input.value = Math.max(0, Math.min(99, next));
      recalc();
    });
    terminal.addEventListener("input", recalc);
    recalc();
  }

  // Live table filter: <input class="filter" data-filter="#table-id">
  document.querySelectorAll("input.filter[data-filter]").forEach(function (input) {
    var table = document.querySelector(input.dataset.filter);
    if (!table) return;
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase().replace(/-/g, "");
      table.querySelectorAll("tbody tr").forEach(function (row) {
        var text = row.textContent.toLowerCase().replace(/-/g, "");
        row.hidden = q !== "" && text.indexOf(q) === -1;
      });
    });
  });

  // Click a card URL to copy it (for writing NFC tags).
  document.querySelectorAll(".copy").forEach(function (el) {
    el.addEventListener("click", function () {
      if (!navigator.clipboard) return;
      navigator.clipboard.writeText(el.textContent.trim()).then(function () {
        var old = el.title;
        el.title = "Copied!";
        el.style.background = "#e3f4e8";
        setTimeout(function () { el.title = old; el.style.background = ""; }, 800);
      });
    });
  });

  // Extract a card ID from an NFC payload (a full card URL or a bare ID).
  function tokenFromText(text) {
    var m = /\/card\/([A-Za-z0-9-]+)/.exec(text);
    return (m ? m[1] : text).replace(/[^A-Za-z0-9]/g, "").toUpperCase();
  }

  // Web NFC: only Chrome on Android, only over HTTPS (or localhost).
  // Manual entry and QR codes remain the fallback everywhere else.
  var scanBtn = document.getElementById("nfc-scan");
  var cardInput = document.getElementById("card-input");
  var status = document.getElementById("nfc-status");
  if (scanBtn && cardInput && "NDEFReader" in window && window.isSecureContext) {
    scanBtn.hidden = false;
    scanBtn.addEventListener("click", function () {
      var reader = new window.NDEFReader();
      var controller = new AbortController();
      status.textContent = "Hold the card near the back of your phone…";
      reader.scan({ signal: controller.signal }).then(function () {
        reader.onreading = function (event) {
          var decoder = new TextDecoder();
          for (var i = 0; i < event.message.records.length; i++) {
            var rec = event.message.records[i];
            if (rec.recordType === "url" || rec.recordType === "text" || rec.recordType === "absolute-url") {
              var token = tokenFromText(decoder.decode(rec.data));
              if (token) {
                cardInput.value = token.replace(/(.{4})(?=.)/g, "$1-");
                var radio = document.querySelector("input[name=card_mode][value=existing]");
                if (radio) radio.checked = true;
                status.textContent = "Card read.";
                controller.abort();
                var autoForm = cardInput.closest("form.nfc-autosubmit");
                if (autoForm) { autoForm.submit(); return; }
                var pin = document.querySelector("input[name=pin]");
                if (pin) pin.focus();
                return;
              }
            }
          }
          status.textContent = "No card ID found on this tag.";
        };
        reader.onreadingerror = function () { status.textContent = "Could not read the tag. Try again."; };
      }).catch(function (err) {
        status.textContent = "NFC unavailable: " + err.message;
      });
    });
  }
})();
