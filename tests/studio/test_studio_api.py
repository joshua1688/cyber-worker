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
    with TestClient(create_app(app, run_idle_sec=0)) as c:
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
    assert d["frames"]["reference"]["url"].startswith("/api/file?path=drafts/demo/templates/comment.png")
    assert client.get(d["frames"]["reference"]["url"]).status_code == 200
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
    assert client.get(saved["frames"]["reference"]["url"]).status_code == 200
    assert client.get(saved["pages"]["页面1"][0]["url"]).status_code == 200  # 锚点图跟着搬过来了
    assert saved["frames"]["latest"]["url"]  # 最新截图也还在
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
    assert client.get(d["frames"]["reference"]["url"]).status_code == 200
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
    assert any("expected_page" in m for m in msgs)
    assert any("{draft}" in m for m in msgs)

    script["steps"] = [{"id": "s01", "action": "teleport"}]
    assert client.put(url, json=script).status_code == 422


def test_capture_then_anchor(client):
    d = new_script(client)
    sid = d["id"]
    assert client.post(f"/api/scripts/{sid}/anchors", json={
        "page": "compose", "x": 50, "y": 40, "w": 40, "h": 30}).status_code == 400  # 还没截图

    d = client.post(f"/api/scripts/{sid}/capture").json()
    assert d["frames"]["latest"]["url"]
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
    with client.websocket_connect("/ws") as ws:
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
    assert r["frames"]["latest"]["url"]  # 录制最后一帧可以直接在画布上看


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

    with client.websocket_connect("/ws") as ws:
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
    with client.websocket_connect("/ws") as ws:
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
    assert client.io.calls == []  # 一下都没动


def test_run_reports_running_once_user_is_idle(client):
    d = new_script(client)
    with client.websocket_connect("/ws") as ws:
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
    assert s["windows"] == {"w2": {"app": "Demo", "title": "My Sky", "width": 300, "height": 200, "scale": 1.0}}
    assert [(x["action"], x.get("win")) for x in s["steps"]] == [("click", None), ("type", "w2"), ("click", "w2")]
    assert s["steps"][0]["count"] == 2
    typed, send = s["steps"][1], s["steps"][2]
    assert send["sends"] and typed["expected_page"] == send["expected_page"]
    assert r["pages"][typed["expected_page"]][0]["win"] == "w2"  # 锚点截自聊天窗口
    assert r["frames"]["latest"]["win"] == "w2"
    assert r["problems"] == []


def test_record_hotkey_toggles_current_macro(client):
    """全局快捷键：没有选中的宏时提示；选中后按一下开始、再按一下停止，结果通过事件推给界面。"""
    from cyber_worker.studio.routes.record import toggle_recording

    st = client.app.state.studio
    assert client.get("/api/status").json()["record_hotkey"] == "alt+r"
    with client.websocket_connect("/ws") as ws:
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
    with client.websocket_connect("/ws") as ws:
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
        ("record", "alt+r", "alt+r"), ("panic", "ctrl+shift+alt+.", "ctrl+shift+alt+.")]

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
    assert sorted(client.hotkeys.bound) == ["ctrl+alt+f10", "ctrl+shift+alt+."]

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
    assert sorted(client.hotkeys.bound) == ["alt+r", "ctrl+shift+alt+."]


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
    with TestClient(create_app(app)) as c:
        s = c.get("/api/status").json()
        assert s["record_hotkey"] == "shift+alt+r"
        assert len(s["hotkey_notes"]) == 1 and "shift+alt+r" in s["hotkey_notes"][0]
    executor.shutdown()


def test_settings_apply_immediately_and_persist(client, tmp_path):
    """设置：默认深色；改了立即生效（执行器、点执行后的等待），写进 config.toml，并推给其他标签页。"""
    s = client.get("/api/settings").json()
    assert s["theme"] == "dark" and s["record_delays"] is True and "language" not in s
    assert s["config_path"].endswith("config.toml") and s["version"]

    with client.websocket_connect("/ws") as ws:
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
    with client.websocket_connect("/ws") as ws:
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
        "comment-2.anchors", "comment-2.pages.yaml", "comment-2.yaml",  # 参考截图没有导出
        "comment.anchors", "comment.pages.yaml", "comment.png", "comment.yaml"]
    d = client.get(f"/api/scripts/{imported['ids'][0]}/detail").json()
    assert client.get(d["pages"]["页面1"][0]["url"]).status_code == 200  # 锚点图跟着改了名
    assert d["frames"]["reference"] is None
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
