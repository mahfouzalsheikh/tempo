const form = document.getElementById("login-form");
const username = document.getElementById("login-username");
const password = document.getElementById("login-password");
const next = document.getElementById("login-next");
const error = document.getElementById("login-error");
const submit = document.getElementById("login-submit");
const csrfToken = document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";

form.addEventListener("submit", async event => {
  event.preventDefault();
  error.hidden = true;
  submit.disabled = true;
  submit.textContent = "Signing in…";
  try {
    const response = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      credentials: "same-origin",
      body: JSON.stringify({
        username: username.value.trim(),
        password: password.value,
        next: next.value,
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.message || "Sign-in failed.");
    }
    window.location.assign(payload.redirect || "/");
  } catch (loginError) {
    error.textContent = loginError.message;
    error.hidden = false;
    password.select();
  } finally {
    submit.disabled = false;
    submit.textContent = "Sign in";
  }
});
