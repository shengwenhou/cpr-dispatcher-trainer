"""台詞庫載入器：讀 adult_script.yaml，提供 canonical／variants／inserts／meta／faq 取用。

規則（SPEC 五、四之防禦層）：
- canonical：每狀態的必講句，依序播放（S5、S6 起始指令有多句，按 YAML 順序）。
- variants：每狀態 2–4 個等價變體，鼓勵語輪替——同一輪不重複（取完才重置）。
- s6.inserts：壓胸鼓勵／糾正插播池，計時輪替（隨機不重複；取完重置）。
- meta_phrases：clarify／bridge／takeover／timeout_l1／timeout_l2／tech_fault／filler。
- faq：意圖庫（id＋intent 描述＋text），供層 3 FAQ 命中後播答句。

i18n 紀律：本模組只吃 YAML 資源檔，不 hardcode 任何派遣員台詞字串；台詞一律以 id 引用，
全文從 YAML 取得。locale／scenario 由呼叫端決定路徑（見 config.script_path）。

「同一輪不重複」的輪替器（_RoundRobinPool）為純資料結構、可注入亂數，方便測試驗證。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass(frozen=True)
class Line:
    """一句台詞。id 即音檔檔名（<id>.wav）；text 為全文；of 指向其 canonical（variants 才有）。"""

    id: str
    text: str
    of: Optional[str] = None
    branch: Optional[str] = None  # 特殊分支標記（如 s1 的 fire_truck）


class _RoundRobinPool:
    """不重複輪替池：一輪內每個元素只出一次，取完自動重置再洗牌。

    - rotate() 用於 variants／inserts：隨機不重複。
    - 可注入 rng（random.Random 實例）以便測試決定性驗證。
    - 空池回傳 None（呼叫端須有 canonical 後備）。
    """

    def __init__(self, items: list[Line], rng: Optional[random.Random] = None) -> None:
        self._items = list(items)
        self._rng = rng or random.Random()
        self._bag: list[Line] = []
        self._refill()

    def _refill(self) -> None:
        self._bag = list(self._items)
        self._rng.shuffle(self._bag)

    def rotate(self) -> Optional[Line]:
        if not self._items:
            return None
        if not self._bag:
            self._refill()
        return self._bag.pop()

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class StateLines:
    """單一狀態的台詞集合。"""

    canonical: list[Line]
    variants: list[Line]
    inserts: list[Line]  # 目前僅 s6 有


class ScriptStore:
    """台詞庫。載入後提供依 id 取全文、canonical 依序取、variant／insert 輪替取。

    rng 可注入以做決定性測試；正式執行用預設隨機。
    """

    def __init__(self, script_path: Path, rng: Optional[random.Random] = None) -> None:
        self._rng = rng or random.Random()
        data = yaml.safe_load(Path(script_path).read_text(encoding="utf-8"))
        self.meta: dict = data.get("meta", {})

        self._by_id: dict[str, Line] = {}
        self._states: dict[str, StateLines] = {}
        # variant 依「所屬 canonical id」分組，供「重講當前 canonical 問句」時輪替變體
        self._variants_by_canonical: dict[str, list[Line]] = {}
        self._variant_pools: dict[str, _RoundRobinPool] = {}

        for sid, sval in (data.get("states") or {}).items():
            canon = [self._mk(x) for x in (sval.get("canonical") or [])]
            vars_ = [self._mk(x) for x in (sval.get("variants") or [])]
            inserts = [self._mk(x) for x in (sval.get("inserts") or [])]
            self._states[sid] = StateLines(canonical=canon, variants=vars_, inserts=inserts)
            for v in vars_:
                if v.of:
                    self._variants_by_canonical.setdefault(v.of, []).append(v)

        # 為每個「有變體的 canonical」建輪替池（canonical 本身也納入池，讓重講時 canonical 與變體一起輪）
        for canon_id, vlist in self._variants_by_canonical.items():
            base = self._by_id.get(canon_id)
            pool_items = ([base] if base else []) + vlist
            self._variant_pools[canon_id] = _RoundRobinPool(pool_items, self._rng)

        # s6 inserts 輪替池
        s6 = self._states.get("s6")
        self._insert_pool = _RoundRobinPool(s6.inserts if s6 else [], self._rng)

        # meta_phrases：每類一個輪替池
        self._meta_pools: dict[str, _RoundRobinPool] = {}
        for cat, arr in (data.get("meta_phrases") or {}).items():
            lines = [self._mk(x) for x in (arr or [])]
            self._meta_pools[cat] = _RoundRobinPool(lines, self._rng)

        # faq：id → (intent, Line)，並保留 intent 清單供 LLM few-shot／比對
        self._faq: dict[str, Line] = {}
        self._faq_intents: dict[str, str] = {}
        for f in (data.get("faq") or []):
            line = self._mk(f)
            self._faq[line.id] = line
            self._faq_intents[line.id] = f.get("intent", "")

    def _mk(self, raw: dict) -> Line:
        line = Line(
            id=raw["id"],
            text=raw["text"],
            of=raw.get("of"),
            branch=raw.get("branch"),
        )
        self._by_id[line.id] = line
        return line

    # ── 依 id 取全文 ─────────────────────────────────────────
    def text_of(self, line_id: str) -> str:
        """取某 id 的全文；找不到丟 KeyError（引擎邏輯錯誤，不該靜默）。"""
        return self._by_id[line_id].text

    def get(self, line_id: str) -> Line:
        return self._by_id[line_id]

    def has(self, line_id: str) -> bool:
        return line_id in self._by_id

    # ── 狀態台詞取用 ─────────────────────────────────────────
    def canonical(self, state_id: str) -> list[Line]:
        """某狀態的 canonical 句（依序，全部）。"""
        st = self._states.get(state_id)
        return list(st.canonical) if st else []

    def branch_line(self, state_id: str, branch: str) -> Optional[Line]:
        """取某狀態帶特定 branch 標記的 canonical（如 s1 的 fire_truck 引導句）。"""
        st = self._states.get(state_id)
        if not st:
            return None
        for c in st.canonical:
            if c.branch == branch:
                return c
        return None

    def rotate_variant(self, canonical_id: str) -> Line:
        """重講某 canonical 問句時，輪替取 canonical 或其變體（同輪不重複）。

        無變體池時退回 canonical 本身。"""
        pool = self._variant_pools.get(canonical_id)
        if pool is None:
            return self.get(canonical_id)
        line = pool.rotate()
        return line if line is not None else self.get(canonical_id)

    def rotate_insert(self) -> Optional[Line]:
        """S6 插播池輪替（隨機不重複）。"""
        return self._insert_pool.rotate()

    # ── meta phrases ─────────────────────────────────────────
    def rotate_meta(self, category: str) -> Optional[Line]:
        """取某類元台詞（clarify／bridge／takeover／timeout_l1／timeout_l2／tech_fault／filler），輪替。"""
        pool = self._meta_pools.get(category)
        return pool.rotate() if pool else None

    # ── faq ──────────────────────────────────────────────────
    def faq_answer(self, faq_id: str) -> Optional[Line]:
        return self._faq.get(faq_id)

    def faq_intents(self) -> dict[str, str]:
        """faq_id → intent 描述，供 LLM 意圖分類的候選集與 few-shot。"""
        return dict(self._faq_intents)
