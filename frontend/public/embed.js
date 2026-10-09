(function () {
  "use strict";

  var containers = document.querySelectorAll("[data-pulse-signup]");
  if (!containers.length) return;

  var API_BASE = (function () {
    var scripts = document.querySelectorAll("script[src*='embed.js']");
    if (scripts.length) {
      var src = scripts[scripts.length - 1].src;
      return src.replace(/\/embed\.js.*$/, "/api/v1");
    }
    return "https://pulse.doaide.com/api/v1";
  })();

  containers.forEach(function (el) {
    var name = el.getAttribute("data-name") || "Newsletter";
    var color = el.getAttribute("data-color") || "#F0B429";
    var layout = el.getAttribute("data-layout") || "compact";
    var source = el.getAttribute("data-source") || "embed";

    var wrapper = document.createElement("div");
    wrapper.style.cssText =
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;" +
      "max-width:400px;border:1px solid " + color + "33;border-radius:12px;padding:20px;" +
      "background:#fff;";

    var title = document.createElement("p");
    title.textContent = "Subscribe to " + name;
    title.style.cssText = "margin:0 0 8px;font-weight:600;font-size:15px;color:#111;";
    wrapper.appendChild(title);

    if (layout === "full") {
      var desc = document.createElement("p");
      desc.textContent = "Get the latest insights delivered to your inbox.";
      desc.style.cssText = "margin:0 0 12px;font-size:13px;color:#666;";
      wrapper.appendChild(desc);
    }

    var form = document.createElement("form");
    form.style.cssText = "display:flex;gap:8px;";

    var input = document.createElement("input");
    input.type = "email";
    input.placeholder = "you@example.com";
    input.required = true;
    input.style.cssText =
      "flex:1;padding:8px 12px;border:1px solid #ddd;border-radius:8px;" +
      "font-size:14px;outline:none;";

    var btn = document.createElement("button");
    btn.type = "submit";
    btn.textContent = "Subscribe";
    btn.style.cssText =
      "padding:8px 16px;border:none;border-radius:8px;font-size:14px;" +
      "font-weight:500;cursor:pointer;background:" + color + ";color:#0A0A0B;";

    form.appendChild(input);
    form.appendChild(btn);
    wrapper.appendChild(form);

    var msg = document.createElement("p");
    msg.style.cssText = "margin:8px 0 0;font-size:12px;display:none;";
    wrapper.appendChild(msg);

    var powered = document.createElement("p");
    powered.style.cssText = "margin:10px 0 0;font-size:11px;color:#999;text-align:center;";
    powered.innerHTML =
      'Powered by <a href="https://pulse.doaide.com?ref=embed" target="_blank" rel="noopener" ' +
      'style="color:' + color + ';text-decoration:none;font-weight:500;">DoAide Pulse</a>';
    wrapper.appendChild(powered);

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var email = input.value.trim();
      if (!email) return;
      btn.disabled = true;
      btn.textContent = "…";

      fetch(API_BASE + "/subscribers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email, source: source }),
      })
        .then(function (r) {
          if (r.ok || r.status === 429) {
            msg.textContent = r.ok
              ? "You’re subscribed! \u{1F389}"
              : "Too many requests. Please try again later.";
            msg.style.color = r.ok ? "#16a34a" : "#dc2626";
            msg.style.display = "block";
            if (r.ok) {
              input.value = "";
              form.style.display = "none";
            }
          } else {
            throw new Error("Request failed");
          }
        })
        .catch(function () {
          msg.textContent = "Something went wrong. Please try again.";
          msg.style.color = "#dc2626";
          msg.style.display = "block";
        })
        .finally(function () {
          btn.disabled = false;
          btn.textContent = "Subscribe";
        });
    });

    el.appendChild(wrapper);
  });
})();
