// Push-уведомления на сайте: WebSocket ← Redis pub/sub.
(function () {
  const badge = document.getElementById("notif-badge");
  if (!badge) return; // гость

  const toasts = document.createElement("div");
  toasts.className = "toasts";
  document.body.appendChild(toasts);

  function toast(data) {
    const el = document.createElement("div");
    el.className = "toast";
    const title = document.createElement("b");
    title.textContent = data.title;
    const text = document.createElement("div");
    text.textContent = data.text;
    el.append(title, text);
    if (data.link) {
      const a = document.createElement("a");
      a.href = data.link;
      a.textContent = "Открыть";
      el.append(a);
    }
    toasts.appendChild(el);
    setTimeout(() => el.remove(), 8000);
  }

  function bump() {
    const n = (parseInt(badge.textContent, 10) || 0) + 1;
    badge.textContent = n;
    badge.hidden = false;
  }

  function browserPush(data) {
    if (!("Notification" in window) || Notification.permission !== "granted" || !document.hidden) return;
    new Notification("ProSpace: " + data.title, { body: data.text, icon: "/static/img/mark.svg" });
  }

  if ("Notification" in window && Notification.permission === "default") {
    document.addEventListener("click", () => Notification.requestPermission(), { once: true });
  }

  let delay = 1000;
  function connect() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/notifications");
    const ping = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000);
    ws.onopen = () => (delay = 1000);
    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type !== "notification") return;
      bump();
      toast(data);
      browserPush(data);
    };
    ws.onclose = (e) => {
      clearInterval(ping);
      if (e.code === 4401) return;
      setTimeout(connect, delay);
      delay = Math.min(delay * 2, 30000);
    };
  }
  connect();
})();

// Индикатор сложности пароля
document.querySelectorAll("[data-pw]").forEach((input) => {
  const list = document.getElementById(input.dataset.pw);
  if (!list) return;
  const rules = {
    len: (v) => v.length >= 8,
    lower: (v) => /[a-zа-яё]/.test(v),
    upper: (v) => /[A-ZА-ЯЁ]/.test(v),
    digit: (v) => /\d/.test(v),
    special: (v) => /[^\w\s]|_/.test(v),
  };
  input.addEventListener("input", () => {
    list.querySelectorAll("li").forEach((li) => li.classList.toggle("ok", rules[li.dataset.rule](input.value)));
  });
});

// Счётчик символов
document.querySelectorAll("[data-counter]").forEach((el) => {
  const out = document.getElementById(el.dataset.counter);
  const upd = () => (out.textContent = `${el.value.length} / ${el.maxLength}`);
  el.addEventListener("input", upd);
  upd();
});
