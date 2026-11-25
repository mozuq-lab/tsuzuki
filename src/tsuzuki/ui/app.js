// Tsuzuki の画面の動き。サーバーが作ってエスケープした HTML の断片を取りに行ってペインに入れ、
// クリック（data-* で振り分ける）・キー操作・URL の # の状態を扱い、操作を POST する。
// HTML を組み立てるのはサーバーだけ。ここで作る要素の文字は textContent で入れる。
// Claude に渡す @ の形もサーバーが作る（data-copy）。ここでは組み立てず、つなぐだけ。
"use strict";

// qdays・role・order は検索の期間・発言者・並び順（一覧の期間 days とは別。検索の既定は、すべて・新しい順）。
// task と status はタスクの表示の、選んだタスク（フォルダー名）と状況の絞り込み（t は会話のターンに使っている）
const HASH_KEYS = ["v", "p", "s", "q", "i", "t", "c", "d", "k", "days", "h", "qdays", "role", "order", "task", "status"];
const VIEWS = ["tasks", "sessions", "docs"];
const STATUSES = ["", "done", "all"]; // 状況の絞り込み。"" は進行中と待ち（既定）
const NO_PLACE = "0"; // タスクの表示の「場所なし」（台帳の場所の ID は 1 から）
const NEW_TASK_URL = "/frag/new-task";
const DAY_CHOICES = ["7", "30", "90", "all"];
const DEFAULT_DAYS = "30";
const ROLES = ["user", "assistant"];
const SEARCH_DELAY_MS = 250;
const POLL_MS = 5000;
const TOAST_MS = 4000;
// タスクに書く操作は、台帳の更新の間はその終わりを待つ（初めての更新で1分ほど）。答えがこれより遅ければ知らせる
const WRITE_WAIT_MS = 1500;
const WRITE_WAIT_TEXT = "更新の終わりを待っています…";
const WRITE_BUSY_TEXT = "前の操作の終わりを待っています";
const RELOAD_TEXT = "画面を読み込み直してください";
const PLACEHOLDER_SESSIONS = "セッションと資料を検索（/ で移動）";
const PLACEHOLDER_DOCS = "資料を検索（/ で移動）";
const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const NUMBER = /^[0-9]{1,18}$/;
// Mermaid の図。枠のページは同梱の Mermaid の版を付けた URL で開く（サーバーの
// MERMAID_FRAME_URL。その URL のページはブラウザが1日覚えるので、図ごとに取り直さない）
const MERMAID_URL = "/mermaid-frame?v=11.17.2";
const MERMAID_MAX_HEIGHT = 2000; // 枠の高さの上限（px）。これより高い図は、枠の中を動かして見る
const MERMAID_MAX_ERROR = 300; // 出す失敗の文の上限（字）

// 表示の切り替え（自動 → ライト → ダーク → 自動）。自動のときは data-theme を付けず、localStorage のキーも持たない。
// localStorage が使えなくても（読み書きは try の中）、自動のまま動く
// アプリ（Tsuzuki.app）は起動のたびにポートが変わり localStorage が引き継がれないので、選んだ値をアプリにも送る
const THEME_KEY = "tsuzuki-theme";
const THEME_TEXT = { "": "表示：自動", light: "表示：ライト", dark: "表示：ダーク" };
const THEME_NEXT = { "": "light", light: "dark", dark: "" };
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
let themeChoice = "";
try {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved === "light" || saved === "dark") themeChoice = saved;
} catch (error) {
  themeChoice = "";
}
let shownTheme = null;

// 今の表示。"light" か "dark"（自動なら OS の設定で決める）
function effectiveTheme() {
  if (themeChoice) return themeChoice;
  return darkQuery.matches ? "dark" : "light";
}

function applyTheme() {
  const root = document.documentElement;
  if (themeChoice) root.setAttribute("data-theme", themeChoice);
  else root.removeAttribute("data-theme");
  const button = document.getElementById("theme");
  if (button) button.textContent = THEME_TEXT[themeChoice];
  const now = effectiveTheme();
  if (now !== shownTheme) {
    const first = shownTheme === null;
    shownTheme = now;
    if (!first) document.dispatchEvent(new CustomEvent("tsuzuki-theme", { detail: { theme: now } }));
  }
}

function cycleTheme() {
  themeChoice = THEME_NEXT[themeChoice];
  try {
    if (themeChoice) localStorage.setItem(THEME_KEY, themeChoice);
    else localStorage.removeItem(THEME_KEY);
  } catch (error) {
    // 保存できなくても、この画面の間は切り替わる
  }
  try {
    window.webkit.messageHandlers.tsuzukiTheme.postMessage(themeChoice);
  } catch (error) {
    /* ブラウザでは何もしない（アプリのときだけ、選んだ表示をアプリに覚えさせる） */
  }
  applyTheme();
}

applyTheme(); // できるだけ早く（ちらつきを抑える）
if (darkQuery.addEventListener) darkQuery.addEventListener("change", applyTheme);
else darkQuery.addListener(applyTheme);

const CSRF = metaContent("tsuzuki-csrf");
const RESUME_DEFAULT = metaContent("tsuzuki-resume-default") || "vscode";
const HEADLESS_DEFAULT = metaContent("tsuzuki-headless-in-list") === "1" ? "1" : "0";
// # に表示がないときの表示（タスクが1つ以上あればタスク、なければセッション）
const DEFAULT_VIEW = metaContent("tsuzuki-has-tasks") === "1" ? "tasks" : "sessions";

const ui = {
  q: byId("q"),
  qClear: byId("q-clear"),
  status: byId("status"),
  counts: byId("counts"),
  banner: byId("banner"),
  paneLeft: byId("pane-left"),
  left: byId("left"),
  list: byId("pane-list"),
  detail: byId("pane-detail"),
  toast: byId("toast"),
  confirm: byId("confirm"),
  placeSelect: byId("place-select"),
  kindSelect: byId("kind-select"),
  statusSelect: byId("status-select"),
  optsSlot: byId("opts-slot"),
  listOpts: byId("list-opts"),
  headless: byId("opt-headless"),
  days: byId("opt-days"),
  qdays: byId("opt-qdays"),
  role: byId("opt-role"),
  order: byId("opt-order"),
};

// 各ペインに入っている断片。url が同じなら取り直さない（取りに行っている途中なら、その結果を待つ）
const panes = {
  left: { node: ui.left, url: null, html: null, promise: null, abort: null },
  list: { node: ui.list, url: null, html: null, promise: null, abort: null },
  detail: { node: ui.detail, url: null, html: null, promise: null, abort: null },
};

let state = readHash();
let renderCount = 0; // 新しい描画が始まったら、古い描画の続きはしない
let searchTimer = 0;
let toastTimer = 0;
let lastUpdate; // /api/status の last_update_at（変わったら一覧を取り直す）
let lastStatus = null;
let reading = false; // 会話を読むときに中央の列を畳んでいるか
let marks = []; // 会話の中の <mark>（検索語の印）
let markIndex = -1;
let revealedKey = ""; // 一覧で最後に見える所へ動かした選択
const expanded = new Set(); // 子の一覧を開いている親のセッション
let writing = false; // タスクに書く操作を送っている途中（続けて押しても、2つ目は送らない）
let writeSlow = false; // 送っている書く操作の答えが WRITE_WAIT_MS を過ぎても来ていない
let toastWaits = false; // 知らせに出ているのが、答えを待っている知らせ（時間で消さない）
let madeTask = null; // 作ったばかりのタスク { task, taskMd }。そのカードが出たら @task.md のコピーを目立たせる
let mermaidFrames = []; // 右のペインの Mermaid の枠 { frame, block, code, loaded }

// 状態と URL の #

function readHash() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  const next = {};
  for (const key of HASH_KEYS) next[key] = params.get(key) || "";
  return normalize(next);
}

function normalize(raw) {
  const st = Object.assign({}, raw);
  for (const key of ["p", "d", "i", "t"]) {
    if (!NUMBER.test(st[key])) st[key] = "";
  }
  if (!SESSION_ID.test(st.s)) st.s = "";
  st.q = st.q.trim();
  st.task = isTaskName(st.task) ? st.task : "";
  // # に表示がなければ既定の表示。ただし v を書かない # （選んだセッション・資料や検索の語、一覧の
  // 期間・ヘッドレスを持つ）は、これまでどおりセッションの表示で開く。days と h は既定の値を入れる前に見る
  if (!VIEWS.includes(st.v)) st.v = !st.task && (st.s || st.d || st.q || st.days || st.h) ? "sessions" : DEFAULT_VIEW;
  // タスクの表示には検索がない。検索の語があればセッションの表示で探す
  if (st.v === "tasks" && st.q) st.v = "sessions";
  if (st.v === "tasks") {
    // タスクの表示ではセッションを選ばず、資料はカードから開いたときだけ（# を読み直しても同じ表示になる）
    st.s = "";
    if (!st.task) st.d = "";
  } else {
    st.task = "";
  }
  st.status = STATUSES.includes(st.status) ? st.status : "";
  if (st.v !== "tasks" && st.p === NO_PLACE) st.p = ""; // 「場所なし」はタスクの表示だけ
  st.c = st.c === "1" && st.s ? "1" : "";
  if (!st.c) st.t = "";
  if (!st.q) st.i = "";
  st.days = DAY_CHOICES.includes(st.days) ? st.days : DEFAULT_DAYS;
  st.h = st.h === "1" || st.h === "0" ? st.h : HEADLESS_DEFAULT;
  st.qdays = DAY_CHOICES.includes(st.qdays) ? st.qdays : "all";
  st.role = ROLES.includes(st.role) ? st.role : "";
  st.order = st.order === "count" ? "count" : "recent";
  return st;
}

function toHash(st) {
  const params = new URLSearchParams();
  for (const key of HASH_KEYS) {
    const value = st[key];
    const isDefault = (key === "v" && value === DEFAULT_VIEW) || (key === "days" && value === DEFAULT_DAYS)
      || (key === "h" && value === HEADLESS_DEFAULT) || (key === "qdays" && value === "all")
      || (key === "order" && value === "recent");
    if (value && !isDefault) params.set(key, value);
  }
  const text = params.toString();
  return text ? "#" + text : location.pathname + location.search;
}

// 状態を変える。push はほかの表示へ移るとき（戻る操作で戻れる）、それ以外はペインの中の移動として置き換える
function go(changes, push) {
  const next = normalize(Object.assign({}, state, changes));
  const url = toHash(next);
  if (url !== toHash(state)) {
    if (push) history.pushState(null, "", url);
    else history.replaceState(null, "", url);
  }
  state = next;
  render();
}

// changes で状態が変わるか（go と同じく normalize してから比べる）
function changesState(changes) {
  return toHash(normalize(Object.assign({}, state, changes))) !== toHash(state);
}

function fromHistory() {
  const next = readHash();
  if (toHash(next) === toHash(state)) return;
  state = next;
  ui.q.value = state.q;
  render();
}

// 描画：中央 → 左（検索のときは、サーバーが覚えた同じ検索の結果を使えるように中央の後）→ 右

async function render() {
  const count = ++renderCount;
  const st = state;
  syncChrome(st);
  await load("list", listUrl(st));
  if (count !== renderCount) return;
  markSelection("changed");
  load("left", leftUrl(st)).then(syncSelects);
  const url = detailUrl(st);
  if (url === null) {
    // 一覧の最初の行を選ぶ。選んでも状態が変わらないなら選ばない（同じ描画をくり返して画面が止まるため）
    const row = selectableRows()[0];
    const changes = row ? rowChanges(row) : null;
    if (changes && changesState(changes)) go(changes, false);
    else clearDetail();
    return;
  }
  await load("detail", url);
}

function syncChrome(st) {
  document.body.classList.toggle("docs", st.v === "docs");
  document.body.classList.toggle("tasks", st.v === "tasks");
  document.body.classList.toggle("searching", Boolean(st.q));
  for (const button of document.querySelectorAll("[data-view]")) {
    if (button.dataset.view === st.v) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  ui.q.placeholder = st.v === "docs" ? PLACEHOLDER_DOCS : PLACEHOLDER_SESSIONS;
  if (document.activeElement !== ui.q && ui.q.value.trim() !== st.q) ui.q.value = st.q;
  syncClear();
  ui.headless.checked = st.h === "1";
  ui.days.value = st.days;
  ui.qdays.value = st.qdays;
  ui.role.value = st.role;
  ui.order.value = st.order;
  showCounts();
}

function query(values) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== "" && value !== undefined && value !== null) params.set(key, value);
  }
  const text = params.toString();
  return text ? "?" + text : "";
}

function searchTargets(st) {
  // 「ヘッドレスも並べる」なら、検索の対象にもヘッドレスを足す（空なら既定：会話・資料・入力履歴）
  return st.h === "1" ? "conversation,documents,history,headless" : "";
}

// 検索の語と絞り込み。一覧（/frag/search）と左の場所の数（/frag/places）に同じものを送る（サーバーは同じ検索を
// 1回で済ませ、場所ごとの数も一覧と同じ絞り込みになる）。既定の値は送らない
function searchParams(st) {
  return {
    q: st.q,
    place: st.p,
    targets: searchTargets(st),
    days: st.qdays === "all" ? "" : st.qdays,
    role: st.role,
    order: st.order === "recent" ? "" : st.order,
  };
}

// タスクの一覧には選んだタスクを送らない（行を選ぶたびに一覧を取り直さない。印は markSelection が付ける）
function listUrl(st) {
  if (st.v === "tasks") return "/frag/tasks" + query({ status: st.status, place: st.p });
  if (st.v === "docs") return "/frag/docs" + query({ kind: st.k, place: st.p, q: st.q });
  if (st.q) return "/frag/search" + query(searchParams(st));
  return "/frag/list" + query({ place: st.p, days: st.days, headless: st.h });
}

function leftUrl(st) {
  if (st.v === "tasks") return "/frag/task-filters" + query({ status: st.status, place: st.p });
  if (st.v === "docs") return "/frag/docs-filters" + query({ kind: st.k, place: st.p });
  if (st.q) return "/frag/places" + query(searchParams(st));
  return "/frag/places" + query({ place: st.p });
}

// 右のペインの断片。null なら、一覧の最初の行を選ぶ。タスクのカードの資料の題（data-doc）を押したときは、
// タスクの表示のまま資料を出す（戻る操作でカードに戻る）
function detailUrl(st) {
  if (st.d && st.v !== "tasks") {
    // 資料の一覧と検索の task.md の行（data-doc と data-task を持つ）は、資料ではなくタスクのカードを今の表示のまま出す
    const row = findRow("doc", st.d);
    if (row && row.dataset.task && isTaskName(row.dataset.task)) return "/frag/task/" + encodeURIComponent(row.dataset.task);
  }
  if (st.d) return "/frag/doc/" + st.d + (st.v === "tasks" ? "" : query({ q: st.q })); // 検索の語は資料の中にも印を付ける
  if (st.v === "tasks") return st.task ? "/frag/task/" + encodeURIComponent(st.task) : null;
  if (st.v === "docs") return null;
  if (st.s && st.c) {
    return "/frag/conversation/" + st.s + query({ turn: st.t, turns: rowTurns(st.s), q: st.q });
  }
  if (st.s) {
    const turns = rowTurns(st.s);
    return "/frag/card/" + st.s + (turns ? query({ turns: turns, i: st.i, q: st.q }) : "");
  }
  if (st.p && !st.q) return "/frag/place/" + st.p;
  return null;
}

// 断片を取りに行き、ペインに入れる。force なら同じ URL でも取り直す（中身が変わっていなければ入れ替えない）。
// 入れ替えたら、そのペインの後始末（afterSwap）を行う。
function load(name, url, force) {
  const pane = panes[name];
  if (!force && pane.url === url && pane.promise) return pane.promise;
  if (pane.abort) pane.abort.abort();
  const abort = new AbortController();
  pane.abort = abort;
  pane.url = url;
  pane.promise = (async () => {
    try {
      const res = await fetch(url, { signal: abort.signal });
      const text = await res.text();
      if (pane.abort !== abort) return false;
      if (!res.ok) {
        failPane(name, failureText(res.status, text));
        return false;
      }
      if (force && text === pane.html) return true;
      pane.html = text;
      pane.node.innerHTML = text; // 断片はサーバーがエスケープ済み
      afterSwap[name](Boolean(force));
      return true;
    } catch (error) {
      if (error.name !== "AbortError" && pane.abort === abort) failPane(name, "サーバーにつながりません");
      return false;
    } finally {
      if (pane.abort === abort) pane.abort = null;
    }
  })();
  return pane.promise;
}

function failPane(name, message) {
  const pane = panes[name];
  pane.url = null; // 次の描画で取り直す
  pane.html = null;
  pane.node.textContent = "";
  pane.node.append(element("div", "empty", message));
  if (name === "detail") setupDetail();
}

function failureText(status, text) {
  const body = (text || "").trim().slice(0, 200);
  return (body || "読み込めませんでした") + "（" + status + "）";
}

const afterSwap = {
  list(refreshed) {
    if (refreshed) {
      reopenChildren();
    } else {
      expanded.clear();
      ui.list.scrollTop = 0;
    }
    markSelection(refreshed ? "" : "always");
  },
  left() {
    syncSelects();
  },
  detail(refreshed) {
    if (!refreshed) ui.detail.scrollTop = 0; // 取り直したとき（タスクに書いたあと）は、見ていた所に留まる
    setupDetail();
    showMadeTask();
  },
};

function clearDetail() {
  const pane = panes.detail;
  if (pane.abort) pane.abort.abort();
  pane.url = "";
  pane.html = "";
  pane.promise = Promise.resolve(true);
  ui.detail.textContent = "";
  setupDetail();
}

// 一覧の選択

// ID が URL の # に残せる形の行だけを選べる。台帳のセッション ID は UUID とは限らず、
// 形の違う ID は normalize で消えるので、選んでも最初の行に戻ってしまう。タスクの表示ではタスクの行（フォルダー名）
function isSelectable(row) {
  if (state.v === "tasks") return isTaskName(row.dataset.task || "");
  return row.dataset.session ? SESSION_ID.test(row.dataset.session) : NUMBER.test(row.dataset.doc || "");
}

function selectableRows() {
  // 入力履歴の行（.row.hist）は開く先がないので選ばない
  const selector = state.v === "tasks" ? ".row[data-task]" : ".row[data-session], .row[data-doc]";
  return Array.from(ui.list.querySelectorAll(selector)).filter(isSelectable);
}

function findRow(attribute, value) {
  return selectableRows().find((row) => row.dataset[attribute] === value) || null;
}

function rowTurns(sessionId) {
  const row = findRow("session", sessionId);
  return row && row.dataset.turns ? row.dataset.turns : "";
}

// 選んだ行に印を付ける。reveal が "always" なら必ず、"changed" なら選んだ物が変わったときだけ見える所へ動かす。
// タスクの表示では選んだタスクの行（カードから資料を開いていても、印はタスクに残す）
function markSelection(reveal) {
  for (const row of ui.list.querySelectorAll(".row.on")) row.classList.remove("on");
  const tasks = state.v === "tasks";
  const key = tasks ? "task:" + state.task : state.s || state.d;
  const row = tasks ? (state.task ? findRow("task", state.task) : null)
    : state.s ? findRow("session", state.s) : state.d ? findRow("doc", state.d) : null;
  if (row) {
    row.classList.add("on");
    if (reveal === "always" || (reveal === "changed" && key !== revealedKey)) row.scrollIntoView({ block: "nearest" });
  }
  revealedKey = key;
}

// 行を選んだときの状態の変化。選べない行なら null
function rowChanges(row) {
  if (!isSelectable(row)) return null;
  if (state.v === "tasks") return { task: row.dataset.task, d: "" };
  if (row.dataset.session) return { s: row.dataset.session, d: "", c: "", t: "", i: "" };
  return state.v === "docs" ? { d: row.dataset.doc } : { d: row.dataset.doc, s: "", c: "", t: "", i: "" };
}

function selectRow(row, push) {
  const changes = rowChanges(row);
  if (changes) go(changes, push);
}

function moveSelection(step) {
  const rows = selectableRows();
  if (!rows.length) return;
  const current = ui.list.querySelector(".row.on");
  const index = rows.indexOf(current);
  const next = index < 0 ? (step > 0 ? 0 : rows.length - 1) : Math.min(Math.max(index + step, 0), rows.length - 1);
  if (rows[next] === current) return;
  rows[next].scrollIntoView({ block: "nearest" });
  selectRow(rows[next], false);
}

// 操作（data-action）

const ACTIONS = {
  "resume": (button) => resumeSession(cardSession(button), button.dataset.how, false),
  "copy": (button) => copyText(button.dataset.copy),
  "conversation": (button) => go({ s: button.dataset.session, d: "", c: "1", t: button.dataset.turn || "" }, true),
  "card": (button) => openCard(button.dataset.session),
  "hit": (button) => moveHit(button),
  "children": (button) => toggleRows(button, "children-" + button.dataset.session,
    "/frag/children/" + encodeURIComponent(button.dataset.session)),
  "orphans": (button) => toggleRows(button, "orphans-" + button.dataset.place,
    "/frag/orphans/" + encodeURIComponent(button.dataset.place)),
  "open-doc": (button) => act("/api/open", { doc_id: Number(button.dataset.doc), how: button.dataset.how }),
  "open-place": (button) => act("/api/open-place", { place_id: Number(button.dataset.place), how: button.dataset.how }),
  "docs": (button) => go({ v: "docs", p: button.dataset.place || "", k: "", d: "", q: "", s: "", c: "", t: "", i: "" },
    true),
  "fold": () => setReading(!reading),
  "read-more": (button) => readMore(button),
  // タスク
  "task-stage": (button) => writeTask("/api/task-stage", button, { stage: button.dataset.stage }),
  "task-waiting": (button) => openWaitingForm(button),
  "task-settings": (button) => openSettings(button),
  "form-cancel": (button) => cancelForm(button),
  "task-unlink": (button) => unlinkSession(button),
  "task-conversation": (button) => startConversation(button),
  "task-trash": (button) => trashTask(button),
  "copy-picked": (button) => copyPicked(button),
  "new-task": () => openNewTask(),
  // セッションのカードと検索・資料の行から
  "task-open": (button) => openTask(button.dataset.task),
  "task-link": (button) => linkToTask(button),
};

// 再開するセッション：ボタンの data-session（タスクのカードの「続きから」）、なければ状況カードのセッション
function cardSession(button) {
  if (button.dataset.session) return button.dataset.session;
  const card = button.closest(".card");
  return card ? card.dataset.session : state.s;
}

function onClick(event) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
  const target = event.target;
  if (!(target instanceof Element) || target.closest("#toast, #confirm, a[href]")) return;
  const action = target.closest("[data-action]");
  if (action) {
    const run = ACTIONS[action.dataset.action];
    if (run && !action.disabled) run(action);
    return;
  }
  const view = target.closest("[data-view]");
  if (view) {
    switchView(view.dataset.view);
    return;
  }
  const picked = target.closest("[data-session], [data-doc], [data-place], [data-kind], [data-status], [data-task]");
  // 状況カードとタスクのカードの外枠は選ばない（カードの中の選ぶ欄を押したときなど）
  if (!picked || picked.classList.contains("card") || picked.classList.contains("task-card")) return;
  if (ui.paneLeft.contains(picked)) {
    if ("kind" in picked.dataset) go({ k: picked.dataset.kind, d: "" }, true);
    else if ("status" in picked.dataset) chooseStatus(picked.dataset.status);
    else if ("place" in picked.dataset) choosePlace(picked.dataset.place);
  } else if (ui.list.contains(picked)) {
    selectRow(picked, false);
  } else if (ui.detail.contains(picked)) {
    openFromDetail(picked);
  }
}

// 右のペインの中のつながり・時系列・子・資料の行から移る（戻る操作で戻れる）
function openFromDetail(picked) {
  if (picked.dataset.session) {
    const changes = { s: picked.dataset.session, d: "", c: "", t: "", i: "" };
    if (state.v !== "sessions") Object.assign(changes, { v: "sessions", p: "", q: "" });
    go(changes, true);
  } else if (picked.dataset.doc) {
    go({ d: picked.dataset.doc, c: "" }, true);
  }
}

// タスクのカードを開く（戻る操作で戻れる）。どの状況のタスクも一覧に出るよう、状況の絞り込みは「すべて」にする
function openTask(name) {
  if (!isTaskName(name || "")) return;
  go({ v: "tasks", task: name, status: "all", p: "", s: "", c: "", d: "", q: "", t: "", i: "" }, true);
}

// 「タスクに足す」（セッションのカードと会話の画面の見出し）：選んだタスクにこのセッションを足す（確定の結び付け）。
// カードは取り直す。会話は読んでいる所を保つため取り直さず、足したタスクを選択肢から外す（なくなれば欄ごと外す）
async function linkToTask(button) {
  const box = button.closest(".taskadd");
  const select = box && box.querySelector("select.pick-task");
  if (!select || !select.value || busyWriting()) return;
  const task = select.value;
  writing = true;
  try {
    const answer = await postWrite("/api/task-link", { task: task, session_id: button.dataset.session,
      action: "add" });
    if (!answer) return;
    toast(answer.message);
    if (box.closest(".conv")) {
      if (!answer.ok) return;
      const option = Array.from(select.options).find((item) => item.value === task);
      if (option) option.remove();
      if (!select.options.length) box.remove();
      return;
    }
    const url = detailUrl(state);
    if (url !== null) await load("detail", url, true);
  } finally {
    writing = false;
  }
}

// セッションのカードを開く（会話の「状況カードに戻る」と、タスクのカードの会話の行）。タスクの表示からは、
// セッションの表示へ移る（戻る操作で戻れる）
function openCard(sessionId) {
  const changes = { s: sessionId, d: "", c: "", t: "" };
  if (state.v !== "sessions") Object.assign(changes, { v: "sessions", p: "", q: "", i: "" });
  go(changes, true);
}

function choosePlace(place) {
  if (state.v === "docs") go({ p: place, d: "" }, true);
  else if (state.v === "tasks") go({ p: place, task: "", d: "" }, true);
  else go({ p: place, s: "", d: "", c: "", t: "", i: "" }, true);
}

// タスクの状況（進行中と待ち・完了・すべて）を選ぶ。選び直した一覧の最初の行を選ぶ
function chooseStatus(status) {
  go({ status: status, task: "", d: "" }, true);
}

function switchView(view) {
  if (view === state.v) return;
  const changes = { v: view, s: "", d: "", c: "", t: "", i: "" };
  // タスクの表示との行き来では、場所の絞り込みと検索の語を持ち越さない（タスクの場所と「場所なし」は
  // タスクの表示だけのもの。タスクの表示には検索がない）
  if (view === "tasks" || state.v === "tasks") Object.assign(changes, { p: "", q: "" });
  go(changes, true);
}

// 子と、親が分からないヘッドレスの一覧（開く・閉じる・さらに表示）
async function toggleRows(button, boxId, base) {
  const box = document.getElementById(boxId);
  if (!box) return;
  if (button.dataset.offset) {
    const text = await fetchText(base + query({ offset: button.dataset.offset }));
    if (text === null) return;
    button.remove();
    box.insertAdjacentHTML("beforeend", text);
    markSelection("");
    return;
  }
  const parent = button.dataset.session;
  if (box.childElementCount) {
    box.textContent = "";
    button.setAttribute("aria-expanded", "false");
    if (parent) expanded.delete(parent);
    return;
  }
  const text = await fetchText(base);
  if (text === null) return;
  box.innerHTML = text;
  button.setAttribute("aria-expanded", "true");
  if (parent) expanded.add(parent);
  markSelection("");
}

// 一覧を取り直したあと、開いていた子の一覧を開き直す
async function reopenChildren() {
  for (const parent of Array.from(expanded)) {
    const box = document.getElementById("children-" + parent);
    const button = Array.from(ui.list.querySelectorAll(".kids")).find((b) => b.dataset.session === parent);
    if (!box || !button) {
      expanded.delete(parent);
      continue;
    }
    const text = await fetchText("/frag/children/" + encodeURIComponent(parent));
    if (text === null) continue;
    box.innerHTML = text;
    button.setAttribute("aria-expanded", "true");
  }
  markSelection("");
}

// 一致の移動。状況カードでは一致の番号を変えてカードを取り直し、会話では <mark> の間を移る
function moveHit(button) {
  const step = button.dataset.dir === "prev" ? -1 : 1;
  const card = button.closest(".card");
  if (card) {
    const total = (card.dataset.turns || "").split(",").filter(Boolean).length;
    const index = Math.min(Math.max(Number(card.dataset.index || 0) + step, 0), Math.max(total - 1, 0));
    go({ i: index ? String(index) : "" }, false);
  } else if (marks.length) {
    showMark(Math.min(Math.max(markIndex + step, 0), marks.length - 1), true);
  }
}

// 長い会話の前後を読み足す。ボタンの所に隣の窓を入れる。前に足すときは、読んでいた発言が
// 画面の同じ高さに残るようにする（ブラウザが自分で保っていれば、ずれは 0 なので動かさない）
async function readMore(button) {
  const params = { q: state.q };
  if (button.dataset.offset) params.offset = button.dataset.offset;
  else params[button.dataset.dir === "before" ? "before" : "after"] = button.dataset.turn;
  button.disabled = true;
  const text = await fetchText("/frag/conversation/" + encodeURIComponent(button.dataset.session) + query(params));
  if (!button.isConnected) return; // 待つ間にほかの表示へ移った
  if (text === null) {
    button.disabled = false;
    return;
  }
  const anchor = button.dataset.dir === "before" ? button.nextElementSibling : null;
  const top = anchor ? anchor.getBoundingClientRect().top : 0;
  button.insertAdjacentHTML("beforebegin", text); // 断片はサーバーがエスケープ済み
  button.remove();
  if (anchor) ui.detail.scrollTop += anchor.getBoundingClientRect().top - top;
  refreshMarks();
}

// 会話に読み足したあと、一致の印を数え直す（今の印はそのまま。まだなければ最初の印。スクロールはしない）
function refreshMarks() {
  const conv = ui.detail.querySelector(".conv");
  if (!conv) return;
  const current = marks[markIndex] || null;
  marks = Array.from(conv.querySelectorAll("mark"));
  const counter = conv.querySelector(".hits");
  if (!counter || !marks.length) return;
  counter.hidden = false;
  const index = current ? marks.indexOf(current) : -1;
  markIndex = index;
  showMark(index < 0 ? 0 : index, false);
}

// 右のペインを入れ替えたあと：資料の Mermaid の図を描き始める。会話なら、目的のターンへ移り、一致の数を出す
function setupDetail() {
  setupMermaid();
  marks = [];
  markIndex = -1;
  for (const toggle of ui.detail.querySelectorAll('.more[data-action="orphans"]:not([data-offset])')) {
    toggle.setAttribute("aria-expanded", "false");
  }
  const conv = ui.detail.querySelector(".conv");
  if (!conv) {
    setReading(false);
    const docMark = ui.detail.querySelector(".doc-view .md mark"); // 資料の最初の印へ送る（印がなければ送らない）
    if (docMark) docMark.scrollIntoView({ block: "center" });
    return;
  }
  setReading(reading);
  const anchor = state.t ? document.getElementById("turn-" + state.t) : null;
  if (anchor) anchor.scrollIntoView({ block: "start" });
  else ui.detail.scrollTop = ui.detail.scrollHeight; // ターンの指定がなければ、最後の発言から読む
  const counter = conv.querySelector(".hits");
  const nav = conv.querySelectorAll('[data-action="hit"]');
  if (!counter && !nav.length) return;
  marks = Array.from(conv.querySelectorAll("mark"));
  if (!marks.length) {
    if (counter) counter.hidden = true;
    for (const button of nav) button.disabled = true;
    return;
  }
  const following = anchor
    ? marks.findIndex((mark) => anchor.compareDocumentPosition(mark) & Node.DOCUMENT_POSITION_FOLLOWING)
    : 0;
  showMark(following < 0 ? marks.length - 1 : following, false);
}

function showMark(index, scroll) {
  if (marks[markIndex]) marks[markIndex].classList.remove("cur");
  markIndex = index;
  const mark = marks[index];
  mark.classList.add("cur");
  const details = mark.closest("details");
  if (details) details.open = true;
  if (scroll) mark.scrollIntoView({ block: "center" });
  const conv = ui.detail.querySelector(".conv");
  const counter = conv && conv.querySelector(".hits");
  if (counter) counter.textContent = "一致 " + (index + 1) + "/" + marks.length;
  for (const button of conv ? conv.querySelectorAll('[data-action="hit"]') : []) {
    button.disabled = button.dataset.dir === "prev" ? index <= 0 : index >= marks.length - 1;
  }
}

// 「中央を畳む」：会話を読むときだけ中央の列を隠す
function setReading(on) {
  const conv = ui.detail.querySelector(".conv");
  reading = Boolean(on && conv);
  document.body.classList.toggle("reading", reading);
  const fold = conv && conv.querySelector('[data-action="fold"]');
  if (fold) {
    fold.textContent = reading ? "中央を戻す" : "中央を畳む";
    fold.setAttribute("aria-pressed", reading ? "true" : "false");
  }
}

// Mermaid の図。資料の画面の Markdown の図（サーバーが mermaid-block にしたもの）ごとに、
// 隔離した枠（sandbox allow-scripts。起源は不透明で、この画面にもクッキーにも触れない）をコードの上に作り、
// 読み込めたら図のコードと今の表示（ライト・ダーク）を postMessage で渡す。枠の起源は "null" なので宛先は "*"。
// 返事は、自分の作った枠の contentWindow から来たものだけを受け取る。描けたら枠の高さを合わせてコードを隠し、
// 描けなければコードを出したまま、その下に失敗の文を1行添える。スクリプトが動かなければ、コードのまま読める

function setupMermaid() {
  mermaidFrames = []; // 前の資料の枠は、ペインを入れ替えたときに消えている
  for (const block of ui.detail.querySelectorAll(".doc-view .md .mermaid-block")) {
    const source = block.querySelector(".mermaid-src code");
    const code = source ? source.textContent : "";
    if (!code.trim()) continue;
    const frame = document.createElement("iframe");
    frame.className = "mermaid-frame";
    frame.setAttribute("sandbox", "allow-scripts"); // 読み込む前に付ける
    frame.title = "Mermaid の図";
    const entry = { frame: frame, block: block, code: code, loaded: false };
    frame.addEventListener("load", () => {
      // 2回目の読み込みは、枠の中が別のページに移ったということ。もう図のコードは送らず、コードに戻す
      if (entry.loaded) {
        dropMermaid(entry);
        return;
      }
      entry.loaded = true;
      sendMermaid(entry);
    });
    frame.src = MERMAID_URL;
    block.prepend(frame);
    mermaidFrames.push(entry);
  }
}

function sendMermaid(entry) {
  const target = entry.frame.contentWindow;
  if (!entry.loaded || !target) return;
  target.postMessage({ type: "tsuzuki-mermaid", code: entry.code, theme: effectiveTheme() }, "*");
}

function receiveMermaid(event) {
  if (!event.source || event.origin !== "null") return;
  const entry = mermaidFrames.find((item) => item.frame.contentWindow === event.source);
  const data = event.data;
  if (!entry || !data || typeof data !== "object" || data.type !== "tsuzuki-mermaid-result") return;
  if (data.ok === true && typeof data.height === "number" && Number.isFinite(data.height) && data.height > 0) {
    showMermaid(entry, data.height);
  } else if (data.ok === false) {
    failMermaid(entry, typeof data.error === "string" ? data.error : "");
  }
}

// 枠の高さは height 属性で決める（style は使わない。描けるまでは app.css が高さを 0 にしている）
function showMermaid(entry, height) {
  entry.frame.setAttribute("height", String(Math.min(Math.ceil(height), MERMAID_MAX_HEIGHT)));
  entry.block.classList.add("drawn");
  const line = entry.block.querySelector(".mermaid-error");
  if (line) line.remove();
}

function failMermaid(entry, message) {
  entry.block.classList.remove("drawn");
  entry.frame.removeAttribute("height");
  let line = entry.block.querySelector(".mermaid-error");
  if (!line) {
    line = element("div", "mermaid-error", "");
    entry.block.append(line);
  }
  const text = message.replace(/\s+/g, " ").trim().slice(0, MERMAID_MAX_ERROR);
  line.textContent = "図を描けませんでした" + (text ? "：" + text : "");
}

function dropMermaid(entry) {
  mermaidFrames = mermaidFrames.filter((item) => item !== entry);
  entry.frame.remove();
  entry.block.classList.remove("drawn");
}

// 表示（ライト・ダーク）が変わったら、描いた図を今の表示で描き直す
function redrawMermaid() {
  for (const entry of mermaidFrames) sendMermaid(entry);
}

// 再開・コピー・外のアプリで開く（POST）

async function post(path, body) {
  let res;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Tsuzuki-CSRF": CSRF },
      body: JSON.stringify(body || {}),
    });
  } catch (error) {
    toast("サーバーにつながりません");
    return null;
  }
  const text = await res.text();
  if (res.status === 403 && text.includes(RELOAD_TEXT)) {
    // サーバーを起動し直すと CSRF の鍵が変わる
    toast(text, { reload: true });
    return null;
  }
  if (!res.ok) {
    toast(failureText(res.status, text));
    return null;
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    toast("応答を読めませんでした");
    return null;
  }
}

async function act(path, body) {
  const answer = await post(path, body);
  if (answer && answer.message) toast(answer.message);
  return answer;
}

async function resumeSession(sessionId, how, confirmed) {
  if (!sessionId) return;
  const answer = await post("/api/resume", { session_id: sessionId, how: how, confirm: Boolean(confirmed) });
  if (!answer) return;
  if (answer.needs_confirm && !confirmed) {
    askToContinue(answer.message, () => resumeSession(sessionId, how, true));
    return;
  }
  toast(answer.message);
}

async function copyText(text) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("コピーしました");
  } catch (error) {
    toast("コピーできませんでした。選択して ⌘C でコピーしてください", { copy: text });
  }
}

// ⌘Enter：選んだセッションを既定の方法（設定 resume_default）で再開する。タスクの表示では、カードの「続きから」
async function defaultResume() {
  if (state.v === "tasks") {
    resumeTask();
    return;
  }
  if (state.v !== "sessions" || !state.s) return;
  if (RESUME_DEFAULT !== "copy") {
    resumeSession(state.s, RESUME_DEFAULT, false);
    return;
  }
  let card = ui.detail.querySelector(".card");
  if (!card || card.dataset.session !== state.s) {
    // 会話を表示しているときなどは、状況カードの断片からコマンドを読む
    const text = await fetchText("/frag/card/" + state.s);
    if (text === null) return;
    const template = document.createElement("template");
    template.innerHTML = text;
    card = template.content.querySelector(".card");
  }
  const button = card && card.querySelector('[data-action="copy"]');
  if (!button || button.disabled || !button.dataset.copy) {
    toast("このセッションは再開できません");
    return;
  }
  copyText(button.dataset.copy);
}

// タスクの操作。カードの外枠の data-task がどのタスクかを示す

// 「続きから」はカードの頭のボタンの最初（既定の再開がコピーなら data-action が copy）。押せなければ理由を出す
function resumeTask() {
  const card = ui.detail.querySelector(".task-card");
  if (!card || card.dataset.task !== state.task) return;
  const button = card.querySelector('.dh [data-action="resume"], .dh [data-action="copy"]');
  if (!button) return;
  if (button.disabled) toast(button.title || "続きから開ける会話がありません");
  else button.click();
}

// 段階と待ち（task.md の stage・waiting の行）。カードを出したときの task.md の更新時刻と大きさを送る。
// data-mtime はサーバーが repr で書いた数なので、Number で読めば同じ float に戻る（その後に task.md が
// 書き換わっていれば、サーバーは書かずに知らせる）
function writeTask(path, button, fields) {
  const card = button.closest(".task-card");
  if (!card) return;
  sendTask(path, Object.assign({ task: card.dataset.task, mtime: Number(card.dataset.mtime),
    size: Number(card.dataset.size) }, fields));
}

// 「待ちにする」「待ちを解く」：待っていることの入力欄を出す（送るのは入力欄の「待ちにする」「待ちを解く」）
function openWaitingForm(button) {
  const card = button.closest(".task-card");
  const form = card ? card.querySelector(".waiting-form") : null;
  if (!form) return;
  form.hidden = false;
  const reason = form.elements.reason;
  if (reason) reason.focus();
}

// 「設定を変える」：題・チケット・リポジトリ・ワークスペースの入力欄を右に出す（# には残さない。
// 一覧の行を選べばカードに戻る）。入力欄は開いた時点の task.md の更新時刻と大きさを持つ
async function openSettings(button) {
  const card = button.closest(".task-card");
  if (!card) return;
  const task = card.dataset.task;
  if (!(await load("detail", "/frag/task-settings/" + encodeURIComponent(task)))) return;
  const title = ui.detail.querySelector('#task-settings [name="title"]');
  if (title) title.focus();
}

// 設定の入力欄の「保存」：入力欄の時刻と大きさを送る。書けたらカードを取り直して戻る。書けなかったときは
// 入力欄のまま、サーバーの文を知らせに出す
async function saveSettings(form) {
  if (busyWriting()) return;
  const data = new FormData(form);
  const field = (name) => String(data.get(name) || "");
  const submit = form.querySelector('[type="submit"]');
  writing = true;
  if (submit) submit.disabled = true;
  let answer;
  try {
    answer = await postWrite("/api/task-settings", { task: form.dataset.task, title: field("title"),
      ticket: field("ticket"), repo: field("repo"), workspace: field("workspace"),
      mtime: Number(form.dataset.mtime), size: Number(form.dataset.size) });
  } finally {
    writing = false;
    if (submit) submit.disabled = false;
  }
  if (!answer) return;
  toast(answer.message);
  if (!answer.ok) return;
  panes.detail.url = null; // 入力欄から、同じ URL のカードへ戻す
  await reloadTasks();
}

// 入力欄の「やめる」：設定の欄（#task-settings）は、カードを取り直して元に戻す。ほかの欄は閉じる
function cancelForm(button) {
  const form = button.closest("form");
  if (!form) return;
  if (form.id === "task-settings") load("detail", detailUrl(state), true);
  else form.hidden = true;
}

// 会話の行の「外す」：このセッションをタスクから外す（利用者が外した記録は、台帳を作り直しても残る）
function unlinkSession(button) {
  const card = button.closest(".task-card");
  if (!card) return;
  sendTask("/api/task-link", { task: card.dataset.task, session_id: button.dataset.session, action: "remove" });
}

// タスクに書く操作（段階・待ち・外す・足す・新しいタスク・新しい会話）は1つずつ送る。前の操作の答えを待って
// いる間に押されたら送らず、そう知らせる（前の知らせが答えを待っている知らせなら、これも答えまで出しておく）
function busyWriting() {
  if (!writing) return false;
  toast(WRITE_BUSY_TEXT, { wait: writeSlow });
  return true;
}

// タスクに書く操作を送る。サーバーは台帳の更新の終わりを待ってから書くので、答えが WRITE_WAIT_MS より遅ければ、
// 待っていると知らせる。その知らせは答えが来るまで出しておき、答えが来たら消す（呼ぶ側が答えの文を出す）
async function postWrite(path, body) {
  const timer = setTimeout(() => {
    writeSlow = true;
    toast(WRITE_WAIT_TEXT, { wait: true });
  }, WRITE_WAIT_MS);
  try {
    return await post(path, body);
  } finally {
    clearTimeout(timer);
    writeSlow = false;
    if (toastWaits) hideToast();
  }
}

// タスクに書く操作を送り、答えの文（書けなかった理由も）を知らせに出して、一覧・左の数・カードを取り直す。
// サーバーは書けなかったときもタスクを読み直すので、取り直したカードは今の task.md の時刻で送れる
async function sendTask(path, body) {
  if (busyWriting()) return;
  writing = true;
  try {
    const answer = await postWrite(path, body);
    if (!answer) return;
    toast(answer.message);
    await reloadTasks();
  } finally {
    writing = false;
  }
}

// タスクに書いたあと：一覧・左の数・右を取り直す（中身が同じなら入れ替えない）。チェックした資料はそのまま
async function reloadTasks() {
  const st = state;
  const listed = await load("list", listUrl(st), true);
  if (st !== state) return;
  if (listed) load("left", leftUrl(st), true);
  const url = detailUrl(st);
  if (url === null) return;
  const picked = new Set(Array.from(ui.detail.querySelectorAll("input.pick:checked"), (box) => box.dataset.copy));
  await load("detail", url, true);
  if (st !== state) return;
  for (const box of ui.detail.querySelectorAll("input.pick")) {
    if (!box.disabled && picked.has(box.dataset.copy)) box.checked = true;
  }
}

// 「選んだ資料の @ をまとめてコピー」：チェックした資料の data-copy（サーバーが作った @ の形）を、
// 空白で区切って1行にする
function copyPicked(button) {
  const card = button.closest(".task-card") || ui.detail;
  const refs = Array.from(card.querySelectorAll("input.pick:checked"), (box) => box.dataset.copy).filter(Boolean);
  if (refs.length) copyText(refs.join(" "));
  else toast("コピーする資料にチェックを入れてください");
}

// 「新しい会話」：task.md を添えて会話を始める。コピーなら、返ったコマンドをコピーする。続けて押しても、
// 答えを待つ間は送らない（会話を2つ始めない）
async function startConversation(button) {
  const card = button.closest(".task-card");
  if (!card) return;
  if (busyWriting()) return;
  const how = button.dataset.how;
  writing = true;
  let answer;
  try {
    answer = await postWrite("/api/task-conversation", { task: card.dataset.task, how: how });
  } finally {
    writing = false;
  }
  if (!answer) return;
  if (answer.ok && how === "copy" && answer.command) copyText(answer.command);
  else toast(answer.message);
}

// 「ゴミ箱に入れる」：確かめてから、タスクのフォルダーをゴミ箱へ動かす。動かせたら一覧に戻る
function trashTask(button) {
  const card = button.closest(".task-card");
  if (!card) return;
  if (busyWriting()) return;
  const task = card.dataset.task;
  askToContinue(button.dataset.confirm, async () => {
    if (busyWriting()) return;
    writing = true;
    let answer;
    try {
      answer = await postWrite("/api/task-trash", { task: task });
    } finally {
      writing = false;
    }
    if (!answer) return;
    toast(answer.message);
    if (!answer.ok) return;
    panes.list.url = panes.left.url = null; // 同じ URL でも取り直す
    go({ task: "" }, false);
  });
}

// 「＋ 新しいタスク」：右に入力欄を出す（# には残さない。一覧の行を選べばカードに戻る）
async function openNewTask() {
  if (!(await load("detail", NEW_TASK_URL))) return;
  const title = ui.detail.querySelector('#new-task [name="title"]');
  if (title) title.focus();
}

function onSubmit(event) {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (form.id === "new-task") {
    event.preventDefault(); // フォームそのものは送らない（ページを読み直さない）
    createTask(form);
  } else if (form.classList.contains("waiting-form")) {
    event.preventDefault();
    writeTask("/api/task-waiting", form, { waiting: form.dataset.waiting === "true", reason: form.elements.reason.value });
  } else if (form.id === "task-settings") {
    event.preventDefault();
    saveSettings(form);
  }
}

// 新しいタスクを作る（題と短い名前は入力欄の required で確かめ済み）。作れたらそのカードを開き、
// @task.md のコピーを目立たせる。同じ名前のフォルダーがあるときなどは、サーバーの文を知らせに出す
async function createTask(form) {
  if (busyWriting()) return;
  const data = new FormData(form);
  const field = (name) => String(data.get(name) || "");
  const submit = form.querySelector('[type="submit"]');
  writing = true;
  if (submit) submit.disabled = true;
  let answer;
  try {
    answer = await postWrite("/api/task-new", { title: field("title"), short_name: field("short_name"),
      ticket: field("ticket"), repo: field("repo") });
  } finally {
    writing = false;
    if (submit) submit.disabled = false;
  }
  if (!answer) return;
  toast(answer.message);
  if (!answer.ok) return;
  madeTask = { task: answer.task, taskMd: answer.task_md };
  // 一覧と左は同じ URL でも取り直す。作ったタスクは進行中なので、完了だけの絞り込みと場所の絞り込みは外す
  panes.list.url = null;
  panes.left.url = null;
  go({ v: "tasks", task: answer.task, status: state.status === "done" ? "" : state.status, p: "", d: "", q: "" },
    true);
}

// 作ったばかりのタスクのカードが出たら、「@task.md をコピー」（その task.md の @ を持つボタン）に印を付けて移る
function showMadeTask() {
  const made = madeTask;
  madeTask = null;
  const card = ui.detail.querySelector(".task-card");
  if (!made || !card || card.dataset.task !== made.task) return;
  const button = Array.from(card.querySelectorAll('.dh [data-action="copy"]'))
    .find((b) => !b.disabled && (b.dataset.copy || "").includes(made.taskMd));
  if (!button) return;
  button.classList.add("hint");
  button.focus();
}

// URL の # に残せるタスクのフォルダー名（区切りの / を含まず、. で始まらない）
function isTaskName(name) {
  return Boolean(name) && !name.includes("/") && !name.startsWith(".");
}

async function fetchText(url) {
  try {
    const res = await fetch(url);
    const text = await res.text();
    if (!res.ok) {
      toast(failureText(res.status, text));
      return null;
    }
    return text;
  } catch (error) {
    toast("サーバーにつながりません");
    return null;
  }
}

// 知らせ（#toast）と確認（#confirm）。ブラウザのダイアログは使わない

function toast(message, options) {
  const opts = options || {};
  clearTimeout(toastTimer);
  ui.toast.textContent = "";
  ui.toast.append(element("span", "", message));
  let field = null;
  if (opts.copy) {
    field = document.createElement("input");
    field.readOnly = true;
    field.value = opts.copy;
    field.setAttribute("aria-label", "コピーする文字");
    ui.toast.append(field);
  }
  if (opts.reload) ui.toast.append(makeButton("読み込み直す", "", () => location.reload()));
  const sticky = Boolean(opts.copy || opts.reload);
  if (sticky) ui.toast.append(makeButton("閉じる", "", hideToast));
  ui.toast.hidden = false;
  if (field) {
    field.focus();
    field.select();
  }
  // 答えを待っている知らせ（wait）は時間で消さない（答えが来たら postWrite が消すか、答えの知らせに替わる）
  toastWaits = Boolean(opts.wait);
  if (!sticky && !opts.wait) toastTimer = setTimeout(hideToast, TOAST_MS);
}

function hideToast() {
  clearTimeout(toastTimer);
  toastWaits = false;
  ui.toast.hidden = true;
  ui.toast.textContent = "";
}

function askToContinue(message, onContinue) {
  const box = element("div", "box", "");
  box.append(element("p", "", message));
  const buttons = element("div", "btns", "");
  const cancel = makeButton("やめる", "btn", hideContinue);
  buttons.append(makeButton("続ける", "btn pri", () => {
    hideContinue();
    onContinue();
  }), cancel);
  box.append(buttons);
  ui.confirm.textContent = "";
  ui.confirm.append(box);
  ui.confirm.hidden = false;
  cancel.focus();
}

function hideContinue() {
  ui.confirm.hidden = true;
  ui.confirm.textContent = "";
}

// 状態の表示（5秒ごとに /api/status を見る）

async function pollStatus() {
  let data;
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error(String(res.status));
    data = await res.json();
  } catch (error) {
    ui.status.textContent = "サーバーにつながりません";
    return;
  }
  lastStatus = data;
  const indexing = data.indexing;
  if (indexing) {
    ui.status.textContent = indexing.total ? "読み込み中 " + indexing.done + "/" + indexing.total : "読み込み中";
  } else {
    ui.status.textContent = data.last_update_at ? "索引 " + localTime(data.last_update_at) : "索引 まだありません";
  }
  showCounts();
  showBanner([data.format_warning, data.source_warning].concat(data.errors || []));
  // 更新の時刻は更新が終わったときに変わる。更新の間は、読み終えたセッションから出すよう毎回取り直す
  if (lastUpdate !== undefined && (data.last_update_at !== lastUpdate || indexing)) refresh();
  lastUpdate = data.last_update_at;
}

function showCounts() {
  const counts = lastStatus && lastStatus.counts;
  if (!counts) return;
  ui.counts.textContent = state.v === "docs"
    ? "資料 " + number(counts.documents) + "件"
    : "対話 " + number(counts.interactive) + " ／ ヘッドレス " + number(counts.headless);
}

function showBanner(lines) {
  const shown = lines.filter(Boolean);
  ui.banner.textContent = "";
  for (const line of shown) ui.banner.append(element("div", "", line));
  ui.banner.hidden = !shown.length;
}

// 台帳が更新されたら、選んだものを保ったまま一覧（と左の数）を取り直す
async function refresh() {
  if (panes.list.abort) await panes.list.promise; // 描画が一覧を取りに行っている途中なら、取り消さずに待つ
  const st = state;
  const listed = await load("list", listUrl(st), true);
  if (st !== state || !listed) return;
  load("left", leftUrl(st), true);
  if (panes.detail.url === "") render(); // まだ何も選んでいなければ（台帳が空だったときなど）、最初の行を選ぶ
}

async function reindex() {
  const answer = await post("/api/reindex", {});
  if (!answer) return;
  toast("更新を始めました");
  for (const delay of [600, 1500, 3000]) setTimeout(pollStatus, delay);
}

// 狭い窓：左の列の代わりに、上の帯の選択肢で場所（資料の種類、タスクの状況）を選ぶ

function syncSelects() {
  // 共通の資料の行（data-action を持つ data-place）は資料の画面へ移る操作なので、場所の選択肢に入れない
  fillSelect(ui.placeSelect, ui.left.querySelectorAll(".place[data-place]:not([data-action])"), "place", state.p);
  fillSelect(ui.kindSelect, ui.left.querySelectorAll(".place[data-kind]"), "kind", state.k);
  fillSelect(ui.statusSelect, ui.left.querySelectorAll(".place[data-status]"), "status", state.status);
}

function fillSelect(select, rows, attribute, value) {
  select.textContent = "";
  for (const row of rows) {
    const name = row.querySelector("span");
    const count = row.querySelector("em");
    const option = document.createElement("option");
    option.value = row.dataset[attribute];
    option.textContent = (name ? name.textContent : "") + (count ? "（" + count.textContent + "）" : "");
    if (name && name.title) option.title = name.title; // 場所は短い名前なので、表示名（パスの全体）を添える
    select.append(option);
  }
  select.value = value;
  select.disabled = !rows.length;
  const chosen = select.options[select.selectedIndex];
  select.title = chosen ? chosen.title : "";
}

function placeListOptions() {
  const home = narrowWindow.matches ? ui.optsSlot : ui.paneLeft;
  if (ui.listOpts.parentNode !== home) home.append(ui.listOpts);
}

// キー操作

function onKey(event) {
  if (event.isComposing || event.keyCode === 229) return; // 日本語の変換中
  const active = document.activeElement;
  if (!ui.confirm.hidden) {
    if (event.key === "Escape") {
      event.preventDefault();
      hideContinue();
    }
    return;
  }
  if (event.key === "Escape" && !ui.toast.hidden && (active === document.body || ui.toast.contains(active))) {
    hideToast();
    return;
  }
  if (active === ui.q) {
    if (event.key === "Escape") {
      event.preventDefault();
      if (ui.q.value) {
        ui.q.value = "";
        runSearch();
        syncClear();
      } else {
        ui.q.blur();
      }
    } else if (event.key === "Enter" && !event.metaKey) {
      // 検索をすぐに行い、↑↓ で一覧を動けるよう検索窓から出る
      event.preventDefault();
      runSearch();
      ui.q.blur();
    }
    return;
  }
  const slash = event.key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey;
  if (active && (active.matches("input, select, textarea") || active.isContentEditable)) {
    // ほかの入力欄（表示の設定など）では / と Esc だけを受ける。文字を打つ欄では / も打たせる
    if (event.key === "Escape") {
      active.blur();
    } else if (slash && !active.isContentEditable && !active.matches('textarea, input:not([type="checkbox"])')) {
      event.preventDefault();
      focusSearch();
    }
    return;
  }
  const plain = !event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey;
  if (slash) {
    event.preventDefault();
    focusSearch();
  } else if ((event.key === "ArrowDown" || event.key === "ArrowUp") && plain) {
    event.preventDefault();
    // ボタンを押した後でも、続く Enter が一覧の選択に効くよう、ボタンから離れる
    if (active && active.matches("button, a, summary")) active.blur();
    moveSelection(event.key === "ArrowDown" ? 1 : -1);
  } else if (event.key === "Enter" && event.metaKey && !event.ctrlKey && !event.altKey) {
    event.preventDefault();
    defaultResume();
  } else if (event.key === "Enter" && plain && !(active && active.matches("button, a, summary"))) {
    event.preventDefault();
    openSelectedConversation();
  }
}

// 検索窓の「×」は、文字があるときだけ出す
function syncClear() {
  ui.qClear.hidden = !ui.q.value;
}

// 検索語を消して一覧に戻る（Esc と同じ処理）。入力の位置は検索窓に置く
function clearSearch() {
  ui.q.value = "";
  runSearch();
  syncClear();
  ui.q.focus();
}

function focusSearch() {
  ui.q.focus();
  ui.q.select();
}

function openSelectedConversation() {
  if (state.v !== "sessions" || !state.s || state.d || state.c) return;
  const turns = rowTurns(state.s).split(",").filter(Boolean);
  const turn = turns.length ? turns[Math.min(Number(state.i || 0), turns.length - 1)] : "";
  go({ c: "1", t: turn }, true);
}

function scheduleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, SEARCH_DELAY_MS);
}

function runSearch() {
  clearTimeout(searchTimer);
  const q = ui.q.value.trim();
  if (q === state.q) return;
  const push = !q !== !state.q; // 検索を始めるときと、やめるとき
  if (state.v === "docs") go({ q: q, d: "" }, push);
  // タスクの表示には検索がない。これまでどおり会話・資料・入力履歴を、セッションの表示で探す
  else if (state.v === "tasks") go({ v: "sessions", q: q, p: "", s: "", d: "", c: "", t: "", i: "" }, true);
  else go({ q: q, s: "", d: "", c: "", t: "", i: "" }, push);
}

// 検索の期間・発言者・並び順を変える。一致が変わるので、新しい検索と同じく最初の一致を選び直す
function refine(changes) {
  go(Object.assign({ s: "", d: "", c: "", t: "", i: "" }, changes), false);
}

// 部品

function byId(id) {
  return document.getElementById(id);
}

function metaContent(name) {
  const tag = document.querySelector('meta[name="' + name + '"]');
  return tag ? tag.content : "";
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function makeButton(label, className, onPress) {
  const node = element("button", className, label);
  node.type = "button";
  node.addEventListener("click", onPress);
  return node;
}

function number(n) {
  return Number(n || 0).toLocaleString("en-US");
}

function localTime(iso) {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  const two = (n) => String(n).padStart(2, "0");
  return two(when.getMonth() + 1) + "/" + two(when.getDate()) + " " + two(when.getHours()) + ":" + two(when.getMinutes());
}

// 始める

const narrowWindow = window.matchMedia("(max-width: 1199.98px)");

document.addEventListener("click", onClick);
document.addEventListener("submit", onSubmit);
document.addEventListener("keydown", onKey);
window.addEventListener("popstate", fromHistory);
window.addEventListener("hashchange", fromHistory);
window.addEventListener("message", receiveMermaid);
document.addEventListener("tsuzuki-theme", redrawMermaid);
ui.q.addEventListener("input", (event) => {
  syncClear();
  if (!event.isComposing) scheduleSearch(); // 日本語の変換中は、確定してから
});
ui.q.addEventListener("compositionend", scheduleSearch);
ui.confirm.addEventListener("click", (event) => {
  if (event.target === ui.confirm) hideContinue(); // 枠の外を押したら、やめる
});
ui.placeSelect.addEventListener("change", () => choosePlace(ui.placeSelect.value));
ui.kindSelect.addEventListener("change", () => go({ k: ui.kindSelect.value, d: "" }, true));
ui.statusSelect.addEventListener("change", () => chooseStatus(ui.statusSelect.value));
ui.headless.addEventListener("change", () => go({ h: ui.headless.checked ? "1" : "0" }, false));
ui.days.addEventListener("change", () => go({ days: ui.days.value }, false));
ui.qdays.addEventListener("change", () => refine({ qdays: ui.qdays.value }));
ui.role.addEventListener("change", () => refine({ role: ui.role.value }));
ui.order.addEventListener("change", () => refine({ order: ui.order.value }));
byId("reindex").addEventListener("click", reindex);
byId("theme").addEventListener("click", cycleTheme);
ui.qClear.addEventListener("click", clearSearch);
if (narrowWindow.addEventListener) narrowWindow.addEventListener("change", placeListOptions);
else narrowWindow.addListener(placeListOptions);
placeListOptions();
ui.q.value = state.q;
render();
pollStatus();
setInterval(pollStatus, POLL_MS);
