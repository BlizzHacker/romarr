"""Chat replies and plugin cards render cleanly."""
from romarr import ui


def test_chat_replies_go_through_the_escaping_markdown_renderer():
    assert "chatAppend(mdLite(m.content),'chat-bot chat-md')" in ui.JS
    assert "n.innerHTML=mdLite(n.dataset.raw)" in ui.JS
    # mdLite escapes before it adds any markup.
    start = ui.JS.index("const mdLite=")
    assert "esc(s)" in ui.JS[start:start + 600]


def test_plugin_card_help_text_has_no_negative_margin():
    assert ".card .pcard p.help{margin:6px 0 0}" in ui.CSS or ".card .pcard p.help{margin:6px 0 0}" in ui.JS


def _md(text):
    """Run the real mdLite (and esc) from ui.JS in node."""
    import json
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node") or "/root/.nvm/versions/node/v20.20.2/bin/node"
    if not shutil.which(node) and not __import__("os").path.exists(node):
        pytest.skip("node is not installed")
    js = ui.JS
    esc = js[js.index("const esc="):js.index("\n", js.index("const esc=") + 60) + 60]
    esc = esc[:esc.index(";", esc.index("[c]))")) + 1]
    start = js.index("const mdLite=")
    end = js.index("\n};\n", start) + 3
    prog = esc + "\n" + js[start:end] + "\nprocess.stdout.write(mdLite(" + json.dumps(text) + "));"
    return subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True).stdout


def test_bold_inside_a_code_span_stays_literal():
    assert "<code>echo &quot;**important**&quot;</code>" in _md('run `echo "**important**"` now')


def test_numbered_steps_become_an_ordered_list():
    html = _md("Do this:\n1. Open settings\n2. Save")
    assert "<ol><li>Open settings</li><li>Save</li></ol>" in html


def test_markup_in_a_reply_is_escaped():
    assert "&lt;script&gt;" in _md("<script>alert(1)</script>")



def test_single_asterisks_render_as_italics_but_bullets_do_not():
    assert "every one was <i>title does not match</i>." in _md("every one was *title does not match*.")
    assert "<ul><li>Score: 82</li></ul>" in _md("* Score: 82")


def test_a_successful_tool_result_with_a_null_error_is_not_failed():
    import json
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node") or "/root/.nvm/versions/node/v20.20.2/bin/node"
    if not shutil.which(node) and not __import__("os").path.exists(node):
        pytest.skip("node is not installed")
    js = ui.JS
    start = js.index("function chatToolFailed(")
    end = js.index("\n}\n", start) + 3
    cases = [json.dumps({"game": "x", "error": None, "results": []}),
             json.dumps({"error": "timed out"}), json.dumps({"declined": True}), "not json"]
    prog = js[start:end] + "\nprocess.stdout.write(JSON.stringify(" + json.dumps(cases) + ".map(chatToolFailed)));"
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [False, True, True, False]
