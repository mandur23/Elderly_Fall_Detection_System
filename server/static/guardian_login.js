"use strict";
const reason = new URLSearchParams(location.search).get("error");
const messages = {
  "1": "아이디 또는 비밀번호가 맞지 않아요.",
  locked: "여러 번 틀려서 5분 동안 로그인할 수 없어요. 잠시 후 다시 해 주세요.",
};
if (reason && messages[reason]) {
  const el = document.getElementById("error");
  el.textContent = messages[reason];
  el.classList.remove("hidden");
}
