# 同梱しているライブラリ

## Mermaid（Markdown の資料の図）

| 項目 | 値 |
| --- | --- |
| 版 | 11.17.2（npm の公開 2026-08-25T11:52:39Z。11 系で、公開から14日以上たった最新の版） |
| 取得元 | https://registry.npmjs.org/mermaid/-/mermaid-11.17.2.tgz |
| tgz の完全性（npm の `dist.integrity`） | `sha512-V6K3C8EBdEsPFZXSKMJe6ppQOENxuHARr9GvHX4hh47lAbhMRD9qf4oEK7LoaRQxULMa80/qt5gHO73aCleBBg==`（取った tgz と一致） |
| `mermaid.min.js` の sha256 | `581ed7d74bd9048d0e3a91363927d72ef22942d7722546b27f7cc29e35390eb8` |
| `mermaid-LICENSE.txt` の sha256 | `ec9fb67dcb25eccc416ed56e1aab819222c805a2a4bfe4cb19e7556bf2ffde80` |
| ライセンス | MIT（`mermaid-LICENSE.txt`。tgz の `package/LICENSE` をそのまま写した） |

- `mermaid.min.js` は tgz の `package/dist/mermaid.min.js` をそのまま写したもの（書き換えていない）。
  普通の `<script>` で読むと、大域の `mermaid` を作る形（esbuild の IIFE。末尾が
  `globalThis["mermaid"] = ….default;`）
- 画面は、このファイルを外から読み込まない。`/mermaid-frame` のページの中に文として入れ、隔離した枠
  （`sandbox allow-scripts`）の中だけで動かす。そのため、文の中に `<script` と `</script` があってはならない
  （サーバーが確かめ、あればページを返さない。テストも確かめる）

### 版を上げるとき

1. `https://registry.npmjs.org/mermaid` で版と公開日時を見て、tgz を作業用の空のディレクトリに取る（中のスクリプトは実行しない）
2. tgz の sha512 が登録簿の `dist.integrity` と一致することを確かめる
3. `package/dist/mermaid.min.js` と `package/LICENSE` だけを写し、この表を書き直す。`server.py` の
   `MERMAID_VERSION` と `app.js` の `MERMAID_URL` の版も書き直す（枠の URL が変わり、ブラウザが覚えた古い版を使わない）
4. 全部のテストを走らせ、ブラウザで流れ図・シーケンス図・壊した図を明るい表示と暗い表示で確かめる
