from cyber_worker.macro.io.base import WindowGeometry
from cyber_worker.macro.recorder import RecordingBuilder
from cyber_worker.macro.schema import Script, Window


def builder():
    t = iter(range(1000))
    return RecordingBuilder(WindowGeometry(100, 50, 400, 300, scale=2.0), now=lambda: float(next(t)))


def test_records_window_relative_steps(tmp_path):
    b = builder()
    b.on_click(120, 80, "left")
    for ch in "hi 你":
        b.on_char(ch)
    b.on_combo("enter")
    b.on_scroll(200, 200, 3)
    b.on_scroll(200, 200, 2)
    s = b.build("demo", Window(app="WeChat", width=400, height=300))
    assert [st.action for st in s.steps] == ["click", "wait", "type", "wait", "key", "wait", "scroll"]
    assert (s.steps[0].x, s.steps[0].y) == (20, 30)
    assert s.steps[2].text == "hi 你"
    assert s.steps[6].dy == 5
    assert [st.id for st in s.steps] == [f"s{i:02d}" for i in range(1, 8)]
    assert s.window.scale == 2.0

    s.dump(tmp_path / "demo.yaml")
    assert Script.load(tmp_path / "demo.yaml").model_dump() == s.model_dump()


def test_ignores_input_after_clicking_outside_window():
    b = builder()
    b.on_click(120, 80, "left")
    b.on_click(900, 900, "left")  # 切到终端
    b.on_combo("ctrl+c")
    b.on_char("x")
    assert [st.action for st in b.steps] == ["click"]


def test_gap_becomes_wait_row():
    b = builder()
    b.on_click(120, 80, "left")  # t=0
    b.on_click(130, 80, "left")  # t=1
    assert [st.action for st in b.steps] == ["click", "wait", "click"]
    assert b.steps[1].seconds == 1.0


def test_guess_send_step():
    from cyber_worker.macro.recorder import guess_send_step
    from cyber_worker.macro.schema import Click, Key, TypeText, Wait

    click, wait = Click(id="a", x=1, y=1), Wait(id="w", seconds=1)
    typed = TypeText(id="t", text="hi")
    assert guess_send_step([click, typed, wait, Click(id="s", x=9, y=9)]) == 3
    assert guess_send_step([click, typed, Key(id="k", combo="enter")]) == 2
    assert guess_send_step([click, typed, Key(id="k", combo="ctrl+a")]) is None
    assert guess_send_step([click, wait]) is None


def test_delays_can_be_skipped_and_ids_continue():
    t = iter(range(1000))
    b = RecordingBuilder(WindowGeometry(100, 50, 400, 300, scale=1.0), now=lambda: float(next(t)),
                         record_delays=False, first_id=7)
    b.on_click(120, 80, "left")
    b.on_click(130, 80, "left")
    assert [(st.action, st.id) for st in b.steps] == [("click", "s07"), ("click", "s08")]


def test_stable_title():
    from cyber_worker.studio.state import stable_title

    assert stable_title("海绵宝宝大战…等2个会话") == "海绵宝宝大战"
    assert stable_title("海绵宝宝大战括约鸡") == "海绵宝宝大战括约鸡"
    assert stable_title("(3) 微信") == "微信"
