from scheduleaid.blocks import html_to_blocks

BASE = "https://canvas.example.edu"


def texts(block):
    kind = block["type"]
    return "".join(i["text"]["content"] for i in block[kind]["rich_text"])


def test_empty():
    assert html_to_blocks("") == []
    assert html_to_blocks("   ") == []
    assert html_to_blocks(None) == []


def test_paragraphs_and_whitespace():
    blocks = html_to_blocks("<p>Hello\n   world</p><p>  Second  </p>")
    assert [b["type"] for b in blocks] == ["paragraph", "paragraph"]
    assert texts(blocks[0]) == "Hello world"
    assert texts(blocks[1]) == "Second"


def test_bare_text_becomes_paragraph():
    blocks = html_to_blocks("Just text <b>bold</b>")
    assert len(blocks) == 1
    items = blocks[0]["paragraph"]["rich_text"]
    assert items[0]["text"]["content"] == "Just text "
    assert items[1]["text"]["content"] == "bold"
    assert items[1]["annotations"] == {"bold": True}


def test_relative_links_are_made_absolute():
    blocks = html_to_blocks('<p>See <a href="/courses/1/files/2">hw4.pdf</a></p>', BASE)
    link_item = blocks[0]["paragraph"]["rich_text"][1]
    assert link_item["text"]["link"]["url"] == f"{BASE}/courses/1/files/2"


def test_relative_link_without_base_is_dropped():
    blocks = html_to_blocks('<p><a href="/x">x</a></p>')
    assert "link" not in blocks[0]["paragraph"]["rich_text"][0]["text"]


def test_lists_including_nested():
    html = "<ol><li>One</li><li>Two<ul><li>Two-a</li></ul></li></ol>"
    blocks = html_to_blocks(html)
    assert [(b["type"], texts(b)) for b in blocks] == [
        ("numbered_list_item", "One"),
        ("numbered_list_item", "Two"),
        ("bulleted_list_item", "Two-a"),
    ]


def test_headings_code_quote_divider():
    html = "<h2>Part A</h2><pre>int x = 1;\nx++;</pre><blockquote>Note</blockquote><hr><h5>Small</h5>"
    blocks = html_to_blocks(html)
    assert [b["type"] for b in blocks] == ["heading_3", "code", "quote", "divider", "paragraph"]
    assert texts(blocks[1]) == "int x = 1;\nx++;"
    assert blocks[4]["paragraph"]["rich_text"][0]["annotations"] == {"bold": True}


def test_table():
    html = "<table><tr><th>Part</th><th>Points</th></tr><tr><td>A</td><td>10</td></tr><tr><td>B</td></tr></table>"
    (table,) = html_to_blocks(html)
    assert table["type"] == "table"
    assert table["table"]["table_width"] == 2
    assert table["table"]["has_column_header"] is True
    rows = table["table"]["children"]
    assert len(rows) == 3
    assert rows[2]["table_row"]["cells"][1] == []  # padded


def test_long_text_is_split_under_notion_limit():
    blocks = html_to_blocks("<p>" + "a" * 4500 + "</p>")
    items = blocks[0]["paragraph"]["rich_text"]
    assert [len(i["text"]["content"]) for i in items] == [2000, 2000, 500]


def test_scripts_and_comments_ignored():
    blocks = html_to_blocks("<script>alert(1)</script><!-- hidden --><p>Shown</p>")
    assert [texts(b) for b in blocks] == ["Shown"]


def test_line_breaks_and_images():
    blocks = html_to_blocks('<p>Line 1<br>Line 2 <img src="/img.png" alt="diagram"></p>', BASE)
    assert texts(blocks[0]) == "Line 1\nLine 2 [diagram]"
    assert blocks[0]["paragraph"]["rich_text"][-1]["text"]["link"]["url"] == f"{BASE}/img.png"
