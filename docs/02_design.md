# Template Design Specification — SVC-C2-144 Managed-Service Change-Freeze Exception Evidence Review Agent

> **承認**: 2026-08-17
> 本設計書は 提案書 §Design 入場条件 (a)〜(e) を schema / API / テストに落とすことを目的とする。
> SDK 契約の実機確認結果は §SDK 実機スパイク（AgentCore SDK **1.0.1** 実体・2026-08-20 確認）に準拠する。

## Position in AgentCore Architecture

- **Agent Class**: `ChangeFreezeExceptionReviewGraph`（`src/graph/graph.py`。設計段階の `config/agent.yaml` は
  scaffold 既定の `src.graph.graph.Graph` を指していた — 実在しないクラスを manifest が指すと registry import が失敗するため、
  リネームは実装と同一 commit で行った）
- **L1 Base**: `AgentBaseGraph`（L1 直継承）
  - `AutonomousBaseGraph` は採らない。本テンプレは自律ループではなく、モードごとに終端が決まる固定パイプラインである。
- **Three-Layer Separation**:
  - **State**: flat TypedDict の合成（`AgentState` を継承・Pydantic 不可＝msgpack 非互換）
  - **Node**: L1 継承（Template Method — `execute(self, state) -> dict` のみ override）
  - **Graph**: 合成（`register_nodes()` で 3 スロットを埋める。`add_edges()` / `route()` は override しない）

## Architecture Overview

### 3 モード

| モード | 目的 | 主な作用 |
|---|---|---|
| `ingest` | **当該申請の証跡**と、**CAB が確定した過去レビュー観点の候補**を構造化して蓄積する。**例外承認基準・凍結カレンダーは対象外**（§統制文書の版固定入力） | 顧客専用 DB へ原記録を append、KB へ retrieval-safe projection を書込 |
| `invoke` | 新規の凍結例外申請を承認済み例外承認基準に照合し、レビューパケットをドラフトする | Stage 1〜4 を実行し、非結論の出力（`INVOKE_TOP_LEVEL_KEYS`）を返す |
| `feedback` | **レビュー確定後**に、CAB／変更管理担当が確定・編集した論点を明示承認 + 監査イベント付きで**候補キューに積む**（**実行中の対話・差し戻しの経路ではない** — それは HITL が担う） | `approved is True` のときのみ `perspective_candidates` へ append し監査イベントを記録。**KB / `review_perspectives` には書かない**（次版への昇格は AG 外の人間作業） |

### Node Configuration（outer graph — 固定 5 ノード backbone）

| Node | 責務 | Input State | Output State | Inherits/Overrides |
|------|------|-------------|--------------|-------------------|
| `initialize` | framework 既定 | — | — | `InitializeNode`（既定・注入） |
| `pre_process` | mode allowlist 検証・顧客解決 fail-closed・パケット形状検証・allowlist 射影・入力品質ゲート | `user_input`, `caller_id` | `mode`, `request_ref`, `validated_input`, `payload_ref` | `FunctionNode` → `FreezeExceptionIntakePreProcessNode` |
| `main` | 内側 domain workflow subgraph への委譲 | 上記キー | モードスコープの結果キー | `GraphNode` → `FreezeExceptionWorkflowGraphNode` |
| `post_process` | S-3 出力安全契約の検査 + `formatted_output` envelope の整形 | main の結果キー | `formatted_output` | `FunctionNode` → `ReviewPacketPostProcessNode` |
| `finalize` | framework 既定 | — | — | `FinalizeNode`（既定・注入） |

> `add_edges()` / `route()` は **override しない**。framework 既定の
> `START → initialize → pre_process → main → {route} → post_process → finalize → END` をそのまま使う。

### Node Configuration（inner `FreezeExceptionWorkflowGraph` — 独自トポロジ）

| Stage | Node | 責務 | LLM |
|---|---|---|---|
| （入口） | `dispatch` | 内側 envelope の ref を解決し、カスタム typed キーへ展開する（§内部 envelope の伝送）。**mode 分岐はこのノードの後**に置く — mode 自体が envelope の中にあるため | 不要 |
| Stage 1 | `deterministic_check` | 必須欄の有無・書式・**凍結カレンダーとの時間窓照合**（§凍結カレンダーと時間窓の決定論契約）を決定論的に検証し `field_gaps` / `freeze_window_facts` / `source_registry` を出す。十分性・承認可否の意味的結論は出さない | 不要 |
| Stage 2 | `recall_perspectives` | 顧客専用 KB から過去例外レビューの **3 点セット（申請要約・確定結論・判定理由）** を想起する。**検索軸には承認結論を使わない**（§過去事例の 3 点セット）。日本語想起の実装契約は §Stage 2 の日本語想起 | 不要 |
| Stage 3 | `sufficiency_draft` | ①例外理由・ロールバック計画・リスク評価と承認済み例外承認基準の条件を突き合わせ、支持・不足・矛盾・確認質問・顧客承認範囲の不一致をドラフトする ②**Stage 2 が想起した過去の論点それぞれについて、今回の申請がそれに触れているかを判定する**（§過去判例の照合） | **必要（本テンプレ唯一の LLM 核）** |
| Stage 3.5 | `hitl_review` | Stage 3 のドラフトを担当者に提示して `interrupt()` し、**戻り値を 4 段サニタイズして `refine_action` を State に書く**（`route_after_hitl` が読む唯一の欄。§HITL 方針）。**`hitl_allowed` ガード必須** | 不要 |
| Stage 4 | `evidence_validation` | claim→source 対応・source ID 実在・逐語引用の忠実性・**引用の非裁定性（span_kind allowlist）**・**本文中の数値と source の一致**・現在申請スコープを機械検証する。不整合は fail-closed または `requires_human_review` | 不要 |
| （ingest） | `accumulate_ingest` | 品質ゲート → サニタイズ → 正規化 → 書込 → `ingest_summary` | 不要 |
| （feedback） | `accumulate_feedback` | `approved is True` 検証 → `perspective_candidates` へ append → 監査イベント → `feedback_summary`。**KB projection は書き換えない** | 不要 |

> **LLM は Stage 3 のみ。** Stage 1/2/4 は非 LLM のため単体テスト可能であり、
> 「LLM が必要な理由」を Stage 3 の当てはめ判断に限定できる（実装可能性の観点）。
> Stage 3 の全 finding は `source_ids` を 1 件以上必須とし、prompt の JSON schema にも `minItems: 1` を宣言する。
> Stage 1 が決定論的に作る「必須欄が存在しない」gap だけは空配列を許すが、**それは無条件の例外ではない**:
> 当該要素は `gap_origin: "deterministic"` を持ち、S-3 は
> **`gap_origin == "deterministic"` の要素にのみ空 `source_ids` を許す**（`gap_origin` は Stage 1 が付ける値で、
> LLM 応答の型射影は `gap_origin` を出力できない＝LLM が自称して空 `source_ids` を通す経路が無い）。
> `evidence_gaps[]` の exact-key は `{claim_id, description, condition_id, source_ids, gap_origin}` とし、
> Stage 3 由来の要素は `gap_origin: "llm"` を持つ。

> 🔴 **Stage 3 に時間の計算をさせない。** 「ロールバックが凍結窓に間に合うか」は
> **LLM が申請文から所要時間の記述を抽出して claim にするだけ**であり、その数値が申請文に実在するかの照合と、
> 凍結窓との突き合わせは **Stage 1 / Stage 4 の決定論**が行う（§凍結カレンダーと時間窓の決定論契約）。
> LLM に算術・日時比較をさせると、根拠のない数値が「所見」として出力に載る。

### inner の `BaseGraph` ABC 実装

| メソッド | 実装方針 |
|---|---|
| `name` | `"freeze_exception_workflow"` |
| `state_schema` | `FreezeExceptionWorkflowState`（inner 専用 flat TypedDict） |
| `_validate_config` | **顧客非依存**の注入物のみ検証する: `resource_resolver` / `payload_store` / `top_k` / `llm`。**`memory_enabled` / `hitl` は inner にも宣言が要る**（無いと inner の `compile()` が checkpointer を attach せず `interrupt()` が RuntimeError・§checkpointer 3 契約）。**`clock_now` は config に置かない**（§凍結カレンダー 契約 #2 — graph は起動時に 1 回だけ構築されるためリクエスト単位の値を config に載せられない）。**`criteria_version_ref` は検証しない**（版は呼出ごとの入力であり config ではない）。**`db_client` / `kb_client` も検証しない**（顧客専用接続は呼出ごとに resolver から取得する） |
| `register_nodes` | 上記 7 ノードを登録 |
| `add_edges` | `START → dispatch` → mode 分岐 + Stage 1 → 2 → 3 → **3.5（`hitl_review`）** → `route_after_hitl` → 4 → END |
| `route_after_hitl` | **HITL 後の分岐**（`hitl_review` が State に書いた `refine_action` だけを読む辞書 allowlist）: `{"confirm": "evidence_validation", "refine": "sufficiency_draft", "discard": END}`。`refine_action` 未設定・未知値は **END**（fail-closed。自由文を LLM に解釈させて再生成ループへ落とさない） |
| `route` | **モード分岐**（`dispatch` の後に評価する）: `{"invoke": "deterministic_check", "ingest": "accumulate_ingest", "feedback": "accumulate_feedback"}.get(mode, END)` の **辞書 allowlist**（denylist ではない）。未知 mode は **API 層が 400 `invalid_request` で先に落とす**ため到達しない。inner の `END` フォールバックは**到達不能な最終防御**であり、単体テストで「未知 mode を直接 inner に流すと結果が空で終端する（例外にもならず、いずれのモードのノードも実行されない）」ことを固定する |
| `get_output` | 実行されたモードのフィールドだけを返す（None 埋めしない） |

> ⚠️ inner の `route()` / `get_output()` の `state` 引数は **`FreezeExceptionWorkflowState` で注釈する**。
> `AgentState` で注釈すると射影でカスタム欄が消え誤 route する（Business Travel Hotel & Plan Recommendation Agent 実例）。

> 🔴 **顧客専用の接続を graph / node にキャッシュしない（最重要のセキュリティ契約）。**
> `GraphNode` の `self._subgraph` キャッシュに `db_client` / `kb_client` を握らせると、
> **次の呼出（別顧客）でも同じ接続が再利用され得る**。node の instance 変数はグラフ実行間で共有されるため、
> フレームワークの禁止事項（可変状態を node instance 変数に置かない）にも抵触する。
>
> **正しい契約:** graph / node が保持してよいのは**顧客非依存の `AuthorizedResourceResolver` だけ**。
> 各 Stage は `execute()` 内で `resolver.for_caller(ctx.caller_id)` を呼び、**その呼出限りの**顧客専用接続
> （`db` / `kb` / `payload_store`）を取得してローカル変数で使う。`self._subgraph` にキャッシュしてよいのは
> **トポロジ（ノード構成）だけ**であり、接続や顧客文脈は含めない。

**`AuthorizedResourceResolver` の契約（MR-B 実装対象）:**

```python
class AuthorizedResourceResolver:                       # 顧客非依存・graph に注入してよい唯一の resource seam
    def for_caller(self, caller_id: str) -> "ResourceScope": ...
        # 1. caller_id → 認可された導入先を解決（マッピング不在なら AuthorizationError で fail-closed）
        # 2. その導入先に provision 済みの専用 DB / KB / payload_store ハンドルを返す
        # 3. default / fallback の接続は返さない（存在しない）

class ResourceScope:                                    # 呼出限りのローカル変数。State にも self にも置かない
    tenant_ref: str                                     # 認可済みの導入先の接続識別。**分離キーではない**
                                                        # （単一顧客 1 デプロイ前提。docstring に根拠を残す）
    permissions: frozenset[str]                         # 認可された operation permission（§write 権限の mode 別契約）
    db:  "FreezeExceptionDbClient"
    kb:  "FreezeExceptionKbClient"
    payloads: "PayloadStore"
    registry: "ControlDocumentRegistry"                 # 統制文書の正本レジストリ（版固定入力の解決先）

    def require_permission(self, name: str) -> None: ...
        # 不足なら AuthorizationError（entry adapter が 403 + 監査イベントに変換）
```

**受入テスト（必須）:** ①未マッピングの `caller_id` で実行すると `AuthorizationError` で fail-closed になり、
default / fallback 接続に落ちないこと ②接続ハンドルが `self._subgraph` や node の instance 変数に
**キャッシュされていない**こと（同一 graph インスタンスで 2 回実行し、2 回目も resolver 経由で
取得していることを実測する。1 顧客デプロイでも、接続を握り込む実装は resolver の
fail-closed を無効化するため許さない）。

### Node 別 `required_trust_level`（S-1）

| Node | `required_trust_level` | 理由 |
|---|---|---|
| `FreezeExceptionIntakePreProcessNode` | `VERIFIED_EXTERNAL` | 顧客識別の解決を行う。匿名呼出は受け付けない |
| `FreezeExceptionWorkflowGraphNode` | `VERIFIED_EXTERNAL` | `GraphNode` 既定は `ANONYMOUS` のため**明示宣言必須**（既定のままだと内側 S-1 と不整合になる実バグ前例あり） |
| `ReviewPacketPostProcessNode` | `VERIFIED_EXTERNAL` | 顧客の機微情報を含む出力を扱う |
| inner の全 Stage ノード | `VERIFIED_EXTERNAL` | KB/DB 読書と LLM 呼出を伴う |

有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` の 3 つのみ（TrustLevel の有効値）。
`INTERNAL` は採らない — 本テンプレに `input_context` の claim だけで到達する特権経路は存在しない
（caller 由来の claim のみで権限を与えるノードに INTERNAL を要求する規則の対象外）。

### Data Flow

```
[MSP 変更管理担当]
   │  POST /ingest | /invoke | /feedback   (Bearer 認証 + middleware)
   ▼
src/api/server.py  ── 認証(caller_id) → 認可テナントの解決 → 顧客専用 DB/KB 接続の取得
                   ├─ 機微本文を payload_store に put → State には payload_ref のみ
                   └─ 基準日時 now を TZ 付きで 1 回確定して State の evaluated_at に載せる
   ▼
START → initialize → pre_process → main(GraphNode) → post_process → finalize → END
                         │              │                  │
                         │              ▼                  ▼
                         │   FreezeExceptionWorkflowGraph  S-3 closed allowlist
                         │   ├ invoke:   Stage1→2→3→3.5(HITL)→4  + claim→source 実在/逐語/数値
                         │   ├ ingest:   accumulate_ingest  + severity=candidate 固定
                         │   └ feedback: accumulate_feedback → formatted_output envelope
                         ▼
                  mode allowlist / 顧客照合 fail-closed / 入力品質ゲート
```

### State Definition

`src/schemas/state.py` — `class State(AgentState, total=False)` + `NotRequired[...]`

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `ingest` / `invoke` / `feedback` の 3 値 | ✅ |
| `request_ref` | `str` | 凍結例外申請の参照 ID（**英字のみ**。理由は下記注記） | ✅ |
| `validated_input` | `str` | **本テンプレでは使わない**（scaffold 由来の欄）。理由は下記 §内部 envelope の伝送 — この欄は S-2 のスキャン対象であり、ここに射影パケットを置くと次ノードのゲートが顧客命名 ID を `[MASKED]` に書き換える | — |
| `payload_ref` | `str` | State に載せない raw 本文の opaque handle。invoke の 4 自由記述に加え、ingest の `records` と feedback 本文もこの参照で局所解決する。**server が発行**しクライアント供給値は破棄 | — |
| `criteria_version_ref` | `str` | 適用する承認済み例外承認基準の**参照 ID**（人間指定・invoke の入力） | ✅(invoke) |
| `criteria_version` | `str` | 適用した基準の**版**（監査記録の必須項目） | ✅(invoke) |
| `criteria_content_hash` | `str` | 基準実体の内容ハッシュ（同一版番号での差替え検出） | ✅(invoke) |
| `freeze_calendar_ref` | `str \| None` | 適用する承認済み凍結カレンダーの参照 ID。**任意**（省略時 `None`） | — |
| `freeze_calendar_version` | `str \| None` | 同上の版 | — |
| `freeze_calendar_content_hash` | `str \| None` | 同上の内容ハッシュ | — |
| `sealed_refs` | `dict` | 入口 adapter が検証済みの実体に発行した**不変シール参照**（`{criteria_sealed_ref, freeze_calendar_sealed_ref}`）。実体そのものは State に載せない（本文が checkpoint に流れるのを避ける） | ✅(invoke) |
| `planned_window` | `dict` | 実施予定時間窓 `{start, end}`（**ISO-8601・オフセット必須**） | invoke 時 |
| `evidence_declarations` | `list[dict]` | nested `documents[].spans[]`（下記正規 schema）。`span_text` は pre-process で NFKC→PII マスク→指示形評価を済ませた値だけを保持する。API 境界では任意（省略時 `[]`） | invoke 時 |
| `ingress_evidence_gaps` | `list[dict]` | 指示形 span を引用候補から外した事実。説明には minted `source_id` だけを載せ、顧客命名の `doc_ref` / `span_ref` は載せない。Stage 1 が `field_gaps` へマージする | invoke 時（該当時） |
| `evaluated_at` | `str` | entry adapter が確定した **TZ 付き ISO-8601 の基準日時**。凍結窓照合の唯一の時刻基準（§凍結カレンダー 契約 #2）。**クライアントは指定できない** — `evaluated_at` は request schema の宣言外キーなので、body に含めると他の宣言外キーと同じく **400 `invalid_request`**（黙って無視するとクライアントは指定できたつもりになる） | invoke 時 |
| `fields_present` | `dict` | Stage 1 の必須欄有無の判定結果 | — |
| `field_gaps` | `list[dict]` | Stage 1 が決定論的に出す不足論点（`{claim_id, description, condition_id, source_ids: [], gap_origin: "deterministic"}`）。post_process が `evidence_gaps` へマージする（envelope に `field_gaps` は現れない） | — |
| `format_check` | `dict` | Stage 1 の書式検証結果 | — |
| `freeze_window_facts` | `dict` | Stage 1 の決定論的な凍結窓照合結果（§凍結カレンダー） | — |
| `recall_refs` | `list[dict]` | Stage 2 が想起した過去事例の 3 点セット。各要素は **`history_ref`**（`source_id` と名前空間が重ならない識別子。例 `HIST-<英字>`）/ `condition_id` / `criteria_version_ref` / **`request_summary_safe`** / **`decision_rationale_safe`** / `decision_outcome`。自由文は必ず `_safe` 値（`_raw` を State に載せると出力への PII 直通路になる）。**`source_registry` には入れない**（＝本件の証拠として引用できない） | — |
| `retrieved_records` | `list[dict]` | evidence-gate 契約キー（引用 ID 実在照合の突合先） | — |
| `source_registry` | `dict[str, dict]` | source_id → `{doc_type, doc_ref, span_ref, span_text, span_kind, provenance}` の**決定論テーブル**。Stage 1 が `evidence_declarations` と解決済み本文から生成し、`span_kind` も Stage 1 が決定論分類する（§S-3 拒否条件 #5）。生成後は **immutable**。**S-3 の照合基準**であり、LLM はここから**選択するだけ**（採番させない） | invoke 時 |
| `criteria_conditions` | `list[dict]` | 当該版の例外承認基準の条件表（`condition_id` の実在照合基準） | invoke 時 |
| `cited_source_ids` | `list[str]` | evidence-gate 契約キー。**検証を通過した claim の `source_ids` から post_process が再計算**する（LLM の自己申告値は使わない） | — |
| `review_packet` | `dict` | Stage 3 のドラフト（6 区分） | — |
| `evidence_map` | `list[dict]` | evidence-gate が検証する claim → source の対応マップ | — |
| `refine_action` | `str` | HITL resume で確定した経路（`confirm` / `refine` / `discard`）。**自由文から LLM に解釈させず、決定論的に正規化した単一欄で route する**（§HITL 方針） | HITL 時 |
| `refine_count` | `int` | `refine` の累計回数（**テンプレ所有**。`max_hitl` の実効的な強制に使う。framework 所有の `hitl_count` とは別欄 — §HITL 方針） | ✅（`/invoke` 時に **0 で初期化**。未設定を 0 とみなす暗黙依存を作らない） |
| `hitl_draft` | `dict` | interrupt 前のドラフト（再開時に LLM を再実行しないための idempotency guard。framework が許可する 7 フィールドの 1 つ） | HITL 時 |
| `hitl_feedback` / `hitl_metadata` / `hitl_status` / `hitl_count` / `hitl_allowed` / `subgraph_thread_id` | — | **framework 管理**。`hitl_count` と `hitl_allowed` は**テンプレから書かない** | — |
| `requires_human_review` | `bool` | Stage 4 が不整合を検出したときに立てる。envelope では **`meta.requires_human_review`** として出る（top-level には出さない） | — |
| `degraded` | `bool` | 縮退したか（envelope の `meta.degraded` に載る） | — |
| `degradation_code` | `str` | 縮退理由の**有限 enum**（`kb_empty` / `llm_unavailable` / `discarded_by_reviewer` / `hitl_limit_reached`）。`status` は `success` のまま。**S-3 の enum（§S-3 出力安全契約）と同一集合**であり、片方だけに値を足すと拒否条件 #11 で落ちる | — |
| `ingest_summary` | `dict` | `written_ids` / `rejected[]` | ingest 時 |
| `feedback_summary` | `dict` | 反映件数・監査イベント ID | feedback 時 |
| `error_code` / `error_type` | `str` | LLM/provider 等の失敗を自由文なしの安定メタデータで返す | error 時 |

**State Constraints（必須）:**
- flat TypedDict のみ（primitive + JSON 直列化可能な型）
- JWT / API key / credential を State に置かない（checkpoint に平文で残る）
- `InvocationContext` を State に入れない（`InvocationContext.from_state(state)` で再構成する）
- Pydantic / dataclass / 任意 Python オブジェクトを置かない（msgpack 非互換）
- `hitl_count` はテンプレから書かない（framework 専用）
- `node_history` / `error_log` は reducer（`operator.add`）— `execute()` は**変更したキーだけ返す**
  （full state 返却は履歴を二重化する）
- **機微本文（自由記述）は State に載せない**。`payload_ref` のみを載せる（§機微データの 2 層統治）

> 🔴 **参照 ID は英字のみで発行する。** framework の S-2 ゲート（`FunctionNode._security_gate_input`・`@final`）は
> `_PII_SCAN_FIELDS`（`user_input` / `validated_input` / `llm_response`・SDK 1.0.1 実機確認）を `detect_pii` で走査し、
> 検出範囲を **`[MASKED]` に破壊的置換**する。`uuid4()` のように数字を含む ref は電話番号等として誤検知され
> 破壊される既知欠陥がある（実測 0.58% の本番失敗）。`request_ref` / `payload_ref` / `history_ref` は
> 英字のみの符号化とする（parts `payload-store` v0.4.0 と同方針）。

### 内部 envelope の伝送（MR-B の実機で確定）

🔴 **`BaseGraph.invoke()` は `user_input` を「ノードが 1 つも走る前」に State へ載せ、
`InitializeNode` 自身が `FunctionNode` なので S-2 の PII マスクが `pre_process` より先に走る。**
したがって射影パケットを JSON 文字列のまま `user_input` / `validated_input` に置くと、
そこに載る識別子が `detect_pii` の誤検出で破壊される。危険なのは自分が発番する ref だけではない —
本テンプレは `criteria_version_ref` / `doc_ref` / `span_ref` という**顧客が命名するフィールド**を運ぶため、
形を制御できない（`EVT-04-12-2024` は `phone_jp`、12 桁連番は `my_number_jp` として検出される。
SDK 1.0.1 実測）。

**採る形 — ref indirection（両ホップで必要）:**

| ホップ | 実装 |
|---|---|
| 外側 | 入口 adapter が envelope を transient store に put し、**数字を含まない 32 文字**の handle を得て `invoke(user_input=<ref>)` を呼ぶ。`pre_process` が解決し**カスタム typed キーにのみ**展開する（`validated_input` には書かない） |
| 内側 | `GraphNode.extract_input()` の戻り値は内側 `BaseGraph.invoke()` の `user_input` になり、内側 `InitializeNode` が再度マスクする。よって**内側にも ref を渡し**、inner の入口ノード `dispatch` が解決する |

32 文字の英小文字列はどの PII パターンにも一致せず S-2 を byte-identical で通過する。
digit-free 符号化でも塞げるが、ref の方がサイズが増えず S-1 の入力サイズ制限からも独立する。

**この結果、inner のトポロジは `START → dispatch → {route} → 各モード` になる**（mode 自体が
envelope の中にあるため、mode 分岐は dispatch の後）。

### 出力契約（`formatted_output` envelope）

```yaml
formatted_output:
  mode: "invoke"                                    # 必須
  gate_kind: "freeze_exception_evidence"            # 必須（evidence-gate の宣言）
  meta:                    { degraded, degradation_code, requires_human_review }      # 閉じた 3 キー
  applied_pins:            { criteria_version_ref, criteria_version, criteria_content_hash,
                             freeze_calendar_ref, freeze_calendar_version, freeze_calendar_content_hash }
  criteria_conditions:     [ { condition_id, criteria_version_ref, condition_ref } ]   # 申請 evidence の source_id は持たない
  evidence_sources:        [ { source_id, doc_type, quote_span, span_kind, provenance } ]
  freeze_window_facts:     { freeze_calendar_ref, planned_window, overlapping_windows, evaluated, evaluated_at }
  supported_conditions:    [ { claim_id, description, condition_id, source_ids, severity: "candidate", confidence: float | null } ]
  evidence_gaps:           [ { claim_id, description, condition_id: str | null, source_ids, gap_origin } ]
  contradictions:          [ { claim_id, description, condition_id: str | null, source_ids } ]
  approval_scope_discrepancies: [ { claim_id, description, source_ids } ]
  precedent_checks:        [ { history_ref, condition_id, prior_issue, prior_outcome, status, source_ids } ]
  verification_questions:  [ { claim_id, question, condition_id: str | null, source_ids } ]
```

> **HITL 保留時の応答は envelope ではない。** `interrupt()` が発火した invoke / resume は
> `status: "awaiting_human"` と `thread_id` を返す（`formatted_output` は未確定なので載せない）。
> クライアントはその `thread_id` を `/resume` に echo する。S-3 の closed allowlist は
> **確定パケット（`status: success`）にのみ適用**する。
>
> `get_output()` は `formatted_output` → `result` の順で `output` を拾う（SDK 1.0.1 実体で確認）。
> したがって最終 envelope は **`formatted_output`** に載せる。
>
> 縮退（KB 空振り、または LLM injection seam 自体が未設定）は `meta.degraded` / `meta.degradation_code` で伝える。
> 注入済み LLM の provider/auth/config/timeout/runtime 失敗は縮退させず
> `status="error"` + `error_code="llm_call_failed"` で fail-closed にする。
> **top-level に `degradation_reason` のような追加キーを生やさない**（S-3 の closed allowlist が reject するため）。
> 各ネストオブジェクトの exact-key 定義は §S-3 出力安全契約を正とする。

**出力スキーマに持たないフィールド（責任境界の構造排除）:**
`approval_verdict` / `exception_approved` / `risk_acceptance` / `change_permissible` /
`customer_approval_covered` / `coverage_verdict` / `sla_liability` / `recommendation` / `next_action`

> **`approval_scope_discrepancies` が「カバレッジ判定」に化けないための契約（設計レビュー指摘 2026-07-24）:**
> 本フィールドは**不一致・未確認事項の列挙のみ**であり、「カバーされている / されていない」という肯定・否定の
> 結論を持たせない。したがって `covered: bool` のような判定キーを要素に**持たせない**（S-3 拒否条件 #3 が
> ネストまで再帰して reject する）。「顧客承認が影響範囲をカバーしているか」という問いに対する出力は、
> 常に「参照された顧客承認の記載範囲と申請影響範囲の差分」と「確認質問」に限られる。

## 凍結カレンダーと時間窓の決定論契約（本テンプレ固有の核）

凍結例外レビューは「**その変更が凍結期間に当たるか**」という時間判定を必ず含む。ここは決定論であり、
LLM に渡さない。他テンプレートで実際に踏んだ 2 つのバグを設計時点で塞ぐ。

| # | 契約 | 理由（実測） |
|---|---|---|
| 1 | **ISO-8601 の文字列比較を禁止**。`planned_window` / 凍結カレンダーの区間は `datetime.fromisoformat()` で **TZ-aware な `datetime` に変換してから**比較する。オフセット省略の入力は **reject**（`format_check` に理由を出し `field_gaps` に積む） | `"2026-08-20T09:00+09:00" < "2026-08-20T10:00Z"` のような比較は辞書順で静かに逆転する（SupplyDataQualityPrecedentAgent MR-B 自己検出） |
| 2 | **基準日時（"now"）は entry adapter が TZ 付き ISO-8601 で 1 回確定**し、**State の `evaluated_at` として運ぶ**（`validated_input` に射影して inner へ渡る）。ノード内で `datetime.now()` を呼ばない。**config には置かない** — scaffold の `src/api/server.py` は module レベルで `agent = Graph(config=_config)` を 1 回だけ構築するため、リクエストごとに config を差し替える経路が存在しない（SDK 1.0.1 実機確認）。`evaluated_at` 欠落は fail-closed（`error_code="evaluated_at_missing"`） | リクエスト内で now が変わる／ホストの TZ で結果が変わると、同じ申請が実行ごとに違う判定になる（決定論性チェック 5 問の 3 番目）。config 経由にすると**そもそも実装できない** |
| 3 | 出力は **`freeze_window_facts`（事実）のみ**。`overlapping_windows[]` に「申請の時間窓と重なる凍結期間の `window_id` / `window_ref` / 重なり区間」を列挙する。「凍結期間中なので承認不可」のような**判定は出さない** | 判定を出した瞬間に §出力契約 の構造排除リストと §S-3 出力安全契約 拒否条件 #3 を破る |
| 4 | 凍結カレンダーが**未指定・未取込**の場合は `evaluated: false` + `overlapping_windows: []` + `freeze_calendar_ref: null` を `freeze_window_facts` に立て、`evidence_gaps` に「凍結カレンダー未提供」を `gap_origin: "deterministic"` で積む。**判定できた場合のみ `evaluated: true`** とし、重なりが無いときも `evaluated: true` + `overlapping_windows: []` を返す | 「重なりゼロ」と「そもそも判定していない」を同じ形で返すと、読み手が「凍結期間外」と誤読する。`evaluated` はこの 2 つを機械判別可能にする唯一のキー |

**ロールバック所要時間の扱い（数値ガード）:** Stage 3 は「ロールバック計画に記載された所要時間」を
`claim` として抽出するだけで、**凍結窓に間に合うかの計算はしない**。Stage 4 が以下を決定論で検証する:

**正規化の契約（安全性の要件であって実装形の指定ではない — 下の性質を満たせば実装は任意）:**

| 項目 | 契約 |
|---|---|
| 前処理 | NFKC 正規化 → ゼロ幅除去 → 漢数字（〇一二三四五六七八九十百千 + 万）をアラビア数字へ変換 |
| 対象の量 | **時間・期間・件数の 3 種のみ**（§S-3 拒否条件 10-b と同一スコープ）。金額・ID 断片・バージョン番号は対象外 |
| 単位の正規形 | 時間 → 分（`時間`/`h`/`hr`/`hours` = 60、`分`/`min` = 1、`日` = 1440）。件数 → 無次元（`件`/`台`/`本`/`ユーザ`/`ノード`） |
| 端数表現 | `半` は 0.5 単位として扱う（`2時間半` = 150 分）。`約` `およそ` `程度` `以内` `以上` は**接頭辞として除去**し、数値のみを比較する（範囲解釈はしない — 「約 2 時間」と「2 時間」は同一値として扱う） |
| 比較 | **完全一致**（正規形に落としたうえでの数値一致）。許容誤差は設けない — 誤差を認めると「4 時間」を「4.5 時間」と書いても通る |
| 未対応表現 | 上記で正規化できない数値表現（範囲 `2〜4時間`、相対 `前回より短い`）は **抽出しない**。抽出しない＝照合対象外であり、**PASS ではなく「その数値については検証していない」**として `requires_human_review` を立てる |

> 🔴 **`\w` 境界を要求する正規表現を使わない。** 「30分以内」「約2時間半」のように単位・助詞が数値に
> 隣接すると、`\w` 境界を要求する抽出器は空集合を返し、**ガードが常に PASS する**（実測）。
> 受入テストに `999999円`（対象外）・`４時間`（全角）・`四時間`（漢数字）・`2時間半`・`30分以内` を必ず入れる。

- 抽出した数値が **申請本文（`source_registry` の `span_text`）に実在する**こと
- 数値の抽出・照合は **NFKC 正規化 → 漢数字変換 → 日本語単位の同義語展開**を前段に置く
  （`４時間` / `四時間` / `4h` / `240分` が同一に落ちること）。
  **`\w` 境界を要求する正規表現を使わない** — 「30分以内」「約2時間半」のように単位・助詞が隣接すると
  抽出器が空集合を返し、ガードが常に PASS する（Professional-Services Engagement Estimation-Phase Agent 実測）
- 数値が本文に無い、または一致しない場合は当該 claim を **drop** し `requires_human_review: true` を立てる

## `evidence_declarations[]` の要素契約（closed schema）

`/invoke` が受ける証跡宣言は **exact-key**。宣言外キー・型違い・enum 外は **400**（黙って捨てない）。

```yaml
evidence_declarations[]:
  doc_type: enum          # change_request | approval_record | rollback_plan | risk_assessment
                          # | test_evidence | monitoring_evidence | vendor_notice | other
  doc_ref: str            # 顧客側の文書参照（空文字不可）
  spans:                  # 1 件以上
    - span_ref: str        # 文書内の位置参照（空文字不可）
      span_text: str       # 当該位置の逐語テキスト（空文字不可）
```

**`source_registry` の生成（Stage 1・決定論）:** document / span の 1 始まり ordinal から英字のみの
`source_id`（例: `SRCAA`）をテンプレ側で採番し、各 span について
`source_registry[source_id] = {doc_type, doc_ref, span_ref, span_text, span_kind, provenance}` を作る。
`span_kind` は §`span_kind` の決定論分類で付与し、`provenance` は
`{request_ref, source_id, doc_ref, span_ref, criteria_version_ref}` を決定論で埋める。
**生成後は immutable** であり、LLM はここから `source_id` を**選ぶだけ**（採番も `span_kind` の自称もできない）。

State 展開前に `span_text` を **NFKC 正規化 → `detect_pii` → 指示形評価 → `span_kind` 分類**の順で処理する。
指示形は申請全体を reject せず、当該 span を固定プレースホルダへ置換して `unclassified` に落とし、
minted `source_id` のみを示す deterministic gap と `requires_human_review: true` を出す。

**受入テスト:** 正常な nested 宣言 / document・span の宣言外キー / `doc_type` enum 外 /
空の `spans` / 空の `span_text` は 400。安全な `span_text` は byte-identical で prompt に到達し、
PII・指示形マーカーは prompt、全 checkpoint、最終出力のいずれにも到達しないことを spy LLM と saver 列挙で測る。

payload store から Stage 3 が局所解決する申請本文 4 フィールドも、model 呼出し前に
**NFKC 正規化 → `detect_pii` → 指示形評価**する。指示形を含むフィールドは部分削除ではなく
`[INSTRUCTION WITHHELD]` へ全体置換し、フィールド名だけを記した deterministic gap を追加して
`requires_human_review` を立てる。正常フィールドは byte-identical で model に渡し、raw / safe の
どちらも State へは書かない。

## `span_kind` の決定論分類（引用可否の判定規則）

引用可否を決める `span_kind` は **Stage 1 が決定論で分類**して `source_registry` に載せる。
**LLM に「これは事実か」を聞かない** — 結論文を事実と誤答した瞬間に穴が開くため。
正本実装は **MunicipalContractVariationEvidenceBriefAgent `src/services/service.py` の `classify_span_kind`**（released 実績あり）。
MR-B はその構造を写経し、**マーカー語彙だけを本ドメイン（変更凍結例外）に置き換える**。

**判定順序（fail-closed。上から評価し、最初に該当したもので確定する）:**

| # | 判定 | 結果 | 本ドメインのマーカー例 |
|---|---|---|---|
| 0 | **正規化** — NFKC + ゼロ幅文字除去 + 空白正規化を**必ず前段**に置く | — | 全角/半角・ゼロ幅スペース挿入による素通りを塞ぐ |
| 1 | **裁定マーカー**を含む | `adjudicative`（引用不可） | 承認、却下、差し支えない、認められる、許容、不許容、妥当と判断、問題ない、可とする、不可とする、例外として認める |
| 2 | **意見マーカー**を含む | `opinion`（引用不可） | と解される、と考えられる、所見、見解、評価する、判断する、助言、推奨 |
| 3 | **評価的文型**を含む（語ではなく**文型**で落とす） | `unclassified`（引用不可） | 〜はない、〜ではない、〜できる、〜が相当、〜が適当、支障はな、問題はな、リスクは低、範囲内、許され |
| 4 | **複文**（句点・接続語で切れる）または **`max_quotable_chars` 超過** | `unclassified`（引用不可） | 句点 `。` `．` 改行、したがって、よって、ゆえに、〜ため、／ 長さ上限は `config` の `max_quotable_chars` |
| 5 | 日時パターン（ISO-8601 / 和暦 / 年月日時分） | `date`（引用可） | `2026-08-20T22:00+09:00`、`2026年8月20日 22時` |
| 6 | 数量パターン（数字 + 単位 / 通貨） | `quantity`（引用可） | `4時間`、`120台`、`¥1,200,000` |
| 7 | 識別子パターン（英数記号の符号） | `identifier`（引用可） | `CHG-0001`、`SRV-A`、`WIN-2026Q3-01` |
| 8 | 観測条件の記述（状態・構成・手順の事実記載） | `observed_condition` / `procedural_record`（引用可） | 「本番 DB は 3 ノード構成」「手順書 §4.2 に切戻し手順が記載されている」 |
| 9 | 上記のいずれにも該当しない | **`unclassified`（引用不可）** | 既定値が引用不可＝分類器の知らない文型は自動的に落ちる（denylist と逆向き） |

> 🔴 **日時を数量より先に見る（MR-B の実装で確定）。** `2026年8月20日` は数量パターンの
> `\d+日` にも一致するため、数量を先に評価すると**日付がすべて `quantity` に化ける**。
> 正本実装（MunicipalContractVariationEvidenceBriefAgent）も日時が先で、そちらが正しい。
>
> 🔴 **3 が 5〜8 より前にあることが要点。** 「支障はない」は事実マーカー（数量・日時）を含んでいても
> 引用不可でなければならない。評価的文型を語の削除で対処しない — 「支障」は正当な事実記述にも出る。
> 4 も同様で、「切戻しに 4 時間。したがって凍結窓内で完了できる」を `quantity` として通すと、
> 事実節に紛れて裁定節が出力に載る。
>
> **受入テスト（AT-3 に含める）:** 上記 9 分岐それぞれの代表入力 + 全角化・ゼロ幅挿入した回避入力 +
> 「事実節と裁定節が 1 span に同居」の複文。**変異検査**として、判定順序 3 を 5 の後ろに移すと
> 「支障はない」が `quantity` として引用可になることをテストが検出できること。

## 統制文書の版固定入力（例外承認基準・凍結カレンダー）

🔴 **例外承認基準と凍結カレンダーは、AG が `ingest` で蓄積して検索で想起する対象ではない。**
これらは顧客側で**所有者・承認・有効期間を持つ版管理された配布物**であり、`invoke` の
**版固定入力**（`*_ref` + `*_version` + `*_content_hash`）として渡す。理由は 3 つ:

1. **実務の形がそうである** — 変更凍結カレンダーも例外承認基準も、MSP の変更管理プロセスで
   承認・配布される管理対象文書であり、「似た基準をベクトル検索で拾う」対象ではない
2. **監査が「どの版を適用したか」を要求する** — 承認基準が改訂されれば、前回の `supported_conditions` は
   新版に対する判定ではない。適用版が出力に残らないとレビュー記録が再現できない
3. **AI が判定基準を自動更新してはならない** — `/feedback` で得た知見を現行の基準表へ自動反映すると、
   実質「AG が承認基準を作っている」ことになり、変更管理の統制として成立しない

| 成果物 | 扱い |
|---|---|
| 例外承認基準（条件表）・凍結カレンダー | **版固定入力**。入口 adapter が顧客の正本レジストリから解決し、`content_hash` 一致・承認済み・有効期間内を検証してから**不変シール参照**で graph に渡す |
| 当該申請の証跡（`evidence_declarations` と本文） | `/ingest` および `/invoke` で取り込む。**RAG / 照合の正当な適用範囲** |
| 過去の例外レビュー（3 点セット） | 参照可。ただし**結論は継承しない**（§過去事例の 3 点セット） |
| CAB の確定フィードバック | **append-only の候補キュー**（`perspective_candidates`）。次版の基準表への昇格は **AG 外の人間作業**。AG は基準表への書込権限を持たない |

**pin は中身ではない。** `*_ref` + `*_version` + `*_content_hash` は**同一性の証明**であって評価可能な内容ではない。
入口 adapter が正本から実体を解決し、以下をすべて検証してから渡す — 1 つでも満たさなければ **fail-closed**:

| 検証 | 失敗時の `error_code` |
|---|---|
| `criteria_version_ref` が指定されている | `criteria_version_missing` |
| 正本レジストリに当該 `ref` + `version` が実在する | `criteria_version_unknown` |
| 解決した実体の hash が `criteria_content_hash` と一致する | `criteria_content_hash_mismatch` |
| その版が**承認済み**かつ `evaluated_at` 時点で**有効期間内** | `criteria_version_not_effective` |
| （指定された場合）`freeze_calendar_ref` について上記と同じ 4 検証 | `freeze_calendar_unknown` / `freeze_calendar_content_hash_mismatch` / `freeze_calendar_not_effective` |

- `freeze_calendar_ref` は **任意**。省略時は `freeze_window_facts` を `{freeze_calendar_ref: null, evaluated: false,
  overlapping_windows: [], planned_window: <入力値>, evaluated_at: <State の evaluated_at>}` で返し、
  `evidence_gaps` に「凍結カレンダー未指定」を `gap_origin: "deterministic"` で 1 件積む。**invoke 自体は成功する**。
  `null` は §S-3 出力安全契約 の型 allowlist に収載済み。**指定された場合は上記 4 検証を通す**（省略と偽 pin は別物）。
- `condition_id` に値がある場合は**当該版の条件表に実在するものだけ**が出力に載る（S-3 拒否条件 #6）。
  `evidence_gaps` / `contradictions` / `verification_questions` は、申請全体に関わり特定条件へ
  紐づかない論点に限り `null` を許す。`supported_conditions.condition_id` は定義上必須であり `null` 不可。
- 適用した版は **`applied_pins` として出力に必ず載せる**（§出力契約）。これが無いと「どの基準で評価したか」が
  外部に残らず、レビュー記録として成立しない。欠落は S-3 拒否条件 #1（top-level 完全一致）で落ちる。
- 基準の解釈・改訂は顧客の変更管理プロセスの所掌であり、AG は版を**選ばない**（人間が指定する）。

**`sealed_refs` の契約（producer / consumer / lifetime / failure）:**

| 項目 | 契約 |
|---|---|
| producer | **入口 adapter のみ**。4 検証をすべて通した実体に対して、その**リクエスト限りの**シール参照を発行する |
| consumer | **Stage 1 のみ**。`ResourceScope.registry.resolve_sealed(ref)` で実体を取得し、条件表を `criteria_conditions` に、凍結窓を `freeze_window_facts` の算出に使う。Stage 2/3/4 は解決済みの構造化結果だけを見る（本文を再取得しない） |
| lifetime | リクエスト内のみ。**State に載るのは参照だけ**で実体は載せない（本文が checkpoint に流れるのを避ける）。応答にも載せない（`applied_pins` に載るのは pin であってシールではない） |
| failure | `criteria_sealed_ref` の解決失敗は **Stage 1 到達前に fail-closed**（`criteria_seal_unresolved`）。`freeze_calendar_sealed_ref` は **pin 省略時のみ `None`** が正当で、pin があるのに解決できない場合は `freeze_calendar_seal_unresolved` で fail-closed（`evaluated: false` に**縮退させない** — 省略と解決失敗を同じ形にしない） |

> 🔴 **これは「KB に何も貯めない」という意味ではない。** 版固定入力にするのは
> **判定基準そのもの**（承認基準の条文・凍結カレンダー）だけであり、
> **過去の例外レビューの判断材料は KB に蓄積して想起する** — それが本テンプレの蓄積 moat の本体である
> （§過去判例の照合）。線引きは次のとおり:
>
> | 版固定入力（KB に貯めない） | KB に蓄積する |
> |---|---|
> | 例外承認基準の条文（`condition_text`） | 過去の例外レビューの**論点**（何が問題になったか） |
> | 承認済み凍結カレンダーの窓定義 | 過去の例外レビューの**結論**（承認 / 却下・事実として） |
> | — | 当該申請の証跡（`case_evidence`） |
> | — | CAB の確定フィードバック（候補キュー） |
>
> **理由の違い:** 基準の条文を KB から検索で拾うと「似た条文」が当たるだけで**適用すべき条文**にならず、
> どの版を適用したかも残らない。一方、過去の判断材料は「似た事例」を拾うことに意味がある
> — それが人間のレビュアーがやっていることそのものだからである。

### outer `get_output()` の override（MR-B で確定）

`AgentBaseGraph.get_output()` は `formatted_output or result` を返し、`error_code` を落とす。
本テンプレは 2 点を変えて override する:

| 変更 | 理由 |
|---|---|
| **`result` フォールバックを持たない** | S-3 が raise すると当該ノードの戻り dict は破棄され、フォールバックが**前段の `result` を出す** — ゲートが拒否した直後にゲートを迂回した値が返る（実測された経路） |
| **`error_code` / `error_type` と HITL の `thread_id` / `hitl_metadata` を通す** | 既定では `criteria_version_unknown` も `payload_expired` も呼出側には無印の "error" として届き、運用が原因を区別できない。**`error_log` は通さない**（provider 由来の文字列が混じりうる） |

## Framework Utilization

### Shared Components Used
- [x] `InvocationContext`（`correlation_id`, `session_id`, `thread_id`, `parent_trace_id`, `caller_id`,
      `caller_trust_level`, `secrets`, `hitl_allowed`）
      — **`tenant_id` / `org_id` は存在しない**（SDK 1.0.1 実機確認）。顧客識別は `caller_id` からの認可解決による。
      `session_id` はクライアント供給値をそのまま echo するため、**顧客ごとのデータをスコープする鍵に使わない**
- [x] S-2: `_extra_security_gate_input()` — mode allowlist・顧客照合 fail-closed・パケット形状検証・
      入力品質ゲート・受領自由記述への `detect_pii()` 明示適用
- [x] S-3: `_extra_security_gate_output()` — 出力の closed exact-key schema・claim→source 実在・逐語/数値照合・
      返却自由記述への `detect_pii()` 明示適用
- [x] S-4: `emit_trace_event()` — 各 `execute()` 内で最低 1 回（`node_start` / `node_complete` / `node_error` は
      framework が `__call__()` で出すので**重複させない**）。**成功経路だけに置かない**（下記）
- [x] `framework.security.detect_pii` / `detect_credentials` — `FunctionNode` が自動実行する。
      **自前の PII/credential 正規表現は書かない**（substrate 再利用の原則）
- [x] `BaseLLM` — **`from shared.services.llm.base_llm import BaseLLM`**（パッケージ root の `shared.services.llm` は
      何も re-export しない。SDK 1.0.1 実機確認）。LLM は `config["llm"]` の注入シームのみ。
      モデル ID / 具体クライアントを `src/` に書かない（テンプレート標準の既定）

> **S-4 の配置（成功経路だけに置かない）:** 以下すべてでドメインイベントを出す —
> ①`invoke` 正常完了 ②入力 reject（形状・品質ゲート・未知 mode）③顧客解決失敗（fail-closed）
> ④`ingest` の行 reject ⑤LLM 呼出失敗 ⑥縮退（`kb_empty` / `llm_unavailable`）
> ⑦Stage 4 の claim drop / `requires_human_review` 立ち上げ ⑧`feedback` の承認なし reject（および候補 append の成功）
> ⑨HITL の suspend（`interrupt()` 発火）・resume（`action` ごと）・discard・直接 graph resume における
> **サニタイザ reject**・`hitl_limit_reached` の打ち切り。HTTP preflight reject は graph/checkpoint を
> 実行しないため、gateway / HTTP access log 側で記録する
> ⑩削除（`deleting` 遷移・完了・部分失敗）と期限到来 purge の実行結果。
> 失敗・降格・縮退が監査に残らないと、後段の運用が「何も起きていない」と読む。

> **S-2 / S-3 の framework 実挙動（SDK 1.0.1 実機確認・設計の前提）:**
> - S-2 の PII マスクは `user_input` / `validated_input` / `llm_response` の**3 フィールドのみ**。
>   injection 評価は `user_input` / `validated_input` のみ。それ以外の受領フィールド
>   （`evidence_declarations[]` の自由記述など）は**框の外**なので、テンプレ側フックで明示的に扱う
> - S-3 の既定検査は **credential のみ**（`detect_credentials_in_value` は dict/list/tuple を再帰する）。
>   **PII マスクは出力側で一切走らない**。テンプレ側は path ごとの exact-key schema と
>   PII 残存検査を `_extra_security_gate_output()` で行う
> - `_extra_security_gate_output(result)` は **`state` を受け取らない**。mode 別の分岐は
>   `result["mode"]` の**宣言値**で行う（`execute()` が必ず載せる）。キーの有無で分岐すると fail-open になる

> **S-2/S-3 のノード種別ごとの挙動（ADR-017）:**
> - `FunctionNode` サブクラス → framework の `@final` ゲートが必ず走る。拡張は `_extra_*` フックのみ
> - `GraphNode` → 意図的な no-op（上流/子側のゲートが適用済み）
> - `_security_gate_input/output` を直接定義すると **import 時 `TypeError`**

> **非結論性は「構造」で担保し、自由文の denylist に依存しない。**
> 禁止語・禁止フレーズの正規表現は言い換えで原理的にすり抜けるため**持たない**（テンプレート標準の既定）。
> 詳細は §S-3 出力安全契約。

### Composition Pattern

- **Pattern**: `GraphNode`（in-process subgraph）
- **Composition target**: `FreezeExceptionWorkflowGraph`（`src/graph/domain_workflow_graph.py`）
- **`error_strategy`**: `"handle"` — inner の例外を**安定した error code に射影**してから外へ返す。
  部分結果は返さない（`review_packet` を欠いた成功を作らない）。provider 例外の `str(exc)` は
  **State / trace / 出力に入れない**（絶対パス入りトレースバックが載る）
- **`propagate_hitl`**: **`True`** — 内側 `hitl_review` の `interrupt()` を外側 `AgentBaseGraph` へ伝播させる。これに伴い **outer の config にも `hitl` / `memory_enabled` が必須**（`invoke()` が thread_config を付ける条件）
- **abstract 3 メソッド**: `get_subgraph()`（lazy + キャッシュ）/ `extract_input()`（inner envelope の opaque ref だけを渡す）/
  `merge_output()`（**実行されたモードのフィールドだけ返す**・`node_history` は返さない）

> `get_subgraph()` は `self._subgraph is None` のときだけ生成してキャッシュする
> （毎 execute で新規生成すると resume が収束しない）。**キャッシュしてよいのはトポロジのみ**
> （顧客専用接続は §`AuthorizedResourceResolver` のとおり呼出ごとに解決）。

## HITL 方針

**HITL を採用する（`hitl.enabled: true` / `memory_enabled: true`）。方式は D6 `interrupt()`。**

要否は「人間の判断が要るか」では決めない — **グラフの実行を止めて、同じ実行の途中から再開する必要があるか
（in-graph suspend / resume）**で決める。4 問すべて Yes なら HITL:

| # | 問い | 本テンプレ |
|---|---|---|
| ① | 人間の応答を待つ間、そのグラフ実行を生かしておく必要があるか | **Yes** — Stage 3 が生成したドラフトを保持したまま直したい。呼び直すと LLM が別の内容を出し、「そのドラフトへの修正」にならない |
| ② | 再開時に中断前の State をそのまま引き継ぐ必要があるか | **Yes** — `source_registry`（immutable な引用照合基準）・`recall_refs`・`freeze_window_facts` を作り直すと、同じ根拠に対する修正であることが保証できない |
| ③ | 応答が同一セッション内・比較的短時間で返るか | **Yes** — 対話するのは**申請を CAB に上げる担当者**であり、画面の前にいる。数分〜数十分 |
| ④ | 応答が来るまで後続ノードが実行できないか | **Yes** — Stage 4 の検証とパケット確定は、確定したドラフトに対して行う |

> 🔴 **「CAB の承認判断」と「担当者との対話」を混同しない。** CAB が例外を承認するかの判断は数時間〜数日かかり、
> AG の実行を跨ぐ別トランザクションである（AG はそもそも承認可否を出さない）。HITL が対象にするのは
> **その手前**、担当者がレビューパケットを仕上げる工程の往復である。前者を根拠に HITL を不要と判断するのは
> 判定軸の取り違えであり、実際に初版でその誤りを犯した（§Design Decision Record に記録）。

> 🔴 **`/feedback` は HITL の代替ではない。** `/feedback` は**レビューが確定した後**に、その判断を次回のために
> 蓄積する入口（`perspective_candidates` への append）であり、**実行中の対話・差し戻しを受ける経路ではない**。
> 両者は目的も時間軸も別で、片方がもう片方を代替しない。

### interrupt の位置と分岐（経路で縛る）

内側グラフの **Stage 3 と Stage 4 の間**に `hitl_review` ノードを置く。

```
Stage 3 (LLM ドラフト) → hitl_review → {refine_action} → Stage 4 (検証) → END
                              ↑                ↓ refine
                              └────────────────┘
```

| `refine_action` | 動作 |
|---|---|
| `confirm` | Stage 4（検証）へ進む |
| `refine` | Stage 3 へ戻り、担当者の指示を追加文脈として**再ドラフト**する |
| `discard` | 破棄して END（`status="success"` + `meta.degradation_code: "discarded_by_reviewer"`。error ではない） |

🔴 **暴走防止は回数ではなく経路で行う。** Cat 2（`AgentBaseGraph` + `GraphNode` + D6）には
**`hitl_count` の circuit breaker が存在しない** — `hitl_count >= max_hitl` を見る実体は Cat 3 の
`ToolActNode` だけで、`AgentBaseGraph` は `hitl.max_hitl` を**正の整数か検証するだけ**（1.0.1 実機確認）。
したがって `max_hitl` の値をいくら小さくしても Cat 2 では何も止まらない。実効的な歯止めは
**`refine_action` を決定論的に正規化した単一欄にし、`refine` のときだけ再ドラフト経路に入ること**である。
自由文の LLM 解釈で再生成ループへ落とさない。
（`max_hitl` の宣言自体は HITL 設定契約の必須キーなので残す — PB-7 stub と AgentRegistry が読む。）

🔴 **ただし「経路の決定論性」は「回数の上限」ではない。** 上の歯止めが防ぐのは*不正な値による暴走*であって、
**正当な `refine` の反復は何度でも成立する**。`max_hitl` を宣言しておきながら実挙動は無制限、という
宣言と実装の乖離が残るため、**回数はテンプレ側で強制する**:

| 項目 | 契約 |
|---|---|
| 意味 | `max_hitl` = **再ドラフト（Stage 3 の再実行）を許す最大回数**。interrupt の総数ではない |
| カウンタ | State の `refine_count: int`。**初期値 0**（`/invoke` 時に必ず 0 を置く。未設定を 0 とみなす暗黙依存を作らない）。**テンプレ所有**であり、framework 所有の `hitl_count` はテンプレから書かない |
| 判定順序 | `/resume` が `action: "refine"` を受理したら、**加算する前に** `refine_count >= max_hitl` を判定する。<br>・**未到達** → `refine_count += 1` して Stage 3 へ戻す（再ドラフト実行）<br>・**到達済み** → Stage 3 へ戻さず、**現在のドラフトをそのまま Stage 4 へ送って確定する** |
| 具体例（`max_hitl: 2`） | 1 回目の `refine` → count 0→1・再ドラフト実行／2 回目 → count 1→2・再ドラフト実行／**3 回目 → count は 2 のまま・再ドラフトせず確定**（＝ AT-26） |
| 打ち切り時の挙動 | **interrupt せず** `refine_action: "confirm"` 相当の経路で Stage 4 へ進む（fail-closed）。破棄ではない — 上限に達したことを理由に対話の成果を捨てない |
| 打ち切り時の envelope | `status: "success"`。`meta = {degraded: true, degradation_code: "hitl_limit_reached", requires_human_review: true}`。**他の `INVOKE_TOP_LEVEL_KEYS` は通常の Stage 4 出力として完全に充足する**（`discard` と違い検証は通っているので空配列にしない） |

**受入テスト:** `max_hitl` を 2 に設定して `refine` を 3 回送り、**1・2 回目は再ドラフトが実行され、3 回目は interrupt せずに** `success` + `hitl_limit_reached` へ抜けること（`refine_count` は 2 で止まる）。カウンタ検査を外すと無限に suspend し続けることを変異検査で確認する。

**`hitl_count` はテンプレから書かない**（framework 専用フィールド）。

### `hitl_allowed` ガード（必須）

```python
if not state.get("hitl_allowed", True):      # 自動パイプライン（親 GraphNode 経由）
    return {"refine_action": "confirm"}      # interrupt せず Stage 4 へ直行
feedback = interrupt({"draft": ..., "reason": "reviewer confirmation"})
```

**ガードのない `interrupt()` は本番デッドロックを起こす blocking defect。**
ただし **resume 経路では framework が `hitl_allowed` を参照しない**ことが実機で判明している
（`GraphNode.execute()` の resume 分岐・`BaseGraph.resume()` が `InvocationContext` を取らない）。
テンプレ層で塞げる範囲は**初回 invoke 経路のガードまで**であり、
「suspend 後に親が HITL 不可へ変わって resume」は framework 側の課題として 運用ガイド に既知の限界として記す。

### resume 応答のサニタイズ（S-2 は resume 経路を通らない）

`interrupt()` が返す値は **S-2 の入力ゲートを通っていない**（ゲートは `execute()` の前に走り、
`interrupt()` は `execute()` の中で返る）。**呼び出し箇所で 4 つとも実施する**:

| # | 処理 | 内容 |
|---|---|---|
| 1 | **キー空間と大きさの制限** | 受理するのは `{"action": str, "note": str}` の 2 キーのみ。未知キー・非 `str` キー・サイズ超過は **reject**（マスクしない — マスクすると `action` が書き換わって route が壊れ、2 キーが同一文字列に潰れて衝突する） |
| 2 | **`action` の enum 照合** | `confirm` / `refine` / `discard` と byte-exact に一致しなければ reject。別名・大小文字違い・日本語表記は受け付けない |
| 3 | **PII マスク** | `note` に `detect_pii()` を適用（`detect_pii()` は非 `str` で `TypeError` を投げるので型チェックが先） |
| 4 | **injection 評価** | `note` は Stage 3 の prompt に載るため、命令文パターンを評価する |

**実行場所**: standalone HTTP 経路では `/resume` が **`agent.resume()` を呼ぶ前**に 4 段を実行する。
reject 時は 400 とし、`agent.resume()` を呼ばず outer / inner checkpoint を変更しない。これにより raw
feedback が framework の resume 値として先に永続化される順序逆転を防ぐ。`hitl_review` の
`execute()` 内にも同じ sanitizer を残し、HTTP adapter を通らない直接 graph resume を防御する。

**直接 graph resume における reject 時の State 更新（3 つセットで行う）:**

1. **`refine_action` を書かない** — 書かなければ `route_after_hitl` は fail-closed で END へ落ちる
2. **`hitl_feedback` を `None` にクリアする** — `AgentBaseGraph.route()` は `hitl_status` と
   `hitl_feedback is not None` の**両方**で HITL 分岐を判定する。クリアしないと stale な `hitl_status` で
   `post_process` へ流れ、**拒否したはずの feedback で確定してしまう**
3. **再度 `interrupt()` する** — 応答は `awaiting_human` のままとし、担当者に有効な入力を促す。
   拒否理由は `hitl_metadata` に載せる（自由文をそのまま返さない）

> `hitl_feedback` / `hitl_status` は framework 管理フィールドだが、**resume 直後の reject 経路でのみ
> テンプレが `hitl_feedback` を `None` に戻すことは許容される**（framework が書いた値を消す唯一の箇所）。
> `hitl_count` と `hitl_allowed` は**いかなる経路でも書かない**。

### checkpointer 3 契約（1 つでも破ると resume が収束しない）

いずれも他テンプレートで実際に発生し、**症状が似ている**ため 3 点セットで検査する。

| # | 契約 | 破った場合 |
|---|---|---|
| ① | **明示 `compile(checkpointer=...)` が inner / outer の両方に必要** | `BaseGraph.compile()` は config から checkpointer を自動生成しない。scaffold の `src/api/server.py` は引数なし `agent.compile()` なので**置換必須**。無いと `interrupt()` 発火時に `RuntimeError: interrupt() called but no checkpointer` |
| ② | **inner と outer の checkpointer は別インスタンス** | `GraphNode.execute()` は inner を outer と同一 `thread_id` で invoke する。同一 saver を共有すると保存キーが衝突し、outer の `user_input` が inner の envelope で上書きされ、**resume が毎回最初から再実行される非収束ループ**になる。inner の saver は `GraphNode` 側で専用インスタンスを新規生成する |
| ③ | **`get_subgraph()` は inner をキャッシュする** | framework は invoke 時と resume 時の**両方**で `get_subgraph()` を呼ぶ。毎回新規生成すると resume 側が**空の saver**を持ち、suspend checkpoint を見つけられず最初から再実行 → 再 interrupt → 永久に完了しない |

**`GraphNode.propagate_hitl = True`** とし、outer の `config` にも `hitl` / `memory_enabled` を宣言する
（内側 interrupt を外側へ伝播させるため）。`route()` に `awaiting_human` 分岐は**足さない**
（HITL は interrupt suspend で表現される）。

### `/resume` の認証と所有権（`/invoke` より強い検証が要る）

`/invoke` は新規セッションの開始だが、**`/resume` は保留中の既存セッションへの介入**である。
認証が抜けると「未認証の呼び出し元が任意の `thread_id` を渡して他人の保留ドラフトを resume し、
feedback を注入できる」ことになり、`/invoke` の認証境界が実質無効化される（他テンプレートで実際に発生）。

| 検証 | 失敗時 |
|---|---|
| `/invoke` と**同一の入口認証**（middleware / bearer）を通す | 401 |
| `thread_id` が指定されている | 422 |
| その `thread_id` の checkpoint が存在する | 404 |
| checkpoint の `session_id` が認証済み呼び出し元のセッションと一致する | 403 |

🔴 **`thread_id` はクライアントが echo する。** `InvocationContext.thread_id` は**インスタンスごとのランダム UUID**で
`session_id` から導出されない（実機確認）。resume のために新しい `InvocationContext` を作ってその `thread_id` で
resume すると、**存在しない checkpoint を resume することになり HTTP 経由の HITL が一度も成立しない**。
`/invoke` が `awaiting_human` を返すときに `thread_id` を応答に含め、クライアントがそれを `/resume` に渡す。

🔴 **所有権の主キーは `session_id`。** token 認証では `caller_id` が空文字で共有されうるため、
`caller_id` 単独では gate にならない。`session_id` を持たない checkpoint は **resume 不可（403）** とする。

**API 受入テスト（MR-B 必須・FastAPI TestClient + 本番 config）:**
token 無し(401) / 不正 token(401) / `thread_id` 無し(422) / 未知 thread(404) / 別セッションの thread(403) /
正当(200) の 6 ケース。**CI の緑は認可の欠落を検出しない**ため、この negative テストを常設する。

### HITL 有効化に伴う運用上の帰結

| 項目 | 帰結 |
|---|---|
| PB-7 | **assertion が必須になる**（auto-waive されない）。`interrupt()` が `GraphInterrupt` を伝播させ、`status` が `error` にならないことを実グラフで検証する。🔴 **MR-A 時点の skip 理由は「`hitl.enabled` が false」から「stub 内の TODO placeholder」に変わっている** — `hitl.enabled: true` により skipif は通過し、テスト本体の `pytest.skip("TODO: ...")` で止まっている状態が正しい。**MR-B はこの TODO（import の有効化・対象ノードの指定・`execute()` が interrupt に到達する state の用意）を埋め、`pytest.skip()` を消す**。埋め忘れると PB-7 は skip のまま緑に見える |
| PB-7 stub | `config/config.yaml` を読む**現行正本**を写経する。**HITL 有効テンプレの PB-7 はノード単体では書けない** — `interrupt()` は checkpointer を持つ実グラフの中でしか正しく振る舞わないため、実グラフ（inner + outer、それぞれ専用 saver）を組んで検証する。`agent.yaml` を読む旧世代 stub は、HITL を有効にしても永久に SKIPPED になる（偽の緑） |
| `pyyaml` | **`pyyaml==6.0.1` を dev に exact pin。** PB-7 stub の `_hitl_enabled()` は `import yaml` を握りつぶすため、pyyaml が無いと HITL 有効でも黙って SKIPPED になる |
| STG | `deploy-stg` は **`overall: FAIL` になる（正常）**。正規パケットは `interrupt()` で `awaiting_human` になり、`agent_status_success` の assertion を構造的に満たせない。**`stage:stg-complete` を付けず `stage:stg`（pending）に留める**。status=success を通すための STG 専用 seed を本番コードに入れない |
| secrets | `InvocationContext.from_state(state)` に `secrets` は載らない。ノード内では `current_secrets().get(key, default)` を使う |

> **運用側の手順は 運用ガイド §HITL 保留セッションの運用** — `thread_id` の回収責任、
> 未回収保留の監視、`InMemorySaver` がプロセス再起動で保留を失う件と永続 checkpointer 使用時の TTL 適用、
> checkpoint に載ってよい / 載せないデータの分類、resume が 404/403/非収束になったときのインシデント対応。

## データ層設計（Design 入場条件 (a) / (b)）

**DB / KB はマルチテナント共有ストアではなく、導入先（MSP）専用に provision された接続である。**
分離は **KB / DB 接続レベル**で担保する — 呼出元を認証（`caller_id`）→ 認可された導入先を解決 →
その導入先に **provision 済みの専用接続**を使う。入力に含まれる識別子は**認可済み値との照合のみ**に使い、
行を絞り込む検索キーには使わない。**default KB は持たない**（認可解決不能なら fail-closed）。

**1 デプロイ内の区画は持たない（`SCOPE_KEYS = ()`）。** 本テンプレは **1 社が自社の変更管理のために
自社デプロイで使う**前提であり、1 つのデプロイに複数社のデータが同居することを想定しない。
したがって KB / DB 内をさらに分割する区画キーは**実在しない**。

`SCOPE_KEYS` は parts（`entry-adapter` / `payload-store`）が「1 顧客 KB 内で混ぜてはいけないデータ区画」を
宣言するための欄であって、マルチテナント分離のための欄ではない。区画が無いドメインでは
**空タプルの明示宣言が正しい状態**であり（`entry-adapter` v0.4.0 で空タプルが「区画なし」の正式な宣言）、
ダミーの区画キーを置くと「SaaS マルチテナント分離をしているように見える」誤読と、
全行に同じ値が入るだけの無意味なカラムを生む。**判断根拠は `_ctx()` の docstring に残す。**

分離は**接続レベル**で担保する — `caller_id` を認証 → 認可された導入先を解決 → その導入先に
provision 済みの専用 DB / KB 接続を使う。**default 接続は持たない**（認可解決不能なら fail-closed）。

### AGENTIC STAR Marketplace entry adapter と単一デプロイ resolver（DDR）

repo 直下の `cli.py` は AGENTIC STAR Marketplace の文字列入力を HTTP adapter と同じ内部 envelope ref に変換し、
payload / envelope / `InvocationContext` / `super().invoke()` の `session_id` を一致させる。Marketplace runner が
渡す `conversation_history` は採用せず、adapter 所有の空 `input_context` を組み直す。また `mode` は常に
`invoke` とし、共有 store に書く `ingest` / `feedback` 経路を Marketplace から到達不能にする。
`planned_window` は凍結期間との重なり判定を左右する申請事実なので adapter は既定値を捏造せず、欠落・型不正・
timezone-aware ISO-8601 でない入力を graph 実行前に `invalid_request` として拒否する。

Marketplace の `caller_id` はデプロイ前に列挙できない。一方、本テンプレートは上記のとおり 1 デプロイ 1 社で
あり、デプロイ内の顧客区画を選択しない。このため Marketplace 専用 resolver は、**任意の非空 caller_id を
同じ単一 `ResourceScope` に解決する**。これは default のマルチテナント接続ではなく、デプロイ自体が 1 社の
境界であるという既存設計を Marketplace caller 契約へ写像したもの。貼り付け JSON の値から tenant / scope を
選択することはない。

動作確認用の統制文書と過去レビュー観点は `config/control_documents.sample.json` と
`config/review_perspectives.sample.json` に置く。ファイル名と値の `SAMPLE-` 表記は実データでないことを明示する
provenance であり、loader は欠落・不正 JSON・形不一致・空配列・必須キー欠落を例外として扱う。投入は主キー
上書きによる冪等処理である。本番データへの差し替えは運用ガイドに定める。

### DB schema（導入先専用接続内・原記録・append only）

| テーブル | 主キー | 主な列 | 用途 |
|---|---|---|---|
| `freeze_exception_requests` | `request_ref` | `criteria_version_ref`, `criteria_version`, `criteria_content_hash`, `freeze_calendar_ref`, `freeze_calendar_version`, `freeze_calendar_content_hash`, `planned_window_start/end`, `evaluated_at`, `submitted_at`, `payload_ref`, `status`（`active` / **`deleting`** = tombstone・§保持と削除の契約）, `hitl_state`（`none` / `suspended`）, `hitl_thread_id_hash`, `hitl_suspended_at`, `delete_plan`(JSON), `delete_progress`(JSON), `delete_started_at`, `delete_updated_at` | 申請の原記録（自由記述本文は payload store 側）。**pin は 3 値そろえて保存する** — `ref` だけでは「どの基準で評価したか」の監査 identity にならない |
| `case_evidence` | `source_id` | `request_ref`, `doc_type`, `doc_ref`, `span_ref`, `span_text`, `span_kind`, `submitted_at` | `/ingest` で取り込む当該申請の証跡 |
| `review_perspectives` | `history_ref` | `status`（`active` / `deleting`）, `delete_plan`(JSON), `delete_progress`(JSON), `delete_started_at`, `delete_updated_at`, `condition_id`, `criteria_version_ref`, `criteria_content_hash`, `request_summary_raw`, `request_summary_audit`, `request_summary_safe`, `decision_outcome`, `decision_rationale_raw`, `decision_rationale_audit`, `decision_rationale_safe`, `approved_by`, `approved_at` | 過去例外レビューの 3 点セット（原記録。自由文は 3 値分離 — §KB projection） |
| `perspective_candidates` | `candidate_id` | `request_ref`, `claim_id`, `perspective_code`, `condition_id`, `criteria_version_ref`, `criteria_content_hash`, `source_ids`(JSON), `decision_outcome`, `decision_rationale_raw`, `decision_rationale_audit`, `decision_rationale_safe`, `action`, `proposed_by`(認証済み `caller_id`), `proposed_at`, `status`(`pending` 固定・AG は遷移させない) | `/feedback` が積む**append-only の候補キュー**。`/feedback` の request schema と 1:1 対応する。次版の基準表への昇格は AG 外の人間作業であり、**AG はこの行の `status` を書き換えない** |
| `audit_events` | `event_id` | `correlation_id`, `event_type`, `request_ref`, `occurred_at`, `actor_id`, `approval_ref`, `outcome`, `deleted_counts`(JSON), `error_code`, `target_type`（`request` / `perspective`）, `target_ref` | S-4 監査の永続化。**`request_ref` だけでは過去観点の削除を識別できない**ため `target_type` / `target_ref` を持つ。**削除系イベントの責任分界を証明できる列を持つ**（§保持と削除の契約） |

### 保持と削除の契約

**削除は 3 系統ある。同じ API では消せない。**

| 系統 | 対象 | 削除経路 | 失効基準 |
|---|---|---|---|
| **申請系** | `freeze_exception_requests` / `case_evidence` / `perspective_candidates` / payload / 保留 HITL checkpoint | `DELETE /requests/{request_ref}` | `submitted_at + 外部ポリシーの申請保持日数` |
| **過去観点系** | `review_perspectives` + その KB projection | `DELETE /perspectives/{history_ref}` | `approved_at + 外部ポリシーの過去観点保持日数` |
| **監査系** | `audit_events` | **どちらの cascade 対象にもしない**（削除操作自体を残すため） | 外部監査保持ポリシーに従う独立 purge |

🔴 **`review_perspectives` は `request_ref` を持たない**（主キーは `history_ref`）。過去の例外レビュー観点は
**特定の申請に属さない独立資産**であり、蓄積 moat の本体そのものである。したがって申請の削除で
連鎖的に消してはならないし、構造的に消すこともできない。KB projection も過去観点系に属する。
**`DELETE /requests/` が KB を触ると書いてはならない。**

**`DELETE /requests/{request_ref}` の cross-store 契約（部分失敗を隠さない）:**

DB と payload store をまたぐため、素朴な「消して 200」では*片方だけ消えた状態*を検出できない。

1. 親行を `status: "deleting"` に遷移させ、削除対象 ID・checkpoint hash・累積削除件数を永続化する（**tombstone**）。保留中なら saver の `delete_thread` で outer / inner checkpoint を物理削除する
2. 子を順に削除する（**保留 checkpoint**（`hitl_state: "suspended"` のとき）→ `case_evidence` → `perspective_candidates` → payload）
3. 全て成功したら親行を削除し `{db_rows, kb_docs, payloads}` を返す（`kb_docs` は**常に 0** — 上記のとおり申請系は KB を持たない）
4. 途中で失敗したら **500 `delete_incomplete`**。tombstone は残す
5. 同じ `request_ref` への再実行は、tombstone がある限り**続きから再開できる**（冪等）
6. **404 `request_not_found` は、live record も tombstone も無い場合にのみ返す**

これにより「未知 ref の typo」と「部分削除の残骸」を取り違えない。

**監査（`audit_events` の列を拡張する）:** `target_type`（`request` / `perspective`）と `target_ref` を含む。従来の `event_id` / `correlation_id` / `event_type` /
`request_ref` / `occurred_at` に加えて **`actor_id`（認証済み caller）/ `approval_ref` /
`outcome`（`success` / `incomplete`）/ `deleted_counts`（JSON）/ `error_code`** を持つ。
これらが無いと「誰が・どの承認に基づいて・何件消したか」が残らず、運用ガイド が要求する
**承認記録と削除イベントの 1:1 突合が実行できない**。

`DELETE` 系は **`X-Approval-Ref` ヘッダを必須**とする（entry adapter の clean-value 契約を適用。
欠落・空・改行混入は 400 `approval_ref_missing`）。削除は不可逆であり、承認参照なしの実行を受け付けない。
応答には `audit_event_id` を返す。

**返した ID を読む手段も定義する**（ID を返すだけでは突合の操作契約にならない）:
`GET /audit-events/{event_id}` を **`freeze.audit.read` permission** で提供する。
このエンドポイントは**読み取り専用**で、`audit_events` の 1 行を返す。監査ロールにはこの permission のみを付与し、
`freeze.request.delete` / `freeze.perspective.delete` は付けない（監査と実行の分離）。

**期限到来分の purge（外部運用ジョブ）:** 失効は自動では起きない。このrepoには列挙jobと保持日数の
runtime configを含めず、外部データライフサイクル管理が上表の失効基準で日次列挙し、
申請系は `DELETE /requests/`、過去観点系は `DELETE /perspectives/` を呼ぶ。
各ジョブは scanned / deleted / failed の件数と監査イベントを残し、failed は次回再試行する。

🔴 **自動 purge も `X-Approval-Ref` を免除しない。** 免除すると「承認参照なしの削除経路」が 1 本できてしまう。
保持ポリシーを承認した時点で**不変の `policy_approval_ref`** を発行し、日次ジョブは
`X-Approval-Ref: retention:<policy_version>:<run_id>` を送る。これにより個々の自動削除から
元の保持ポリシー承認まで監査で辿れる。
**運用側の手順は 運用ガイド §データ保持と削除。**

### Stage 2 の日本語想起（部分一致 backend での実装契約）

SDK の永続化層は現時点で全て STUB のため、MR-B の retrieval backend は**暫定実装**になる。

🔴 **本節は「暫定 backend が満たすべき挙動」であって、恒久的な設計制約ではない。**
実 backend（ベクトル検索等）に差し替えたときに捨てる前提の内容であり、恒久要件は
**「日本語の実務入力（空白で区切られていない文）で想起が 0 件にならないこと」の 1 点**に尽きる。
下の 4 項目はそれを暫定 backend で満たすための**既知解**であり、同等以上を満たす別実装で置き換えてよい。

**なぜ既知解を書くか:** 空白・句読点での語分割だけを使うと日本語の業務文は文全体が 1 語になり、
保存側の要約に部分一致せず **0 件**になる（実測）。そしてこの欠陥は
**テスト入力に手で空白を入れると隠れる**ため、実装者が独力で気づける保証がない。

| # | 暫定 backend の要求挙動 |
|---|---|
| 1 | 空白分割に加えて **n-gram 展開**を持つ（既知解: 7 文字以上の語を 2-gram に展開）。形態素解析・辞書（janome / fugashi / MeCab）は**入れない** — backend は差し替え前提で捨てる量を増やさない。Rule 4.5 の依存制限もある |
| 2 | **最低一致数を絶対値で置く**（`config` の `min_recall_hits`・初期値 3）。n-gram は 1 語共有が容易なため `match_score > 0` だと一般語 1 個で無関係な行が返る。**比率にしない** — 長文ほど閾値が上がりどの行も届かなくなる |
| 3 | term 数に上限を置くなら、**先頭切り詰めにしない**（既知解: 全体から等間隔サンプリング）。業務文は関連部分が末尾に来ることが多く、先頭 N 語で切ると落ちる |
| 4 | `LIKE` パターンは `%` `_` `\` を**エスケープし** SQL に `ESCAPE` を付ける。本文に `%` があると同一接続内の全行にヒットする |

🔴 **契約テストの入力に手で空白を入れない。** `本番 DB の 切戻し を 実施` のように人手で区切った入力で
テストすると、実務の入力（`本番DBの切戻しを実施`）では 0 件のままなのに緑になる。
**port 契約テストは `xfail` で置き**、実 backend が繋がった日に `XPASS` で気づけるようにする。

### 過去事例の 3 点セット（`review_perspectives`）

過去例外レビューは **申請要約・確定結論・判定理由の 3 点**を原記録として持つ。
「理由だけ」を切り出すと観点として成立しない（なぜ重要だったかは結論に紐づく）ため 3 点で保持する。

**ただし結論の再流入を 3 箇所で止める:**

| 箇所 | 契約 |
|---|---|
| 検索軸 | Stage 2 の検索クエリ・ランキングに `decision_outcome` を**使わない**（承認された事例が上位に来る偏りを作らない）。**検索は KB projection に対して走り、projection は `decision_outcome` を持たない**（§KB projection）。結論は**ヒット後に `history_ref` で DB 原記録を決定論 join して取得する** — 検索軸からの排除と出力での提示は、この経路でのみ両立する |
| 証拠適格 | `recall_refs` は `source_registry` に入れない ＝ **本件の証拠として引用できない**。`history_ref` は `source_id` と名前空間を分ける |
| prompt 上の役割 | Stage 3 の prompt では「過去の論点（何を確認したか）」として提示し、`decision_outcome` は**モデル可視の projection に含めない**。判定理由は `decision_rationale_safe`（`max_rationale_chars` 上限・PII マスク済み）に射影する。**`decision_outcome` は出力（`precedent_checks[].prior_outcome`）には決定論で載る** — prompt に入れないことと出力に出さないことは別（§過去判例の照合） |

> 過去の裁定・判断結果そのものは組織の事実であり、蓄積して想起することが本テンプレの moat である。
> 塞ぐのは**今回の結論を断定的に導かせる経路**であって、過去実績の保持ではない。

### 過去判例の照合（`precedent_checks`）

> **本テンプレの蓄積 moat の本体。** 過去の凍結例外レビューで実際に論点になったこと
> （「切り戻し手順は書いてあったが、検証済みかの記載が無かった」「顧客承認が影響範囲の一部しか
> カバーしていなかった」等）を KB に貯め、**今回の申請がその論点に触れているかを毎回照合する**。
> 人間のレビュアーが「前にこれで差し戻したな」と思い出してチェックする作業を機械化する部分である。

**フロー:**

```
Stage 2  KB projection（結論を含まない）で検索 → ヒットした history_ref で DB 原記録を決定論 join
           → recall_refs[] = { history_ref, condition_id, criteria_version_ref,
                               request_summary_safe, decision_rationale_safe, decision_outcome }
              ↓ LLM に渡すのは request_summary_safe / decision_rationale_safe まで。
                decision_outcome は渡さない
Stage 3  各 prior_issue について「今回の申請はこの論点に触れているか」を判定
           → status ∈ {addressed, not_addressed, unclear} + 根拠の source_ids
              ↓
post_process  prior_issue / prior_outcome は Stage 2 が取得した KB の値を決定論で写す
           → precedent_checks[] として出力に載せる
```

**出力フィールドの由来（写像は 1 対 1・ここが唯一の定義）:**

| `precedent_checks[]` の値 | 由来 | 誰が決めるか |
|---|---|---|
| `history_ref` / `condition_id` | `recall_refs[]` の同名フィールド | Stage 2（KB から決定論） |
| `prior_issue` | **`recall_refs[].decision_rationale_safe` をそのまま写す** | Stage 2（決定論）。**LLM に書かせない** — 過去の事実を LLM が要約すると内容が静かに変わる。**必ず `_safe` 値**（`_raw` を写すと出力への PII 直通路になる） |
| `prior_outcome` | **`recall_refs[].decision_outcome` をそのまま写す**（`/feedback` と同一 enum: `approved` / `rejected` / `withdrawn` / `deferred`） | Stage 2（決定論）。**LLM の prompt には渡さない**（「過去は承認された」に引きずられる経路を作らない）が、**出力には載せる** |
| `status` / `source_ids` | — | Stage 3（LLM・`candidate` 粒度）。「今回の申請がその論点に触れているか」は自由記述の意味判断であり、ここが LLM の仕事 |

> 🔴 **`prior_outcome`（過去の承認 / 却下）を出力に載せるのは意図的である。**
> 過去の裁定は組織の事実であり、CAB がレビューパケットを読むときの正当な判断材料になる。
> 塞ぐのは「**過去が承認されたのだから今回も承認相当**」という推論を AG がすることであって、
> 過去実績の提示ではない。だから ①LLM の prompt には入れない ②出力では
> `precedent_checks[]` の中に**過去の事実として**置き、今回の申請に対する評価とは別区分にする
> ③`status` は `addressed` / `not_addressed` / `unclear` の 3 値で、承認可否を含意しない。
>
> 過度に塞ぐと蓄積 moat そのものが消える（他テンプレートで実際に起きた失敗）。**個人が特定される情報**と
> **今回の結論の断定**は塞ぐが、**過去の判断結果**は塞がない。

**`status` の意味（承認可否ではない）:**

| 値 | 意味 | `source_ids` |
|---|---|---|
| `addressed` | 今回の申請に、その論点に対応する記述がある | **1 件以上必須**（どこに書いてあるか） |
| `not_addressed` | 対応する記述が見当たらない | 空を許す（無いことは示せない）。`verification_questions` に対応する確認質問が 1 件出る |
| `unclear` | 記述はあるが対応しているか判断できない | 1 件以上必須（判断に迷った箇所） |

**受入テスト = AT-18。**

### KB projection（retrieval-safe・model 可視）

| 含める | 含めない |
|---|---|
| `history_ref` / `condition_id` / `criteria_version_ref` / `criteria_content_hash` | `decision_outcome`（承認・却下の結論ラベル）— **検索軸に入れないため**。出力に載せる値は projection ではなく DB 原記録から `history_ref` で join して取る（§過去判例の照合） |
| `request_summary_safe` | `request_summary_raw` / `_audit`（原記録側にのみ残す） |
| `decision_rationale_safe` | `decision_rationale_raw` / `_audit`（同上） |
| 観点タグ（`perspective_code`） | 申請者・承認者の氏名等の識別子、契約・SLA の金額、顧客名の生値 |

🔴 **自由文列は 3 値に分離する。** 人間が書いた自由文（`request_summary` / `decision_rationale`）を
KB に残すと、その値は**次回の Stage 3 prompt に載る** — PII 漏洩だけでなく **prompt injection の永続経路**になる。
「KB projection に自由入力を含めない」と方針だけ書いても、判定理由のように業務上どうしても自由文になる列で
自己矛盾が起きる。1 つの入力を次の 3 値に分ける:

| 値 | 保存先 | 内容 |
|---|---|---|
| `*_raw` | **DB 原記録のみ** | 入力そのまま。監査・説明責任のための正本 |
| `*_audit` | DB 原記録 | NFKC 正規化後に PII マスク済み。監査ログ・エクスポート用 |
| **`*_safe`** | **KB projection（model 可視はこれだけ）** | ①NFKC 正規化 ②`detect_pii()` ③命令文パターン検出（該当時は reject）④`max_rationale_chars` で切詰。PII detector は全角連絡先を認識しないため、この順序を変えない |

**model 可視値は構造化 JSON の値として prompt に渡し、地の文に連結しない。**
**PII マスクは書き込み時に行う**（読み出し時マスクでは KB が汚染されたまま蓄積する）。
**受入テスト = AT-17**（PII / 他顧客名 / 命令文 / 長さ超過 の 4 パターン）。

### 機微データの 2 層統治

🔴 **射影は `invoke()` に渡す入力そのものに掛ける（State に入ってから消すのでは間に合わない）。**
`BaseGraph.invoke()` は受け取った生入力を `user_input` として State に載せ、checkpointer が有効なら
**その時点の State が最初の checkpoint に入る**。したがって entry adapter は、`payload_store.put()` で
本文を退避したうえで、**graph に渡す入力自体**から 4 自由記述を除去し `payload_ref` に置換した
exact-key projection を渡す。`validated_input`（pre_process が作る JSON 文字列）も同じ射影済みパケットを
出典とし、**自由記述本文を含まない**。平文を一度も State に入れないことが契約であり、
「入れてから後段で消す」は checkpoint に対して無効である。

| 層 | 対象 | 契約 |
|---|---|---|
| 層 1 | 申請の自由記述本文（変更内容・例外理由・ロールバック計画・リスク評価） | entry adapter で `payload_store.put()` → State には `payload_ref` のみ。**クライアント供給の `payload_ref` は無条件破棄**して常に再発行する（偽装 ref で層 1 を迂回されない） |
| 層 2 | Stage 3 が prompt に載せる本文 | `payload_store.get()` は **`thread_id`（＝呼出セッション）束縛**。解決した本文は**呼出限りのローカル変数**とし、State・戻り dict・trace・subgraph output に一切戻さない。`finally` で破棄するのは**この解決済みローカル変数**であって、`payload_store` のエントリではない（下記 §payload の生存期間契約） |

> 🔴 **LLM に本文を渡す以上、モデル経由で State へ還流する経路が開く。** Stage 3 の応答は
> `project_typed_finding()` の型射影を通し、**宣言された finding キー以外を捨てる**。
> `description` に本文を丸ごとコピーさせない上限を prompt と validator の**同一定数**から生成する:
>
> ```python
> # src/services/response_contract.py — prompt と validator の唯一の出典
> FINDING_TEXT_LIMITS = {
>     "description": 400,   # supported_conditions / evidence_gaps / contradictions / approval_scope_discrepancies
>     "question":    300,   # verification_questions
> }
> ```
>
> 上限超過は**切り詰めではなく当該 finding の drop**（切り詰めると引用の逐語性が壊れ、S-3 拒否条件 #4 が
> 別の理由で落ちて原因が読めなくなる）。drop 時は `requires_human_review: true` を立てる。
> **受入テスト:** 各フィールドで上限ちょうど（accept）/ 上限 +1（drop されて `meta.requires_human_review` が true）。

### payload の生存期間契約（HITL 保留を跨ぐ）

`refine` は Stage 3 を**再実行**するため申請本文を再取得する。したがって payload は
**HITL 保留を跨いで生存していなければならない**。

| 事象 | payload store のエントリ |
|---|---|
| `awaiting_human`（suspend） | **削除しない。** HTTP 応答を返しても store のエントリは残す |
| `/resume`(`refine`) | `payload_store.get()` で再取得する（`thread_id` 束縛は suspend 前と同一セッションなので解決できる） |
| terminal（`success` / `discarded_by_reviewer` / `error`） | payload を削除し、HITL 保留を経た場合は outer / inner checkpoint も物理削除する |
| `DELETE /requests/{request_ref}` | 削除する |
| `payload_ttl_s` 到達 | store 側の TTL で失効する |
| `hitl_session_ttl_s` 到達 | **checkpoint と payload をまとめて削除**する（下記） |

**TTL 切れ後の `refine` は fail-closed**（200 / `status="error"` / `error_code="payload_expired"`）。
本文なしで再ドラフトすると根拠のない差分を生成するため、縮退させない。担当者は `/invoke` からやり直す。

🔴 **`payload_ttl_s` は HITL の想定滞留時間より長く設定する。** 既定を 900 秒（15 分）から
**3600 秒（60 分）** に引き上げる — §HITL 方針 ③ の想定滞留は「数分〜数十分」であり、
15 分では対話の途中で本文が消えて `refine` が使えなくなる。
運用でこれを短縮する場合は、保持リスクと `refine` の可用性のトレードオフになる（運用ガイド §主要設定）。

### HITL セッションの生存期間契約

🔴 **`payload_ttl_s` は payload の期限であって checkpoint の期限ではない。** `confirm` は本文を読まないため、
payload が失効した後でも resume 自体は成立する。永続 checkpointer を使う構成では、
**放置された保留 checkpoint が無期限に残り、申請の派生情報を保持し続ける**。

| 項目 | 契約 |
|---|---|
| 期限 | `hitl_session_ttl_s`（既定 **3600 秒** — `payload_ttl_s` と揃える。揃えないと「本文は消えたが checkpoint は残る」中途半端な状態が生まれる） |
| 起点 | `hitl_suspended_at`（`freeze_exception_requests` に持つ） |
| 期限到達時 | payload を削除し、`hitl_state` を `none` に戻す。以後の `/resume` は **410 `hitl_expired`**（404 ではない — 「存在しない」ではなく「期限切れ」を区別する）。**checkpoint は outer / inner の両 saver から物理削除する**（`delete_thread`）。台帳を消して resume を拒否するだけでは派生データが saver に残る。生の `thread_id` は保存していないため（漏洩時に他人のセッションを resume されるので保存しない）、saver 側のスレッドを走査してハッシュ一致で特定する |
| 申請の削除時 | `DELETE /requests/{request_ref}` は **保留中の checkpoint も削除対象に含める**（`hitl_state: "suspended"` なら checkpoint → payload → DB 行 の順）。削除したのに派生情報が checkpointer に残る状態を作らない |
| terminal 到達時 | payload を削除し、記録済み `hitl_thread_id_hash` で outer / inner saver を走査して両 checkpoint を物理削除してから HITL 台帳を clear する |
| 台帳 | 専用テーブルは作らない。1 申請 = 1 セッションなので `freeze_exception_requests` の `hitl_state` / `hitl_thread_id_hash` / `hitl_suspended_at` で足りる。**`thread_id` は生値でなくハッシュを保存する**（漏洩時に他人の保留を resume されない） |

**S-4 のイベント名と payload を固定する**（運用が数えられるようにするため・§S-4 の配置 ⑨）:
`hitl_suspended` / `hitl_resumed` / `hitl_discarded` / `hitl_expired` / `hitl_limit_reached` の 5 種、
payload は `{request_ref, thread_id_hash, occurred_at, action, outcome}`。

## S-3 出力安全契約（Design 入場条件 (c)・testable schema）

Stage 3 は model 応答を State に返す前に、top-level と finding の exact-key/type を検証し、
自由文を NFKC→PII マスクしたうえで上限超過 finding を drop する。Stage 4 まで待つと、未検証の
model 応答が Stage 3 直後の checkpoint に一度残るためである。不正 JSON や不足 section は
空 packet へ coercion せず `output_contract_violation` で fail-closed とする。

**検証を 2 段に分ける（`_extra_security_gate_output(result)` は `state` を受け取らないため）。**
`state` を必要とする照合をゲートに置くと、実装者はキーの有無で分岐する fail-open へ逃げる。

| 段 | 実行場所 | 見るもの | 拒否条件 |
|---|---|---|---|
| 1 | `ReviewPacketPostProcessNode.execute(state)` の**中**、envelope を組み立てる**前** | `state` が必要な照合 — `source_registry` との完全一致、`condition_id` の当該版実在、`request_ref` / `criteria_version_ref` のスコープ、逐語引用と `span_text`、数値の双方向照合、**`recall_refs` との写像一致** — `prior_issue == recall_refs[history_ref].decision_rationale_safe` かつ `prior_outcome == recall_refs[history_ref].decision_outcome` | #4 / #5 / #6 / #7 / #10 / #10-b / **#10-c** |
| 2 | `_extra_security_gate_output(result)` | **`result` だけで判定できるもの** — top-level と全ネストの exact-key、`severity` / `confidence` / enum / 型、**`precedent_checks[].status` の enum と `source_ids` 必須性**、`detect_pii()` 残存 | #1 / #2 / #8 / #9 / **#10-d** / #11 / #12 |

段 1 で不合格の claim は **drop** して `requires_human_review: true` を立てる（envelope には載らない）。
段 2 で不合格なら `SecurityViolationError`（`error_code="output_contract_violation"`）。
**段 2 は段 1 の結果に依存しない** — 段 1 を丸ごと削除しても段 2 が素通りにならないことを変異検査で確認する
（段 1 が「実質的な検査」で段 2 が「形だけ」になると、ゲートを外しても緑のまま通る）。

**主防御は closed allowlist（構造）であり、自由文フレーズ・キー名の denylist はどちらも持たない。**

Stage 3 の LLM 応答契約は `src/services/response_contract.py` の `FINDING_FIELDS_BY_SECTION` /
`PRECEDENT_DRAFT_KEYS` / `FINDING_TEXT_LIMITS` を単一の正本とする。

> 🔴 **`precedent_checks` は LLM 応答と出力で形が違う。** LLM が返してよいのは
> `PRECEDENT_DRAFT_KEYS`（`history_ref` / `status` / `source_ids`）だけで、
> **`prior_issue` / `prior_outcome` を LLM に書かせない**（型射影が両キーを捨てる）。
> post_process が `recall_refs` から決定論で付け足して `PRECEDENT_CHECK_KEYS` の形にする。
> prompt には過去の論点（`decision_rationale_safe`）を提示するが、`decision_outcome` は載せない。
> `evidence_gaps[].gap_origin` も model schema には載せず Stage 4 が `"llm"` を付与する。
> 同じ定数から prompt 用 JSON contract と `project_typed_finding()` の型射影を生成するため、
> prompt と validator の手書き二重管理を行わない。

**閉じた schema（invoke モード）— ネストも含めて全オブジェクトが exact-key:**

```yaml
formatted_output:                       # top-level key は INVOKE_TOP_LEVEL_KEYS と完全一致（過不足とも reject）
  mode: "invoke"
  gate_kind: "freeze_exception_evidence"
  meta:                    { degraded: bool, degradation_code: <enum|null>, requires_human_review: bool }
  applied_pins:            { criteria_version_ref, criteria_version, criteria_content_hash,
                             freeze_calendar_ref, freeze_calendar_version, freeze_calendar_content_hash }
  criteria_conditions:     [ { condition_id, criteria_version_ref, condition_ref } ]
  evidence_sources:        [ { source_id, doc_type, quote_span, span_kind, provenance } ]
  freeze_window_facts:     { freeze_calendar_ref, planned_window, overlapping_windows, evaluated, evaluated_at }
  supported_conditions:    [ { claim_id, description, condition_id, source_ids, severity, confidence: float | null } ]
  evidence_gaps:           [ { claim_id, description, condition_id: str | null, source_ids, gap_origin } ]
  contradictions:          [ { claim_id, description, condition_id: str | null, source_ids } ]
  approval_scope_discrepancies: [ { claim_id, description, source_ids } ]
  precedent_checks:        [ { history_ref, condition_id, prior_issue, prior_outcome, status, source_ids } ]
  verification_questions:  [ { claim_id, question, condition_id: str | null, source_ids } ]

precedent status enum:   [ "addressed", "not_addressed", "unclear" ]
gap_origin enum:         [ "deterministic", "llm" ]           # 空 source_ids を許すのは deterministic のみ
provenance:              { request_ref, source_id, doc_ref, span_ref, criteria_version_ref }
planned_window:          { start, end }                      # TZ 付き ISO-8601
overlapping_windows[]:   { window_id, window_ref, overlap_start, overlap_end }
freeze_calendar_ref:     str | null                          # null = 未指定（evaluated: false と対）
evaluated:               bool                                # false のとき overlapping_windows は必ず []
evaluated_at:            str                                 # State の evaluated_at をそのまま載せる
quote_span:              str                                 # 逐語引用テキスト（source_registry[source_id].span_text の部分文字列）
span_kind:               引用可 allowlist の 5 値のみ（下記 #5）
degradation_code enum:   [ "kb_empty", "llm_unavailable", "discarded_by_reviewer", "hitl_limit_reached", null ]
severity:                固定値 "candidate" のみ
```

**正規定義（MR-B の validator とテストはこの定数を単一の出典とする）:**

```python
# src/nodes/post_process_node.py — 他の場所でキー集合を再列挙しない
INVOKE_TOP_LEVEL_KEYS = frozenset({
    "mode", "gate_kind", "meta", "applied_pins",
    "criteria_conditions", "evidence_sources", "freeze_window_facts",
    "supported_conditions", "evidence_gaps", "contradictions",
    "approval_scope_discrepancies", "precedent_checks", "verification_questions",
})
INGEST_TOP_LEVEL_KEYS   = frozenset({"mode", "gate_kind", "meta", "ingest_summary"})
FEEDBACK_TOP_LEVEL_KEYS = frozenset({"mode", "gate_kind", "meta", "feedback_summary"})

# finding セクションごとの exact-key。prompt の JSON contract と project_typed_finding() の
# 型射影は、どちらもこの 1 つの定数から生成する（手書きの二重管理をしない）。
FINDING_FIELDS_BY_SECTION = {
    "supported_conditions":         ("claim_id", "description", "condition_id", "source_ids",
                                     "severity", "confidence"),
    "evidence_gaps":                ("claim_id", "description", "condition_id", "source_ids", "gap_origin"),
    "contradictions":               ("claim_id", "description", "condition_id", "source_ids"),
    "approval_scope_discrepancies": ("claim_id", "description", "source_ids"),
    "verification_questions":       ("claim_id", "question", "condition_id", "source_ids"),
}

# ネストの exact-key もここで 1 回だけ定義する（§出力契約の yaml は本定数の説明であって別定義ではない）
META_KEYS              = frozenset({"degraded", "degradation_code", "requires_human_review"})
EVIDENCE_SOURCE_KEYS   = frozenset({"source_id", "doc_type", "quote_span", "span_kind", "provenance"})
PROVENANCE_KEYS        = frozenset({"request_ref", "source_id", "doc_ref", "span_ref", "criteria_version_ref"})
FREEZE_FACT_KEYS       = frozenset({"freeze_calendar_ref", "planned_window", "overlapping_windows",
                                    "evaluated", "evaluated_at"})
APPLIED_PIN_KEYS       = frozenset({"criteria_version_ref", "criteria_version", "criteria_content_hash",
                                    "freeze_calendar_ref", "freeze_calendar_version",
                                    "freeze_calendar_content_hash"})   # 凍結カレンダー未指定時は 3 値とも null
CRITERIA_CONDITION_KEYS = frozenset({"condition_id", "criteria_version_ref", "condition_ref"})
PRECEDENT_CHECK_KEYS   = frozenset({"history_ref", "condition_id", "prior_issue", "prior_outcome",
                                    "status", "source_ids"})   # 出力の形
PRECEDENT_DRAFT_KEYS   = frozenset({"history_ref", "status", "source_ids"})   # LLM が返してよい形
PRECEDENT_STATUSES     = frozenset({"addressed", "not_addressed", "unclear"})
PLANNED_WINDOW_KEYS    = frozenset({"start", "end"})
OVERLAPPING_WINDOW_KEYS = frozenset({"window_id", "window_ref", "overlap_start", "overlap_end"})
INGEST_SUMMARY_KEYS    = frozenset({"written_ids", "rejected"})
REJECTED_ITEM_KEYS     = frozenset({"index", "record_type", "reason"})
FEEDBACK_SUMMARY_KEYS  = frozenset({"applied_count", "audit_event_id"})
DELETE_TOP_LEVEL_KEYS  = frozenset({"status", "deleted", "audit_event_id"})
AUDIT_EVENT_KEYS       = frozenset({"event_id", "correlation_id", "event_type", "request_ref",
                                    "occurred_at", "actor_id", "approval_ref", "outcome",
                                    "deleted_counts", "error_code", "target_type", "target_ref"})
DELETED_KEYS           = frozenset({"db_rows", "kb_docs", "payloads"})
QUOTABLE_SPAN_KINDS    = frozenset({"quantity", "date", "identifier",
                                    "observed_condition", "procedural_record"})
```

> 数え間違いによる validator とテストの食い違いを防ぐため、**キー集合は定数で 1 回だけ定義**し、
> 拒否条件・accept 条件・受入テストはすべてこの定数を参照する（本文中で個数を再掲しない）。

**拒否条件（いずれかに該当したら `SecurityViolationError`）:**

| # | 拒否条件 | 意図 |
|---|---|---|
| 1 | top-level の key が **`INVOKE_TOP_LEVEL_KEYS` と完全一致しない**（過剰キーだけでなく `mode` / `gate_kind` の**欠落も reject**） | 未宣言キーを一切通さない。set 差分だけで見ると必須キー欠落を見逃す |
| 2 | **ネストされた各オブジェクト**の key が**下記の定数のいずれか**と一致しない（`META_KEYS` / `APPLIED_PIN_KEYS` / `CRITERIA_CONDITION_KEYS` / `EVIDENCE_SOURCE_KEYS` / `PROVENANCE_KEYS` / `FREEZE_FACT_KEYS` / `PRECEDENT_CHECK_KEYS` / `PLANNED_WINDOW_KEYS` / `OVERLAPPING_WINDOW_KEYS` / `INGEST_SUMMARY_KEYS` / `REJECTED_ITEM_KEYS` / `FEEDBACK_SUMMARY_KEYS` / `DELETED_KEYS` / `AUDIT_EVENT_KEYS` と、各 finding の `FINDING_FIELDS_BY_SECTION`）。**exact-key 集合は本文の散文で再列挙せず、必ずこの定数名で参照する** | 「浅い所は閉じているがネストの深い所に判定を置ける」抜け道を塞ぐ。**`provenance` を任意 `dict` にしない**のが要点 |
| 3 | 契約定数のいずれかに verdict / approval / recommendation 等の**判定フィールドを追加してしまう** | AT-4 の不在テストで契約変更自体を失敗させる。runtime では特定名の denylist を持たず、任意の未宣言キーを #1 / #2 の exact-key 比較で同一に reject する |
| 4 | `evidence_sources[]` の各要素が **`source_registry` のエントリと `source_id` / `doc_type` / `doc_ref` / `span_ref` / `span_kind` / `provenance` まで完全一致しない**。`quote_span` が **`span_text` の literal な部分文字列でない**、または**長さが下限（`min_quote_chars`）未満** | 🔴 **LLM の自己申告に閉じさせない。** 偽 `source_id` と辻褄の合う偽 `provenance` を同時に出せば通る抜け道を塞ぐ。空文字・1 文字の引用は `x in text` が常に真になり照合が vacuous になる |
| 5 | `evidence_sources[].span_kind` が **引用可 allowlist に含まれない**（引用可 = `quantity` / `date` / `identifier` / `observed_condition` / `procedural_record`。`adjudicative`（承認済み・許容される・実質的に問題なし 等）・`opinion`・**`unclassified` は引用不可**）。`span_kind` は **Stage 1 が `source_registry` に決定論分類して載せる値**であり、LLM は `source_id` を選ぶだけで `span_kind` を自称できない（#4 の完全一致検査が自称を弾く） | 🔴 **逐語引用は「出所」しか保証しない。** 過去 CAB 記録や顧客承認文書に「本件は例外として承認可能」等の**裁定文が載っていれば、正しく引用するほど結論が漏れる**。分類は決定論（LLM に「これは事実か」を聞かない）で、**既定値を `unclassified` にして fail-closed**。判定前に NFKC + ゼロ幅除去の正規化を通す |
| 6 | `source_ids[]` の要素が `source_registry` に**実在しない**、または `condition_id` が当該 `criteria_version_ref` の条件表に**実在しない** | claim→source と claim→condition の**両方**を信頼側テーブルで照合する。**`criteria_conditions[]` は申請 evidence の `source_id` を持たない**（基準条項の根拠と申請証跡は別レジストリ） |
| 7 | `evidence_sources[].provenance["request_ref"] != state["request_ref"]`、または `provenance["criteria_version_ref"] != state["criteria_version_ref"]` | **現在申請・現在版スコープ外**の source を証拠にできない |
| 8 | `severity != "candidate"`、または `confidence` が `null` でも 0.0–1.0 の数値でもない | **確定粒度を作れない**（`confirmed` 等を生成不能にする）。根拠から確信度を評価できない場合、model に数値を捏造させず `null` を許す |
| 9 | `approval_scope_discrepancies[]` の要素が exact-key（`claim_id` / `description` / `source_ids`）と一致しない | **カバレッジ判定キーを増設できない**（レビュー指摘の契約を構造で担保する） |
| 10 | 自由文フィールド（`description` / `question`）に **数値が含まれるのに、その数値が対応する `source_ids` の `span_text` に実在しない**（正方向） | LLM が入力に無い数値（ロールバック所要時間・影響台数）を書いても ID 照合は発火しない。数値抽出は NFKC + 漢数字 + 単位同義語（§凍結カレンダー）。一致しない数値を含む finding は **drop** し `requires_human_review: true` |
| 10-b | **逆方向の網羅**: `freeze_window_facts` と `field_gaps` が決定論的に確定させた数値（凍結窓の開始・終了、重なり区間、必須欄の不足件数）が、`formatted_output` の自由文中で**別の値に書き換えられている** | 正方向だけだと「無関係な claim を 1 件入れて数値は散文にだけ書く」で全回避できる。逆方向は**決定論側が持つ値の集合**を基準に、同種の単位を持つ数値が自由文中で矛盾していないかだけを見る（自由文に現れないこと自体は違反にしない — LLM がすべての数値に言及する義務はない）。対象は「時刻・期間・件数」の 3 種に限定し、それ以外の数値（金額・ID 断片）は照合対象外と明示する |
| 10-c | `precedent_checks[]` の `history_ref` が Stage 2 の `recall_refs` に**実在しない**、または `prior_issue != recall_refs[history_ref].decision_rationale_safe`、または `prior_outcome != recall_refs[history_ref].decision_outcome`（いずれも文字列の完全一致） | 過去の事実は KB の値をそのまま写す。**LLM が要約・改変した過去の論点や結論を出力に載せない**（過去の事実が書き換わるのは、今回の判定を誤らせるより静かで危険） |
| 10-d | `precedent_checks[].status` が `PRECEDENT_STATUSES` の 3 値でない、または `status == "addressed"` / `"unclear"` なのに `source_ids` が空 | 「触れている」と言う以上どこに書いてあるかを示す。`not_addressed` のみ空を許す（無いことは指し示せない） |
| 11 | 固定 list フィールド・全 nested object・各 scalar が宣言型でない、`meta.degraded` / `freeze_window_facts.evaluated` が bool でない、`meta.degradation_code` が enum 外、`gate_kind != "freeze_exception_evidence"` | 型崩れと別 gate の出力偽装を防ぐ |
| 12 | 自由文フィールドに `detect_pii()` の finding が残っている | 出力側の PII マスクは framework が一切行わない（SDK 1.0.1 実機確認）。**返却直前に自前で走らせてマスク**する |

さらに result-only 検査で、全 claim の `source_ids` が `evidence_sources` に実在すること、
`source_ids` 必須の claim が空でないこと、source ID が重複しないこと、`quote_span` が
`min_quote_chars` 以上かつ引用可 `span_kind` であること、`provenance.source_id` が外側の
`source_id` と一致することを再検証する。Stage 4 / post-process の組立てを将来変更しても
dangling citation が最終 envelope に残らないための独立した防御層である。

> **`cited_source_ids` は LLM の自己申告を使わない。** 検証を通過した claim の `source_ids` から
> post_process が**再計算**する（evidence-gate 部品への入力もこの再計算値）。

**ingest / feedback モードの envelope も exact-key で閉じる:**
```yaml
{ mode: "ingest",   gate_kind: ..., meta: <META_KEYS>, ingest_summary:   { written_ids, rejected } }
{ mode: "feedback", gate_kind: ..., meta: <META_KEYS>, feedback_summary: { applied_count, audit_event_id } }

rejected[]:          { index, record_type, reason }
```

> `meta` は 3 モードとも同一の `META_KEYS`（`degraded` / `degradation_code` / `requires_human_review`）で閉じる。
> `ingest` / `feedback` では `requires_human_review` は常に `false`（人手判断を要求する経路が invoke にしか無いため）。
> **モードごとに `meta` の形を変えない** — 変えると validator が mode 分岐を持つことになり、
> `result["mode"]` の宣言値を信頼する構造（fail-open の温床）が 1 つ増える。

**accept / reject の対テスト（`docs/03_test_spec.md` に落とす）:**
- **reject**: `approval_verdict` を足した出力 / 全角「承認可否」キー / `supported_conditions[0].recommendation`
  を足す（ネストの追加キー）/ `approval_scope_discrepancies[0].covered` を足す / `provenance` に任意キー /
  `source_registry` に無い `source_id` を辻褄の合う偽 `provenance` 付きで出す / **実在するが無関係な
  `source_id` を引用する** / 当該版に無い `condition_id` / `severity: "confirmed"` / 別申請の `request_ref` /
  別版の `criteria_version_ref` / `quote_span` 空・1 文字 / `span_kind: "adjudicative"` の引用 /
  `description` に source に無い「4時間」を書く / `mode` 欠落 / `gate_kind` 欠落
- **accept**: `INVOKE_TOP_LEVEL_KEYS` のみ、全ネストが exact-key、全 `source_ids` が `source_registry` に実在、
  全 `condition_id` が当該版に実在、全 `quote_span` が `span_text` の部分文字列かつ下限以上かつ引用可 `span_kind`、
  `severity` が `candidate` の出力

> **denylist を持たない理由（重要）:** 「この例外は承認して差し支えない」という断定は
> 「特段の支障は見当たりません」等の言い換えで正規表現をすり抜ける。禁止語リストを置くと
> 「網羅している」という誤った安心（false assurance）が生まれるため置かない。キー名も closed
> exact-key schema が任意の未宣言名を既に拒否するため、既知名だけの補助 denylist は置かない。
>
> **残余リスクを正直に書く:** 上記の拒否条件は「**判定フィールドを構造的に生成不能にする**」ものであり、
> `description` / `question` の自由文に断定的な言い回しが混じる可能性は**残る**。
> これを潰すために自由文を有限コードとテンプレート描画に置き換える設計も検討したが**採らない**
> （100% 機械保証を追う過剰設計として巻き戻された前例がある）。自由文の残余は
> ①判定フィールドが**存在しない**こと ②`severity` が `candidate` 固定であること
> ③全 claim に検証済み source が紐づくこと ④引用が非裁定 span に限られること、で
> 「読み手が断定として受け取らない」構造を作った上で、業務プロセス側の人間レビューが担う。
>
> この位置づけは「**製品安全の緩和策**」であって、法令遵守・契約上の責任充足を確立するものではない
> （基準の妥当性検証は顧客の変更管理・品質部門の責任）。

**不在テスト（Design 入場条件 (c)）:** 「この例外を承認すべきか」「リスクは受容できるか」
「顧客承認はカバーしているか」という直接の問いを `/invoke` に与えたとき、出力が
`evidence_gaps` / `verification_questions` / `approval_scope_discrepancies` に限られ、
**判定に相当する文字列・キーがどの階層にも現れない**ことを固定する。

## API 契約（`src/api/server.py`）

| endpoint | 認証 | 許可 mode | request（本文以外は allowlist 射影される） | response |
|---|---|---|---|---|
| `POST /invoke` | **middleware 必須**。申請 registry を保存するため実副作用は write | `invoke` | `{ criteria_version_ref, criteria_version, criteria_content_hash, freeze_calendar_ref?, freeze_calendar_version?, freeze_calendar_content_hash?, planned_window{start,end}, change_description, exception_rationale, rollback_plan, risk_assessment, evidence_declarations[] }` | 確定時: `{ status: "success", output: <invoke envelope>, correlation_id }` ／ HITL 保留時: `{ status: "awaiting_human", thread_id, hitl_metadata, correlation_id }`（`thread_id` を `/resume` に echo する） |
| `POST /ingest` | **middleware 必須**（token 単独では不可＝認可されていない書込を防ぐ） | `ingest` | `{ records[] }`（版参照は**行ごと**に持つ。下記 `records[]` 契約） | `{ status, output: <ingest envelope> }` |
| `POST /feedback` | **middleware 必須** | `feedback` | `{ request_ref, claim_id, perspective_code, condition_id, criteria_version_ref, source_ids[], decision_outcome(enum), decision_rationale(≤`max_rationale_chars`), action(enum), approved }`。**`additionalProperties: false`（未宣言キーは 400）**・`decided_by` / `decided_at` はクライアント指定不可（400）・書込は全部成功か全部失敗 | `{ status, output: <feedback envelope> }` |
| `POST /resume/{thread_id}` | **middleware 必須（`/invoke` と同一の入口認証）+ checkpoint 所有権検証** | — | `{ action(enum: confirm / refine / discard), note? }` の **closed schema**（未宣言キーは 400）。`thread_id` は **`/invoke` の `awaiting_human` 応答が返した値をクライアントが echo する**（サーバ側で新規生成した `thread_id` では既存 checkpoint を引けない） | 確定時: `{ status: "success", output: <invoke envelope>, correlation_id }` ／ 再 suspend 時: `{ status: "awaiting_human", thread_id, hitl_metadata, correlation_id }`。**`hitl_session_ttl_s` 超過後は 410 / `status: "error"` / `error_code: "hitl_expired"`** |
| `DELETE /requests/{request_ref}` | **middleware 必須** + **`X-Approval-Ref` ヘッダ必須** | — | path parameter のみ（body なし。body があれば 400） | `{ status, deleted: { db_rows, kb_docs, payloads }, audit_event_id }` — top-level は `DELETE_TOP_LEVEL_KEYS` で閉じる。**`hitl_state: "suspended"` の保留 checkpoint も cascade 削除する**。**live record も tombstone も無ければ 404 `request_not_found`**／部分失敗は **500 `delete_incomplete`**（tombstone を残し再実行で再開・§保持と削除の契約） |
| `GET /audit-events/{event_id}` | **middleware 必須**（`freeze.audit.read`） | — | path parameter のみ。**読み取り専用・副作用なし** | `audit_events` の 1 行。返却キーは **`AUDIT_EVENT_KEYS`** で閉じる（散文で再列挙しない）。未知 `event_id` は 404 |
| `DELETE /perspectives/{history_ref}` | **middleware 必須** + **`X-Approval-Ref` ヘッダ必須** | — | path parameter のみ（body があれば 400） | 同上の envelope。`deleted` は `{db_rows, kb_docs, payloads: 0}`（過去観点は payload を持たない）。**未知 `history_ref`（live も tombstone も無い）は 404 `perspective_not_found`**／部分失敗は 500 `delete_incomplete`。**過去観点系の削除経路**であり申請系の cascade とは別系統（§保持と削除の契約） |

**`/feedback` の enum（closed・正規化は NFKC + casefold のみ。別名・日本語表記は受け付けない）:**

| フィールド | 許容値 | 意味 |
|---|---|---|
| `decision_outcome` | `"approved"` / `"rejected"` / `"withdrawn"` / `"deferred"` | CAB がその申請に対して**実際に下した結論**（AG の出力ではなく、人間の確定結果の記録） |
| `action` | `"confirm"` / `"amend"` / `"discard"` | AG が出した当該 claim をレビュー担当がどう扱ったか（確定 / 修正して確定 / 破棄） |

- enum 外の値・大文字小文字以外の揺れ・空文字は **400 `invalid_request`** で reject し、**write は 1 件も走らせない**
- `decision_outcome` は `review_perspectives` の原記録にのみ入り、**KB projection には載らない**（§KB projection）
- **受入テスト:** 正常 3 × 3 の組合せから代表 3 ケース / enum 外の値 / 空文字 / `approved: false` / `approved` 欠落 /
  `decided_by` をクライアントが指定 — いずれも write が発生していないことを store 側で assert

**write 権限の mode 別契約（executable contract）:**

`required_trust_level = VERIFIED_EXTERNAL` は「認証済みの呼び出し元か」しか言わないので、**それだけでは
「任意の認証済み利用者が承認基準を書き換えられる／申請記録を削除できる」状態になる**。認可は
`AuthorizedResourceResolver` が `caller_id` に紐づけて返す **operation permission の集合**で行い、
entry adapter が endpoint ごとに必要な permission を要求する（不足は **403** + 監査イベント）。

| endpoint / mode | 副作用 | 必要 permission | その他の入口条件 |
|---|---|---|---|
| `POST /invoke` | 申請 registry + claim snapshot の write | `freeze.invoke.write_snapshot` | 認証 middleware（token 単独不可）・`caller_id` → 認可導入先の解決成功・**クライアントヘッダ由来の scope 値は破棄する**（区画は無いが、header で store を切り替えられる経路自体を作らない） |
| `POST /ingest` | DB / KB への **申請証跡と過去レビュー観点の write**（**統制文書は書けない** — §統制文書の版固定入力） | `freeze.ingest.write_case_data` | 同上に加え、`records[]` の各行が品質ゲートを通過すること。行単位で reject し `rejected[]` に理由を返す（部分成功は許すが、DB+KB の二重書込が片方失敗したら**その行を補償 rollback**して 0 件適用にする） |
| `POST /feedback` | **`perspective_candidates` への append のみ**（KB / `review_perspectives` には書かない） | `freeze.feedback.append_candidate` | 同上に加え **`approved is True` が必須**（false / 欠落は 400 で reject し write しない）。`decided_by` はサーバが認証済み `caller_id` から設定する（クライアント指定は 400） |
| `POST /resume/{thread_id}` | 保留中セッションの**再開**（確定 / 再ドラフト / 破棄） | `freeze.invoke.write_snapshot` | `/invoke` と同一の入口認証 + `thread_id` の checkpoint が存在すること + **checkpoint の `session_id` が認証済み呼び出し元と一致すること**（§`/resume` の認証と所有権）。permission 不足は 403・所有権不一致も 403・未知 thread は 404・`thread_id` 欠落は 422。**`approved` / `decided_by` は `/feedback` の契約であり `/resume` には無い**（body は `{action, note?}` の closed schema） |
| `DELETE /requests/{request_ref}` | **申請系** DB 行 + payload + **保留 checkpoint** の cascade 削除（**KB は触らない** — 過去観点系は別系統・§保持と削除の契約） | `freeze.request.delete` | 同上に加え **`X-Approval-Ref` ヘッダ必須**（欠落は 400 `approval_ref_missing`）。tombstone 方式のため再実行は冪等。削除操作自体を `actor_id` / `approval_ref` 付きで監査イベントに残す |
| `GET /audit-events/{event_id}` | **副作用なし（read-only）** | `freeze.audit.read` | 認証 middleware。**監査ロールには削除 permission を付与しない**（監査と実行の分離） |
| `DELETE /perspectives/{history_ref}` | **過去観点系** `review_perspectives` の行 + その KB projection の削除 | **`freeze.perspective.delete`**（`freeze.request.delete` を再利用しない — 通常の申請削除権限で**組織全体の蓄積観点**を消せてしまうため） | 同上（`X-Approval-Ref` 必須）。**申請系の cascade からは到達できない独立経路**であり、蓄積 moat の資産を消す操作なので承認参照なしでは実行しない。DB 削除成功後に KB 削除が失敗した場合も申請系と同じ tombstone 方式で再開する（`review_perspectives.status = "deleting"`） |

**受入テスト（MR-B 必須）:** **7 endpoint**（`/invoke` / `/ingest` / `/feedback` / `/resume` / `DELETE /requests` / `DELETE /perspectives` / `GET /audit-events`）× {permission あり → 200 / permission なし → 403 + write が発生していない} の **14 ケース**。permission 不足時に**書き込みが 1 件も起きていないこと**を store 側で assert する
（403 を返しつつ write が走っている、が最悪の failure mode）。

**`records[]` の契約（`record_type` による closed discriminated union）:**

`/ingest` が受けるのは **2 種類のみ**。**例外承認基準・凍結カレンダーは受け付けない**
（§統制文書の版固定入力 — AG は統制文書への書込権限を持たない。当該 `record_type` は
未知値として reject される）。

| `record_type` | 必須キー | 書込先 |
|---|---|---|
| `"case_evidence"` | `request_ref`, `source_id`, `doc_type`, `doc_ref`, `span_ref`, `span_text`, `submitted_at` | `case_evidence` |
| `"review_perspective"` | `history_ref`, `criteria_version_ref`, `criteria_content_hash`, `condition_id`, `request_summary`, `decision_outcome`, `decision_rationale`, `approved_by`, `approved_at` | `review_perspectives`（+ KB projection。自由文 2 列は 3 値分離を経る） |

- `record_type` が上記 2 値のいずれでもない行（`"exception_criteria"` / `"freeze_calendar"` を含む）、
  必須キーを欠く行、宣言外キーを持つ行、型違いの行は **その行だけ reject** し
  `rejected[]` に `{index, record_type, reason}` を返す（リクエスト全体は 200）
- `submitted_at` / `approved_at` は **TZ 付き ISO-8601 必須**（オフセット省略は reject）
- `decision_outcome` は **`/feedback` と同一の enum**（`approved` / `rejected` / `withdrawn` / `deferred`）。
  enum 外は行 reject（2 箇所で別々に定義しない）
- `doc_type` は `evidence_declarations[]` と同一の enum（§`evidence_declarations[]` の要素契約）
- **受入テスト:** 2 種の正常行 / `record_type: "exception_criteria"`（→ reject されること）/ `"freeze_calendar"`（同上）/
  未知 `record_type` / 必須キー欠落 / 宣言外キー / オフセット省略 / enum 外 `decision_outcome`

**入力の allowlist と品質ゲート（省略可フィールドの既定値まで固定する）:**

| 項目 | 契約 |
|---|---|
| 受理形式 | **`application/json` の body のみ**。csv / PDF / xlsx / docx など**ファイルアップロードの経路自体を持たない**（パーサを持たない選択＝攻撃面を作らない。csv を「軽量だから」と受けると区切り・エンコーディング・行エラー索引の契約が要り、MR-B のスコープが膨らむ）。他の Content-Type は 415。body の先頭バイトが JSON でない場合も reject（Content-Type の自己申告を信用しない） |
| テキスト品質ゲート | 索引・prompt に入るテキストそのものに適用する。**NFKC 正規化 → ゼロ幅文字除去 → 判定**の順に固定し、各比率は「該当文字数 ÷ 正規化後の総文字数」で算出する。閾値は `config/config.yaml` に置く（`text_gate_*`）。**判定はすべて「閾値以上／以下で reject」に統一する**（境界の扱いを 4 指標でばらけさせない）: U+FFFD 比率 >= 0.02 / 制御文字（`\t\n` を除く C0・C1）比率 >= 0.01 / 可読文字（文字・数字・句読点・空白）比率 <= 0.80 / 長さ >= 20000 のいずれかで reject。**境界テスト**（閾値の直下 = accept、同値 = reject、直上 = reject）を MR-B に要求する |
| 未知キー・型違い | pre_process の allowlist 射影で**破棄ではなく reject**（400）。黙って捨てると呼び出し側が送ったつもりの制約が消える |
| `evidence_declarations` 省略時 | **`[]` を注入**（正規化後は必須）。`source_registry` が空になり Stage 1 が「証跡未提出」の gap を `gap_origin: "deterministic"` / `source_ids: []` で必ず 1 件出す。空は許容するが、**空であることが出力に現れる**（黙って成功しない）。**受入テスト:** 省略 / 空 list 明示 / 非 list（→ 400）/ 要素が宣言外キーを持つ（→ 400） |
| `freeze_calendar_ref` 省略時 | 既定値は **`null`**（S-3 の型 allowlist に収載済み）。`freeze_window_facts.evaluated: false` として返し `evidence_gaps` に「凍結カレンダー未指定」を `gap_origin: "deterministic"` で積む（§凍結カレンダー 契約 #4）。**受入テスト:** 省略 / `null` 明示 / 未取込の ref 指定（→ 400 `criteria_version_unknown` と同型の `freeze_calendar_unknown`）/ 型違い（→ 400） |
| `criteria_version_ref` 省略時 | **fail-closed**（`error_code="criteria_version_missing"`）。基準の版なしに十分性照合はできない |
| `planned_window` のオフセット省略 | **reject**（`format_check` に理由）。ローカル時刻の解釈をサーバ側で補完しない |

**エラーコード（fail-closed の可視化）:**

| 状況 | HTTP | `status` | `error_code` |
|---|---|---|---|
| 認証なし / middleware 未通過 | 401 | — | — |
| `caller_id` → 導入先の認可解決失敗 | 403 | `error` | `customer_resolution_failed` |
| 未知 mode / 形状不正 / 未宣言キー | 400 | `error` | `invalid_request` |
| 版未指定・未取込の版指定 | 400 | `error` | `criteria_version_missing` / `criteria_version_unknown` |
| pin の hash 不一致・未承認・有効期間外 | 400 | `error` | `criteria_content_hash_mismatch` / `criteria_version_not_effective` |
| 凍結カレンダー pin の同型エラー | 400 | `error` | `freeze_calendar_unknown` / `freeze_calendar_content_hash_mismatch` / `freeze_calendar_not_effective` |
| シール参照の解決失敗（正本 registry 配線・request-lifetime シールの消失） | 400 | `error` | `criteria_seal_unresolved` / `freeze_calendar_seal_unresolved`（§統制文書の版固定入力 failure 行。**`freeze_calendar` は pin 省略時のみ `None` が正当**で、pin があるのに解決できない場合は `evaluated: false` に縮退させない） |
| `evaluated_at` が State に無い（adapter 経由でない呼出） | 200 | `error` | `evaluated_at_missing` |
| HITL 保留中（担当者の確認待ち） | 200 | `awaiting_human` | —（応答に `thread_id` を含める） |
| `/resume` の `thread_id` 未指定 / 未知 / 別セッション | 422 / 404 / 403 | `error` | — |
| `/resume`(`refine`) 時に payload が TTL 失効している | 200 | `error` | `payload_expired`（縮退させない・§payload の生存期間契約） |
| `refine` が `max_hitl` に到達 | 200 | `success` | —（`meta.degradation_code: "hitl_limit_reached"` + `requires_human_review: true`。error ではない） |
| `DELETE /requests` の live record も tombstone も無い | 404 | `error` | `request_not_found` |
| `DELETE /perspectives` の live record も tombstone も無い | 404 | `error` | `perspective_not_found` |
| HITL セッションが `hitl_session_ttl_s` を超過した後の `/resume` | 410 | `error` | `hitl_expired` |
| `DELETE` の cross-store 部分失敗 | 500 | `error` | `delete_incomplete`（tombstone を残す・再実行で再開） |
| `DELETE` に `X-Approval-Ref` が無い / 空 / 不正値 | 400 | `error` | `approval_ref_missing` |
| resume 応答が HTTP preflight の 4 段サニタイズで reject | 400 | `error` | `invalid_request`（`agent.resume()` 未呼出し・checkpoint 不変） |
| 品質ゲート不合格（ingest 行） | 200 | `success` | —（`rejected[]` に理由） |
| KB 空振り | 200 | `success` | —（`meta.degradation_code: "kb_empty"`） |
| LLM 未注入 | 200 | `success` | —（`meta.degradation_code: "llm_unavailable"`） |
| 注入済み LLM の実行時失敗 | 200 | `error` | `llm_call_failed` |
| S-3 拒否 | 200 | `error` | `output_contract_violation` |

> **`startup()` は CI が一度も実行しない。** `run-tests` は `pytest tests/` を回すだけなので、
> manifest 値のロード漏れ・TrustLevel 表記ゆれ・secret 必須化といった**本番起動 blocker が全ゲート緑を
> すり抜ける**。MR-B は `with TestClient(app):` で startup を走らせる統合テストを**1 本以上**置き、
> config の値が service / store に届いていることも同時に assert する。

## 適用部品一覧（共通部品）

> MR-B はゼロから書かず `parts/` の実装部品を写経して adaptation_points を埋める。
> `apply_part.py` を流す際の指示書となる。適用時に各 `PART.yaml` の version / status を再確認すること。

| part-id | 適用先 | adaptation_points の埋め方 |
|---|---|---|
| `cat2-graph-skeleton` | `main` スロット（`FreezeExceptionWorkflowGraphNode`） | concurrency lock 付き lazy cache。`mode_specs` → 3 モード。`error_strategy="handle"` で inner の自由文を安定 error code に射影。**`propagate_hitl=True`**（HITL 採用）+ inner 専用 checkpointer の新規生成 + `get_subgraph()` キャッシュ（§checkpointer 3 契約） |
| `output-envelope` | `post_process`（`formatted_output` 整形） | `format_invoke` → `INVOKE_TOP_LEVEL_KEYS`（§出力契約）。`format_ingest` → `ingest_summary`。`format_feedback` → `feedback_summary`。`required_fields_by_mode` → fail-open 防止の必須キー。`gate_kind` → `"freeze_exception_evidence"` |
| `evidence-gate` | inner Stage 3/4 + post_process の S-3 | `FindingField` / `project_typed_finding` で候補を型付き射影し、宣言外キー・型違いを fail-closed。claim→source は `validate_provenance()` で実レコードへ照合。**逐語引用の下限・`span_kind` allowlist・数値照合は本テンプレの追加契約**として §S-3 の拒否条件 #4/#5/#10 に従って拡張する |
| `payload-store` | `src/api/server.py`（層 1 checkpoint 隔離） | **`scope_keys=("request_ref",)`**（MR-B で確定）。部品は空タプルを明示的に拒否する（「区画が無いなら scope-bound storage を使うな」）。`entry-adapter` の `SCOPE_KEYS = ()` とは**別物**で、あちらは KB/DB の区画（本テンプレには無い）、こちらは「どの payload を一緒に解決してよいか」＝申請単位の分離であり、実在する。`session_id` は**サーバが決める値**（`ctx.thread_id` はインスタンスごとの乱数で、adapter が put 時に知り得ないため一致しない）。英字 ref を使う。`finally` で破棄するのは**解決済みローカル変数**のみ。🔴 **store のエントリは `awaiting_human` では 削除しない**（terminal 応答・明示 `DELETE`・`payload_ttl_s` 到達時のみ削除 — §payload の生存期間契約）。ここを写経で「HTTP 処理の finally で削除」にすると `refine` が本文を引けず壊れる |
| `llm-draft-stage` | inner Stage 3（`sufficiency_draft`） | 2 ブロック prompt に検証定数から生成した closed JSON contract を追加。**要素 1 件の実例と allowlist の値を prompt に含める**（空配列だけ示すと実 LLM が `{}` を返す既知欠陥）。LLM call failure は fallback せず `llm_call_failed`。例外文は State/trace/output に入れない |
| `ingest-row-pipeline` | inner `accumulate_ingest` | 区画キーを持たないため `trusted_scope` は注入しない（`SCOPE_KEYS=()`）。resolver が返す DB / KB ハンドルと `session_id=ctx.thread_id` を constructor 注入する。`write_batch` を 1 回呼び、DB+KB 失敗時は補償 rollback して 0 件適用。**PII マスクは書き込み時**に適用 |
| `entry-adapter` | `src/api/server.py`（認証 + scope + operation policy） | **`SCOPE_KEYS=()`**（区画なしの明示宣言。写経元の語彙をそのまま残さない — このドメインに区画は実在しない）。`invoke` も snapshot を保存するため `WRITE_INGEST` に分類し、全 3 mode と削除を middleware-only にする |

**不採用の部品と理由:**

| part-id | 不採用理由 |
|---|---|
| `scoped-retrieval` | 導入先専用 KB（接続レベル分離）のため、部品としての導入はしない。ただし**安全契約は写経踏襲**する（safe projection allowlist・`normalize_query` の共有・`source_id` 必須・空振り時 `degradation_code: "kb_empty"`）。区画キーを持たないため scope 引数の絞り込みも無い（分離は接続レベル）。日本語想起は §Stage 2 の日本語想起 の 4 契約に従う |
| `ingest-file-sanitizer` | 受理形式を軽量テキストに絞るためファイル種別分岐は不要。ただし**未知形式 reject（マジックバイト判定）とテキスト品質ゲートの考え方は pre_process / ingest の形状検証に取り込む** |

**部品同梱テストの扱い:** 写経した部品のテストは `tests/parts/{part-id}/` に入る。**削除・弱体化しない**。
写経の忠実性は `verify_part.py` で機械照合する。

> **空振りとエラーを混同しない:** KB に該当観点が無いのは**空振り**であり
> `status="success"` + `meta.degraded: true` / `meta.degradation_code: "kb_empty"` とする。
> backend への接続失敗は **fail-closed** で `status="error"`。

## `docs/03_test_spec.md` に落とす受入テスト一覧

`docs/03_test_spec.md` はテストフェーズの成果物であり **実装フェーズで埋める**（設計時点では scaffold 雛形のまま）。
本設計が要求する受入テストをここに集約しておき、MR-B はこの表を `docs/03_test_spec.md` の AT 節へ展開する。
**設計が契約を宣言したのに証明テストが無い、という状態を作らないための対応表**である。

| AT-ID | 対象契約 | 正常系 | 異常系 |
|---|---|---|---|
| AT-1 | 接続境界（§`AuthorizedResourceResolver`） | 認可済み caller で invoke → 導入先の KB/payload に到達 | ①未マッピングの caller → `AuthorizationError` で fail-closed（default 接続にフォールバックしない）②payload は `thread_id` 束縛で、別セッションの `payload_ref` を提示しても解決されない ③**同一 graph インスタンスで 2 回実行し、2 回とも resolver が呼ばれ、DB/KB/payload ハンドルが `self._subgraph` や node の instance 変数にキャッシュされていない**ことを実測 |
| AT-2 | operation permission（§write 権限の mode 別契約） | **7 endpoint**（`/invoke` / `/ingest` / `/feedback` / `/resume` / `DELETE /requests` / `DELETE /perspectives` / `GET /audit-events`）× permission あり → 200 | permission なし → 403 かつ **write / 削除が 1 件も発生していない** |
| AT-3 | S-3 closed allowlist（§S-3 accept/reject 対テスト） | 正規 envelope が通る | 拒否条件 #1〜#12 の各ケース（未宣言の判定キー・ネスト追加キー・偽 source・無関係 source・`span_kind: adjudicative`・空/1 文字引用・別版 `condition_id`・`severity: confirmed`・source に無い数値・PII 残存）。**10-b の逆方向**として「`freeze_window_facts` が持つ重なり区間・件数を自由文が別の値に書き換えている」ケースを含める。**`span_kind` 分類の 9 分岐**（§`span_kind` の決定論分類）と全角化・ゼロ幅挿入の回避入力もここに含める |
| AT-4 | 不在テスト（§S-3 末尾） | 「承認すべきか」「リスクは受容できるか」「顧客承認はカバーしているか」を invoke に与える → 出力が gaps / questions / discrepancies に限られる | 判定に相当する文字列・キーがどの階層にも現れない |
| AT-5 | 凍結窓の決定論（§凍結カレンダー 契約 #1〜#4） | オフセット違いの等価時刻が同一判定 | オフセット省略 → reject / `evaluated: false` と「重なり 0 件」が区別できる / 同一入力を TZ の異なる環境で 2 回実行して同一結果 |
| AT-6 | `records[]` discriminated union（§API 契約） | `case_evidence` / `review_perspective` の正常行 | `exception_criteria` / `freeze_calendar` / 未知 `record_type` / 必須キー欠落 / 宣言外キー / オフセット省略 / enum 外 `decision_outcome` → 当該行のみ `rejected[]` |
| AT-7 | テキスト品質ゲート（§入力の allowlist と品質ゲート） | 閾値の**直下**は通る | 閾値の**同値・直上**で reject（4 指標それぞれ。設計の演算子は `>=` / `<=` で統一済み） |
| AT-8 | `/feedback` enum（§`/feedback` の enum） | 代表 3 ケースが write される | enum 外 / 空文字 / `approved: false` / `approved` 欠落 / `decided_by` 指定 → write ゼロ |
| AT-9 | 既定値注入（§入力の allowlist と品質ゲート） | 省略時に宣言どおりの既定値が入る | 型違い・宣言外キーは破棄でなく 400 |
| AT-10 | `FINDING_TEXT_LIMITS`（§機微データの 2 層統治） | 上限ちょうどは通る | 上限 +1 で当該 finding が drop され `meta.requires_human_review: true` |
| AT-11 | 縮退と失敗の区別（§縮退動作） | KB 空 → `success` + `kb_empty` / LLM 未注入 → `success` + `llm_unavailable` | 注入済み LLM の実行時失敗 → `status="error"` + `llm_call_failed`（縮退に化けない）。inner から外へ渡せる error code は allowlist に閉じ、provider 例外文は応答に出さない |
| AT-12 | prompt contract（§適用部品一覧 `llm-draft-stage`） | `build_prompt()` に model が返してよい finding キー（`gap_origin` を除く）・allowlist・上限値・`source_ids` 必須の指示・`PRECEDENT_DRAFT_KEYS` が現れ、**`prior_outcome` / `gap_origin` が現れない** | 不正 JSON・top-level section 欠落・finding 必須キー欠落を空 packet にせず `output_contract_violation` にする。prompt の値を削る変異でテストが落ちること |
| AT-13 | startup（§API 契約 末尾） | 非空 `auth_runbook_url` で `with TestClient(app):` のlifespanとhealthが通り、config 値が service / store に届く | **`auth_runbook_url` が空なら環境を問わずlifespan起動が失敗する**。空/非空を実startupの対テストで固定する |
| AT-14 | 未知 mode の二重防御（§inner の `BaseGraph` ABC 実装） | 3 モードが正しく route される | API に未知 mode → 400。inner に直接流す → いずれのモードのノードも実行されず終端する |
| AT-15 | 統制文書の版固定入力（§統制文書の版固定入力） | 正規の pin で invoke が通り、`applied_pins` が出力に載る | 未取込 ref / hash 不一致 / 未承認 / 有効期間外 の 4 ケースが **それぞれ固有の `error_code` で 400** かつ Stage 1 に到達しない。`applied_pins` を欠いた envelope は S-3 拒否条件 #1 で落ちる |
| AT-16 | AG が統制文書に書けないこと（§統制文書の版固定入力） | `/ingest` に `case_evidence` / `review_perspective` を送ると書ける | `record_type: "exception_criteria"` / `"freeze_calendar"` を送ると **行 reject**（`rejected[]` に理由）。`/feedback` の書込先が `perspective_candidates` に限られ、基準表側のテーブルに 1 行も書かれていないことを store 側で assert |
| AT-17 | KB projection と invoke 自由文の安全射影（§KB projection／証拠文書の入力形） | `/ingest` の正常な自由文が `*_safe` として projection に載る。invoke の正常な span / 申請本文は全 LLM message 上で byte-identical | 全角 PII / 命令文を span と申請本文へ投げ、全 LLM message と全 checkpoint に原文が到達しない。申請本文の指示形は当該フィールドだけ withhold され deterministic gap + 人手確認になる。KB 側の命令文は reject して書かない |
| AT-19 | HITL full lifecycle（§HITL 方針） | `/invoke` → `awaiting_human` + `thread_id` → `/resume`(`confirm`) → **`status="success"` に到達**し `node_history` が finalize まで伸びる。`refine` は Stage 3 を再実行して再度 `awaiting_human`。`discard` は success + `discarded_by_reviewer` かつ finding 6 配列と `evidence_sources` がすべて空、pins / conditions / freeze facts は保持 | 「漏洩なし / suspend=awaiting_human」だけを見る PB では checkpointer 3 契約の 3 バグとも捕捉できない（レビューと CI が素通りした実績）。**resume が success に到達すること**と discard 後の 7 配列を assert する |
| AT-20 | `/resume` の認証と所有権（§`/resume` の認証と所有権） | 正当な token + 自分の thread → 200 | token 無し(401) / 不正 token(401) / `thread_id` 無し(422) / 未知 thread(404) / 別セッションの thread(403)。**いずれも resume が実行されていない**ことを assert |
| AT-21 | resume 応答のサニタイズ（§resume 応答のサニタイズ） | `{action: "confirm"}` が通り、PII note は mask 後の値だけが resume へ渡る | 未知キー / 非 `str` キー / サイズ超過 / enum 外 `action` / `note` に命令文 → reject。HTTP 経路では **`agent.resume()` 0 回かつ outer / inner checkpoint 不変**。key→enum→PII→injection の順序を spy で固定する |
| AT-22 | `hitl_allowed` ガード | `hitl_allowed: False` で invoke → `interrupt()` せず `refine_action: "confirm"` で Stage 4 へ直行し `success` に到達 | ガードを外すと自動パイプラインがデッドロックすることを変異検査で確認 |
| AT-18 | 過去判例の照合（§過去判例の照合） | 過去レビューが 1 件以上想起されると `precedent_checks[]` が出る。`prior_issue` が `recall_refs[].decision_rationale_safe` と、`prior_outcome` が `recall_refs[].decision_outcome` と完全一致し、`status: addressed` には `source_ids` が付く | `prior_issue` を LLM が書き換えた応答 → 10-c で reject。`prior_outcome` を prompt に渡していないこと（prompt 文字列の assert）。`status: addressed` で `source_ids` 空 → 10-d で reject。KB が空のとき `precedent_checks: []` かつ `degradation_code: "kb_empty"` |
| AT-23 | `DELETE /requests/` の cross-store 契約（§保持と削除の契約） | 既存 `request_ref` を削除 → 200 + `{db_rows, kb_docs: 0, payloads}` の件数と `audit_event_id` が返り、`audit_events` に `actor_id` / `approval_ref` / `outcome: "success"` / `deleted_counts` / `target_type: "request"` / `target_ref: <request_ref>` が残る。**suspend 中の申請を削除すると `hitl_state` / `hitl_thread_id_hash` / `hitl_suspended_at` が保存されていた checkpoint も消える** | live record も tombstone も無い → **404 `request_not_found`** かつ mutation ゼロ／body 付き → 400／permission 無し → 403 かつ削除ゼロ／`X-Approval-Ref` 欠落 → 400 `approval_ref_missing`／**子 store の削除を 1 つ失敗させて 500 `delete_incomplete` になり tombstone が残り、同じ ref への再実行が続きから再開して 200 になる**（冪等）／**500 の後にプロセスを再起動しても tombstone の `delete_plan` / `delete_progress` / `delete_started_at` / `delete_updated_at` から再開して 200 になる**（同一プロセス内の retry だけでは永続化を検証できない）／`hitl_state: "suspended"` の申請を削除すると **checkpoint も消えている**こと |
| AT-24 | 削除系統の分離と対称性（§保持と削除の契約） | `DELETE /perspectives/{history_ref}` で `review_perspectives` の行と対応する KB projection が消え、`deleted` が `{db_rows, kb_docs, payloads: 0}` になり、`audit_events` に `target_type: "perspective"` / `target_ref: <history_ref>` が残る | **`DELETE /requests/{request_ref}` を実行しても `review_perspectives` と KB projection が 1 件も減らない**（過去観点は申請に属さない独立資産）／`freeze.request.delete` **だけ**を持つ caller は 403（`freeze.perspective.delete` が必要）／未知 `history_ref` → 404 `perspective_not_found`／body 付き → 400／`X-Approval-Ref` 欠落 → 400／**DB 削除成功後に KB 削除を失敗させて 500 `delete_incomplete` になり、プロセスを再起動しても `delete_plan` / `delete_progress` から KB の続きを再開する**／`audit_events` はどちらの削除でも減らない |
| AT-25 | payload の生存期間（§payload の生存期間契約） | `/invoke` → `awaiting_human` の後も payload が store に残り、`/resume`(`refine`) の Stage 3 が**同一 payload を再取得して**再ドラフトできる | suspend 応答時に payload を削除する実装に変異させると `refine` が本文を引けず落ちること／TTL 失効後の `refine` が **200 `status="error"` + `payload_expired`** になり、**縮退（成功扱い）に化けない**こと／terminal 到達後に payload と outer / inner checkpoint が消えていること |
| AT-27 | HITL セッションの期限（§HITL セッションの生存期間契約） | `hitl_session_ttl_s` 到達後に checkpoint と payload が消え、`hitl_state` が `none` に戻る | 期限後の `/resume` が **410 `hitl_expired`**（404 ではない）。`hitl_expired` イベントが `{request_ref, thread_id_hash, occurred_at, action, outcome}` で残る。`refine_count` は `/invoke` 時に 0 で初期化され、`refine` 受理のたびに増えることを併せて assert する |
| AT-28 | 監査の参照経路（§保持と削除の契約） | `freeze.audit.read` を持つ caller が `GET /audit-events/{event_id}` で削除イベントを取得し、`actor_id` / `approval_ref` / `target_type` / `target_ref` / `deleted_counts` が揃っている | 当該 permission を持たない caller は 403。監査ロールが `freeze.request.delete` と `freeze.perspective.delete` の**どちらも持たない**ことを配線側で assert。**日次 purge が送る `X-Approval-Ref: retention:<policy_version>:<run_id>` がそのまま `audit_events.approval_ref` に保存される**ことも検証する |
| AT-26 | `refine` の回数上限（§HITL 方針） | `max_hitl: 2` で `refine` を 3 回送ると、3 回目は **interrupt せず** `status="success"` + `meta.degradation_code: "hitl_limit_reached"` + `requires_human_review: true` に到達する | カウンタ検査を外す変異で**無限に suspend し続ける**ことを確認（`max_hitl` を宣言しただけでは Cat 2 では何も止まらない） |

PB（proof-of-boundary）は scaffold 既定の PB-1〜PB-6 に加え、**PB-1 は各 node module の `audit.emit`
binding を patch し、通常 return と通常 emit より前の強制例外の双方でドメインイベントを実測する**
（framework 自動イベントを見ていると domain emit を全削除しても緑になる）。
PB-7 は **assertion 必須**（HITL 有効のため auto-waive されない）。stub は `config/config.yaml` を読む現行正本を写経する。永続層に対する PB として
**PII / credential が KB に索引化されないこと**と、**接続解決が fail-closed であること**（default 接続を持たない・ハンドルをキャッシュしない・`thread_id` 束縛の payload 解決）を追加する。

🔴 **checkpoint の平文検査は「保留後」だけでは足りない。** `BaseGraph.invoke()` が生入力を `user_input` に載せる以上、**最初の checkpoint（START 直後）に平文が入る**のが典型的な漏れ方である。PB は **outer / inner それぞれの saver から全 checkpoint を列挙し、msgpack を復号して**4 自由記述の原文が 1 つも含まれないことを検査する（`repr()` やバイト検索は blob に対して必ず vacuous）。entry adapter の射影を外す変異で**この PB が落ちること**まで確認する（§機微データの 2 層統治）。

## 縮退動作（導入初期・データ未整備時）

| 状況 | 挙動 |
|---|---|
| KB に過去観点が無い（導入初日） | Stage 2 は空を返し `meta.degradation_code: "kb_empty"`。`precedent_checks: []` になり、Stage 1 の `field_gaps` と Stage 3 の基準照合だけでレビューパケットを返す（**初日から使える**。過去判例の照合は蓄積が進むほど効く — これが蓄積 moat の効き方そのもの） |
| 例外承認基準の pin が解決できない（未登録・hash 不一致・未承認・期間外） | fail-closed（§統制文書の版固定入力 の 4 検証それぞれ固有の `error_code`）。基準なしの「十分性」は定義できない |
| 凍結カレンダーが未取込 / 未指定 | `freeze_window_facts` = `{freeze_calendar_ref: null, evaluated: false, overlapping_windows: [], planned_window: <入力値>, evaluated_at: <State 値>}` + `evidence_gaps` に「凍結カレンダー未提供」（`gap_origin: "deterministic"`）。invoke 自体は成功する |
| LLM 未注入（`config["llm"] is None`） | `meta.degradation_code: "llm_unavailable"`。Stage 1 の決定論結果のみを返し、Stage 3 由来の区分は空配列。🔴 **この経路では `hitl_review` は `interrupt()` しない** — レビュー対象のドラフトが存在しないため、保留しても担当者に見せるものが無い（`hitl_allowed` ガードと同じ経路で Stage 4 へ直行する） |
| LLM 注入済みで実行時失敗 | **縮退させない**。`status="error"` + `error_code="llm_call_failed"` |
| `refine` が `max_hitl` に到達 | `status="success"`。**envelope は `INVOKE_TOP_LEVEL_KEYS` を完全に満たし、各区分は通常の Stage 4 出力**（`discard` と違い検証を通っているので空配列にしない）。`meta = {degraded: true, degradation_code: "hitl_limit_reached", requires_human_review: true}`。**error にしない**（上限まで対話した正常な運用結果である・§HITL 方針） |
| 担当者が HITL で破棄（`discard`） | `status="success"`。**envelope は `INVOKE_TOP_LEVEL_KEYS` を完全に満たす**（S-3 拒否条件 #1 は discard 経路にも適用される）。`meta = {degraded: true, degradation_code: "discarded_by_reviewer", requires_human_review: false}`、`applied_pins` と `freeze_window_facts` は **Stage 1 が確定した決定論の値をそのまま載せる**（破棄しても「どの基準・どの凍結窓で見たか」は事実として残す）、`criteria_conditions` も Stage 1 の値を載せる。`evidence_sources` / `supported_conditions` / `evidence_gaps` / `contradictions` / `approval_scope_discrepancies` / `precedent_checks` / `verification_questions` は **すべて空配列**（Stage 4 の検証を通っていない候補を出さない）。**error にしない**（正常な運用結果である） |

## Professional Services Engagement Acceptance Evidence Agent / FinancialOutsourcingDueDiligenceEvidenceBriefAgent との差異（Design 入場条件 (e)）

実コードを参照して比較した（`Idea/2481-*/repo` / `Idea/2484-*/repo`・2026-08-20 確認）。

| 観点 | SVC-C2-144（本テンプレ） | Professional Services Engagement Acceptance Evidence Agent（受任受入証拠） | FinancialOutsourcingDueDiligenceEvidenceBriefAgent（外部委託 DD 証拠ブリーフ） |
|---|---|---|---|
| 規則体系 | MSP の**変更凍結ポリシー・例外承認基準** | 事務所の**受任方針**（利益相反・独立性・AML） | 金融監督の**外部委託管理**要求 |
| 利用者 | MSP の**変更管理担当者**（CAB 前） | 専門サービス事務所の**受任受付担当者** | 金融機関の**外部委託管理担当者** |
| 判断単位 | 「この変更をこの時期に通す例外理由が承認基準を実質的に満たすか」 | 「この新規案件を受任してよいか」 | 「この委託先の DD 証跡が監督要求を満たすか」 |
| 時間次元 | **あり（凍結時間窓との重なり・ロールバックの時間的実現性）** | なし | なし |
| 入力コーパス | 変更内容・例外理由・ロールバック計画・リスク評価・凍結カレンダー・顧客承認参照 | 依頼者情報・案件叙述・関係者・KYC 資料 | 委託先 DD 資料・契約・再委託構造・監査報告 |
| KB スキーマ | `case_evidence` / `review_perspectives` / `perspective_candidates`（**統制文書は KB に持たず版固定入力**） | `acceptance_policies` / `review_perspectives` | 統制表・観点表は**版固定入力**（FinancialOutsourcingDueDiligenceEvidenceBriefAgent で確立した型。本テンプレはそれを踏襲） |
| 出力区分 | `freeze_window_facts` / `supported_conditions` / `evidence_gaps` / `contradictions` / **`approval_scope_discrepancies`** / `verification_questions` | `policy_clauses` / `candidate_conflicts` / `gaps` / `verification_questions` | 委託先評価項目の充足・不足論点 |
| 排除フィールド | 承認可否 / リスク受容 / 変更可否 / **顧客承認カバレッジ結論** / SLA 責任 | 受任可否 / 法的助言 / 独立性阻害判定 / AML 格付 | 委託可否 / 監督上の適合判定 |
| 実装構成 | parts 写経（106 と同系） | parts 写経（`src/<part>/` 配置） | 自前実装（parts 化前の世代） |

**共通するのは「証拠ブリーフ型の非結論契約」という設計パターン（4 段骨格）のみ**であり、
規則体系・利用者・判断単位・入力・KB スキーマ・出力区分・排除フィールドが全て異なる。
とくに**時間窓という制約次元は本テンプレ固有**であり、Stage 1 の決定論と Stage 4 の数値照合が
他 2 本には存在しない。3 本を 1 テンプレに統合すると、どの顧客にも合わない config 分岐の塊になる。

## `config/agent.yaml` / `config/config.yaml` の設計値

**`config/agent.yaml`（静的マニフェスト・root level・`agent:` ネスト禁止）:**

```yaml
id: "SVC-C2-144"
name: "ChangeFreezeExceptionReviewAgent"
namespace: "svc"
version: "0.1.0"
enabled: true
category: "Cat 2"
generation_mode: "llm"                 # Stage 3 が config["llm"] 注入シームで実 LLM を呼ぶ
industry: "SVC"
base_type: "AgentBaseGraph"
class: "src.graph.graph.Graph"         # MR-B で ChangeFreezeExceptionReviewGraph にリネーム（実装と同一 commit）
required_trust_level: "VERIFIED_EXTERNAL"
requires:
  extras: ["anthropic"]
  secrets: []
```

> **`requires.secrets` を空のままにする理由:** この欄は `require_at_compile()` が**起動時に hard-fail**
> させる「起動時に必ず存在すべき秘密」の宣言欄である。scaffold の `src/api/server.py` は
> `ANTHROPIC_API_KEY` を `.get()` で読み、鍵が無ければ `config["llm"] = None` に縮退する契約になっている
> （`deploy-stg` は鍵を持たない）。ここに載せると server 契約と矛盾して**鍵なし起動が不能**になる。
> DB / KB の接続情報も同様に、リクエスト時に resolver が解決するものであって起動時必須ではない。

**`config/config.yaml`（runtime パラメータ）:**

```yaml
# --- Retrieval ---
top_k: 8                              # Stage 2 が想起する過去観点の件数

# --- Evidence contract ---
min_quote_chars: 12                   # 逐語引用の長さ下限（空文字・1 文字の vacuous 照合を防ぐ）
max_quotable_chars: 120               # これを超える span は散文とみなし引用不可（span_kind: unclassified）
min_recall_hits: 3                    # Stage 2 の最低一致数（絶対値。比率にしない）
max_rationale_chars: 1000             # モデル可視 *_safe 射影の上限

# --- Input quality gate（NFKC + ゼロ幅除去の後に判定。比率は正規化後の文字数に対する） ---
text_gate_max_replacement_ratio: 0.02  # U+FFFD 比率がこれ以上で reject
text_gate_max_control_ratio: 0.01      # 制御文字比率がこれ以上で reject
text_gate_min_readable_ratio: 0.80     # 可読文字比率がこれ以下で reject
text_gate_max_chars: 20000             # 1 フィールドの長さがこれ以上で reject

# --- Data governance ---
# 期限列挙jobは本runtimeに無いため retention_days / audit_retention_days は置かない。
# 保持日数は外部データライフサイクル管理のポリシー入力とする（§保持と削除の契約）。
payload_ttl_s: 3600                   # payload_store の TTL。HITL 保留を跨ぐため滞留想定より長くする
                                      # （§payload の生存期間契約）

# --- Execution ---
max_retry: 3
timeout_s: 60

# --- Checkpointing / HITL ---
memory_enabled: true                  # hitl.enabled: true には必須（無いと compile 時 ConfigError）
hitl:
  enabled: true
  max_hitl: 8                         # 再ドラフトの最大回数。framework は Cat 2 で強制しないが、
                                      # 本テンプレは refine_count で強制する（§HITL 方針）
hitl_session_ttl_s: 3600              # 保留 checkpoint の期限。到達で checkpoint+payload を削除し
                                      # 以後の resume は 410 hitl_expired（§HITL セッションの生存期間契約）
auth_runbook_url: "docs/07_operation_guide.md#認証情報の発行更新失効"
                                      # FastAPI lifespanで非空を必須検証（運用ガイド §認証情報の発行・更新・失効）
```

> runtime 運用パラメータ（`top_k` / `min_quote_chars` / `payload_ttl_s` / `max_rationale_chars`）は
> **テンプレ利用者が自環境で設定する前提の初期値**である。基準日時は config に置かず、
> リクエストごとに entry adapter が State の `evaluated_at` として載せる（§凍結カレンダー 契約 #2）。

## SDK 実機スパイク（AgentCore SDK 1.0.1・2026-08-20 確認）

設計が依拠する framework / shared のシンボルを venv で introspection して確認した
（CI の `AGENTCORE_WHEEL_SPEC` = `agenticstar-agentcore[anthropic]==1.0.1` と同一版）。

| 確認項目 | 実機の結果 | 設計への反映 |
|---|---|---|
| `InvocationContext` のフィールド | `correlation_id` / `session_id` / `thread_id` / `parent_trace_id` / `caller_id` / `caller_trust_level` / `secrets` / `hitl_allowed`。**`tenant_id` / `org_id` は無い** | 顧客識別は `caller_id` からの認可解決。`session_id` は client echo なのでスコープ鍵に使わない |
| `AgentBaseGraph.get_output()` | `output` に `formatted_output` → `result` の順で拾う | 最終 envelope は `formatted_output` に載せる |
| S-2 `_PII_SCAN_FIELDS` | `("user_input", "validated_input", "llm_response")` のみ。injection ブロックは `user_input` / `validated_input` | それ以外の受領フィールドは `_extra_security_gate_input()` で明示的に `detect_pii()` |
| S-3 の既定検査 | `detect_credentials_in_value` を `result.items()` の各値に適用（dict/list/tuple を**再帰**）。**PII は出力側で走らない** | exact-key/type/enum と PII 残存検査は `_extra_security_gate_output()` が明示実装。キー/断定フレーズの denylist は持たない |
| `_extra_security_gate_output` の引数 | `result` のみ（**`state` を受け取らない**） | mode 分岐は `result["mode"]` の宣言値で行う |
| `GraphNode` の abstract | `get_subgraph` / `extract_input` / `merge_output`。既定 `error_strategy="propagate"` / `propagate_hitl=False` | 3 メソッドを実装し `error_strategy="handle"` と `propagate_hitl=True` を明示宣言 |
| `AgentStatus` の値 | `pending` / `success` / `retry` / `error` / `timeout` / `awaiting_human` / `cancelled` | `status` は必ず `AgentStatus.<X>.value`（文字列）で返す |
| `TrustLevel` の値 | `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL`（文字列 enum・`int()` 比較不可） | §S-1 の表のとおり |
| `BaseLLM` の抽象 | `complete(messages: list) -> dict` / `stream` / `bind_tools` | Stage 3 は `complete()` を呼ぶ（`invoke()` ではない） |
| `emit_trace_event` | `(event_type: str, payload: dict, state: dict) -> None`（`shared.utils.audit_logger`） | 各 `execute()` 内で最低 1 回 |

## Import Isolation Confirmation
- [x] Level 0（`agenticstar`）を import しない
- [x] import 先は `framework/` と `shared/` のみ（`agents/base/` は不要）
- [x] 他テンプレを直接 import しない
- [x] 写経した parts は `src/<part>/` に置き、消費側は **`from src.<part> import ...` に統一**
      （top-level import は editable install の shim 依存になり非 editable 経路で `ModuleNotFoundError`）

## EU AI Act Annex III

**Not in scope.** SVC は EU AI Act 判定の対象業種（FIN / GOV / HCR / ENE / RET）に含まれず、
出力は承認可否・リスク受容の判定を持たない。提案書 §EU AI Act Annex III Scope Declaration を正とする。

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | **A** | 自律ループではなくモードごとに終端が決まる固定パイプライン |
| Composition pattern | `GraphNode`（内側 subgraph） | 3 スロットに直接実装 | **A** | Stage 1〜4 の直列 + 3 モード分岐を内側の独自トポロジで表現する必要がある（Cat2 標準） |
| 顧客分離 | 接続レベル分離（導入先専用 DB/KB） | 行レベル `tenant_id` フィルタ | **A** | テンプレート標準の既定。`InvocationContext` に `tenant_id` が無いことも実機確認済み |
| 1 デプロイ内の区画キー | 区画なし **`SCOPE_KEYS=()`**（明示宣言） | `SCOPE_KEYS=("customer_ref",)` | **A** | ユーザー裁定（2026-08-20）。本テンプレは**1 社が自社の変更管理のために自社デプロイで使う**前提で、1 デプロイに複数社のデータは同居しない。区画が実在しないのにキーを置くと、全行に同じ値が入るだけのカラムと「マルチテナント分離をしている」という誤読を生む。分離は接続レベル |
| 時間窓判定の担い手 | Stage 1/4 の決定論 | Stage 3 の LLM | **A** | ISO 文字列比較の破綻・LLM の算術は根拠なき数値を所見に載せる。実測済みの 2 バグ |
| S-3 の主防御 | closed allowlist（構造） | 禁止語 denylist | **A** | denylist は言い換えで原理的に閉じない（テンプレート標準の既定） |
| 引用の非結論性 | `span_kind` allowlist（引用可 5 種・既定 `unclassified` で fail-closed） | 逐語引用のみで足りるとする | **A** | 逐語引用は**出所しか保証しない**。証跡側に裁定文があれば正しく引用するほど結論が漏れる（MunicipalContractVariationEvidenceBriefAgent で確立） |
| HITL | 非採用 + `/feedback` 非同期 | **D6 `interrupt()`** | **B**（ユーザー裁定で初版の A を却下） | suspend/resume の 4 問がすべて Yes（§HITL 方針）。対話相手は CAB ではなく**パケットを仕上げる担当者**で、画面の前におり数分で返る。LLM ドラフトと `source_registry` を引き継がないと「同じドラフトへの修正」にならない。**`/feedback` は確定後の蓄積口であって対話経路ではない** |
| `error_strategy` | `handle`（安定 error code に射影） | `propagate` | **A** | provider 例外の生文字列を State/trace に残さないため。部分結果は返さない点は同じ |
| source ID の採番 | `source_registry` から LLM が**選択**のみ | LLM が採番 | **A** | LLM に採番させると捏造 ID を作れてしまう |
| 過去実績の KB 保持 | 3 点セット（申請要約・結論・判定理由） | 判定理由のみ | **A** | 理由だけ切り出すと観点として成立しない。結論の再流入は検索軸・証拠適格・prompt 役割の 3 箇所で止める |
| 入力ファイル形式 | csv 等の軽量テキストのみ | PDF / xlsx も受理 | **A** | パーサ増・OCR 崩れ・依存膨張を避けるテンプレート標準の既定 |
| `requires.secrets` | 空（起動時必須の秘密なし） | `ANTHROPIC_API_KEY` 等を宣言 | **A** | `require_at_compile()` の hard-fail と server の縮退契約が矛盾し、鍵なし起動が不能になる |
| 日本語 PII 補完 | FW 標準 `detect_pii` のみ | 姓辞書・敬称起点の自前補完 | **A** | テンプレート標準の既定（抜け漏れと過検知・保守負担だけが残る）。残余は 運用ガイド に既知の限界として記録 |
| 例外承認基準・凍結カレンダーの扱い | **版固定入力**（`ref` + `version` + `content_hash` を invoke で受け、正本レジストリから解決・検証） | `/ingest` で KB に蓄積し検索で想起 | **A** | 実務では承認・有効期間を持つ配布物であり、監査は「どの版を適用したか」を要求する。KB に貯めて AI が判定基準を自動更新する形は変更管理の統制として成立しない（FinancialOutsourcingDueDiligenceEvidenceBriefAgent で確立した型を踏襲）。🔴 **これは「KB に何も貯めない」という意味ではない** — 版固定にするのは**判定基準そのもの**（承認基準の条文・凍結カレンダー）だけであり、**過去の例外レビューの判断材料は KB に蓄積して想起する**（§過去判例の照合）。蓄積 moat はそちらが本体であり、失われない |
| KB に載せる自由文 | **3 値分離**（`_raw` は DB のみ / `_audit` は監査 / `_safe` のみ model 可視） | PII マスクのみ | **A** | KB の自由文は次回の prompt に載る＝**prompt injection の永続経路**。マスクだけでは命令文が残る（Professional Services Engagement Acceptance Evidence Agent のレビューで検出された型） |
| 過去判例の照合を出力に出すか | **`precedent_checks` として出す**（`prior_outcome` は決定論で写し、LLM prompt には渡さない） | 想起するが出力には出さない（prompt の補助情報に留める） | **A** | 過去の裁定は組織の事実であり CAB の正当な判断材料。塞ぐのは「過去が承認されたから今回も」という AG の推論であって過去実績の提示ではない。出力に出さないと**蓄積 moat が使う側から見えない**（過剰に塞いで moat を潰しかけた前例がある） |
| Stage 2 の日本語想起 | 7 文字以上を 2-gram 展開 + 最低一致数を絶対値で置く | 空白・句読点での語分割のみ | **A** | 日本語の業務文は空白で区切られないため語分割だけでは実務入力で 0 件になる。形態素解析器は入れない（backend 差し替え前提 + 依存制限） |

### レビュー対応で確定した設計変更（2026-08-20）

運用ガイド を scaffold の空テンプレから実体化する過程で、**運用として実行できない設計**が
複数見つかった。運用手順を書くと「その操作を実行する手段が設計にあるか」を 1 つずつ問うことになるため、
設計だけを見ていたときには通っていた記述の穴が出た。以下はその対応であり、**いずれも既定適用**
（業務スコープの変更ではなく、設計の内部整合の回復）である。

| # | 指摘元 | 論点 | 確定した設計 | 採らなかった案と理由 |
|---|---|---|---|---|
| R-1 | レビュー指摘 High | 提案書 が「PII 残余は 運用ガイド に記録する」と宣言し、本設計書 が保持・削除・HITL の運用帰結を設計しているのに、運用ガイド は scaffold の空テンプレのまま | 運用ガイド を本テンプレ固有の運用文書として実体化し、提案書 / 本設計書 から節名付きで相互参照 | 「運用ガイドは後段ステージの成果物だから設計段階では空でよい」— **採らない**。ステージゲート上そうであっても、**他 doc が実体を参照している以上その参照は空振り**であり、レビュー指摘 High の原因 A（宣言に実体が無い）そのもの |
| R-2 | レビュー指摘 H-1 | 「本文は State に載せず `payload_ref` のみ」と書いていたが、`BaseGraph.invoke()` が生入力を `user_input` に載せるため **最初の checkpoint に平文が入る**経路が塞がっていなかった | 射影は **`invoke()` に渡す入力そのもの**に掛ける。`user_input` / `validated_input` に平文を入れない。PB は START 直後を含む全 checkpoint を復号検査する | 「後段のノードで State から消す」— **採らない**。checkpoint は super-step ごとに取られるので、入れてから消すのは無効 |
| R-3 | レビュー指摘 H-2 | `refine` は Stage 3 を再実行して本文を再取得するのに、payload の生存期間が保留を跨ぐと定義されていなかった。`payload_ttl_s: 900`（15 分）は想定滞留「数分〜数十分」に足りない | §payload の生存期間契約 を新設。**suspend では削除しない**／terminal・`DELETE`・TTL で削除。既定を **3600 秒**へ。TTL 切れ後の `refine` は `payload_expired` で fail-closed | 「TTL を無期限にする」— **採らない**。機微本文の保持を長くするほど露出が増える。**可用性（refine が使える）と保持リスクのトレードオフを 60 分に置き**、運用が調整できるよう 運用ガイド に両方向の影響を書いた |
| R-4 | レビュー指摘 H-3 | `max_hitl` を宣言しているが Cat 2 では framework が強制せず、正当な `refine` は無限に繰り返せた（「決定論ルートで収束を保証」は*不正値による暴走*の話であって回数の話ではない） | State に **テンプレ所有の `refine_count`** を持ち、`hitl_review` が `interrupt()` の前に上限を検査する。上限到達時は **interrupt せずその時点のドラフトを検証して確定**し `hitl_limit_reached` + `requires_human_review: true` | 「上限超過は 409 で拒否」（レビューでの案）— **採らない**。それまでの対話の成果を捨てることになる。打ち切って確定し、人間が続きを判断できる形の方が安全側 |
| R-5 | レビュー指摘 H-4 | `review_perspectives` は `request_ref` を持たない（主キーは `history_ref`）ため、`DELETE /requests/` では**構造的に消せない**のに 運用ガイド は「cascade 削除される」と書いていた | 削除を **申請系 / 過去観点系 / 監査系の 3 系統**に分離し、`DELETE /perspectives/{history_ref}` を新設。`DELETE /requests/` は KB を触らない（`kb_docs` は常に 0） | 「`review_perspectives` に `request_ref` を持たせて cascade する」— **採らない**。過去観点は特定の申請に属さない独立資産＝蓄積 moat の本体であり、申請を消すたびに moat が削れる |
| R-6 | レビュー指摘 H-5 | DB と payload store を跨ぐ削除の部分失敗が定義されておらず、今回追加した「未知 ref → 404」が**部分削除の残骸を隠す**方向に働いていた | tombstone 方式（`status: "deleting"` へ遷移 → 子削除 → 親削除）。部分失敗は **500 `delete_incomplete`** で tombstone を残し、同じ ref への再実行で再開（冪等）。**404 は live も tombstone も無い場合のみ** | 「常に 200 冪等（0 件でも成功）」— **採らない**。`request_ref` の typo を検出できない。tombstone があれば「消え残り」と「もともと無い」を区別でき、404 の目的を保ったまま R-6 を解ける |
| R-7 | レビュー指摘 H-6 | `audit_events` が `event_id` / `correlation_id` / `event_type` / `request_ref` / `occurred_at` しか持たず、運用ガイド が要求する「承認記録と削除の 1:1 突合」が実行できなかった | `actor_id` / `approval_ref` / `outcome` / `deleted_counts` / `error_code` を追加。`DELETE` 系に **`X-Approval-Ref` ヘッダ必須**（欠落は 400）。応答に `audit_event_id` | 「承認は運用手順に委ね API では受け付けない」— **採らない**。削除は不可逆で、承認参照なしの実行を API が通してしまうと運用手順が実効性を持たない |
| R-8 | レビュー指摘 M-1 | 運用ガイド に「LLM 未注入なら起動拒否」「`ctx.secrets`」と書いたが、いずれも設計と逆（LLM 未注入は `llm_unavailable` 縮退／`InvocationContext` に `secrets` は載らない） | 運用ガイド を設計に合わせて修正。鍵の失効は**注入済みクライアントの実行時失敗**なので `llm_call_failed` であり、`llm_unavailable`（未注入）ではないと明記 | — （こちらの記述誤り） |
| R-9 | レビュー指摘 M-2 | PII 緩和として「申請者名は構造化フィールドへ」と案内したが、`/invoke` に氏名フィールドは**存在しない**（`approved_by` は `/ingest` の過去観点専用） | 「現在申請の個人名は AG に送らず、申請元システムが `request_ref` に紐付けて保持する。AG には役割名か非個人の principal ID のみ」に修正 | — （こちらの記述誤り。実行不能な運用を案内していた） |
| R-10 | レビュー指摘 M-5 | 本設計書 が「suspend 後に親が `hitl_allowed=false` へ変わっても resume が止まらない制約を 運用ガイド に記す」と宣言していたが実体が無かった（R-1 と同型） | 運用ガイド に §既知の限界 を新設し、gateway での resume 時 HITL 再検証を緩和策として記載 | — |
| R-11 | レビュー指摘 M-7 / Low-1 | `criteria_seal_unresolved` / `freeze_calendar_seal_unresolved` がエラーコード表に無い。`/resume` と `DELETE` の 2 行が markdown 表の外に孤立してレンダリングされていなかった | エラーコード表に追加。孤立行を API 表内へ移動 | — （前者は定義漏れ、後者は改訂時に作り込んだ構造破壊） |

| R-12 | 2巡目レビュー High-2 | `hitl_limit_reached` を新設したのに State と S-3 の `degradation_code` enum に足していなかった。**AT-26 が期待する success envelope は S-3 拒否条件 #11 で落ちる** | 両 enum に追加し、「片方だけに値を足すと #11 で落ちる」ことを enum 定義の隣に明記 | — （新しい値を導入したのに allowlist を更新しない、という今回 4 回目の同型ミス。**enum の定義箇所を 2 つ持っていること自体が原因**なので、相互参照を張って気付けるようにした） |
| R-13 | 2巡目レビュー High-2 | `refine` 回数の判定が off-by-one（「受理時に +1 → interrupt 前に `>= max_hitl`」だと `max_hitl: 2` は再ドラフト 1 回で打ち切られ、AT-26 の「3 回目」と食い違う） | `max_hitl` = **再ドラフトの最大回数**と定義し、判定を「加算する**前**に `>= max_hitl` を見る」に確定。`max_hitl: 2` なら 1・2 回目は実行、3 回目で打ち切りと具体例を表に書いた。初期値 0 も明記 | 「interrupt の総数」と定義する案 — **採らない**。運用が数えたいのは「何回書き直したか」であって suspend の回数ではない |
| R-14 | 2巡目レビュー High-1 | §payload の生存期間契約 を新設したのに、**部品表（実装用の写経指示書）は「HTTP 処理の `finally` で raw 値を削除」のまま**だった。実装時にそちらを写経すると `refine` が壊れる | 部品表を生存期間契約に合わせ、「ここを写経で戻すと `refine` が壊れる」と理由付きで書いた | — （**契約を新設したら、その契約を写経で実装する側の指示も同時に直す**。設計本文だけ直して指示書を残すのは、実装に欠陥を持ち込むのと同じ） |
| R-15 | 2巡目レビュー High-3 | `DELETE /perspectives/` に `freeze.request.delete` を再利用していた＝**通常の申請削除権限で組織全体の蓄積観点を消せる** | 専用 permission `freeze.perspective.delete` を新設。`perspective_not_found` / body 400 / 部分失敗の再開 / `payloads: 0` / audit の `target_type`・`target_ref` まで申請系と対称にし、AT-24 を拡充 | 「同じ削除権限でよい（どちらも削除だから）」— **採らない**。消す対象の価値が違う（1 申請 vs 蓄積 moat 全体）ものに同じ鍵を使わない |
| R-16 | 2巡目レビュー High-4 | `payload_ttl_s` は**本文**の期限であって checkpoint の期限ではない（`confirm` は本文を読まないので payload 失効後も resume は成立する）。永続 checkpointer では保留が無期限に残り、申請を削除しても checkpoint に派生情報が残っていた | `hitl_session_ttl_s`（既定 3600・`payload_ttl_s` と揃える）を新設。期限到達で checkpoint と payload を削除し、以後の resume は **410 `hitl_expired`**（404 と区別）。`DELETE /requests/` の cascade に保留 checkpoint を含める | 専用の HITL セッション台帳テーブルを作る案（レビュー提案）— **採らない**。1 申請 = 1 セッションなので `freeze_exception_requests` の 3 列（`hitl_state` / `hitl_thread_id_hash` / `hitl_suspended_at`）で足りる。`thread_id` は生値でなくハッシュを保存する |
| R-17 | 2巡目レビュー High-5 | tombstone 契約が「削除対象 ID と件数を永続化する」と書いているのに、**その列が DB schema に無かった**（プロセス再起動後に再開できない） | `delete_plan` / `delete_progress` / `delete_started_at` / `delete_updated_at` を申請系・過去観点系の両テーブルに追加。AT-23 に「500 → 再起動 → tombstone から再開 → 200」を追加 | — （同一プロセス内の retry しか検証しない AT では永続化を証明できない、という指摘も併せて受け入れた） |
| R-18 | 2巡目レビュー M-1 | 「ノードが `current_secrets()` で鍵を取る」と「`config["llm"]` に起動時配線する」が併記され、**鍵の失効に再起動が要るかどうかが決まらなかった** | **起動時配線を正本**に統一（server が起動時に解決して `BaseLLM` を構築、domain node は `config["llm"]` のみ）。失効の反映には再配線が要ると明記し、Troubleshooting の「LLM 未注入で RuntimeError」も削除 | ノードで呼出ごとに解決する案 — **採らない**。テンプレート標準の既定の注入シーム（`config["llm"]`）を一本化した方が、モデル ID も鍵もノードに漏れない |
| R-19 | 2巡目レビュー M-4 | 全 `DELETE` に `X-Approval-Ref` を必須化した結果、**日次 purge ジョブが自分で承認参照を作れず 400 になる**（自動化が成立しない） | 保持ポリシー承認時に不変の `policy_approval_ref` を発行し、ジョブは `X-Approval-Ref: retention:<policy_version>:<run_id>` を送る | 自動 purge を免除する案 — **採らない**。免除は「承認参照なしで削除できる経路」を 1 本作ることであり、R-7 で塞いだ穴を自分で開け直すことになる |
| R-20 | 2巡目レビュー M-5 | `audit_event_id` を返すようにしたが、**それを読む手段（API・permission）が無く**「ID で突合できる」が操作契約になっていなかった | `GET /audit-events/{event_id}` を `freeze.audit.read` で提供。監査ロールには削除 permission を付与しない（監査と実行の分離）。AT-28 を追加 | 外部 SIEM の query を手順として書く案 — **採らない**。SIEM 製品を前提にすると導入先ごとに手順が変わる。読み取り専用 API なら AG 側で閉じる |
| R-21 | 2巡目レビュー M-2 / M-6 / M-7 | 提案書 の PII 手順と Scope が古いまま（運用ガイド だけ直っていた）。credential 最低契約が「決めること」だけで**決定値の記入欄が無く**、runbook URL の保存先も無かった | 提案書 を追随（個人名を送らない／2 つの `DELETE` を Scope に追加）。credential 表に「決定値」列を追加し、`auth_runbook_url` を config キーとして新設（空なら環境を問わず lifespan 起動を拒否） | — （**同じ事実を 3 つの doc に書いている以上、1 つ直したら残り 2 つを必ず引き直す**） |

| R-22 | 3巡目レビュー（観点を機械照合に限定） | 2 巡目で足した 10 契約が、周辺 6 箇所（State 表・S-3 定数/enum・API 表・権限表・エラー表・AT・config 3 箇所・docs 3 冊）に**部分的にしか反映されていなかった**（22 件） | 全件反映。`AUDIT_EVENT_KEYS` を定数化して `GET /audit-events` の返却も exact-key で閉じ、endpoint 数（6→7）・削除対象への保留 checkpoint・`review_perspectives` の tombstone 列の非対称も解消 | — （**3 巡目で打ち止め**。観点を「新しい設計提案はせず、既存契約の反映漏れだけ挙げる」に絞り、返ってきたのも反映漏れリストのみだった。これ以上は §8-1 の言う「対処を厚くしすぎ」に入る） |

### 実装で確定した差分（実装段階・2026-08-20〜21）

設計だけでは見えず、**実機で動かして初めて分かった**もの。設計が誤っていた箇所は設計を直し
（同じ MR 内で解消）、実装が設計に追いついていなかった箇所は実装を直した。

| # | 事象 | どちらが誤っていたか | 確定した内容 |
|---|---|---|---|
| I-1 | 射影パケットを `user_input` / `validated_input` に JSON で載せる設計 | **設計** | `BaseGraph.invoke()` は `user_input` を全ノードより先に State へ載せ、`InitializeNode` の S-2 が `pre_process` より前に走る。顧客命名の `criteria_version_ref` 等が `[MASKED]` に破壊される → **ref indirection**（内側ホップ含む）。inner に `dispatch` ノードを新設（§内部 envelope の伝送） |
| I-2 | `span_kind` の判定順序が「数量 → 日時」 | **設計** | `2026年8月20日` は `\d+日` にも一致するため、日時を先に見ないと**日付がすべて `quantity` に化ける**。正本（MunicipalContractVariationEvidenceBriefAgent）も日時が先 |
| I-3 | 初期実装で State に `records` / `feedback` の宣言が無かった | **設計** | 一度は State に宣言して欠落を解消した。その後 raw 自由文の checkpoint 残留が判明したため、最終形は I-15 の payload ref 局所解決へ移行した |
| I-4 | `payload-store` を `scope_keys=()` で使う設計 | **設計** | 部品が空タプルを明示的に拒否する（「区画が無いなら scope-bound storage を使うな」）。payload は申請単位で分離されるべきで、区画は実在する → `("request_ref",)` |
| I-5 | payload の `session_id` 束縛 | **設計** | put は entry adapter、resolve は Stage 3 で、`ctx.thread_id` は adapter が put 時に知り得ない（インスタンスごとの乱数）。**サーバが決める `session_id`** に統一し、クライアント指定は廃止（指定できると他人の body を解決できてしまう） |
| I-6 | inner 終端ノードが `status` を設定していない | **実装** | `pending` のまま返ると outer の `route()` が「success でない」と判断し、**post_process を黙って飛ばす**（envelope が空で返る） |
| I-7 | `_extra_security_gate_output` が `None` を返していた | **実装** | 契約は `-> dict`。`__call__` は**戻り値を result として使う**ため、検証のみでも `return result` が要る（返さないと全実行が error 化） |
| I-8 | entry adapter が `agent.invoke()` に `ctx` を渡していなかった | **実装** | 既定の `InvocationContext` は `ANONYMOUS`。S-1 が全ノードを拒否し、HTTP 層では認証が成功しているのに trust 拒否で落ちる |
| I-9 | `AuthContext.principal` を文字列として扱っていた | **実装** | 実体は `{"caller_id": ...}` の dict |
| I-10 | HITL 上限の判定位置 | **実装** | 設計は「`/resume` が `refine` を受理したら加算前に判定」。実装は `interrupt()` の前で判定しており、**最後の再ドラフト結果を担当者に見せずに 1 回早く打ち切っていた**。設計に合わせた |
| I-11 | resume 応答 reject 後の経路 | **実装** | 返すだけだと `route_after_hitl` が END（fail-closed）に落ちて確定できない。**上限付き（3 回）で再 `interrupt()`** し、尽きたら fail-closed |
| I-12 | `get_output()` の既定実装 | **実装** | `result` フォールバックが S-3 を迂回し、`error_code` が呼出側に届かない → override（§outer `get_output()` の override） |
| I-13 | `evidence_declarations` の設計表が flat、実装/API が nested | **設計** | `evidence_declarations[].{doc_type,doc_ref,spans[]}` を正本とし、`source_id` は document/span ordinal からテンプレが英字のみで採番する契約へ統一した |
| I-14 | 解決後の `span_text` が S-2 の対象外の custom State に raw のまま入り、checkpoint と prompt に残った | **実装** | pre-process の State 展開時に NFKC→PII マスク→指示形評価。指示形 span は固定値へ置換し、minted `source_id` だけを示す gap と人手確認を出す。正常 span の内容は変えない |
| I-15 | ingest `records` と feedback 本文も envelope から raw のまま State に展開された | **実装** | 両本文を payload store 参照にし、各終端ノードのローカル変数としてのみ解決。terminal 後に削除する |
| I-16 | KB projection が `detect_pii` 後に NFKC を掛け、全角連絡先を見逃した | **実装** | NFKC→`detect_pii`→指示形評価→切詰に統一。全角電話番号の変異検査を追加した |
| I-17 | LLM 自由文の長さ・PII 検査が Stage 4 で、Stage 3 checkpoint には未検証値が残った | **実装** | Stage 3 の State 返却前に PII マスクと上限超過 finding の drop を実施。Stage 4 の再検査も維持した |
| I-18 | result-only S-3 が `gate_kind` 値、dangling citation、空 source table、短い quote を検査していなかった | **実装** | 値・型・claim→source 実在・引用可否・最小長・provenance 一致を検査。短い registry span は Stage 4 で claim ごと drop し、post-process と S-3 でも防御する |
| I-19 | tombstone が削除計画と進捗を保存しても再実行時に使わず、累積件数が失われた | **実装** | child ID と checkpoint hash、完了 step、累積件数を tombstone から復元し、途中失敗後は未完了 step から再開する |
| I-20 | prompt が model に `gap_origin` を要求する一方、型射影は同キーを禁止していた | **実装** | model schema から `gap_origin` を除外し、Stage 4 だけが `"llm"` を付与。不正 JSON・不足 section の空 packet 化も廃止した |
| I-21 | payload store から解決した申請本文 4 フィールドが、2 通目の LLM message に raw のまま入った | **実装** | `_resolve_body()` の返却前に NFKC→PII マスク→指示形評価。指示形フィールドだけ固定値へ置換し、フィールド名だけの deterministic gap と人手確認を追加。spy は全 message を連結して検査する |
| I-22 | resume feedback を node 内で初めて検査すると、framework が raw 値を checkpoint に保存した後になる | **実装** | HTTP adapter で key→enum→PII→injection を `agent.resume()` 前に実施。reject は resume 0 回・両 saver 不変。node 内検査も直接 graph resume の防御として維持する |
| I-23 | HITL terminal 時に payload/台帳は消えるが outer / inner checkpoint が残った | **実装** | terminal 応答整形時、台帳 clear 前に保存済み thread hash から両 saver の checkpoint を物理削除する |
| I-24 | PB-1 の表は domain emit を宣言していたが実テストがなく、no-op return と予期せぬ例外で emit が欠けた | **実装** | invocation-local `ContextVar` で既存 domain emit を検知し、未発火経路だけ fallback event を出す decorator を全 template node に適用。PB はfallbackを正常判定から除外し、各 node 固有の event 型を要求する。`EVT_STAGE1` だけを抑止する変異で判定が落ちることも固定した |
| I-25 | S-3 に既知の責任境界キーを列挙する補助 denylist があり、closed schema と二重化していた | **実装** | `_FORBIDDEN_KEYS` と正規化・再帰検査を撤去。任意の未宣言キーは top-level / path別 nested exact-key schemaだけで rejectし、判定フィールドの誤追加はAT-4の契約不在テストで検出する |
| I-26 | inner の `error_code` が GraphNode 境界で安定コードとして扱われず、LLM 実行失敗が `invalid_request` に化けた | **実装** | main GraphNode に `error_strategy="handle"` と inner failure code allowlist を明示。inner は allowlist 内の code だけを `error_log` に射影し、未知/provider 由来文字列は `subgraph_failed` に閉じる。HTTP 実測で `llm_call_failed` が到達し例外文が出ないことを固定した |
| I-27 | `auth_runbook_url` と保持期間の runtime 設定が読まれず、宣言だけになっていた | **実装・設計** | FastAPI lifespan で `auth_runbook_url` の非空を必須検証し、空/非空の実 startup 対テストを追加。期限行を列挙する scheduler/job はこの成果物に無いため、`retention_days` / `audit_retention_days` は runtime config から削除し、保持期間は外部データライフサイクル管理の policy input とした |
| I-28 | HITL `discard` が status だけを変え、Stage 3 の未検証 finding と source を最終 envelope に残した | **実装** | discard 時に finding 6 配列と `evidence_sources` を明示的に空にする。Stage 1 の決定論的事実である `applied_pins` / `criteria_conditions` / `freeze_window_facts` は保持し、7 配列の空を統合テストで固定した |
| I-29 | 実 LLM が正当に返した `evidence_gaps[].condition_id: null` を Stage 3 の型射影が拒否した。`confidence` も後段は null を想定する一方、同じ前段で拒否していた | **実装** | section-qualified な nullable 契約を正本化し、`evidence_gaps` / `contradictions` / `verification_questions` の `condition_id` と `supported_conditions.confidence` に限定して Stage 3 / S-3 / prompt を同期。`supported_conditions.condition_id: null` は引き続き拒否する。実 LLM と同形の double で HITL 後の success・非空 findings/source を固定した |

**上流 parts への修正:** `entry-adapter` の同梱テストが `SCOPE_KEYS` の**既定値そのものを assert**
しており、適応点を宣言どおりに変えると必ず落ちる状態だった。テンプレ側で弱体化せず、
**上流を修正**した（既定値 assert → 派生関係 assert、scope 前提スイートに `SCOPE_KEYS` 非空の skipif）。

#### 🔴 前提の再点検（同型指摘が 2 巡したので前提を疑った）

独立レビューが 2 巡し、どちらも大量の指摘が返った。§8-1 の規律に従い、3 巡目の対処を書く前に
**前提そのもの**を点検した。

- **貫いている型は 1 つ**: 「契約を宣言するたびに、それを成立させる周辺（enum・DB 列・permission・AT・
  部品表・他 doc の同一記述）を揃え損ねる」。論点は毎回違うが、失敗の形は同じ。
- **疑った前提**: 「MR-A の設計書で運用契約をここまで確定させる必要があるか」。
  → **維持**。外すとレビュー指摘 High（運用が実行不能）が閉じず、実装フェーズの担当者が契約を自分で決めることになる。
  これは顧客・FW が課した制約ではなく工程上の要請だが、**外すと成果物が使えなくなる**種類の前提である。
- **ただし「対処を厚くしすぎ」は実際に起きていた**ので、レビューで提案された最も厚い形は採らなかった —
  perspective 専用の tombstone テーブル（→ 既存テーブルの列で足りる）、HITL セッション台帳テーブル
  （→ 1 申請 = 1 セッションなので 3 列で足りる）、外部 SIEM 手順の固定（→ 読み取り API で閉じる）。
- **構造的な再発防止**: 同じ事実の定義箇所が複数ある場所（`degradation_code` enum が State と S-3 の
  2 箇所、endpoint が提案書・本設計書・運用ガイドの 3 箇所）に**相互参照を張った**。次に値を足す人が
  「もう片方も直す」ことに気付ける形にしてある。

**この対応から得た一般形（マージ後に知見 doc へ）:** 「§X で対応」の実体確認は**同一ファイル内だけでなく
doc 跨ぎの forward reference にも適用する**。参照先が scaffold の空テンプレなら、それは「実体なし」である。

### ユーザー判断反映表（設計段階・2026-08-20）

MR-A 提示時にユーザー裁定を仰いだ項目と、その反映先。**推奨のまま通ったものと覆されたものを区別して残す。**

| # | 論点 | 提示した推奨 | ユーザー判断 | 反映先 |
|---|---|---|---|---|
| A-1/A-7 | 例外承認基準・凍結カレンダーを KB 蓄積でなく**版固定入力**にする（提案承認時点の記載を変更する） | (A) 版固定入力 | **承認（(A)）** | §統制文書の版固定入力 / §データ層設計 / §API 契約 `records[]` / DDR「例外承認基準・凍結カレンダーの扱い」/ 提案書 §Scope・§蓄積 moat |
| A-3 | HITL 要否 | (A) **非採用** + `/feedback` 非同期 | 🔴 **却下 — HITL を採用する。`/feedback` の使い方も誤り** | §HITL 方針を全面書き換え（採用・D6・`hitl_review` ノード・`refine_action` route・4 段サニタイザ・checkpointer 3 契約・`/resume` 認証）/ `config.yaml`（`hitl.enabled: true` / `memory_enabled: true`）/ `pyproject.toml`（`pyyaml==6.0.1`）/ 提案書 HITL Gate = Yes / AT-19〜AT-22 |
| A-4 | 1 デプロイ内の区画キー | (A) `SCOPE_KEYS=("customer_ref",)` | 🔴 **却下 — 1 社が自社で使う想定なので区画は無い** | `SCOPE_KEYS=()` の明示宣言。`customer_ref` / `customer_id` を State・DB 全表・payload 束縛・API request・permission 表から削除。AT-1 を接続 fail-closed + 非キャッシュ検証に変更 |
| — | KB に何を蓄積するか（A-1 の説明が誤っていた点） | 「蓄積 moat を放棄する」と記載 | 🔴 **是正 — 過去判例の蓄積と照合フローはあって然るべき** | §過去判例の照合（`precedent_checks`）を新設。提案書 §蓄積 moat を「何を貯め、何を貯めないか」に書き換え |

**A-3 で私が犯した誤り（再発防止のため明記する）:** HITL の要否を「人間の判断が要るか」で判定していた。
正しい判定軸は「**グラフの実行を止めて同じ実行の途中から再開する必要があるか**」であり、
その 4 問を当てると本テンプレは全て Yes になる。さらに「CAB の承認判断（数日）」と
「担当者がパケットを仕上げる対話（数分）」を混同し、前者を根拠に不要と結論した。
`/feedback`（確定後の蓄積口）を対話の代替として扱ったのも同じ誤りの一部である。

### ユーザー判断反映表（実装段階・2026-08-21）

MR-B 提示時に裁定を仰いだ項目。正典の MR-B 標準セットのうち B-1（核心ロジックの妥当性）/
B-2（S-3 の網羅性）/ B-4（未配線の可否）/ B-5（PII サニタイザ方針）は決着済み判例として
**既定適用**とし、報告のみで判断は求めていない。真に判断が要ったのは責任境界の変更 1 件。

| # | 論点 | 提示した推奨 | ユーザー判断 | 反映先 |
|---|---|---|---|---|
| B-3 | データ保持期間の責務を AG から外部データライフサイクル管理へ移す（設計段階では `config.yaml` に `retention_days` / `audit_retention_days` を置いていたが、実装で**コードが一度も読まない**ことが判明した。列挙 job も scheduler も本 repo に無い） | (A) 外部責務にし、runtime config から削除する | **承認（(A)）** | `config/config.yaml`（両キーを削除し、理由をコメントで明示）/ §保持と削除の契約（失効基準を「外部ポリシーの申請保持日数」等へ）/ §期限到来分の purge（外部運用ジョブと明記）/ 運用ガイド §保持期間・§purge 手順 / DDR I-26 |

**この裁定の含意:** AG は削除の**実行**（`DELETE` API・cascade・tombstone・監査記録）に責任を持ち、
**いつ消すかの決定**は持たない。動かない設定を残すと「設定したつもりで消えていない」という
最悪の失敗形になること、保持期間は顧客の内部規程・監査要件で決まるもので AG が既定値を持つと
規程との二重管理になることが根拠。

### 先回りセット照合（設計段階のレビュー指摘 先回りセット）

| # | 先回り項目 | 該当 | 本設計での対処 |
|---|---|---|---|
| 1 | provenance を「ID 実在」で終わらせない | ✅ 該当 | §S-3 拒否条件 #4（逐語引用 + 長さ下限）/ #5（`span_kind` allowlist）/ #6（ID 実在）/ #10（**本文↔source の数値双方向照合**・NFKC + 漢数字 + 単位同義語）。`cited_source_ids` は post_process が再計算 |
| 2 | 永続 KB を持つなら統治契約を design 入場条件に | ✅ 該当 | §データ層設計（DB 5 表 + KB projection の含める/含めない表 + 保持/削除 + 監査）。分離は**接続レベル**（テンプレート標準の既定。`tenant_id`/`org_id` の行フィルタは採らない）、1 デプロイ内の区画は**持たない**（`SCOPE_KEYS=()` の明示宣言・単一顧客デプロイ）。PII/credential が KB に索引化されないことの PB を `docs/03_test_spec.md` に要求 |
| 3 | PoC 検証計画を proposal に | ❌ 不採用 | 買い手実名・導入責任者・コンタクトパスを受入条件化しない（プロジェクト方針）。販売実現性は 提案書 §販売実現性についての正直な記録に**記録のみ**。当該指摘は本プロジェクトの現行運用では適用しない |
| 4 | §1「継承先」欄は継承クラスのみ | ✅ 該当 | 提案書 の Inherits は `AgentBaseGraph`（L1 直継承）のみ。`GraphNode` / inner graph は本設計書 §Architecture Overview / §Composition Pattern に置いた |
| 5 | 外部ソース確認は実施日を明記 | ✅ 該当（外部 API 依存はなし） | 外部 API / 公開データセットへの依存はない（顧客保有データのみ）。代わりに **SDK 実機スパイクの確認日（2026-08-20・SDK 1.0.1）** を §SDK 実機スパイクに明記 |
| 6 | 入力の allowlist と品質ゲート | ✅ 該当 | §API 契約「入力の allowlist と品質ゲート」（マジックバイト判定・パーサを持たない選択・索引テキストの品質ゲート・未知キーは破棄でなく reject） |

### 既知の実装欠陥に対する先回り

| 既知欠陥 | 本設計での対処 |
|---|---|
| ISO-8601 の文字列比較がオフセットで静かに壊れる | §凍結カレンダー 契約 #1（TZ-aware `datetime` に変換してから比較・オフセット省略は reject） |
| 基準日時がリクエスト内・ホスト間で揺れる | §凍結カレンダー 契約 #2（entry adapter で 1 回確定し **State の `evaluated_at`** で運ぶ。config 経由は graph 構築が 1 回なので実装不能） |
| 数値ガードの抽出器が日本語単位・助詞の隣接で無効化 | §凍結カレンダー ロールバック所要時間（NFKC + 漢数字 + 単位同義語・`\w` 境界を使わない） |
| S-3 の既定検査は credential だけ。PII は出力側で走らない | §S-3 拒否条件 #12 + §Framework Utilization の実挙動注記（自前で再帰 walk） |
| `_extra_security_gate_output(result)` は `state` を受け取らない | §Framework Utilization（mode は `result["mode"]` の宣言値で分岐） |
| checkpoint スクラバを denylist で書くと列挙外キーが平文で残る | **HITL 採用により checkpointer は使う**ので、この欠陥は本テンプレに当たる。機微本文は**そもそも State に載せない**（§機微データの 2 層統治 — 層 1 の `payload_ref` 間接化）ことで、checkpoint に載りうるのは参照と構造化フィールドだけにする。allowlist スクラバが要るのは「State に載せてよいキー」を絞る側であり、denylist は書かない |
| `uuid4()` の ref が S-2 の PII マスクで破壊される | §State Definition の注記（参照 ID は英字のみ） |
| モックが完成品を返すと安全機構の不発が隠れる | `docs/03_test_spec.md` に **prompt contract テスト**（`build_prompt()` の戻り文字列に要素実例・allowlist・閾値・指示形が現れる）と**変異検査**を要求。MR-B の push 前に実 LLM 1 回転を必須とする |
| PB-1 が framework 自動イベントを見ていると domain emit 全削除でも緑 | `docs/03_test_spec.md` の PB-1 は各 node module の binding を patch して**ドメインイベントの発火を実測**する |
| `startup()` を CI が一度も実行しない | §API 契約の末尾（`with TestClient(app):` の統合テストを MR-B 成果物に含める） |
| 部品の写経語彙が出自テンプレのまま残る | `SCOPE_KEYS` は**空タプルを明示宣言**する（区画が実在しないドメイン。写経元の `production_company_id` 等をダミーで残さない・§データ層設計） |
| LLM が「許可形」の指示に倒れて正規形を出さない | `llm-draft-stage` の adaptation（要素 1 件の実例 + allowlist を prompt に含め、**指示形**で書く） |

## ユーザビリティ設計レビュー（設計判断として修正した点）

エンドユーザー（MSP の変更管理担当者）のユースケースを想定して、以下を設計に反映した。

| 観点 | 当初案 | 修正後 | 理由 |
|---|---|---|---|
| 入力の最小性 | 例外理由・ロールバック計画を構造化 JSON で要求 | **自由記述のまま受ける**（構造化を要求するのは版参照・実施予定時間窓・証跡宣言のみ） | 担当者が手元に持つのは申請フォームの自由記述であり、事前構造化を要求すると導入前に人手が増える |
| 出力の実用性 | 論点のリストのみ | **`verification_questions` を独立フィールド**として返す | 担当者が次に取るアクション（申請者への差戻し確認）が出力からそのまま決まる |
| 出力の粒度 | 不足をリスクレベルでランク付け | **`severity` は `candidate` 固定**にし、代わりに `confidence` を数値で出す | リスクのランク付けは実質的なリスク受容判定に近づく。確信度は「AG がどれだけ根拠を持てたか」の指標であり判定ではない |
| 時間窓の見せ方 | 「凍結期間中のため要注意」と文章で出す | **`freeze_window_facts` として重なり区間を機械可読で出す**（判定文は出さない） | CAB は重なりの事実を自分の基準で解釈する。文章化すると AG が判定したように読める |
| 縮退時の挙動 | KB 空振り時にエラー | **Stage 1 の gap だけでドラフトを返し `meta.degraded` / `meta.degradation_code` を添える** | 導入初期は KB が空。過去観点が無くても「必須欄の不足」「凍結窓の重なり」は返せるので初日から使える |
| エラー時の返却 | 例外をそのまま返す | 顧客解決失敗は fail-closed で `status="error"` + 監査イベント。KB 空振りは `success` + `meta.degradation_code: "kb_empty"` | 「セキュリティ的に止めるべき失敗」と「データが無いだけ」を利用者が区別できるようにする |

## Design 入場条件の解決状況

| # | 条件 | 解決 |
|---|---|---|
| (a) | 凍結ポリシー・例外承認基準（1 版）の保有前提と未保有時の縮退 | ✅ §データ層設計 + §縮退動作 |
| (b) | 過去例外レビュー観点の KB 蓄積口と原記録／projection の分離 | ✅ §過去事例の 3 点セット + §KB projection |
| (c) | 承認可否・リスク受容・変更可否・カバレッジ結論の構造排除と不在テスト | ✅ §S-3 出力安全契約（拒否条件 #1〜#12 + accept/reject 対テスト + 不在テスト） |
| (d) | 必須欄・凍結カレンダー照合の Tool 分離（LLM に再判定させない） | ✅ §Node Configuration（inner）Stage 1 + §凍結カレンダーと時間窓の決定論契約 |
| (e) | Professional Services Engagement Acceptance Evidence Agent / FinancialOutsourcingDueDiligenceEvidenceBriefAgent の実コードとの比較 | ✅ §Professional Services Engagement Acceptance Evidence Agent / FinancialOutsourcingDueDiligenceEvidenceBriefAgent との差異（8 観点の比較表・2026-08-20 実コード確認） |
