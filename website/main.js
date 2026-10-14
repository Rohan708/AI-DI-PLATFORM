// Lumida landing page: small, dependency-free interactions.
(() => {
  "use strict";

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  document.documentElement.classList.remove("no-js");

  // Navigation: border on scroll, mobile menu -------------------------------------------
  const nav = document.querySelector(".nav");
  const toggle = document.querySelector(".nav__toggle");
  const links = document.getElementById("nav-links");
  const onScroll = () => nav.classList.toggle("is-scrolled", window.scrollY > 8);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
  toggle.addEventListener("click", () => {
    const open = links.classList.toggle("is-open");
    toggle.setAttribute("aria-expanded", String(open));
  });
  links.addEventListener("click", (event) => {
    if (event.target.closest("a")) {
      links.classList.remove("is-open");
      toggle.setAttribute("aria-expanded", "false");
    }
  });

  // Hero: the scan lights up three cells, one after another ------------------------------
  const cells = [...document.querySelectorAll("[data-find]")];
  const chips = [...document.querySelectorAll("[data-chip]")];
  const light = (index) => {
    cells.forEach((cell) => cell.classList.toggle("is-lit", Number(cell.dataset.find) <= index));
    chips.forEach((chip) => chip.classList.toggle("is-lit", Number(chip.dataset.chip) <= index));
  };
  if (reduceMotion) {
    light(cells.length - 1);
  } else {
    // One cell at a time, hold with all three lit, then go dark and start again.
    const sequence = [0, 1, 2, 2, 2, -1];
    let step = 0;
    light(-1);
    setInterval(() => {
      light(sequence[step]);
      step = (step + 1) % sequence.length;
    }, 1300);
  }

  // Findings inbox: expand rows, confirm or dismiss -------------------------------------
  const inbox = document.getElementById("inbox");
  inbox.addEventListener("click", (event) => {
    const row = event.target.closest(".finding__row");
    if (row) {
      const body = row.nextElementSibling;
      const open = row.getAttribute("aria-expanded") === "true";
      row.setAttribute("aria-expanded", String(!open));
      body.hidden = open;
      return;
    }
    const action = event.target.closest("[data-act]");
    if (action) {
      const finding = action.closest(".finding");
      finding.classList.add("is-done");
      action.closest(".finding__actions").textContent =
        action.dataset.act === "confirm"
          ? "Confirmed. It stays open until it's fixed."
          : "Marked as normal. It won't be raised again.";
    }
  });

  // "It asks. You answer." --------------------------------------------------------------
  const ask = document.getElementById("ask");
  const result = ask.querySelector(".ask__result");
  const outcomes = {
    yes: "→ rule added: shop.addresses.postal_code is always filled. Checked from tonight.",
    no: "→ noted: empty postal codes are fine here. Nothing will alert on them.",
    skip: "→ skipped. Lumida will ask again in 30 days.",
  };
  ask.addEventListener("click", (event) => {
    const button = event.target.closest("[data-answer]");
    if (button) result.textContent = outcomes[button.dataset.answer];
  });

  // Terminal: type the run out once it scrolls into view --------------------------------
  const terminal = document.getElementById("terminal");
  const lines = terminal.textContent.split("\n");
  const render = (count, caret) => {
    const html = lines
      .slice(0, count)
      .map((line, i) =>
        i === 0
          ? `<span class="prompt">$</span>${escapeHtml(line.slice(1))}`
          : escapeHtml(line).replace(/\bok\b/, '<span class="ok">ok</span>'),
      )
      .join("\n");
    terminal.innerHTML = `<code>${html}${caret ? '<span class="caret"></span>' : ""}</code>`;
  };
  if (reduceMotion || !("IntersectionObserver" in window)) {
    render(lines.length, false);
  } else {
    render(0, true);
    const typeOut = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      typeOut.disconnect();
      let shown = 0;
      const next = () => {
        shown += 1;
        render(shown, shown < lines.length);
        if (shown < lines.length) setTimeout(next, shown === 1 ? 700 : 380);
      };
      next();
    }, { threshold: 0.5 });
    typeOut.observe(terminal);
  }

  // Reveal sections as they scroll in ---------------------------------------------------
  const revealed = document.querySelectorAll(".reveal");
  if (reduceMotion || !("IntersectionObserver" in window)) {
    revealed.forEach((el) => el.classList.add("is-visible"));
  } else {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15 });
    revealed.forEach((el) => observer.observe(el));
  }

  // Early access: no sign-up backend yet, so say so honestly ----------------------------
  // Set this to a form endpoint (e.g. Formspree, or your own API) when there is one.
  const EARLY_ACCESS_ENDPOINT = "";
  const form = document.getElementById("signup");
  const message = document.getElementById("signup-msg");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const email = form.email.value.trim();
    message.classList.remove("is-error");
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      message.textContent = "Please enter a valid work email.";
      message.classList.add("is-error");
      return;
    }
    if (!EARLY_ACCESS_ENDPOINT) {
      message.textContent = "Sign-ups open soon. This page is a preview and doesn't store emails yet.";
      return;
    }
    try {
      const response = await fetch(EARLY_ACCESS_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ email }),
      });
      if (!response.ok) throw new Error(String(response.status));
      form.reset();
      message.textContent = "Thanks! We'll be in touch.";
    } catch {
      message.textContent = "That didn't go through. Please try again in a moment.";
      message.classList.add("is-error");
    }
  });

  document.getElementById("year").textContent = String(new Date().getFullYear());

  function escapeHtml(text) {
    return text.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  }
})();
