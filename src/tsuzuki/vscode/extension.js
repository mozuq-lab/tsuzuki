"use strict";
// Tsuzuki Terminal：Tsuzuki が書いた依頼を読み、VS Code の統合ターミナルでコマンドを実行する拡張。
//
// Tsuzuki は依頼を <台帳>/vscode-requests/<ID>.json（権限 0600）に書き、vscode://tsuzuki-local.terminal/run?id=<ID>
// を開く。URI に載るのは乱数の ID だけで、実行するコマンドとフォルダーは依頼のファイルにだけある。そのため、
// Web ページなどがこの URI を開いても、Tsuzuki が書いていない依頼は実行されない。依頼は読んだら消し、
// 古いものは使わない。依頼の置き場所は、入れるときに Tsuzuki が tsuzuki.json に書く。
const fs = require("fs");
const path = require("path");

const REQUEST_ID = /^[0-9a-f]{32}$/;
const MAX_AGE_MS = 120 * 1000; // Tsuzuki の vscode_ext.REQUEST_MAX_AGE_SECONDS と同じ
const DEFAULT_NAME = "Claude Code";

function requestsDir(extensionPath) {
  let conf;
  try {
    conf = JSON.parse(fs.readFileSync(path.join(extensionPath, "tsuzuki.json"), "utf8"));
  } catch (e) {
    throw new Error("拡張の設定（tsuzuki.json）を読めません。tsuzuki vscode install で入れ直してください");
  }
  if (!conf || typeof conf.requests_dir !== "string" || !path.isAbsolute(conf.requests_dir)) {
    throw new Error("拡張の設定（tsuzuki.json）の requests_dir が正しくありません");
  }
  return conf.requests_dir;
}

// 依頼を読んで消す。読めない・古い・形が違う依頼は使わない（どの場合も消す）。
function takeRequest(dir, id, now) {
  if (!REQUEST_ID.test(id)) throw new Error("依頼の ID が正しくありません");
  const file = path.join(dir, id + ".json");
  let text, stat;
  try {
    stat = fs.statSync(file);
    text = fs.readFileSync(file, "utf8");
  } catch (e) {
    throw new Error("依頼が見つかりません（使用済みか、期限切れです）");
  }
  fs.unlinkSync(file);
  if (now - stat.mtimeMs > MAX_AGE_MS) throw new Error("依頼の期限が切れています。Tsuzuki の画面からもう一度開いてください");
  let req;
  try {
    req = JSON.parse(text);
  } catch (e) {
    throw new Error("依頼を読めません");
  }
  if (!req || typeof req.cwd !== "string" || !path.isAbsolute(req.cwd)
      || typeof req.command !== "string" || !req.command || /[\r\n]/.test(req.command)) {
    throw new Error("依頼の中身が正しくありません");
  }
  let isDir = false;
  try {
    isDir = fs.statSync(req.cwd).isDirectory();
  } catch (e) {
    isDir = false;
  }
  if (!isDir) throw new Error("作業ディレクトリが見つかりません: " + req.cwd);
  return { cwd: req.cwd, command: req.command, name: typeof req.name === "string" && req.name ? req.name : DEFAULT_NAME };
}

// 新しいターミナルには、ほかの拡張も文字を打ち込む（Python の拡張が仮想環境を有効にする
// source <venv>/bin/activate など）。それらはシェルの統合（shell integration）が使えるようになってから
// 打ち込まれるので、すぐに送ると、先に起動した claude の入力欄に入ってしまう。そこで、シェルの統合を待ち、
// そのターミナルでコマンドが走っていない時間が quietMs 続いてから送る（走っているものは終わるのを待つ）。
// シェルの統合が上限の後に来たときも、そこから待ち直す。上限まで待っても走っているコマンドがあれば、
// executeCommand（走っているものを止めることがある）ではなく sendText で、その後ろに並べる。
const TIMING = {
  shellIntegrationMs: 5000, // シェルの統合を待つ上限（来なければ、静かになるのを待って sendText で送る）
  quietMs: 1500, // コマンドが走っていない時間がこれだけ続いたら送る
  maxWaitMs: 10000, // 静かになるのを待つ上限
};

function waitForShellIntegration(vscode, terminal, timeoutMs) {
  if (terminal.shellIntegration) return Promise.resolve(terminal.shellIntegration);
  return new Promise((resolve) => {
    let listener = null;
    const done = (value) => {
      clearTimeout(timer);
      if (listener) listener.dispose();
      resolve(value);
    };
    const timer = setTimeout(() => done(terminal.shellIntegration || null), timeoutMs);
    if (typeof vscode.window.onDidChangeTerminalShellIntegration === "function") {
      listener = vscode.window.onDidChangeTerminalShellIntegration((e) => {
        if (e.terminal === terminal) done(e.shellIntegration);
      });
    }
  });
}

function waitUntilQuiet(vscode, terminal, quietMs, maxWaitMs) {
  return new Promise((resolve) => {
    const listeners = [];
    let running = 0;
    let quietTimer = null;
    const done = () => {
      clearTimeout(quietTimer);
      clearTimeout(maxTimer);
      listeners.forEach((l) => l.dispose());
      resolve(running === 0);
    };
    const restart = () => {
      clearTimeout(quietTimer);
      if (running === 0) quietTimer = setTimeout(done, quietMs);
    };
    const maxTimer = setTimeout(done, maxWaitMs);
    const w = vscode.window;
    if (typeof w.onDidChangeTerminalShellIntegration === "function") {
      listeners.push(w.onDidChangeTerminalShellIntegration((e) => {
        if (e.terminal === terminal) restart(); // 遅れて来たシェルの統合の後に、ほかの拡張が打ち込むのを待つ
      }));
    }
    if (typeof w.onDidStartTerminalShellExecution === "function") {
      listeners.push(w.onDidStartTerminalShellExecution((e) => {
        if (e.terminal !== terminal) return;
        running += 1;
        restart();
      }));
    }
    if (typeof w.onDidEndTerminalShellExecution === "function") {
      listeners.push(w.onDidEndTerminalShellExecution((e) => {
        if (e.terminal !== terminal) return;
        running = Math.max(0, running - 1);
        restart();
      }));
    }
    restart();
  });
}

async function runWhenReady(vscode, terminal, command, timing) {
  const shellIntegration = await waitForShellIntegration(vscode, terminal, timing.shellIntegrationMs);
  const quiet = await waitUntilQuiet(vscode, terminal, timing.quietMs, timing.maxWaitMs);
  const current = terminal.shellIntegration || shellIntegration;
  if (current && quiet) current.executeCommand(command);
  else terminal.sendText(command, true);
}

async function handle(vscode, extensionPath, uri, now, timing) {
  if (uri.path !== "/run") throw new Error("知らない操作です: " + uri.path);
  const id = new URLSearchParams(uri.query).get("id") || "";
  const req = takeRequest(requestsDir(extensionPath), id, now);
  const terminal = vscode.window.createTerminal({ name: req.name, cwd: req.cwd });
  terminal.show();
  await runWhenReady(vscode, terminal, req.command, timing || TIMING);
}

function activate(context) {
  const vscode = require("vscode");
  context.subscriptions.push(vscode.window.registerUriHandler({
    handleUri(uri) {
      handle(vscode, context.extensionPath, uri, Date.now()).catch((e) => {
        vscode.window.showErrorMessage("Tsuzuki: " + (e && e.message ? e.message : String(e)));
      });
    },
  }));
}

function deactivate() {}

module.exports = { activate, deactivate, _handle: handle, _takeRequest: takeRequest };
