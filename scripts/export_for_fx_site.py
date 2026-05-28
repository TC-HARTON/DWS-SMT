#!/usr/bin/env python3
"""
export_for_fx_site.py — fx.tcharton.com 用 通貨強弱 JSON エクスポータ

MT5_Python の analyzer/currency_strength.py の出力を、
fx.tcharton.com (静的サイト) の src/data/currency-strength.json
フォーマットに変換して出力する。

【重要 / 個人情報の完全分離】
本スクリプトは以下を **絶対に export しない**:
- 口座番号 / 口座残高 / 証拠金 / P&L
- 保有ポジション / 注文履歴 / 取引記録
- 個人を特定する識別子 (broker login 等)

公開するのは: **通貨強弱 (G7+XAU) の客観スコアのみ** (市場全体のデータから計算)。

【使い方】

出力先はマシン依存にしない。次のどちらかで指定する:
- 環境変数 ``FX_SITE_DATA_OUT`` に書き出し先 JSON のフルパスを設定 (一度だけ)
- もしくは実行時に ``--out <path>`` を渡す (環境変数が無い場合は必須)

```cmd
:: 環境変数を設定済みなら引数なしで:
python scripts\\export_for_fx_site.py

:: 明示指定:
python scripts\\export_for_fx_site.py --out <path-to>\\tcharton-fx\\src\\data\\currency-strength.json

:: 出力後に静的サイト側で自動 commit+push (Cloudflare Pages 再ビルド):
python scripts\\export_for_fx_site.py --git-push
```

【Windows タスクスケジューラ自動化】
docs/CLOUDFLARE-TUNNEL-SETUP.md (fx.tcharton.com 側) 参照。
推奨: 5 分間隔 + 差分あれば git push。

【SPEC 整合性】
- MT5_Python SPEC §12 currency strength の出力をそのまま転載 (改変なし)
- SPEC §22 不採用機能 (通知/自動売買/ML) には触れない
- 出力は「客観データ」 = 投資助言業に該当しない (金商法 §29 非該当)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Output path is NOT hardcoded (this repo is shared) — take it from the
# FX_SITE_DATA_OUT env var, else require --out at runtime.
_ENV_OUT = os.environ.get("FX_SITE_DATA_OUT")
DEFAULT_OUT = Path(_ENV_OUT) if _ENV_OUT else None

# 公開対象通貨 (G7 + XAU) — analyzer/currency_strength.py の出力から該当のみ抽出
PUBLIC_CURRENCIES = ["USD", "EUR", "JPY", "GBP", "CHF", "AUD", "CAD", "NZD", "XAU"]
CURRENCY_NAMES_JA = {
    "USD": "米ドル",
    "EUR": "ユーロ",
    "JPY": "日本円",
    "GBP": "英ポンド",
    "CHF": "スイスフラン",
    "AUD": "豪ドル",
    "CAD": "加ドル",
    "NZD": "NZドル",
    "XAU": "金 (Gold)",
}


def compute_strength_via_analyzer(window: str = "H4") -> list[dict]:
    """
    analyzer/currency_strength.py を呼んで通貨強弱を計算。

    Returns:
        list[{"code", "name", "score", "rank"}] sorted by rank
    """
    try:
        import config  # MT5_Python config.py
        from analyzer.mt5_connector import MT5Connector
        from analyzer.currency_strength import compute as compute_strength
    except ImportError as e:
        log.error("MT5_Python の analyzer/ を import できません: %s", e)
        log.error("MT5_Python リポジトリのルートで実行してください (analyzer/ が import 可能な状態)")
        sys.exit(2)

    conn = MT5Connector()
    if not conn.initialize():
        log.error("MT5 接続失敗 (MT5 が起動していてログイン済か確認)")
        sys.exit(3)

    try:
        # SPEC §12.4 window 指定 (H4 推奨)
        result = compute_strength(conn, window=window)
        # result は CurrencyScore のリスト想定
        scored = []
        for cs in result:
            if cs.currency not in PUBLIC_CURRENCIES:
                continue
            scored.append({
                "code": cs.currency,
                "name": CURRENCY_NAMES_JA.get(cs.currency, cs.currency),
                "score": int(round(cs.score)),
            })
        # rank 計算 (score 降順)
        scored.sort(key=lambda x: -x["score"])
        for i, s in enumerate(scored, 1):
            s["rank"] = i
        return scored
    finally:
        # Best-effort cleanup — a shutdown failure must not mask the export
        # result, but we log it rather than swallow silently.
        try:
            conn.shutdown()
        except Exception as exc:                       # noqa: BLE001 — cleanup
            log.debug("MT5 shutdown during cleanup failed: %s", exc)


def fallback_placeholder() -> list[dict]:
    """MT5 接続なしのテスト用 (本番では使わない)"""
    return [
        {"code": "XAU", "name": "金 (Gold)",     "score": 78, "rank": 1},
        {"code": "USD", "name": "米ドル",         "score": 62, "rank": 2},
        {"code": "CHF", "name": "スイスフラン",   "score": 55, "rank": 3},
        {"code": "EUR", "name": "ユーロ",         "score": 48, "rank": 4},
        {"code": "CAD", "name": "加ドル",         "score": 45, "rank": 5},
        {"code": "GBP", "name": "英ポンド",       "score": 41, "rank": 6},
        {"code": "AUD", "name": "豪ドル",         "score": 38, "rank": 7},
        {"code": "NZD", "name": "NZドル",         "score": 32, "rank": 8},
        {"code": "JPY", "name": "日本円",         "score": 28, "rank": 9},
    ]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, required=DEFAULT_OUT is None,
                   help="出力 JSON パス (未指定時は環境変数 FX_SITE_DATA_OUT を使用)")
    p.add_argument("--window", default="H4", choices=["H1", "H4", "D1", "W1"], help="集計タイムフレーム")
    p.add_argument("--lookback-bars", type=int, default=3, help="集計バー数 (analyzer 既定 3)")
    p.add_argument("--placeholder", action="store_true", help="MT5 接続なし / 既定値で出力 (テスト用)")
    p.add_argument("--git-push", action="store_true", help="出力後に tcharton-fx で git add+commit+push")
    args = p.parse_args()

    currencies = fallback_placeholder() if args.placeholder else compute_strength_via_analyzer(window=args.window)

    out = {
        "_note": "MT5_Python (analyzer/currency_strength.py) から週次 export / 個人情報は含まない",
        "_source": "MT5_Python SPEC §12 currency strength (Z-score 0..100 normalised)",
        "_disclaimer": "本データは参考情報であり、投資判断の助言ではありません",
        "_fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "_timeframe": args.window,
        "_lookback_bars": args.lookback_bars,
        "_basis": "対象通貨を含む主要ペアの 変化率正規化平均 (XAU は参照のみ / SPEC §12.3)",
        "currencies": currencies,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("wrote %s (%d currencies, window=%s)", args.out, len(currencies), args.window)

    if args.git_push:
        repo_dir = args.out.parent.parent.parent  # tcharton-fx/
        rel_path = args.out.relative_to(repo_dir)
        try:
            subprocess.run(["git", "add", str(rel_path)], cwd=repo_dir, check=True)
            # 差分なしなら commit skip
            diff = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=repo_dir,
            )
            if diff.returncode == 0:
                log.info("no diff — skip commit/push")
            else:
                subprocess.run(
                    ["git", "commit", "-m", f"data(currency-strength): MT5_Python {args.window} snapshot"],
                    cwd=repo_dir,
                    check=True,
                )
                subprocess.run(["git", "push"], cwd=repo_dir, check=True)
                log.info("git push completed")
        except subprocess.CalledProcessError as e:
            log.error("git operation failed: %s", e)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
