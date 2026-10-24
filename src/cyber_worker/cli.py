"""命令行入口。

    cyber-worker studio                      # 可视化编辑器（推荐）
    cyber-worker record OUT.yaml --app WeChat --size 1200x900
    cyber-worker anchor SCRIPT.yaml PAGE --region x,y,w,h
    cyber-worker run SCRIPT.yaml [--dry-run] [--var name=value ...]
"""

from __future__ import annotations

import argparse
import sys
import threading
from datetime import date
from pathlib import Path


def _size(s: str) -> tuple[int, int]:
    w, h = s.lower().split("x")
    return int(w), int(h)


def _region(s: str) -> tuple[float, float, float, float]:
    x, y, w, h = (float(v) for v in s.split(","))
    return x, y, w, h


def cmd_studio(args) -> int:
    import uvicorn

    from cyber_worker.bootstrap import build
    from cyber_worker.studio.server import create_app

    app = build(args.config)
    api = create_app(app)  # 里面注册全局快捷键（录制开关、急停），被占用时自动换备选
    hk = app.hotkeys
    print(f"studio：http://127.0.0.1:{args.port}    录制开关：{hk.effective('record')}    "
          f"急停：{hk.effective('panic')}")
    for note in hk.notes.values():
        print(f"注意：{note}")
    uvicorn.run(api, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_record(args) -> int:
    from cyber_worker.bootstrap import build
    from cyber_worker.macro.recorder import RecordingBuilder
    from cyber_worker.macro.schema import Window
    from cyber_worker.macro.screenshot.storage import write_image

    app = build(args.config)
    w, h = args.size
    window = Window(app=args.app, title=args.title, width=w, height=h)
    geo = app.io.calibrate(window)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_image(out.with_suffix(".png"), app.io.capture(geo))

    print(f"开始录制 {args.app}（{w}x{h}，缩放 {geo.scale:g}）。在终端按 Ctrl+C 结束。")
    builder = RecordingBuilder(geo)
    stop = threading.Event()
    t = threading.Thread(target=app.listen, args=(builder, stop), daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.2)
    except KeyboardInterrupt:
        stop.set()
        t.join()

    script = builder.build(out.stem, window)
    script.reference = out.with_suffix(".png").name
    script.dump(out)
    typed = [s.id for s in script.steps if s.action == "type"]
    print(f"已保存 {len(script.steps)} 步到 {out}")
    if typed:
        print(f"注意：输入步骤 {', '.join(typed)} 需要补上 expected_page，否则执行时会被拒绝。")
    return 0


def cmd_anchor(args) -> int:
    from cyber_worker.bootstrap import build
    from cyber_worker.macro.anchors import add_anchor
    from cyber_worker.macro.schema import Script

    script = Script.load(args.script)
    app = build(args.config)
    geo = app.io.calibrate(script.window, avoid=script.other_titles(None))
    anchor = add_anchor(Path(args.script), args.page, args.region, app.io.capture(geo), geo.scale)
    print(f"已为页面 {args.page} 添加锚点 {anchor.image}")
    return 0


def cmd_run(args) -> int:
    from cyber_worker.bootstrap import build
    from cyber_worker.macro.runner import Cancelled, RunStatus
    from cyber_worker.macro.schema import Script
    from cyber_worker.macro.vision.fingerprint import PageBook

    script = Script.load(args.script)
    pages = PageBook.load(script.pages_path) if script.pages_path else None
    variables = dict(v.split("=", 1) for v in args.var)

    app = build(args.config)
    dry_run = args.dry_run or app.config.executor.dry_run
    app.prune_audit(date.today())
    app.hotkeys.apply({"panic": app.config.executor.panic_hotkey},
                      {"panic": lambda: print(f"\n急停：取消了 {app.executor.panic()} 个排队任务")})
    hotkey = app.hotkeys.effective("panic")
    for note in app.hotkeys.notes.values():
        print(f"注意：{note}")

    print(f"执行 {script.name}{'（空跑）' if dry_run else ''}，急停：{hotkey}")
    fut = app.executor.submit(
        lambda gate: app.runner.run(script, pages=pages, variables=variables,
                                    dry_run=dry_run, gate=gate)
    )
    try:
        result = fut.result()
    except Cancelled:
        print("已急停")
        return 130
    print(f"[{result.status.value}] 完成 {result.steps_done}/{len(script.steps)} 步，run_id={result.run_id}"
          + (f"：{result.reason}" if result.reason else ""))
    return 0 if result.status in (RunStatus.OK, RunStatus.DRY_RUN_STOPPED) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cyber-worker")
    parser.add_argument("--config", default="config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("studio", help="可视化编辑器")
    p.add_argument("--port", type=int, default=9999)
    p.set_defaults(func=cmd_studio)

    p = sub.add_parser("record", help="录制宏")
    p.add_argument("out")
    p.add_argument("--app", required=True, help="进程名，如 WeChat")
    p.add_argument("--title", help="可选：窗口标题包含这段文字（同一程序有多个窗口时用）")
    p.add_argument("--size", type=_size, required=True, help="窗口尺寸，如 1200x900")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("anchor", help="截取当前窗口的一块区域，作为页面指纹锚点")
    p.add_argument("script")
    p.add_argument("page")
    p.add_argument("--region", type=_region, required=True, help="窗口内区域 x,y,w,h（点）")
    p.set_defaults(func=cmd_anchor)

    p = sub.add_parser("run", help="执行宏")
    p.add_argument("script")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--var", action="append", default=[], help="变量 name=value，可重复")
    p.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, OSError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
