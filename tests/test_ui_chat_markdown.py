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
