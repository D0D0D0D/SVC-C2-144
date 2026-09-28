# SVC-C2-144 テスト仕様

`docs/02_design.md` が宣言した契約の一つずつに、それを証明するテストを対応させる。
**設計が契約を宣言したのに証明テストが無い、という状態を作らない**ための対応表である。

## 実行結果（2026-08-21・`agenticstar-agentcore==1.0.1`）

```
.venv/bin/python -m pytest tests -q
473 passed, 1 skipped
```

| 区分 | ファイル | 件数 | 主な対象 |
|---|---|---:|---|
| unit | `tests/unit/test_services.py` | 51 | `span_kind` 9 分岐・凍結窓・数値ガード・想起・3 値分離・Stage 3 checkpoint 前サニタイズ・HITL sanitizer 順序・prompt/nullability contract |
| unit | `tests/unit/test_s3_output_gate.py` | 33 | S-3 拒否条件 #1〜#12、`gate_kind` 値、citation integrity、全 model field の nullability accept/reject 対 |
| unit | `tests/unit/test_main_node.py` | 11 | GraphNode 契約（キャッシュ・checkpointer・ref 伝送・mode/error code allowlist） |
| unit | `tests/unit/test_framework_compliance_tc06_tc07.py` | 2 | `@final` ゲートの非バイパス性（scaffold 提供） |
| integration | `tests/integration/test_endpoints.py` | 50 | API schema・入力品質境界・認可・版固定入力・LLM失敗コード・削除再開・監査 |
| integration | `tests/integration/test_hitl.py` | 15 | suspend → resume → success・実LLM型 nullable finding の accept/reject 対・上限・所有権・全 message検査・terminal payload/checkpoint削除・期限削除 |
| integration | `tests/integration/test_orch_verify_144.py` | 5 | span/申請本文のPII・指示形非到達と正常値byte-identical到達 |
| PB | `tests/proof_of_boundary/` | 21 | PB-1〜PB-7・実 lifespan の空/非空設定 |
| parts | `tests/parts/` | 286 | 写経部品の同梱テスト（改変せず実行） |

**skipped 1 件:** PB-5（下記のとおり framework の ingress hooks が未提供のため auto-waived）。

repo root の `.venv/bin/python -m pytest -q` も実行した。結果は
`810 passed, 4 skipped, 1 failed, 22 errors`。失敗 1 件は repo 外に置かれる配布物
scaffold の配布マニフェスト不在、error 22 件は sandbox が `127.0.0.1` の listen socket を禁止するため
`scripts/test_stg_invoke_evidence.py` の fixture を開始できないものだった。テンプレ実装を対象とする
`tests/` は上記のとおり完走している。

**部品同梱テストは 1 件も改変せず、1 件も skip していない。** 本テンプレは
`SCOPE_KEYS = ()`（区画なし）だが、`tests/parts/entry-adapter/conftest.py` の autouse fixture が
部品の scoped 契約テストの実行時だけ `SCOPE_KEYS` を部品既定値に monkeypatch で戻す
（`apply_part` が生成する標準の形。scopeless の先行テンプレ 5 本と同一）。
これにより **部品の汎用契約（scoped）とテンプレの実設定（scopeless）の両方**が検証される。

---

## TC — フレームワーク準拠

| TC-ID | 対象 | 実装 | 結果 |
|---|---|---|---|
| TC-01 | State は flat TypedDict・Pydantic/dataclass 不使用 | `src/schemas/state.py`（`NotRequired` のみ）/ `tests/proof_of_boundary/test_state_safety.py` | ✅ |
| TC-02 | `SecurityViolationError` が発火する | `tests/unit/test_s3_output_gate.py`（拒否条件 12 種すべて） | ✅ |
| TC-03 | 資格情報がスキャン範囲に存在しない | `gate-credential-scan`（CI） | ✅ |
| TC-04 | `InvocationContext` は `from_state()` のみ | 全ノードが `from_state`。直接構築は**入口 adapter のみ**（`src/api/server.py` — ノード内ではない） | ✅ |
| TC-05 | 各 `execute()` がドメインイベントを発火 | `tests/proof_of_boundary/test_pb1_*`（PB-1 と同一実装） | ✅ |
| TC-06 | `_security_gate_input()` が `@final` | `tests/unit/test_framework_compliance_tc06_tc07.py`（scaffold 提供・改変なし） | ✅ |
| TC-07 | `_security_gate_output()` が `@final` | 同上 | ✅ |
| TC-08 | `required_trust_level` の宣言と強制 | `tests/proof_of_boundary/test_pb_invoke_order.py`（**全ノード動的スキャン**で `node(state)` を `__call__` 経由で実行）／`tests/integration/test_endpoints.py::test_anonymous_caller_is_refused` | ✅ |

---

## AT — 設計契約の受入テスト

| AT-ID | 契約 | 実装 | 結果 |
|---|---|---|---|
| AT-1 | 接続境界（未登録 caller で fail-closed・接続を握らない） | `test_endpoints.py::test_unregistered_caller_is_fail_closed` / `test_main_node.py::test_subgraph_is_built_once_and_cached` | ✅ |
| AT-2 | operation permission（7 endpoint） | `/ingest`・perspective delete・audit read の 403 と mutation 無しを実測。7 endpoint 全組合せは未網羅 | ⚠️ 部分 |
| AT-3 | S-3 closed allowlist・`span_kind` 9 分岐 | `test_s3_output_gate.py`（`gate_kind`・dangling/empty citation・短い quote を含む）。nullable は gap / contradiction / question の `condition_id` と supported の `confidence` だけを許可し、`supported_conditions.condition_id` 等へ広がらないことを全 model field の対で検証 / `test_services.py::test_span_kind_branches` ほか | ✅ |
| AT-4 | 不在テスト（判定フィールドが構造上作れない） | `test_s3_output_gate.py::test_no_verdict_field_exists_anywhere_in_the_contract` | ✅ |
| AT-5 | 凍結窓の決定論 | `test_services.py`（オフセット逆転・offset-naive reject・「重なり 0」と「未評価」の区別・隣接窓）/ `test_endpoints.py::test_freeze_window_overlap_is_computed_deterministically` | ✅ |
| AT-6 | `records[]` discriminated union | `test_endpoints.py::test_control_documents_cannot_be_ingested` / `test_ingest_rejects_rows_individually` | ✅ |
| AT-7 | テキスト品質ゲート | `test_text_gate_boundary_pairs` で 4 指標の accept/reject 境界、invoke span と ingest/feedback 適用を検証 | ✅ |
| AT-8 | `/feedback` enum と `approved` 必須 | 代表 3 組の正常 write、enum 外、`approved` false/欠落、`decided_by` 追加の write ゼロを実測 | ✅ |
| AT-9 | nested `evidence_declarations`・既定値注入・未宣言キー 400 | `test_evidence_declarations_use_the_nested_closed_schema` / `test_undeclared_request_key_is_rejected_not_dropped` | ✅ |
| AT-10 | `FINDING_TEXT_LIMITS` | `src/services/response_contract.py`（prompt と validator が同一定数） | ✅ |
| AT-11 | 縮退と失敗の区別 | `test_endpoints.py::test_llm_absent_degrades_but_still_succeeds`（`llm_unavailable` は success）／RuntimeError を投げる LLM を注入した実 HTTP 応答が `status=error`・`error_code=llm_call_failed` で、provider 例外文を含まない | ✅ |
| AT-12 | prompt contract | `test_prompt_contains_the_executable_contract_not_only_a_reference_to_it`。`prior_outcome` / `gap_origin` は model schema に現れず、gap / contradiction / question の condition と confidence の nullable 規則を明記。不正 JSON・不足 section は `output_contract_violation`。実 LLM と同形の null 応答が success・非空 findings/source へ到達 | ✅ |
| AT-13 | startup が実際に走る | `with TestClient(app):` でlifespanを実行。`auth_runbook_url` 空は `RuntimeError`、非空はhealth 200の対テスト。config→service配線と、未使用の保持期間キーがruntime configに無いことも実測 | ✅ |
| AT-14 | 未知 mode の二重防御 | 公開 API は mode を受け取らず、inner の未知 mode が allowlist fallback で END になることを単体実測 | ✅ |
| AT-15 | 版固定入力の検証 | unknown criteria / criteria hash mismatch / unknown calendar を固有 code で実測。未承認・有効期間外は未実測 | ⚠️ 部分 |
| AT-16 | AG は統制文書に書けない | `test_endpoints.py::test_control_documents_cannot_be_ingested` | ✅ |
| AT-17 | KB projection・申請 span・申請本文の安全射影 | spy LLM の全 message を捕捉し、全角 PII・指示形の非到達、withhold gap、人手確認、正常 span/本文の byte-identical 到達、raw 本文の checkpoint 非残留を実測。`/feedback` 全 4 パターンは未網羅 | ⚠️ 部分 |
| AT-18 | 過去判例の照合 | `src/nodes/stage4_validation.py::_validate_precedents`（`prior_issue`/`prior_outcome` は KB の値を決定論で写す）/ `test_s3_output_gate.py`（#10-d） | ✅ |
| AT-19 | HITL full lifecycle | `test_hitl.py::test_invoke_suspends_and_resume_reaches_success`（**resume が success に到達すること**を assert）。discard は success のまま finding 6 配列 + `evidence_sources` が空、pins / conditions / freeze facts が保持されることを全項目 assert | ✅ |
| AT-20 | `/resume` の認証と所有権 | unknown thread と同一 deployment 内の別 caller による resume が実行前に拒否されることを実測 | ✅ |
| AT-21 | resume 応答のサニタイズ | key→enum→PII→injection の順序、指示形 reject 時の `agent.resume()` 0 回、outer / inner checkpoint 不変を実測。非文字キーは JSON object で表現不能、サイズ超過は未実測 | ⚠️ 部分 |
| AT-22 | `hitl_allowed` ガード | `test_hitl.py::test_hitl_allowed_false_never_suspends` / `test_pb7_hitl_interrupt_propagation.py` | ✅ |
| AT-23 | `DELETE /requests/` の cross-store 契約 | 正常・404・承認参照欠落・checkpoint 物理削除・payload 障害後の tombstone 再開と累積件数を実測。プロセス再起動越しは未実測 | ⚠️ 部分 |
| AT-24 | 削除系統の分離と部分失敗からの再開 | `test_endpoints.py::test_deleting_a_request_does_not_delete_past_perspectives` / `test_perspective_delete_removes_both_db_row_and_projection` / **`test_perspective_delete_partial_failure_is_visible_and_resumable`**（KB 削除を失敗させ、500 `delete_incomplete`・tombstone 残存・監査に `outcome: incomplete`・再実行で完了、を実測） | ✅ |
| AT-25 | payload の生存期間 | Stage 3 の `consume=False`、suspend 中の payload 残存、terminal 後の payload と outer / inner checkpoint の物理削除を実測。TTL 失効の実測は下記「盲点」 | ⚠️ 部分 |
| AT-26 | `refine` の回数上限 | `test_hitl.py::test_refine_ceiling_stops_the_loop`（`max_hitl: 2` で 3 回目に `hitl_limit_reached`） | ✅ |
| AT-27 | HITL セッションの期限 | 期限判定を注入して 410、payload・checkpoint 物理削除、台帳 clear を実測。実時間境界は未実測 | ⚠️ 部分 |
| AT-28 | 監査の参照経路 | audit read の permission 不足 403 は実測。正常 read と purge approval ref は未実測 | ⚠️ 部分 |

---

## PB — Proof of Boundary

| PB-ID | 境界 | 実装 | 結果 |
|---|---|---|---|
| PB-1 | BaseNode → AuditLogger | 各 node module の `audit.emit` binding を patch。正常経路はfallbackを除外してノード固有event型を要求し、例外経路はfallbackを確認。`EVT_STAGE1` 抑止変異で正常経路の判定が失敗することも実測。framework自動イベントは対象外 | ✅ |
| PB-2 | State のシリアライズ | `test_state_safety.py` | ✅ |
| PB-3 | L1 → 外部サービス | **未配線**（SDK の永続化層は STUB）。実接続時に解消する事項は 運用ガイド に明記 | ⚠️ 未配線 |
| PB-4 | Import isolation | `test_import_isolation.py`（AST スキャン） | ✅ |
| PB-5 | checkpoint 安全性 | **auto-waived** — 導入済み agentcore に `BaseGraph` ingress hooks が無い（`_sanitize_ingress` / `_sanitize_resume_feedback` 不在）。PB-5 の条件付き規則どおり | ⏭ 条件未成立 |
| PB-6 | invoke 実行順序 | `test_pb_invoke_order.py` — **全ノード動的スキャンを維持**。依存を要するノードも構築して実行し、S-1 → node_start → S-2 → execute → S-3 の順序を全ノードで実測 | ✅ |
| PB-7 | HITL interrupt 伝播 | `test_pb7_hitl_interrupt_propagation.py` — **実グラフで検証**（ノード単体では `interrupt()` は正しく振る舞わない）。scaffold の `_CONFIG_PATH` / `_hitl_enabled()` API は温存 | ✅ |

---

## 実 LLM 1 回転（2026-08-21）

**2026-08-21:** Azure OpenAI `gpt-5.5` の responses API を直接呼ぶアダプタで検証（ローカル CLI はセッションログが応答へ混入し3回中2回が空応答だったため不使用）。`status: success`、`supported_conditions: 1` / `evidence_gaps: 3` / `approval_scope_discrepancies: 1` / `verification_questions: 3`、`evidence_sources` 非空を確認。この検証で `condition_id: null` の契約不整合（I-29）を発見し、Stage 3 / S-3 / prompt の nullable 契約を修正した。

---

## テストの盲点（隠さず申告する）

後段の STG / レビュー検証計画になるので、通っていない範囲を明示する。

| # | 盲点 | 理由 | いつ解消するか |
|---|---|---|---|
| 1 | **PB-3（実バックエンド接続）** | SDK の永続化層が全 STUB。DB / KB は in-memory 実装 | 実接続が提供された時点。運用ガイド §未配線シーム に解消事項を明記 |
| 2 | **TTL の実時間境界**（AT-25 / AT-27） | payload-store の注入 clock と期限到達処理は個別に実測したが、standalone 全体を同一 fake clock で進める E2E は未実装 | 永続 backend 導入時の clock seam で追補 |
| 3 | **削除再開のプロセス再起動越し** | payload / KB 障害後の同一プロセス再開は実測。in-memory DB では再起動後の tombstone 復元を証明できない | 永続 backend 接続時 |
| 4 | **並行 resume** | 同一 `thread_id` への同時 resume を検証していない | 実運用の並行性が判明した時点 |
| 5 | `scripts/test_gitlab_ci_stage_matrix.py` | ローカルに scaffold の配布マニフェスト（repo にコミットしない）が無いと失敗する | scaffold 側の課題 |
