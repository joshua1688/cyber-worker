"""studio 后端：全部走 fake IO，不碰真实屏幕。"""

import random
import shutil
import threading

import pytest
from fastapi.testclient import TestClient

from cyber_worker.bootstrap import App
from cyber_worker.config import Config, PathsConfig
from cyber_worker.infra.sqlite.audit_store import SqliteAuditStore
from cyber_worker.infra.sqlite.db import connect
from cyber_worker.macro.executor import Executor
from cyber_worker.macro.io.hotkeys import HotkeyManager
from cyber_worker.macro.runner import Runner
from cyber_worker.macro.screenshot.storage import ScreenshotStorage
from cyber_worker.studio.server import create_app
from fakes import FakeActivity, FakeHotkeys, FakeIO
from helpers import pattern, screen_with


def fake_listen(builder, stop: threading.Event):
    """假录制：点一下输入框、打几个字、点「发送」，然后等停止。"""
    builder.on_click(120, 80, "left")
    for ch in "hi":
        builder.on_char(ch)
    builder.on_click(400, 300, "left")
    stop.wait()


def fake_clicks():
    """假的「下一次点击」：先点到 studio 自己（应被忽略），再点到目标窗口。"""
    clicks = iter([(-5, -5), (150, 100)])
    return lambda timeout: next(clicks, None)


WS = "ws://127.0.0.1:9999/ws"  # studio 只接受本机地址发来的请求（见 studio/guard.py）


@pytest.fixture
def client(tmp_path):
    # 画面上 (50, 40) 处有一块纹理，可以作为锚点
    io = FakeIO(lambda: screen_with(pattern()))
    data = tmp_path / "user_data"
    storage = ScreenshotStorage(data / "screenshots")
    audit = SqliteAuditStore(connect(data / "state.db"))
    executor = Executor(FakeActivity(), poll_interval=0.01)
    app = App(
        config=Config(paths=PathsConfig(user_data=data)), io=io, activity=executor.activity,
        storage=storage, audit=audit,
        runner=Runner(io, storage, audit, rng=random.Random(0), sleep=lambda s: None),
        executor=executor, listen=fake_listen, wait_click=fake_clicks(),
        hotkeys=HotkeyManager(FakeHotkeys(taken={"ctrl+alt+f9"}), platform="win32"),
        config_path=tmp_path / "config.toml",  # 改快捷键写到这里，不碰仓库里的 config.toml
    )
    with TestClient(create_app(app, run_idle_sec=0), base_url="http://127.0.0.1:9999") as c:
        c.io = io
        c.activity = executor.activity
        c.hotkeys = app.hotkeys.backend
        yield c
    executor.shutdown()


def new_script(client, name="comment"):
    r = client.post("/api/scripts", json={"platform": "demo", "name": name, "app": "Demo",
                                          "width": 400, "height": 300})
    assert r.status_code == 200, r.text
    return r.json()


def test_new_macro_is_a_draft_until_saved(client, tmp_path):
    d = new_script(client)
    assert d["id"] == "_draft/demo/templates/comment.yaml" and d["draft"] is True
    assert d["frames"]["reference"]["main"]["url"].startswith("/api/file?path=drafts/demo/templates/comment.png")
    assert client.get(d["frames"]["reference"]["main"]["url"]).status_code == 200
    assert client.get("/api/scripts").json() == [
        {"id": d["id"], "platform": "demo", "name": "comment", "draft": True}]
    assert not (tmp_path / "user_data/platforms").exists()  # 宏库里还没有
    # 重名不报错，自动加序号
    again = new_script(client)
    assert again["id"] == "_draft/demo/templates/comment-2.yaml"
    assert again["script"]["name"] == "comment-2"  # 显示名也带序号，列表里分得清


def test_save_moves_draft_with_reference_and_anchors(client, tmp_path):
    d = new_script(client)
    sid = d["id"]
    client.post(f"/api/scripts/{sid}/capture")
    client.post(f"/api/scripts/{sid}/anchors", json={"page": "页面1", "x": 50, "y": 40, "w": 40, "h": 30})
    saved = client.post(f"/api/scripts/{sid}/commit").json()
    assert saved["id"] == "demo/templates/comment.yaml" and saved["draft"] is False
    lib = tmp_path / "user_data/platforms/demo/templates"
    assert sorted(p.name for p in lib.iterdir()) == [
        "comment.anchors", "comment.pages.yaml", "comment.png", "comment.yaml"]
    assert not list((tmp_path / "user_data/drafts").rglob("*.*"))  # 草稿清空了
    assert client.get(saved["frames"]["reference"]["main"]["url"]).status_code == 200
    assert client.get(saved["pages"]["页面1"][0]["url"]).status_code == 200  # 锚点图跟着搬过来了
    assert saved["frames"]["latest"]["main"]["url"]  # 最新截图也还在
    assert client.post(f"/api/scripts/{saved['id']}/commit").status_code == 400  # 已经保存过了


def test_save_renames_when_library_already_has_same_name(client):
    first = client.post(f"/api/scripts/{new_script(client)['id']}/commit").json()
    second = client.post(f"/api/scripts/{new_script(client)['id']}/commit").json()
    assert first["id"] == "demo/templates/comment.yaml"
    assert second["id"] == "demo/templates/comment-2.yaml"
    assert [first["script"]["name"], second["script"]["name"]] == ["comment", "comment-2"]


def test_delete_removes_macro_and_its_own_files(client, tmp_path):
    d = new_script(client)
    sid = d["id"]
    client.post(f"/api/scripts/{sid}/capture")
    client.post(f"/api/scripts/{sid}/anchors", json={"page": "页面1", "x": 50, "y": 40, "w": 40, "h": 30})
    assert client.delete(f"/api/scripts/{sid}/detail").status_code == 200
    assert not list((tmp_path / "user_data/drafts").rglob("*.*"))
    assert client.get("/api/scripts").json() == []


def test_chinese_name_and_auto_platform(client):
    r = client.post("/api/scripts", json={"name": "朋友圈 评论", "app": "WeChat.exe",
                                          "width": 400, "height": 300})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["id"] == "_draft/wechat/templates/朋友圈_评论.yaml"
    assert d["script"]["name"] == "朋友圈 评论"
    assert client.get(d["frames"]["reference"]["main"]["url"]).status_code == 200
    saved = client.post(f"/api/scripts/{d['id']}/commit").json()
    assert saved["id"] == "wechat/templates/朋友圈_评论.yaml"
    assert client.get("/api/scripts").json() == [
        {"id": saved["id"], "platform": "wechat", "name": "朋友圈 评论", "draft": False}]
    again = client.post("/api/scripts", json={"name": "朋友圈 评论", "app": "WeChat.exe",
                                              "width": 400, "height": 300}).json()
    assert again["script"]["name"] == "朋友圈 评论-2"


def test_windows_list_hides_studio_and_flags_shared_apps(client):
    ws = client.get("/api/windows").json()
    assert [w["app"] for w in ws] == ["Demo", "QQ", "QQ"]
    assert [w["shared"] for w in ws] == [False, True, True]
    assert ws[0]["platform"] == "demo"


def test_rejects_path_traversal(client):
    new_script(client)
    assert client.get("/api/scripts/../../state.db/detail").status_code in (400, 404)
    assert client.get("/api/file", params={"path": "../secret.png"}).status_code == 400
    assert client.post("/api/scripts", json={"platform": "../x", "name": "a", "app": "A",
                                             "width": 1, "height": 1}).status_code == 400


def test_save_validates_and_reports_problems(client):
    d = new_script(client)
    url = f"/api/scripts/{d['id']}/detail"
    script = d["script"]
    script["steps"] = [{"id": "s01", "action": "type", "text": "{draft}"}]
    r = client.put(url, json=script)
    assert r.status_code == 200
    msgs = [p["message"] for p in r.json()["problems"]]
    assert any("在哪个画面执行" in m for m in msgs)
    assert any("{draft}" in m for m in msgs)

    script["steps"] = [{"id": "s01", "action": "teleport"}]
    assert client.put(url, json=script).status_code == 422


def test_capture_then_anchor(client):
    d = new_script(client)
    sid = d["id"]
    assert client.post(f"/api/scripts/{sid}/anchors", json={
        "page": "compose", "x": 50, "y": 40, "w": 40, "h": 30}).status_code == 400  # 还没截图

    d = client.post(f"/api/scripts/{sid}/capture").json()
    assert d["frames"]["latest"]["main"]["url"]
    d = client.post(f"/api/scripts/{sid}/anchors", json={
        "page": "compose", "x": 50, "y": 40, "w": 40, "h": 30}).json()
    assert d["script"]["pages"] == "comment.pages.yaml"  # 每个宏自己一份
    assert len(d["pages"]["compose"]) == 1 and d["pages"]["compose"][0]["url"]

    # 宏自己的页面文件不能出现在宏列表里
    assert [x["id"] for x in client.get("/api/scripts").json()] == [sid]

    d = client.delete(f"/api/scripts/{sid}/anchors/compose/0").json()
    assert d["pages"] == {}


def test_record_appends_steps(client):
    d = new_script(client)
    with client.websocket_connect(WS) as ws:
        assert client.post("/api/record/start", json={"script_id": d["id"]}).status_code == 200
        assert ws.receive_json()["type"] in ("record", "record_state")
        r = client.post("/api/record/stop").json()
    assert r["added"] >= 3
    actions = [s["action"] for s in r["script"]["steps"]]
    assert actions[0] == "click" and "type" in actions


def test_record_automates_send_and_page(client):
    """录完自动：标记发送步骤、选锚点、把页面填给输入和发送步骤；用户什么都不用填。"""
    d = new_script(client)
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": False})
    r = client.post("/api/record/stop").json()
    steps = r["script"]["steps"]
    send = next(s for s in steps if s.get("sends"))
    assert send["id"] == r["auto_send"] and send["action"] == "click"
    assert r["auto_anchor"]["page"] == "页面1"
    assert "页面1" in r["pages"]
    typed = next(s for s in steps if s["action"] == "type")
    assert typed["expected_page"] == send["expected_page"] == "页面1"
    assert r["problems"] == []
    assert r["frames"]["latest"]["main"]["url"]  # 录制最后一帧可以直接在画布上看


def test_pick_window_skips_studio_and_cleans_title(client):
    r = client.post("/api/windows/pick")
    assert r.status_code == 200, r.text
    w = r.json()
    assert w["app"] == "QQ" and w["shared"] is True
    assert w["suggested_name"] == "海绵宝宝大战"  # 「…等2个会话」会变，去掉
    assert w["match_title"] == "海绵宝宝大战"
    # 再点一次就没有点击了 → 超时
    assert client.post("/api/windows/pick").status_code == 408


def test_dry_run_streams_progress_then_audit(client):
    d = new_script(client)
    sid = d["id"]
    client.post(f"/api/scripts/{sid}/capture")
    client.post(f"/api/scripts/{sid}/anchors", json={"page": "compose", "x": 50, "y": 40, "w": 40, "h": 30})
    script = client.get(f"/api/scripts/{sid}/detail").json()["script"]
    script["variables"] = ["draft"]
    script["steps"] = [
        {"id": "s01", "action": "click", "x": 20, "y": 30},
        {"id": "s02", "action": "type", "text": "{draft}", "expected_page": "compose"},
        {"id": "s03", "action": "key", "combo": "enter", "sends": True, "expected_page": "compose"},
    ]
    assert client.put(f"/api/scripts/{sid}/detail", json=script).json()["problems"] == []

    with client.websocket_connect(WS) as ws:
        client.post(f"/api/scripts/{sid}/run", json={"dry_run": True, "variables": {"draft": "好看"}})
        events = []
        while not (events and events[-1].get("state") == "done"):
            events.append(ws.receive_json())
    entries = [e["entry"] for e in events if e["type"] == "run_entry"]
    assert [e["status"] for e in entries] == ["ok", "ok", "skipped"]
    assert entries[-1]["screenshot_url"]
    assert events[-1]["status"] == "dry_run_stopped"
    assert client.io.typed() == "好看" and ("combo", "enter") not in client.io.calls

    audit = client.get("/api/audit").json()
    assert audit["report"]["runs"] == 1 and audit["report"]["success_rate"] == 1.0
    first = audit["entries"][0]
    assert client.post(f"/api/audit/{first['id']}", json={"reviewed": 1}).status_code == 200
    assert client.get("/api/audit", params={"unreviewed": True}).json()["entries"][0]["id"] != first["id"]

    # 清空这一天：记录和截图一起删
    day = first["at"][:10]
    shots = client.app.state.studio.app.storage.root / day
    assert shots.is_dir()
    assert client.delete(f"/api/audit/days/{day}").json() == {"deleted": 3}
    assert client.get("/api/audit").json()["entries"] == [] and not shots.exists()


def test_pages_served(client):
    assert "<canvas" in client.get("/").text
    assert client.get("/audit").status_code == 200
    fav = client.get("/favicon.ico")  # 浏览器默认会请求它，不能 404
    assert fav.status_code == 200 and "🤖" in fav.text


def test_run_waits_while_user_is_moving_and_can_be_cancelled(client):
    """你一直在动鼠标：任务停在「排队」不开始；点取消后收到「完成：已取消」，按钮能恢复。"""
    d = new_script(client)
    client.app.state.studio.run_idle_sec = 2
    client.activity.idle = 0  # 用户正在动鼠标
    with client.websocket_connect(WS) as ws:
        client.post(f"/api/scripts/{d['id']}/run", json={"dry_run": True})
        queued = ws.receive_json()
        assert queued["state"] == "queued" and queued["idle_sec"] == 2
        assert client.get("/api/status").json()["busy"] is True
        wait = ws.receive_json()  # 等你停手：推剩余秒数
        assert wait["type"] == "run_wait" and wait["remaining"] == 2 and wait["total"] == 2
        client.post("/api/panic")
        done = ws.receive_json()
        while done["type"] == "run_wait":
            done = ws.receive_json()
    assert done["state"] == "done" and done["status"] == "cancelled"
    assert client.get("/api/status").json()["busy"] is False
    assert [c for c in client.io.calls if c[0] != "chime"] == []  # 一下都没动（只响了一声「开始」）


def test_run_reports_running_once_user_is_idle(client):
    d = new_script(client)
    with client.websocket_connect(WS) as ws:
        client.post(f"/api/scripts/{d['id']}/run", json={"dry_run": True})
        states = []
        while not states or states[-1] != "done":
            ev = ws.receive_json()
            if ev["type"] == "run_state":
                states.append(ev["state"])
    assert states == ["queued", "running", "done"]


def test_record_across_windows_saves_window_and_its_anchor(client):
    """QQ 那种场景：主窗口双击会话，弹出的聊天窗口里打字、点发送。"""
    from cyber_worker.macro.io.base import WindowGeometry, WindowInfo

    # 同一个程序弹出的聊天窗口（别的程序的窗口不录）
    chat = (WindowInfo("Demo", "My Sky等2个会话", 300, 200), WindowGeometry(150, 80, 300, 200, 1.0))

    def listen_two_windows(builder, stop):
        main = (WindowInfo("Demo", "Demo 窗口", 400, 300), builder.geo)
        builder.on_click(120, 80, "left", (), main)
        builder.on_click(120, 80, "left", (), main)
        for ch in "hi":
            builder.on_char(ch, chat)
        builder.on_click(400, 250, "left", (), chat)
        stop.wait()

    client.app.state.studio.app.listen = listen_two_windows
    d = new_script(client)
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": False})
    r = client.post("/api/record/stop").json()
    s = r["script"]
    assert s["windows"] == {"w2": {"app": "Demo", "title": "My Sky", "width": 300, "height": 200, "scale": 1.0,
                                   "reference": "comment.refs/w2.png", "popup": False}}
    assert [(x["action"], x.get("win")) for x in s["steps"]] == [("click", None), ("type", "w2"), ("click", "w2")]
    assert s["steps"][0]["count"] == 2
    typed, send = s["steps"][1], s["steps"][2]
    assert send["sends"] and typed["expected_page"] == send["expected_page"]
    assert r["pages"][typed["expected_page"]][0]["win"] == "w2"  # 锚点截自聊天窗口
    assert r["last_win"] == "w2"
    # 每个窗口各有参考图和最新截图：画布上选到聊天窗口的步骤就能看到聊天窗口
    assert set(r["frames"]["reference"]) == set(r["frames"]["latest"]) == {"main", "w2"}
    assert s["windows"]["w2"]["reference"] == "comment.refs/w2.png"
    assert client.get(r["frames"]["reference"]["w2"]["url"]).status_code == 200
    assert r["problems"] == []


def test_record_hotkey_toggles_current_macro(client):
    """全局快捷键：没有选中的宏时提示；选中后按一下开始、再按一下停止，结果通过事件推给界面。"""
    from cyber_worker.studio.routes.record import toggle_recording

    st = client.app.state.studio
    assert client.get("/api/status").json()["record_hotkey"] == "alt+r"
    with client.websocket_connect(WS) as ws:
        toggle_recording(st)  # 还没选宏
        ev = ws.receive_json()
        assert ev["type"] == "toast" and ev["error"]

        d = new_script(client)
        client.post("/api/current", json={"script_id": d["id"]})
        toggle_recording(st)  # 开始
        assert st.recording is not None and st.recording.script_id == d["id"]
        toggle_recording(st)  # 停止
        assert st.recording is None
        events = []
        while not events or events[-1]["type"] != "record_done":
            events.append(ws.receive_json())
    done = events[-1]
    assert done["id"] == d["id"] and done["added"] >= 3 and isinstance(done["rec_id"], str)



def test_record_hotkey_double_press_does_not_restart(client):
    """停止要好几秒、人在别的窗口里看不到反应，会再按一下：不能又开一次录制，把后面的操作都录进去。"""
    from cyber_worker.studio.routes.record import toggle_recording

    st = client.app.state.studio
    d = new_script(client)
    client.post("/api/current", json={"script_id": d["id"]})
    with client.websocket_connect(WS) as ws:
        toggle_recording(st)  # 开始
        st.record_lock.acquire()  # 模拟「正在停止」：这时候按下的也不算
        toggle_recording(st)
        st.record_lock.release()
        assert st.recording is not None
        toggle_recording(st)  # 停止
        toggle_recording(st)  # 紧跟着又按一下
        assert st.recording is None and not st.stopping
        events = []
        while not events or events[-1]["type"] != "record_done":
            events.append(ws.receive_json())
    # 一按停止就先通知界面「正在停止」，最后才是结果
    states = [(e["recording"], e.get("stopping", False)) for e in events if e["type"] == "record_state"]
    assert states == [(True, False), (True, True), (False, False)]
    assert [c for c in client.io.calls if c[0] == "chime"] == [("chime", "start"), ("chime", "stop")]
    assert client.get("/api/status").json()["stopping"] is False
    assert client.post("/api/record/stop").status_code == 409


def test_record_hotkey_works_again_after_cooldown(client):
    from cyber_worker.studio.routes import record

    st = client.app.state.studio
    d = new_script(client)
    client.post("/api/current", json={"script_id": d["id"]})
    record.toggle_recording(st)
    record.toggle_recording(st)
    st.stopped_at -= record.RESTART_COOLDOWN  # 过了冷却时间
    record.toggle_recording(st)
    assert st.recording is not None
    record.toggle_recording(st)

def test_hotkey_settings(client, tmp_path):
    """设置 → 快捷键：检查冲突、有冲突不让存、存了写进 config.toml 并立即按新键生效。"""
    r = client.get("/api/hotkeys").json()
    assert [(i["name"], i["combo"], i["default"]) for i in r["items"]] == [
        ("record", "alt+r", "alt+r"), ("dry_run", "alt+t", "alt+t"),
        ("panic", "ctrl+shift+alt+.", "ctrl+shift+alt+.")]

    def check(combo, name="record", other=None):
        return client.post("/api/hotkeys/check", json={"name": name, "combo": combo, "other": other}).json()

    assert check("ctrl+alt+f10")["status"] == "ok"
    assert check("ctrl+alt+f9")["status"] == "taken"
    assert check("ctrl+v")["status"] == "reserved"
    assert check("ctrl+shift+alt+.")["status"] == "duplicate"
    assert check("ctrl+shift+alt+.", other="ctrl+alt+f12")["status"] == "ok"  # 另一个也正在改
    assert check("alt+r")["status"] == "ok"  # 正在用的

    assert client.put("/api/hotkeys", json={"record": "ctrl+alt+f9"}).status_code == 400
    assert not (tmp_path / "config.toml").exists()

    r = client.put("/api/hotkeys", json={"record": "Ctrl+Alt+F10"}).json()
    assert r["items"][0]["combo"] == "ctrl+alt+f10"
    assert 'record_hotkey = "ctrl+alt+f10"' in (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert client.get("/api/status").json()["record_hotkey"] == "ctrl+alt+f10"
    assert sorted(client.hotkeys.bound) == ["alt+t", "ctrl+alt+f10", "ctrl+shift+alt+."]

    # 新键马上能用：按一下开始录制
    d = new_script(client)
    client.post("/api/current", json={"script_id": d["id"]})
    client.hotkeys.press("ctrl+alt+f10")
    st = client.app.state.studio
    for _ in range(100):
        if st.recording:
            break
        threading.Event().wait(0.02)
    assert st.recording is not None
    client.post("/api/record/stop")


def test_hotkey_capture_suspends_global_keys(client):
    client.post("/api/hotkeys/suspend")
    assert client.hotkeys.bound == {} and client.get("/api/hotkeys").json()["suspended"]
    client.post("/api/hotkeys/resume")
    assert sorted(client.hotkeys.bound) == ["alt+r", "alt+t", "ctrl+shift+alt+."]


def test_taken_hotkey_falls_back_and_tells_ui(tmp_path):
    """启动时配置的键被占用：自动换备选，状态里带上说明。"""
    io = FakeIO(lambda: screen_with(pattern()))
    data = tmp_path / "user_data"
    storage = ScreenshotStorage(data / "screenshots")
    audit = SqliteAuditStore(connect(data / "state.db"))
    executor = Executor(FakeActivity(), poll_interval=0.01)
    app = App(config=Config(paths=PathsConfig(user_data=data)), io=io, activity=executor.activity,
              storage=storage, audit=audit, runner=Runner(io, storage, audit), executor=executor,
              listen=fake_listen, wait_click=fake_clicks(),
              hotkeys=HotkeyManager(FakeHotkeys(taken={"alt+r"}), platform="win32"))
    with TestClient(create_app(app), base_url="http://127.0.0.1:9999") as c:
        s = c.get("/api/status").json()
        assert s["record_hotkey"] == "shift+alt+r"
        assert len(s["hotkey_notes"]) == 1 and "shift+alt+r" in s["hotkey_notes"][0]
    executor.shutdown()


def test_settings_apply_immediately_and_persist(client, tmp_path):
    """设置：默认深色；改了立即生效（执行器、点执行后的等待），写进 config.toml，并推给其他标签页。"""
    s = client.get("/api/settings").json()
    assert s["theme"] == "dark" and s["record_delays"] is True and "language" not in s
    assert s["config_path"].endswith("config.toml") and s["version"]

    with client.websocket_connect(WS) as ws:
        r = client.patch("/api/settings", json={"theme": "light", "record_delays": False, "run_idle_sec": 5,
                                                "pause_on_user_input": False, "resume_after_idle_sec": 30})
        assert r.status_code == 200
        ev = ws.receive_json()
        assert ev["type"] == "settings" and ev["theme"] == "light"
    st = client.app.state.studio
    assert st.run_idle_sec == 5 and st.record_delays is False
    assert st.app.executor.pause_on_user_input is False and st.app.executor.resume_after_idle_sec == 30

    from cyber_worker.config import Config
    saved = Config.load(tmp_path / "config.toml")
    assert saved.studio.theme == "light" and saved.studio.run_idle_sec == 5
    assert saved.studio.record_delays is False and saved.executor.resume_after_idle_sec == 30

    assert client.patch("/api/settings", json={"theme": "pink"}).status_code == 422
    assert client.patch("/api/settings", json={"run_idle_sec": -1}).status_code == 422


def test_run_countdown_restarts_when_user_moves(client):
    """倒计时：剩余秒数随你停手的时间减少；你一动就回到满值；停够了就开始。"""
    d = new_script(client)
    client.app.state.studio.run_idle_sec = 2
    client.activity.idle = 0
    with client.websocket_connect(WS) as ws:
        client.post(f"/api/scripts/{d['id']}/run", json={"dry_run": True})

        def next_wait():
            while True:
                ev = ws.receive_json()
                if ev["type"] == "run_wait":
                    return ev["remaining"]

        assert next_wait() == 2
        client.activity.idle = 1.5  # 停手 1.5 秒
        while next_wait() != 0.5:
            pass
        client.activity.idle = 0.2  # 又动了一下：重新计时
        while next_wait() != 1.8:
            pass
        client.activity.idle = 10  # 停够了
        while True:
            ev = ws.receive_json()
            if ev["type"] == "run_state" and ev["state"] == "running":
                break
        while ev.get("state") != "done":
            ev = ws.receive_json()


def test_recording_click_gets_target_image(client, tmp_path):
    """录制时点在认得出来的地方：自动截目标图（按图找）；可以在画面上重新框；保存、删除时目标图跟着走。"""
    def listen(builder, stop):
        builder.on_click(100 + 70, 50 + 55, "left")  # 点在画面上那块纹理里（窗口原点 100, 50）
        builder.on_click(100 + 300, 50 + 250, "left")  # 空白处：认不出来，保持固定坐标
        stop.wait()

    st = client.app.state.studio
    st.app.listen = listen
    d = new_script(client)
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": False})
    r = client.post("/api/record/stop").json()
    assert r["auto_targets"] == 1
    s1, s2 = r["script"]["steps"]
    assert s1["target"]["image"] == "comment.targets/s01.png" and s2["target"] is None
    t = s1["target"]
    assert t["x"] <= 70 <= t["x"] + t["w"] and t["y"] <= 55 <= t["y"] + t["h"]
    assert client.get(r["targets"]["s01"]).status_code == 200

    # 在画面上重新框一块（用录制最后一帧）；保留调过的等待时间
    sc = r["script"]
    sc["steps"][0]["target"]["timeout"] = 9
    client.put(f"/api/scripts/{d['id']}/detail", json=sc)
    r = client.post(f"/api/scripts/{d['id']}/targets",
                    json={"step_id": "s01", "x": 50, "y": 40, "w": 40, "h": 30, "frame": "latest"}).json()
    t = r["script"]["steps"][0]["target"]
    assert (t["x"], t["y"], t["w"], t["h"], t["timeout"]) == (50, 40, 40, 30, 9)
    bad = client.post(f"/api/scripts/{d['id']}/targets", json={"step_id": "s02", "x": 0, "y": 0, "w": 2, "h": 2})
    assert bad.status_code == 400

    # 保存进宏库：目标图目录一起搬，引用跟着改
    saved = client.post(f"/api/scripts/{d['id']}/commit").json()
    t = saved["script"]["steps"][0]["target"]
    assert t["image"] == "comment.targets/s01.png"
    lib = tmp_path / "user_data/platforms/demo/templates"
    assert (lib / "comment.targets/s01.png").exists()
    assert not (tmp_path / "user_data/drafts/demo/templates/comment.targets").exists()
    client.delete(f"/api/scripts/{saved['id']}/detail")
    assert not (lib / "comment.targets").exists()


def saved_with_anchor(client, name="comment"):
    sid = new_script(client, name)["id"]
    client.post(f"/api/scripts/{sid}/capture")
    client.post(f"/api/scripts/{sid}/anchors", json={"page": "页面1", "x": 50, "y": 40, "w": 40, "h": 30})
    return client.post(f"/api/scripts/{sid}/commit").json()


def test_pack_export_then_import_renames_same_name_macros(client, tmp_path):
    saved = saved_with_anchor(client)
    new_script(client, "草稿")  # 草稿不进包
    r = client.get("/api/packs/demo")
    assert r.status_code == 200
    assert "demo.cwpack" in r.headers["content-disposition"]

    # 本机已经有同名宏：导入的自动加 -2，原来的不动
    imported = client.post("/api/packs", content=r.content).json()
    assert imported == {"platform": "demo", "ids": ["demo/templates/comment-2.yaml"]}
    lib = tmp_path / "user_data/platforms/demo/templates"
    assert sorted(p.name for p in lib.iterdir()) == [
        "comment-2.anchors", "comment-2.pages.yaml", "comment-2.png", "comment-2.yaml",  # 参考截图跟着导出、改名
        "comment.anchors", "comment.pages.yaml", "comment.png", "comment.yaml"]
    d = client.get(f"/api/scripts/{imported['ids'][0]}/detail").json()
    assert client.get(d["pages"]["页面1"][0]["url"]).status_code == 200  # 锚点图跟着改了名
    assert client.get(d["frames"]["reference"]["main"]["url"]).status_code == 200
    assert d["script"]["name"] == "comment-2"
    assert not list((tmp_path / "user_data").glob(".import-*"))  # 临时目录清掉了
    assert saved["id"] in [s["id"] for s in client.get("/api/scripts").json()]


def test_pack_import_into_fresh_platform(client, tmp_path):
    saved_with_anchor(client)
    data = client.get("/api/packs/demo").content
    shutil.rmtree(tmp_path / "user_data/platforms/demo")
    assert client.post("/api/packs", content=data).json()["ids"] == ["demo/templates/comment.yaml"]


def test_pack_errors(client, tmp_path):
    assert client.get("/api/packs/demo").status_code == 404  # 还没有保存的宏
    assert client.get("/api/packs/..").status_code in (400, 404)
    r = client.post("/api/packs", content=b"garbage")
    assert r.status_code == 400 and "cwpack" in r.json()["detail"]
    assert client.post("/api/packs", content=b"").status_code == 400
    assert not list((tmp_path / "user_data").glob(".import-*"))


def test_rerecording_replaces_old_steps_by_default(client, tmp_path):
    """重新录一遍就是要这一遍：旧步骤不能留在前面，看着像录重复了。"""
    def listen(builder, stop):
        builder.on_click(100 + 70, 50 + 55, "left")  # 点在纹理上：有目标图
        stop.wait()

    st = client.app.state.studio
    st.app.listen = listen
    sid = new_script(client)["id"]
    first = None
    for _ in range(2):
        client.post("/api/record/start", json={"script_id": sid, "record_delays": False})
        r = client.post("/api/record/stop").json()
        first = first or r
    assert [s["id"] for s in r["script"]["steps"]] == ["s01"] and r["replaced"] == 1
    assert r["rec_id"] != first["rec_id"]  # 每次都不一样：studio 重启后也不会和页面记着的撞上
    # 旧步骤的目标图留着（界面上撤销回去还能用），新的换了个名字
    assert first["script"]["steps"][0]["target"]["image"] == "comment.targets/s01.png"
    assert r["script"]["steps"][0]["target"]["image"] == "comment.targets/s01-2.png"
    tpl = tmp_path / "user_data/drafts/demo/templates"
    assert (tpl / "comment.targets/s01.png").exists() and (tpl / "comment.targets/s01-2.png").exists()


def test_record_append_setting_keeps_old_steps(client, tmp_path):
    sid = new_script(client)["id"]
    assert client.patch("/api/settings", json={"record_append": True}).json()["record_append"] is True
    assert "record_append = true" in (tmp_path / "config.toml").read_text(encoding="utf-8")
    for _ in range(2):
        client.post("/api/record/start", json={"script_id": sid})
        r = client.post("/api/record/stop").json()
    assert r["replaced"] == 0 and len(r["script"]["steps"]) == 2 * r["added"]
    ids = [s["id"] for s in r["script"]["steps"]]
    assert len(set(ids)) == len(ids)  # 编号接着往后排
    # 这一次单独要替换：请求里明确说不追加
    client.post("/api/record/start", json={"script_id": sid, "record_append": False})
    r = client.post("/api/record/stop").json()
    assert len(r["script"]["steps"]) == r["added"]


def test_dry_run_hotkey_runs_current_macro(client):
    """在目标窗口里按 Alt+T：空跑界面上当前打开的宏（用界面上填的变量），不用切回 studio。"""
    from cyber_worker.studio.routes.run import hotkey_dry_run

    st = client.app.state.studio
    assert client.get("/api/status").json()["dry_run_hotkey"] == "alt+t"
    with client.websocket_connect(WS) as ws:
        hotkey_dry_run(st)  # 还没选宏
        assert "先在 studio 里选一个宏" in ws.receive_json()["text"]

        d = new_script(client)
        sc = d["script"]
        sc["steps"] = [{"id": "s01", "action": "type", "text": "{评论}"}]
        sc["variables"] = ["评论"]
        client.put(f"/api/scripts/{d['id']}/detail", json=sc)
        client.post("/api/current", json={"script_id": d["id"]})
        hotkey_dry_run(st)  # 变量没填
        assert "先在 studio 里填变量：评论" in ws.receive_json()["text"]

        client.post("/api/current", json={"script_id": d["id"], "variables": {"评论": "好看"}})
        hotkey_dry_run(st)
        hotkey_dry_run(st)  # 连按：正在跑，不再排一次
        events = []
        while not (events and events[-1].get("state") == "done"):
            events.append(ws.receive_json())
    states = [e["state"] for e in events if e["type"] == "run_state"]
    assert states == ["queued", "running", "done"]
    assert all(e["dry_run"] for e in events if e["type"] == "run_state" and "dry_run" in e)
    assert any(e["type"] == "toast" and "正在执行" in e["text"] for e in events)
    assert client.io.calls.count(("chime", "start")) == 1  # 连按的第二下没跑，也不响


def test_every_run_chimes_once(client):
    """点「空跑」「执行」按钮（Ctrl+Enter 也是点按钮）和按快捷键一样：开跑时响一声。"""
    d = new_script(client)
    for dry in (True, False):
        with client.websocket_connect(WS) as ws:
            client.post(f"/api/scripts/{d['id']}/run", json={"dry_run": dry})
            while ws.receive_json().get("state") != "done":
                pass
    assert client.io.calls.count(("chime", "start")) == 2


def test_dry_run_hotkey_hands_unsaved_edits_to_the_page(client):
    """页面上有没保存的修改（比如刚把输入法按键改成「你好」）：快捷键交给那个页面去跑，
    它会先写回再跑；不能直接跑磁盘上的旧版本。"""
    from cyber_worker.studio.routes.run import hotkey_dry_run

    st = client.app.state.studio
    d = new_script(client)
    with client.websocket_connect(WS) as ws:
        client.post("/api/current", json={"script_id": d["id"], "dirty": True, "page": "tab-1"})
        hotkey_dry_run(st)
        ev = ws.receive_json()
        assert ev == {"type": "dry_run_request", "script_id": d["id"], "page": "tab-1"}
        assert not st.app.executor.busy  # 服务端自己没跑
        assert ("chime", "start") not in client.io.calls  # 那个页面开跑时才响，不然要响两次

        # 保存后（或页面关掉时撤回）又直接跑文件
        client.post("/api/current", json={"script_id": d["id"], "dirty": False, "page": "tab-1"})
        hotkey_dry_run(st)
        events = []
        while not (events and events[-1].get("state") == "done"):
            events.append(ws.receive_json())
    assert not any(e["type"] == "dry_run_request" for e in events)


def test_page_from_another_window_is_a_problem(client):
    """主窗口的锚点拿去核对聊天窗口，永远对不上：执行前就要指出来。"""
    d = new_script(client)
    sid = d["id"]
    client.post(f"/api/scripts/{sid}/capture")
    d = client.post(f"/api/scripts/{sid}/anchors", json={
        "page": "列表", "x": 50, "y": 40, "w": 40, "h": 30}).json()
    sc = d["script"]
    sc["windows"] = {"w2": sc["window"]}
    sc["steps"] = [
        {"id": "s01", "action": "type", "text": "你好", "expected_page": "列表"},
        {"id": "s02", "win": "w2", "action": "type", "text": "你好", "expected_page": "列表"},
    ]
    r = client.put(f"/api/scripts/{sid}/detail", json=sc).json()
    msgs = {p["step"]: p["message"] for p in r["problems"]}
    assert "s01" not in msgs
    assert "截自别的窗口" in msgs["s02"]


def _record_two_windows(client):
    """主窗口点一下，弹出的聊天窗口里打字、点发送。返回录完的结果。"""
    from cyber_worker.macro.io.base import WindowGeometry, WindowInfo

    chat = (WindowInfo("Demo", "My Sky", 300, 200), WindowGeometry(150, 80, 300, 200, 1.0))

    def listen(builder, stop):
        builder.on_click(120, 80, "left", (), (WindowInfo("Demo", "Demo 窗口", 400, 300), builder.geo))
        for ch in "hi":
            builder.on_char(ch, chat)
        builder.on_click(400, 250, "left", (), chat)
        stop.wait()

    client.app.state.studio.app.listen = listen
    d = new_script(client)
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": False})
    return client.post("/api/record/stop").json()


def test_each_window_has_its_own_reference(client, tmp_path):
    """主窗口点开的聊天窗口也有参考图：画布切到它就能看、能在上面框锚点；跟着宏一起保存、删除，不进平台包。"""
    r = _record_two_windows(client)
    sid = r["id"]
    # 在聊天窗口的参考图上框锚点：锚点属于聊天窗口
    d = client.post(f"/api/scripts/{sid}/anchors", json={
        "page": "聊天", "x": 10, "y": 10, "w": 40, "h": 30, "frame": "reference", "win": "w2"}).json()
    assert d["pages"]["聊天"][0]["win"] == "w2"

    # 保存进宏库：参考图目录跟着搬，引用也改了
    saved = client.post(f"/api/scripts/{sid}/commit").json()
    lib = tmp_path / "user_data/platforms/demo/templates"
    assert saved["script"]["windows"]["w2"]["reference"] == "comment.refs/w2.png"
    assert (lib / "comment.refs/w2.png").exists()
    assert not (tmp_path / "user_data/drafts/demo/templates/comment.refs").exists()
    assert client.get(saved["frames"]["reference"]["w2"]["url"]).status_code == 200

    # 平台包默认带上每个窗口的参考图和每一步的画面；设置里关掉就都不带
    import io
    import zipfile
    r = client.get("/api/packs/demo")
    assert r.headers["X-Pack-Screenshots"] == "1"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "templates/comment.refs/w2.png" in names and any(".shots/" in n for n in names)
    client.patch("/api/settings", json={"pack_screenshots": False})
    r = client.get("/api/packs/demo")
    assert r.headers["X-Pack-Screenshots"] == "0"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert not any(".refs/" in n or ".shots/" in n or n == "templates/comment.png" for n in names)

    client.delete(f"/api/scripts/{saved['id']}/detail")
    assert not (lib / "comment.refs").exists() and not (lib / "comment.shots").exists()


def test_capture_fills_missing_window_reference(client):
    """以前录的宏，聊天窗口没有参考图：在它上面截一张就顺便存成参考图；最新截图每个窗口各一张。"""
    r = _record_two_windows(client)
    sid = r["id"]
    sc = r["script"]
    sc["windows"]["w2"].pop("reference")
    for step in sc["steps"]:  # 更早的宏：也没有步骤画面
        step.pop("shot", None)
    client.put(f"/api/scripts/{sid}/detail", json=sc)
    assert "w2" not in client.get(f"/api/scripts/{sid}/detail").json()["frames"]["reference"]

    d = client.post(f"/api/scripts/{sid}/capture?win=w2").json()
    assert d["script"]["windows"]["w2"]["reference"] == "comment.refs/w2.png"
    assert set(d["frames"]["reference"]) == {"main", "w2"}
    d = client.post(f"/api/scripts/{sid}/capture").json()  # 截主窗口不会把聊天窗口的最新截图冲掉
    assert set(d["frames"]["latest"]) == {"main", "w2"}
    assert client.post(f"/api/scripts/{sid}/anchors", json={
        "page": "x", "x": 1, "y": 1, "w": 10, "h": 10, "frame": "reference", "win": "w9"}).status_code == 400

    # 没有步骤画面时在「录制画面」上截：换的是窗口参考图，存成新文件
    d = client.post(f"/api/scripts/{sid}/capture?replace=true").json()
    assert d["replaced_steps"] == [] and d["script"]["reference"] == "comment.refs/main-2.png"
    d = client.post(f"/api/scripts/{sid}/capture?win=w2&replace=true").json()
    assert d["script"]["windows"]["w2"]["reference"] == "comment.refs/w2-2.png"


def test_each_step_keeps_the_frame_it_was_recorded_on(client):
    """每一步存它动作之前的画面：画面变了才另存一张，没变（或只多了几个字）就和上一步共用。"""
    import time as _time

    io = client.io
    page_a = screen_with(pattern(seed=1))
    page_b = screen_with(pattern(80, 60, seed=2), at=(200, 150))  # 点开了别的页面：大块变化

    def listen(builder, stop):
        def settle():
            _time.sleep(0.15)  # 截图在后台线程里，等它截完再换画面

        io.screen = lambda: page_a.copy()
        builder.on_click(120, 80, "left")       # s01：画面 A
        settle()
        builder.on_click(300, 200, "left")      # s02：还是 A → 共用
        settle()
        io.screen = lambda: page_b.copy()
        for ch in "hi":                          # s03：画面 B → 新存一张
            builder.on_char(ch)
        settle()
        typed = page_b.copy()
        typed[5:9, 5:12] = 0                     # 输入框里多了几个字：不算变了
        io.screen = lambda: typed.copy()
        builder.on_combo("enter")                # s04：共用 B
        settle()
        stop.wait()

    client.app.state.studio.app.listen = listen
    d = new_script(client)
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": False})
    r = client.post("/api/record/stop").json()
    shots = {s["id"]: s.get("shot") for s in r["script"]["steps"]}
    assert shots == {"s01": "comment.shots/s01.png", "s02": "comment.shots/s01.png",
                     "s03": "comment.shots/s03.png", "s04": "comment.shots/s03.png"}
    assert set(r["shots"]) == {"s01", "s02", "s03", "s04"}
    assert client.get(r["shots"]["s03"]["url"]).status_code == 200

    # 在某一步的画面上直接框锚点、重框目标图
    a = client.post(f"/api/scripts/{d['id']}/anchors", json={
        "page": "页面B", "x": 200, "y": 150, "w": 60, "h": 40, "frame": "step", "step_id": "s03"}).json()
    assert a["pages"]["页面B"][0]["win"] is None
    t = client.post(f"/api/scripts/{d['id']}/targets", json={
        "step_id": "s02", "x": 50, "y": 40, "w": 40, "h": 30, "frame": "step", "shot_of": "s02"})
    assert t.status_code == 200, t.text

    # 保存进宏库：画面目录跟着搬
    saved = client.post(f"/api/scripts/{d['id']}/commit").json()
    assert client.get(saved["shots"]["s01"]["url"]).status_code == 200

    # 录制画面录错了：停在这一步的录制画面上截，换掉它和共用它的那几步；旧图留着给撤销
    sid = saved["id"]
    lib = client.app.state.studio.script_path(sid).parent
    r = client.post(f"/api/scripts/{sid}/capture?replace=true&step_id=s02").json()
    assert r["replaced_steps"] == ["s01", "s02"]
    shots = {s["id"]: s.get("shot") for s in r["script"]["steps"]}
    assert shots["s01"] == shots["s02"] == "comment.shots/s01-2.png"
    assert shots["s03"] == "comment.shots/s03.png"
    assert (lib / "comment.shots/s01.png").exists()
    assert client.post(f"/api/scripts/{sid}/capture?replace=true&step_id=s09").status_code == 400


def test_hover_recorded_only_when_something_pops_up(client):
    """停一下什么都没变：不记。停在头像上弹出了资料卡：记一步悬停（带目标图和画面），资料卡标成弹出窗口。"""
    import time as _time

    from cyber_worker.macro.io.base import WindowGeometry, WindowInfo

    io = client.io
    base = io.list_windows()
    card = (WindowInfo("Demo", "资料卡", 200, 150), WindowGeometry(300, 100, 200, 150, 1.0))

    def listen(builder, stop):
        io.screen = lambda: screen_with(pattern())
        builder.on_move(250, 200)          # 停在空白处：画面没变、没弹窗口
        _time.sleep(1.3)
        builder.on_move(165, 105)          # 停在 (50, 40) 那块纹理（头像）上
        _time.sleep(0.4)                   # 已经截了「悬停前」
        io.list_windows = lambda: [*base, card[0]]  # 资料卡弹出来了
        _time.sleep(1.0)
        builder.on_click(400, 200, "left", (), card)  # 在资料卡上点「发消息」
        done.set()
        stop.wait()

    done = threading.Event()
    client.app.state.studio.app.listen = listen
    d = new_script(client)
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": False})
    assert done.wait(10)  # 操作完了再停（盯悬停的线程在停止录制时就退出了）
    r = client.post("/api/record/stop").json()
    io.list_windows = lambda: base
    steps = r["script"]["steps"]
    assert [(s["action"], s.get("win")) for s in steps] == [("hover", None), ("click", "w2")]
    hover = steps[0]
    assert (hover["x"], hover["y"]) == (65, 55) and 0.5 <= hover["seconds"] <= 3
    assert hover["target"] and hover["shot"]  # 按图找头像；画面是悬停之前的主窗口
    assert r["script"]["windows"]["w2"]["popup"] is True
    assert set(r["targets"]) >= {hover["id"]}


def test_short_pause_on_the_way_is_not_a_hover(client):
    """移动途中顿了一下（不到半秒就移开），哪怕那块画面正好在变（比如被挡住的地方有动画）：不算悬停。"""
    import time as _time

    io = client.io
    frames = iter(range(10_000))
    # 每截一次都不一样：画面一直在变
    io.screen = lambda: screen_with(pattern(seed=next(frames) % 7 + 1), at=(150, 100))

    def listen(builder, stop):
        builder.on_move(165, 105)
        _time.sleep(0.35)                 # 顿了一下：已经截了「悬停前」
        builder.on_move(260, 200)         # 不到半秒就移开了
        _time.sleep(0.1)
        builder.on_click(260, 200, "left")
        done.set()
        stop.wait()

    done = threading.Event()
    client.app.state.studio.app.listen = listen
    d = new_script(client)
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": False})
    assert done.wait(10)
    r = client.post("/api/record/stop").json()
    assert [s["action"] for s in r["script"]["steps"]] == ["click"]


def _record_with(client, listen):
    """用这段假操作录一遍，等操作做完再停（盯悬停的线程在停止录制时就退出了）。"""
    done = threading.Event()

    def run(builder, stop):
        listen(builder)
        done.set()
        stop.wait()

    client.app.state.studio.app.listen = run
    d = new_script(client)
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": False})
    assert done.wait(15)
    return client.post("/api/record/stop").json()


def test_hover_with_jitter_and_slow_popup_is_recorded(client):
    """停在头像上手一直在抖，资料卡 1.5 秒才弹出来：照样记成悬停，时长是等到它弹出的时间。"""
    import time as _time

    from cyber_worker.macro.io.base import WindowGeometry, WindowInfo

    io = client.io
    base = io.list_windows()
    card = (WindowInfo("Demo", "资料卡", 200, 150), WindowGeometry(300, 100, 200, 150, 1.0))
    io.screen = lambda: screen_with(pattern())

    def listen(builder):
        builder.on_move(165, 105)
        for i in range(15):                       # 1.5 秒里一直抖 1～2 个像素
            _time.sleep(0.1)
            builder.on_move(165 + (i % 3) - 1, 105 + (i % 2))
        io.list_windows = lambda: [*base, card[0]]  # 1.5 秒后资料卡才弹出来
        _time.sleep(0.5)
        builder.on_click(400, 200, "left", (), card)

    r = _record_with(client, listen)
    io.list_windows = lambda: base
    steps = r["script"]["steps"]
    assert [s["action"] for s in steps] == ["hover", "click"]
    assert 1.4 <= steps[0]["seconds"] <= 2.2


def test_hover_highlight_vanishing_on_leave_is_not_a_change(client):
    """停在会话那一行上（有悬停高亮），什么都没弹出来；移开时高亮消失、画面变了：不算悬停。"""
    import time as _time

    io = client.io
    highlighted = screen_with(pattern(seed=3), at=(20, 80))
    plain = screen_with(None)

    def listen(builder):
        io.screen = lambda: highlighted.copy()
        builder.on_move(165, 145)
        _time.sleep(0.9)                          # 停着：高亮一直在，画面没变
        io.screen = lambda: plain.copy()          # 移开：高亮消失
        builder.on_move(300, 300)
        _time.sleep(0.5)
        builder.on_click(300, 300, "left")

    r = _record_with(client, listen)
    assert [s["action"] for s in r["script"]["steps"]] == ["click"]


def test_rerecording_drops_unused_windows(client):
    """重新录一遍（替换）：以前录过、现在没有步骤用到的窗口去掉；还在用的按这次的尺寸记。"""
    r = _record_two_windows(client)
    sid = r["id"]
    sc = r["script"]
    sc["windows"]["w3"] = {"app": "Demo", "title": "旧窗口", "width": 930, "height": 613}
    sc["windows"]["w2"]["width"] = 999
    client.put(f"/api/scripts/{sid}/detail", json=sc)
    r = client.post("/api/record/start", json={"script_id": sid, "record_delays": False})
    r = client.post("/api/record/stop").json()
    assert set(r["script"]["windows"]) == {"w2"}
    assert r["script"]["windows"]["w2"]["width"] == 300


def test_new_macro_remembers_fixed_window_titles():
    """标题固定（「QQ」）就记下来，以后再开聊天窗口也认得出主窗口；带文件名的标题只在开着好几个时才用。"""
    from cyber_worker.macro.io.base import WindowInfo
    from cyber_worker.studio.state import window_json

    assert window_json(WindowInfo("QQ", "QQ", 320, 740), set())["match_title"] == "QQ"
    assert window_json(WindowInfo("QQ", "My Sky等2个会话", 680, 613), set())["match_title"] == "My Sky"
    assert window_json(WindowInfo("Notepad", "a.txt - 记事本", 800, 600), set())["match_title"] is None
    assert window_json(WindowInfo("Notepad", "a.txt - 记事本", 800, 600), {"notepad"})["match_title"] == "a.txt - 记事本"


def test_window_reference_uses_its_first_step_frame(client, tmp_path):
    """录制中点 ✕ 关掉聊天窗口：之后后台截到的不是它（以前退回截屏幕区域，拍到 studio 和主窗口）。
    参考图用它第一步按下那一刻的画面。"""
    import time as _time

    import numpy as np

    from cyber_worker.macro.io.base import WindowGeometry, WindowInfo
    from cyber_worker.macro.screenshot.storage import read_image

    io = client.io
    chat_img = screen_with(pattern(seed=7), size=(300, 200))
    garbage = np.zeros((200, 300, 3), np.uint8)
    state = {"closed": False}
    io.capture = lambda geo: ((garbage if state["closed"] else chat_img).copy() if geo.width == 300
                              else screen_with(pattern()))
    chat = (WindowInfo("Demo", "My Sky", 300, 200), WindowGeometry(150, 80, 300, 200, 1.0))

    def listen(builder):
        builder.on_click(120, 80, "left")
        _time.sleep(0.2)
        builder.on_click(150 + 280, 80 + 10, "left", (), chat)  # 点 ✕
        _time.sleep(0.2)
        state["closed"] = True  # 窗口关了：之后后台截到的都是乱图
        _time.sleep(1.2)

    r = _record_with(client, listen)
    ref = tmp_path / "ref.png"
    ref.write_bytes(client.get(r["frames"]["reference"]["w2"]["url"]).content)
    assert np.array_equal(read_image(ref), chat_img)


def test_window_shows_its_first_step_frame_over_a_stale_reference(client, tmp_path):
    """旧版本录的宏，资料卡的参考图是它关掉后截到的乱图：画布上优先显示它第一步的画面。"""
    r = _record_two_windows(client)
    sc = r["script"]
    lib = tmp_path / "user_data/drafts/demo/templates"
    (lib / sc["windows"]["w2"]["reference"]).write_bytes(b"\x89PNG stale")  # 存下来的旧参考图（乱的）
    d = client.get(f"/api/scripts/{r['id']}/detail").json()
    first_w2 = next(s for s in sc["steps"] if s.get("win") == "w2")
    assert d["frames"]["reference"]["w2"]["url"].split("&v=")[0] == d["shots"][first_w2["id"]]["url"].split("&v=")[0]
    # 在这张画面上框锚点，截的也是它
    a = client.post(f"/api/scripts/{r['id']}/anchors", json={
        "page": "聊天", "x": 5, "y": 5, "w": 40, "h": 30, "frame": "reference", "win": "w2"})
    assert a.status_code == 200, a.text


def test_click_far_from_its_target_is_a_problem(client):
    """按图找的点击，点击位置被挪到目标框外面（以前点一下画面就会挪）：执行时会点空，执行前就提示。"""
    d = new_script(client)
    sc = d["script"]
    target = {"image": "t.png", "x": 686, "y": 609, "w": 48, "h": 40}
    sc["steps"] = [
        {"id": "s01", "action": "click", "x": 710, "y": 629, "target": target},    # 在目标上
        {"id": "s02", "action": "click", "x": 192.5, "y": 539, "target": target},  # 挪到输入框里了
        {"id": "s03", "action": "click", "x": 192.5, "y": 539, "target": target, "use_target": False},  # 固定坐标不管
    ]
    r = client.put(f"/api/scripts/{d['id']}/detail", json=sc).json()
    bad = {p["step"] for p in r["problems"] if "不在目标图上" in p["message"]}
    assert bad == {"s02"}


def test_delays_are_not_steps(client, tmp_path):
    """延迟不算「步」：动作编号 s01、s02…，延迟单独 d01、d02…；保存、录制、打开旧宏都统一成这样。"""
    from cyber_worker.macro.schema import Script

    d = new_script(client)
    sc = d["script"]
    sc["steps"] = [
        {"id": "s01", "action": "click", "x": 1, "y": 1},
        {"id": "s02", "action": "wait", "seconds": 1},
        {"id": "s03", "action": "key", "combo": "enter"},
        {"id": "s09", "action": "wait", "seconds": 2},
        {"id": "s04", "action": "click", "x": 2, "y": 2},
    ]
    r = client.put(f"/api/scripts/{d['id']}/detail", json=sc).json()
    assert [s["id"] for s in r["script"]["steps"]] == ["s01", "d01", "s02", "d02", "s03"]

    # 以前的宏（延迟也占 s 号）：打开时统一改过来并存回去
    path = tmp_path / "user_data/drafts/demo/templates/comment.yaml"
    old = Script.load(path)
    for i, s in enumerate(old.steps, 1):
        s.id = f"s{i:02d}"
    old.dump(path)
    got = client.get(f"/api/scripts/{d['id']}/detail").json()
    assert [s["id"] for s in got["script"]["steps"]] == ["s01", "d01", "s02", "d02", "s03"]
    assert [s.id for s in Script.load(path).steps] == ["s01", "d01", "s02", "d02", "s03"]

    # 录制（带延迟）：一样的编号
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": True})
    rec = client.post("/api/record/stop").json()
    ids = [s["id"] for s in rec["script"]["steps"]]
    acts = [s["action"] for s in rec["script"]["steps"]]
    assert [i for i, a in zip(ids, acts) if a == "wait"] == [f"d{k:02d}" for k in range(1, acts.count("wait") + 1)]
    assert [i for i, a in zip(ids, acts) if a != "wait"] == [f"s{k:02d}" for k in range(1, len(acts) - acts.count("wait") + 1)]
    if rec["auto_send"]:
        assert rec["auto_send"] in ids


def test_rerecording_reuses_matching_page_and_drops_unused_ones(client, tmp_path):
    """重新录一遍（替换）：同一个窗口里「页面1」还对得上就沿用，不再多出一个几乎一样的「页面2」；
    没有步骤再用的页面清出去（画面上不显示），锚点图留着：撤销回来的步骤用到它就放回来。"""
    first = _record_two_windows(client)
    assert first["auto_anchor"]["pages"] == ["页面1"]
    sid = first["id"]
    lib = tmp_path / "user_data/drafts/demo/templates"
    # 再加一个谁都不用的页面
    client.post(f"/api/scripts/{sid}/capture")
    client.post(f"/api/scripts/{sid}/anchors", json={"page": "旧页面", "x": 50, "y": 40, "w": 40, "h": 30})
    old_img = lib / "comment.anchors/旧页面_1.png"
    assert old_img.exists()

    client.post("/api/record/start", json={"script_id": sid, "record_delays": False})
    again = client.post("/api/record/stop").json()
    assert again["auto_anchor"]["reused"] == ["页面1"]
    assert set(again["pages"]) == {"页面1"}
    assert again["dropped_pages"] == ["旧页面"] and old_img.exists()
    assert {s.get("expected_page") for s in again["script"]["steps"] if s["action"] == "type"} == {"页面1"}

    # Ctrl+Z：界面写回的旧步骤用到「旧页面」，它自动回到页面列表
    old = again["script"]
    next(s for s in old["steps"] if s["action"] == "type")["expected_page"] = "旧页面"
    back = client.put(f"/api/scripts/{sid}/detail", json=old).json()
    assert "旧页面" in back["pages"] and not any("还没有锚点" in p["message"] for p in back["problems"])


def test_old_unused_images_are_cleaned_up_after_a_few_days(client, tmp_path):
    """没人引用、而且放了好几天的旧图（重新录制留下的 s01-2.png 之类、清出去很久的页面）启动时删掉；
    还在用的、刚产生的（撤销还要用）不动。"""
    import time as _time

    from cyber_worker.studio.state import GARBAGE_AGE, collect_garbage

    r = _record_two_windows(client)
    sid = r["id"]
    st = client.app.state.studio
    path = st.script_path(sid)
    stale = path.parent / "comment.shots/s01-9.png"
    stale.write_bytes((path.parent / r["script"]["steps"][0]["shot"]).read_bytes())
    assert collect_garbage(path) == 0  # 刚产生的：撤销可能还要用
    later = _time.time() + GARBAGE_AGE + 60
    assert collect_garbage(path, now=later) == 1 and not stale.exists()
    d = client.get(f"/api/scripts/{sid}/detail").json()
    assert all(client.get(s["url"]).status_code == 200 for s in d["shots"].values())  # 在用的都还在
    assert client.get(d["frames"]["reference"]["main"]["url"]).status_code == 200


def test_only_local_pages_can_talk_to_studio(client):
    """别的网页（带别的 Origin）、DNS 重绑定（Host 是别的域名）都被拒绝；本机页面照常。"""
    from starlette.websockets import WebSocketDisconnect

    assert client.get("/api/status").status_code == 200
    assert client.post("/api/panic", headers={"Origin": "http://127.0.0.1:9999"}).status_code == 200
    assert client.post("/api/panic", headers={"Origin": "http://localhost:9999"}).status_code == 200
    assert client.post("/api/panic", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/panic", headers={"Origin": "null"}).status_code == 403
    assert client.get("/api/scripts", headers={"Host": "evil.example:9999"}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(WS, headers={"Origin": "https://evil.example"}):
            pass
    with client.websocket_connect(WS, headers={"Origin": "http://127.0.0.1:9999"}):
        pass


def _clear_pages(client, sid):
    """把输入 / 发送步骤上的页面去掉，回到「还没设在哪个画面执行」。"""
    script = client.get(f"/api/scripts/{sid}/detail").json()["script"]
    for s in script["steps"]:
        s.pop("expected_page", None)
    return client.put(f"/api/scripts/{sid}/detail", json=script).json()


def test_auto_page_button_fixes_a_step_without_page(client):
    """步骤上「自动选一个」：同一窗口里还对得上的页面就沿用；没有页面就从录制画面里挑一块新建，并填给这一步。"""
    r = client.post("/api/record/start", json={"script_id": new_script(client)["id"], "record_delays": False})
    assert r.status_code == 200
    sid = client.post("/api/record/stop").json()["id"]
    d = _clear_pages(client, sid)
    typed = next(s for s in d["script"]["steps"] if s["action"] == "type")
    assert d["problems"]

    got = client.post(f"/api/scripts/{sid}/auto-page", json={"step_id": typed["id"]}).json()
    assert got["auto_page"]["reused"] and got["auto_page"]["page"] == "页面1"
    assert typed["id"] in got["auto_page"]["steps"] and got["problems"] == []

    # 页面删掉以后再点：新挑一块
    client.delete(f"/api/scripts/{sid}/anchors/页面1/0")
    _clear_pages(client, sid)
    got = client.post(f"/api/scripts/{sid}/auto-page", json={"step_id": typed["id"]}).json()
    page = got["auto_page"]["page"]
    assert not got["auto_page"]["reused"] and got["pages"][page]
    assert {s.get("expected_page") for s in got["script"]["steps"] if s["action"] == "type"} == {page}
    assert got["problems"] == []


def test_record_reports_steps_it_could_not_give_a_page(client):
    """画面里挑不出锚点（一片空白）：录完明确告诉界面哪几步没设好，「自动选一个」也说清楚该怎么办。"""
    import numpy as np

    client.io.screen = lambda: np.full((300, 400, 3), 255, np.uint8)
    client.post("/api/record/start", json={"script_id": new_script(client)["id"], "record_delays": False})
    r = client.post("/api/record/stop").json()
    typed = next(s for s in r["script"]["steps"] if s["action"] == "type")
    assert typed["id"] in r["auto_anchor"]["missing"] and not typed.get("expected_page")
    bad = client.post(f"/api/scripts/{r['id']}/auto-page", json={"step_id": typed["id"]})
    assert bad.status_code == 400 and "我自己框" in bad.json()["detail"]


def test_chimes_can_be_switched_off(client, tmp_path):
    """设置里关掉「录制提示音」「执行提示音」：录制开始 / 停止、空跑都不再响；立即生效，写进 config.toml。"""
    r = client.patch("/api/settings", json={"record_chime": False, "run_chime": False}).json()
    assert r["record_chime"] is False and r["run_chime"] is False
    assert "record_chime = false" in (tmp_path / "config.toml").read_text(encoding="utf-8")

    d = new_script(client)
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": False})
    client.post("/api/record/stop")
    with client.websocket_connect(WS) as ws:
        client.post(f"/api/scripts/{d['id']}/run", json={"dry_run": True})
        while ws.receive_json().get("state") != "done":
            pass
    assert [c for c in client.io.calls if c[0] == "chime"] == []

    # 只开回录制提示音：录制响，空跑照样不响
    client.patch("/api/settings", json={"record_chime": True})
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": False})
    client.post("/api/record/stop")
    with client.websocket_connect(WS) as ws:
        client.post(f"/api/scripts/{d['id']}/run", json={"dry_run": True})
        while ws.receive_json().get("state") != "done":
            pass
    assert [c for c in client.io.calls if c[0] == "chime"] == [("chime", "start"), ("chime", "stop")]


def test_startup_cleanup_drops_old_latest_frames_and_empty_platforms(client, tmp_path):
    """启动时：上次留下的「最新截图」都删掉（只记在内存里，重启后没人读）；宏删光后剩下的空平台目录也删掉，有文件的不动。"""
    st = client.app.state.studio
    data = tmp_path / "user_data"
    old = data / "frames" / "qq__templates__已删掉的宏__w7.png"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"png")
    empty = data / "platforms" / "chrome" / "templates"
    empty.mkdir(parents=True)
    kept = data / "platforms" / "qq" / "templates" / "readme.txt"
    kept.parent.mkdir(parents=True)
    kept.write_text("x", encoding="utf-8")

    st.latest.clear()  # 刚启动：内存里还没有
    st.collect_garbage()
    assert not old.exists()
    assert not (data / "platforms" / "chrome").exists()
    assert kept.exists()

    # 用着用着（内存里有最新截图）就不碰 frames 目录
    d = new_script(client)
    client.post(f"/api/scripts/{d['id']}/capture")
    frame = next((data / "frames").glob("*.png"))
    st.collect_garbage()
    assert frame.exists()


def test_build_id_changes_when_frontend_files_change(tmp_path):
    """页面靠它认出自己是旧的：前端文件改了指纹就变，没改不变。"""
    from cyber_worker.studio.server import build_id

    (tmp_path / "app.js").write_text("a", encoding="utf-8")
    first = build_id(tmp_path)
    assert build_id(tmp_path) == first
    (tmp_path / "app.js").write_text("ab", encoding="utf-8")
    assert build_id(tmp_path) != first


def test_audit_screenshots_are_served_as_webp(client):
    """执行后的截图是 .webp：审计页能直接打开；别的类型照样拒绝。"""
    d = new_script(client)
    client.post("/api/record/start", json={"script_id": d["id"], "record_delays": False})
    client.post("/api/record/stop")
    with client.websocket_connect(WS) as ws:
        client.post(f"/api/scripts/{d['id']}/run", json={"dry_run": True})
        while ws.receive_json().get("state") != "done":
            pass
    day = client.get("/api/audit/days").json()[0]
    rows = client.get(f"/api/audit?day={day}").json()
    url = next(r["screenshot_url"] for r in (rows["entries"] if isinstance(rows, dict) else rows) if r["screenshot_url"])
    r = client.get(url)
    assert ".webp" in url and r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert client.get("/api/file?path=state.db").status_code == 400


def _macro_with_targets(client):
    """一个草稿宏：一次点击、一次悬停，都按图找（目标是画面上 (50, 40) 那块纹理）。"""
    d = new_script(client)
    sid = d["id"]
    script = d["script"]
    script["steps"] = [{"id": "s01", "action": "click", "x": 70, "y": 55},
                       {"id": "s02", "action": "hover", "x": 70, "y": 55, "seconds": 1}]
    client.put(f"/api/scripts/{sid}/detail", json=script)
    client.post(f"/api/scripts/{sid}/capture")
    for step in ("s01", "s02"):
        r = client.post(f"/api/scripts/{sid}/targets", json={"step_id": step, "x": 50, "y": 40, "w": 40, "h": 30})
        assert r.status_code == 200, r.text
    return sid


def test_moving_a_macro_keeps_hover_targets_and_second_looks(client, tmp_path):
    """宏改了文件名搬进宏库（同名的已经有了 → comment-2）：点击、悬停的目标图和另一种样子，路径都跟着改。"""
    first = new_script(client)
    client.post(f"/api/scripts/{first['id']}/commit")
    sid = _macro_with_targets(client)
    from cyber_worker.macro.anchors import learn_look
    from helpers import pattern as tex

    draft = client.app.state.studio.script_path(sid)
    image = f"{draft.stem}.targets/s01.png"
    assert learn_look(draft, "s01", image, tex(40, 30, seed=5), 1.0)
    saved = client.post(f"/api/scripts/{sid}/commit").json()
    lib = tmp_path / "user_data/platforms/demo/templates"
    click, hover = saved["script"]["steps"]
    for path in (click["target"]["image"], click["target"]["alt"], hover["target"]["image"]):
        assert path.startswith("comment-2.targets/") and (lib / path).exists(), path
    assert saved["targets"]["s02"]  # 悬停的目标图在界面上也看得到


def test_recording_keeps_the_normal_look_of_a_clicked_button():
    """录制时目标图截在按下那一刻（按钮带着按下的颜色）：从前面鼠标不在它上面的画面里截同一块，当作平时的样子。"""
    from types import SimpleNamespace

    import numpy as np

    from cyber_worker.macro.schema import Click, TypeText
    from cyber_worker.studio.routes.record import _normal_look

    normal = screen_with(pattern(40, 30, seed=7), at=(300, 200))
    pressed = normal.copy()
    pressed[200:230, 300:314] = 200  # 按下：左边一截盖上高亮
    steps = [Click(id="s01", x=60, y=50), TypeText(id="s02", text="hi"), Click(id="s03", x=320, y=215)]
    rec = SimpleNamespace(
        frames=[], step_frames={"s02": ("main", 1.0, normal)},
        press_frames={1: ("main", 1.0, normal), 2: ("main", 1.0, pressed)},
        builder=SimpleNamespace(press_of={"s01": 1, "s03": 2}))
    box = (300, 200, 40, 30)
    crop = pressed[200:230, 300:340]
    got = _normal_look(steps, 2, rec, box, 1.0, crop)
    assert got is not None and np.array_equal(got, normal[200:230, 300:340])
    # 按钮平时也长这样（没有悬停效果）：不用另存
    assert _normal_look(steps, 2, rec, box, 1.0, normal[200:230, 300:340].copy()) is None


def test_window_you_typed_into_is_never_a_popup(client):
    """以前录的同名窗口带着「悬停弹出的小窗」标记（资料卡也叫 My Sky）：这次在它里面打了字，就不是小窗，标记去掉。
    小窗执行时不切前台、不改尺寸，聊天窗口要是被当成小窗，字可能打到挡在前面的窗口里。"""
    first = _record_two_windows(client)
    sid = first["id"]
    script = first["script"]
    script["windows"]["w2"]["popup"] = True
    client.put(f"/api/scripts/{sid}/detail", json=script)
    client.post("/api/record/start", json={"script_id": sid, "record_delays": False})
    again = client.post("/api/record/stop").json()
    assert again["script"]["windows"]["w2"].get("popup", False) is False
